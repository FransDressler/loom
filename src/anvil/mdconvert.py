"""MarkItDown bridge — turn links, audio and EPub into Markdown.

Complements Mathpix: Mathpix is best for math/scientific PDFs and images, while
MarkItDown (Microsoft, open source) handles the things you just want to *send in*:

- YouTube and web links  -> transcript / article as Markdown (convert_uri)
- audio attachments       -> transcription                  (convert_stream)
- EPub attachments        -> book text as Markdown           (convert_stream)

Audio transcription uses SpeechRecognition's free Google endpoint via pydub/ffmpeg
— fine for short clips/voice memos, weaker on long files. All conversion failures
raise MarkItDownError so callers can fall back gracefully.
"""

from __future__ import annotations

import io
import mimetypes
import os
import re
from functools import lru_cache

# Attachment MIME types we route through MarkItDown (audio + EPub). Images and
# PDFs stay with Mathpix; see imessage._document_attachments.
AUDIO_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/m4a",
    "audio/aac", "audio/wav", "audio/x-wav", "audio/wave", "audio/ogg",
    "audio/flac", "audio/x-flac", "audio/webm",
}
EPUB_MIMES = {"application/epub+zip", "application/x-epub+zip"}

_MIME_EXT = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/webm": ".webm", "application/epub+zip": ".epub",
    "application/x-epub+zip": ".epub",
}

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


class MarkItDownError(RuntimeError):
    """A MarkItDown conversion failed."""


@lru_cache(maxsize=1)
def _engine():
    # Imported lazily so the dependency is only needed when conversion runs.
    from markitdown import MarkItDown

    return MarkItDown()


def _clean(mime: str) -> str:
    return (mime or "").lower().split(";")[0].strip()


def supports_attachment(mime: str) -> bool:
    """True for audio/EPub attachments MarkItDown should convert."""
    m = _clean(mime)
    return m in AUDIO_MIMES or m in EPUB_MIMES


def kind_of(mime: str) -> str:
    m = _clean(mime)
    if m in AUDIO_MIMES:
        return "audio"
    if m in EPUB_MIMES:
        return "epub"
    return "datei"


def find_url(text: str) -> str | None:
    """Return the first http(s) URL in `text`, or None.

    Used by the iMessage inbox so a link you text in gets fetched + filed rather
    than captured as a bare URL note.
    """
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;)") if m else None


def _extension(mime: str, name: str) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if ext:
        return ext
    return _MIME_EXT.get(_clean(mime)) or (mimetypes.guess_extension(_clean(mime)) or "")


def convert_url(url: str) -> str:
    """Fetch a URL (YouTube transcript, web article, …) and return Markdown."""
    try:
        result = _engine().convert_uri(url)
    except Exception as exc:  # noqa: BLE001 — normalize every backend failure
        raise MarkItDownError(f"{type(exc).__name__}: {exc}") from exc
    text = (getattr(result, "text_content", "") or "").strip()
    if not text:
        raise MarkItDownError("MarkItDown returned no text for this URL.")
    return text


def convert_bytes(data: bytes, mime: str, name: str) -> str:
    """Convert raw attachment bytes (audio/EPub) to Markdown."""
    from markitdown import StreamInfo

    info = StreamInfo(
        mimetype=_clean(mime) or None,
        extension=_extension(mime, name) or None,
        filename=name or None,
    )
    try:
        result = _engine().convert_stream(io.BytesIO(data), stream_info=info)
    except Exception as exc:  # noqa: BLE001 — normalize every backend failure
        raise MarkItDownError(f"{type(exc).__name__}: {exc}") from exc
    text = (getattr(result, "text_content", "") or "").strip()
    if not text:
        raise MarkItDownError("MarkItDown returned no text for this file.")
    return text
