"""Tests for the generic listener engine (loom.listener).

A `FakeChannel` drives `run_poll` so the whole capture pipeline — noise filtering,
attachment routing, history, replies, cursor — is exercised once, independent of any
real service. Also covers the shared outbox sandbox and context block. Network-free.
"""

from __future__ import annotations

import json

import pytest

from loom import inbox, listener, mdconvert


# --- a fake channel ------------------------------------------------------------

class FakeChannel(listener.Channel):
    name = "fake"
    label = "Fake"

    def __init__(self, msgs, *, can_send_media=False, capture_own=False):
        self._msgs = msgs
        self._csm = can_send_media
        self.capture_own = capture_own
        self.sent: list[str] = []
        self.media: list[tuple] = []
        self.committed = False

    @property
    def chat_id(self):
        return "chat1"

    @property
    def can_send_media(self):
        return self._csm

    def fetch(self, cursor):
        return list(self._msgs)

    def normalize(self, raw):  # the fake feeds already-normalized dicts
        return raw

    def download(self, att):
        return b"DATA-" + att["key"].encode()

    def send_text(self, text):
        self.sent.append(text)

    def send_media(self, data, mime, name, caption=""):
        self.media.append((name, mime, caption, data))

    def commit(self):
        self.committed = True


def _msg(mid, sort, **kw):
    base = {"id": mid, "_sort": sort, "text": "", "from_me": False, "system": False, "attachments": []}
    base.update(kw)
    return base


def _att(key, mime, name):
    return {"key": key, "id": key, "mime": mime, "name": name}


# --- run_poll engine -----------------------------------------------------------

def test_run_poll_captures_routes_and_records_history(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    for attr, val in {
        "VAULT_PATH": str(vault), "STATE_DIR": str(tmp_path / "s"),
        "MARKITDOWN": True, "MARKITDOWN_URLS": False, "INBOX_SKILLS": False,
        "CHAT_HISTORY": True, "CHAT_HISTORY_TURNS": 16, "CHAT_HISTORY_MAX_CHARS": 1500,
        "DOC_ASSET_DIR": "attachments",
    }.items():
        monkeypatch.setattr(listener.config, attr, val)

    async def fake_run(prompt, options):
        return "Gespeichert"

    monkeypatch.setattr(inbox, "run_capture", fake_run)
    monkeypatch.setattr(mdconvert, "transcribe_audio_bytes", lambda *a, **k: "transkript")
    monkeypatch.setattr(mdconvert, "convert_bytes",
                        lambda d, m, n: "transkript" if mdconvert.is_audio(m, n) else "## docx")

    msgs = [
        _msg("m1", 100, text="Erste Idee"),
        _msg("m2", 200, attachments=[_att("v.ogg", "audio/ogg; codecs=opus", "ptt.ogg")]),
        _msg("m3", 300, text="Plan", attachments=[_att("p.docx", "application/octet-stream", "plan.docx")]),
        _msg("m4", 400, attachments=[_att("x.dmg", "application/octet-stream", "disk.dmg")]),
        _msg("sys", 250, system=True),
        _msg("own", 260, from_me=True, text="meins"),
    ]
    ch = FakeChannel(msgs)
    captured = listener.run_poll(ch)

    assert captured == 4  # text + voice + docx + dmg (system + own skipped)
    assert sorted(p.name for p in (vault / "attachments").glob("*")) == ["disk.dmg", "plan.docx", "ptt.ogg"]
    # Every sent message is tagged; exactly 4 are the final capture confirmations,
    # the rest are progress one-liners (OCR/convert/store) for the 3 attachments.
    assert all(t.startswith(inbox.CONFIRM_PREFIX) for t in ch.sent)
    assert sum(1 for t in ch.sent if "Gespeichert" in t) == 4
    assert any("transkribiert" in t for t in ch.sent)      # voice progress
    assert any("konvertiert" in t for t in ch.sent)        # office-file progress
    assert ch.committed is True
    state = json.loads((tmp_path / "s" / "fake.json").read_text())["chat1"]
    assert state["cursor"] == 400 and len(state["seen"]) == 6  # cursor past all; every id deduped
    turns = json.loads((tmp_path / "s" / "fake_history.json").read_text())["chat1"]
    assert [t["role"] for t in turns] == ["user", "anvil"] * 4
    assert turns[0]["text"] == "Erste Idee"
    assert "[Anhang: ptt.ogg]" in turns[2]["text"]


def test_run_poll_unpacks_zip_into_drop_folder(monkeypatch, tmp_path):
    import io
    import zipfile

    from loom import ingest

    vault = tmp_path / "v"
    vault.mkdir()
    drop = tmp_path / "dump"
    for attr, val in {
        "VAULT_PATH": str(vault), "STATE_DIR": str(tmp_path / "s"),
        "MARKITDOWN": True, "MARKITDOWN_URLS": False, "INBOX_SKILLS": False,
        "CHAT_HISTORY": True, "CHAT_HISTORY_TURNS": 16, "CHAT_HISTORY_MAX_CHARS": 1500,
        "DOC_ASSET_DIR": "attachments",
    }.items():
        monkeypatch.setattr(listener.config, attr, val)
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(drop))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.pdf", b"%PDF-a")
        zf.writestr("sub/b.png", b"PNG-b")
    zbytes = buf.getvalue()

    ch = FakeChannel([_msg("z1", 100, attachments=[_att("zid", "application/zip", "bundle.zip")])])
    ch.download = lambda att: zbytes  # hand back real zip bytes for this attachment
    captured = listener.run_poll(ch)

    assert captured == 1
    # Each member landed in the drop folder (flattened), NOT as one doc in the vault.
    assert sorted(p.name for p in drop.glob("*")) == ["a.pdf", "b.png"]
    assert not list((vault / "attachments").glob("*.zip"))
    assert any("entpackt" in t for t in ch.sent)  # the chat got an unpack confirmation
    # The zip turn is still recorded in history (so the agent has context).
    turns = json.loads((tmp_path / "s" / "fake_history.json").read_text())["chat1"]
    assert "[Anhang: bundle.zip]" in turns[0]["text"]


def test_is_zip_distinguishes_epub(monkeypatch):
    assert listener._is_zip({"mime": "application/zip", "name": "x.zip"})
    assert listener._is_zip({"mime": "application/octet-stream", "name": "archiv.ZIP"})
    # an EPub is a zip under the hood but must route to MarkItDown, not be unpacked
    assert not listener._is_zip({"mime": "application/epub+zip", "name": "book.epub"})
    assert not listener._is_zip({"mime": "image/png", "name": "p.png"})


def test_run_poll_dedups_seen_across_polls(monkeypatch, tmp_path):
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(tmp_path / "v"))
    monkeypatch.setattr(listener.config, "STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(listener.config, "MARKITDOWN", False)
    monkeypatch.setattr(listener.config, "INBOX_SKILLS", False)
    monkeypatch.setattr(listener.config, "CHAT_HISTORY", False)
    (tmp_path / "v").mkdir()
    runs = []

    async def fake_run(prompt, options):
        runs.append(prompt)
        return "ok"

    monkeypatch.setattr(inbox, "run_capture", fake_run)
    ch = FakeChannel([_msg("m1", 100, text="eins")])
    assert listener.run_poll(ch) == 1
    assert listener.run_poll(ch) == 0  # same message id already in seen -> skipped
    assert len(runs) == 1


def test_run_poll_survives_a_capture_error_and_keeps_dedup(monkeypatch, tmp_path):
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(tmp_path / "v"))
    monkeypatch.setattr(listener.config, "STATE_DIR", str(tmp_path / "s"))
    for attr in ("MARKITDOWN", "INBOX_SKILLS", "CHAT_HISTORY"):
        monkeypatch.setattr(listener.config, attr, False)
    (tmp_path / "v").mkdir()

    async def flaky(prompt, options):
        if "boom" in prompt:
            raise RuntimeError("agent down")
        return "ok"

    monkeypatch.setattr(inbox, "run_capture", flaky)
    ch = FakeChannel([_msg("m1", 100, text="boom"), _msg("m2", 200, text="fine")])
    # m1's capture raises; the engine logs + skips it but still captures m2 and
    # persists BOTH ids to `seen`, so the next poll never re-captures m2.
    assert listener.run_poll(ch) == 1
    assert listener.run_poll(ch) == 0


def test_run_poll_requires_chat(monkeypatch):
    class NoChat(FakeChannel):
        @property
        def chat_id(self):
            return ""

    with pytest.raises(listener.ChannelError):
        listener.run_poll(NoChat([]))


# --- outbox sandbox ------------------------------------------------------------

def test_resolve_vault_file_accepts_in_vault(monkeypatch, tmp_path):
    (tmp_path / "attachments").mkdir()
    target = tmp_path / "attachments" / "skizze.jpg"
    target.write_bytes(b"img")
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(tmp_path))
    assert listener._resolve_vault_file("attachments/skizze.jpg") == target.resolve()


def test_resolve_vault_file_rejects_escape_and_protected(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "attachments").mkdir(parents=True)
    (tmp_path / "secret.txt").write_bytes(b"nope")
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(vault))
    for bad in ("../secret.txt", "../../etc/passwd", ".obsidian/app.json", "missing.jpg"):
        with pytest.raises(listener.ChannelError):
            listener._resolve_vault_file(bad)


def test_resolve_vault_file_allows_file_named_like_venv(monkeypatch, tmp_path):
    (tmp_path / "notes-venv.md").write_bytes(b"x")
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(tmp_path))
    assert listener._resolve_vault_file("notes-venv.md") == (tmp_path / "notes-venv.md").resolve()
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "f.txt").write_bytes(b"x")
    with pytest.raises(listener.ChannelError):
        listener._resolve_vault_file("venv/f.txt")


def test_send_vault_file_size_cap_does_not_read_oversize(monkeypatch, tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 64)
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(listener.config, "OUTBOX_MAX_MB", 0)
    monkeypatch.setattr(type(big), "read_bytes",
                        lambda self: pytest.fail("read_bytes called on an over-cap file"))
    with pytest.raises(listener.ChannelError):
        listener._send_vault_file(FakeChannel([]), "big.bin")


def test_send_vault_file_sends_via_channel(monkeypatch, tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"img-bytes")
    monkeypatch.setattr(listener.config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(listener.config, "OUTBOX_MAX_MB", 16)
    ch = FakeChannel([], can_send_media=True)
    msg = listener._send_vault_file(ch, "a.jpg", "Skizze")
    assert ch.media == [("a.jpg", "image/jpeg", "Skizze", b"img-bytes")]
    assert "a.jpg" in msg


def test_build_outbox_server_and_options(monkeypatch):
    monkeypatch.setattr(listener.config, "INBOX_SKILLS", True)
    ch = FakeChannel([], can_send_media=True)
    assert listener.build_outbox_server(ch) is not None
    assert listener.build_inbox_options(ch) is not None  # composes outbox + queue_skill


def test_full_agent_off_keeps_sandbox(monkeypatch):
    monkeypatch.setattr(listener.config, "INBOX_SKILLS", True)
    monkeypatch.setattr(listener.config, "CODE_SESSIONS", False)
    monkeypatch.setattr(listener.config, "FULL_AGENT", False)
    opts = listener.build_inbox_options(FakeChannel([], can_send_media=True))
    assert opts.permission_mode == "acceptEdits"   # default sandbox
    assert "Bash" not in opts.allowed_tools         # no arbitrary shell when sandboxed


def test_full_agent_on_grants_bash_and_bypass(monkeypatch):
    monkeypatch.setattr(listener.config, "INBOX_SKILLS", True)
    monkeypatch.setattr(listener.config, "CODE_SESSIONS", False)
    monkeypatch.setattr(listener.config, "FULL_AGENT", True)
    opts = listener.build_inbox_options(FakeChannel([], can_send_media=True))
    assert opts.permission_mode == "bypassPermissions"  # no prompts on any tool
    assert "Bash" in opts.allowed_tools                  # full local execution
    assert opts.tools == {"type": "preset", "preset": "claude_code"}  # every built-in available
    # Escalation only UNIONS in the extra power — the curated tools survive.
    assert "Read" in opts.allowed_tools
    assert listener.OUTBOX_TOOL in opts.allowed_tools


# --- context block + name helper -----------------------------------------------

def test_context_block_composition(monkeypatch):
    monkeypatch.setattr(listener.config, "INBOX_SKILLS", True)
    ctx = listener.context_block(FakeChannel([], can_send_media=True),
                                 [{"role": "user", "text": "frage"}])
    assert "queue_skill" in ctx          # skills overview
    assert "send_attachment" in ctx      # send hint (channel can send media)
    assert "Bisheriger Chatverlauf" in ctx
    # No history + no skills + no send -> empty context.
    monkeypatch.setattr(listener.config, "INBOX_SKILLS", False)
    monkeypatch.setattr(listener.config, "FULL_AGENT", False)
    assert listener.context_block(FakeChannel([]), []) == ""
    # With full agent on, the agent is told it may act directly (incl the destructive-
    # action guardrail) even with no skills/history/send capability.
    monkeypatch.setattr(listener.config, "FULL_AGENT", True)
    ctx = listener.context_block(FakeChannel([]), [])
    assert "Systemzugriff" in ctx and "rückfragen" in ctx


def test_name_of_falls_back_to_id_stub():
    assert listener._name_of({"name": "real.pdf", "id": "abc12345xyz"}) == "real.pdf"
    assert listener._name_of({"name": "", "id": "abc12345xyz"}) == "attachment-abc12345"
    assert listener._name_of({"name": "", "id": ""}) == "attachment-file"
    # A nameless WhatsApp voice note gets the .ogg extension from its MIME, so the
    # embedded original audio still plays in Obsidian.
    assert listener._name_of(
        {"name": "", "id": "true_120xyz", "mime": "audio/ogg; codecs=opus"}
    ) == "attachment-true_120.ogg"
