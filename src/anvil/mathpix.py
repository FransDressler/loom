"""Mathpix OCR client — turn an image or PDF into Mathpix Markdown (MMD).

Images go through the synchronous /v3/text endpoint. PDFs go through the
asynchronous /v3/pdf flow: upload, poll until the job is "completed", then
download the result as Mathpix Markdown. Both return a Markdown string with
math kept as $…$ / $$…$$, ready to drop into an Obsidian note.

Uses only urllib so the package stays dependency-free, matching imessage.py.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid

from . import config

IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
}
PDF_MIMES = {"application/pdf"}

# How Mathpix should wrap math in the returned Markdown — Obsidian renders these.
_MATH_OPTIONS = {
    "math_inline_delimiters": ["$", "$"],
    "math_display_delimiters": ["$$", "$$"],
    "rm_spaces": True,
}
_POLL_INTERVAL = 3  # seconds between PDF status checks


class MathpixError(RuntimeError):
    """A Mathpix request failed, timed out, or credentials are missing."""


def is_configured() -> bool:
    return bool(config.MATHPIX_APP_ID and config.MATHPIX_APP_KEY)


def is_supported(mime: str) -> bool:
    mime = (mime or "").lower()
    return mime in IMAGE_MIMES or mime in PDF_MIMES


def _auth_headers() -> dict[str, str]:
    if not is_configured():
        raise MathpixError("Mathpix credentials are not set (ANVIL_MATHPIX_APP_ID / _APP_KEY).")
    return {"app_id": config.MATHPIX_APP_ID, "app_key": config.MATHPIX_APP_KEY}


def _open(req: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=config.BB_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise MathpixError(f"{req.get_method()} {req.full_url.split('?')[0]} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MathpixError(f"cannot reach Mathpix at {config.MATHPIX_URL}: {exc.reason}") from exc


def _post_json(path: str, body: dict) -> dict:
    url = f"{config.MATHPIX_URL.rstrip('/')}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    for key, value in _auth_headers().items():
        req.add_header(key, value)
    req.add_header("Content-Type", "application/json")
    raw = _open(req)
    try:
        return json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise MathpixError(f"POST {path} returned non-JSON") from exc


def _get_json(path: str) -> dict:
    url = f"{config.MATHPIX_URL.rstrip('/')}{path}"
    req = urllib.request.Request(url, method="GET")
    for key, value in _auth_headers().items():
        req.add_header(key, value)
    raw = _open(req)
    try:
        return json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise MathpixError(f"GET {path} returned non-JSON") from exc


def _get_text(path: str) -> str:
    url = f"{config.MATHPIX_URL.rstrip('/')}{path}"
    req = urllib.request.Request(url, method="GET")
    for key, value in _auth_headers().items():
        req.add_header(key, value)
    return _open(req).decode(errors="replace")


def _multipart(file_field: str, filename: str, file_bytes: bytes, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    bnd = boundary.encode()
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + bnd + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += value.encode() + crlf
    body += b"--" + bnd + crlf
    body += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode() + crlf
    body += b"Content-Type: application/octet-stream" + crlf + crlf
    body += file_bytes + crlf
    body += b"--" + bnd + b"--" + crlf
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def ocr_image(data: bytes, mime: str) -> str:
    """OCR a single image and return its Mathpix Markdown."""
    src = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    body = {"src": src, "formats": ["text"], **_MATH_OPTIONS}
    resp = _post_json("/v3/text", body)
    if resp.get("error"):
        raise MathpixError(f"Mathpix OCR error: {resp.get('error')}")
    return (resp.get("text") or "").strip()


def ocr_pdf(data: bytes, filename: str) -> str:
    """Upload a PDF, wait for the async job, return its Mathpix Markdown."""
    options = json.dumps(_MATH_OPTIONS)
    payload, content_type = _multipart("file", filename or "document.pdf", data, {"options_json": options})

    url = f"{config.MATHPIX_URL.rstrip('/')}/v3/pdf"
    req = urllib.request.Request(url, data=payload, method="POST")
    for key, value in _auth_headers().items():
        req.add_header(key, value)
    req.add_header("Content-Type", content_type)
    try:
        resp = json.loads(_open(req).decode())
    except json.JSONDecodeError as exc:
        raise MathpixError("POST /v3/pdf returned non-JSON") from exc

    pdf_id = resp.get("pdf_id")
    if not pdf_id:
        raise MathpixError(f"Mathpix did not return a pdf_id: {resp.get('error') or resp}")

    deadline = time.monotonic() + config.MATHPIX_PDF_TIMEOUT
    while True:
        status = _get_json(f"/v3/pdf/{pdf_id}")
        state = status.get("status")
        if state == "completed":
            break
        if state == "error":
            raise MathpixError(f"Mathpix PDF conversion failed: {status.get('error') or status}")
        if time.monotonic() > deadline:
            raise MathpixError(f"Mathpix PDF conversion timed out after {config.MATHPIX_PDF_TIMEOUT}s (status: {state}).")
        time.sleep(_POLL_INTERVAL)

    return _get_text(f"/v3/pdf/{pdf_id}.mmd").strip()


def convert(data: bytes, mime: str, filename: str) -> str:
    """Dispatch by MIME type and return Mathpix Markdown."""
    mime = (mime or "").lower()
    if mime in PDF_MIMES:
        return ocr_pdf(data, filename)
    if mime in IMAGE_MIMES:
        return ocr_image(data, mime)
    raise MathpixError(f"unsupported attachment type for OCR: {mime!r}")
