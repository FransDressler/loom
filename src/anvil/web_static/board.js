/* Kanban-Board — Logik. Datenquellen: GET /api/board (immer ein frischer Read,
   kein /api/state-Cache), POST /api/board/add, POST /api/board/move; optional
   GET /api/events (SSE) für Fremd-Updates (Chat-Tools, andere Tabs).
   Rendering ausschließlich über textContent/DOM-API — nie innerHTML mit
   Servertexten (XSS). Nach jeder Mutation wird neu geladen: der Vault-Ordner
   ist die Quelle der Wahrheit, nicht der lokale DOM-Zustand. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var STATUSES = ['todo', 'working', 'done'];

  // Embed-Modus (?embed=1): das Board läuft als iframe-Overlay im Dashboard —
  // Brand + Zurück-Link sind dort redundant und werden per CSS ausgeblendet.
  if (new URLSearchParams(location.search).has('embed')) {
    document.body.classList.add('embed');
  }

  function setStatus(text, err) {
    var el = $('boardStatus');
    el.textContent = text;
    el.className = 'board-status' + (err ? ' err' : '');
  }

  function chip(text, cls) {
    var s = document.createElement('span');
    s.className = 'k-chip' + (cls ? ' ' + cls : '');
    s.textContent = text;
    return s;
  }

  function card(t) {
    var el = document.createElement('article');
    var prio = (t.priority === 1 || t.priority === 3) ? t.priority : 2;
    el.className = 'kcard prio-' + prio;
    el.draggable = true;
    el.title = t.file || '';

    var title = document.createElement('div');
    title.className = 'k-title';
    title.textContent = t.title || '(ohne Titel)';
    el.appendChild(title);

    var meta = document.createElement('div');
    meta.className = 'k-meta';
    meta.appendChild(chip('P' + prio));
    if (t.due) {
      var overdue = t.status !== 'done' && t.due < new Date().toISOString().slice(0, 10);
      meta.appendChild(chip('fällig ' + t.due, 'due' + (overdue ? ' overdue' : '')));
    }
    if (t.project) meta.appendChild(chip(String(t.project).replace(/^\[\[|\]\]$/g, ''), 'project'));
    if (t.effort) meta.appendChild(chip('~' + t.effort));
    el.appendChild(meta);

    el.addEventListener('dragstart', function (e) {
      e.dataTransfer.setData('text/plain', t.rel_path);
      e.dataTransfer.effectAllowed = 'move';
      el.classList.add('dragging');
    });
    el.addEventListener('dragend', function () { el.classList.remove('dragging'); });
    return el;
  }

  function render(data) {
    STATUSES.forEach(function (st) {
      var box = $('col-' + st);
      box.textContent = '';                     // leeren per DOM-API, kein innerHTML
      var items = data[st] || [];
      $('count-' + st).textContent = String(items.length);
      if (!items.length) {
        var empty = document.createElement('div');
        empty.className = 'board-empty';
        empty.textContent = st === 'done' ? 'noch nichts erledigt' : 'leer — Karte hierher ziehen';
        box.appendChild(empty);
        return;
      }
      items.forEach(function (t) { box.appendChild(card(t)); });
    });
  }

  function load() {
    fetch('/api/board', { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { window.location.href = '/'; return null; }  // Session weg → Login
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) { if (data) { render(data); setStatus('live'); } })
      .catch(function () { setStatus('offline — Seite neu laden?', true); });
  }

  // Drag&Drop: natives HTML5-DnD, Drop auf eine Spalte = Status-Move.
  STATUSES.forEach(function (st) {
    var box = $('col-' + st);
    box.addEventListener('dragover', function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      box.classList.add('dragover');
    });
    box.addEventListener('dragleave', function () { box.classList.remove('dragover'); });
    box.addEventListener('drop', function (e) {
      e.preventDefault();
      box.classList.remove('dragover');
      var file = e.dataTransfer.getData('text/plain');
      if (!file) return;
      fetch('/api/board/move', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: file, to: st })
      }).then(function (r) {
        if (!r.ok) {
          setStatus('Move fehlgeschlagen (HTTP ' + r.status + ')', true);
          setTimeout(load, 3000);               // Fehlermeldung lesbar lassen, dann neu lesen
          return;
        }
        load();                                 // Server ist die Wahrheit — immer neu lesen
      }).catch(function () {
        setStatus('Move fehlgeschlagen — offline?', true);
        setTimeout(load, 3000);
      });
    });
  });

  // Add-Feld oben → POST /api/board/add → create_task (landet in todo/).
  $('addForm').addEventListener('submit', function (e) {
    e.preventDefault();
    var input = $('addInput');
    var title = (input.value || '').trim();
    if (!title) return;
    fetch('/api/board/add', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title })
    }).then(function (r) {
      if (r.ok) { input.value = ''; } else { setStatus('Anlegen fehlgeschlagen (HTTP ' + r.status + ')', true); }
      load();
    }).catch(function () { setStatus('Anlegen fehlgeschlagen — offline?', true); });
  });

  // Fremd-Updates (Chat-Tools task_add/task_move, andere Tabs): kanban-Events vom
  // vorhandenen SSE-Feed → entprellter Reload. Optional — ohne SSE funktioniert
  // das Board vollständig, nur ohne Live-Update.
  try {
    var es = new EventSource('/api/events?backlog=0');
    var pending = null;
    es.onmessage = function (m) {
      var ev;
      try { ev = JSON.parse(m.data); } catch (err) { return; }
      if (!ev || ev.kind !== 'kanban') return;
      if (pending) clearTimeout(pending);
      pending = setTimeout(load, 400);          // ein Reload je Event-Schub
    };
  } catch (err) { /* SSE ist optional */ }

  load();
})();
