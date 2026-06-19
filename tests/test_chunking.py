"""Tests für das fence-bewusste Chunking (loom.chunking) und seine Verdrahtung
im Listener-Sendepfad (_reply, Confirm-Sender) und im Notifier. Netzwerkfrei.

Die zentrale Invariante: JEDER Chunk beginnt mit dem Eigen-Tag (Confirm-/
Proposal-Präfix) und hält das Kanal-Limit nach dessen Längenmaß ein — sonst
Telegram/Discord-API-Fehler bzw. Capture-Schleife im WA_CAPTURE_OWN-Modus.
"""

from __future__ import annotations

import re

from loom import confirm, inbox, listener, notify
from loom.chunking import split_message, utf16_len


def _no_indicator(chunk: str) -> str:
    """Chunk ohne sein angehängtes » (i/n)«-Suffix."""
    return re.sub(r" \(\d+/\d+\)$", "", chunk)


def _fence_lines(chunk: str) -> int:
    return sum(1 for ln in _no_indicator(chunk).split("\n") if ln.strip().startswith("```"))


# --- split_message: Grundverhalten ----------------------------------------------

def test_short_text_is_one_unchanged_chunk():
    assert split_message("Hallo Welt", 100) == ["Hallo Welt"]
    # Mit Präfix: ein Chunk, Präfix vorangestellt, kein (i/n)-Indikator.
    assert split_message("Hallo", 100, prefix="✅ ANVIL · ") == ["✅ ANVIL · Hallo"]
    assert split_message("", 10, prefix="✅ ") == ["✅ "]


def test_open_code_fence_is_closed_and_reopened_with_language():
    code = "\n".join(f"zeile_{i} = {i}" for i in range(60))
    text = f"Vorwort\n```python\n{code}\n```\nNachwort"
    chunks = split_message(text, 200)
    assert len(chunks) > 1
    # Der erste Chunk läuft in den Codeblock hinein und schließt den Fence …
    assert chunks[0].startswith("Vorwort\n```python\n")
    assert _no_indicator(chunks[0]).endswith("\n```")
    # … der Folge-Chunk öffnet ihn mit dem Sprach-Tag neu.
    assert chunks[1].startswith("```python\n")
    # Kein Chunk lässt einen Codeblock offen (gerade Fence-Zahl je Chunk) …
    assert all(_fence_lines(c) % 2 == 0 for c in chunks)
    # … und keine Code-Zeile geht beim Stückeln verloren.
    joined = "\n".join(chunks)
    for i in range(60):
        assert f"zeile_{i} = {i}" in joined
    assert "Nachwort" in chunks[-1]


def test_every_chunk_respects_limit_and_starts_with_prefix():
    prefix = "✅ ANVIL · "
    chunks = split_message(("wort " * 400).strip(), 300, prefix=prefix)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.startswith(prefix)
        assert len(c) <= 300
    # Auch mit eigenem Längenmaß: Emoji zählen doppelt, das Budget hält trotzdem.
    emoji_chunks = split_message("😀" * 500, 100, len_fn=utf16_len, prefix=prefix)
    assert len(emoji_chunks) >= 2
    for c in emoji_chunks:
        assert c.startswith(prefix)
        assert utf16_len(c) <= 100


def test_utf16_len_counts_surrogate_pairs():
    assert utf16_len("") == 0
    assert utf16_len("abc") == 3
    assert utf16_len("äöü") == 3   # Umlaute: BMP, je 1 Unit
    assert utf16_len("𝔘") == 2     # außerhalb der BMP: Surrogat-Paar
    assert utf16_len("😀") == 2
    assert utf16_len("a😀b") == 4


def test_chunk_indicators_count_up():
    text = "\n".join(f"Zeile {i}" for i in range(200))
    chunks = split_message(text, 120)
    n = len(chunks)
    assert n > 1
    for i, c in enumerate(chunks, 1):
        assert c.endswith(f"({i}/{n})")


# --- Listener-Verdrahtung (Fake-Channel, ohne echte Transporte) ------------------

class ChunkChannel(listener.Channel):
    name = "fake"
    label = "Fake"
    max_message_len = 160

    def __init__(self, msgs=()):
        self._msgs = list(msgs)
        self.sent: list[str] = []

    @property
    def chat_id(self):
        return "chat1"

    def fetch(self, cursor):
        return list(self._msgs)

    def normalize(self, raw):  # der Fake liefert bereits normalisierte Dicts
        return raw

    def download(self, att):
        return b""

    def send_text(self, text):
        self.sent.append(text)


def test_reply_chunks_long_message_and_tags_every_chunk():
    ch = ChunkChannel()
    listener._reply(ch, ("lang " * 200).strip())
    assert len(ch.sent) > 1
    for c in ch.sent:
        assert c.startswith(inbox.CONFIRM_PREFIX)  # jeder Chunk wird im Poll geskippt
        assert inbox.is_own_message(c)
        assert len(c) <= ch.max_message_len


def test_send_tagged_keeps_proposal_prefix_on_every_chunk():
    ch = ChunkChannel()
    items = [{"kind": "k", "summary": f"Aktion {i}: " + "x" * 40} for i in range(1, 9)]
    msg = confirm.format_proposal(items)
    assert msg.startswith(confirm.PROPOSAL_PREFIX)
    listener._send_tagged(ch, msg)
    assert len(ch.sent) > 1
    for c in ch.sent:
        assert c.startswith(confirm.PROPOSAL_PREFIX)
        assert inbox.is_own_message(c)  # auch Folge-Chunks: kein Capture-Loop
        assert len(c) <= ch.max_message_len
    # Chunk 1 beginnt exakt wie die ungestückelte Proposal (Präfix-Semantik erhalten).
    assert msg.startswith(_no_indicator(ch.sent[0]))


def test_run_poll_sends_long_agent_reply_in_tagged_chunks(monkeypatch, tmp_path):
    (tmp_path / "v").mkdir()
    for attr, val in {
        "VAULT_PATH": str(tmp_path / "v"), "STATE_DIR": str(tmp_path / "s"),
        "MARKITDOWN": False, "INBOX_SKILLS": False, "CHAT_HISTORY": False,
        "CONTEXT_HINT": False,
    }.items():
        monkeypatch.setattr(listener.config, attr, val)

    long_reply = ("Erledigt.\n```python\n"
                  + "\n".join(f"x{i} = {i}" for i in range(80))
                  + "\n```\nEnde")

    async def fake_run(prompt, options, session_id=None):
        return long_reply

    monkeypatch.setattr(inbox, "run_capture", fake_run)
    ch = ChunkChannel([{"id": "m1", "_sort": 1, "text": "hi",
                        "from_me": False, "system": False, "attachments": []}])
    assert listener.run_poll(ch) == 1
    assert len(ch.sent) > 1
    for c in ch.sent:
        assert c.startswith(inbox.CONFIRM_PREFIX)
        assert len(c) <= ch.max_message_len
        assert _fence_lines(c) % 2 == 0  # kein Chunk lässt einen Codeblock offen


def test_notifier_chunks_long_progress(monkeypatch):
    ch = ChunkChannel()
    monkeypatch.setattr(notify.config, "NOTIFY_CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "_channel", lambda name: ch)
    post = notify.build_notifier()
    post(("schritt " * 100).strip())
    assert len(ch.sent) > 1
    for c in ch.sent:
        assert c.startswith(inbox.CONFIRM_PREFIX)
        assert len(c) <= ch.max_message_len
