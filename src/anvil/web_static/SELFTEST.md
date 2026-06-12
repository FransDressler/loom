# Atlas-Frontend — Selbsttest-Checkliste

Qualitätsnachweis für `web_static/` gegen die Design-Spec (`/tmp/atlas-spec.txt`,
Dump von `/home/frans/atlas.pen`, Artboards „Atlas — Idle/Listening/Working“)
und den API-Kontrakt. Geprüft am 2026-06-11 gegen den ECHTEN Server
(`anvil.web` mit Test-Vault/-State unter /tmp/atlas-test, Playwright-Chromium
1600×1000 und 1000px schmal). Screenshots: /tmp/atlas-test/shot-*.png.

## Validierung

- [x] `node --check atlas.js` → OK (keine Syntaxfehler)
- [x] HTML-Tag-Balance-Check (eigenes Python-Skript, html.parser) für
      atlas.html und login.html → OK, keine offenen Tags
- [x] Keine Konsolen-/Page-Errors in drei Playwright-Läufen (Login → Idle →
      Working → Overlays → Upload → Deep-Ask → schmaler Viewport)

## Layout & Maße (aus der Spec übernommen)

- [x] Seite 1600×1000-tauglich, Hintergrund #1e1e2e, vertikales Layout:
      Header 72px / BodyRow flexibel / InputSlot 120px (InputBar 80px, r=18)
- [x] Drei rotierte Streak-Linien: mauve 6px −7°, sapphire 4px 5°, pink 5px −10°
      (Opazität .55/.5/.45); Idle-Set umgefärbt (sapphire/teal/blue + 4. Streak
      mauve, Opazität .45/.35/.35/.4) — weicher 600ms-Übergang
- [x] Radialer Glow hinter dem Zentrum (480px, #cba6f7 → transparent; Idle 18%)
- [x] Dezentes kariertes 1px-Grid (40px-Raster, rgba(205,214,244,.028))
- [x] BodyRow: links 300px, Mitte flexibel, rechts 360px; Panels #181825ee,
      r=14, Stroke #45475a, Padding 20 (Mitte 24), gap 20, clip
- [x] Responsiv ≤1100px: Panels stapeln, InputBar sticky am Viewport-Boden
      (Screenshot shot-narrow3.png — kein Überlappen mehr)

## Header (alle Elemente verdrahtet)

- [x] Brand: Logo-Tile 36px r=10, radialer mauve→sapphire-Gradient, Stroke
      #cba6f7AA, Sparkles-Icon; „ATLAS“ 16px/600/ls2 + „obsidian intelligence
      core“ 10px Mono #6c7086
- [x] StatePill 36px r=18: Mini-Orb 10px (Gradient je State: teal/sapphire/
      mauve) + Glow, Label 11px Mono 600 ls2 (IDLE #cdd6f4 / LISTENING
      #74c7ec / WORKING #cba6f7), Divider 1×14, Detail 10px Mono — Detailtexte
      echt („awaiting input“ / „voice channel open · X dB“ /
      „N sub-agents · active“ / „reconnecting…“ bei SSE-Abriss)
- [x] Clock-Chip 32px r=8: ECHTE Uhrzeit UTC, sekündlich (geprüft: 21:50:48 UTC)
- [x] Bell 32px: Ungelesen-Punkt 7px pink @ (20,6) mit 1.5px-Ring — ECHT:
      reply/task-Events seit letztem Öffnen (localStorage `atlasBellSeen`);
      Klick öffnet Notifications-Overlay mit den echten Events (geprüft:
      Dot ging bei reply/task-Event an, nach Öffnen wieder aus)
- [x] History-Btn → Overlay, eigener EventSource auf `/api/events?backlog=1000`
      (geprüft: 7 historische Events erschienen; ES wird beim Schließen
      geschlossen)
- [x] Settings-Btn → Overlay mit echten Integrations-Details aus /api/state
      (23 Zeilen Status-Dot/Name/Detail) + Queue-/Jobs-Zeile + Logout-Button
      (POST /logout via verstecktem Formular)
- [x] Profile-Chip: „Frans“, Gradient-Avatar 26px (pink→mauve, 135°)

## Linkes Panel

- [x] OBSIDIAN-VAULT-Header: Status-Dot grün wenn /api/state erreichbar,
      gelb bei SSE-Reconnect, rot/„offline“ wenn /api/state weg
- [x] 4 Stat-Karten (#313244, r=8): NOTES/LINKS/TAGS/LINKED % — echte Zahlen
      aus state.vault, de-DE-Format (Intl.NumberFormat); Zahlfarben aus Spec
      (#E5E7EB/mauve/pink/sapphire), Labels 9px Mono ls1
- [x] Divider 1px; INTEGRATIONS-Liste: Icon-Tile 32px (Tint-Hintergrund 20 +
      Border 33), Name 12px, Detail 9px Mono, Status-Dot grün (enabled+ok) /
      gelb (enabled, nicht ok) / grau (disabled); Zähler „N ACTIVE“ echt
      (geprüft: „9 ACTIVE“ bei 9 enabled+ok-Features)

## Mittleres Panel

- [x] TopStatusBar „CORE STATE“ (mauve, ls1.5) + Metriken echt aus state.sys:
      CPU x% / MEM x.xGB (sapphire), LATENCY = last_run_ms oder „—“ (pink),
      UPTIME hh:mm:ss (subtext) — geprüft mit Live-Werten
- [x] ORB: Canvas 2D, Fibonacci-Kugel 1400 Punkte (Goldener Winkel), NUR
      fillRect (kein arc/shadowBlur — per grep verifiziert), DPR max 2,
      Palette exakt #f5c2e7/#cba6f7/#74c7ec/#94e2d5/#b4befe/#fab387,
      Tiefen-Alpha über 16 vorquantisierte rgba-Stufen pro Farbe
- [x] Idle: ~340px (R=170), langsame Rotation + Atmen, Label „IDLE“ mit Punkt
      (wie Idle-Artboard) — „STANDING BY“ steht in der Ambient-Karte darunter
- [x] Working: ~280px (R=140), schnellere Rotation, Kern-Glühen (24 helle
      innere Cluster-Punkte à la wp1-8 + CSS-Kern-Puls + Orbit-Ellipse
      180×120 −15°), Label „PROCESSING · 1.4s · 2 streams“ — Sekunden seit
      Working-Beginn und Stream-Zahl ECHT (Playwright-geprüft)
- [x] Listening: Puls = echter Mic-Pegel (RMS→geglättet), Wellenringe
      200/280/360px + 4 Ticks + Puls-Ring + Mic-Glyphe im Zentrum, Label
      „LISTENING · −x dB · de-DE“ mit echter dBFS-Zahl
- [x] Übergänge ~600ms (exponentielle Radius-/Speed-Annäherung + CSS .6s);
      rAF-Loop pausiert bei document.hidden (visibilitychange)
- [x] CURRENT-TASK-Karte NUR im Working-State (display geprüft): Accent-Bar
      3px mauve, Titel = jüngstes task-Event (geprüft: „Draft Q3 product
      strategy memo“), Untertitel = letztes progress/log-Event,
      „RUNNING Xs“ (echte Sekunden) + Indeterminate-Bar statt erfundener %
- [x] LIVE STREAM: roter Live-Dot (blinkend), Zähler echt („15 events ·
      auto-scroll“), Zeilen = SSE-Events mit HH:MM:SS, Icon+Farbe nach kind,
      Auto-Scroll mit Pause-bei-Hover („paused“ im Zähler), DOM-Cap 250
- [x] Idle-Mitte: Ambient-Karte „STANDING BY“ + drei Suggestion-Chips aus der
      Spec als ECHTE Buttons → /api/ask (Summarize today / Plan deep work /
      Find note about Q3)

## Rechtes Panel

- [x] SUB-AGENTS aus state.active_sources: Name=source (letztes Segment,
      kapitalisiert), Sub-Zeile = roher source, Status-Pill Active/Streaming
      (Streaming wenn last_kind text/reply — geprüft), Task-Zeile = kind +
      echtes Alter, Mono-Box mit last_text; Badge „N running“ echt
      (geprüft: „3 running“ nach State-Poll)
- [x] Idle-Zustand: Moon-Icon, „All agents resting“, „Last activity X ago“
      mit echter Zeit aus dem jüngsten Event (kein erfundener „idle pool“)
- [x] TOOL CALLS: kind=="tool"-Events als Karten — Toolname (erstes Wort) +
      Args-Hint in Mono-Box + „time ago“ statt erfundener ms (geprüft:
      „1s ago…1m ago“, 5s-Refresh); Badge „N total“ = state.tools_today
- [x] Ruhe-Variante: „No active calls“ + „recent: N completed today“ (echt) +
      RECENT-Liste der letzten 4 echten Tool-Events mit Uhrzeit HH:MM

## InputBar (jeder Button tut etwas Echtes)

- [x] Mic 56px r=28, radialer Gradient (Idle teal→sapphire wie Idle-Artboard,
      Listening/Working pink→mauve wie Spec), Ring 68px im Listening pulsend;
      echte Spracherkennung webkitSpeechRecognition lang=de-DE, interim →
      Input-Feld; Feature-Detection: ohne Support disabled + title-Hinweis
      (KEIN toter Button)
- [x] Waveform 14 Balken: Listening = echte Mic-Pegel (AnalyserNode fftSize
      256, Frequenzbins → 4–40px), sonst flache Ruhe-Balken (2px, Höhen
      6/5/7/4/… exakt aus der Idle-Spec, #45475a)
- [x] Hint-Zeile 9px Mono ls2 wechselt mit State (IDLE · TAP MIC OR TYPE /
      LISTENING · capturing voice / WORKING · or type to ask); Input 16px,
      Placeholder „Ask Atlas anything…“, Enter sendet
- [x] Attach 42px r=12 → echter Upload via /api/upload (File-Picker, 50MB-
      Client-Check; geprüft: Datei landete real im Ingest-Ordner, Erfolg als
      hervorgehobene Zeile im Stream)
- [x] DEEP-THINK-Toggle 42px: aktiv = sapphire Border + Tint (geprüft,
      classList 'on'); schaltet mode chat↔deep für /api/ask
- [x] Send 56px r=16 (Gradient mauve→sapphire 135°) → POST /api/ask;
      Streaming-Antwort als hervorgehobene Zeile mit Blink-Cursor im LIVE
      STREAM (geprüft im Deep-Modus: Bestätigung gestreamt, echter Task in
      agent-tasks/todo/ angelegt, Working-State ausgelöst)
- [x] Dedupe: getippte User-Zeile + gestreamte Antwort lokal gerendert;
      SSE-Events mit source=="web" und gleichem Text ≤5s unterdrückt
      (geprüft: keine Doppel-Zeilen beim Deep-Ask; Bell/Tools zählen die
      Events trotzdem)

## Zustandsmaschine & Verbindungen

- [x] listening = Mic aktiv (lokal); working = state.running ODER SSE-Event
      jünger 8s; sonst idle — geprüft: Event-Publish → working in <2.5s,
      ~10s nach letztem Event zurück zu idle (StatePill/Orb/Mitte/Hint
      wechseln konsistent über body[data-state])
- [x] SSE-Client: EventSource /api/events; Reconnect MIT letztem Event-id-
      Cursor (?cursor=ino:offset, 2s-Backoff); Signatur-Dedupe gegen
      Backlog-Replay; Verbindungsverlust → Vault-Dot gelb + StatePill-Detail
      „reconnecting…“; 401 bei /api/state → location.reload() (Login)
- [x] /api/state alle 10s gepollt; age_s der Quellen wird zwischen Polls
      fortgeschrieben
- [x] Fonts: Geist/Geist Mono via fontsource-CDN (jsdelivr) mit ehrlichem
      Fallback (system-ui / ui-monospace) — Seite offline benutzbar (Smoke-
      Test lief ohne CDN-Abhängigkeit, keine Fehler); Icons: 29 inline-SVG-
      lucide-Symbole, keine Icon-Font
- [x] login.html: Atlas-Optik (Grid+Glow+Streaks auf #1e1e2e, Card, Logo-
      Tile, Gradient-Button), Feldname exakt name="token", POST /login —
      Login-Flow per Playwright geprüft
- [x] Kein innerHTML mit Event-/API-Daten — alle dynamischen Texte über
      textContent (XSS-sicher)

## Bewusste Abweichungen von den Artboards (ehrlich statt Mock)

- „64%“ am Task-Header → echte „RUNNING Xs“-Anzeige + Indeterminate-Bar
  (eine echte Fortschritts-% existiert nicht)
- Tool-Karten „142ms“ → „time ago“ (Tool-Events tragen keine Laufzeit)
- „INDEXED 99.8%“ → „LINKED %“ aus state.vault.linked_pct (Kontrakt)
- Idle-Sub-Agents „idle pool 3 ready“ → weggelassen (wäre erfunden);
  „Last activity X ago“ bleibt und ist echt
- Platzhalter-Integrationen (Slack/Figma/Notion…) → echte doctor-FEATURES
- Statische wp/lp-Punktwolken der Artboards → ein animierter Canvas-Orb mit
  identischer Palette; Beiwerk (Orbit, Kern-Puls, Ringe, Ticks) als DOM/CSS
