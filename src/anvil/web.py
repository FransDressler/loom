"""Web chat front-end for ANVIL.

Serves a small single-page chat UI and a streaming endpoint that runs each
message through the same agent the iMessage inbox and REPL use. Meant to be
reached from anywhere through a private tunnel (Tailscale) or a Cloudflare
Tunnel rather than by opening a router port — see deploy/ for the setup.

A shared token gates every request: the agent can read and write your entire
vault, so the server refuses to start without ANVIL_WEB_TOKEN set.

    anvil-web            start the server on ANVIL_WEB_HOST:ANVIL_WEB_PORT
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import config
from .agent import build_options, run_stream

# One agent run at a time. The SDK spawns a subprocess that writes into the
# vault under acceptEdits; serialising keeps concurrent captures from clashing.
_run_lock = asyncio.Lock()


def _cookie_value() -> str:
    """The opaque value stored in the auth cookie (never the raw token)."""
    return hashlib.sha256(config.WEB_TOKEN.encode()).hexdigest()


def _authed(request: Request) -> bool:
    if not config.WEB_TOKEN:
        return False
    presented = request.cookies.get(config.WEB_COOKIE, "")
    return hmac.compare_digest(presented, _cookie_value())


# --- pages ---------------------------------------------------------------------

_LOGIN_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANVIL — Login</title>{style}</head>
<body class="center">
  <form class="card" method="post" action="/login">
    <h1>ANVIL</h1>
    <p class="muted">Token eingeben, um fortzufahren.</p>
    {error}
    <input type="password" name="token" placeholder="Token" autofocus
           autocomplete="current-password">
    <button type="submit">Anmelden</button>
  </form>
</body></html>"""

_CHAT_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANVIL</title>{style}</head>
<body>
  <header>
    <span class="brand">ANVIL</span>
    <span class="spacer"></span>
    <button id="reset" class="ghost" title="Neuer Chat">Neu</button>
    <form method="post" action="/logout" style="display:inline">
      <button class="ghost" type="submit">Abmelden</button>
    </form>
  </header>
  <main id="log"></main>
  <form id="composer">
    <textarea id="input" rows="1" placeholder="Gedanke erfassen oder Frage stellen…"
              autofocus></textarea>
    <button id="send" type="submit">Senden</button>
  </form>
<script>
const log = document.getElementById('log');
const input = document.getElementById('input');
const composer = document.getElementById('composer');
const sendBtn = document.getElementById('send');

function bubble(role, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); }
});
document.getElementById('reset').addEventListener('click', () => { log.innerHTML = ''; });

composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  bubble('me', text);
  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;

  const reply = bubble('anvil', '…');
  reply.classList.add('pending');
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    if (resp.status === 401) { location.reload(); return; }
    if (!resp.ok) { reply.textContent = '⚠️ Fehler: ' + resp.status; return; }
    reply.textContent = '';
    reply.classList.remove('pending');
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let got = false;
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      reply.textContent += dec.decode(value, {stream: true});
      got = true;
      log.scrollTop = log.scrollHeight;
    }
    if (!got) reply.textContent = '(keine Antwort)';
  } catch (err) {
    reply.textContent = '⚠️ ' + err;
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
});
</script>
</body></html>"""

_STYLE = """<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.5 -apple-system, system-ui, sans-serif;
  background: #0e0f13; color: #e7e9ee; height: 100dvh; display: flex;
  flex-direction: column; }
body.center { align-items: center; justify-content: center; }
header { display: flex; align-items: center; gap: .5rem; padding: .6rem 1rem;
  border-bottom: 1px solid #23252d; }
.brand { font-weight: 700; letter-spacing: .08em; }
.spacer { flex: 1; }
.ghost { background: none; border: 1px solid #2c2f39; color: #aeb3c0;
  border-radius: 8px; padding: .3rem .7rem; cursor: pointer; font-size: .85rem; }
.ghost:hover { color: #fff; border-color: #3a3e4a; }
main { flex: 1; overflow-y: auto; padding: 1rem; display: flex;
  flex-direction: column; gap: .6rem; }
.msg { max-width: min(46rem, 92%); padding: .6rem .85rem; border-radius: 14px;
  white-space: pre-wrap; word-wrap: break-word; }
.msg.me { align-self: flex-end; background: #2b5cff; color: #fff;
  border-bottom-right-radius: 4px; }
.msg.anvil { align-self: flex-start; background: #1a1c23; border: 1px solid #23252d;
  border-bottom-left-radius: 4px; }
.msg.pending { opacity: .55; }
#composer { display: flex; gap: .5rem; padding: .75rem 1rem;
  border-top: 1px solid #23252d; }
textarea { flex: 1; resize: none; background: #15171d; color: #e7e9ee;
  border: 1px solid #2c2f39; border-radius: 12px; padding: .6rem .8rem;
  font: inherit; }
textarea:focus { outline: none; border-color: #2b5cff; }
#send, .card button { background: #2b5cff; color: #fff; border: none;
  border-radius: 12px; padding: 0 1.1rem; font: inherit; font-weight: 600;
  cursor: pointer; }
#send:disabled { opacity: .5; cursor: default; }
.card { background: #15171d; border: 1px solid #23252d; border-radius: 16px;
  padding: 1.8rem; width: min(22rem, 90vw); display: flex; flex-direction: column;
  gap: .8rem; }
.card h1 { margin: 0; letter-spacing: .08em; }
.muted { color: #8a90a0; margin: 0; font-size: .9rem; }
.card input { background: #0e0f13; color: #e7e9ee; border: 1px solid #2c2f39;
  border-radius: 12px; padding: .7rem .8rem; font: inherit; }
.card button { padding: .7rem; }
.err { color: #ff7b7b; font-size: .85rem; margin: 0; }
</style>"""


def _render_login(error: str = "") -> str:
    # Plain replace, not str.format: the chat page embeds JS with `{}` braces.
    return _LOGIN_PAGE.replace("{style}", _STYLE).replace("{error}", error)


async def homepage(request: Request) -> Response:
    if not _authed(request):
        return HTMLResponse(_render_login())
    return HTMLResponse(_CHAT_PAGE.replace("{style}", _STYLE))


async def login(request: Request) -> Response:
    form = await request.form()
    token = str(form.get("token", ""))
    if config.WEB_TOKEN and hmac.compare_digest(token, config.WEB_TOKEN):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            config.WEB_COOKIE,
            _cookie_value(),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
            secure=request.url.scheme == "https",
        )
        return resp
    return HTMLResponse(_render_login('<p class="err">Falsches Token.</p>'), status_code=401)


async def logout(request: Request) -> Response:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(config.WEB_COOKIE)
    return resp


async def chat(request: Request) -> Response:
    if not _authed(request):
        return Response("unauthorized", status_code=401)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response("bad request", status_code=400)
    message = str(payload.get("message", "")).strip()
    if not message:
        return Response("empty message", status_code=400)

    options = build_options(config.VAULT_PATH, config.MODEL)

    async def body():
        async with _run_lock:
            first = True
            try:
                async for chunk in run_stream(message, options):
                    yield (chunk if first else "\n" + chunk).encode()
                    first = False
            except Exception as exc:  # surface agent failures into the chat
                yield f"\n⚠️ {exc}".encode()

    return StreamingResponse(body(), media_type="text/plain; charset=utf-8")


app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
    ]
)


def main() -> None:
    if not config.WEB_TOKEN:
        print(
            "error: ANVIL_WEB_TOKEN is not set. The web agent can read and write\n"
            "your whole vault, so a token is required. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"',
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import uvicorn
    except ModuleNotFoundError:
        print("error: uvicorn is not installed — run `uv sync`.", file=sys.stderr)
        sys.exit(1)

    print(f"ANVIL web on http://{config.WEB_HOST}:{config.WEB_PORT}", file=sys.stderr)
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
