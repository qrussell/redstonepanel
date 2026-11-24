// 1.21.10: the terminal. A shell on this machine (or a cluster node), in the browser, for admins.
// xterm.js is loaded only when a terminal opens (static/vendor/xterm, MIT). The panel shows the
// login prompt itself; see core/terminal.py for who gets which account and why.
const RPTerminal = {
    loaded: null,
    open: [],

    load() {
        if (this.loaded) return this.loaded;
        const script = src => new Promise((ok, fail) => {
            const s = document.createElement('script');
            s.src = src;
            s.onload = ok;
            s.onerror = () => fail(new Error(`Couldn't load ${src}`));
            document.head.appendChild(s);
        });
        const css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = '/static/vendor/xterm/xterm.css';
        document.head.appendChild(css);
        this.loaded = script('/static/vendor/xterm/xterm.js').then(() => script('/static/vendor/xterm/addon-fit.js'));
        this.loaded.catch(() => { this.loaded = null; });
        return this.loaded;
    },

    // opts: {node: cluster node id or '', name: shown in the title}
    async start(opts = {}) {
        const node = opts.node || '';
        const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
        const box = document.createElement('div');
        box.className = 'rp-terminal fixed inset-0 z-50 flex justify-center items-center bg-black bg-opacity-50 p-0 sm:p-4';
        const btn = 'px-3 py-1 rounded text-xs font-bold bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const keys = [['Esc', '\x1b'], ['Tab', '\t'], ['Ctrl', 'ctrl'], ['↑', '\x1b[A'], ['↓', '\x1b[B'], ['←', '\x1b[D'], ['→', '\x1b[C'],
            ['|', '|'], ['~', '~'], ['/', '/'], ['-', '-']];
        box.innerHTML = `
            <div class="rp-dialog bg-white dark:bg-gray-800 rounded-lg shadow-xl border border-gray-200 dark:border-gray-700 w-full flex flex-col"
                 style="max-width:1100px;height:min(90vh,760px)">
                <div class="flex items-center justify-between gap-2 p-3 border-b border-gray-200 dark:border-gray-700">
                    <h2 class="font-bold text-gray-900 dark:text-white truncate" data-title>⌨️ Terminal on ${esc(opts.name || 'this machine')}</h2>
                    <div class="flex flex-wrap justify-end gap-2 shrink-0">
                        <button data-act="copy" class="${btn}" title="Copy what is selected">Copy</button>
                        <button data-act="paste" class="${btn}" title="Paste">Paste</button>
                        <button data-act="smaller" class="${btn}" aria-label="Smaller text">A−</button>
                        <button data-act="bigger" class="${btn}" aria-label="Bigger text">A+</button>
                        <button data-act="close" class="px-3 py-1 rounded text-xs font-bold bg-red-600 hover:bg-red-700 text-white">Close</button>
                    </div>
                </div>
                <p data-warning class="hidden mx-3 mt-2 text-sm p-2 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200"></p>
                <div data-screen class="flex-1 min-h-0 m-3 rounded overflow-hidden" style="background:#111827;padding:4px"></div>
                <div data-keys class="md:hidden flex flex-wrap gap-1 px-3 pb-2">${keys.map(([label, send]) =>
                    `<button data-key="${esc(send)}" class="${btn}" style="min-width:44px;min-height:44px">${esc(label)}</button>`).join('')}</div>
                <p data-note class="px-3 pb-3 text-xs text-gray-500 dark:text-gray-400">Connecting…</p>
            </div>`;
        document.body.appendChild(box);
        const $ = sel => box.querySelector(sel);
        const note = text => { $('[data-note]').textContent = text; };
        const state = { ws: null, term: null, fit: null, ctrl: false, closed: false };
        this.open.push(state);

        const finish = () => {
            if (state.closed) return;
            state.closed = true;
            try { state.ws && state.ws.close(); } catch (e) { /* already closed */ }
            if (state.observer) state.observer.disconnect();
            if (state.term) state.term.dispose();
            box.remove();
            this.open = this.open.filter(s => s !== state);
        };
        $('[data-act="close"]').onclick = finish;

        try {
            await this.load();
        } catch (e) {
            note(e.message);
            return;
        }
        const term = state.term = new window.Terminal({
            cursorBlink: true, fontSize: window.innerWidth < 768 ? 13 : 14, scrollback: 5000, convertEol: false,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
            theme: { background: '#111827', foreground: '#e5e7eb', cursor: '#e5e7eb', selectionBackground: '#374151' }
        });
        const fit = state.fit = new window.FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open($('[data-screen]'));
        fit.fit();
        term.focus();

        const encoder = new TextEncoder();
        const send = text => {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(encoder.encode(text));
        };
        const sendSize = () => {
            try { fit.fit(); } catch (e) { return; }
            if (state.ws && state.ws.readyState === WebSocket.OPEN) {
                state.ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
            }
        };
        term.onData(data => {
            if (state.ctrl && data.length === 1) {           // the phone row's Ctrl, then a letter
                const c = data.toUpperCase().charCodeAt(0);
                if (c >= 64 && c <= 95) data = String.fromCharCode(c - 64);
                state.ctrl = false;
                box.querySelector('[data-key="ctrl"]').classList.remove('bg-theme');
            }
            send(data);
        });
        state.observer = new ResizeObserver(() => sendSize());
        state.observer.observe($('[data-screen]'));

        box.querySelectorAll('[data-key]').forEach(b => {
            b.onclick = () => {
                const k = b.getAttribute('data-key');
                if (k === 'ctrl') {
                    state.ctrl = !state.ctrl;
                    b.classList.toggle('bg-theme', state.ctrl);
                } else send(k);
                term.focus();
            };
        });
        $('[data-act="copy"]').onclick = async () => {
            const text = term.getSelection();
            if (text && navigator.clipboard) await navigator.clipboard.writeText(text).catch(() => {});
            term.focus();
        };
        $('[data-act="paste"]').onclick = async () => {
            if (navigator.clipboard && navigator.clipboard.readText) {
                const text = await navigator.clipboard.readText().catch(() => '');
                if (text) term.paste(text);
            }
            term.focus();
        };
        const zoom = d => {
            term.options.fontSize = Math.max(9, Math.min(24, term.options.fontSize + d));
            sendSize();
        };
        $('[data-act="smaller"]').onclick = () => zoom(-1);
        $('[data-act="bigger"]').onclick = () => zoom(1);

        // The ticket (a POST: the usual cross-site checks), then the websocket
        const res = await fetch('/api/terminal/ticket', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(node ? { node } : {})
        });
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
            term.write(`\r\n${detail}\r\n`);
            note('The terminal couldn\'t be opened.');
            return;
        }
        const { ticket } = await res.json();
        const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
        const path = node ? `/api/cluster/nodes/${encodeURIComponent(node)}/terminal/ws` : '/api/terminal/ws';
        const ws = state.ws = new WebSocket(`${scheme}://${location.host}${path}?ticket=${encodeURIComponent(ticket)}`);
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => {
            note(`Closes after ${opts.idleMinutes || 15} minutes with nothing typed. Nothing you type is recorded.`);
            sendSize();
        };
        ws.onmessage = e => {
            if (typeof e.data === 'string') {
                let msg = {};
                try { msg = JSON.parse(e.data); } catch (err) { return; }
                if (msg.type === 'started') {
                    $('[data-title]').textContent = `⌨️ ${msg.account} on ${opts.name || 'this machine'}`;
                    $('[data-warning]').classList.add('hidden');
                } else if (msg.type === 'warning') {
                    $('[data-warning]').textContent = msg.text;
                    $('[data-warning]').classList.remove('hidden');
                }
                return;
            }
            $('[data-warning]').classList.add('hidden');
            term.write(new Uint8Array(e.data));
        };
        ws.onclose = e => {
            if (state.closed) return;
            term.write(e.code === 4403 ? '\r\n[Refused: sign in again, or the terminal is off.]\r\n' : '\r\n[Disconnected]\r\n');
            note('This terminal has ended. Close it to open a new one.');
        };
    }
};
window.RPTerminal = RPTerminal;
