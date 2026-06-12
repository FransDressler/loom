/* Atlas-UI — Logik. Eine Seite, drei Zustände (idle/listening/working).
   Datenquellen: GET /api/events (SSE), GET /api/state (Poll alle 10s),
   POST /api/ask, POST /api/upload. Keine erfundenen Zahlen: jede Anzeige
   kommt aus einer echten Quelle oder zeigt einen ehrlichen Ersatz. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  // ---- globaler Zustand -----------------------------------------------------------
  var S = {
    state: 'idle',          // idle | listening | working
    micActive: false,
    micLevel: 0,            // 0..1 (geglättet, echter Pegel)
    micDb: null,            // echte dBFS-Zahl
    workingSince: null,     // ms-Epoche des Working-Beginns
    lastEventTs: 0,         // ms-Epoche des jüngsten SSE-Events
    sseOk: false,
    stateOk: false,
    server: null,           // letzter /api/state-Snapshot
    serverAt: 0,            // ms-Epoche des Snapshots (für age_s-Fortschreibung)
    deep: false,
    eventCount: 0,
    streamPaused: false,
    bellSeen: Number(localStorage.getItem('atlasBellSeen') || 0),
    notifEvents: [],        // reply/task-Events (für Bell-Overlay)
    toolEvents: [],         // kind=="tool"-Events, neueste zuerst
    seen: new Set(),        // Dedupe über SSE-Reconnects (Signaturen)
    seenOrder: [],
    echoes: []              // lokal gerenderte Texte (Web-Chat-Dedupe, 5s-Fenster)
  };

  // ---- kleine Helfer ---------------------------------------------------------------
  var nf = new Intl.NumberFormat('de-DE');
  var nf1 = new Intl.NumberFormat('de-DE', { maximumFractionDigits: 1 });

  function fmtClock(d, utc) {
    function p(n) { return String(n).padStart(2, '0'); }
    if (utc) return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function evEpoch(ev) {
    var t = Date.parse(ev.ts || '');
    return isNaN(t) ? 0 : t;
  }

  function fmtAgo(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + 's ago';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function fmtUptime(sec) {
    function p(n) { return String(n).padStart(2, '0'); }
    sec = Math.max(0, Math.floor(sec));
    var d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d > 0) return d + 'd ' + p(h) + ':' + p(m);
    return p(h) + ':' + p(m) + ':' + p(s);
  }

  function fmtMs(ms) {
    if (ms == null) return '—';
    if (ms >= 10000) return Math.round(ms / 1000) + 's';
    if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
    return Math.round(ms) + 'ms';
  }

  function icon(name) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ic');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  var KIND_ICON = {
    user: 'user', text: 'msg', tool: 'term', progress: 'act',
    log: 'file', task: 'task', reply: 'reply', upload: 'upload', kanban: 'task'
  };
  var KIND_COLOR = {
    user: '#f5c2e7', text: '#cba6f7', tool: '#74c7ec', progress: '#b4befe',
    log: '#6c7086', task: '#94e2d5', reply: '#f5c2e7', upload: '#fab387', kanban: '#94e2d5'
  };

  // ---- Uhr (echte Zeit, UTC, sekündlich) --------------------------------------------
  function tickClock() {
    $('clockText').textContent = fmtClock(new Date(), true) + ' UTC';
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ---- Zustandsmaschine -------------------------------------------------------------
  // listening = Mic aktiv (lokal); working = state.running ODER SSE-Event jünger 8s; sonst idle.
  function computeState() {
    if (S.micActive) return 'listening';
    // Eigener Chat-Lauf in Flight: working hart halten — lange LLM-Generierungen
    // liefern minutenlang keine Events, der Timer darf dabei NICHT neu starten.
    if (S.webRunActive) return 'working';
    // Server-running verfällt mit dem Snapshot-Alter — aber das Fenster muss
    // GRÖSSER sein als das Poll-Intervall (10s), sonst flackert working→idle in
    // der Lücke und die RUNNING-Uhr springt auf 0 (gemeldeter Bug).
    var serverFresh = S.server && S.server.running &&
      (Date.now() - (S.serverAt || 0) < 15000);
    var running = serverFresh || (Date.now() - S.lastEventTs < 8000);
    return running ? 'working' : 'idle';
  }

  function applyState() {
    if (document.hidden) return;   // Hintergrund-Tab: keine Timer-Arbeit am DOM
    var next = computeState();
    if (next === S.state) { refreshStateTexts(); return; }
    if (next === 'working' && S.state !== 'working') S.workingSince = Date.now();
    S.state = next;
    document.body.dataset.state = next;
    refreshStateTexts();
    renderTools();
    if (next === 'working') renderTask();
  }

  function refreshStateTexts() {
    var label = $('stateLabel'), detail = $('stateDetail'), hint = $('textHint');
    var n = S.server && S.server.active_sources ? S.server.active_sources.length : 0;
    if (S.state === 'listening') {
      label.textContent = 'LISTENING';
      detail.textContent = !S.sseOk ? 'reconnecting…'
        : 'voice channel open' + (S.micDb != null ? ' · ' + S.micDb + ' dB' : '');
      hint.textContent = 'LISTENING · capturing voice';
      $('orbLabelText').textContent = 'LISTENING · ' + (S.micDb != null ? S.micDb + ' dB' : '— dB') + ' · de-DE';
    } else if (S.state === 'working') {
      label.textContent = 'WORKING';
      detail.textContent = !S.sseOk ? 'reconnecting…'
        : n + ' sub-agent' + (n === 1 ? '' : 's') + ' · active';
      hint.textContent = 'WORKING · or type to ask';
      var secs = S.workingSince ? ((Date.now() - S.workingSince) / 1000).toFixed(1) : '0.0';
      $('orbLabelText').textContent = 'PROCESSING · ' + secs + 's · ' + n + ' stream' + (n === 1 ? '' : 's');
      $('taskElapsed').textContent = 'RUNNING ' + Math.round((Date.now() - (S.workingSince || Date.now())) / 1000) + 's';
    } else {
      label.textContent = 'IDLE';
      detail.textContent = !S.sseOk ? 'reconnecting…' : 'awaiting input';
      hint.textContent = S.transcribing ? 'TRANSCRIBING …' : 'IDLE · TAP MIC OR TYPE';
      $('orbLabelText').textContent = S.transcribing ? 'TRANSCRIBING' : 'IDLE';
    }
  }

  setInterval(applyState, 500);

  // ---- SSE-Client ---------------------------------------------------------------------
  var es = null;

  function sig(ev) { return (ev.ts || '') + '|' + (ev.kind || '') + '|' + (ev.text || ''); }

  function rememberSig(s) {
    S.seen.add(s);
    S.seenOrder.push(s);
    if (S.seenOrder.length > 1500) {
      var drop = S.seenOrder.splice(0, 500);
      for (var i = 0; i < drop.length; i++) S.seen.delete(drop[i]);
    }
  }

  function addEcho(text) {
    S.echoes.push({ text: text, ts: Date.now() });
    if (S.echoes.length > 30) S.echoes.shift();
  }

  function matchesEcho(ev) {
    if (ev.source !== 'web') return false;
    var now = Date.now();
    for (var i = S.echoes.length - 1; i >= 0; i--) {
      var e = S.echoes[i];
      if (now - e.ts > 5000) continue;
      var a = (e.text || '').trim(), b = (ev.text || '').trim();
      if (!a || !b) continue;
      if (a === b || a.indexOf(b) !== -1 || b.indexOf(a.slice(0, 500)) !== -1) return true;
    }
    return false;
  }

  var lastEventId = '';   // SSE-id = (ino:offset)-Cursor des Servers
  var esRetry = null;

  function connectSSE(cursor) {
    if (es) es.close();
    var url = '/api/events' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : '');
    es = new EventSource(url);
    es.onopen = function () { S.sseOk = true; renderVaultDot(); refreshStateTexts(); };
    es.onerror = function () {
      // Manueller Reconnect mit dem letzten Event-id-Cursor: nahtloser
      // Wiedereinstieg ohne Backlog-Replay (Dedupe fängt den Rest).
      S.sseOk = false; renderVaultDot(); refreshStateTexts();
      es.close();
      clearTimeout(esRetry);
      esRetry = setTimeout(function () { connectSSE(lastEventId); }, 2000);
    };
    es.onmessage = function (msg) {
      if (msg.lastEventId) lastEventId = msg.lastEventId;
      var ev;
      try { ev = JSON.parse(msg.data); } catch (e) { return; }
      handleEvent(ev);
    };
  }

  function handleEvent(ev) {
    var s = sig(ev);
    if (S.seen.has(s)) return;     // Backlog-Replay nach Reconnect unterdrücken
    rememberSig(s);
    var ts = evEpoch(ev);
    // Telemetrie (kind=log, z.B. prompt_built jedes Listener-Polls) zählt nicht
    // als Aktivität — sonst pulste der Orb alle zwei Minuten grundlos auf WORKING.
    if (ev.kind !== 'log' && ts > S.lastEventTs) S.lastEventTs = ts;

    if (ev.kind === 'tool') {
      S.toolEvents.unshift(ev);
      if (S.toolEvents.length > 40) S.toolEvents.pop();
      renderTools();
    }
    if (ev.kind === 'reply' || ev.kind === 'task') {
      S.notifEvents.unshift(ev);
      if (S.notifEvents.length > 100) S.notifEvents.pop();
      renderBell();
    }
    if (ev.kind === 'task') renderTask();

    if (!matchesEcho(ev)) addStreamRow(ev, false);
    applyState();
  }

  // ---- LIVE-STREAM ----------------------------------------------------------------------
  var streamBody = $('streamBody');

  function rowFor(ev, highlight) {
    var row = document.createElement('div');
    row.className = 'ev-row' + (highlight ? ' hl' : '');
    var t = document.createElement('span');
    t.className = 'ev-time';
    var d = evEpoch(ev);
    t.textContent = d ? fmtClock(new Date(d), false) : '--:--:--';
    var ic = icon(KIND_ICON[ev.kind] || 'act');
    ic.style.color = KIND_COLOR[ev.kind] || '#a6adc8';
    var x = document.createElement('span');
    x.className = 'ev-text';
    x.textContent = ev.text || '';
    row.appendChild(t); row.appendChild(ic); row.appendChild(x);
    // Maschinell lesbare Quelle/Art am Element — title ist rein kosmetisch
    // (ein Server-source wie "x · user" könnte title-Sniffing spoofen).
    row.dataset.kind = ev.kind || '';
    row.dataset.source = ev.source || '';
    row.dataset.ts = String(evEpoch(ev));
    if (ev.source) row.title = ev.source + ' · ' + (ev.kind || '');
    return row;
  }

  function addStreamRow(ev, highlight) {
    var row = rowFor(ev, highlight);
    streamBody.appendChild(row);
    S.eventCount += 1;
    while (streamBody.children.length > 250) streamBody.removeChild(streamBody.firstChild);
    updateStreamCount();
    if (!S.streamPaused) streamBody.scrollTop = streamBody.scrollHeight;
    return row;
  }

  function updateStreamCount() {
    $('streamCount').textContent = S.eventCount + ' events · ' + (S.streamPaused ? 'paused' : 'auto-scroll');
  }

  streamBody.addEventListener('mouseenter', function () { S.streamPaused = true; updateStreamCount(); });
  streamBody.addEventListener('mouseleave', function () {
    S.streamPaused = false; updateStreamCount();
    streamBody.scrollTop = streamBody.scrollHeight;
  });

  // ---- /api/state Poll ---------------------------------------------------------------------
  function pollState() {
    fetch('/api/state', { credentials: 'same-origin' }).then(function (r) {
      if (r.status === 401) { location.reload(); throw new Error('401'); }
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    }).then(function (data) {
      S.server = data;
      S.serverAt = Date.now();
      S.stateOk = true;
      renderVault(data);
      renderIntegrations(data);
      renderMetrics(data);
      renderAgents();
      renderTools();
      renderVaultDot();
      applyState();
    }).catch(function () {
      S.stateOk = false;
      renderVaultDot();
    });
  }
  pollState();
  setInterval(pollState, 10000);

  function renderVaultDot() {
    var dot = $('vaultDot'), st = $('vaultStatus');
    if (!S.stateOk) {
      dot.className = 'dot dot-red'; st.className = 'vault-status off'; st.textContent = 'offline';
    } else if (!S.sseOk) {
      dot.className = 'dot dot-yellow'; st.className = 'vault-status warn'; st.textContent = 'reconnecting…';
    } else {
      dot.className = 'dot dot-green'; st.className = 'vault-status'; st.textContent = 'Connected';
    }
  }

  function renderVault(data) {
    var v = data.vault || {};
    $('statNotes').textContent = v.notes != null ? nf.format(v.notes) : '—';
    $('statLinks').textContent = v.links != null ? nf.format(v.links) : '—';
    $('statTags').textContent = v.tags != null ? nf.format(v.tags) : '—';
    $('statLinked').textContent = v.linked_pct != null ? nf1.format(v.linked_pct) + '%' : '—';
  }

  // Integrations-Icons & -Tints (deterministisch nach Name)
  var INT_ICONS = [
    [/imessage|whatsapp|telegram|discord/, 'chat'], [/^web$/, 'globe'], [/notify/, 'bell'],
    [/mathpix/, 'file'], [/media/, 'upload'], [/chat-history/, 'msg'], [/cleaner/, 'wrench'],
    [/consolidate|memory/, 'db'], [/confirm/, 'task'], [/context/, 'book'],
    [/retrieve|builder/, 'term'], [/task-queue/, 'task'], [/ingest/, 'inbox'],
    [/research|wiki/, 'book'], [/feynman/, 'mic'], [/fitness/, 'heart'],
    [/code|agent/, 'term'], [/jobs/, 'clock'], [/core/, 'logo']
  ];
  var INT_TINTS = ['#74c7ec', '#f5c2e7', '#cba6f7', '#b4befe', '#94e2d5', '#fab387', '#89b4fa', '#a6e3a1'];

  function intIcon(name) {
    for (var i = 0; i < INT_ICONS.length; i++) {
      if (INT_ICONS[i][0].test(name)) return INT_ICONS[i][1];
    }
    return 'plug';
  }

  function renderIntegrations(data) {
    var list = data.integrations || [];
    var active = list.filter(function (x) { return x.enabled && x.ok; }).length;
    $('intCount').textContent = active + ' ACTIVE';
    var box = $('intList');
    box.textContent = '';
    list.forEach(function (it, i) {
      var row = document.createElement('div');
      row.className = 'int-row';
      var tint = INT_TINTS[i % INT_TINTS.length];
      var tile = document.createElement('div');
      tile.className = 'int-tile';
      tile.style.background = tint + '20';
      tile.style.border = '1px solid ' + tint + '33';
      var ic = icon(intIcon(it.name || ''));
      ic.style.color = tint;
      tile.appendChild(ic);
      var text = document.createElement('div');
      text.className = 'int-text';
      var nm = document.createElement('span');
      nm.className = 'int-name'; nm.textContent = it.name || '?';
      var dt = document.createElement('span');
      dt.className = 'int-detail'; dt.textContent = it.detail || '';
      dt.title = it.detail || '';
      text.appendChild(nm); text.appendChild(dt);
      var dot = document.createElement('span');
      dot.className = 'dot ' + (it.enabled && it.ok ? 'dot-green' : it.enabled ? 'dot-yellow' : 'dot-grey');
      row.appendChild(tile); row.appendChild(text); row.appendChild(dot);
      box.appendChild(row);
    });
  }

  function renderMetrics(data) {
    var sys = data.sys || {};
    $('mCpu').textContent = 'CPU ' + (sys.cpu_pct != null ? Math.round(sys.cpu_pct) + '%' : '—');
    $('mMem').textContent = 'MEM ' + (sys.mem_used_mb != null ? (sys.mem_used_mb / 1024).toFixed(1) + 'GB' : '—');
    $('mLat').textContent = 'LATENCY ' + fmtMs(sys.last_run_ms);
    $('mUp').textContent = 'UPTIME ' + (sys.uptime_s != null ? fmtUptime(sys.uptime_s) : '—');
    $('toolBadge').textContent = (data.tools_today != null ? data.tools_today : '—') + ' total';
  }

  // ---- SUB-AGENTS ------------------------------------------------------------------------
  function renderAgents() {
    var box = $('agentsList');
    box.textContent = '';
    var sources = (S.server && S.server.active_sources) || [];
    var extra = (Date.now() - S.serverAt) / 1000;     // age_s seit Poll fortschreiben
    $('agentBadge').textContent = sources.length + ' running';
    $('agentBadge').className = 'badge' + (sources.length ? '' : ' muted');

    if (!sources.length) {
      var card = document.createElement('div');
      card.className = 'empty-card';
      card.appendChild(icon('moon'));
      var msg = document.createElement('span');
      msg.className = 'empty-msg'; msg.textContent = 'All agents resting';
      var sub = document.createElement('span');
      sub.className = 'empty-sub';
      sub.textContent = S.lastEventTs
        ? 'Last activity ' + fmtAgo(Date.now() - S.lastEventTs)
        : 'No activity recorded yet';
      card.appendChild(msg); card.appendChild(sub);
      box.appendChild(card);
      return;
    }

    var TINTS = ['#74c7ec', '#cba6f7', '#f5c2e7', '#94e2d5', '#fab387', '#b4befe'];
    sources.slice(0, 6).forEach(function (src, i) {
      var tint = TINTS[i % TINTS.length];
      var card = document.createElement('div');
      card.className = 'agent-card';

      var h = document.createElement('div'); h.className = 'agent-h';
      var av = document.createElement('div'); av.className = 'agent-av';
      av.style.background = tint + '1f';
      av.style.border = '1px solid ' + tint + '80';
      var avIc = icon(/chat|imessage|whatsapp|telegram|discord/.test(src.source) ? 'chat'
        : /web/.test(src.source) ? 'globe'
        : /ingest/.test(src.source) ? 'inbox'
        : /task|worker|builder/.test(src.source) ? 'term' : 'act');
      avIc.style.color = tint;
      av.appendChild(avIc);

      var info = document.createElement('div'); info.className = 'agent-info';
      var parts = String(src.source || '?').split(':');
      var nm = document.createElement('span'); nm.className = 'agent-name';
      var last = parts[parts.length - 1] || '?';
      nm.textContent = last.charAt(0).toUpperCase() + last.slice(1);
      var id = document.createElement('span'); id.className = 'agent-id';
      id.textContent = src.source || '?';
      info.appendChild(nm); info.appendChild(id);

      var streaming = src.last_kind === 'text' || src.last_kind === 'reply';
      var pill = document.createElement('div');
      pill.className = 'agent-pill ' + (streaming ? 'streaming' : 'active');
      var pd = document.createElement('span'); pd.className = 'dot';
      var pt = document.createElement('span');
      pt.textContent = streaming ? 'Streaming' : 'Active';
      pill.appendChild(pd); pill.appendChild(pt);
      h.appendChild(av); h.appendChild(info); h.appendChild(pill);

      var child = document.createElement('div'); child.className = 'agent-child';
      var conn = document.createElement('span'); conn.className = 'agent-conn';
      conn.style.background = tint + '40';
      var col = document.createElement('div'); col.className = 'agent-task-col';
      var taskRow = document.createElement('div'); taskRow.className = 'agent-task';
      var tIc = icon(KIND_ICON[src.last_kind] || 'act');
      tIc.style.color = tint;
      var tTx = document.createElement('span'); tTx.className = 'agent-task-text';
      var age = Math.round((src.age_s || 0) + extra);
      tTx.textContent = (src.last_kind || '?') + ' · ' + age + 's ago';
      taskRow.appendChild(tIc); taskRow.appendChild(tTx);
      col.appendChild(taskRow);
      if (src.last_text) {
        var sb = document.createElement('div'); sb.className = 'agent-stream';
        sb.textContent = '> ' + src.last_text.slice(0, 120);
        sb.title = src.last_text;
        col.appendChild(sb);
      }
      child.appendChild(conn); child.appendChild(col);

      card.appendChild(h); card.appendChild(child);
      box.appendChild(card);
    });
  }

  // ---- TOOL CALLS --------------------------------------------------------------------------
  function splitTool(ev) {
    var text = (ev.text || '').trim();
    var sp = text.indexOf(' ');
    if (sp === -1) return { name: text || '?', arg: '' };
    return { name: text.slice(0, sp), arg: text.slice(sp + 1) };
  }

  function renderTools() {
    var box = $('toolsList');
    var recent = $('recentList');
    var hdr = $('recentHdr');
    box.textContent = '';
    recent.textContent = '';

    var now = Date.now();
    var fresh = S.toolEvents.filter(function (ev) { return now - evEpoch(ev) < 120000; });

    if (S.state === 'working' && fresh.length) {
      hdr.hidden = true;
      fresh.slice(0, 5).forEach(function (ev, i) {
        var t = splitTool(ev);
        var card = document.createElement('div');
        card.className = 'tool-card' + (i === 0 ? ' fresh' : '');
        var h = document.createElement('div'); h.className = 'tool-h';
        var ico = document.createElement('div'); ico.className = 'tool-ico';
        ico.appendChild(icon('term'));
        var nm = document.createElement('span'); nm.className = 'tool-name';
        nm.textContent = t.name; nm.title = ev.text || '';
        var ago = document.createElement('span'); ago.className = 'tool-ago';
        ago.textContent = fmtAgo(now - evEpoch(ev));   // ehrlich: time ago statt erfundener ms
        h.appendChild(ico); h.appendChild(nm); h.appendChild(ago);
        card.appendChild(h);
        var arg = document.createElement('div'); arg.className = 'tool-arg';
        arg.textContent = t.arg || (ev.source || '');
        arg.title = t.arg || '';
        card.appendChild(arg);
        box.appendChild(card);
      });
      return;
    }

    // Idle-/Ruhe-Variante: leere Karte + RECENT-Liste der letzten echten Tool-Events
    var card = document.createElement('div');
    card.className = 'tool-empty';
    card.appendChild(icon('term'));
    var col = document.createElement('div'); col.className = 'tool-empty-col';
    var msg = document.createElement('span'); msg.className = 'tool-empty-msg';
    msg.textContent = 'No active calls';
    var sub = document.createElement('span'); sub.className = 'tool-empty-sub';
    var total = S.server && S.server.tools_today != null ? S.server.tools_today : null;
    sub.textContent = total != null ? 'recent: ' + total + ' completed today' : 'recent: —';
    col.appendChild(msg); col.appendChild(sub);
    card.appendChild(col);
    box.appendChild(card);

    var last = S.toolEvents.slice(0, 4);
    hdr.hidden = !last.length;
    last.forEach(function (ev) {
      var t = splitTool(ev);
      var row = document.createElement('div');
      row.className = 'recent-row';
      row.appendChild(icon('task'));
      var tx = document.createElement('span'); tx.className = 'recent-text';
      tx.textContent = t.name + (t.arg ? ' · ' + t.arg : '');
      tx.title = ev.text || '';
      var tm = document.createElement('span'); tm.className = 'recent-time';
      var d = evEpoch(ev);
      tm.textContent = d ? fmtClock(new Date(d), false).slice(0, 5) : '--:--';
      row.appendChild(tx); row.appendChild(tm);
      recent.appendChild(row);
    });
  }

  // "time ago"-Anzeigen regelmäßig auffrischen
  setInterval(function () { renderTools(); renderAgents(); }, 5000);

  // ---- CURRENT TASK --------------------------------------------------------------------------
  function renderTask() {
    // Titel: jüngstes task-Event DES LAUFENDEN ZYKLUS, sonst jüngste User-Nachricht.
    // Frische-Schranke ist Pflicht: ohne sie zeigte die Karte bei jedem Working-
    // Eintritt den letzten task-Titel aus dem Backlog (z.B. den Morgen-Tagesplan)
    // mit neu startender Uhr — als würde dieselbe Task ewig neu beginnen.
    var cutoff = (S.workingSince || Date.now()) - 15000;
    var title = null, sub = null;
    for (var i = 0; i < S.notifEvents.length; i++) {
      var ev = S.notifEvents[i];
      if (ev.kind === 'task' && evEpoch(ev) >= cutoff) { title = ev.text; break; }
    }
    var rows = streamBody.children;
    for (var j = rows.length - 1; j >= 0 && (!title || !sub); j--) {
      var d = rows[j].dataset || {};
      if (Number(d.ts || 0) < cutoff) break;   // Zeilen sind chronologisch — ab hier nur Altes
      var tx = rows[j].querySelector('.ev-text');
      if (!tx) continue;
      if (!title && d.kind === 'user') title = tx.textContent;
      if (!sub && (d.kind === 'progress' || d.kind === 'log')) sub = tx.textContent;
    }
    $('taskTitle').textContent = title || 'Agent run in progress';
    $('taskTitle').title = title || '';
    $('taskSub').textContent = sub || 'streaming events live — see LIVE STREAM below';
  }

  // ---- Bell / Notifications --------------------------------------------------------------------
  function unreadCount() {
    var n = 0;
    for (var i = 0; i < S.notifEvents.length; i++) {
      if (evEpoch(S.notifEvents[i]) > S.bellSeen) n++;
    }
    return n;
  }

  function renderBell() {
    $('notifDot').className = 'notif-dot' + (unreadCount() > 0 ? ' on' : '');
  }

  $('bellBtn').addEventListener('click', function () {
    var body = $('notifBody');
    body.textContent = '';
    if (!S.notifEvents.length) {
      var e = document.createElement('div');
      e.className = 'overlay-empty';
      e.textContent = 'No reply/task events yet.';
      body.appendChild(e);
    } else {
      S.notifEvents.slice(0, 50).forEach(function (ev) { body.appendChild(rowFor(ev, false)); });
    }
    S.bellSeen = Date.now();
    localStorage.setItem('atlasBellSeen', String(S.bellSeen));
    renderBell();
    $('notifOverlay').classList.add('open');
  });

  // ---- History-Overlay (eigener SSE-Stream mit ?backlog=1000) ------------------------------------
  var histES = null;

  $('histBtn').addEventListener('click', function () {
    var body = $('histBody');
    body.textContent = '';
    var empty = document.createElement('div');
    empty.className = 'overlay-empty';
    empty.textContent = 'loading…';
    body.appendChild(empty);
    $('histOverlay').classList.add('open');
    if (histES) histES.close();
    histES = new EventSource('/api/events?backlog=1000');
    var first = true;
    histES.onmessage = function (msg) {
      var ev;
      try { ev = JSON.parse(msg.data); } catch (e) { return; }
      if (first) { body.textContent = ''; first = false; }
      body.appendChild(rowFor(ev, false));
      while (body.children.length > 1000) body.removeChild(body.firstChild);
      body.scrollTop = body.scrollHeight;
    };
    histES.onerror = function () {
      if (first) { empty.textContent = 'no events / connection failed'; }
      // Verbindung wirklich beenden — sonst reconnectet der Browser automatisch
      // und mutiert nach dem Schließen des Overlays stale DOM-Nodes weiter.
      if (histES) { histES.close(); histES = null; }
    };
  });

  // ---- Settings-Overlay ----------------------------------------------------------------------------
  $('setBtn').addEventListener('click', function () {
    var body = $('setBody');
    body.textContent = '';
    if (!S.server) {
      var e = document.createElement('div');
      e.className = 'overlay-empty';
      e.textContent = '/api/state not reachable.';
      body.appendChild(e);
    } else {
      (S.server.integrations || []).forEach(function (it) {
        var row = document.createElement('div');
        row.className = 'set-row';
        var dot = document.createElement('span');
        dot.className = 'dot ' + (it.enabled && it.ok ? 'dot-green' : it.enabled ? 'dot-yellow' : 'dot-grey');
        var nm = document.createElement('span'); nm.className = 'set-name'; nm.textContent = it.name || '?';
        var dt = document.createElement('span'); dt.className = 'set-detail';
        dt.textContent = (it.enabled ? '' : '(disabled) ') + (it.detail || '');
        dt.title = it.detail || '';
        row.appendChild(dot); row.appendChild(nm); row.appendChild(dt);
        body.appendChild(row);
      });
      var q = S.server.queues || {};
      var info = 'queues — builder: ' + (q.builder != null ? q.builder : '—')
        + ' · tasks: ' + (q.tasks != null ? q.tasks : '—')
        + ' · ingest: ' + (q.ingest != null ? q.ingest : '—')
        + ' · confirm: ' + (q.confirm != null ? q.confirm : '—');
      if (S.server.jobs) {
        info += ' — jobs: ' + S.server.jobs.count
          + (S.server.jobs.next_run_at ? ' (next ' + S.server.jobs.next_run_at + ')' : '');
      }
      $('setFootInfo').textContent = info;
    }
    $('setOverlay').classList.add('open');
  });

  $('logoutBtn').addEventListener('click', function () { $('logoutForm').submit(); });

  // ---- Kanban-Overlay (Board als iframe über allem) --------------------------------
  $('boardBtn').addEventListener('click', function () {
    var frame = $('boardFrame');
    if (!frame.src) frame.src = frame.dataset.src;   // lazy: erst beim ersten Öffnen laden
    $('boardOverlay').classList.add('open');
  });

  // ---- Tagesplan (Panel rechts oben): echte Notiz, abhakbar -------------------------
  function renderDayplan(data) {
    var list = $('planList'), badge = $('planBadge');
    list.textContent = '';
    if (!data || !data.exists || !data.items.length) {
      badge.textContent = '—';
      var empty = document.createElement('div');
      empty.className = 'plan-empty';
      empty.textContent = data && data.exists
        ? 'Tagesplan ohne Checkliste (' + (data.rel || '') + ')'
        : 'noch kein Tagesplan heute — kommt morgens automatisch (anvil-dayplan)';
      list.appendChild(empty);
      return;
    }
    badge.textContent = data.done + '/' + data.items.length;
    data.items.forEach(function (item, i) {
      var row = document.createElement('label');
      row.className = 'plan-item' + (item.done ? ' done' : '');
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.className = 'plan-check';
      box.checked = item.done;
      box.addEventListener('change', function () {
        box.disabled = true;
        fetch('/api/dayplan/toggle', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: i })
        }).then(function (r) {
          if (r.status === 401) { location.href = '/'; throw new Error('401'); }
          if (!r.ok) throw new Error('http ' + r.status);
          return r.json();
        }).then(renderDayplan)
          .catch(function () { box.disabled = false; box.checked = !box.checked; });
      });
      var txt = document.createElement('span');
      txt.className = 'plan-text';
      txt.textContent = item.text;
      row.appendChild(box); row.appendChild(txt);
      list.appendChild(row);
    });
  }

  function pollDayplan() {
    fetch('/api/dayplan', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(renderDayplan)
      .catch(function () { /* Panel behält den letzten Stand; nächster Poll versucht es neu */ });
  }
  pollDayplan();
  setInterval(pollDayplan, 120000);   // die Notiz ändert sich selten — 2 min reichen

  // Overlays schließen (Close-Button + Klick auf Backdrop)
  document.querySelectorAll('.overlay-close').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var ov = $(btn.dataset.close);
      ov.classList.remove('open');
      if (btn.dataset.close === 'histOverlay' && histES) { histES.close(); histES = null; }
    });
  });
  document.querySelectorAll('.overlay').forEach(function (ov) {
    ov.addEventListener('click', function (e) {
      if (e.target === ov) {
        ov.classList.remove('open');
        if (ov.id === 'histOverlay' && histES) { histES.close(); histES = null; }
      }
    });
  });

  // ---- Senden (/api/ask, Streaming) -------------------------------------------------------------------
  var input = $('textInput');
  var sendBtn = $('sendBtn');

  $('modeBtn').addEventListener('click', function () {
    S.deep = !S.deep;
    $('modeBtn').classList.toggle('on', S.deep);
  });

  function sendMessage(text) {
    text = (text || '').trim();
    if (!text) return;
    var mode = S.deep ? 'deep' : 'chat';
    addEcho(text);
    addStreamRow({ ts: new Date().toISOString(), kind: 'user', text: text, source: 'web' }, false);
    input.value = '';
    sendBtn.disabled = true;
    S.webRunActive = true;
    applyState();   // sofort working, Uhr startet jetzt — und hält bis zum Stream-Ende

    var row = addStreamRow({ ts: new Date().toISOString(), kind: 'reply', text: '', source: 'web' }, true);
    var txtEl = row.querySelector('.ev-text');
    var cursor = document.createElement('span');
    cursor.className = 'ev-cursor';
    txtEl.after(cursor);
    var acc = '';

    fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ message: text, mode: mode })
    }).then(function (resp) {
      if (resp.status === 401) { location.reload(); throw new Error('401'); }
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var reader = resp.body.getReader();
      var dec = new TextDecoder();
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          acc += dec.decode(r.value, { stream: true });
          txtEl.textContent = acc;
          addEcho(acc);
          if (!S.streamPaused) streamBody.scrollTop = streamBody.scrollHeight;
          return pump();
        });
      }
      return pump();
    }).then(function () {
      if (!acc) txtEl.textContent = '(no reply)';
      cursor.remove();
      setLastAnswer(acc, text);
    }).catch(function (err) {
      txtEl.textContent = acc || ('error: ' + err.message);
      cursor.remove();
      setLastAnswer(acc, text);
    }).finally(function () {
      S.webRunActive = false;
      sendBtn.disabled = false;
      input.focus();
    });
  }

  // ---- Letzte Antwort in der Idle-Karte pinnen --------------------------------------
  // Der Live-Stream ist (artboard-treu) nur in working/listening sichtbar — ohne
  // das Pinning verschwände die Chat-Antwort ~10s nach dem Lauf mit dem Idle-Fall.
  function setLastAnswer(answer, question) {
    answer = (answer || '').trim();
    var box = $('ambientAnswer');
    if (!answer) { box.hidden = true; return; }
    try {
      sessionStorage.setItem('atlasLastAnswer', JSON.stringify(
        { q: question || '', a: answer }));
    } catch (e) { /* Storage voll/aus — Pinning lebt dann nur bis zum Reload */ }
    $('ambientAnswerQ').textContent = question ? '» ' + question : '';
    $('ambientAnswerText').textContent = answer;
    box.hidden = false;
  }
  try {
    var saved = JSON.parse(sessionStorage.getItem('atlasLastAnswer') || 'null');
    if (saved && saved.a) setLastAnswer(saved.a, saved.q);
  } catch (e) { /* kaputter Storage-Eintrag — einfach ohne starten */ }

  sendBtn.addEventListener('click', function () { sendMessage(input.value); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); sendMessage(input.value); }
  });
  document.querySelectorAll('.chip').forEach(function (chip) {
    chip.addEventListener('click', function () { sendMessage(chip.dataset.ask); });
  });

  // ---- Upload (/api/upload → INGEST_DIR) ------------------------------------------------------------------
  var fileInput = $('fileInput');
  $('attachBtn').addEventListener('click', function () { fileInput.click(); });
  fileInput.addEventListener('change', function () {
    var f = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!f) return;
    if (f.size > 50 * 1024 * 1024) {
      addStreamRow({ ts: new Date().toISOString(), kind: 'log', text: 'Upload rejected: ' + f.name + ' > 50MB', source: 'web' }, false);
      return;
    }
    $('attachBtn').disabled = true;
    var fd = new FormData();
    fd.append('file', f);
    fetch('/api/upload', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { location.reload(); throw new Error('401'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (j) {
        addStreamRow({ ts: new Date().toISOString(), kind: 'upload', text: 'Uploaded ' + (j.name || f.name) + ' → ingest queue', source: 'web' }, true);
      })
      .catch(function (err) {
        addStreamRow({ ts: new Date().toISOString(), kind: 'log', text: 'Upload failed: ' + err.message, source: 'web' }, false);
      })
      .finally(function () { $('attachBtn').disabled = false; });
  });

  // ---- Waveform (14 Balken) -------------------------------------------------------------------------------
  var waveBox = $('waveform');
  var REST_H = [6, 5, 7, 4, 6, 8, 5, 6, 7, 4, 5, 8, 6, 5];   // Ruhe-Höhen aus der Idle-Spec
  var bars = [];
  for (var bi = 0; bi < 14; bi++) {
    var b = document.createElement('span');
    b.className = 'wbar';
    b.style.height = REST_H[bi] + 'px';
    waveBox.appendChild(b);
    bars.push(b);
  }

  function setRestBars() {
    for (var i = 0; i < bars.length; i++) bars[i].style.height = REST_H[i] + 'px';
  }

  // ---- Mic / Spracheingabe: Aufnahme → /api/transcribe (ANVILs eigene Engine) -----
  // Bewusst KEIN webkitSpeechRecognition mehr: das war Chrome-only und lief über
  // Googles Cloud. Jetzt nimmt MediaRecorder auf (jeder Browser, https/localhost)
  // und der Server transkribiert mit derselben Engine wie WhatsApp-Sprachnotizen
  // (faster-whisper falls installiert, sonst markitdown — AUDIO_LANG=de-DE).
  var micBtn = $('micBtn');
  var audioCtx = null, analyser = null, micStream = null, recorder = null, micTimer = null;
  var recChunks = [];
  var inputBase = '';
  var micSupported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
                        window.MediaRecorder);

  if (!micSupported) {
    // KEIN toter Button: ohne Browser-Support ausgegraut mit Hinweis
    micBtn.disabled = true;
    micBtn.title = 'Aufnahme wird hier nicht unterstützt (MediaRecorder fehlt — https oder localhost nötig)';
  } else {
    micBtn.addEventListener('click', function () {
      if (S.micActive) stopListening(); else startListening();
    });
  }

  function startListening() {
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      micStream = stream;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      audioCtx.createMediaStreamSource(stream).connect(analyser);

      inputBase = input.value ? input.value + ' ' : '';
      recChunks = [];
      var mime = '';
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
        mime = 'audio/webm;codecs=opus';
      } else if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/mp4')) {
        mime = 'audio/mp4';   // Safari
      }
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) recChunks.push(e.data); };
      recorder.onstop = onRecordingDone;
      recorder.start();

      S.micActive = true;
      micTimer = setInterval(sampleMic, 60);
      applyState();
    }).catch(function (err) {
      addStreamRow({ ts: new Date().toISOString(), kind: 'log', text: 'Microphone denied: ' + err.message, source: 'web' }, false);
    });
  }

  function stopListening() {
    S.micActive = false;
    if (micTimer) { clearInterval(micTimer); micTimer = null; }
    if (audioCtx) { audioCtx.close(); audioCtx = null; analyser = null; }
    S.micDb = null;
    S.micLevel = 0;
    setRestBars();
    applyState();
    // Tracks erst in onRecordingDone stoppen — recorder.stop() flusht noch Daten.
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch (e) { onRecordingDone(); }
    } else {
      onRecordingDone();
    }
  }

  function onRecordingDone() {
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    var rec = recorder; recorder = null;
    if (!rec || !recChunks.length) return;
    var blob = new Blob(recChunks, { type: recChunks[0].type || 'audio/webm' });
    recChunks = [];
    if (blob.size < 2000) return;   // <~0,1s — nichts Gesagtes, nichts senden
    S.transcribing = true;
    micBtn.disabled = true;
    refreshStateTexts();
    fetch('/api/transcribe', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': blob.type || 'audio/webm' },
      body: blob
    }).then(function (r) {
      if (r.status === 401) { location.href = '/'; throw new Error('401'); }
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || ('http ' + r.status));
        return data;
      });
    }).then(function (data) {
      var text = (data.text || '').trim();
      if (text) {
        input.value = inputBase + text;
        input.focus();
      } else {
        addStreamRow({ ts: new Date().toISOString(), kind: 'log',
                       text: 'Transkription: keine Sprache erkannt', source: 'web' }, false);
      }
    }).catch(function (err) {
      addStreamRow({ ts: new Date().toISOString(), kind: 'log',
                     text: '⚠️ ' + err.message, source: 'web' }, false);
    }).finally(function () {
      S.transcribing = false;
      micBtn.disabled = false;
      refreshStateTexts();
    });
  }

  function sampleMic() {
    if (!analyser) return;
    // Echte Pegel: RMS aus Zeitdomäne → dBFS; Frequenzbins → 14 Waveform-Balken.
    var time = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(time);
    var sum = 0;
    for (var i = 0; i < time.length; i++) {
      var v = (time[i] - 128) / 128;
      sum += v * v;
    }
    var rms = Math.sqrt(sum / time.length);
    var db = 20 * Math.log10(rms + 1e-7);
    S.micDb = Math.max(-90, Math.round(db));
    var level = Math.min(1, Math.max(0, (db + 60) / 55));
    S.micLevel = S.micLevel * 0.7 + level * 0.3;

    var freq = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(freq);
    var step = Math.floor(freq.length / 2 / bars.length);
    for (var b = 0; b < bars.length; b++) {
      var f = freq[Math.min(freq.length - 1, 2 + b * step)] / 255;
      bars[b].style.height = Math.round(4 + f * 36) + 'px';
    }
    refreshStateTexts();
  }

  // ---- ORB (Canvas 2D, Fibonacci-Kugel ~1400 Punkte, nur fillRect) -----------------------------------------
  var canvas = $('orb');
  var ctx = canvas.getContext('2d');
  var DPR = Math.min(2, window.devicePixelRatio || 1);
  var PALETTE = ['#f5c2e7', '#cba6f7', '#74c7ec', '#94e2d5', '#b4befe', '#fab387'];
  var N = 1400;
  var ALPHA_STEPS = 16;

  // rgba-Strings vorquantisieren: PALETTE × 16 Alphastufen (kein String-Bau pro Punkt/Frame)
  var COLORS = PALETTE.map(function (hex) {
    var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
    var steps = [];
    for (var a = 0; a < ALPHA_STEPS; a++) {
      steps.push('rgba(' + r + ',' + g + ',' + b + ',' + ((a + 1) / ALPHA_STEPS).toFixed(3) + ')');
    }
    return steps;
  });

  // Fibonacci-Kugel: gleichmäßige Punktverteilung über den Goldenen Winkel
  var pts = [];
  var GA = Math.PI * (3 - Math.sqrt(5));
  for (var pi = 0; pi < N; pi++) {
    var y = 1 - (pi / (N - 1)) * 2;
    var rad = Math.sqrt(1 - y * y);
    var th = GA * pi;
    pts.push({
      x: Math.cos(th) * rad, y: y, z: Math.sin(th) * rad,
      c: pi % PALETTE.length,
      s: 1 + ((pi * 7) % 10) / 9          // deterministische Größenvarianz 1..2
    });
  }
  // Kern-Cluster fürs Working-Glühen (Spec wp1-8): wenige große helle Punkte nahe dem Zentrum
  var core = [];
  for (var ci = 0; ci < 24; ci++) {
    var cy = 1 - ((ci * 37 % 24) / 23) * 2;
    var cr = Math.sqrt(1 - cy * cy) * 0.22;
    var cth = GA * ci * 5;
    core.push({ x: Math.cos(cth) * cr, y: cy * 0.22, z: Math.sin(cth) * cr, c: ci % PALETTE.length, s: 3.2 + (ci % 4) });
  }

  var orb = {
    radius: 170, targetRadius: 170,    // Idle ~340px Durchmesser
    speed: 0.15, targetSpeed: 0.15,
    angle: 0, breath: 0, raf: null, last: 0
  };

  function orbTargets() {
    if (S.state === 'working') { orb.targetRadius = 140; orb.targetSpeed = 0.8; }      // ~280px, schneller
    else if (S.state === 'listening') { orb.targetRadius = 150; orb.targetSpeed = 0.3; }
    else { orb.targetRadius = 170; orb.targetSpeed = 0.15; }
  }

  function sizeCanvas() {
    var zone = $('orbzone');
    var w = zone.clientWidth, h = zone.clientHeight;
    canvas.width = Math.round(w * DPR);
    canvas.height = Math.round(h * DPR);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
  }
  sizeCanvas();
  window.addEventListener('resize', sizeCanvas);
  if (window.ResizeObserver) new ResizeObserver(sizeCanvas).observe($('orbzone'));

  function drawOrb(now) {
    orb.raf = null;
    if (document.hidden) return;            // Loop pausiert bei document.hidden
    var dt = Math.min(0.1, (now - orb.last) / 1000) || 0.016;
    orb.last = now;

    orbTargets();
    // ~600ms-Übergang: exponentielle Annäherung an die Zielwerte
    var k = 1 - Math.exp(-dt / 0.18);
    orb.radius += (orb.targetRadius - orb.radius) * k;
    orb.speed += (orb.targetSpeed - orb.speed) * k;
    orb.angle += orb.speed * dt;
    orb.breath += dt;

    var scale = 1;
    if (S.state === 'idle') scale = 1 + 0.03 * Math.sin(orb.breath * 0.9);             // Atmen
    else if (S.state === 'listening') scale = 1 + 0.22 * S.micLevel;                   // Puls = echter Pegel
    else scale = 1 + 0.015 * Math.sin(orb.breath * 2.4);

    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    var cx = W / 2, cy = H / 2;
    var R = orb.radius * scale * DPR;
    var cosA = Math.cos(orb.angle), sinA = Math.sin(orb.angle);
    var tilt = 0.35 + 0.08 * Math.sin(orb.breath * 0.4);
    var cosT = Math.cos(tilt), sinT = Math.sin(tilt);

    for (var i = 0; i < N; i++) {
      var p = pts[i];
      // Rotation um Y, leichte Kippung um X
      var x = p.x * cosA - p.z * sinA;
      var z = p.x * sinA + p.z * cosA;
      var y = p.y * cosT - z * sinT;
      z = p.y * sinT + z * cosT;
      var depth = (z + 1) / 2;                       // 0 hinten .. 1 vorn
      var ai = Math.min(ALPHA_STEPS - 1, Math.floor(depth * (ALPHA_STEPS - 1) * 0.9) + 1);
      var sz = Math.max(1, p.s * (0.6 + depth) * DPR);
      ctx.fillStyle = COLORS[p.c][ai];
      ctx.fillRect(cx + x * R - sz / 2, cy + y * R * 0.96 - sz / 2, sz, sz);
    }

    // Kern-Glühen im Working-State (heller innerer Cluster)
    if (S.state === 'working') {
      var pulse = 0.8 + 0.2 * Math.sin(orb.breath * 6);
      for (var c = 0; c < core.length; c++) {
        var q = core[c];
        var qx = q.x * cosA - q.z * sinA;
        var qz = q.x * sinA + q.z * cosA;
        var sz2 = q.s * pulse * DPR;
        ctx.fillStyle = COLORS[q.c][ALPHA_STEPS - 1];
        ctx.fillRect(cx + qx * R - sz2 / 2, cy + q.y * R - sz2 / 2, sz2, sz2);
      }
    }

    orb.raf = requestAnimationFrame(drawOrb);
  }

  function startOrb() {
    if (orb.raf == null && !document.hidden) {
      orb.last = performance.now();
      orb.raf = requestAnimationFrame(drawOrb);
    }
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      if (orb.raf != null) { cancelAnimationFrame(orb.raf); orb.raf = null; }
    } else {
      startOrb();
    }
  });
  startOrb();

  // ---- Start ------------------------------------------------------------------------------------------------
  connectSSE();
  renderAgents();
  renderTools();
  applyState();
})();
