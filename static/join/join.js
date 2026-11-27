// The public join pages (core/joinsite.py): the Copy buttons, and the status line refreshing
// by itself. The page works without this file; nothing here is needed to read it.
(function () {
  'use strict';
  // Addresses on this same site, shown and copied in full (the page alone only knows the path)
  document.querySelectorAll('[data-abs]').forEach(function (el) {
    try { el.textContent = new URL(el.getAttribute('data-abs'), window.location.href).href; } catch (e) { /* the path is shown */ }
  });
  document.querySelectorAll('button[data-abs-copy]').forEach(function (b) {
    try { b.setAttribute('data-copy', new URL(b.getAttribute('data-abs-copy'), window.location.href).href); } catch (e) { /* the path is copied */ }
  });
  document.querySelectorAll('button[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      var text = b.getAttribute('data-copy');
      var done = function () { var old = b.textContent; b.textContent = 'Copied'; setTimeout(function () { b.textContent = old; }, 1500); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        var t = document.createElement('textarea');
        t.value = text; t.setAttribute('readonly', ''); t.style.position = 'fixed'; t.style.opacity = '0';
        document.body.appendChild(t); t.select();
        try { if (document.execCommand('copy')) done(); } catch (e) { /* the address is on the page to type */ }
        document.body.removeChild(t);
      }
    });
  });

  var url = document.body.getAttribute('data-status');
  if (!url || !window.fetch) return;
  var pill = document.getElementById('status'), players = document.getElementById('players'), names = document.getElementById('names');
  var wake = document.getElementById('wake'), wakeNote = document.getElementById('wake-note'), startBtn = document.getElementById('start');
  var stopBtn = document.getElementById('stop'), lastPlayers = null;
  var STATES = { online: ['on', 'Online'], starting: ['starting', 'Starting'], sleeping: ['sleeping', 'Asleep'], offline: ['off', 'Offline'], stopping: ['starting', 'Stopping'] };
  var fast = null;
  function show(d) {
    var on = d.status === 'online', st = STATES[d.status] || STATES.offline;
    if (pill) { pill.textContent = st[1]; pill.className = 'pill ' + st[0]; }
    var p = on ? d.players : null;
    lastPlayers = p;
    if (players) {
      players.textContent = !p || p.online === null || p.online === undefined ? ''
        : p.max === null || p.max === undefined ? p.online + ' playing' : p.online + ' of ' + p.max + ' playing';
    }
    if (names) names.textContent = p && p.names && p.names.length ? 'Playing now: ' + p.names.join(', ') : '';
    // 1.26: an asleep server, the Start button, and checking often while it starts
    if (wake) wake.hidden = on;
    if (wakeNote) {
      wakeNote.textContent = d.status === 'sleeping' ? 'The server is asleep to save power. It starts by itself when you join; then join again in about 30 seconds.'
        : d.status === 'starting' ? 'The server is starting. This page shows Online when you can join.'
        : d.status === 'stopping' ? 'The server is stopping.' : '';
    }
    if (wake && d.status === 'stopping') wake.hidden = false;
    if (startBtn) {
      startBtn.disabled = d.status === 'starting';
      // 1.28: on a page that needs sign-in, the visitor's permission says whether Start shows
      if (d.can_start !== undefined && startBtn.hasAttribute('data-start')) startBtn.hidden = !d.can_start && d.status !== 'starting';
    }
    if (stopBtn && d.can_stop !== undefined) stopBtn.hidden = !d.can_stop;
    if (on && fast) { clearInterval(fast); fast = null; if (wakeNote) wakeNote.textContent = ''; }
    if (d.status === 'offline' && fast && stopBtn) { clearInterval(fast); fast = null; }
  }
  function refresh() {
    if (document.hidden) return;
    fetch(url, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) show(d); })
      .catch(function () {});
  }
  if (startBtn) {
    startBtn.addEventListener('click', function () {
      startBtn.disabled = true;
      fetch(startBtn.getAttribute('data-start'), { method: 'POST', cache: 'no-store', credentials: 'same-origin', headers: { 'X-Join-Start': '1' } })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          if (d.error) { if (wakeNote) wakeNote.textContent = d.error; startBtn.disabled = false; return; }
          show({ status: d.status === 'online' ? 'online' : 'starting', players: null });
          if (!fast) fast = setInterval(refresh, 5000);
        })
        .catch(function () { startBtn.disabled = false; });
    });
  }
  // 1.28: Stop, for a signed-in visitor whose permission gives it
  if (stopBtn) {
    stopBtn.addEventListener('click', function () {
      var n = lastPlayers && lastPlayers.online ? lastPlayers.online : 0;
      if (n && !window.confirm(n + (n === 1 ? ' player is' : ' players are') + ' on the server. Stop it anyway?')) return;
      stopBtn.disabled = true;
      fetch(stopBtn.getAttribute('data-stop'), { method: 'POST', cache: 'no-store', credentials: 'same-origin', headers: { 'X-Join-Start': '1' } })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          stopBtn.disabled = false;
          if (d.error) { if (wakeNote) { wakeNote.textContent = d.error; if (wake) wake.hidden = false; } return; }
          show({ status: d.status === 'offline' ? 'offline' : 'stopping', players: null, can_stop: false });
          if (!fast) fast = setInterval(refresh, 5000);
        })
        .catch(function () { stopBtn.disabled = false; });
    });
  }
  if (pill && pill.className.indexOf('starting') >= 0) fast = setInterval(refresh, 5000);
  if (stopBtn) refresh();
  setInterval(refresh, 30000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();
