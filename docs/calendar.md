# Kalender — Phase 1: Lesen + Workload-Bild (`LOOM_CALENDAR`)

ANVIL spiegelt ein 8-Wochen-Fenster deiner Kalender (Google + öffentliche
ICS-Feeds, z. B. iCloud) in einen lokalen SQLite-Cache
(`~/.local/state/anvil/calendar.db`). Daraus beantwortet der Chat-Agent
„Was steht morgen an? Wie voll ist die Woche?“ (MCP-Tools `calendar_overview`,
`calendar_events`, `calendar_freebusy`). Phase 1 ist **rein lesend** — geschrieben wird erst
in Phase 2, dann ausschließlich über die Bestätigungs-Queue.

Alles ist hinter `LOOM_CALENDAR=1` verriegelt (default aus).

---

## 1. Google Calendar einrichten

### 1.1 GCP-Projekt + API

1. <https://console.cloud.google.com> → neues Projekt (z. B. „anvil-cal“).
2. „APIs & Services → Library“ → **Google Calendar API** aktivieren.

### 1.2 Consent-Screen — ⚠️ Reihenfolge ist entscheidend

> **WICHTIG — ERST PUBLISHEN, DANN AUTORISIEREN.**
>
> Solange der OAuth-Consent-Screen im Publishing-Status **„Testing“** steht,
> verfallen Refresh-Tokens nach **7 Tagen** — der Sync stirbt dann wöchentlich
> still, bis neu autorisiert wird (dieselbe Sorte Falle wie die toten
> Oura-PATs). Deshalb:
>
> 1. „APIs & Services → OAuth consent screen“: External, App-Name, deine Mail.
> 2. Status auf **„In production“** stellen (**Publish App**). Eine
>    Verifizierung durch Google ist **nicht** nötig — die Kalender-Scopes sind
>    nur „sensitive“; es bleibt beim einmaligen „Google hasn’t verified this
>    app“-Hinweis beim Consent (Advanced → weiter), für Single-User irrelevant.
> 3. **Erst danach** `anvil-cal --auth google` ausführen. Tokens, die noch im
>    Testing-Modus ausgestellt wurden, behalten ihre 7-Tage-Frist **auch nach
>    dem Publish** — in dem Fall einfach einmal neu autorisieren.

### 1.3 OAuth-Client + Autorisierung

1. „APIs & Services → Credentials → Create Credentials → OAuth client ID“,
   Typ **„Desktop app“**. Client-ID und -Secret nach `~/.config/anvil/env`
   (chmod 600, nie in den Vault):

   ```sh
   LOOM_GOOGLE_CLIENT_ID=…apps.googleusercontent.com
   LOOM_GOOGLE_CLIENT_SECRET=…
   ```

2. Einmalig autorisieren (Loopback-Callback auf Port `LOOM_CAL_OAUTH_PORT`,
   default 8724; auf einer Headless-Box die Redirect-URL einfach ins Terminal
   einfügen):

   ```sh
   anvil-cal --auth google
   ```

   Die Tokens landen als `fitness_google_tokens.json` in
   `~/.local/state/anvil/` (gleiche atomare Persistenz wie Oura/Strava).
   Google rotiert Refresh-Tokens nicht — die Datei bleibt stabil.

3. Kalender auswählen:

   ```sh
   anvil-cal --calendars          # listet IDs
   # in der env:
   LOOM_CAL_GOOGLE_IDS=primary   # oder kommasepariert mehrere IDs
   ```

---

## 2. iCloud-/sonstige Kalender als ICS-Feed (read-only)

Pro Kalender auf dem iPhone (Kalender-App → Kalender → ⓘ → „Öffentlicher
Kalender“) oder auf icloud.com die öffentliche Freigabe aktivieren und die
`webcal://…`-URL kopieren. Dann in der env:

```sh
LOOM_CAL_ICS_URLS="uni=webcal://p64-caldav.icloud.com/published/2/…,klausuren=https://…"
```

Format: kommasepariert `name=url`; der Name wird zum Kalender-Label im
Workload-Bild (und kann als `LOOM_CAL_EXAM_CALENDAR` dienen).

> ⚠️ **Diese URLs sind Bearer-Geheimnisse**: Wer die URL kennt, kann den
> Kalender vollständig lesen. Sie gehören ausschließlich in
> `~/.config/anvil/env` (chmod 600) — nie in den Vault, nie in Notizen.
> `redact.py` maskiert ICS-/Published-URLs zusätzlich, bevor Text einen
> Chat-Kanal oder das Dashboard verlässt.

Der ICS-Parser expandiert wiederkehrende Termine selbst
(FREQ=DAILY/WEEKLY/MONTHLY mit INTERVAL/BYDAY/COUNT/UNTIL, EXDATE,
RECURRENCE-ID-Overrides; harter 8-Wochen-Horizont). **Exotische Regeln werden
nie still verworfen**: der Serien-Master bleibt als Einzeltermin erhalten und
eine Warnung erscheint im Sync-Log, im Live-Feed und in
`workload()["warnings"]`.

---

## 3. Einschalten + Timer

```sh
# in ~/.config/anvil/env:
LOOM_CALENDAR=1
# optional:
LOOM_CAL_DAY_START=8          # Wachfenster für freie Blöcke
LOOM_CAL_DAY_END=22
LOOM_CAL_EXAM_CALENDAR=       # dedizierter Klausur-Kalender (Name/ID) …
LOOM_CAL_EXAM_PATTERN=klausur|prüfung|exam   # … sonst Titel-Muster

# erster Sync + Blick auf das Ergebnis:
anvil-cal --sync
anvil-cal --status
anvil-cal --workload           # Workload-Bild ab heute als JSON

# Timer (alle 15 min):
cp deploy/loom-calendar.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
anvil-cal --check && systemctl --user enable --now anvil-calendar.timer
```

`anvil-cal --check` ist das Gate fürs `anvil-start.sh`-Muster: Exit 0 nur,
wenn `LOOM_CALENDAR=1` und mindestens eine Quelle nutzbar ist (Google
autorisiert oder ein ICS-Feed konfiguriert).

## 4. Was danach automatisch da ist

- **Chat:** die Read-Tools `calendar_overview` / `calendar_events` /
  `calendar_freebusy` werden über `mcp.build_network_servers()` in jeden
  Chat-Agenten (WhatsApp/Telegram/iMessage/Discord) eingehängt, sobald das
  Flag an ist — zusammen mit den Fitness-Read-Tools.
- **Spätere Phasen** (Coach-Workload, Tagesplan) bauen auf
  `calsync.workload()` auf; das Schreiben (Lernblöcke) ist Teil 2 unten.

---

# Teil 2 — Schreiben via Propose-and-Confirm (`LOOM_CALENDAR_WRITE`)

ANVIL schreibt **ausschließlich** in einen dedizierten Google-Kalender
(„ANVIL Lernplan“) und **ausschließlich nach Bestätigung im Chat** — jede
Aktion läuft über die Bestätigungs-Queue (`confirm.py`), genau wie die
Lösch-Vorschläge des Cleaners. iCloud bleibt read-only; der Lernplan-Kalender
ist über das Google-Konto auf dem iPhone sichtbar.

## 5. Schreib-Kalender anlegen + einschalten

1. Im Google-Konto (Web oder App) einen **neuen Kalender „ANVIL Lernplan“**
   anlegen — nie den primären Kalender als Ziel verwenden.
2. Die ID des neuen Kalenders holen und eintragen:

   ```sh
   anvil-cal --calendars          # listet IDs (…@group.calendar.google.com)
   # in ~/.config/anvil/env:
   LOOM_CALENDAR_WRITE=1
   LOOM_CAL_WRITE_ID=…@group.calendar.google.com
   ```

   **Beide** müssen gesetzt sein — fehlt eines, verweigert der Handler jede
   Aktion, auch eine bereits bestätigte. Der Phase-1-OAuth-Scope
   (`calendar.events`) deckt das Schreiben bereits ab; keine neue
   Autorisierung nötig.

## 6. Bestätigungs-Workflow (Lernblöcke on demand — keine Automatik)

```sh
anvil-cal --plan-week            # oder im Chat: „plan mir Lernblöcke für die Woche“
```

Beides startet **einen** Agent-Lauf (`calsync.run_plan_week`, im Chat das
MCP-Tool `calendar_propose_blocks`): er liest Klausur-Countdowns + freie
Blöcke aus `workload()` und die Budgets aus der Profil-Notiz und erzeugt
konkrete Blöcke. Der Lauf **schlägt nur vor** — die Blöcke landen als
nummerierte Liste in der Bestätigungs-Queue und werden in den Chat getextet.
Jede Zeile trägt das Präfix `[Kalender]` + Datum, damit sie auch in einer
gemischten Liste (z. B. neben Cleaner-Vorschlägen) lesbar bleibt:

```
📋 ANVIL — 3 Aktion(en) zur Bestätigung:
1. [Kalender] Lernblock Di 17.6. 14–16 — MW-Klausur
2. [Kalender] Lernblock Mi 18.6. 9–11 — MW-Klausur
3. …
Antworte mit Nummern (z.B. »1 3«), »alle« oder »keine«.
```

Antwort `1 3` / `alle` / `keine` im Chat führt aus bzw. verwirft.

> ⏱ **TTL vs. Poll-Latenz:** Ein Vorschlag bleibt
> `LOOM_CONFIRM_PENDING_TTL_H` Stunden (Default 48) beantwortbar, danach
> verfällt er still. Die Antwort wird erst vom **nächsten Poll** des Kanals
> verarbeitet — die Ausführung folgt also mit Poll-Latenz, nicht sofort.

## 7. Sicherheitsmodell

- **Nur der eine Kalender:** geschrieben wird ausschließlich in
  `LOOM_CAL_WRITE_ID`; alle anderen Kalender sind für Writes tabu.
- **Eigene-Events-Signatur:** jedes ANVIL-Event trägt
  `extendedProperties.private.anvil="1"`. `update`/`delete` verweigern HART
  alles ohne diese Signatur — ANVIL fasst nie menschliche Termine an, auch
  nicht im eigenen Schreib-Kalender.
- **Idempotenz statt .trash:** ein bestätigter, aber scheinbar fehlgeschlagener
  Write ist nicht vault-recoverable — deshalb deterministische Client-Event-IDs
  (base32hex aus SHA-256 von Titel+Start) plus Get-vor-Insert: ein Retry nach
  Timeout trifft dasselbe Event und kann nie doppeln. Ein versehentlich
  gelöschter Block ist über denselben Vorschlag (gleicher Titel+Start ⇒
  gleiche ID) verlustfrei wieder anlegbar.
