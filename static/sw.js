// RedstonePanel's service worker (1.16.10). Browsers only offer "Install app" for a site that
// has one. It caches nothing: a panel that can't reach its server has nothing true to show, so
// every request goes to the server as usual. The one thing it adds is a plain page, instead of
// the browser's error, when the panel can't be reached at all (the phone is off the network, or
// the panel is restarting).
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

const OFFLINE = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>RedstonePanel</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:system-ui,sans-serif;background:#1b1a19;color:#eeecea;text-align:center;padding:1.5rem;box-sizing:border-box}
h1{font-size:1.25rem}p{color:#c0bcb8;max-width:22rem}button{margin-top:1rem;font:inherit;font-weight:700;
color:#fff;background:#d9261b;border:0;border-radius:.375rem;padding:.75rem 1.5rem;min-height:44px}</style></head>
<body><div><h1>Can't reach the panel</h1><p>Check that this phone is on the same network as the panel's machine
(or on your VPN), and that the panel is running.</p><button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener('fetch', event => {
    // Only page loads; everything else (the API, downloads, uploads) goes straight through
    if (event.request.mode !== 'navigate') return;
    event.respondWith(fetch(event.request).catch(() =>
        new Response(OFFLINE, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});
