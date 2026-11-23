// The public join pages (core/joinsite.py): the Copy buttons, and the status line refreshing
// by itself. The page works without this file; nothing here is needed to read it.
(function () {
  'use strict';
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
  function show(d) {
    var on = d.status === 'online';
    if (pill) { pill.textContent = on ? 'Online' : 'Offline'; pill.className = 'pill ' + (on ? 'on' : 'off'); }
    var p = on ? d.players : null;
    if (players) {
      players.textContent = !p || p.online === null || p.online === undefined ? ''
        : p.max === null || p.max === undefined ? p.online + ' playing' : p.online + ' of ' + p.max + ' playing';
    }
    if (names) names.textContent = p && p.names && p.names.length ? 'Playing now: ' + p.names.join(', ') : '';
  }
  function refresh() {
    if (document.hidden) return;
    fetch(url, { cache: 'no-store', credentials: 'omit' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) show(d); })
      .catch(function () {});
  }
  setInterval(refresh, 30000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();
