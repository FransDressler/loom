"""Code-Fence-bewusstes Chunking für ausgehende Chat-Nachrichten.

Adaptiert aus Hermes Agent (NousResearch, MIT):
/home/frans/Projekte/hermes-agent/gateway/platforms/base.py
(truncate_message, utf16_len, _custom_unit_to_cp).

Statt eine lange Antwort hart zu kappen, zerlegt `split_message` sie in
versandfertige Chunks: Splits fallen bevorzugt auf Zeilenumbrüche/Leerzeichen,
ein am Chunk-Ende offener ```-Codeblock wird geschlossen und im Folge-Chunk mit
seinem Sprach-Tag neu geöffnet, mehrteilige Antworten bekommen ein (i/n)-Suffix.
`len_fn` erlaubt dienst-eigene Längenmaße (Telegram misst in UTF-16-Units).
"""

from __future__ import annotations

from collections.abc import Callable

# Platz für das " (XX/XX)"-Suffix, in jedem Chunk-Budget vorgehalten.
_INDICATOR_RESERVE = 10
_FENCE_CLOSE = "\n```"


def utf16_len(s: str) -> int:
    """Länge von `s` in UTF-16-Code-Units (Telegrams 4096er-Limit zählt so).

    Zeichen außerhalb der BMP (Emoji wie 😀, 𝔘, …) belegen als Surrogat-Paar
    ZWEI Units, obwohl Pythons len() sie als ein Zeichen zählt.
    """
    return len(s.encode("utf-16-le")) // 2


def _unit_to_cp(s: str, budget: int, len_fn: Callable[[str], int]) -> int:
    """Größter Codepoint-Offset n mit len_fn(s[:n]) <= budget (Binärsuche)."""
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def split_message(text: str, limit: int, *,
                  len_fn: Callable[[str], int] = len, prefix: str = "") -> list[str]:
    """Zerlege `text` in Chunks, die nach `len_fn` je höchstens `limit` messen.

    KRITISCHE INVARIANTE: `prefix` wird JEDEM Chunk vorangestellt und zählt ins
    Limit. inbox.is_own_message erkennt ANVILs eigene Sendungen am
    startswith(Tag) — ein Folge-Chunk ohne Präfix würde im capture_own-Modus
    (WA_CAPTURE_OWN) als neue User-Nachricht eingefangen (Capture-Schleife).

    Ein kurzer Text kommt unverändert (nur mit Präfix) als einzelner Chunk
    zurück, ohne (i/n)-Indikator.
    """
    _len = len_fn
    if _len(prefix + text) <= limit:
        return [prefix + text]

    chunks: list[str] = []
    remaining = text
    # Endete der vorige Chunk mitten in einem Codeblock, hält dies den Sprach-Tag
    # (ggf. "") zum Neu-Öffnen des Fences; None = kein offener Block.
    carry_lang: str | None = None

    while remaining:
        fence_open = f"```{carry_lang}\n" if carry_lang is not None else ""
        head = prefix + fence_open
        # Der wieder geöffnete Fence braucht einen Zeilenanfang, sonst rendert
        # er hinter dem Präfix nicht als Codeblock.
        if fence_open and prefix and not prefix.endswith("\n"):
            head = prefix + "\n" + fence_open

        # Budget für den Fließtext: Limit minus Präfix/Fence, möglichem
        # Fence-Schluss und dem (i/n)-Indikator.
        headroom = limit - _INDICATOR_RESERVE - _len(head) - _len(_FENCE_CLOSE)
        if headroom < 1:  # Limit kleiner als Präfix+Reserven: überschreiten statt hängen
            headroom = max(1, limit // 2)

        # Der Rest passt komplett in einen letzten Chunk.
        if _len(head) + _len(remaining) <= limit - _INDICATOR_RESERVE:
            chunks.append(head + remaining)
            break

        # headroom ist in len_fn-Einheiten gemessen; zum Slicen braucht es den
        # Codepoint-Offset, der dieses Einheiten-Budget einhält.
        cp_limit = _unit_to_cp(remaining, headroom, _len) if _len is not len else headroom
        cp_limit = max(1, cp_limit)  # Fortschritt erzwingen (sonst Endlosschleife)
        region = remaining[:cp_limit]
        split_at = region.rfind("\n")
        if split_at < cp_limit // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = cp_limit

        # Nicht mitten in einem Inline-Code-Span (`…`) trennen: eine ungerade
        # Zahl unescapter Backticks vor dem Split hieße ein unpaarer Backtick im
        # Chunk (und Parse-Fehler z.B. bei Telegram-MarkdownV2).
        candidate = remaining[:split_at]
        if (candidate.count("`") - candidate.count("\\`")) % 2 == 1:
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                safe_split = max(candidate.rfind(" ", 0, last_bt),
                                 candidate.rfind("\n", 0, last_bt))
                if safe_split > cp_limit // 4:
                    split_at = safe_split

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = head + chunk_body

        # Nur den chunk_body abgehen (nicht das selbst erzeugte head), um zu
        # sehen, ob der Chunk in einem offenen Codeblock endet.
        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk_body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code, lang = False, ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""

        if in_code:
            # Verwaisten Fence schließen, damit der Chunk für sich gültig ist.
            full_chunk += _FENCE_CLOSE
            carry_lang = lang
        else:
            carry_lang = None
        chunks.append(full_chunk)

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)]
    return chunks
