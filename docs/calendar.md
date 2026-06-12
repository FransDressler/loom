# Kalender — Phase 1: Lesen + Workload-Bild (`ANVIL_CALENDAR`)

ANVIL spiegelt ein 8-Wochen-Fenster deiner Kalender (Google + öffentliche
ICS-Feeds, z. B. iCloud) in einen lokalen SQLite-Cache
(`~/.local/state/anvil/calendar.db`). Daraus beantwortet der Chat-Agent
„Was steht morgen an? Wie voll ist die Woche?“ (MCP-Tools `calendar_overview`,
`calendar_events`, `calendar_freebusy`), und das Atlas-Dashboard zeigt einen
kompakten Kalender-Block. Phase 1 ist **rein lesend** — geschrieben wird erst
in Phase 2, dann ausschließlich über die Bestätigungs-Queue.

Alles ist hinter `ANVIL_CALENDAR=1` verriegelt (default aus).

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
   ANVIL_GOOGLE_CLIENT_ID=…apps.googleusercontent.com
   ANVIL_GOOGLE_CLIENT_SECRET=…
   ```

2. Einmalig autorisieren (Loopback-Callback auf Port `ANVIL_CAL_OAUTH_PORT`,
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
   ANVIL_CAL_GOOGLE_IDS=primary   # oder kommasepariert mehrere IDs
   ```

---

## 2. iCloud-/sonstige Kalender als ICS-Feed (read-only)

Pro Kalender auf dem iPhone (Kalender-App → Kalender → ⓘ → „Öffentlicher
Kalender“) oder auf icloud.com die öffentliche Freigabe aktivieren und die
`webcal://…`-URL kopieren. Dann in der env:

```sh
ANVIL_CAL_ICS_URLS="uni=webcal://p64-caldav.icloud.com/published/2/…,klausuren=https://…"
```

Format: kommasepariert `name=url`; der Name wird zum Kalender-Label im
Workload-Bild (und kann als `ANVIL_CAL_EXAM_CALENDAR` dienen).

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
ANVIL_CALENDAR=1
# optional:
ANVIL_CAL_DAY_START=8          # Wachfenster für freie Blöcke
ANVIL_CAL_DAY_END=22
ANVIL_CAL_EXAM_CALENDAR=       # dedizierter Klausur-Kalender (Name/ID) …
ANVIL_CAL_EXAM_PATTERN=klausur|prüfung|exam   # … sonst Titel-Muster

# erster Sync + Blick auf das Ergebnis:
anvil-cal --sync
anvil-cal --status
anvil-cal --workload           # Workload-Bild ab heute als JSON

# Timer (alle 15 min):
cp deploy/anvil-calendar.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
anvil-cal --check && systemctl --user enable --now anvil-calendar.timer
```

`anvil-cal --check` ist das Gate fürs `anvil-start.sh`-Muster: Exit 0 nur,
wenn `ANVIL_CALENDAR=1` und mindestens eine Quelle nutzbar ist (Google
autorisiert oder ein ICS-Feed konfiguriert).

## 4. Was danach automatisch da ist

- **Chat:** die Read-Tools `calendar_overview` / `calendar_events` /
  `calendar_freebusy` werden über `mcp.build_network_servers()` in jeden
  Chat-Agenten (WhatsApp/Telegram/iMessage/Discord) eingehängt, sobald das
  Flag an ist — zusammen mit den Fitness-Read-Tools.
- **Atlas:** `/api/state` enthält einen `calendar`-Block
  (`{today_events, next, busy_hours_today, exams_soon}`), sobald Flag + Cache
  existieren; Termine durchlaufen die Redaction unverstümmelt.
- **Spätere Phasen** (Coach-Workload, Lernblock-Vorschläge, Tagesplan) bauen
  auf `calsync.workload()` auf; `gcal.insert/patch/delete_event` + `freebusy`
  liegen für Phase 2 bereit, werden aber noch nirgends aufgerufen.
