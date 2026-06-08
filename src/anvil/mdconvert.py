"""MarkItDown bridge — turn links, audio, EPub and office/text files into Markdown.

Complements Mathpix: Mathpix is best for math/scientific PDFs and images, while
MarkItDown (Microsoft, open source) handles the things you just want to *send in*:

- YouTube and web links   -> transcript / article as Markdown (convert_uri)
- voice notes / audio     -> transcription                    (transcribe_audio_bytes)
- EPub attachments        -> book text as Markdown            (convert_stream)
- office / text / data    -> docx, pptx, xlsx, csv, html, json, txt, rtf, … as Markdown

Voice notes are the tricky case: WhatsApp sends them as ogg/opus, which
MarkItDown's own audio converter rejects (it only takes wav/mp3/m4a/mp4). So we
transcode any audio to WAV via ffmpeg first, then run it through MarkItDown's
bundled SpeechRecognition stack with a configurable language (config.AUDIO_LANG,
de-DE by default) — short clips/voice memos work well, long files less so. All
conversion failures raise MarkItDownError so callers can fall back gracefully
(and still store the original file).
"""

from __future__ import annotations

import io
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache

from . import config

# Audio MIME types. WhatsApp voice notes arrive as "audio/ogg; codecs=opus" (the
# codecs param is stripped by _clean). Images and PDFs stay with Mathpix.
AUDIO_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/m4a",
    "audio/aac", "audio/wav", "audio/x-wav", "audio/wave", "audio/ogg",
    "audio/opus", "audio/flac", "audio/x-flac", "audio/webm",
    "audio/amr", "audio/3gpp", "audio/x-caf",
}
# Audio-only extensions. The dual-use container extensions (.mp4/.webm/.3gp) are
# deliberately excluded: they are usually *video*, and we only treat them as audio
# when the MIME explicitly says audio/* (see is_audio). This keeps inbound video
# out of speech-to-text (no wasted/privacy-leaking STT call) and in the store-only
# bucket instead.
AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus",
    ".flac", ".amr", ".caf",
}
EPUB_MIMES = {"application/epub+zip", "application/x-epub+zip"}
EPUB_EXTS = {".epub"}

# Office / text / data documents MarkItDown can read (NOT images/PDF — those are
# Mathpix's job). WhatsApp often delivers these as "application/octet-stream" with
# only a filename, so classification falls back to the extension too.
DOC_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/msword",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.ms-excel",
    "text/csv", "application/csv", "text/tab-separated-values",
    "text/html", "application/xhtml+xml",
    "application/json", "application/jsonl", "application/x-ndjson",
    "text/plain", "text/markdown", "application/markdown",
    "application/rtf", "text/rtf",
    "application/vnd.ms-outlook",  # .msg
    "application/xml", "text/xml", "application/rss+xml", "application/atom+xml",
    # NOTE: .zip is deliberately NOT here. A zip is UNPACKED (each member filed on its
    # own via ingest.extract_zip_to_drop), never flattened by MarkItDown into one note.
}
DOC_EXTS = {
    ".docx", ".pptx", ".xlsx", ".xls", ".csv", ".tsv", ".html", ".htm",
    ".json", ".jsonl", ".ndjson", ".txt", ".text", ".md", ".markdown",
    ".rtf", ".msg", ".xml", ".rss", ".atom", ".ipynb",  # .zip excluded — see DOC_MIMES
}

_MIME_EXT = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/flac": ".flac",
    "audio/x-flac": ".flac", "audio/webm": ".webm", "audio/amr": ".amr",
    "audio/3gpp": ".3gp", "audio/x-caf": ".caf",
    "application/epub+zip": ".epub", "application/x-epub+zip": ".epub",
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


def _ext(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def is_audio(mime: str, name: str = "") -> bool:
    """True for audio attachments (voice notes, music) — routed to transcription.

    Video is never audio: a ``video/*`` MIME is rejected outright so an inbound
    video clip (often ``video/mp4`` / ``clip.mp4``) is stored as-is instead of
    being sent to speech-to-text.
    """
    m = _clean(mime)
    if m.startswith("video/"):
        return False
    return m in AUDIO_MIMES or _ext(name) in AUDIO_EXTS


def supports_attachment(mime: str, name: str = "") -> bool:
    """True for attachments MarkItDown should convert (audio, EPub, office/text/data).

    Classifies by MIME *or* filename extension — WhatsApp/WAHA frequently report a
    generic ``application/octet-stream`` for documents, so the extension is the only
    reliable signal. Images and PDFs are deliberately excluded (Mathpix handles them).
    """
    m = _clean(mime)
    ext = _ext(name)
    return (
        is_audio(mime, name)
        or m in EPUB_MIMES or ext in EPUB_EXTS
        or m in DOC_MIMES or ext in DOC_EXTS
    )


def kind_of(mime: str, name: str = "") -> str:
    """Coarse class of a supported attachment: ``audio`` | ``epub`` | ``doc``."""
    if is_audio(mime, name):
        return "audio"
    if _clean(mime) in EPUB_MIMES or _ext(name) in EPUB_EXTS:
        return "epub"
    return "doc"


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


def _to_wav(data: bytes, ext: str) -> bytes:
    """Transcode arbitrary audio (ogg/opus, m4a, amr, …) to 16 kHz mono PCM WAV.

    MarkItDown's bundled transcriber only reads wav/mp3/mp4, so WhatsApp voice
    notes (ogg/opus) have to be transcoded first. ffmpeg reads from a temp file
    (not a pipe) so containers whose index sits at the end — m4a/mp4 — still work.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MarkItDownError(
            "ffmpeg ist nicht installiert — Audio/Sprachnachrichten können nicht "
            "dekodiert werden. Bitte ffmpeg installieren."
        )
    with tempfile.NamedTemporaryFile(suffix=ext or ".bin") as src:
        src.write(data)
        src.flush()
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", src.name, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
            capture_output=True,
        )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode(errors="replace").strip()[:200] or "unbekannter Fehler"
        raise MarkItDownError(f"ffmpeg-Transkodierung fehlgeschlagen: {detail}")
    return proc.stdout


def transcribe_audio_bytes(data: bytes, mime: str, name: str, language: str | None = None) -> str:
    """Transcribe an audio attachment to text in `language` (default config.AUDIO_LANG).

    Uses MarkItDown's SpeechRecognition backend (Google Web Speech) — the same
    stack MarkItDown's AudioConverter uses — but feeds it ffmpeg-transcoded WAV and
    the right language, so ogg/opus voice notes in German actually transcribe.
    Returns "" when no speech is detected (caller decides how to file that).
    """
    try:
        import speech_recognition as sr
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise MarkItDownError(
            "speech_recognition fehlt — markitdown[audio-transcription] installieren."
        ) from exc

    wav = _to_wav(data, _extension(mime, name) or ".ogg")
    recognizer = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(wav)) as source:
        audio = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio, language=language or config.AUDIO_LANG).strip()
    except sr.UnknownValueError:
        return ""  # no intelligible speech — not an error
    except sr.RequestError as exc:
        raise MarkItDownError(f"Spracherkennung nicht erreichbar: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — normalize any backend failure
        raise MarkItDownError(f"{type(exc).__name__}: {exc}") from exc


def convert_bytes(data: bytes, mime: str, name: str) -> str:
    """Convert raw attachment bytes to Markdown.

    Audio is transcribed (via ffmpeg + SpeechRecognition); everything else
    (EPub, docx, pptx, xlsx, csv, html, json, txt, …) goes through MarkItDown.
    """
    if is_audio(mime, name):
        transcript = transcribe_audio_bytes(data, mime, name)
        if not transcript:
            raise MarkItDownError("Keine verständliche Sprache in der Audiodatei erkannt.")
        return transcript

    try:
        from markitdown import StreamInfo

        info = StreamInfo(
            mimetype=_clean(mime) or None,
            extension=_extension(mime, name) or None,
            filename=name or None,
        )
        result = _engine().convert_stream(io.BytesIO(data), stream_info=info)
    except Exception as exc:  # noqa: BLE001 — normalize every backend failure, incl. a
        # missing markitdown/onnxruntime (Intel-mac, where only the audio path is available)
        raise MarkItDownError(f"{type(exc).__name__}: {exc}") from exc
    text = (getattr(result, "text_content", "") or "").strip()
    if not text:
        raise MarkItDownError("MarkItDown returned no text for this file.")
    return text
