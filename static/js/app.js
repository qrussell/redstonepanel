// Any API call that comes back 401 (not signed in / session expired) sends the browser to the login page
(() => {
    const realFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        const res = await realFetch(...args);
        const url = String(args[0] && args[0].url ? args[0].url : args[0]);
        if (res.status === 401 && !url.includes('/api/auth/login')) {
            window.location.replace('/static/login.html');
        }
        return res;
    };
})();

// Which panel a request goes to. On a cluster master, a server that lives on a node is reached
// through the master's proxy, /api/cluster/nodes/{id}/proxy/api/...; everything else is this panel.
// By default a request goes where the open server lives; opts.node picks a node explicitly
// (null = this panel), e.g. for New Server's "Where". Panel-wide calls (the server list, settings,
// users, the cluster page) use fetch() directly: they are always about this panel.
function apiUrl(path, node) {
    if (node === undefined) node = ServerApp.activeServer ? ServerApp.activeServer.node || null : null;
    if (!node || path.startsWith('/api/cluster/nodes/')) return path;
    return `/api/cluster/nodes/${encodeURIComponent(node)}/proxy${path}`;
}

function api(path, opts = {}) {
    const { node, ...init } = opts;
    return fetch(apiUrl(path, node), init);
}

const ServerApp = {
    activeServer: null,
    servers: [],
	rconOutputs: [],
    sortOrder: localStorage.getItem('serverSortOrder') || 'name-asc',
    currentUser: null,

    async init() {
        this.loadPanelName();   // public, so it shows even while the sign-in check runs
        // Who is signed in? (a 401 here redirects to the login page)
        const me = await fetch('/api/auth/me');
        if (!me.ok) return;
        this.currentUser = await me.json();
        this.renderCurrentUser();
        // Panel Settings require two-factor for admins and this admin hasn't set it up yet:
        // nothing else works until they do (the API refuses it too).
        if (this.currentUser.needs_2fa_setup) {
            this.applyTheme();
            this.openAccountModal({ forced: true });
            return;
        }
        const ver = document.getElementById('panel-version');
        if (ver && this.currentUser.panel_version) {
            // On Windows the panel is a preview (since 1.22) and says so next to its version
            const preview = this.currentUser.system === 'windows';
            ver.innerText = 'v' + this.currentUser.panel_version + (preview ? ' · Windows preview' : '');
            ver.title = `RedstonePanel ${this.currentUser.panel_version}` + (preview ? ' for Windows, a preview: one computer, clusters come later' : '');
        }

        this.applyTheme();
        this.setupDropZones();
        this.setupPhone();
        // Many actions open the console; remember what was clicked so it can be kept in view
        document.addEventListener('click', e => {
            const el = e.target.closest && e.target.closest('button, a, input, select, label');
            if (el) this.lastClick = { el, at: Date.now() };
        }, true);
        window.addEventListener('resize', () => {
            if (this.consoleOpen()) this.setConsoleSpace(true);
        });
        this.fetchTemplates();
        const sortDropdown = document.getElementById('server-sort');
        if (sortDropdown) sortDropdown.value = this.sortOrder;
        
        await this.loadServers();
    },

    // The Windows preview doesn't do everything yet: /api/auth/me lists what under not_yet
    // ('cluster', 'sftp', ...).
    // Those parts of the page are hidden, and the API refuses them too.
    notYet(name) {
        return !!(this.currentUser && (this.currentUser.not_yet || []).includes(name));
    },

    onWindows() {
        return !!(this.currentUser && this.currentUser.system === 'windows');
    },

    // On a cluster master: the nodes from the last overview, [{id, name, state, error, ...}]
    clusterNodes: [],

    isMaster() {
        const u = this.currentUser;
        return !!(u && u.cluster && u.cluster.role === 'master' && u.source !== 'trusted');
    },

    // --- 1.18: per-server permissions ---
    // Admins can do everything. A `user` account gets, with each server in the list, what an
    // admin ticked for it under Users → Servers (srv.perms); the API checks the same.
    can(perm, srv) {
        if (this.isAdmin()) return true;
        srv = srv || this.activeServer;
        return !!(srv && Array.isArray(srv.perms) && srv.perms.includes(perm));
    },

    // Shows only the tabs, sub-tabs and buttons this account may use on this server
    applyPerms(srv) {
        const show = (id, on) => { const el = document.getElementById(id); if (el) el.style.display = on ? '' : 'none'; };
        const c = p => this.can(p, srv);
        show('ws-power-controls', c('power'));
        show('tab-btn-chat', c('console'));
        show('tab-btn-properties', c('settings') || c('players'));
        show('tab-btn-maintenance', c('settings'));
        show('tab-btn-backups', c('backups'));
        show('tab-btn-world', c('settings') || c('backups'));
        show('tab-btn-files', c('files'));
        ['visual', 'raw'].forEach(t => show(`subtab-${t}`, c('settings')));
        ['players', 'access', 'permissions'].forEach(t => show(`subtab-${t}`, c('players')));
        show('export-card', c('files'));
        show('world-export-card', c('backups'));
        show('world-rename-card', c('settings'));
    },

    firstConfigSubTab() {
        return this.can('settings') ? 'visual' : 'players';
    },

    sameServer(a, b) {
        return !!(a && b && a.name === b.name && (a.node || null) === (b.node || null));
    },

    // "minecraft-java-1" here, or "minecraft-java-1 · Basement" for a node's server
    serverLabel(srv) {
        return srv.node ? `${srv.name} · ${srv.nodeName}` : srv.name;
    },

    // --- Phones (1.16.10) ---
    // Below the md breakpoint the sidebar is a menu behind the ☰ button in the top bar.
    // Picking anything in it (a server, Dashboard, New Server…) closes it again; folding a group
    // or changing the sort order doesn't.
    setupPhone() {
        const aside = document.getElementById('sidebar');
        aside.addEventListener('click', e => {
            const hit = e.target.closest('button, [onclick]');
            if (hit && !hit.hasAttribute('aria-expanded') && aside.contains(hit)) this.closeNav();
        });
        document.addEventListener('keydown', e => { if (e.key === 'Escape' && this.navOpen()) this.closeNav(); });
        window.matchMedia('(min-width: 768px)').addEventListener('change', e => { if (e.matches) this.closeNav(); });
        this.registerServiceWorker();
    },

    navOpen() {
        return document.body.classList.contains('rp-nav-open');
    },

    openNav() {
        document.body.classList.add('rp-nav-open');
        document.getElementById('nav-backdrop').classList.remove('hidden');
        document.getElementById('btn-nav').setAttribute('aria-expanded', 'true');
    },

    closeNav() {
        if (!this.navOpen()) return;
        document.body.classList.remove('rp-nav-open');
        document.getElementById('nav-backdrop').classList.add('hidden');
        document.getElementById('btn-nav').setAttribute('aria-expanded', 'false');
    },

    // What the phone's top bar says: the open server, the dashboard or the cluster page
    setMobileTitle(title, subtitle) {
        document.getElementById('mobile-title').innerText = title;
        document.getElementById('mobile-subtitle').innerText = subtitle || '';
    },

    // Live Chat on a phone: once the on-screen keyboard has opened (and the page has shrunk to
    // the space above it), bring the message box back into view.
    keepInView(el) {
        if (window.matchMedia('(min-width: 768px)').matches) return;
        setTimeout(() => el.scrollIntoView({ block: 'nearest' }), 300);
    },

    // Installing the panel as an app needs a service worker. It caches nothing (a panel that
    // can't reach its server has nothing true to show); when the server can't be reached it
    // says so instead of the browser's error page. Browsers only allow it over HTTPS (or on
    // localhost), so on plain HTTP this quietly does nothing.
    registerServiceWorker() {
        if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
        navigator.serviceWorker.register('/static/sw.js', { scope: '/static/' }).catch(() => { /* an app is optional */ });
    },

    // --- "Open on your phone" (desktop): a QR code of this panel's address ---
    // Made by this panel (core/qr.py, GET /api/phone-qr), never an outside service. If the panel
    // was opened as localhost, a phone can't use that, so the machine's network addresses are
    // offered instead (from the dashboard's host info).
    async openPhoneQr() {
        const esc = t => this.escapeHtml(String(t ?? ''));
        const here = window.location;
        const port = here.port ? `:${here.port}` : '';
        const local = /^(localhost|127\.|\[::1\]$|::1$)/.test(here.hostname);
        let urls = local ? [] : [`${here.protocol}//${here.host}/`];
        let ips = [];
        try {
            if (!this.dashData) {
                const res = await fetch('/api/host');
                if (res.ok) this.dashData = await res.json();
            }
            ips = (this.dashData && this.dashData.host && this.dashData.host.ips) || [];
        } catch (e) { /* the current address is enough */ }
        ips.filter(ip => !/^(127\.|::1$|fe80:)/i.test(ip)).forEach(ip => {
            const u = `${here.protocol}//${ip.includes(':') ? `[${ip}]` : ip}${port}/`;
            if (!urls.includes(u)) urls.push(u);
        });
        if (!urls.length) urls = [`${here.protocol}//${here.host}/`];
        const https = here.protocol === 'https:';
        document.getElementById('modal-content').innerHTML = `
            <h3 class="text-lg font-bold text-gray-900 dark:text-white mb-1">📱 Open on your phone</h3>
            <p class="text-sm text-gray-600 dark:text-gray-300 mb-3">Point your phone's camera at the code. Your phone needs to be able to reach this address, usually by being on the same network.</p>
            <div class="rp-qr mx-auto w-60 max-w-full" id="phone-qr"></div>
            ${urls.length > 1 ? `<select id="phone-qr-url" onchange="ServerApp.drawPhoneQr(this.value)" class="mt-3 w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-sm text-gray-900 dark:text-white">
                ${urls.map(u => `<option value="${esc(u)}">${esc(u)}</option>`).join('')}</select>`
                : `<p class="mt-3 text-center font-mono text-sm break-all text-gray-900 dark:text-gray-200" id="phone-qr-text">${esc(urls[0])}</p>`}
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-3">${https
                ? `To keep it on your home screen, choose <b>Install app</b> (Android) or <b>Share → Add to Home Screen</b> (iPhone). It opens without the browser's bars.${/^self/.test(((this.dashData || {}).services || {}).https ? this.dashData.services.https.mode : '') ? ' With a self-signed certificate your phone will warn you first, as this browser did.' : ''}`
                : `The panel uses plain HTTP, so phones can only add it to the home screen as a bookmark. To install it as an app, turn on HTTPS in Panel Settings.`}</p>
            <button onclick="document.getElementById('modal-overlay').classList.add('hidden')" class="mt-4 w-full px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white font-bold transition">Close</button>`;
        document.getElementById('modal-overlay').classList.remove('hidden');
        this.drawPhoneQr(urls[0]);
    },

    drawPhoneQr(url) {
        const box = document.getElementById('phone-qr');
        box.innerHTML = `<img src="/api/phone-qr?url=${encodeURIComponent(url)}" alt="QR code for ${this.escapeHtml(url)}" class="block w-full h-auto">`;
        box.querySelector('img').onerror = () => {
            box.innerHTML = `<p class="text-sm text-red-600 p-4">Couldn't make a QR code for this address.</p>`;
        };
    },

    async loadServers() {
        let servers = null;
        if (this.isMaster()) {
            try {
                const res = await fetch('/api/cluster/overview');
                if (res.ok) {
                    const ov = await res.json();
                    this.clusterNodes = ov.nodes.filter(n => !n.local);
                    servers = ov.nodes.flatMap(n => (n.servers || []).map(s => n.local ? { ...s, node: null }
                        : { ...s, node: n.id, nodeName: n.name, nodeApi: n.cluster_api || 1 }));
                }
            } catch (e) { servers = null; }
        }
        if (!servers) {
            const res = await fetch('/api/servers');
            servers = await res.json();
        }
        this.servers = servers;
                
        this.applySort();
        this.renderSidebar();
        this.loadRemovedServers();

        // Nothing selected: show the dashboard (unless the cluster page is open)
        if (!this.activeServer && document.getElementById('dashboard').classList.contains('hidden')
            && document.getElementById('cluster-view').classList.contains('hidden')) {
            this.showDashboard();
        }

        if (this.activeServer) {
            const srv = this.servers.find(s => this.sameServer(s, this.activeServer));
            if (srv) this.openWorkspace(srv);
        }
    },
    
    // --- Which panel is this? (several servers may each run one) ---
    panelName: '',

    async loadPanelName() {
        try {
            const res = await fetch('/api/panel-info');
            if (res.ok) this.showPanelName((await res.json()).name);
        } catch (e) { /* the name is a convenience; the panel works without it */ }
    },

    showPanelName(name) {
        this.panelName = name || '';
        const el = document.getElementById('panel-name');
        if (el) {
            el.innerText = this.panelName;
            el.title = this.panelName;
        }
        document.title = this.panelName ? `${this.panelName} · RedstonePanel` : 'RedstonePanel';
    },

    // --- Theming & Preferences ---
    themeMode: localStorage.getItem('rp_themeMode') || 'dark',
    themeColor: localStorage.getItem('rp_themeColor') || 'redstone',

    accents: [['redstone', 'Redstone Red', '#d9261b'], ['diamond', 'Diamond Blue', '#2563eb'],
              ['emerald', 'Emerald Green', '#059669'], ['amethyst', 'Amethyst Purple', '#7c3aed']],

    applyTheme() {
        document.documentElement.classList.toggle('dark', this.themeMode === 'dark');
        document.documentElement.setAttribute('data-theme', this.themeColor);
        this.renderThemeControls();
    },

    // The light/dark switch and the accent colour dot: in the page's top right corner on a
    // desktop, at the right end of the top bar on a phone. One click each, saved in this browser.
    renderThemeControls() {
        const dark = this.themeMode === 'dark';
        const to = dark ? 'light' : 'dark';
        const btn = 'w-11 h-11 md:w-10 md:h-10 flex items-center justify-center rounded-full text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-white transition';
        const sun = '<svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
        const moon = '<svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
        const dots = this.accents.map(([id, name, hex]) => `<button onclick="ServerApp.setAccent('${id}')" title="${name}" aria-label="${name}" aria-pressed="${id === this.themeColor}" class="w-11 h-11 md:w-9 md:h-9 flex items-center justify-center rounded-full hover:bg-gray-200 dark:hover:bg-gray-700"><span class="w-5 h-5 rounded-full ${id === this.themeColor ? 'ring-2 ring-offset-2 ring-gray-500 dark:ring-gray-300 ring-offset-white dark:ring-offset-gray-800' : ''}" style="background:${hex}"></span></button>`).join('');
        document.querySelectorAll('.theme-controls').forEach(el => {
            const open = !!el.querySelector('.accent-menu:not(.hidden)');
            el.innerHTML = `
                <div class="relative">
                    <button onclick="ServerApp.toggleAccentMenu(this)" title="Accent color" aria-label="Accent color" aria-haspopup="true" aria-expanded="${open}" class="${btn}"><span class="w-4 h-4 rounded-full bg-theme"></span></button>
                    <div class="accent-menu ${open ? '' : 'hidden'} absolute right-0 top-full mt-1 flex gap-1 p-1.5 rounded-lg shadow-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 z-30">${dots}</div>
                </div>
                <button onclick="ServerApp.toggleThemeMode()" title="Switch to ${to} mode" aria-label="Switch to ${to} mode" class="${btn}">${dark ? sun : moon}</button>`;
        });
    },

    toggleThemeMode() {
        this.themeMode = this.themeMode === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem('rp_themeMode', this.themeMode); } catch (e) {}
        this.applyTheme();
    },

    toggleAccentMenu(btn) {
        const menu = btn.nextElementSibling;
        const show = menu.classList.contains('hidden');
        this.closeAccentMenus();
        menu.classList.toggle('hidden', !show);
        btn.setAttribute('aria-expanded', String(show));
        if (show && !this._accentCloser) {
            // A click anywhere else, or Esc, closes it
            this._accentCloser = true;
            document.addEventListener('click', e => { if (!e.target.closest('.theme-controls')) this.closeAccentMenus(); });
            document.addEventListener('keydown', e => { if (e.key === 'Escape') this.closeAccentMenus(); });
        }
    },

    closeAccentMenus() {
        document.querySelectorAll('.theme-controls .accent-menu').forEach(m => {
            m.classList.add('hidden');
            m.previousElementSibling.setAttribute('aria-expanded', 'false');
        });
    },

    setAccent(color) {
        this.themeColor = color;
        try { localStorage.setItem('rp_themeColor', color); } catch (e) {}
        this.closeAccentMenus();
        this.applyTheme();
    },

    changeSortOrder(order) {
        this.sortOrder = order;
        localStorage.setItem('serverSortOrder', order); 
        this.applySort();
        this.renderSidebar();
    },

    applySort() {
        this.servers.sort((a, b) => {
            if (this.sortOrder === 'name-asc') {
                return a.name.localeCompare(b.name);
            } else if (this.sortOrder === 'name-desc') {
                return b.name.localeCompare(a.name);
            } else if (this.sortOrder === 'status-online') {
                if (a.status === b.status) return a.name.localeCompare(b.name);
                return a.status === 'online' ? -1 : 1;
            }
            return 0;
        });
    },

    // Sidebar headings the user collapsed (kept in this browser): 'java', 'bedrock', 'other',
    // 'node:<id>' for a whole node, 'node:<id>:java' and so on for a node's editions
    collapsedGroups: (() => {
        try { return new Set(JSON.parse(localStorage.getItem('rp_collapsedGroups') || '[]')); } catch (e) { return new Set(); }
    })(),

    toggleGroup(key) {
        if (this.collapsedGroups.has(key)) this.collapsedGroups.delete(key);
        else this.collapsedGroups.add(key);
        try { localStorage.setItem('rp_collapsedGroups', JSON.stringify([...this.collapsedGroups])); } catch (e) {}
        this.renderSidebar();
    },

    renderSidebar() {
        const list = document.getElementById('server-list');
        const esc = t => this.escapeHtml(String(t ?? ''));
        const editions = [['java', 'Java Edition', s => s.edition === 'Java'],
                          ['bedrock', 'Bedrock Edition', s => s.edition === 'Bedrock'],
                          ['other', 'Unknown / Other', s => s.edition !== 'Java' && s.edition !== 'Bedrock']];

        const serverRow = srv => {
            const dot = srv.status === 'online' ? 'rp-lamp-on' : 'rp-lamp-off';
            const isActive = this.sameServer(this.activeServer, srv) ? 'rp-active bg-gray-200 dark:bg-gray-800' : 'hover:bg-gray-100 dark:hover:bg-gray-800';
            return `
                <div onclick="ServerApp.openWorkspace(${JSON.stringify(srv).replace(/"/g, '&quot;')})"
                     class="p-3 rounded cursor-pointer transition flex items-center gap-3 ${isActive}">
                    <span class="${dot}">●</span>
                    <div class="min-w-0">
                        <div class="font-bold text-gray-900 dark:text-gray-200 truncate">${esc(srv.name)}${this.hasProxyProblem(srv) || this.hasConnectProblem(srv) ? ' <span class="text-yellow-600" title="Players may not be able to connect: see Overview">⚠</span>' : ''}</div>
                        <div class="text-xs text-gray-500">${esc(srv.edition)}${srv.version ? ' ' + esc(srv.version) : ''} • Port ${srv.port}${this.updateBadge(srv)}</div>
                    </div>
                </div>`;
        };

        // A heading that folds what's under it. When folded it shows how many servers it holds
        // and how many are online, so a folded group still tells you something.
        // level: 'top' (this machine's editions), 'node' (a cluster node) or 'sub' (a node's editions)
        const heading = (key, title, servers, level) => {
            const open = !this.collapsedGroups.has(key);
            const online = servers.filter(s => s.status === 'online').length;
            const count = servers.length ? `<span class="ml-auto font-normal normal-case tracking-normal">${open ? servers.length : `${online}/${servers.length} online`}</span>` : '';
            return `<button type="button" onclick="ServerApp.toggleGroup(${this.jsArg(key)})" aria-expanded="${open}"
                        class="w-full flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider rounded hover:text-gray-900 dark:hover:text-gray-100 transition ${level === 'node' ? 'text-gray-700 dark:text-gray-300' : 'text-gray-500'} ${level === 'sub' ? 'mt-2 mb-1 pl-5 pr-3' : 'mt-4 mb-1 px-3'}">
                        <span class="inline-block w-3 text-center transition-transform ${open ? 'rotate-90' : ''}" aria-hidden="true">▸</span>
                        <span class="truncate">${title}</span>${count}
                    </button>`;
        };

        // Edition groups for one machine. On a node they sit under its heading, indented.
        const editionGroups = (servers, prefix, sub) => editions.map(([id, title, match]) => {
            const group = servers.filter(match);
            if (!group.length) return '';
            const key = prefix + id;
            return heading(key, title, group, sub ? 'sub' : 'top')
                + (this.collapsedGroups.has(key) ? '' : `<div class="${sub ? 'pl-3' : ''}">${group.map(serverRow).join('')}</div>`);
        }).join('');

        let html = editionGroups(this.servers.filter(s => !s.node), '', false);
        // A cluster master: each node's servers under the node's name, split by edition like this machine's
        this.clusterNodes.forEach(n => {
            const servers = this.servers.filter(s => s.node === n.id);
            const key = `node:${n.id}`;
            const note = n.state !== 'ok' ? `⚠️ ${esc(this.clusterStates[n.state] || n.state)}`
                : (n.cluster_api || 1) < 2 ? 'Update RedstonePanel on this node to manage its servers here.'
                : (servers.length ? '' : 'No servers yet.');
            html += heading(key, `🌐 ${esc(n.name)}`, servers, 'node');
            if (note) html += `<div class="text-xs text-yellow-600 dark:text-yellow-500 px-3 mb-1">${note}</div>`;
            if (!this.collapsedGroups.has(key)) html += editionGroups(servers, `${key}:`, true);
        });
        list.innerHTML = html;
    },

    async openWorkspace(srv) {
        this.activeServer = srv;
		this.chatHistoryIndex = -1;
		// Clear previous command outputs so they don't bleed into the new server tab
		this.rconOutputs = [];
        
		this.closeCluster();
		this.hideDashboard();
		document.getElementById('workspace').classList.remove('hidden');
        this.renderSidebar();

        this.updateAddonsTab(srv);
        this.applyPerms(srv);
        document.getElementById('ws-title').innerText = srv.name;
        document.getElementById('ws-subtitle').innerText = srv.node ? `🌐 ${srv.nodeName}: ${srv.directory}` : srv.directory;
        document.getElementById('ws-status').innerText = srv.status === 'online' ? '🟢' : '🔴';
        this.setMobileTitle(`${srv.status === 'online' ? '🟢' : '🔴'} ${srv.name}`, srv.node ? `🌐 ${srv.nodeName}` : `${srv.edition}${srv.version ? ' ' + srv.version : ''}`);
        
        // Populate the live chat RCON port badge
        const rconBadge = document.getElementById('chat-rcon-port');
        if (rconBadge) rconBadge.innerText = srv.rcon_port;
        
        // Minecraft version; for Java also whether the installed Java is new enough to run it
        const esc = t => this.escapeHtml(String(t));
        let versionNote = '';
        if (srv.edition === 'Java' && srv.java_required) {
            if (!srv.java_installed) {
                versionNote = `<p class="text-xs text-red-500 mt-1">Needs Java ${esc(srv.java_required)}; Java isn't installed</p>`;
            } else if (srv.java_installed < srv.java_required) {
                versionNote = `<p class="text-xs text-red-500 mt-1">Needs Java ${esc(srv.java_required)}; installed: Java ${esc(srv.java_installed)}</p>`;
            } else {
                versionNote = `<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Java ${esc(srv.java_installed)} (needs ${esc(srv.java_required)}+)</p>`;
            }
        } else if (!srv.version && srv.edition === 'Bedrock') {
            versionNote = `<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Shown after the server's next start</p>`;
        }
        if (srv.update) {
            versionNote += `<p class="text-xs mt-1"><button onclick="ServerApp.switchTab('maintenance')" class="text-theme underline" title="Open Maintenance to update">⬆ Update available: ${esc(srv.update.label)}</button></p>`;
        }
        document.getElementById('overview-stats').innerHTML = `
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Status</p><p class="font-bold uppercase ${srv.status === 'online' ? 'text-green-500' : 'text-red-500'}">${srv.status}</p></div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Engine</p><p class="font-bold text-blue-600 dark:text-blue-400">${srv.edition}</p></div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Minecraft Version</p><p class="font-bold text-gray-900 dark:text-gray-200">${srv.version ? esc(srv.version) : 'Unknown'}</p>${versionNote}</div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Port (TCP/UDP)</p><p class="font-bold text-gray-900 dark:text-gray-200">${srv.port}</p></div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Max RAM</p><p class="font-bold text-gray-900 dark:text-gray-200">${srv.ram}</p></div>
        `;
        document.getElementById('overview-notes').innerHTML = this.serverNotes(srv);
        document.getElementById('overview-crash').innerHTML = '';
        this.loadCrash();
        this.perfData = null;
        document.getElementById('overview-perf').innerHTML = '<p class="text-sm text-gray-500">Loading performance…</p>';

        // A search typed for the previous server shouldn't hide this one's properties
        const propSearch = document.getElementById('prop-search');
        if (propSearch) propSearch.value = '';

        this.switchTab('overview');
        if (this.can('settings', srv)) this.switchConfigSubTab('visual');
    },

    // --- 1.17.1: the "Last crash" card on the Overview (core/crashes.py)
    async loadCrash() {
        const srv = this.activeServer;
        const box = document.getElementById('overview-crash');
        if (!srv || !box) return;
        let rec = null;
        try {
            const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/crash`);
            if (res.ok) rec = (await res.json()).crash;
        } catch (e) { /* no card */ }
        if (this.activeServer !== srv) return;
        this.crash = rec;
        box.innerHTML = rec && !rec.dismissed ? this.crashCard(rec) : '';
    },

    crashCard(rec) {
        const esc = t => this.escapeHtml(String(t));
        const admin = this.isAdmin();
        const noun = rec.noun || 'mod';
        const verb = rec.crashed === false ? 'stopped' : 'crashed';
        const causes = (rec.causes || []).map(c => `
            <div class="mt-2">
                <p class="font-bold text-gray-900 dark:text-white">${esc(c.title)}</p>
                <p class="text-gray-800 dark:text-gray-200">${esc(c.text)}</p>
                <p class="text-gray-600 dark:text-gray-400"><b>Fix:</b> ${esc(c.fix)}</p>
            </div>`).join('');
        const suspects = (rec.suspects || []).filter(s => s.present !== false).map(s => `
            <div class="mt-2 flex flex-wrap items-center gap-2">
                <span>Likely ${esc(noun)}: <b>${esc(s.name)}</b> <span class="font-mono text-xs text-gray-500 dark:text-gray-400">${esc(s.file)}</span>${s.enabled ? '' : ' (turned off)'}</span>
                ${admin && s.enabled ? `<button onclick="ServerApp.crashTurnOff(${this.jsArg(s.file)})" class="bg-theme hover-bg-theme text-white px-3 py-1.5 rounded text-sm font-bold">Turn off and restart</button>` : ''}
            </div>`).join('');
        const undo = rec.undo && admin ? `
            <div class="mt-2 flex flex-wrap items-center gap-2">
                <span>The last ${esc(noun)} update was ${esc(this.fmtAgo(rec.undo.at))} (${esc((rec.undo.files || []).join(', '))}).</span>
                <button onclick="ServerApp.crashUndo()" class="bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-3 py-1.5 rounded text-sm font-bold">Undo the last ${esc(noun)} update</button>
            </div>` : '';
        const none = !causes && !suspects ? `<p class="mt-2 text-gray-700 dark:text-gray-300">No known cause was found. The highlighted lines below are the errors to look at.</p>` : '';
        const lines = (rec.lines || []);
        const hl = lines.filter(l => l.hl).length;
        const log = lines.length ? `
            <details class="mt-3">
                <summary class="cursor-pointer text-sm text-theme">${esc(rec.source || 'Log')}: ${hl} error line${hl === 1 ? '' : 's'} highlighted (${lines.length} lines)</summary>
                <pre class="mt-2 max-h-80 overflow-auto text-xs font-mono p-2 rounded bg-gray-900 text-gray-300">${lines.map(l => l.hl
                    ? `<span class="text-red-300 bg-red-900/40">${esc(l.text)}</span>` : esc(l.text)).join('\n')}</pre>
            </details>` : '';
        return `
            <section class="p-4 rounded border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/20 text-sm text-gray-900 dark:text-gray-100">
                <div class="flex items-start justify-between gap-2">
                    <h3 class="font-bold text-base">💥 Last crash: ${esc(verb)} ${esc(this.fmtAgo(rec.at))}${rec.exit ? ` <span class="font-normal text-xs text-gray-500 dark:text-gray-400">(${esc(rec.exit)})</span>` : ''}</h3>
                    ${this.can('power') ? `<button onclick="ServerApp.dismissCrash()" class="text-xs underline text-gray-600 dark:text-gray-400 whitespace-nowrap">Dismiss</button>` : ''}
                </div>
                ${causes}${suspects}${undo}${none}${log}
            </section>`;
    },

    async crashTurnOff(file) {
        const srv = this.activeServer;
        if (!confirm(`Turn off ${file} (renamed to ${file}.disabled) and restart ${srv.name}?`)) return;
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/crash/turn-off`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file }) });
        if (!res.ok) return alert(await this.apiError(res));
        this.addonsChanged = this.addonsChanged || {};
        this.addonsChanged[srv.name] = true;
        await this.loadCrash();
    },

    async crashUndo() {
        const srv = this.activeServer;
        if (!confirm(`Put back the files the last update replaced and restart ${srv.name}?`)) return;
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/crash/undo`, { method: 'POST' });
        if (!res.ok) return alert(await this.apiError(res));
        this.addonsChanged = this.addonsChanged || {};
        this.addonsChanged[srv.name] = true;
        await this.loadCrash();
    },

    async dismissCrash() {
        const srv = this.activeServer;
        await api(`/api/servers/${encodeURIComponent(srv.name)}/crash`, { method: 'DELETE' });
        await this.loadCrash();
    },

    // A running Phantom proxy with a problem: worth a marker in the sidebar
    hasProxyProblem(srv) {
        return (srv.proxies || []).some(p => p.active === 'active' && p.problems && p.problems.length);
    },

    // Online but players can't connect: worth a marker in the sidebar too
    hasConnectProblem(srv) {
        return srv.status === 'online' && !!(srv.transport_rejected || srv.signaling_closed);
    },

    // Overview notes: how Bedrock players can join, and Phantom proxies the panel didn't create
    serverNotes(srv) {
        const esc = t => this.escapeHtml(String(t));
        const warn = 'p-3 rounded border border-yellow-300 bg-yellow-50 text-yellow-800 text-sm';
        const info = 'p-3 rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 text-sm';
        const notes = [];
        if (srv.edition === 'Bedrock' && srv.transport_rejected) {
            notes.push(`<div class="${warn}"><b>Players can't connect:</b> at its last start this server said it doesn't support ` +
                `transport=${esc(srv.transport || '(not set)')}. This release only accepts NetherNet: set transport=nethernet in Properties and restart.</div>`);
        } else if (srv.edition === 'Bedrock' && srv.signaling_closed) {
            const cause = srv.udp_ports_address
                ? ` The likely cause is server-udp-ports=${esc(srv.udp_ports)}: with a public address in front, this release ` +
                  `doesn't open the port. Use server-udp-ports=${esc(srv.udp_ports_plain)} instead and restart.`
                : '';
            notes.push(`<div class="${warn}"><b>Players can't connect:</b> the server started but isn't accepting clients ` +
                `on TCP port ${esc(srv.port)} (no "Accepting clients" line in its log since the last start).${cause}</div>`);
        } else if (srv.edition === 'Bedrock' && srv.transport === 'nethernet' && srv.udp_ports_address) {
            notes.push(`<div class="${warn}"><b>server-udp-ports=${esc(srv.udp_ports)}:</b> on Bedrock 1.26.63.1 the form with a public address ` +
                `stops the server from accepting clients on TCP port ${esc(srv.port)}. ` +
                `Use server-udp-ports=${esc(srv.udp_ports_plain)} instead and restart.</div>`);
        } else if (srv.edition === 'Bedrock' && srv.transport === 'nethernet') {
            const udp = srv.udp_ports
                ? `gameplay uses UDP ${esc(srv.udp_ports)}`
                : 'gameplay uses random UDP ports (set server-udp-ports to a fixed port to make forwarding possible)';
            notes.push(`<div class="${info}"><b>NetherNet:</b> players connect to TCP port ${esc(srv.port)} (signaling); ${udp}. ` +
                `For players outside your network, forward both through your router.</div>`);
        }
        (srv.proxies || []).forEach(p => {
            const running = p.active === 'active';
            const state = `${running ? 'running' : 'stopped'}, ${p.enabled === 'enabled' ? 'starts at boot' : 'doesn\'t start at boot'}`;
            const problems = (p.problems || []).map(x => `<li>${esc(x)}</li>`).join('');
            const head = `<b>Phantom proxy ${esc(p.unit)}</b> (${esc(state)}): forwards to ${esc(p.target || '?')}, listens on UDP ${esc((p.listens || []).join(', '))}.`;
            if (running && problems) {
                notes.push(`<div class="${warn}">${head}<ul class="list-disc ml-5 mt-1 space-y-1">${problems}</ul>` +
                    `<p class="mt-1">Stop it with: <span class="font-mono">sudo systemctl disable --now ${esc(p.unit)}</span></p></div>`);
            } else {
                const note = problems ? ' Not a problem while it stays stopped.' : '';
                notes.push(`<div class="${info}">${head}${note}</div>`);
            }
        });
        return notes.join('');
    },

	chatInterval: null,
	chatHistory: {}, // Object mapping server names to arrays
	chatHistoryIndex: -1,
	
    switchTab(tabId) {
        // Safely iterate all possible tabs to prevent null errors if a tab doesn't exist
        if (this.fileEditor && this.fileEditor.dirty && tabId !== 'files'
            && !confirm('Discard your unsaved changes to ' + this.fileEditor.path + '?')) return;
        if (tabId !== 'files') this.fileEditor = null;
        ['overview', 'properties', 'maintenance', 'backups', 'chat', 'world', 'files', 'addons'].forEach(t => {
            const tab = document.getElementById(`tab-${t}`);
            const btn = document.getElementById(`tab-btn-${t}`);
            if (tab) tab.classList.add('hidden-tab');
            if (btn) btn.classList.remove('tab-active');
        });
        
        const activeTab = document.getElementById(`tab-${tabId}`);
        const activeBtn = document.getElementById(`tab-btn-${tabId}`);
        if (activeTab) activeTab.classList.remove('hidden-tab');
        if (activeBtn) activeBtn.classList.add('tab-active');
        
        if (tabId === 'overview') this.startPerfPolling(); else this.stopPerfPolling();
        // Clear chat polling if we navigate away
        if (this.chatInterval) {
            clearInterval(this.chatInterval);
            this.chatInterval = null;
        }
        
        if (tabId === 'properties') this.switchConfigSubTab(this.firstConfigSubTab());
        if (tabId === 'maintenance') { this.refreshApplyTemplates(); this.loadRestarts(); this.loadUpdateOptions().then(() => this.loadModpack()); this.loadAutoUpdate(); this.loadConvert(); this.loadJavaChoice(); this.loadResources(); }
        if (tabId === 'world') this.loadWorldName();
        if (tabId === 'backups') this.loadBackups();
        if (tabId === 'files') this.loadFiles(this.filesServer === this.serverKey(this.activeServer) ? this.filesPath : '');
        if (tabId === 'addons') this.loadAddons();
        if (tabId === 'chat') {
            this.loadChat();
            this.chatInterval = setInterval(() => this.loadChat(), 3000); // Poll every 3 seconds
            this.openChatCommands();
        }
    },

    async clearChat() {
        if (!this.activeServer) return;
        this.rconOutputs = [];
        this.chatHistoryIndex = -1;
        // Tell the backend, otherwise the next 3-second poll just repaints the old history
        await api(`/api/servers/${this.activeServer.name}/chat/clear`, { method: 'POST' });
        document.getElementById('chat-history').innerHTML = '<p class="text-gray-500 italic">Chat cleared...</p>';
    },

    serverKey(srv) {
        return srv ? `${srv.node || ''}/${srv.name}` : '';
    },

    escapeHtml(t) {
        return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },

    async loadChat() {
        if (!this.activeServer) return;
        const serverName = this.activeServer.name;
        const box = document.getElementById('chat-history');
        let data;
        try {
            const res = await api(`/api/servers/${serverName}/chat`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            data = await res.json();
        } catch (e) {
            box.innerHTML = `<p class="text-red-500 italic">Could not load chat history (${this.escapeHtml(e.message)}).</p>`;
            return;
        }
        // The user may have switched servers while this request was in flight
        if (!this.activeServer || this.activeServer.name !== serverName) return;
        
        // Increased threshold to 50px to account for sub-pixel rendering and line-heights
        const isScrolledToBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 50;
        
        if (!data.messages || data.messages.length === 0) {
            box.innerHTML = '<p class="text-gray-500 italic">No recent messages...</p>';
            return;
        }
        
        // Player-typed text must be escaped before it goes into innerHTML
        const esc = t => this.escapeHtml(t);
        let htmlContent = data.messages.map(m => {
            let colorClass = 'text-gray-800 dark:text-gray-300';
            if (m.startsWith('[Server]')) colorClass = 'text-theme font-bold';
            else if (m.startsWith('* ')) colorClass = 'text-gray-500 dark:text-gray-400 italic';
            return `<div class="${colorClass}">${esc(m)}</div>`;
        }).join('');
        
        // Append RCON command outputs at the bottom of the chat box
        if (this.rconOutputs && this.rconOutputs.length > 0) {
            htmlContent += this.rconOutputs.map(out => `<div class="text-blue-600 dark:text-blue-400 font-bold whitespace-pre-wrap mt-3 border-l-2 border-blue-500 pl-2">${esc(out)}</div>`).join('');
        }
        
        box.innerHTML = htmlContent;
        
        // Wait for the browser to paint the new DOM nodes before reading the new scrollHeight
        if (isScrolledToBottom) {
            requestAnimationFrame(() => {
                box.scrollTop = box.scrollHeight;
            });
        }
    },

    handleChatKey(event) {
		const input = document.getElementById('chat-input');
		if (!this.activeServer) return;
		
		const serverName = this.activeServer.name;
		// Initialize the array for this server if it doesn't exist yet
		if (!this.chatHistory[serverName]) this.chatHistory[serverName] = [];
		
		// Suggestions open: arrows move through them, Tab fills one in, Esc closes them.
		// Enter still sends, unless a suggestion was picked with the arrows first.
		const sug = this.chatSuggestion;
		if (sug && sug.items.length) {
			if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
				event.preventDefault();
				const n = sug.items.length, step = event.key === 'ArrowDown' ? 1 : -1;
				this.chatSuggestIndex = this.chatSuggestIndex < 0 ? (step > 0 ? 0 : n - 1) : (this.chatSuggestIndex + step + n) % n;
				this.renderChatSuggest();
				return;
			}
			if (event.key === 'Tab' || (event.key === 'Enter' && this.chatSuggestIndex >= 0)) {
				event.preventDefault();
				this.pickChatSuggestion(Math.max(this.chatSuggestIndex, 0));
				return;
			}
		}
		if (event.key === 'Escape' && sug) {
			this.chatSuggestDismissed = input.value;
			this.hideChatSuggest(true);
			return;
		}

		if (event.key === 'Enter') {
			this.sendChat();
		} else if (event.key === 'ArrowUp') {
			event.preventDefault(); 
			if (this.chatHistory[serverName].length > 0) {
				this.chatHistoryIndex = Math.min(this.chatHistoryIndex + 1, this.chatHistory[serverName].length - 1);
				input.value = this.chatHistory[serverName][this.chatHistoryIndex];
				this.hideChatSuggest(true);
			}
		} else if (event.key === 'ArrowDown') {
			event.preventDefault();
			if (this.chatHistoryIndex > 0) {
				this.chatHistoryIndex -= 1;
				input.value = this.chatHistory[serverName][this.chatHistoryIndex];
			} else if (this.chatHistoryIndex === 0) {
				this.chatHistoryIndex = -1;
				input.value = '';
			}
			this.hideChatSuggest(true);
		}
	},

    // Sends what is in the box, or `text` (a quick command or one built in the Commands form)
    async sendChat(text) {
		const input = document.getElementById('chat-input');
		const typed = typeof text !== 'string';
		const msg = (typed ? input.value : text).trim();
		if (!msg || !this.activeServer) return;
		this.hideChatSuggest(true);
		
		const serverName = this.activeServer.name;
		if (!this.chatHistory[serverName]) this.chatHistory[serverName] = [];

		// Save to history and reset index
		if (this.chatHistory[serverName][0] !== msg) {
			this.chatHistory[serverName].unshift(msg);
			if (this.chatHistory[serverName].length > 50) this.chatHistory[serverName].pop();
		}
		this.chatHistoryIndex = -1;

        // 1. Optimistic UI Update: Display the message instantly
        const box = document.getElementById('chat-history');
        if (box.innerHTML.includes('No recent messages') || box.innerHTML.includes('Loading chat')) {
            box.innerHTML = '';
        }

        // Update this line inside sendChat()
		// Match the format the history endpoint returns, so the line doesn't change on the next poll
		const displayMsg = msg.startsWith('/') ? `<span class="text-blue-400">[Console]</span> ${this.escapeHtml(msg)}` : `[Server] ${this.escapeHtml(msg)}`;
		box.innerHTML += `<div class="text-theme font-bold">${displayMsg}</div>`;
        box.scrollTop = box.scrollHeight;
        if (typed) input.value = '';

        // 2. Send the payload to the backend API
        const res = await api(`/api/servers/${this.activeServer.name}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: msg })
        });

        let data;
        try {
            data = await res.json();
        } catch (e) {
            data = { status: 'error', message: `Send failed (HTTP ${res.status}).` };
        }
    
		if (data.status === 'success' && data.response) {
			// Use .includes() to catch the command even if a pfid is prepended
			if (!data.response.includes('tellraw')) {
				this.rconOutputs.push(`[Console Output]\n${data.response}`);
				if (this.rconOutputs.length > 5) this.rconOutputs.shift();
			}
		} else if (data.status === 'info' || data.status === 'error') {
			alert(data.message);
		}

		this.loadChat(); // Refresh instantly to paint the output to the screen
    },

    // --- Live Chat: suggestions, the Commands form and quick commands (1.21.4) ---
    // The logic is in chat-commands.js (ChatCommands); the command list is static/commands.json.
    chatCatalog: null,
    chatAssist: {},            // server key -> { at, data } from GET .../chat/assist
    chatSuggestion: null,
    chatSuggestIndex: -1,
    chatSuggestDismissed: null,
    chatBuilderCmd: null,

    // What ChatCommands needs to know about the open server
    chatEnv() {
        const srv = this.activeServer || {};
        const assist = (this.chatAssist[this.serverKey(srv)] || {}).data || {};
        return { catalog: this.chatCatalog || { commands: [], lists: {}, quick: [], groups: [] },
                 edition: srv.edition === 'Bedrock' ? 'bedrock' : 'java', minecraft: srv.minecraft || null,
                 online: assist.online || [], known: assist.known || [],
                 commands: assist.commands || [], gamerules: assist.gamerules || [] };
    },

    async openChatCommands() {
        this.closeChatBuilder();
        this.hideChatSuggest(true);
        if (!this.chatCatalog) {
            try {
                const res = await fetch('/static/commands.json');
                if (res.ok) this.chatCatalog = await res.json();
            } catch (e) { /* typing commands by hand still works */ }
        }
        this.renderChatQuick();
        this.loadChatAssist();
    },

    // Who is online and what this server's own commands are. Asked again after 20 seconds,
    // when the box is used, so a player who just joined shows up.
    async loadChatAssist(force) {
        const srv = this.activeServer;
        if (!srv) return;
        const key = this.serverKey(srv);
        const hit = this.chatAssist[key];
        if (!force && hit && Date.now() - hit.at < 20000) return;
        this.chatAssist[key] = { at: Date.now(), data: hit ? hit.data : null };
        try {
            const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/chat/assist`);
            if (!res.ok) return;      // a node on an older version: no player names, the rest works
            this.chatAssist[key] = { at: Date.now(), data: await res.json() };
            if (this.activeServer && this.serverKey(this.activeServer) === key && document.activeElement === document.getElementById('chat-input')) this.updateChatSuggest();
        } catch (e) { /* suggestions without player names */ }
    },

    renderChatQuick() {
        const row = document.getElementById('chat-quick');
        if (!row) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const env = this.chatEnv();
        const cmds = ChatCommands.forEdition(env);
        if (!cmds.length) { row.innerHTML = ''; return; }
        const groups = env.catalog.groups.map(g => {
            const opts = cmds.map((c, i) => c.group === g ? `<option value="${i}">${esc(c.label)} (/${esc(c.name)})</option>` : '').join('');
            return opts ? `<optgroup label="${esc(g)}">${opts}</optgroup>` : '';
        }).join('');
        const chip = 'shrink-0 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-800 dark:text-gray-200 border border-gray-300 dark:border-gray-600 px-3 py-2 rounded text-sm font-medium transition';
        const quick = (env.catalog.quick || []).filter(q => q.editions.includes(env.edition))
            .map(q => `<button type="button" title="/${esc(q.command)}" data-command="${esc(q.command)}" onclick="ServerApp.sendChat('/' + this.dataset.command)" class="${chip}">${esc(q.label)}</button>`).join('');
        row.innerHTML = `<select id="chat-cmd-select" aria-label="Commands" onchange="ServerApp.openChatBuilder(this.value)" class="shrink-0 max-w-[60%] md:max-w-xs bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded px-2 py-2 text-sm font-bold text-gray-900 dark:text-white shadow-sm">
                <option value="">Commands…</option>${groups}</select>${quick}`;
    },

    updateChatSuggest() {
        const input = document.getElementById('chat-input');
        if (!input || !this.activeServer || document.activeElement !== input) return;
        if (this.chatSuggestDismissed !== null && this.chatSuggestDismissed === input.value) return;
        this.chatSuggestDismissed = null;
        this.loadChatAssist();
        const sug = ChatCommands.suggest(input.value, input.selectionStart, this.chatEnv());
        this.chatSuggestion = (sug.items.length || sug.hint) ? sug : null;
        this.chatSuggestIndex = -1;
        this.renderChatSuggest();
    },

    renderChatSuggest() {
        const box = document.getElementById('chat-suggest');
        const sug = this.chatSuggestion;
        if (!box) return;
        if (!sug) { box.classList.add('hidden'); box.innerHTML = ''; return; }
        const esc = t => this.escapeHtml(String(t ?? ''));
        const hint = sug.hint ? `<div class="px-3 py-2 font-mono text-xs border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-500 dark:text-gray-400 sticky top-0 whitespace-normal break-words">
                ${sug.hint.parts.map(p => p.active ? `<span class="text-theme font-bold">${esc(p.text)}</span>` : esc(p.text)).join(' ')}
                ${sug.hint.about ? `<span class="font-sans ml-2">${esc(sug.hint.about)}${sug.hint.done ? '. Press Enter to run.' : ''}</span>` : ''}</div>` : '';
        const items = sug.items.map((it, i) => {
            const on = i === this.chatSuggestIndex;
            return `<div role="option" id="chat-sug-${i}" aria-selected="${on}" onpointerdown="event.preventDefault()" onclick="ServerApp.pickChatSuggestion(${i})"
                class="flex items-baseline gap-3 px-3 py-2 max-md:py-3 cursor-pointer ${on ? 'bg-gray-200 dark:bg-gray-700' : 'hover:bg-gray-100 dark:hover:bg-gray-700/60'}">
                <span class="font-mono font-bold shrink-0 ${it.kind === 'player' ? 'text-theme' : 'text-gray-900 dark:text-white'}">${it.kind === 'player' && it.detail === 'online' ? '🟢 ' : ''}${esc(it.label)}</span>
                <span class="text-xs text-gray-500 dark:text-gray-400 truncate">${esc(it.detail)}</span></div>`;
        }).join('');
        box.innerHTML = hint + items;
        box.classList.remove('hidden');
        const sel = document.getElementById(`chat-sug-${this.chatSuggestIndex}`);
        if (sel) sel.scrollIntoView({ block: 'nearest' });
    },

    // now: close at once. Otherwise wait a moment, so a tap on a suggestion still lands.
    hideChatSuggest(now) {
        const close = () => {
            if (!now && document.activeElement === document.getElementById('chat-input')) return;
            this.chatSuggestion = null;
            this.chatSuggestIndex = -1;
            this.renderChatSuggest();
        };
        if (now) close(); else setTimeout(close, 200);
    },

    pickChatSuggestion(i) {
        const input = document.getElementById('chat-input');
        const sug = this.chatSuggestion;
        if (!sug || !sug.items[i]) return;
        const done = ChatCommands.apply(input.value, sug, sug.items[i]);
        input.value = done.text;
        input.focus();
        input.setSelectionRange(done.caret, done.caret);
        this.updateChatSuggest();     // straight on to what the command wants next
    },

    // The Commands list: a form that asks only for what the picked command needs
    openChatBuilder(index) {
        const env = this.chatEnv();
        const cmd = ChatCommands.forEdition(env)[Number(index)];
        if (index === '' || !cmd) { this.closeChatBuilder(); return; }
        this.chatBuilderCmd = cmd;
        this.loadChatAssist();
        const esc = t => this.escapeHtml(String(t ?? ''));
        const field = 'w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white';
        const change = 'oninput="ServerApp.chatBuilderChanged()" onchange="ServerApp.chatBuilderChanged()"';
        const playerSelect = (id, arg, selectors) => {
            const opt = n => `<option value="${esc(n)}">${esc(n)}</option>`;
            const online = env.online.map(opt).join('');
            const known = arg.online ? '' : env.known.map(opt).join('');
            return `<select id="${id}" ${change} class="${field}">
                    <option value="">Choose a player…</option>
                    ${selectors ? ChatCommands.SELECTORS.map(([s, what]) => `<option value="${s}">${esc(what[0].toUpperCase() + what.slice(1))} (${s})</option>`).join('') : ''}
                    ${online ? `<optgroup label="Online now">${online}</optgroup>` : ''}
                    ${known ? `<optgroup label="Seen before">${known}</optgroup>` : ''}
                    <option value="__other">Another name…</option></select>
                <input type="text" id="${id}-other" placeholder="Player name" autocomplete="off" autocapitalize="off" spellcheck="false" ${change} class="${field} mt-2 hidden">`;
        };
        const xyz = id => `<div id="${id}-xyz" class="flex gap-2">${['x', 'y', 'z'].map(p =>
            `<input type="text" id="${id}-${p}" placeholder="${p}" aria-label="${p}" inputmode="text" autocomplete="off" ${change} class="${field} min-w-0">`).join('')}</div>`;
        const fields = cmd.args.map((arg, i) => {
            const id = `cb-arg-${i}`;
            let html;
            if (arg.type === 'player') html = playerSelect(id, arg, false);
            else if (arg.type === 'target') html = playerSelect(id, arg, true);
            else if (arg.type === 'position') html = xyz(id);
            else if (arg.type === 'destination') {
                html = `<select id="${id}-mode" ${change} class="${field} mb-2"><option value="player">To a player</option><option value="xyz">To a place (x y z)</option></select>
                    <div id="${id}-player">${playerSelect(id, arg, false)}</div><div class="hidden" id="${id}-place">${xyz(id)}</div>`;
            } else if (arg.type === 'choice') {
                html = `<select id="${id}" ${change} class="${field}">${arg.optional ? '<option value="">(leave out)</option>' : ''}
                    ${ChatCommands.listFor(arg, env).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}</select>`;
            } else if (arg.type === 'suggest') {
                html = `<input type="text" id="${id}" list="${id}-list" autocomplete="off" autocapitalize="off" spellcheck="false" ${change} class="${field}">
                    <datalist id="${id}-list">${ChatCommands.listFor(arg, env).map(c => `<option value="${esc(c)}"></option>`).join('')}</datalist>`;
            } else if (arg.type === 'number') {
                html = `<input type="number" id="${id}" ${arg.min !== undefined ? `min="${Number(arg.min)}"` : ''} step="1" inputmode="numeric" ${change} class="${field}">`;
            } else {
                html = `<input type="text" id="${id}" autocomplete="off" ${change} class="${field}">`;
            }
            return `<label class="block min-w-0"><span class="block text-xs font-bold text-gray-600 dark:text-gray-400 mb-1 capitalize">${esc(arg.name)}${arg.optional ? ' <span class="font-normal normal-case">(optional)</span>' : ''}</span>${html}</label>`;
        }).join('');
        const box = document.getElementById('chat-builder');
        box.innerHTML = `<div class="flex justify-between items-start gap-3 mb-3">
                <div class="min-w-0"><p class="font-bold text-gray-900 dark:text-white">${esc(cmd.label)}</p>
                <p class="text-xs text-gray-500 dark:text-gray-400">${esc(cmd.about)}.</p></div>
                <button type="button" onclick="ServerApp.closeChatBuilder()" aria-label="Close" class="shrink-0 text-xl leading-none px-2 text-gray-500 hover:text-gray-900 dark:hover:text-white">✕</button></div>
            ${fields ? `<div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">${fields}</div>` : ''}
            <p id="cb-error" class="hidden text-xs text-yellow-600 dark:text-yellow-500 mb-2"></p>
            <div class="flex flex-wrap items-center gap-2">
                <code id="cb-preview" class="flex-1 min-w-0 basis-full md:basis-0 font-mono text-sm bg-gray-100 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded px-3 py-2 text-gray-900 dark:text-white break-words"></code>
                <button type="button" id="cb-run" onclick="ServerApp.runChatBuilder()" class="bg-theme hover-bg-theme text-white px-5 py-2 rounded font-bold transition shadow-sm disabled:opacity-50">Run</button>
                <button type="button" id="cb-edit" onclick="ServerApp.editChatBuilder()" title="Put the command in the message box to change it" class="bg-gray-500 hover:bg-gray-600 text-white px-4 py-2 rounded font-bold transition shadow-sm">Edit</button>
            </div>`;
        box.classList.remove('hidden');
        this.chatBuilderChanged();
        const first = box.querySelector('select, input');
        if (first && window.matchMedia('(min-width: 768px)').matches) first.focus();
    },

    // What the form holds now: { argName: text }
    chatBuilderValues() {
        const val = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
        const show = (id, on) => { const el = document.getElementById(id); if (el) el.classList.toggle('hidden', !on); };
        const player = id => {
            const other = val(id) === '__other';
            show(`${id}-other`, other);
            return other ? val(`${id}-other`) : val(id);
        };
        const xyz = id => ['x', 'y', 'z'].map(p => val(`${id}-${p}`)).filter(Boolean).join(' ');
        const values = {};
        (this.chatBuilderCmd ? this.chatBuilderCmd.args : []).forEach((arg, i) => {
            const id = `cb-arg-${i}`;
            if (arg.type === 'player' || arg.type === 'target') values[arg.name] = player(id);
            else if (arg.type === 'position') values[arg.name] = xyz(id);
            else if (arg.type === 'destination') {
                const place = val(`${id}-mode`) === 'xyz';
                show(`${id}-player`, !place);
                show(`${id}-place`, place);
                values[arg.name] = place ? xyz(id) : player(id);
            } else values[arg.name] = val(id);
        });
        return values;
    },

    chatBuilderChanged() {
        if (!this.chatBuilderCmd) return null;
        const built = ChatCommands.build(this.chatBuilderCmd, this.chatBuilderValues());
        const ready = !built.missing.length && !built.error;
        document.getElementById('cb-preview').innerText = built.command + built.missing.map(m => ` <${m}>`).join('');
        document.getElementById('cb-run').disabled = !ready;
        const err = document.getElementById('cb-error');
        err.innerText = built.error || '';
        err.classList.toggle('hidden', !built.error);
        return ready ? built.command : null;
    },

    runChatBuilder() {
        const command = this.chatBuilderChanged();
        const cmd = this.chatBuilderCmd;
        if (!command || !cmd) return;
        if (cmd.confirm && !confirm(`Run ${command} on ${this.activeServer.name}?\n\n${cmd.about}.`)) return;
        this.closeChatBuilder();
        this.sendChat(command);
    },

    // Puts the command, as far as it is filled in, into the message box to finish by hand
    editChatBuilder() {
        if (!this.chatBuilderCmd) return;
        const cmd = this.chatBuilderCmd;
        const built = ChatCommands.build(cmd, this.chatBuilderValues());
        this.closeChatBuilder();
        const input = document.getElementById('chat-input');
        input.value = built.command + (cmd.args.length ? ' ' : '');
        input.focus();
        this.updateChatSuggest();
    },

    closeChatBuilder() {
        this.chatBuilderCmd = null;
        const box = document.getElementById('chat-builder');
        if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
        const sel = document.getElementById('chat-cmd-select');
        if (sel) sel.value = '';
    },

    switchConfigSubTab(subId) {
		['visual', 'raw', 'players', 'access', 'permissions'].forEach(t => {
			document.getElementById(`config-${t}`).classList.add('hidden-tab');
			document.getElementById(`subtab-${t}`).classList.remove('subtab-active');
			document.getElementById(`subtab-${t}`).classList.add('text-gray-600', 'dark:text-gray-400');
		});
		document.getElementById(`config-${subId}`).classList.remove('hidden-tab');
		document.getElementById(`subtab-${subId}`).classList.add('subtab-active');
		document.getElementById(`subtab-${subId}`).classList.remove('text-gray-600', 'dark:text-gray-400');
		
		if (subId === 'visual') this.loadProperties(this.activeServer.name);
		if (subId === 'raw') this.loadRawProperties();
		if (subId === 'players') this.loadPlayers();
		if (subId === 'access') this.loadAccess();
		if (subId === 'permissions') {
			// Dynamically change the editor title based on edition
			const titleEl = document.getElementById('permissions-editor-title');
			if (titleEl) {
				titleEl.innerText = this.activeServer.edition === 'Java' ? 'ops.json Editor' : 'permissions.json Editor';
			}
			this.loadRawPermissions();
		}
	},

    async loadRawPermissions() {
        const res = await api(`/api/servers/${this.activeServer.name}/permissions/raw`);
        const data = await res.json();
        document.getElementById('permissions-editor').value = data.content;
    },

    async saveRawPermissions() {
        const content = document.getElementById('permissions-editor').value;
        await api(`/api/servers/${this.activeServer.name}/permissions/raw`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
        });
        alert("Permissions file saved. Restart server to apply.");
    },

    // One card of a visual properties editor: the name on the left and, on the right, a True/False
    // list, a list of the known choices, a number box or a text box, picked from the value.
    // Shared by Configuration's Visual Editor and the Files tab's (PropFile.widget decides).
    propRowHtml(key, value, inputClass, attrs) {
        const esc = t => this.escapeHtml(String(t ?? ''));   // values go into HTML attributes (a motd may contain ")
        const cls = `${inputClass} w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-gray-900 dark:text-white`;
        const w = PropFile.widget(key, value);
        let input;
        if (w.type === 'bool') {
            input = `<select ${attrs} class="${cls}">${w.options.map((o, i) =>
                `<option value="${esc(o)}" ${value === o ? 'selected' : ''}>${i ? 'False' : 'True'}</option>`).join('')}</select>`;
        } else if (w.type === 'enum') {
            input = `<select ${attrs} class="${cls}">${w.options.map(o =>
                `<option value="${esc(o)}" ${value === o ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
        } else {
            input = `<input type="${w.type === 'number' ? 'number' : 'text'}" ${w.type === 'number' ? 'step="any"' : ''} ${attrs} value="${esc(value)}" class="${cls}">`;
        }
        return `
                <div class="prop-row bg-white dark:bg-gray-800 p-4 rounded border border-gray-200 dark:border-gray-700 flex justify-between items-center gap-4 shadow-sm" data-key="${esc(key)}">
                    <div class="flex-1 min-w-0"><label class="text-sm font-bold text-gray-700 dark:text-gray-300 break-words">${esc(key)}</label></div>
                    <div class="w-1/2">${input}</div>
                </div>`;
    },

    async loadProperties(serverName) {
        const res = await api(`/api/servers/${serverName}/properties`);
        const props = await res.json();
        const editor = document.getElementById('properties-editor');
        editor.innerHTML = '';

        let html = '';
        for (const [key, value] of Object.entries(props)) html += this.propRowHtml(key, value, 'prop-input', `data-key="${this.escapeHtml(key)}"`);
        editor.innerHTML = html;
        this.filterProperties();   // keep the current search applied after a reload or save
    },

    // Live filter for the Visual Editor. Matches the property name or its current value;
    // several words must all match ("rcon port"). Hidden rows stay in the page, so Save still sends everything.
    filterProperties() {
        const search = document.getElementById('prop-search');
        const rows = document.querySelectorAll('#properties-editor .prop-row');
        const terms = (search ? search.value : '').toLowerCase().split(/\s+/).filter(Boolean);
        let shown = 0;
        rows.forEach(row => {
            const input = row.querySelector('.prop-input');
            const haystack = `${row.dataset.key} ${input ? input.value : ''}`.toLowerCase();
            const match = terms.every(t => haystack.includes(t));
            row.style.display = match ? '' : 'none';
            if (match) shown++;
        });
        const count = document.getElementById('prop-search-count');
        if (count) count.innerText = terms.length ? `${shown} of ${rows.length}` : `${rows.length} properties`;
        const empty = document.getElementById('prop-search-empty');
        if (empty) empty.classList.toggle('hidden', !(terms.length && rows.length && shown === 0));
    },
    
    async saveProperties() {
        const inputs = document.querySelectorAll('.prop-input');
        const updatedProps = {};
        inputs.forEach(input => updatedProps[input.getAttribute('data-key')] = input.value);
        await api(`/api/servers/${this.activeServer.name}/properties`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ properties: updatedProps })
        });
        alert("Properties saved. Restart server to apply.");
    },

    async loadRawProperties() {
        const res = await api(`/api/servers/${this.activeServer.name}/properties/raw`);
        const data = await res.json();
        document.getElementById('raw-editor').value = data.content;
    },

    async saveRawProperties() {
        const content = document.getElementById('raw-editor').value;
        await api(`/api/servers/${this.activeServer.name}/properties/raw`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
        });
        alert("Raw file saved.");
    },

    // Consolidated list: players seen in the logs + everyone in ops.json / permissions.json
    async loadPlayers() {
		const serverName = this.activeServer.name;
		const list = document.getElementById('player-list');
		list.innerHTML = "<p class='text-gray-400'>Loading players...</p>";
		let data;
		try {
			const res = await api(`/api/servers/${serverName}/players`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			data = await res.json();
		} catch (e) {
			list.innerHTML = `<p class='text-red-500'>Could not load players (${this.escapeHtml(e.message)}).</p>`;
			return;
		}
		if (!this.activeServer || this.activeServer.name !== serverName) return;

		const esc = t => this.escapeHtml(t);
		const file = data.file || 'permissions file';
		const help = document.getElementById('player-list-help');
		if (help) help.innerText = `Players seen in the server logs, plus everyone with a role in ${file}.`;

		if (!data.players || data.players.length === 0) {
			list.innerHTML = `<p class='text-gray-400'>No players found in the logs or in ${esc(file)}.</p>`;
			return;
		}

		const roles = data.roles || ['operator', 'member'];
		const label = r => r.charAt(0).toUpperCase() + r.slice(1);
		const badge = (text, cls, title) => `<span title="${esc(title)}" class="text-xs px-2 py-0.5 rounded border ${cls}">${esc(text)}</span>`;

		list.innerHTML = data.players.map(p => {
			const badges = [
				p.in_log ? badge('seen in logs', 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400', 'This player appears in the server logs') : '',
				p.in_file ? badge(`in ${file}`, 'border-blue-300 dark:border-blue-700 text-blue-700 dark:text-blue-300', `This player has an entry in ${file}`) : '',
			].join(' ');
			const options = roles.map(r => `<option value="${esc(r)}" ${p.role === r ? 'selected' : ''}>${esc(label(r))}</option>`).join('');
			const idLine = p.id ? `<div class="text-xs text-gray-500 dark:text-gray-400 font-mono truncate">${esc(p.id)}</div>` : '';
			// Without a name we can't write a proper entry, so the role can only be changed in the raw editor
			const disabled = p.name_known ? '' : 'disabled title="Name unknown: edit this entry in Permissions (Raw)"';
			return `
			<div class="flex flex-wrap justify-between items-center gap-x-4 gap-y-2 p-3 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm">
				<div class="min-w-0">
					<div class="flex items-center gap-2 flex-wrap"><span class="font-bold text-gray-900 dark:text-white">${esc(p.name)}</span>${badges}</div>
					${idLine}
				</div>
				<select data-player="${esc(p.name)}" data-player-id="${esc(p.id || '')}" data-prev="${esc(p.role)}" ${disabled}
					onchange="ServerApp.setPlayerRole(this)" class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 p-1 rounded text-sm text-gray-800 dark:text-gray-300">
					${options}
				</select>
			</div>`;
		}).join('');
	},

    // Whitelist & Access: the whitelist/allowlist, players turned away, and the access log
    fmtAgo(ts) {
		if (!ts) return 'never seen';
		const s = Math.max(0, Date.now() / 1000 - ts);
		if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
		if (s < 86400) return `${Math.round(s / 3600)} h ago`;
		const d = Math.round(s / 86400);
		return `${d} day${d === 1 ? '' : 's'} ago`;
	},

    async loadAccess() {
		const server = this.activeServer.name, esc = t => this.escapeHtml(t === null || t === undefined ? '' : t);
		const list = document.getElementById('wl-list');
		list.innerHTML = `<p class="p-3 text-gray-400">Loading...</p>`;
		let d;
		try {
			const res = await api(`/api/servers/${encodeURIComponent(server)}/whitelist`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			d = await res.json();
		} catch (e) {
			list.innerHTML = `<p class="p-3 text-red-500">Could not load the whitelist (${esc(e.message)}).</p>`;
			return;
		}
		if (!this.activeServer || this.activeServer.name !== server) return;
		this.accessData = d;
		await this.loadModeration();
		const word = d.edition === 'java' ? 'whitelist' : 'allowlist';
		document.getElementById('wl-state').innerHTML = d.enabled
			? `<span class="text-green-600 dark:text-green-400 font-bold">On</span> <span class="text-gray-500 dark:text-gray-400">(${esc(d.setting)}=true)</span>`
			: `<span class="text-yellow-600 dark:text-yellow-400 font-bold">Off</span> <span class="text-gray-500 dark:text-gray-400">(anyone can join; turn on ${esc(d.setting)} in the Visual Editor)</span>`;
		document.getElementById('wl-help').innerText = `Players in ${d.file}. ` + (d.live
			? 'The server is running, so changes apply at once.'
			: d.running ? (d.edition === 'java' ? "RCON isn't set up, so changes apply when the server restarts. Open Live Chat once to set it up."
				: "Changes apply when the server restarts.")
			: 'Changes apply when the server starts.');
		list.innerHTML = d.players.length ? d.players.map(p => `
			<div class="flex items-center justify-between gap-3 p-3">
				<div class="min-w-0">
					<div class="font-bold text-gray-900 dark:text-white">${esc(p.name)}</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">${p.last_join ? `last joined ${esc(this.fmtAgo(p.last_join))}` : 'no join in the log'}${p.id ? ` · <span class="font-mono">${esc(p.id)}</span>` : ''}</div>
				</div>
				<button onclick="ServerApp.accessRemove(${this.jsArg(p.name)})" class="text-sm text-red-600 dark:text-red-400 hover:underline">Remove</button>
			</div>`).join('') : `<p class="p-3 text-gray-500 dark:text-gray-400">Nobody is on the ${word}${d.enabled ? ", so nobody can join" : ''}.</p>`;
		const away = d.turned_away || [];
		document.getElementById('wl-away-section').classList.toggle('hidden', !away.length);
		const reasons = { not_whitelisted: `not on the ${word}`, banned: 'banned', wrong_version: 'wrong game version', server_full: 'server full' };
		document.getElementById('wl-away').innerHTML = away.map(a => `
			<div class="flex items-center justify-between gap-3 p-3">
				<div><span class="font-bold text-gray-900 dark:text-white">${esc(a.name)}</span>
					<span class="text-xs text-gray-500 dark:text-gray-400">${a.count}× · ${esc(reasons[a.reason] || a.reason || 'refused')}</span></div>
				${a.reason === 'not_whitelisted' ? `<button onclick="ServerApp.accessAdd(${this.jsArg(a.name)})" class="text-sm text-theme hover:underline">Allow</button>`
				  : a.reason === 'banned' && this.modBanned(a.name) ? `<button onclick="ServerApp.modUnban(${this.jsArg(a.name)})" class="text-sm text-theme hover:underline">Unban</button>` : ''}
			</div>`).join('');
		this.loadAccessLog();
	},

    async accessAdd(name) {
		name = (name || '').trim();
		if (!name) return;
		await this.accessChange('add', name);
	},

    async accessRemove(name) {
		const word = this.accessData && this.accessData.edition === 'java' ? 'whitelist' : 'allowlist';
		if (!confirm(`Remove ${name} from the ${word}?`)) return;
		await this.accessChange('remove', name);
	},

    async accessChange(action, player) {
		let msg;
		try {
			const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/whitelist/${action}`, {
				method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ player })
			});
			const j = await res.json().catch(() => ({}));
			msg = res.ok ? j.message : `Not changed: ${j.detail || `HTTP ${res.status}`}`;
		} catch (e) {
			msg = `Not changed: ${e.message}`;
		}
		if (action === 'add') document.getElementById('wl-add-name').value = '';
		this.loadAccess();
		alert(msg);
	},

    accessLogSoon() {
		clearTimeout(this._accessLogTimer);
		this._accessLogTimer = setTimeout(() => this.loadAccessLog(), 300);
	},

    async loadAccessLog() {
		const server = this.activeServer.name, esc = t => this.escapeHtml(t === null || t === undefined ? '' : t);
		const q = new URLSearchParams({
			player: document.getElementById('al-player').value.trim(),
			event: document.getElementById('al-event').value,
			days: document.getElementById('al-days').value,
		});
		const rows = document.getElementById('al-rows');
		let d;
		try {
			const res = await api(`/api/servers/${encodeURIComponent(server)}/players/log?${q}`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			d = await res.json();
		} catch (e) {
			rows.innerHTML = `<tr><td colspan="4" class="p-2 text-red-500">Could not load the access log (${esc(e.message)}).</td></tr>`;
			return;
		}
		if (!this.activeServer || this.activeServer.name !== server) return;
		const word = this.accessData && this.accessData.edition === 'bedrock' ? 'allowlist' : 'whitelist';
		const reasons = Object.assign({}, d.reasons, { not_whitelisted: `not on the ${word}` });
		const label = e => e.ev === 'join' ? '<span class="text-green-600 dark:text-green-400">Joined</span>'
			: e.ev === 'leave' ? '<span class="text-gray-500 dark:text-gray-400">Left</span>'
			: `<span class="text-red-600 dark:text-red-400">Turned away</span> <span class="text-gray-500 dark:text-gray-400">(${esc(reasons[e.reason] || e.reason || 'refused')})</span>`;
		rows.innerHTML = d.events.length ? d.events.map(e => `
			<tr class="border-t border-gray-100 dark:border-gray-700">
				<td class="p-2 whitespace-nowrap text-gray-600 dark:text-gray-300" data-label="Time">${esc(new Date(e.ts * 1000).toLocaleString())}</td>
				<td class="p-2 font-bold text-gray-900 dark:text-white" data-label="Player">${esc(e.name)}</td>
				<td class="p-2" data-label="Event">${label(e)}</td>
				<td class="rp-card-actions p-2 text-right whitespace-nowrap">${this.modActions(e.name, 'text-xs')}</td>
			</tr>`).join('') : `<tr><td colspan="4" class="p-2 text-gray-500 dark:text-gray-400">Nothing in this period.</td></tr>`;
		document.getElementById('al-count').innerText = d.events.length >= 500
			? 'Showing the newest 500; narrow the filter to see more.'
			: `${d.events.length} event${d.events.length === 1 ? '' : 's'}. Kept for ${d.retention_days} days.`;
	},

    // Kick, ban and unban (1.16.2). Bans are Java only: Bedrock has no ban list.
    async loadModeration() {
		const server = this.activeServer.name, esc = t => this.escapeHtml(t === null || t === undefined ? '' : t);
		this.modData = null;
		let m;
		try {
			const res = await api(`/api/servers/${encodeURIComponent(server)}/moderation`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			m = await res.json();
		} catch (e) {
			document.getElementById('mod-online').innerHTML = `<p class="p-3 text-red-500">Could not load who's online (${esc(e.message)}).</p>`;
			return;
		}
		if (!this.activeServer || this.activeServer.name !== server) return;
		this.modData = m;
		document.getElementById('mod-online-note').innerText = !m.running ? 'The server is stopped.'
			: m.online_source === 'rcon' ? 'Live, from the server.'
			: 'From the server log; it can be up to 30 seconds behind.';
		document.getElementById('mod-online').innerHTML = m.online.length ? m.online.map(n => `
			<div class="flex items-center justify-between gap-3 p-3">
				<span class="font-bold text-gray-900 dark:text-white min-w-0 truncate"><span class="text-green-500">●</span> ${esc(n)}</span>
				<span class="flex gap-3 whitespace-nowrap">${this.modActions(n, 'text-sm')}</span>
			</div>`).join('') : `<p class="p-3 text-gray-500 dark:text-gray-400">${m.running ? 'Nobody is online.' : 'Nobody is online while the server is stopped.'}</p>`;

		document.getElementById('mod-ban-form').classList.toggle('hidden', !m.bans);
		document.getElementById('mod-banned').classList.toggle('hidden', !m.bans);
		document.getElementById('mod-ban-help').innerText = !m.bans
			? "Bedrock servers have no ban list. To keep someone out, turn on the allowlist and leave them off it; to remove someone now, use Kick."
			: `Players in banned-players.json can't join. ` + (m.live ? 'The server is running, so changes apply at once.'
				: m.running ? "RCON isn't set up, so bans can't change while the server runs. Open Live Chat once to set it up."
				: 'Changes apply when the server starts.');
		document.getElementById('mod-banned').innerHTML = m.banned.length ? m.banned.map(b => `
			<div class="flex items-center justify-between gap-3 p-3">
				<div class="min-w-0">
					<div class="font-bold text-gray-900 dark:text-white">${esc(b.name)}</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">${esc(b.reason || 'No reason given')}${b.created ? ` · ${esc(b.created)}` : ''}${b.source ? ` · ${b.source === 'Rcon' ? 'from the panel or another RCON tool' : b.source === 'Server' ? 'from the server console' : `by ${esc(b.source)}`}` : ''}${b.expires && b.expires !== 'forever' ? ` · until ${esc(b.expires)}` : ''}</div>
				</div>
				<button onclick="ServerApp.modUnban(${this.jsArg(b.name)})" class="text-sm text-theme hover:underline">Unban</button>
			</div>`).join('') : `<p class="p-3 text-gray-500 dark:text-gray-400">Nobody is banned.</p>`;
	},

    modBanned(name) {
		const m = this.modData;
		return !!(m && m.banned.some(b => b.name.toLowerCase() === String(name).toLowerCase()));
	},

    // Kick (if online) and Ban (Java, if not banned) buttons for one player
    modActions(name, size) {
		const m = this.modData;
		if (!m) return '';
		const online = m.online.some(n => n.toLowerCase() === String(name).toLowerCase());
		const out = [];
		if (online) out.push(`<button onclick="ServerApp.modKick(${this.jsArg(name)})" class="${size} text-yellow-700 dark:text-yellow-400 hover:underline">Kick</button>`);
		if (m.bans && !this.modBanned(name)) out.push(`<button onclick="ServerApp.modBan(${this.jsArg(name)})" class="${size} text-red-600 dark:text-red-400 hover:underline">Ban</button>`);
		return out.join(' ');
	},

    async modKick(name) {
		const reason = prompt(`Kick ${name}? They can join again straight away.\n\nReason shown to them (optional):`, '');
		if (reason === null) return;
		await this.modAction('kick', name, reason);
	},

    async modBan(name) {
		const reason = prompt(`Ban ${name}? They're removed if online and can't join until you unban them.\n\nReason (optional):`, '');
		if (reason === null) return;
		await this.modAction('ban', name, reason);
	},

    async modBanFromForm() {
		const name = document.getElementById('mod-ban-name').value.trim();
		if (!name) return;
		if (!confirm(`Ban ${name}?`)) return;
		const ok = await this.modAction('ban', name, document.getElementById('mod-ban-reason').value);
		if (ok) {
			document.getElementById('mod-ban-name').value = '';
			document.getElementById('mod-ban-reason').value = '';
		}
	},

    async modUnban(name) {
		if (!confirm(`Unban ${name}? They'll be able to join again.`)) return;
		await this.modAction('unban', name, '');
	},

    async modAction(action, player, reason) {
		let msg, ok = false;
		try {
			const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/players/${action}`, {
				method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ player, reason: reason || '' })
			});
			const j = await res.json().catch(() => ({}));
			ok = res.ok;
			msg = res.ok ? j.message : `Not done: ${j.detail || `HTTP ${res.status}`}`;
		} catch (e) {
			msg = `Not done: ${e.message}`;
		}
		await this.loadAccess();
		alert(msg);
		return ok;
	},

    async setPlayerRole(select) {
        const player = select.dataset.player;
        const role = select.value;
        select.disabled = true;
        let ok = false, detail = '';
        try {
            const res = await api(`/api/servers/${this.activeServer.name}/players/role`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ player, role, player_id: select.dataset.playerId || null })
            });
            ok = res.ok;
            if (!ok) {
                detail = `HTTP ${res.status}`;
                try { detail = (await res.json()).detail || detail; } catch (e) {}
            }
        } catch (e) {
            detail = e.message;
        }
        if (!ok) {
            select.value = select.dataset.prev;   // put the dropdown back to what is really in the file
            select.disabled = false;
            return alert(`Role not changed: ${detail}`);
        }
        alert(`${player} is now ${role}. Restart the server if required.`);
        this.loadPlayers();   // refresh badges/IDs from the file
    },

    templates: [],
    editingTemplateId: null,

    async fetchTemplates() {
        const res = await fetch('/api/templates');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.templates = await res.json();
    },

    // Templates only fit servers of the same edition ("Java" / "Bedrock")
    templatesFor(edition) {
        const ed = String(edition || '').toLowerCase();
        return this.templates.filter(t => String(t.edition || '').toLowerCase() === ed);
    },

    templateOptionsHtml(edition, firstLabel) {
        const esc = t => this.escapeHtml(String(t));
        return `<option value="">${esc(firstLabel)}</option>` +
            this.templatesFor(edition).map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
    },

    // Fills the Maintenance tab's "Apply Template" dropdown for the open server
    async refreshApplyTemplates() {
        const select = document.getElementById('apply-temp-select');
        if (!select || !this.activeServer) return;
        const serverName = this.activeServer.name;
        select.innerHTML = '<option value="">Loading...</option>';
        try {
            await this.fetchTemplates();
        } catch (e) {
            select.innerHTML = '<option value="">Could not load templates</option>';
            return;
        }
        // The user may have switched servers while the request was in flight
        if (!this.activeServer || this.activeServer.name !== serverName) return;

        const edition = this.activeServer.edition;
        const count = this.templatesFor(edition).length;
        select.innerHTML = count
            ? this.templateOptionsHtml(edition, `Select a ${edition} template...`)
            : this.templateOptionsHtml(edition, `No ${edition} templates yet - save one first`);
    },

    async saveAsTemplate() {
        const name = document.getElementById('save-temp-name').value.trim();
        if (!name) return alert("Enter a template name.");
        // A node's server is saved as one of the master's templates, like every other template here
        const res = this.activeServer.node
            ? await fetch(`/api/cluster/nodes/${encodeURIComponent(this.activeServer.node)}/servers/${encodeURIComponent(this.activeServer.name)}/template`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })
            : await api(`/api/servers/${this.activeServer.name}/template/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        if (!res.ok) return alert(`Could not save template (HTTP ${res.status}).`);
        alert("Template saved!");
        document.getElementById('save-temp-name').value = '';
        await this.refreshApplyTemplates();
    },

    async applyTemplate() {
        const tempId = document.getElementById('apply-temp-select').value;
        if (!tempId) return alert("Select a template.");
        if (!confirm("This will overwrite existing properties and permissions (ports are preserved). Continue?")) return;
        if (this.activeServer.node && !await this.pushTemplate(this.activeServer.node, tempId)) return;

        const res = await api(`/api/servers/${this.activeServer.name}/template/apply`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ template_id: tempId })
        });
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (e) {}
            return alert(`Template not applied: ${detail}`);
        }
        alert("Template applied. Restart the server for changes to take effect.");
        this.loadProperties(this.activeServer.name);
    },

    // The master's templates live on the master: copy one to a node before using it there
    async pushTemplate(node, templateId) {
        const res = await fetch(`/api/cluster/nodes/${encodeURIComponent(node)}/templates`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: templateId })
        });
        if (!res.ok) { alert(`The template couldn't be copied to the node: ${await this.apiError(res)}`); return false; }
        return true;
    },

    async openTemplateManager() {
        await this.fetchTemplates();
        document.getElementById('template-workspace').classList.remove('hidden');
        this.renderTemplateSidebar();
        document.getElementById('template-editor-main').classList.add('hidden');
    },

    renderTemplateSidebar() {
        const aside = document.getElementById('template-list-aside');
        aside.innerHTML = '<h3 class="font-bold text-gray-500 mb-2">Saved Templates</h3>';
        
        this.templates.forEach(t => {
            const isActive = this.editingTemplateId === t.id ? 'bg-gray-200 dark:bg-gray-800' : 'hover:bg-gray-100 dark:hover:bg-gray-800';
            aside.innerHTML += `
                <div onclick="ServerApp.openTemplateEditor('${t.id}')" class="p-3 mb-1 rounded cursor-pointer transition ${isActive}">
                    <div class="font-bold text-gray-900 dark:text-gray-200">${this.escapeHtml(t.name)}</div>
                    <div class="text-xs text-gray-500">${this.escapeHtml(t.edition)} Template</div>
                </div>
            `;
        });
        this.renderNodeTemplates(aside);
    },

    openTemplateEditor(id) {
        this.editingTemplateId = id;
        this.renderTemplateSidebar();
        
        const temp = this.templates.find(t => t.id === id);
        if (!temp) return;
        
        document.getElementById('template-editor-main').classList.remove('hidden');
        document.getElementById('editing-temp-title').innerText = `Edit Template: ${temp.name}`;
        this.renderTemplateCopy(temp);
        
        // Build properties editor
        const editor = document.getElementById('temp-properties-editor');
        editor.innerHTML = '';
        const inputClasses = 'temp-prop-input w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-gray-900 dark:text-white';
        
        for (const [key, value] of Object.entries(temp.properties)) {
            let inputHtml = `<input type="text" data-key="${key}" value="${value}" class="${inputClasses}">`;
            editor.innerHTML += `
                <div class="bg-white dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700 flex justify-between items-center gap-4 shadow-sm">
                    <div class="flex-1"><label class="text-xs font-bold text-gray-700 dark:text-gray-300">${key}</label></div>
                    <div class="w-1/2">${inputHtml}</div>
                </div>`;
        }
        
        // Build permissions editor
        document.getElementById('temp-permissions-editor').value = temp.permissions;
    },

    async saveTemplateEdits() {
        if (!this.editingTemplateId) return;
        
        const inputs = document.querySelectorAll('.temp-prop-input');
        const updatedProps = {};
        inputs.forEach(input => updatedProps[input.getAttribute('data-key')] = input.value);
        const perms = document.getElementById('temp-permissions-editor').value;
        
        await fetch(`/api/templates/${this.editingTemplateId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ properties: updatedProps, permissions: perms })
        });
        alert("Template updated globally.");
        await this.fetchTemplates();
    },

    async deleteTemplate() {
        if (!this.editingTemplateId) return;
        if (!confirm("Delete this template permanently?")) return;
        await fetch(`/api/templates/${this.editingTemplateId}`, { method: 'DELETE' });
        this.editingTemplateId = null;
        this.openTemplateManager();
    },

    async openDeployModal() {
        this.modalWide(false);
        await this.fetchTemplates(); // Ensure templates are loaded for the dropdown
        this.loadRemovedServers();   // fills the "Removed servers" section when it arrives
        const modal = document.getElementById('modal-overlay');
        
        // Only templates for the chosen edition; the list follows the edition dropdown
        const templateOptions = this.templateOptionsHtml('java', 'None (Standard Defaults)');

        // A cluster master can create the server on any node that answers (and is new enough)
        const nodes = this.isMaster() ? this.clusterNodes.filter(n => n.state === 'ok' && (n.cluster_api || 1) >= 2) : [];
        const esc = t => this.escapeHtml(String(t ?? ''));
        const where = this.activeServer && this.activeServer.node && nodes.some(n => n.id === this.activeServer.node)
            ? this.activeServer.node : '';
        this.deployNode = where || null;
        const whereHtml = nodes.length ? `
            <div class="mb-4">
                <label class="block text-sm text-gray-400 mb-1">Where</label>
                <select id="deploy-where" onchange="ServerApp.onDeployWhereChange(this.value)" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white">
                    <option value="">${esc(this.panelName || 'This panel')} (this panel, the master)</option>
                    ${nodes.map(n => `<option value="${esc(n.id)}" ${n.id === where ? 'selected' : ''}>🌐 ${esc(n.name)}</option>`).join('')}
                </select>
                <p class="text-xs text-gray-500 mt-1">Ports are chosen on that server, a template is copied to it first, and a missing Java can be installed there.</p>
            </div>` : '';
        document.getElementById('modal-content').innerHTML = `
            <h3 class="text-xl font-bold mb-4">Deploy or Import Server</h3>
            ${whereHtml}

            <div class="mb-4">
                <label class="block text-sm text-gray-400 mb-1">Engine Edition</label>
                <select id="deploy-edition" onchange="ServerApp.onDeployEditionChange(this.value)" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white mb-2">
                    <option value="java">Java Edition (Auto-Port)</option>
                    <option value="bedrock">Bedrock Edition (Auto-Port)</option>
                </select>
                <p id="deploy-bedrock-note" class="text-xs text-gray-500 mb-2 hidden"></p>

                <div id="deploy-java-options">
                    <div class="grid grid-cols-2 gap-2">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Server Type</label>
                            <select id="deploy-loader" onchange="ServerApp.loadDeployVersions(); ServerApp.refreshAdvanced()" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white mb-2">
                                <option value="vanilla">Vanilla (Mojang)</option>
                                <optgroup label="Plugins">
                                    <option value="paper">Paper (plugins)</option>
                                    <option value="purpur" class="loader-1-20">Purpur (plugins)</option>
                                </optgroup>
                                <optgroup label="Mods">
                                    <option value="fabric">Fabric (mods)</option>
                                    <option value="quilt" class="loader-1-20">Quilt (mods)</option>
                                    <option value="neoforge" class="loader-1-20">NeoForge (mods)</option>
                                    <option value="forge" class="loader-1-20">Forge (mods)</option>
                                </optgroup>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Minecraft Version</label>
                            <select id="deploy-version" onchange="ServerApp.showJavaRequirement(this.value || this.dataset.newest, 'deploy-java-note'); ServerApp.loadAdvancedBuilds()" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white mb-2">
                                <option value="">Loading…</option>
                            </select>
                        </div>
                    </div>
                    <p id="deploy-java-note" class="text-xs text-gray-400 mb-2"></p>
                    <p id="deploy-loader-note" class="text-xs text-gray-500 mb-2 hidden"></p>
                </div>
                
                <label class="block text-sm text-gray-400 mb-1">Base Template (Optional)</label>
                <select id="deploy-template" onchange="ServerApp.advancedTemplateHints()" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white mb-2">
                    ${templateOptions}
                </select>

                ${this.advancedOptionsHtml()}

                <button onclick="ServerApp.executeDeploy()" class="mt-2 w-full bg-green-600 hover:bg-green-700 py-2 rounded text-white font-bold transition">Deploy Fresh Server</button>
            </div>
            ${this.isAdmin() ? `<hr class="border-gray-200 dark:border-gray-700 my-4" />
            <div class="mb-2">
                <label class="block text-sm text-gray-400 mb-1">From a modpack</label>
                <p class="text-xs text-gray-500 mb-2">Pick a modpack from Modrinth, or upload a .mrpack or a CurseForge server pack.</p>
                <button id="deploy-modpack-btn" onclick="ServerApp.openModpackDialog()" class="w-full bg-theme hover-bg-theme py-2 rounded text-white font-bold transition">Choose a modpack…</button>
                <p id="deploy-modpack-note" class="text-xs text-gray-500 mt-1 hidden"></p>
            </div>` : ''}
            <hr class="border-gray-200 dark:border-gray-700 my-4" />
            <div class="mb-6">
                <label class="block text-sm text-gray-400 mb-1">Import Existing (.zip, .tar.gz, .tar)</label>
                <input type="file" id="import-file" accept=".zip,.tar.gz,.tgz,.tar,application/zip,application/gzip,application/x-tar" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white text-sm mb-2" />
                <p class="text-xs text-gray-500 mb-2">A backup wrapped in a single folder is fine; its contents are used.</p>
                <button onclick="ServerApp.executeImport()" class="w-full bg-blue-600 hover:bg-blue-700 py-2 rounded text-white font-bold transition">Upload & Import</button>
            </div>
            ${!this.deployNode && this.currentUser && this.currentUser.role === 'admin' ? `<div class="mb-6">
                <label class="block text-sm text-gray-400 mb-1">From backup storage</label>
                <p class="text-xs text-gray-500 mb-2">Bring a server back from its off-site copies on SSH or S3 storage, for example after its machine died.</p>
                <div id="storage-import"><button onclick="ServerApp.openStorageImport()" class="w-full bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 py-2 rounded text-gray-900 dark:text-white font-bold transition">Choose a backup…</button></div>
            </div>` : ''}
            <div id="removed-servers" class="mb-6">${this.removedServersHtml()}</div>
            <div id="found-servers" class="mb-6">${this.foundServersHtml()}</div>
            <button onclick="document.getElementById('modal-overlay').classList.add('hidden')" class="w-full px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white transition">Cancel</button>
        `;
        modal.classList.remove('hidden');
        this.showBedrockChoice();
        this.loadDeployVersions();
        this.refreshAdvanced();
        if (this.deployNode) this.loadRemovedServers();
        this.loadFoundServers();
    },

    // --- New server's Advanced options (1.17.2): everything optional, checked again by the panel ---
    advancedOptionsHtml() {
        const input = 'w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white';
        const label = 'block text-sm text-gray-400 mb-1';
        const head = 'text-sm font-bold text-gray-700 dark:text-gray-200 mt-3 mb-1';
        const heaps = ['1G', '2G', '3G', '4G', '6G', '8G', '10G', '12G', '16G', '24G', '32G'];
        return `<details id="deploy-advanced" class="mb-2 border border-gray-200 dark:border-gray-700 rounded p-2">
            <summary class="cursor-pointer text-sm font-bold text-theme py-1">Advanced options</summary>
            <p id="adv-node-note" class="text-xs text-yellow-600 dark:text-yellow-400 mt-1 hidden"></p>
            <div id="adv-fields">
            <p class="text-xs text-gray-500 mt-1">All optional. Left empty, each one is decided as without Advanced options.</p>

            <h4 class="${head}">Server</h4>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div><label class="${label}" for="adv-name">Name</label>
                    <div class="flex items-center gap-1"><span class="text-sm text-gray-500">minecraft-</span>
                    <input id="adv-name" type="text" maxlength="30" autocapitalize="none" spellcheck="false" placeholder="automatic" class="${input}"></div></div>
                <div><label class="${label}" for="adv-port">Game port</label>
                    <input id="adv-port" type="number" min="1024" max="65535" inputmode="numeric" placeholder="automatic" class="${input}"></div>
            </div>

            <div id="adv-java-groups">
            <h4 class="${head}">Software</h4>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div id="adv-build-wrap"><label class="${label}" for="adv-build">Build</label>
                    <select id="adv-build" class="${input}"><option value="">Latest stable</option></select></div>
                <div><label class="${label}" for="adv-java">Java</label>
                    <select id="adv-java" class="${input}"><option value="">Automatic</option></select></div>
            </div>
            <label id="adv-snapshot-wrap" class="flex items-center gap-2 text-sm mt-2"><input id="adv-snapshot" type="checkbox" onchange="ServerApp.loadDeployVersions()"> Show snapshots and pre-releases (Vanilla)</label>

            <h4 class="${head}">Memory</h4>
            <div><label class="${label}" for="adv-heap">Heap (Java's memory)</label>
                <select id="adv-heap" class="${input}"><option value="">Automatic (2 GB, or 1 GB on small machines)</option>
                ${heaps.map(h => `<option value="${h}">${h.replace('G', ' GB')}</option>`).join('')}</select>
                <p class="text-xs text-gray-500 mt-1">Refused if it leaves less than 1 GB for the system. Java also needs some memory outside the heap.</p></div>

            <h4 class="${head}">Startup flags</h4>
            <div><select id="adv-flags" class="${input}" aria-label="Startup flags preset">
                    <option value="none">None (Java's defaults)</option>
                    <option value="aikar">Aikar's flags (recommended for Paper)</option></select>
                ${this.isAdmin() ? `<label class="${label} mt-2" for="adv-flags-extra">Extra flags (admins)</label>
                <input id="adv-flags-extra" type="text" autocapitalize="none" spellcheck="false" placeholder="-XX:+UseZGC -Dfile.encoding=UTF-8" class="${input}">
                <p class="text-xs text-gray-500 mt-1">Only -XX: options, -D properties and -Xss. Set memory with Heap.</p>` : ''}</div>
            </div>

            <h4 class="${head}">Hard limits</h4>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div><label class="${label}" for="adv-memory-limit">Hard memory limit</label>
                    <select id="adv-memory-limit" class="${input}"><option value="">Off</option>
                    <option value="auto" class="adv-java-only">Automatic (heap plus room for Java)</option>
                    ${['1G', '2G', '3G', '4G', '6G', '8G', '12G', '16G', '24G', '32G'].map(h => `<option value="${h}">${h.replace('G', ' GB')}</option>`).join('')}</select></div>
                <div><label class="${label}" for="adv-cpu">CPU limit (cores)</label>
                    <input id="adv-cpu" type="number" min="0" step="0.5" inputmode="decimal" placeholder="no limit" class="${input}"></div>
            </div>
            <p class="text-xs text-gray-500 mt-1">So one busy server can't slow the others. At the memory limit the server is stopped, so leave room above the heap.</p>

            <h4 class="${head}">World</h4>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div><label class="${label}" for="adv-seed">Seed</label>
                    <input id="adv-seed" type="text" maxlength="64" placeholder="random" class="${input}"></div>
                <div><label class="${label}" for="adv-level-type">World type</label>
                    <select id="adv-level-type" class="${input}"></select></div>
                <div class="sm:col-span-2"><label class="${label}" for="adv-level-name">World folder name</label>
                    <input id="adv-level-name" type="text" maxlength="32" placeholder="world" class="${input}"></div>
            </div>

            <h4 class="${head}">Settings</h4>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div class="sm:col-span-2"><label class="${label}" for="adv-motd">Server message (MOTD)</label>
                    <input id="adv-motd" type="text" maxlength="120" class="${input}"></div>
                <div><label class="${label}" for="adv-gamemode">Game mode</label>
                    <select id="adv-gamemode" class="${input}"><option value="">Default (survival)</option>
                    <option value="survival">Survival</option><option value="creative">Creative</option>
                    <option value="adventure">Adventure</option><option value="spectator" class="adv-java-only">Spectator</option></select></div>
                <div><label class="${label}" for="adv-difficulty">Difficulty</label>
                    <select id="adv-difficulty" class="${input}"><option value="">Default</option>
                    <option value="peaceful">Peaceful</option><option value="easy">Easy</option>
                    <option value="normal">Normal</option><option value="hard">Hard</option></select></div>
                <div><label class="${label}" for="adv-max-players">Max players</label>
                    <input id="adv-max-players" type="number" min="1" max="1000" inputmode="numeric" class="${input}"></div>
                <div><label class="${label}" for="adv-online">Online mode</label>
                    <select id="adv-online" class="${input}"><option value="">Default (on)</option>
                    <option value="true">On: players sign in with their account</option>
                    <option value="false">Off: anyone can use any name</option></select></div>
                <div class="sm:col-span-2"><label class="${label}" for="adv-whitelist"><span id="adv-whitelist-label">Whitelist</span>: players allowed to join</label>
                    <input id="adv-whitelist" type="text" autocapitalize="none" spellcheck="false" placeholder="Names separated by spaces; empty = anyone" class="${input}"></div>
                <div class="sm:col-span-2 adv-java-only"><label class="${label}" for="adv-ops">Operators</label>
                    <input id="adv-ops" type="text" autocapitalize="none" spellcheck="false" placeholder="Names separated by spaces" class="${input}"></div>
            </div>
            </div>
        </details>`;
    },

    // Shows what fits the chosen edition, type and machine; called whenever one of them changes
    async refreshAdvanced() {
        if (!document.getElementById('deploy-advanced')) return;
        const edition = document.getElementById('deploy-edition').value;
        const java = edition === 'java';
        const loader = java ? document.getElementById('deploy-loader').value : '';
        document.getElementById('adv-java-groups').classList.toggle('hidden', !java);
        document.querySelectorAll('#deploy-advanced .adv-java-only').forEach(e => e.classList.toggle('hidden', !java));
        document.getElementById('adv-whitelist-label').innerText = java ? 'Whitelist' : 'Allowlist';
        if (!java && document.getElementById('adv-memory-limit').value === 'auto') document.getElementById('adv-memory-limit').value = '';
        document.getElementById('adv-snapshot-wrap').classList.toggle('hidden', loader !== 'vanilla');
        if (loader !== 'vanilla') document.getElementById('adv-snapshot').checked = false;
        document.getElementById('adv-build-wrap').classList.toggle('hidden', !this.isJavaAddonKind(loader));
        this.refreshLoaderChoices();
        const types = java ? [['default', 'Default'], ['flat', 'Flat'], ['large_biomes', 'Large biomes'], ['amplified', 'Amplified']]
                           : [['default', 'Default'], ['flat', 'Flat']];
        const lt = document.getElementById('adv-level-type');
        const keep = lt.value;
        lt.innerHTML = types.map(([v, t]) => `<option value="${v}">${t}</option>`).join('');
        if (types.some(([v]) => v === keep)) lt.value = keep;
        // An older node would ignore the options it doesn't know, so they're only offered when it can use them
        const node = this.deployNode && this.clusterNodes.find(n => n.id === this.deployNode);
        const old = node && (node.cluster_api || 1) < 6;
        const note = document.getElementById('adv-node-note');
        note.classList.toggle('hidden', !old);
        note.innerText = old ? `Update RedstonePanel on ${node.name} to use Advanced options there.` : '';
        document.getElementById('adv-fields').classList.toggle('hidden', !!old);
        this.advancedTemplateHints();
        if (java) { this.loadAdvancedBuilds(); this.loadAdvancedJavas(); }
    },

    // The 1.20 server types and modpacks need a node that knows them; and a line on what the
    // chosen type's installer does
    refreshLoaderChoices() {
        const select = document.getElementById('deploy-loader');
        if (!select) return;
        const node = this.deployNode && this.clusterNodes.find(n => n.id === this.deployNode);
        const old = !!(node && (node.cluster_api || 1) < 7);
        select.querySelectorAll('.loader-1-20').forEach(o => { o.disabled = old; });
        if (old && select.selectedOptions[0] && select.selectedOptions[0].disabled) { select.value = 'vanilla'; this.loadDeployVersions(); }
        const note = document.getElementById('deploy-loader-note');
        const loader = select.value;
        const text = old ? `Update RedstonePanel on ${node.name} to create Purpur, Quilt, Forge and NeoForge servers there.`
            : ['forge', 'neoforge'].includes(loader) ? `${this.loaderName(loader)}'s installer runs when the server is created, which takes a few minutes.`
            : loader === 'quilt' ? 'Quilt also loads Fabric mods. Its installer runs when the server is created.'
            : loader === 'purpur' ? 'Purpur is Paper with more settings; Paper plugins work on it.' : '';
        note.innerText = text;
        note.classList.toggle('hidden', !text || document.getElementById('deploy-edition').value !== 'java');
        const btn = document.getElementById('deploy-modpack-btn'), packNote = document.getElementById('deploy-modpack-note');
        if (btn) {
            btn.disabled = old;
            btn.classList.toggle('opacity-50', old);
            packNote.classList.toggle('hidden', !old);
            packNote.innerText = old ? `Update RedstonePanel on ${node.name} to create modpack servers there.` : '';
        }
    },

    // A template's own values show as hints, since the template is applied first and these win
    advancedTemplateHints() {
        const id = document.getElementById('deploy-template')?.value;
        const t = id && (this.templates || []).find(x => String(x.id) === String(id));
        const p = (t && t.properties) || {};
        const hint = (field, value, fallback) => {
            const el = document.getElementById(field);
            if (el) el.placeholder = value !== undefined && value !== '' ? `${value} (from the template)` : fallback;
        };
        hint('adv-seed', p['level-seed'], 'random');
        hint('adv-level-name', p['level-name'], 'world');
        hint('adv-motd', p['motd'] ?? p['server-name'], '');
        hint('adv-max-players', p['max-players'], '');
    },

    async loadAdvancedBuilds() {
        const loader = document.getElementById('deploy-loader')?.value;
        const select = document.getElementById('adv-build');
        if (!select || !this.isJavaAddonKind(loader)) return;
        const version = document.getElementById('deploy-version').value || document.getElementById('deploy-version').dataset.newest;
        const perVersion = !['fabric', 'quilt'].includes(loader);      // their loader versions fit every Minecraft version
        const key = `${loader}|${perVersion ? version : ''}`;
        if (select.dataset.key === key) return;
        select.dataset.key = key;
        select.innerHTML = `<option value="">${loader === 'forge' ? 'Recommended' : 'Latest stable'}</option>`;
        if (!version && perVersion) return;
        try {
            const res = await fetch(`/api/java/builds?loader=${encodeURIComponent(loader)}&version=${encodeURIComponent(version || '')}`);
            if (!res.ok || select.dataset.key !== key) return;
            const esc = t => this.escapeHtml(String(t));
            select.innerHTML += (await res.json()).builds.slice(0, 60)
                .map(b => `<option value="${esc(b.build)}">${esc(['fabric', 'quilt'].includes(loader) ? 'Loader ' + b.label : b.label)}</option>`).join('');
        } catch (e) { /* the latest stable build still works */ }
    },

    async loadAdvancedJavas() {
        const select = document.getElementById('adv-java');
        if (!select) return;
        const node = this.deployNode;
        const res = await api('/api/java/runtimes', { node });
        if (!res.ok || this.deployNode !== node) return;
        const keep = select.value;
        const esc = t => this.escapeHtml(String(t));
        const majors = (await res.json()).installed.map(j => j.major).sort((a, b) => a - b);
        select.innerHTML = '<option value="">Automatic (the version it needs)</option>'
            + majors.map(m => `<option value="${m}">Java ${esc(m)}</option>`).join('');
        if (majors.map(String).includes(keep)) select.value = keep;
    },

    // The advanced object for /api/servers/deploy: only what was filled in
    advancedPayload(edition) {
        if (!document.getElementById('deploy-advanced') || document.getElementById('adv-fields').classList.contains('hidden')) return null;
        const v = id => { const el = document.getElementById(id); return el ? String(el.value || '').trim() : ''; };
        const a = {};
        if (v('adv-name')) a.name = v('adv-name');
        if (v('adv-port')) a.port = +v('adv-port');
        if (edition === 'java') {
            if (v('adv-build') && !document.getElementById('adv-build-wrap').classList.contains('hidden')) a.build = v('adv-build');
            if (document.getElementById('adv-snapshot').checked) a.snapshot = true;
            if (v('adv-java')) a.java = +v('adv-java');
            if (v('adv-heap')) a.heap = v('adv-heap');
            if (v('adv-memory-limit') === 'auto') a.memory_limit = 'auto';
            const extra = v('adv-flags-extra').split(/\s+/).filter(Boolean);
            if (v('adv-flags') !== 'none' || extra.length) a.flags = { preset: v('adv-flags'), extra };
            if (v('adv-ops')) a.ops = v('adv-ops');
        }
        if (v('adv-memory-limit') && v('adv-memory-limit') !== 'auto') a.memory_limit = v('adv-memory-limit');
        if (v('adv-cpu') && +v('adv-cpu') > 0) a.cpu = +v('adv-cpu');
        if (v('adv-seed')) a.seed = v('adv-seed');
        if (v('adv-level-type') && v('adv-level-type') !== 'default') a.level_type = v('adv-level-type');
        if (v('adv-level-name')) a.level_name = v('adv-level-name');
        if (v('adv-motd')) a.motd = v('adv-motd');
        if (v('adv-gamemode')) a.gamemode = v('adv-gamemode');
        if (v('adv-difficulty')) a.difficulty = v('adv-difficulty');
        if (v('adv-max-players')) a.max_players = +v('adv-max-players');
        if (v('adv-online')) a.online_mode = v('adv-online') === 'true';
        if (v('adv-whitelist')) { a.whitelist = true; a.whitelist_players = v('adv-whitelist'); }
        return Object.keys(a).length ? a : null;
    },

    // Mojang only makes Bedrock Dedicated Server for x86-64: an ARM64 machine (a Raspberry Pi)
    // runs Java servers only. node: a node id, or null for this panel. Unknown (an older node) = allowed.
    bedrockSupported(node) {
        if (!node) return !(this.currentUser && this.currentUser.bedrock_supported === false);
        const n = this.clusterNodes.find(x => x.id === node);
        return !(n && n.machine && !['x86_64', 'amd64'].includes(String(n.machine).toLowerCase()));
    },

    showBedrockChoice() {
        const select = document.getElementById('deploy-edition');
        const note = document.getElementById('deploy-bedrock-note');
        if (!select || !note) return;
        const ok = this.bedrockSupported(this.deployNode);
        const opt = select.querySelector('option[value="bedrock"]');
        opt.disabled = !ok;
        opt.textContent = ok ? 'Bedrock Edition (Auto-Port)' : 'Bedrock Edition (needs an x86-64 machine)';
        note.classList.toggle('hidden', ok);
        note.innerText = ok ? '' : 'This machine is ARM64 (for example a Raspberry Pi), and Mojang only makes the Bedrock server '
            + 'for 64-bit Intel and AMD machines. Java servers work, and Bedrock players can join one through Geyser.';
        if (!ok && select.value === 'bedrock') {
            select.value = 'java';
            this.onDeployEditionChange('java');
        }
    },

    // New Server's "Where": null = this panel, else a node id
    deployNode: null,

    onDeployWhereChange(node) {
        this.deployNode = node || null;
        this.showBedrockChoice();
        this.refreshAdvanced();
        const version = document.getElementById('deploy-version');
        if (version) this.showJavaRequirement(version.value || version.dataset.newest, 'deploy-java-note');
        this.loadRemovedServers();
    },

    onDeployEditionChange(edition) {
        document.getElementById('deploy-template').innerHTML = this.templateOptionsHtml(edition, 'None (Standard Defaults)');
        document.getElementById('deploy-java-options').classList.toggle('hidden', edition !== 'java');
        this.refreshAdvanced();
    },

    // --- Java server software: loader, Minecraft version, and the Java it needs ---
    javaVersions: {},

    async fetchJavaVersions(loader) {
        if (this.javaVersions[loader]) return this.javaVersions[loader];
        const res = await fetch(`/api/java/versions?loader=${encodeURIComponent(loader)}`);
        if (!res.ok) throw new Error(await this.apiError(res));
        const versions = (await res.json()).versions;
        this.javaVersions[loader] = versions;
        return versions;
    },

    async loadDeployVersions() {
        const loader = document.getElementById('deploy-loader').value;
        const select = document.getElementById('deploy-version');
        const note = document.getElementById('deploy-java-note');
        select.innerHTML = '<option value="">Loading…</option>';
        note.innerText = '';
        try {
            const versions = await this.fetchJavaVersions(loader);
            if (document.getElementById('deploy-loader').value !== loader) return;   // changed meanwhile
            const esc = t => this.escapeHtml(String(t));
            // Advanced options: Vanilla snapshots and pre-releases of the current cycle, listed first
            let snapshots = [];
            if (loader === 'vanilla' && document.getElementById('adv-snapshot')?.checked) {
                const r = await fetch('/api/java/snapshots');
                if (r.ok) snapshots = (await r.json()).versions;
            }
            select.dataset.newest = versions[0] || '';
            select.innerHTML = `<option value="">Newest release (${esc(versions[0] || '?')})</option>`
                + (snapshots.length ? `<optgroup label="Snapshots and pre-releases">${snapshots.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('')}</optgroup><optgroup label="Releases">` : '')
                + versions.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('')
                + (snapshots.length ? '</optgroup>' : '');
            if (versions[0]) this.showJavaRequirement(versions[0], 'deploy-java-note');
            this.loadAdvancedBuilds();
        } catch (e) {
            select.innerHTML = '<option value="">Newest</option>';
            note.className = 'text-xs text-red-400 mb-2';
            note.innerText = `Couldn't list versions: ${e.message}`;
        }
    },

    // "Needs Java 21 (installed)" or which package to install
    async showJavaRequirement(version, noteId) {
        const note = document.getElementById(noteId);
        if (!note || !version) return;
        note.dataset.version = version;
        const res = await api(`/api/java/requirement?version=${encodeURIComponent(version)}`,
            noteId === 'deploy-java-note' ? { node: this.deployNode } : {});
        if (note.dataset.version !== version) return;
        if (!res.ok) { note.innerText = ''; return; }
        const r = await res.json();
        const base = noteId === 'deploy-java-note' ? 'text-xs mb-2 ' : 'text-xs mt-1 ';
        // This system's package sources may not have that Java (Debian 13 packages only 21 and 25)
        const substitute = r.package_major && r.package_major !== r.java_required
            ? ` This system doesn't package Java ${r.java_required}, so it would get Java ${r.package_major}`
              + (r.java_required <= 11 && r.package_major >= 17 ? ', which some old Minecraft versions and mods don\'t start on.' : ', which usually works.') : '';
        if (r.java_used === null && r.package_major === null) {
            note.className = base + 'text-red-400';
            note.innerText = `Minecraft ${r.minecraft} needs Java ${r.java_required}, and this system's package sources `
                + `have no Java that new (they have: ${(r.packaged || []).join(', ') || 'none'}). Choose another version.`;
        } else if (r.java_used === null) {
            const verb = noteId === 'deploy-java-note' ? 'create the server' : 'update';
            note.className = base + (this.isAdmin() ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-400');
            note.innerText = `Minecraft ${r.minecraft} needs Java ${r.java_required}, which isn't installed. `
                + (this.isAdmin() ? `You'll be asked to install it (${r.package}) when you ${verb}.`
                                  : `Ask an admin to install it, or: sudo apt install ${r.package}`) + substitute;
        } else {
            note.className = base + 'text-gray-400';
            note.innerText = `Minecraft ${r.minecraft} needs Java ${r.java_required}; it will run on Java ${r.java_used}`
                + (r.java_used !== r.java_required ? ` (Java ${r.java_required} isn't installed; a newer one usually works).` : '.');
        }
    },

    // --- Java runtimes (admins): install the Java a server needs, remove one no server uses,
    // and choose the Java a server runs on. apt runs on the panel in one background job. ---
    isAdmin() { return !!(this.currentUser && this.currentUser.role === 'admin'); },
    javaJob: null,

    // node: where to install it (undefined = where the open server lives, null = this panel)
    async installJava(major, why, node) {
        const host = this.nodeName(node);
        if (!confirm(`${why ? why + '\n\n' : ''}Install Java ${major} (openjdk-${major}-jre-headless) ${host ? 'on ' + host + ' ' : ''}now? `
            + `apt downloads it from ${host ? 'that' : 'this'} machine's package sources (roughly 50–100 MB); it takes a minute or two.`)) return false;
        return this.runJavaJob('install', major, node);
    },

    // The node's name for messages, or '' for this panel
    nodeName(node) {
        if (node === undefined) node = this.activeServer ? this.activeServer.node : null;
        const n = node && this.clusterNodes.find(x => x.id === node);
        return n ? n.name : '';
    },

    async removeJava(major) {
        if (!confirm(`Remove Java ${major} (openjdk-${major}-jre-headless)? No server uses it.\n\n`
            + `Anything else on this machine that runs Java ${major} stops working. A server that needs it later `
            + `can install it again.`)) return;
        await this.runJavaJob('remove', major);
    },

    async runJavaJob(action, major, node) {
        const done = action === 'install' ? 'installed' : 'removed';
        this.toggleConsole(true, node);
        const res = await api(`/api/java/runtimes/${action}`, {
            node, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ major })
        });
        if (!res.ok) { alert(`Java ${major} wasn't ${done}: ${await this.apiError(res)}`); return false; }
        this.javaJob = (await res.json());
        this.refreshJavaViews();
        let job = this.javaJob;
        try {
            while (job && job.state === 'running') {
                await new Promise(r => setTimeout(r, 1500));
                const r = await api('/api/java/runtimes/job', { node });
                if (!r.ok) throw new Error(await this.apiError(r));
                job = (await r.json()).job;
            }
        } catch (e) {
            job = { state: 'failed', error: e.message };
        }
        this.javaJob = null;
        await this.loadServers();
        this.refreshJavaViews();
        if (!job || job.state !== 'done') {
            const tail = job && job.lines && job.lines.length ? '\n\n' + job.lines.slice(-6).join('\n') : '';
            alert(`Java ${major} wasn't ${done}: ${job ? job.error : 'the job disappeared'}${tail}`);
            return false;
        }
        if (job.rebound && job.rebound.length) {
            alert(`Java ${major} is installed. ${job.rebound.map(r => r.name).join(', ')} now run${job.rebound.length === 1 ? 's' : ''} on it.`);
        }
        return true;
    },

    refreshJavaViews() {
        if (!document.getElementById('dashboard').classList.contains('hidden')) this.loadDashboard();
        if (this.activeServer && document.getElementById('java-card')) this.loadJavaChoice();
    },

    // A request refused because the server needs a Java that isn't installed (HTTP 409 with
    // {java: {required, package}}): an admin can install it and send the request again.
    async withJava(res, send, node) {
        if (res.status !== 409 || !this.isAdmin()) return res;
        let data = {};
        try { data = await res.clone().json(); } catch (e) { return res; }
        if (!data.java) return res;
        // major: the Java this system packages for it (null when its package sources have none new enough)
        const major = data.java.major === undefined ? data.java.required : data.java.major;
        if (major === null) return res;
        if (!await this.installJava(major, data.detail, node)) return res;
        return send();
    },

    // Maintenance tab: which Java this server runs on, and switching to another installed one
    // -----------------------------------------------------------------------------------------
    // 1.17: performance. The Overview's Performance card (tick health, memory, history charts,
    // suggestions), Maintenance > Memory and limits, and the dashboard's history charts.
    // Charts are inline SVG: one measure per chart (small multiples), never two y-scales.
    // -----------------------------------------------------------------------------------------
    perfRange: 'day',
    perfTimer: null,
    perfData: null,
    charts: {},
    hostHistory: null,
    hostHistoryAt: 0,
    hostRange: 'day',

    startPerfPolling() {
        this.stopPerfPolling();
        this.loadPerf();
        this.perfTimer = setInterval(() => {
            const tab = document.getElementById('tab-overview');
            const ws = document.getElementById('workspace');
            if (!this.activeServer || !tab || tab.classList.contains('hidden-tab') || ws.classList.contains('hidden')) {
                this.stopPerfPolling();
                return;
            }
            this.tickPerfUpdated();
            if (!document.hidden) this.loadPerf();
        }, 10000);
    },

    stopPerfPolling() {
        clearInterval(this.perfTimer);
        this.perfTimer = null;
    },

    setPerfRange(r) {
        this.perfRange = r;
        this.loadPerf();
    },

    async loadPerf() {
        const box = document.getElementById('overview-perf');
        const srv = this.activeServer;
        if (!box || !srv) return;
        let p;
        try {
            const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/perf?range=${this.perfRange}`);
            if (!res.ok) throw new Error(await this.apiError(res));
            p = await res.json();
        } catch (e) {
            if (!this.perfData || this.perfData.server !== srv.name) {
                box.innerHTML = `<p class="text-sm text-red-500">Couldn't load performance: ${this.escapeHtml(e.message)}</p>`;
            }
            this.tickPerfUpdated();
            return;
        }
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        this.perfData = p;
        box.innerHTML = this.perfHtml(p);
    },

    tickPerfUpdated() {
        const el = document.getElementById('perf-updated');
        if (!el) return;
        const ago = Math.max(0, Math.round(Date.now() / 1000 - (+el.dataset.t)));
        el.textContent = ago < 5 ? 'updated just now' : `updated ${this.fmtDuration(ago)} ago`;
        el.classList.toggle('text-yellow-600', ago > 60);
    },

    // TPS as a state with an icon and a word, never colour alone
    tpsState(tps) {
        if (tps === null || tps === undefined) return null;
        if (tps >= 19) return { icon: '●', word: 'Smooth', cls: 'text-green-600 dark:text-green-400' };
        if (tps >= 15) return { icon: '▲', word: 'Some lag', cls: 'text-yellow-600 dark:text-yellow-400' };
        return { icon: '■', word: 'Lagging', cls: 'text-red-600 dark:text-red-400' };
    },

    perfHtml(p) {
        const esc = t => this.escapeHtml(t === null || t === undefined ? '' : String(t));
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const tile = (title, big, lines, extra = '') => `
            <div class="${card} p-4 min-w-0">
                <p class="${note}">${title}</p>
                <p class="text-xl font-bold text-gray-900 dark:text-white mt-1">${big}</p>${extra}
                <p class="${note} mt-1">${lines}</p>
            </div>`;
        const t = p.tick || {};
        let tickTile;
        if (t.available) {
            const st = this.tpsState(t.tps);
            const more = t.source === 'tps'
                ? `5 min ${esc(t.tps_5m)} · 15 min ${esc(t.tps_15m)}${t.mspt !== null && t.mspt !== undefined ? ` · ${esc(t.mspt)} ms per tick` : ''}`
                : `${esc(t.mspt)} ms per tick${t.p95 ? ` · 95 % under ${esc(t.p95)} ms` : ''}${t.state && t.state !== 'normal' ? ` · ${esc(t.state)}` : ''}`;
            tickTile = tile('Tick health (TPS)', `${esc(t.tps)} <span class="text-sm font-bold ${st.cls}">${st.icon} ${st.word}</span>`, more);
        } else {
            tickTile = tile('Tick health (TPS)', '<span class="text-base text-gray-500 dark:text-gray-400">Not available</span>', esc(t.why || ''));
        }
        const m = p.memory || {};
        const cap = m.limit || m.heap_bytes;
        const memLines = [m.heap ? `Max heap ${esc(m.heap)}` : null, m.limit ? `hard limit ${this.fmtBytes(m.limit)}` : null,
            p.cpu_limit ? `CPU limit ${esc(p.cpu_limit)} cores` : null].filter(Boolean).join(' · ') || 'No limits set';
        const memTile = tile('Memory (whole process)', m.used !== null && m.used !== undefined ? this.fmtBytes(m.used) : '—', memLines,
            cap && m.used ? `<div class="mt-2">${this.bar(100 * m.used / cap)}</div>` : '');
        const playersTile = tile('Players', p.players !== null && p.players !== undefined ? `${p.players} / ${p.max_players}` : '—',
            p.connections !== null && p.connections !== undefined ? `${p.connections} connection${p.connections === 1 ? '' : 's'} to port ${esc(p.port)}` : (p.edition === 'Bedrock' ? 'Not counted for Bedrock' : 'Shown while it runs, with RCON on'));
        const worldTile = tile('World', p.world_size !== null && p.world_size !== undefined ? this.fmtBytes(p.world_size) : '<span class="text-base text-gray-500">measuring…</span>',
            p.addons !== null && p.addons !== undefined ? `${p.addons} ${p.addons_folder === 'plugins' ? 'plugin' : 'mod'}${p.addons === 1 ? '' : 's'} installed` : esc(p.edition));
        const held = (p.held_back || []).length ? `<div class="p-3 rounded border border-yellow-300 bg-yellow-50 dark:bg-yellow-900/30 dark:border-yellow-800 text-yellow-800 dark:text-yellow-200 text-sm">▲ <b>Held back by its limits:</b> ${p.held_back.map(esc).join(' ')}</div>` : '';

        // History: small multiples, one measure each
        const h = p.history || { points: [], fields: [] };
        const col = f => h.points.map(r => [r[0], r[1 + h.fields.indexOf(f)]]);
        const w = this.chartWidth('overview-perf', 2, 2);
        const charts = [
            this.chartCard(`perf-cpu`, 'CPU (% of the machine)', col('cpu'), { fmt: v => `${v.toFixed(1)} %`, max: null, range: h.range, step: h.step, w }),
            this.chartCard(`perf-mem`, 'Memory', col('memory'), { fmt: v => this.fmtBytes(v), max: m.limit || null, range: h.range, step: h.step, w }),
            this.chartCard(`perf-players`, 'Players online', col('players'), { fmt: v => `${Math.round(v * 10) / 10}`, max: null, minMax: 5, range: h.range, step: h.step, w }),
        ];
        if (p.edition === 'Java') {
            charts.push(this.chartCard(`perf-tps`, 'TPS', col('tps'), { fmt: v => v.toFixed(1), max: 20, range: h.range, step: h.step, w,
                empty: 'Nothing measured yet. TPS needs Java 1.20.3 or newer (or Paper) and RCON on.' }));
        }
        const rangeBtn = (r, label) => `<button onclick="ServerApp.setPerfRange('${r}')" class="px-3 py-1 text-sm rounded ${this.perfRange === r ? 'subtab-active' : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'}" aria-pressed="${this.perfRange === r}">${label}</button>`;

        // Suggestions
        const where = { properties: ['properties', 'Open Configuration'], memory: ['maintenance', 'Open Memory and limits'], type: ['maintenance', 'Open Server type'],
            addons: ['addons', 'Open Mods'], backups: ['backups', 'Open Backups'], files: ['files', 'Open Files'], chat: ['chat', 'Open Live Chat'] };
        const sug = (p.suggestions || []).map(s => {
            const w = where[s.where];
            const tabBtn = w && document.getElementById(`tab-btn-${w[0]}`);
            const link = tabBtn && !tabBtn.classList.contains('hidden')
                ? ` <button onclick="ServerApp.switchTab('${w[0]}')" class="underline text-theme whitespace-nowrap">${w[0] === 'maintenance' ? w[1] : 'Open ' + esc(tabBtn.textContent.trim())}</button>` : '';
            const warn = s.level === 'warn';
            return `<li class="p-3 rounded border ${warn ? 'border-yellow-300 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-900/30' : 'border-gray-200 dark:border-gray-700'} text-sm text-gray-800 dark:text-gray-200">${warn ? '▲' : '💡'} ${esc(s.text)}${link}</li>`;
        }).join('');

        return `
            <div class="flex flex-wrap items-baseline justify-between gap-2 mb-2">
                <h3 class="text-lg font-bold text-theme">Performance</h3>
                <p class="${note}">${p.address ? `${esc(p.address)} · ` : ''}<span id="perf-updated" data-t="${p.updated}">updated just now</span></p>
            </div>
            <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">${tickTile}${memTile}${playersTile}${worldTile}</div>
            ${held ? `<div class="mt-3">${held}</div>` : ''}
            <div class="flex flex-wrap items-center justify-between gap-2 mt-4 mb-2">
                <h4 class="font-bold text-gray-900 dark:text-white">History</h4>
                <div class="flex gap-1" role="group" aria-label="History range">${rangeBtn('day', 'Last 24 hours')}${rangeBtn('week', 'Last 7 days')}</div>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 md:gap-4">${charts.join('')}</div>
            <p class="${note} mt-2">Measured every minute whether anyone is watching; the last 7 days are kept as 15-minute averages.</p>
            ${sug ? `<h4 class="font-bold text-gray-900 dark:text-white mt-4 mb-2">Suggestions</h4><ul class="space-y-2">${sug}</ul>
                <p class="${note} mt-1">Nothing here is changed for you: each change trades how the world plays for speed.</p>` : ''}`;
    },

    // One chart in a card: title, latest value, the SVG (or a note when nothing was measured)
    chartCard(id, title, pts, opts) {
        const esc = t => this.escapeHtml(String(t));
        const vals = pts.filter(p => p[1] !== null && p[1] !== undefined);
        const last = vals.length ? vals[vals.length - 1][1] : null;
        const body = vals.length ? this.chartSvg(id, title, pts, opts)
            : `<p class="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">${esc(opts.empty || 'Nothing measured yet: the first point is written a minute after the panel starts.')}</p>`;
        return `
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm p-3 min-w-0">
                <div class="flex justify-between gap-2 text-sm"><span class="font-bold text-gray-900 dark:text-white">${esc(title)}</span>
                <span class="text-gray-700 dark:text-gray-300">${last !== null ? esc(opts.fmt(last)) : ''}</span></div>
                ${body}
            </div>`;
    },

    // The drawing width of one chart in a grid of cards (1 column on phones, md and xl columns above)
    chartWidth(containerId, mdCols, xlCols) {
        const el = document.getElementById(containerId);
        const full = el && el.clientWidth ? el.clientWidth : 640;
        const vw = window.innerWidth;
        const cols = vw >= 1280 ? xlCols : vw >= 768 ? mdCols : 1;
        return (full - 16 * (cols - 1)) / cols - 26;
    },

    niceMax(v) {
        if (!(v > 0)) return 1;
        const p = Math.pow(10, Math.floor(Math.log10(v)));
        const n = v / p;
        return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
    },

    // An SVG line chart. series: [[t, v], ...] (opts.series2: a second line of the same unit,
    // with a legend). Lines break where nothing was measured. Hover or touch shows the value.
    chartSvg(id, title, pts, opts) {
        // Drawn at the size it's shown (opts.w), so text and lines stay 1:1 on phones and wide screens
        const W = Math.max(240, Math.round(opts.w || 320)), H = 130, L = 52, R = 8, T = 8, B = 20;
        const span = opts.range === 'week' ? 7 * 86400 : 86400;
        const now = Math.floor(Date.now() / 1000);
        const t0 = now - span;
        const all = pts.concat(opts.series2 || []).filter(p => p[1] !== null && p[1] !== undefined);
        const top = opts.max || this.niceMax(Math.max(opts.minMax || 0, ...all.map(p => p[1])));
        const x = t => L + (W - L - R) * Math.max(0, Math.min(1, (t - t0) / span));
        const y = v => T + (H - T - B) * (1 - Math.max(0, Math.min(1, v / top)));
        const gap = (opts.step || 60) * 2.5;
        const path = series => {
            let d = '', prev = null;
            series.forEach(([t, v]) => {
                if (v === null || v === undefined || t < t0) { prev = null; return; }
                d += `${prev === null || t - prev > gap ? 'M' : 'L'}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
                prev = t;
            });
            return d;
        };
        const d1 = path(pts);
        const fmt = opts.fmt;
        const grid = [0, 0.5, 1].map(f => {
            const yy = y(top * f).toFixed(1);
            return `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text class="axis" x="${L - 4}" y="${+yy + 3}" text-anchor="end">${this.escapeHtml(fmt(top * f))}</text>`;
        }).join('');
        const xl = `<text class="axis" x="${L}" y="${H - 4}">${opts.range === 'week' ? '7 days ago' : '24 h ago'}</text><text class="axis" x="${W - R}" y="${H - 4}" text-anchor="end">now</text>`;
        const vals = all.map(p => p[1]);
        const summary = vals.length ? `${title}: lowest ${fmt(Math.min(...vals))}, highest ${fmt(Math.max(...vals))}, latest ${fmt(vals[vals.length - 1])}` : title;
        this.charts[id] = { pts, s2: opts.series2 || null, x, y, t0, span, L, R, W, fmt, names: opts.names || null };
        const legend = opts.series2 ? `<p class="text-xs text-gray-600 dark:text-gray-300 mt-1"><span class="rp-swatch" style="background:var(--chart-1)"></span> ${this.escapeHtml(opts.names[0])} &nbsp; <span class="rp-swatch" style="background:var(--chart-2)"></span> ${this.escapeHtml(opts.names[1])}</p>` : '';
        return `
            <div class="relative mt-1">
                <svg class="rp-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${this.escapeHtml(summary)}">
                    ${grid}${xl}
                    ${opts.series2 ? '' : `<path class="area" d="${this.areaPath(d1, y(0))}"/>`}
                    <path class="line s1" d="${d1}"/>
                    ${opts.series2 ? `<path class="line s2" d="${path(opts.series2)}"/>` : ''}
                    <g id="${id}-hover" style="display:none"><line class="cross" y1="${T}" y2="${H - B}"/><circle class="dot s1" r="4"/>${opts.series2 ? '<circle class="dot s2" r="4"/>' : ''}</g>
                    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"
                        onpointermove="ServerApp.chartHover(event, '${id}')" onpointerdown="ServerApp.chartHover(event, '${id}')" onpointerleave="ServerApp.chartLeave('${id}')"/>
                </svg>
                <div id="${id}-tip" class="hidden absolute z-10 pointer-events-none px-2 py-1 rounded text-xs bg-gray-900 text-white shadow whitespace-nowrap"></div>
                ${legend}
            </div>`;
    },

    // The area under a line: each drawn segment closed down to the baseline
    areaPath(d, base) {
        return (d.match(/M[^M]*/g) || []).map(seg => {
            const pts = seg.slice(1).split('L');
            const first = pts[0].split(','), last = pts[pts.length - 1].split(',');
            return `${seg}L${last[0]},${base}L${first[0]},${base}Z`;
        }).join('');
    },

    chartHover(ev, id) {
        const c = this.charts[id];
        const svg = ev.currentTarget.ownerSVGElement;
        if (!c || !svg) return;
        const box = svg.getBoundingClientRect();
        const sx = (ev.clientX - box.left) * (c.W / box.width);
        const t = c.t0 + (sx - c.L) / (c.W - c.L - c.R) * c.span;
        const near = series => series.reduce((best, p) => p[1] !== null && p[1] !== undefined && (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) ? p : best, null);
        const p = near(c.pts);
        if (!p) return;
        const g = document.getElementById(`${id}-hover`);
        const tip = document.getElementById(`${id}-tip`);
        const px = c.x(p[0]);
        g.style.display = '';
        const line = g.querySelector('line');
        line.setAttribute('x1', px); line.setAttribute('x2', px);
        const dots = g.querySelectorAll('circle');
        dots[0].setAttribute('cx', px); dots[0].setAttribute('cy', c.y(p[1]));
        let text = `${this.fmtChartTime(p[0], c.span)} · ${c.names ? c.names[0] + ' ' : ''}${c.fmt(p[1])}`;
        if (c.s2) {
            const q = c.s2.find(r => r[0] === p[0]);
            if (q && q[1] !== null) {
                dots[1].setAttribute('cx', px); dots[1].setAttribute('cy', c.y(q[1]));
                text += ` · ${c.names[1]} ${c.fmt(q[1])}`;
            }
        }
        tip.textContent = text;
        tip.classList.remove('hidden');
        const left = px / c.W * box.width;
        tip.style.top = '0px';
        tip.style.left = `${Math.min(Math.max(0, left - tip.offsetWidth / 2), box.width - tip.offsetWidth)}px`;
    },

    chartLeave(id) {
        const g = document.getElementById(`${id}-hover`), tip = document.getElementById(`${id}-tip`);
        if (g) g.style.display = 'none';
        if (tip) tip.classList.add('hidden');
    },

    fmtChartTime(t, span) {
        const d = new Date(t * 1000);
        const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        return span > 86400 ? `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}` : hm;
    },

    // Maintenance > Memory and limits
    async loadResources() {
        const card = document.getElementById('resources-card');
        const body = document.getElementById('resources-body');
        const srv = this.activeServer;
        if (!card || !srv) return;
        card.classList.remove('hidden');
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/resources`);
        if (!res.ok) { body.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
        const r = await res.json();
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        this.resourcesData = r;
        body.innerHTML = this.resourcesHtml(r);
        this.onLimitModeChange();
        const st = document.getElementById('res-status');
        if (this.resourcesSaved && st) {
            st.className = 'text-sm text-green-600 dark:text-green-400';
            st.textContent = this.resourcesSaved;
            card.scrollIntoView({ block: 'nearest' });
        }
        this.resourcesSaved = null;
    },

    resourcesHtml(r) {
        const esc = t => this.escapeHtml(t === null || t === undefined ? '' : String(t));
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const input = 'bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-1 text-sm text-gray-900 dark:text-white';
        const admin = this.isAdmin();
        const limitNow = r.memory_limit === 'auto' ? `Automatic (${this.fmtBytes(r.memory_max)})`
            : r.memory_limit === 'off' ? 'Off' : this.fmtBytes(r.memory_limit);
        const machine = `This machine has ${this.fmtBytes(r.host_memory)} of memory and ${esc(r.cores)} CPU core${r.cores === 1 ? '' : 's'}.`
            + (r.java ? ` Other running Java servers use up to ${this.fmtBytes(r.others_running)}; ${this.fmtBytes(r.room)} is left after ${this.fmtBytes(r.reserve)} for the system.` : '');
        if (!admin) {
            return `<p class="text-sm text-gray-700 dark:text-gray-300">${r.java ? `Memory: <b>${esc(r.xms)}</b> to start, at most <b>${esc(r.xmx)}</b>. ` : ''}
                Hard memory limit: <b>${esc(limitNow)}</b>. CPU limit: <b>${r.cpu_limit ? esc(r.cpu_limit) + ' cores' : 'none'}</b>.</p>
                <p class="${note} mt-2">Only an admin can change these.</p>`;
        }
        const custom = typeof r.memory_limit === 'number';
        return `
            <p class="${note} mb-3">${machine}</p>
            ${r.java ? `
            <p class="text-sm font-bold text-gray-900 dark:text-white">Memory (Java heap)</p>
            <div class="flex flex-wrap items-end gap-3 mt-1">
                <label class="text-sm text-gray-700 dark:text-gray-300">To start <input id="res-xms" class="${input} w-24 block mt-1" value="${esc(r.xms)}" placeholder="1G" autocomplete="off"></label>
                <label class="text-sm text-gray-700 dark:text-gray-300">At most <input id="res-xmx" class="${input} w-24 block mt-1" value="${esc(r.xmx)}" placeholder="2G" autocomplete="off"></label>
            </div>
            <p class="${note} mt-1">Write sizes like 2G or 1536M. A new server on this machine gets ${esc(r.default_heap.xms)} to ${esc(r.default_heap.xmx)}. A running server uses a new size after it restarts.</p>` : ''}
            <p class="text-sm font-bold text-gray-900 dark:text-white mt-4">Hard memory limit</p>
            <div class="flex flex-wrap items-center gap-2 mt-1">
                <select id="res-limit-mode" onchange="ServerApp.onLimitModeChange()" class="${input}">
                    <option value="off" ${r.memory_limit === 'off' ? 'selected' : ''}>Off</option>
                    ${r.java ? `<option value="auto" ${r.memory_limit === 'auto' ? 'selected' : ''}>Automatic (${this.fmtBytes(r.auto_limit)})</option>` : ''}
                    <option value="custom" ${custom ? 'selected' : ''}>Custom</option>
                </select>
                <input id="res-limit" class="${input} w-24" value="${custom ? esc(this.heapText(r.memory_limit)) : ''}" placeholder="3G" autocomplete="off">
            </div>
            <p class="${note} mt-1">${this.onWindows()
                ? "Windows refuses the server more memory than this, so Java runs out of memory and stops instead of starving the others; the Last crash card on the Overview says when that happened."
                : "systemd stops the server if its whole process goes over this, so one runaway server can't starve the others."}${r.java ? ' Automatic is its memory plus half again, for what Java uses outside the heap.' : ''}</p>
            <p class="text-sm font-bold text-gray-900 dark:text-white mt-4">CPU limit</p>
            <label class="flex flex-wrap items-center gap-2 mt-1 text-sm text-gray-700 dark:text-gray-300">
                <input id="res-cpu" type="number" min="0" max="${esc(r.cores)}" step="0.5" class="${input} w-24" value="${r.cpu_limit || 0}"> cores (0 = no limit)
            </label>
            <p class="${note} mt-1">Limits apply at once, without a restart. Minecraft's main loop uses one core, so a limit under 1 makes it lag.</p>
            ${r.java && r.running ? `<label class="flex items-center gap-2 mt-4 text-sm text-gray-700 dark:text-gray-300"><input type="checkbox" id="res-restart"> Restart now if the memory changes</label>` : ''}
            <div class="mt-4 flex items-center gap-3">
                <button onclick="ServerApp.saveResources()" class="bg-theme hover-bg-theme text-white px-4 py-2 rounded font-bold text-sm">Save</button>
                <span id="res-status" class="text-sm"></span>
            </div>`;
    },

    heapText(n) {
        return n % (1024 ** 3) === 0 ? `${n / 1024 ** 3}G` : `${Math.max(1, Math.floor(n / 1024 ** 2))}M`;
    },

    onLimitModeChange() {
        const mode = document.getElementById('res-limit-mode'), box = document.getElementById('res-limit');
        if (mode && box) box.classList.toggle('hidden', mode.value !== 'custom');
    },

    async saveResources() {
        const srv = this.activeServer, r = this.resourcesData;
        if (!srv || !r) return;
        const val = id => { const el = document.getElementById(id); return el ? el.value.trim() : null; };
        const mode = val('res-limit-mode');
        const body = {
            memory: mode === 'custom' ? val('res-limit') : mode,
            cpu: parseFloat(val('res-cpu') || '0') || 0,
            restart: !!(document.getElementById('res-restart') || {}).checked,
        };
        if (r.java) { body.xms = val('res-xms'); body.xmx = val('res-xmx'); }
        const status = document.getElementById('res-status');
        status.className = 'text-sm text-gray-500';
        status.textContent = 'Saving…';
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/resources`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        if (!res.ok) {
            status.className = 'text-sm text-red-500';
            status.textContent = await this.apiError(res);
            return;
        }
        const out = await res.json();
        // Refresh the server list (Max RAM, status) but stay on Maintenance: loadServers reopens
        // the server on its Overview
        await this.loadServers();
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        // switchTab reloads the card; it shows this once it has
        this.resourcesSaved = out.restarted ? 'Saved and restarted.' : out.restart_needed ? 'Saved. The new memory is used after the next restart.' : 'Saved.';
        this.switchTab('maintenance');
    },

    // Dashboard: host history (fetched at most once a minute; the tiles refresh every 10 s)
    async loadHostHistory(force) {
        if (!force && this.hostHistory && Date.now() - this.hostHistoryAt < 55000) return;
        try {
            const r = await fetch(`/api/host/history?range=${this.hostRange}`);
            if (r.ok) { this.hostHistory = await r.json(); this.hostHistoryAt = Date.now(); }
        } catch (e) { /* keep the last answer */ }
    },

    async setHostRange(r) {
        this.hostRange = r;
        await this.loadHostHistory(true);
        if (this.dashData) this.renderDashboard();
    },

    hostHistoryHtml() {
        const h = this.hostHistory;
        if (!h) return '';
        const col = f => h.points.map(r => [r[0], r[1 + h.fields.indexOf(f)]]);
        const btn = (r, label) => `<button onclick="ServerApp.setHostRange('${r}')" class="px-3 py-1 text-sm rounded ${this.hostRange === r ? 'subtab-active' : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'}" aria-pressed="${this.hostRange === r}">${label}</button>`;
        const o = { range: h.range, step: h.step, w: this.chartWidth('dash-body', 2, 4) };
        return `
            <section>
                <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
                    <h3 class="font-bold text-gray-900 dark:text-white">History</h3>
                    <div class="flex gap-1" role="group" aria-label="History range">${btn('day', 'Last 24 hours')}${btn('week', 'Last 7 days')}</div>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
                    ${this.chartCard('host-cpu', 'CPU', col('cpu'), { ...o, fmt: v => `${Math.round(v)} %`, max: 100 })}
                    ${this.chartCard('host-mem', 'Memory used', col('memory'), { ...o, fmt: v => `${Math.round(v)} %`, max: 100 })}
                    ${this.chartCard('host-disk', 'Game servers disk', col('disk'), { ...o, fmt: v => `${Math.round(v)} %`, max: 100 })}
                    ${this.chartCard('host-net', 'Network', col('rx'), { ...o, fmt: v => this.fmtRate(v), series2: col('tx'), names: ['↓ in', '↑ out'] })}
                </div>
            </section>`;
    },

    async loadJavaChoice() {
        const card = document.getElementById('java-card');
        const body = document.getElementById('java-card-body');
        const srv = this.activeServer;
        if (!card || !srv) return;
        card.classList.toggle('hidden', srv.edition !== 'Java' || !this.isAdmin());
        if (srv.edition !== 'Java' || !this.isAdmin()) return;
        const res = await api('/api/java/runtimes');
        if (!res.ok) { body.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
        const rt = await res.json();
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        const esc = t => this.escapeHtml(String(t));
        const need = srv.java_required, using = srv.java_installed;
        const busy = rt.job && rt.job.state === 'running' ? rt.job : this.javaJob;
        const usable = rt.installed.filter(j => !need || j.major >= need);
        const btn = 'bg-theme hover-bg-theme text-white px-3 py-1 rounded font-bold text-sm';
        let html = `<p class="text-sm text-gray-600 dark:text-gray-300">`
            + (using ? `Runs on <b>Java ${esc(using)}</b>` : `<span class="text-red-500 font-bold">Its Java isn't installed</span>`)
            + (srv.java_path ? ` <span class="text-xs text-gray-500 dark:text-gray-400 break-all">(${esc(srv.java_path)})</span>` : '')
            + (need ? `. ${srv.minecraft ? 'Minecraft ' + esc(srv.minecraft) : 'This version'} needs Java ${esc(need)}${using && using > need ? ' or newer' : ''}.` : '.')
            + `</p>`;
        if (busy) {
            html += `<p class="text-sm mt-2">⏳ ${busy.action === 'install' ? 'Installing' : 'Removing'} Java ${esc(busy.major)}…</p>`;
        } else {
            if (need && !rt.installed.some(j => j.major === need)) {
                html += `<p class="text-sm mt-2">Java ${esc(need)} itself isn't installed${using ? `, so it runs on the closest newer one` : ''}.
                    <button onclick="ServerApp.installJava(${need})" class="underline text-theme">Install Java ${esc(need)}</button></p>`;
            }
            if (usable.length > 1 || (usable.length === 1 && usable[0].major !== using)) {
                html += `<div class="mt-3 flex items-center gap-2">
                    <select id="java-choice" class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-1 text-sm text-gray-900 dark:text-white">
                        ${usable.map(j => `<option value="${j.major}" ${j.major === using ? 'selected' : ''}>Java ${esc(j.major)}${j.major === need ? ' (what it needs)' : ''}${j.major === using ? ' (current)' : ''}</option>`).join('')}
                    </select>
                    <button onclick="ServerApp.useJava(+document.getElementById('java-choice').value)" class="${btn}">Use</button>
                </div>
                <p class="text-xs mt-1 text-gray-500 dark:text-gray-400">Only Java ${esc(need || '')} and newer are listed. A running server restarts.</p>`;
            }
        }
        html += this.flagsHtml(srv);
        body.innerHTML = html;
    },

    // Maintenance → Java: the startup flags (1.17.2), the same choice as New server's Advanced options
    flagsHtml(srv) {
        if (!srv || !('jvm_flags' in srv)) return '';      // a node older than 1.17.2
        const esc = t => this.escapeHtml(String(t));
        const f = srv.jvm_flags || { preset: 'none', extra: [] };
        const input = 'w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-sm text-gray-900 dark:text-white';
        return `<div class="mt-4 pt-3 border-t border-gray-200 dark:border-gray-700">
            <p class="text-sm font-bold text-gray-700 dark:text-gray-200">Startup flags</p>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-2">
                <select id="flags-preset" class="${input}" aria-label="Startup flags preset">
                    <option value="none" ${f.preset !== 'aikar' ? 'selected' : ''}>None (Java's defaults)</option>
                    <option value="aikar" ${f.preset === 'aikar' ? 'selected' : ''}>Aikar's flags (recommended for Paper)</option>
                </select>
                <input id="flags-extra" type="text" autocapitalize="none" spellcheck="false" value="${esc((f.extra || []).join(' '))}"
                    placeholder="Extra flags, e.g. -XX:+UseZGC" aria-label="Extra startup flags" class="${input}">
            </div>
            <div class="mt-2 flex items-center gap-2"><button onclick="ServerApp.saveFlags()" class="shrink-0 whitespace-nowrap bg-theme hover-bg-theme text-white px-3 py-1 rounded font-bold text-sm">Save flags</button>
            <span class="text-xs text-gray-500 dark:text-gray-400">Only -XX: options, -D properties and -Xss. A running server restarts.</span></div>
        </div>`;
    },

    async saveFlags() {
        const srv = this.activeServer;
        if (!srv) return;
        const preset = document.getElementById('flags-preset').value;
        const extra = document.getElementById('flags-extra').value.trim().split(/\s+/).filter(Boolean);
        if (srv.status === 'online' && !confirm(`Save ${srv.name}'s startup flags? It restarts now.`)) return;
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/jvm-flags`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preset, extra })
        });
        if (!res.ok) return alert(`Flags not saved: ${await this.apiError(res)}`);
        await this.loadServers();
        this.switchTab('maintenance');
    },

    async useJava(major) {
        const srv = this.activeServer;
        if (!srv) return;
        if (major === srv.java_installed) return;
        if (srv.status === 'online' && !confirm(`Run ${srv.name} on Java ${major}? It restarts now.`)) return;
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/java`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ major })
        });
        if (!res.ok) return alert(`Java not changed: ${await this.apiError(res)}`);
        await this.loadServers();
        this.loadJavaChoice();
    },

    // Maintenance tab: Java servers can pick the Minecraft version to update to (never older)
    async loadUpdateOptions() {
        const srv = this.activeServer;
        const text = document.getElementById('update-text');
        const select = document.getElementById('update-version');
        const note = document.getElementById('update-java-note');
        if (!srv || !text) return;
        note.innerText = '';
        const backupRow = document.getElementById('update-backup-row');
        if (backupRow) backupRow.classList.toggle('hidden', srv.edition !== 'Java');
        if (srv.edition !== 'Java') {
            select.classList.add('hidden');
            text.innerText = 'Download the newest Bedrock Dedicated Server release.';
            return;
        }
        const loader = srv.loader || 'vanilla';
        const kind = this.loaderName(loader);
        text.innerText = srv.minecraft
            ? `Runs ${kind} ${srv.minecraft}. Download the newest build, or move to a newer Minecraft version (worlds can't go back).`
            : `Download the newest ${kind} server.`;
        select.classList.remove('hidden');
        select.innerHTML = '<option value="">Loading…</option>';
        try {
            const versions = await this.fetchJavaVersions(loader);
            if (!this.activeServer || this.activeServer.name !== srv.name) return;
            const key = v => v.split('.').map(Number);
            const newerOrSame = v => {
                if (!srv.minecraft) return true;
                const a = key(v), b = key(srv.minecraft);
                for (let i = 0; i < Math.max(a.length, b.length); i++) {
                    if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
                }
                return true;
            };
            const esc = t => this.escapeHtml(String(t));
            const allowed = versions.filter(newerOrSame);
            select.innerHTML = `<option value="">Newest (${esc(versions[0] || '?')})</option>`
                + allowed.map(v => `<option value="${esc(v)}" ${v === srv.minecraft ? 'selected' : ''}>${esc(v)}${v === srv.minecraft ? ' (current)' : ''}</option>`).join('');
            select.onchange = () => this.showJavaRequirement(select.value || versions[0], 'update-java-note');
            this.showJavaRequirement(select.value || versions[0], 'update-java-note');
        } catch (e) {
            select.innerHTML = '<option value="">Newest</option>';
            note.innerText = `Couldn't list versions: ${e.message}`;
        }
    },

    // Maintenance tab (Java): Server type card, Undo type change and Roll back the last update (1.19)
    async loadConvert() {
        const card = document.getElementById('convert-card');
        const srv = this.activeServer;
        if (!card || !srv) return;
        const roll = document.getElementById('rollback-content');
        if (srv.edition !== 'Java') {
            card.classList.add('hidden');
            if (roll) roll.innerHTML = '';
            return;
        }
        card.classList.remove('hidden');
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/convert`);
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        if (!res.ok) {
            document.getElementById('convert-body').innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        this.renderConvert(await res.json());
    },

    renderConvert(info) {
        const body = document.getElementById('convert-body');
        const roll = document.getElementById('rollback-content');
        if (!body) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const when = t => t ? esc(new Date(t * 1000).toLocaleString()) : '';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const isAdmin = this.isAdmin();
        const job = info.job;
        const running = job && job.state === 'running';
        const icon = { done: '✓', running: '⏳', failed: '✕', waiting: '○', skipped: '–' };
        const tone = { done: 'text-green-600 dark:text-green-500', running: 'text-theme font-bold', failed: 'text-red-500 font-bold', waiting: 'text-gray-500 dark:text-gray-400', skipped: 'text-gray-400' };
        const verb = { convert: 'Changing', undo: 'Undoing the change', rollback: 'Rolling back the update' };
        let jobBox = '';
        if (running) {
            jobBox = `<div class="mt-3 border border-gray-200 dark:border-gray-700 rounded p-3">
                <p class="text-sm font-bold text-gray-900 dark:text-white mb-1">${esc(verb[job.action] || 'Working')}: ${esc(job.from.name || this.typeName(job.from))} → ${esc(this.typeName(job.to))}</p>
                <ul class="text-sm space-y-1">${job.steps.map(s => `<li class="${tone[s.state] || ''}">${icon[s.state] || ''} ${esc(s.label)}</li>`).join('')}</ul>
                <p class="${note} mt-2">Leave this page if you like; it carries on. If the server doesn't start within ${Math.round(info.start_wait / 60)} minutes, everything is put back.</p></div>`;
        } else if (job && job.state === 'failed') {
            const why = job.why || {};
            jobBox = `<div class="mt-3 border border-red-300 dark:border-red-800 bg-red-50 dark:bg-gray-900 rounded p-3">
                <p class="text-sm font-bold text-red-600 dark:text-red-400">${job.action === 'convert' ? `Changing to ${esc(this.typeName(job.to))} didn't work` : 'Putting the old server back didn\'t work'} (${when(job.finished)})</p>
                <p class="text-sm text-gray-700 dark:text-gray-300 mt-1">${esc(job.error)}</p>
                ${(why.causes || []).map(c => `<p class="text-sm text-gray-700 dark:text-gray-300 mt-2"><b>${esc(c.title)}.</b> ${esc(c.text)} ${esc(c.fix)}</p>`).join('')}
                ${(why.lines || []).length ? `<details class="mt-2"><summary class="${note} cursor-pointer">What the server's log said</summary><pre class="text-xs whitespace-pre-wrap break-all text-gray-700 dark:text-gray-300 mt-1">${why.lines.map(esc).join('\n')}</pre></details>` : ''}</div>`;
        } else if (job && job.state === 'done' && (job.notes || []).length) {
            jobBox = `<div class="mt-3 ${note}">${job.notes.map(esc).join('<br>')}</div>`;
        }
        const u = info.undo;
        let undoBox = '';
        if (u && u.kind === 'convert' && !running) {
            undoBox = `<div class="mt-3 border-t border-gray-200 dark:border-gray-700 pt-3">
                <p class="text-sm text-gray-700 dark:text-gray-300">Changed from <b>${esc(u.from.name)}</b> to <b>${esc(u.to.name)}</b> on ${when(u.at)} by ${esc(u.by)}.</p>
                ${u.available
                    ? (isAdmin ? `<button onclick="ServerApp.undoConvert('convert')" class="mt-2 bg-yellow-600 hover:bg-yellow-700 text-white px-4 py-2 rounded font-bold text-sm">Undo type change</button>` : '')
                      + `<p class="${note} mt-2">Undo puts ${esc(u.from.name)} and the world from before the change back ("Before type change" on the Backups tab). What was built since is saved as a "Before restore" backup, not kept in the world.</p>`
                    : `<p class="${note} mt-1">It can't be undone any more: ${esc(u.reason)}.</p>`}</div>`;
        }
        const types = info.types.map(t => `<button onclick="ServerApp.previewConvert(${this.jsArg(t.id)})" class="bg-theme hover-bg-theme text-white px-4 py-2 rounded font-bold text-sm">Change to ${esc(t.name)}…</button>`).join('');
        body.innerHTML = `
            <p class="text-sm text-gray-600 dark:text-gray-300">Runs <b>${esc(info.current.name)}</b>. Change it to another type and keep the world, settings, players and schedules: Paper or Purpur for plugins; Fabric, Quilt, NeoForge or Forge for mods; Vanilla for neither.</p>
            ${running ? '' : isAdmin
                ? `<div class="flex flex-wrap gap-2 mt-3">${types}</div>
                   <p class="${note} mt-2">You see what will happen before anything changes. A backup is always taken first, and the old type comes back by itself if the new one doesn't start.</p>`
                : `<p class="${note} mt-2">Only an admin can change a server's type.</p>`}
            ${jobBox}${undoBox}`;
        if (roll) {
            roll.innerHTML = (u && u.kind === 'update' && !running) ? `
                <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded p-4 shadow-sm">
                    <p class="text-sm text-gray-700 dark:text-gray-300">Last update: <b>${esc(u.from.name)}</b> → <b>${esc(u.to.name)}</b> on ${when(u.at)} by ${esc(u.by)}.</p>
                    ${u.available
                        ? (isAdmin ? `<button onclick="ServerApp.undoConvert('update')" class="mt-2 bg-yellow-600 hover:bg-yellow-700 text-white px-4 py-2 rounded font-bold text-sm">Roll back the last update</button>` : '')
                          + `<p class="${note} mt-2">Puts ${esc(u.from.name)} and the world from before the update back ("Before update" on the Backups tab). A newer world can't be opened by an older version, so what was built since is saved as a "Before restore" backup, not kept in the world.</p>`
                        : `<p class="${note} mt-1">It can't be rolled back: ${esc(u.reason)}.</p>`}
                </div>` : '';
        }
        clearTimeout(this.convertTimer);
        if (running) {
            const name = info.server;
            this.convertTimer = setTimeout(async () => {
                if (!this.activeServer || this.activeServer.name !== name) return;
                const r = await api(`/api/servers/${encodeURIComponent(name)}/convert`);
                if (!r.ok || !this.activeServer || this.activeServer.name !== name) return;
                const next = await r.json();
                if (!next.job || next.job.state !== 'running') {
                    await this.loadServers();
                    this.loadUpdateOptions();
                    this.loadAutoUpdate();
                    this.loadJavaChoice();
                }
                if (document.getElementById('convert-body')) this.renderConvert(next);
            }, 2000);
        }
    },

    typeName(t) {
        const names = ServerApp.LOADER_NAMES;
        return t ? `${names[t.loader] || t.loader || ''} ${t.minecraft || ''}`.trim() : '';
    },

    // The preview of a type change: nothing changes until the button at the bottom is pressed
    async previewConvert(to, minecraft) {
        const srv = this.activeServer;
        if (!srv) return;
        const modal = document.getElementById('modal-overlay');
        const content = document.getElementById('modal-content');
        const esc = t => this.escapeHtml(String(t ?? ''));
        const names = ServerApp.LOADER_NAMES;
        const cancel = `<button onclick="ServerApp.closeModal()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Cancel</button>`;
        content.innerHTML = `<h3 class="text-xl font-bold mb-3 text-gray-900 dark:text-white">Change ${esc(srv.name)} to ${esc(names[to] || to)}</h3>
            <p class="text-sm text-gray-500 dark:text-gray-400">Checking what would change…</p><div class="flex gap-2 mt-4">${cancel}</div>`;
        modal.classList.remove('hidden');
        const q = `to=${encodeURIComponent(to)}` + (minecraft ? `&minecraft=${encodeURIComponent(minecraft)}` : '');
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/convert?${q}`);
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        if (!res.ok) {
            content.innerHTML = `<h3 class="text-xl font-bold mb-3 text-gray-900 dark:text-white">Change ${esc(srv.name)} to ${esc(names[to] || to)}</h3>
                <p class="text-sm text-red-500">${esc(await this.apiError(res))}</p><div class="flex gap-2 mt-4">${cancel}</div>`;
            return;
        }
        const p = await res.json();
        this.convertPlan = p;
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const row = (title, html) => `<div class="border-t border-gray-200 dark:border-gray-700 py-3"><p class="text-sm font-bold text-gray-900 dark:text-white mb-1">${title}</p>${html}</div>`;
        const text = t => `<p class="text-sm text-gray-700 dark:text-gray-300">${t}</p>`;
        const versions = p.versions.includes(p.to.minecraft) ? p.versions : [p.to.minecraft, ...p.versions];
        const versionSelect = `<select id="convert-version" onchange="ServerApp.previewConvert(${this.jsArg(to)}, this.value)" class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-sm text-gray-900 dark:text-white">
            ${versions.map(v => `<option value="${esc(v)}" ${v === p.to.minecraft ? 'selected' : ''}>${esc(v)}${v === p.from.minecraft ? ' (same as now)' : ''}</option>`).join('')}</select>`;
        const java = p.java ? (p.java.installed
            ? `Runs on Java ${esc(p.java.used)}, which is installed.`
            : `Needs Java ${esc(p.java.required)}, which isn't installed yet. You're asked to install it when you press the button.`) : '';
        const software = text(`${esc(p.from.name)} ${esc(p.from.minecraft)} → <b>${esc(p.to.name)} ${esc(p.to.minecraft)}</b>${p.to.build && to !== 'fabric' ? ` (build ${esc(p.to.build)})` : ''}. ${java}`)
            + `<label class="flex flex-wrap items-center gap-2 text-sm text-gray-700 dark:text-gray-300 mt-2">Minecraft version ${versionSelect}</label>
               <p class="${note} mt-1">Only the same version or newer: a world can't go back to an older one.</p>`;
        const moves = p.world.moves.map(m => `<li><code>${esc(m.from)}</code> → <code>${esc(m.to)}</code></li>`).join('');
        const world = text(esc(p.world.note)) + (moves ? `<ul class="text-xs text-gray-600 dark:text-gray-400 mt-1 list-disc ml-5">${moves}</ul>` : '');
        const a = p.addons;
        let addons = '';
        if (a.action === 'off') {
            const eq = a.items.filter(i => i.equivalent && i.enabled);
            const n = a.items.length;
            const cant = to === 'vanilla' ? 'Vanilla loads neither mods nor plugins' : `${esc(p.to.name)} can't load ${esc(p.from.name)} ${esc(a.noun)}s`;
            addons = text(`${cant}, so the <code>${esc(a.folder)}</code> folder (${n} ${esc(a.noun)}${n === 1 ? '' : 's'}) is renamed <code>${esc(a.folder)}.off</code> and kept for switching back.`)
                + (eq.length ? `<p class="text-sm text-gray-700 dark:text-gray-300 mt-2">These also have a ${esc(p.to.name)} build. Install it now:</p>
                    <div class="mt-1 space-y-1">${eq.map(i => `<label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300"><input type="checkbox" name="convert-install" value="${esc(i.equivalent.project)}" checked> ${esc(i.equivalent.title || i.name)}</label>`).join('')}</div>
                    <p class="${note} mt-1">Their settings and saved data aren't carried over: a ${esc(a.noun)} and its ${esc(p.to.name)} build keep them differently.${to === 'fabric' ? ' Fabric API, which most mods need, is installed with them.' : ''}</p>` : '')
                + (to !== 'vanilla' && n > eq.length ? `<p class="${note} mt-1">The others have no ${esc(p.to.name)} build on Modrinth${n - eq.length === n ? '' : ' (or aren\'t from Modrinth)'}.</p>` : '');
        } else if (a.action === 'keep') {
            addons = text(esc(a.note));
            a.note = null;
        } else {
            addons = text(p.from.loader === 'vanilla' ? 'Vanilla has none to carry over.' : `No ${esc(a.noun || 'add-on')}s are installed.`);
        }
        if (a.restore) addons += `<p class="text-sm text-gray-700 dark:text-gray-300 mt-2"><code>${esc(a.restore)}.off</code> from before comes back as <code>${esc(a.restore)}</code>.</p>`;
        if (a.note) addons += `<p class="${note} mt-1">${esc(a.note)}</p>`;
        if (p.suggest && to !== 'vanilla') addons += `<p class="${note} mt-1">Afterwards, the Mods tab offers the ${esc(p.suggest)} bundle for ${esc(p.to.name)}.</p>`;
        const c = p.content;
        const danger = p.confirm_name ? `<div class="border border-red-300 dark:border-red-800 bg-red-50 dark:bg-gray-900 rounded p-3 my-3">
            <p class="text-sm font-bold text-red-600 dark:text-red-400">This world uses things that mods added</p>
            <p class="text-sm text-gray-700 dark:text-gray-300 mt-1">Without these mods, their blocks, items and mobs disappear from the world when ${esc(p.to.name)} opens it${c.dimensions.length ? `, and their dimensions (${c.dimensions.map(esc).join(', ')}) can't be reached` : ''}:</p>
            <ul class="text-sm text-gray-700 dark:text-gray-300 list-disc ml-5 mt-1">${c.mods.map(m => `<li><b>${esc(m.name)}</b> <span class="${note}">adds ${esc(m.adds.join(', '))}</span></li>`).join('')}</ul>
            <p class="text-sm text-gray-700 dark:text-gray-300 mt-2">Undo brings them back with the world from before. To go ahead, type the server's name (<b>${esc(p.server)}</b>):</p>
            <input id="convert-confirm" type="text" autocomplete="off" oninput="ServerApp.checkConvertConfirm()" class="mt-1 w-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded p-2 text-gray-900 dark:text-white"></div>` : '';
        const mb = n => `${Math.max(1, Math.round(n / 1048576)).toLocaleString()} MB`;
        const safety = text(`A <b>Before type change</b> backup is always taken first (about ${mb(p.backup.needed)}${p.backup.free != null ? `; ${mb(p.backup.free)} free` : ''}). ${p.running ? 'The server stops for the change, then starts' : 'The server is started'} on ${esc(p.to.name)} to check it${p.running ? '' : ', and stopped again'}. If it doesn't start, ${esc(p.from.name)} and the world are put back by themselves.`);
        const blockers = p.blockers.map(b => `<p class="text-sm text-red-600 dark:text-red-400 mt-1">✕ ${esc(b)}</p>`).join('');
        const warnings = p.warnings.map(w => `<p class="text-sm text-yellow-700 dark:text-yellow-500 mt-1">⚠ ${esc(w)}</p>`).join('');
        content.innerHTML = `
            <h3 class="text-xl font-bold mb-1 text-gray-900 dark:text-white">Change ${esc(p.server)} to ${esc(p.to.name)}</h3>
            <p class="${note} mb-3">Nothing changes until you press the button. Port, settings, players, whitelist, backups and restart schedules stay as they are.</p>
            ${row('Server software', software)}
            ${row('World', world)}
            ${row(p.from.loader === 'paper' ? 'Plugins' : 'Mods and plugins', addons)}
            ${row('Safety', safety)}
            ${danger}${warnings}${blockers}
            <div class="flex gap-2 mt-4">${cancel}
                <button id="convert-go" onclick="ServerApp.startConvert(this)" ${p.ok ? '' : 'disabled'} class="flex-1 px-4 py-2 rounded font-bold text-white ${p.ok ? 'bg-theme hover-bg-theme' : 'bg-gray-400 cursor-not-allowed'}">Change to ${esc(p.to.name)}</button></div>`;
        this.checkConvertConfirm();
    },

    checkConvertConfirm() {
        const p = this.convertPlan, go = document.getElementById('convert-go'), box = document.getElementById('convert-confirm');
        if (!p || !go || !p.ok || !p.confirm_name) return;
        const ok = box && box.value.trim() === p.server;
        go.disabled = !ok;
        go.className = `flex-1 px-4 py-2 rounded font-bold text-white ${ok ? 'bg-red-600 hover:bg-red-700' : 'bg-gray-400 cursor-not-allowed'}`;
    },

    async startConvert(button) {
        const p = this.convertPlan;
        if (!p || !this.activeServer || this.activeServer.name !== p.server) return;
        const install = [...document.querySelectorAll('input[name="convert-install"]:checked')].map(e => e.value);
        const confirmBox = document.getElementById('convert-confirm');
        button.disabled = true;
        const send = () => api(`/api/servers/${encodeURIComponent(p.server)}/convert`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to: p.to.loader, minecraft: p.to.minecraft, confirm: confirmBox ? confirmBox.value.trim() : null, install })
        });
        const res = await this.withJava(await send(), send);
        if (!res.ok) {
            button.disabled = false;
            return alert(`Not changed: ${await this.apiError(res)}`);
        }
        this.closeModal();
        this.loadConvert();
    },

    async undoConvert(kind) {
        const srv = this.activeServer;
        if (!srv) return;
        const sure = kind === 'update'
            ? `Roll back ${srv.name}'s last update? The server stops, the old version and the world from before the update are put back, and it starts again. What was built since the update is saved as a "Before restore" backup, but isn't in the world any more.`
            : `Undo ${srv.name}'s type change? The server stops, the old type and the world from before the change are put back, and it starts again. What was built since the change is saved as a "Before restore" backup, but isn't in the world any more.`;
        if (!confirm(sure)) return;
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/${kind === 'update' ? 'update/rollback' : 'convert/undo'}`, { method: 'POST' });
        if (!res.ok) return alert(`Not done: ${await this.apiError(res)}`);
        this.loadConvert();
    },

    // "⬆ 1.26.10.4" next to a server's version when the update check found a newer one (1.16.12)
    updateBadge(srv) {
        if (!srv || !srv.update) return '';
        return ` <span class="text-theme font-bold whitespace-nowrap" title="Update available: ${this.escapeHtml(String(srv.update.label))}">⬆ ${this.escapeHtml(String(srv.update.latest))}</span>`;
    },

    // Maintenance tab: the update check for this server and Bedrock's Update automatically
    async loadAutoUpdate() {
        const box = document.getElementById('autoupdate-content');
        if (!box || !this.activeServer) return;
        const name = this.activeServer.name;
        const res = await api(`/api/servers/${encodeURIComponent(name)}/updates`);
        if (!this.activeServer || this.activeServer.name !== name) return;
        if (!res.ok) {
            box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        this.renderAutoUpdate(await res.json());
    },

    renderAutoUpdate(info) {
        const box = document.getElementById('autoupdate-content');
        if (!box) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const when = t => t ? esc(new Date(t * 1000).toLocaleString()) : '';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const found = info.update
            ? `<p class="text-sm font-bold text-theme">⬆ Update available: ${esc(info.update.label)}</p>
               <p class="${note}">This server runs ${esc(info.current || 'an unknown version')}. ${info.edition === 'java' ? 'Choose the version above and press Check & Update.' : 'Press Check & Update above, or let it update itself.'}</p>`
            : info.error
                ? `<p class="text-sm text-yellow-600 dark:text-yellow-500">Couldn't check for updates: ${esc(info.error)}</p>`
                : `<p class="text-sm text-gray-600 dark:text-gray-300">${info.checked ? '✓ Runs the newest version' : 'Not checked yet'}${info.current ? ` (${esc(info.current)})` : ''}.</p>`;
        const last = info.last;
        const lastLine = last ? (last.ok
            ? `<p class="${note}">Last update: ${when(last.at)}, ${esc(last.from || '?')} → ${esc(last.to)} (${esc(last.by || '')}).</p>`
            : `<p class="text-xs text-red-500 mt-1">Updating to ${esc(last.to)} failed ${when(last.at)}: ${esc(last.error)}.${last.rolled_back ? ` It was put back on ${esc(last.from || 'the old version')}.` : ''}</p>`) : '';
        const auto = info.edition === 'bedrock' ? (info.supported ? `
            <label class="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300 mt-3">
                <input type="checkbox" id="autoupdate-enabled" class="mt-1" ${info.auto ? 'checked' : ''} onchange="ServerApp.saveAutoUpdate(this)">
                <span><b>Update automatically</b><span class="block ${note}">Bedrock players' apps update themselves and then can't join an older server. When a new release is out, players are warned in game, ${esc(info.warn_minutes)} minutes later the server stops, is backed up ("Before update"), updated and started. If the new version doesn't start, the old one and its world are put back.</span></span>
            </label>` : `<p class="${note} mt-2">Bedrock servers can't run on this machine's processor, so they can't be updated here.</p>`)
            : `<p class="${note} mt-2">Java servers are never updated automatically: a new version can break mods and plugins. You get an alert here, on the dashboard and by webhook instead.</p>`;
        box.innerHTML = `
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded p-5 max-w-xl shadow-sm">
                ${found}
                ${lastLine}
                ${auto}
                <div class="flex flex-wrap items-center gap-3 mt-3">
                    <button onclick="ServerApp.checkUpdatesNow(this)" class="bg-gray-500 hover:bg-gray-600 text-white px-3 py-2 rounded font-bold text-sm">Check now</button>
                    <span class="${note}">${info.checked ? `Checked ${when(info.checked)}` : ''}${info.next_check ? ` · next check ${when(info.next_check)}` : ''}</span>
                </div>
            </div>`;
    },

    async saveAutoUpdate(input) {
        input.disabled = true;
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/updates`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: input.checked })
        });
        input.disabled = false;
        if (!res.ok) {
            input.checked = !input.checked;
            return alert(`Couldn't change automatic updates: ${await this.apiError(res)}`);
        }
        this.renderAutoUpdate(await res.json());
    },

    async checkUpdatesNow(button) {
        button.disabled = true;
        button.innerText = 'Checking…';
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/updates/check`, { method: 'POST' });
        button.disabled = false;
        button.innerText = 'Check now';
        if (!res.ok) return alert(`Couldn't check for updates: ${await this.apiError(res)}`);
        this.renderAutoUpdate(await res.json());
        this.loadServers();
    },

    // --- Backups tab: schedule, list, back up now, download, restore, delete ---
    backupInfo: null,

    formatSize(bytes) {
        if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
        if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
        return Math.max(1, Math.round(bytes / 1024)) + ' KB';
    },

    async loadBackups() {
        if (!this.activeServer) return;
        const name = this.activeServer.name;
        const res = await api(`/api/servers/${encodeURIComponent(name)}/backups`);
        if (!this.activeServer || this.activeServer.name !== name) return;   // switched server meanwhile
        const box = document.getElementById('backups-content');
        if (!res.ok) {
            box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        this.backupInfo = await res.json();
        this.backupCopies = null;
        this.backupCopiesAll = null;
        this.renderBackups();
        this.loadBackupCopies();
        this.loadOffsite();
    },

    // --- 1.16.8: copies of a server's backups on another cluster member (set on the master) ---
    backupMember() {
        return (this.activeServer && this.activeServer.node) || 'local';
    },

    async loadBackupCopies() {
        const role = this.currentUser && this.currentUser.cluster ? this.currentUser.cluster.role : 'standalone';
        const box = document.getElementById('backup-copies-box');
        if (!box || !this.activeServer) return;
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded p-5 shadow-sm';
        if (role === 'node') {
            box.className = card;
            box.innerHTML = `<p class="text-xs text-gray-500 dark:text-gray-400">A copy of these backups on another cluster member is set on the cluster's master, on this server's Backups tab there.</p>`;
            return;
        }
        if (role !== 'master') return;
        const name = this.activeServer.name;
        const res = await fetch(`/api/cluster/backup-copies/${encodeURIComponent(this.backupMember())}/${encodeURIComponent(name)}`);
        if (!this.activeServer || this.activeServer.name !== name) return;
        box.className = card;
        if (!res.ok) {
            box.innerHTML = `<p class="text-sm text-red-500">Backup copies: ${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        this.backupCopies = await res.json();
        this.renderBackupCopies();
    },

    copyRows(list, actions) {
        const esc = t => this.escapeHtml(String(t ?? ''));
        const kindLabel = { scheduled: 'Scheduled', manual: 'Manual', 'pre-restore': 'Before restore', 'pre-change': 'Before mod changes', 'pre-update': 'Before update', 'pre-convert': 'Before type change' };
        return list.map(c => `
            <tr class="border-t border-gray-200 dark:border-gray-700">
                <td class="rp-card-head py-2 pr-3 whitespace-nowrap">${esc(new Date(c.created * 1000).toLocaleString())}</td>
                <td class="py-2 pr-3" data-label="Type">${esc(kindLabel[c.kind] || c.kind)}</td>
                <td class="py-2 pr-3" data-label="Server">${esc(c.server)}${c.panel_name ? ` <span class="text-xs text-gray-500 dark:text-gray-400">from ${esc(c.panel_name)}</span>` : ''}</td>
                <td class="py-2 pr-3" data-label="Kept on">${esc(c.holder_name)}</td>
                <td class="py-2 pr-3 text-right whitespace-nowrap" data-label="Size">${esc(this.formatSize(c.size))}</td>
                <td class="rp-card-actions py-2 text-right whitespace-nowrap">${actions(c)}</td>
            </tr>`).join('');
    },

    copyPath(c) {
        return [c.holder, c.panel, c.server, c.file].map(encodeURIComponent).join('/');
    },

    renderBackupCopies() {
        const box = document.getElementById('backup-copies-box');
        const v = this.backupCopies;
        if (!box || !v || !v.available) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const isAdmin = this.currentUser && this.currentUser.role === 'admin';
        const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
        const btn = 'px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const options = [`<option value="">Nowhere else (only this machine)</option>`]
            .concat(v.members.map(m => `<option value="${esc(m.id)}" ${m.id === v.to ? 'selected' : ''}>${esc(m.name)}${m.id === 'local' ? ' (the master)' : ''}</option>`)).join('');
        let status = '';
        const st = v.status;
        if (v.to && st) {
            const when = new Date(st.at * 1000).toLocaleString();
            status = st.ok
                ? `<p class="text-xs text-gray-500 dark:text-gray-400">Last checked ${esc(when)}: every backup has its copy on ${esc(v.to_name)}${st.last_copied_at ? `; last copied ${esc(new Date(st.last_copied_at * 1000).toLocaleString())}` : ''}.</p>`
                : `<p class="text-xs text-red-500">Copying to ${esc(v.to_name)} failed (${esc(when)}): ${esc(st.error)}. The master tries again every few minutes.</p>`;
        } else if (v.to) {
            status = `<p class="text-xs text-gray-500 dark:text-gray-400">Copying to ${esc(v.to_name)} starts within a few minutes.</p>`;
        }
        const actions = c => `
            <a href="/api/cluster/backup-copies/file/${this.copyPath(c)}" class="${btn}" title="Download">⬇</a>
            ${isAdmin ? `<button onclick="ServerApp.putCopyInto(${this.jsArg(c.holder)}, ${this.jsArg(c.panel)}, ${this.jsArg(c.server)}, ${this.jsArg(c.file)})" class="px-2 py-1 rounded bg-yellow-600 hover:bg-yellow-700 text-white" title="Copy it into this server's backups, to restore from there">Put back</button>
            <button onclick="ServerApp.deleteCopy(${this.jsArg(c.holder)}, ${this.jsArg(c.panel)}, ${this.jsArg(c.server)}, ${this.jsArg(c.file)})" class="${btn} hover:bg-red-700 hover:text-white" title="Delete this copy">🗑</button>` : ''}`;
        const head = `<thead><tr class="text-left text-xs text-gray-500 dark:text-gray-400"><th class="pb-1">Taken</th><th class="pb-1">Type</th><th class="pb-1">Server</th><th class="pb-1">Kept on</th><th class="pb-1 text-right">Size</th><th></th></tr></thead>`;
        const table = list => `<div class="overflow-x-auto -mx-3 sm:mx-0"><table class="rp-cards w-full text-sm text-gray-700 dark:text-gray-300">${head}<tbody>${this.copyRows(list, actions)}</tbody></table></div>`;
        const missing = (v.unreachable || []).length
            ? `<p class="text-xs text-yellow-600 dark:text-yellow-500 mt-2">Not answering, so their copies aren't listed: ${v.unreachable.map(m => esc(m.name)).join(', ')}.</p>` : '';
        const all = this.backupCopiesAll;
        const others = all ? all.copies.filter(c => !v.copies.some(x => x.holder === c.holder && x.panel === c.panel && x.file === c.file)) : null;
        box.innerHTML = `
            <div class="flex items-center justify-between mb-2">
                <p class="text-sm font-bold text-gray-900 dark:text-white">Copies on another cluster member</p>
                <p class="text-xs text-gray-500 dark:text-gray-400">${v.copies.length} · ${esc(this.formatSize(v.copies.reduce((a, c) => a + c.size, 0)))}</p>
            </div>
            <div class="flex flex-wrap items-center gap-2 mb-2">
                <label class="text-sm text-gray-700 dark:text-gray-300" for="backup-copy-to">Also keep a copy on</label>
                <select id="backup-copy-to" class="${field}" ${isAdmin ? '' : 'disabled'}>${options}</select>
                ${isAdmin ? `<button onclick="ServerApp.saveCopyTarget(this)" class="bg-theme hover-bg-theme text-white px-3 py-2 rounded font-bold text-sm">Save</button>` : ''}
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">A backup on the same disk doesn't survive losing the disk. After each scheduled or manual backup, the master copies it to that member within a few minutes and checks it arrived whole (SHA-256). Scheduled copies are kept to the same number as here; manual ones stay until you delete them. Copies stay there even if this machine is lost.</p>
            ${status}
            ${v.copies.length ? `<div class="mt-3">${table(v.copies)}</div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-2"><b>Put back</b> copies a backup into this server's list above, where <b>Restore</b> brings its world back.</p>`
                : `<p class="text-sm text-gray-500 dark:text-gray-400 mt-2">No copies yet.</p>`}
            ${missing}
            ${isAdmin ? (others === null
                ? `<button onclick="ServerApp.loadAllCopies(this)" class="mt-3 text-sm underline text-theme">Use a copy of another server's backup…</button>`
                : `<div class="mt-4"><p class="text-sm font-bold text-gray-900 dark:text-white mb-1">Copies of other servers' backups</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">For a server whose machine is gone: make a new server of the same edition and version here, then put one of its copies back into it and restore.</p>
                    ${others.length ? table(others) : `<p class="text-sm text-gray-500 dark:text-gray-400">There are no others.</p>`}</div>`) : ''}`;
    },

    async saveCopyTarget(button) {
        const to = document.getElementById('backup-copy-to').value || null;
        button.disabled = true;
        const res = await fetch(`/api/cluster/backup-copies/${encodeURIComponent(this.backupMember())}/${encodeURIComponent(this.activeServer.name)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to })
        });
        button.disabled = false;
        if (!res.ok) return alert(`Couldn't save: ${await this.apiError(res)}`);
        this.backupCopies = await res.json();
        this.renderBackupCopies();
    },

    async loadAllCopies(button) {
        button.disabled = true;
        const res = await fetch('/api/cluster/backup-copies');
        if (!res.ok) { button.disabled = false; return alert(`Couldn't list the copies: ${await this.apiError(res)}`); }
        this.backupCopiesAll = await res.json();
        this.renderBackupCopies();
    },

    async putCopyInto(holder, panel, server, file) {
        const target = this.activeServer.name;
        if (!confirm(`Copy ${file} into ${target}'s backups?\n\nNothing is restored yet: afterwards, use Restore on it in the list of backups.`)) return;
        const res = await fetch(`/api/cluster/backup-copies/file/${this.copyPath({ holder, panel, server, file })}/into`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ member: this.backupMember(), server: target })
        });
        if (!res.ok) return alert(`Couldn't put it back: ${await this.apiError(res)}`);
        const r = await res.json();
        alert(r.existed ? `${target} already has this backup (${r.file}).` : `It's in ${target}'s backups now as ${r.file}. Use Restore on it to bring that world back.`);
        this.loadBackups();
    },

    async deleteCopy(holder, panel, server, file) {
        if (!confirm(`Delete the copy ${file}? This can't be undone.`)) return;
        const res = await fetch(`/api/cluster/backup-copies/file/${this.copyPath({ holder, panel, server, file })}`, { method: 'DELETE' });
        if (!res.ok) return alert(`Delete failed: ${await this.apiError(res)}`);
        this.loadBackupCopies();
    },

    // --- Files tab (admins): browse, edit text, upload, download, rename, delete ---
    filesPath: '',
    filesServer: null,
    filesData: null,
    fileEditor: null,     // {path, modified, dirty}

    filesUrl(suffix, params) {
        const q = new URLSearchParams(params || {}).toString();
        return apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/files${suffix}${q ? '?' + q : ''}`);
    },

    joinPath(...parts) {
        return parts.filter(p => p).join('/');
    },

    async loadFiles(path) {
        const box = document.getElementById('files-content');
        if (!this.activeServer || !box) return;
        this.fileEditor = null;
        const res = await api(this.filesUrl('', { path: path || '' }));
        if (!res.ok) {
            const msg = await this.apiError(res);
            if (path) return this.loadFiles('');           // the folder went away: back to the top
            box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(msg)}</p>`;
            return;
        }
        this.filesData = await res.json();
        this.filesPath = this.filesData.path;
        this.filesServer = this.serverKey(this.activeServer);
        this.renderFiles();
        this.loadSftpInfo();
    },

    // --- 1.18: SFTP (core/sftp.py). The Files tab says how to connect with FileZilla or WinSCP.
    sftpInfo: null,

    async loadSftpInfo() {
        try {
            const res = await fetch('/api/sftp');
            this.sftpInfo = res.ok ? await res.json() : null;
        } catch (e) { this.sftpInfo = null; }
        const box = document.getElementById('files-sftp');
        if (box) box.innerHTML = this.sftpCardHtml();
    },

    sftpAddress() {
        const i = this.sftpInfo, srv = this.activeServer;
        if (!i || !srv || !i.account) return null;
        const host = window.location.hostname.includes(':') ? `[${window.location.hostname}]` : window.location.hostname;
        const user = `${i.account}.${srv.name}`;
        return { host: window.location.hostname, port: i.port, user, url: `sftp://${encodeURIComponent(user)}@${host}:${i.port}/` };
    },

    sftpCardHtml() {
        const i = this.sftpInfo, srv = this.activeServer;
        if (!i || !srv) return '';
        const esc = t => this.escapeHtml(String(t ?? ''));
        const box = 'mb-3 p-3 rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm text-gray-700 dark:text-gray-300';
        const small = 'px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        if (srv.node) {
            return this.isAdmin() ? `<div class="${box}"><b class="text-gray-900 dark:text-white">SFTP</b> for this server comes from ${esc(srv.nodeName)}'s own panel: turn it on there (Panel Settings → SFTP) and sign in with an account of that panel.</div>` : '';
        }
        if (!i.enabled || !i.running) {
            if (!this.isAdmin()) return '';
            return `<div class="${box}"><b class="text-gray-900 dark:text-white">SFTP</b> ${i.enabled ? 'is on but its service isn\'t running.' : 'is off.'} To move files with FileZilla or WinSCP, turn it on under <button onclick="ServerApp.openPanelSettings('sftp')" class="underline text-theme">Panel Settings → SFTP</button>.</div>`;
        }
        const a = this.sftpAddress();
        if (!a) return '';
        const row = (label, value) => `<span class="whitespace-nowrap">${label} <span class="font-mono text-gray-900 dark:text-white select-all">${esc(value)}</span></span>`;
        return `<div class="${box}">
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1">
                <b class="text-gray-900 dark:text-white">SFTP</b>
                ${row('Host', a.host)} ${row('Port', a.port)} ${row('Username', a.user)}
                <span class="flex gap-2 ml-auto">
                    <button onclick="ServerApp.copySftp(this)" class="${small}">Copy</button>
                    <a href="${esc(a.url)}" class="${small}" title="Opens FileZilla, WinSCP or another SFTP program">Open</a>
                </span>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">${i.two_factor
                ? 'Your account has two-factor sign-in, so SFTP needs an SSH key: add one under 🔑 Password → SSH keys.'
                : 'Password: your panel password, or an SSH key from 🔑 Password → SSH keys.'}${i.host_key ? ` Host key: <span class="font-mono break-all">${esc(i.host_key)}</span>` : ''}</p>
        </div>`;
    },

    copySftp(button) {
        const a = this.sftpAddress();
        if (!a) return;
        const done = () => { const t = button.innerText; button.innerText = 'Copied'; setTimeout(() => { button.innerText = t; }, 1500); };
        if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(a.url).then(done).catch(() => prompt('Copy the address:', a.url));
        else prompt('Copy the address:', a.url);
    },

    filesNotice: null,

    renderFiles() {
        const d = this.filesData;
        const notice = this.filesNotice;
        this.filesNotice = null;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const attr = t => esc(t).replace(/'/g, '&#39;');
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const btn = 'px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const small = 'px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';

        const parts = d.path ? d.path.split('/') : [];
        let crumbs = `<button onclick="ServerApp.loadFiles('')" class="text-theme hover:underline">${esc(this.activeServer.name)}</button>`;
        parts.forEach((p, i) => {
            const to = parts.slice(0, i + 1).join('/');
            crumbs += ` <span class="text-gray-400">/</span> <button onclick="ServerApp.loadFiles('${attr(to)}')" class="text-theme hover:underline">${esc(p)}</button>`;
        });

        const rows = d.entries.map(e => {
            const full = this.joinPath(d.path, e.name);
            const icon = { dir: '📁', file: '📄', link: '🔗', other: '❔' }[e.type];
            let name;
            if (e.type === 'dir') name = `<button onclick="ServerApp.loadFiles('${attr(full)}')" class="text-theme hover:underline text-left">${esc(e.name)}</button>`;
            else if (e.editable) name = `<button onclick="ServerApp.openFileEditor('${attr(full)}')" class="hover:underline text-left text-gray-900 dark:text-gray-200">${esc(e.name)}</button>`;
            else name = `<span class="text-gray-900 dark:text-gray-200">${esc(e.name)}</span>`;
            if (e.type === 'link') name += ` <span class="text-xs text-gray-500">→ ${esc(e.target || '?')} (not followed)</span>`;
            const actions = [
                e.editable ? `<button onclick="ServerApp.openFileEditor('${attr(full)}')" class="${small}">Edit</button>` : '',
                e.type === 'file' ? `<a href="${this.filesUrl('/download', { path: full })}" class="${small}">Download</a>` : '',
                e.type !== 'other' ? `<button onclick="ServerApp.renameFile('${attr(full)}')" class="${small}">Rename</button>` : '',
                `<button onclick="ServerApp.deleteFile('${attr(full)}', '${e.type}')" class="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white">Delete</button>`,
            ].join(' ');
            return `<tr class="border-t border-gray-200 dark:border-gray-700">
                <td class="rp-card-icon py-1.5 pl-3 pr-2 w-6">${icon}</td>
                <td class="rp-card-head py-1.5 pr-3 break-all">${name}</td>
                <td class="py-1.5 pr-3 text-right whitespace-nowrap text-gray-500 dark:text-gray-400" ${e.size === null ? '' : 'data-label="Size"'}>${e.size === null ? '' : esc(this.formatSize(e.size))}</td>
                <td class="py-1.5 pr-3 whitespace-nowrap text-gray-500 dark:text-gray-400" data-label="Changed">${esc(new Date(e.modified * 1000).toLocaleString())}</td>
                <td class="rp-card-actions py-1.5 pr-3 text-right whitespace-nowrap">${actions}</td>
            </tr>`;
        }).join('');

        document.getElementById('files-content').innerHTML = `
            <div id="files-sftp">${this.sftpCardHtml()}</div>
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
                <div class="text-sm font-mono break-all">${crumbs}</div>
                <div class="flex flex-wrap gap-2">
                    ${d.path ? `<button onclick="ServerApp.loadFiles('${attr(parts.slice(0, -1).join('/'))}')" class="${btn}">⬆ Up</button>` : ''}
                    <button onclick="ServerApp.newFolder()" class="${btn}">New folder</button>
                    <button onclick="ServerApp.newTextFile()" class="${btn}">New file</button>
                    <button onclick="document.getElementById('files-upload').click()" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white">Upload</button>
                    <input type="file" id="files-upload" multiple class="hidden" onchange="ServerApp.uploadFiles(this.files)">
                </div>
            </div>
            <div id="files-status" class="space-y-2 mb-2"></div>
            ${(notice || []).length ? `<div class="${card} p-3 mb-2 text-sm space-y-1 text-gray-800 dark:text-gray-200">${notice.map(m => `<p>${esc(m)}</p>`).join('')}<p class="text-xs text-gray-500 dark:text-gray-400">Restart the server to load them.</p></div>` : ''}
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">Drop files anywhere on this tab to upload them here.${this.addonKind(this.activeServer) ? ` Mods, plugins and add-ons dropped at the top are checked and put in the right folder.` : ''}</p>
            <div class="${card} overflow-x-auto">
                <table class="rp-cards w-full text-sm">
                    <tbody>${rows || '<tr><td class="p-3 text-gray-500">This folder is empty.</td></tr>'}</tbody>
                </table>
            </div>
            ${d.truncated ? '<p class="text-xs text-yellow-600 mt-2">Only the first 5000 entries are shown.</p>' : ''}
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">Text files up to ${esc(this.formatSize(d.max_text))} can be edited here; anything else, download and upload. Symlinks are listed but never followed. Most changes apply when the server restarts.</p>`;
    },

    async openFileEditor(path, isNew) {
        let data = { path, content: '', modified: null };
        if (!isNew) {
            const res = await api(this.filesUrl('/text', { path }));
            if (!res.ok) return alert(`Can't open ${path}: ${await this.apiError(res)}`);
            data = await res.json();
        }
        // A text box turns Windows line endings (CRLF) into LF; remember them so Save writes them back
        // (Bedrock's server.properties and many plugin files use them).
        const lf = (data.content.match(/\n/g) || []).length, crlf = (data.content.match(/\r\n/g) || []).length;
        this.fileEditor = { path, modified: data.modified, dirty: false, mode: 'raw', crlf: crlf > 0 && crlf * 2 >= lf };
        const esc = t => this.escapeHtml(String(t ?? ''));
        const tab = 'px-3 py-1 text-sm rounded text-gray-600 dark:text-gray-400';
        document.getElementById('files-content').innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
                <div class="text-sm font-mono break-all text-gray-900 dark:text-gray-200">${esc(this.activeServer.name)}/${esc(path)}${isNew ? ' <span class="text-xs text-gray-500">(new)</span>' : ''}</div>
                <div class="flex gap-2">
                    <span id="file-editor-status" class="text-xs text-gray-500 dark:text-gray-400 self-center"></span>
                    <button onclick="ServerApp.saveFileEditor()" class="px-3 py-1.5 rounded text-sm bg-green-600 hover:bg-green-700 text-white">Save</button>
                    <button onclick="ServerApp.closeFileEditor()" class="px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Close</button>
                </div>
            </div>
            <div id="file-editor-modes" class="hidden rp-tabs flex gap-2 mb-3 bg-gray-200 dark:bg-gray-800 p-1 rounded w-max max-w-full border border-gray-300 dark:border-gray-700">
                <button onclick="ServerApp.setFileEditorMode('visual')" id="file-mode-visual" class="${tab}">Visual Editor</button>
                <button onclick="ServerApp.setFileEditorMode('raw')" id="file-mode-raw" class="${tab}">Raw Text</button>
            </div>
            <div id="file-visual" class="hidden">
                <div class="flex items-center gap-3 mb-4">
                    <input type="search" id="file-prop-search" placeholder="Filter settings (e.g. port, rcon)..." autocomplete="off"
                           oninput="ServerApp.filterFileProps()" onkeydown="if (event.key === 'Escape') { this.value = ''; ServerApp.filterFileProps(); }"
                           class="flex-1 min-w-0 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white">
                    <span id="file-prop-count" class="text-sm text-gray-500 dark:text-gray-400 whitespace-nowrap"></span>
                </div>
                <p id="file-prop-empty" class="hidden text-sm text-gray-500 dark:text-gray-400 italic mb-4">No settings match your search.</p>
                <div id="file-props" class="grid grid-cols-1 xl:grid-cols-2 gap-4 pb-4"></div>
                <p class="text-xs text-gray-500 dark:text-gray-400 pb-4">Comments, blank lines and the order of the file are kept. To add or remove a setting, or to edit a comment, use <b>Raw Text</b>.</p>
            </div>
            <textarea id="file-editor" spellcheck="false" class="w-full min-h-[400px] font-mono text-sm bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-3 text-gray-900 dark:text-gray-200 outline-none" style="height: 65vh; tab-size: 4;"></textarea>`;
        const ta = document.getElementById('file-editor');
        ta.value = data.content;
        ta.addEventListener('input', () => this.fileEditorChanged());
        // After typing in Raw Text, offer (or stop offering) the Visual Editor for what the file is now
        ta.addEventListener('change', () => this.offerFileVisual());
        const saveKey = e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); this.saveFileEditor(); }
        };
        ta.addEventListener('keydown', saveKey);
        const visual = document.getElementById('file-visual');
        visual.addEventListener('keydown', saveKey);
        // A change in the form is written straight into the text, so both views always agree
        const fromForm = e => {
            const input = e.target.closest('.file-prop-input');
            if (!input) return;
            const before = ta.value;
            ta.value = PropFile.setValue(before, +input.dataset.line, input.value, this.fileEditor.path);
            if (ta.value !== before) this.fileEditorChanged();
        };
        visual.addEventListener('input', fromForm);
        visual.addEventListener('change', fromForm);
        // Files that are a list of key=value settings open in the view used last (the form at first)
        let last = 'visual';
        try { last = localStorage.getItem('rp_fileEditorMode') || 'visual'; } catch (e) {}
        if (this.offerFileVisual() && last === 'visual') this.setFileEditorMode('visual', true);
        else ta.focus();
    },

    // The text to save, with the file's own line endings.
    fileEditorText() {
        const text = document.getElementById('file-editor').value;
        return this.fileEditor.crlf ? text.replace(/\r?\n/g, '\r\n') : text;
    },

    fileEditorChanged() {
        this.fileEditor.dirty = true;
        document.getElementById('file-editor-status').innerText = 'Unsaved changes';
    },

    // Shows the Visual Editor / Raw Text switch when the text is a list of key=value settings.
    offerFileVisual() {
        const ed = this.fileEditor, modes = document.getElementById('file-editor-modes');
        if (!ed || !modes) return false;
        const ok = PropFile.parse(document.getElementById('file-editor').value, ed.path).ok;
        if (ok) ed.offered = true;     // once shown it stays, and says why if the file stops fitting
        modes.classList.toggle('hidden', !ed.offered);
        this.markFileEditorMode();
        return ok;
    },

    markFileEditorMode() {
        ['visual', 'raw'].forEach(m => {
            const b = document.getElementById(`file-mode-${m}`), on = this.fileEditor.mode === m;
            b.classList.toggle('subtab-active', on);
            b.classList.toggle('text-gray-600', !on);
            b.classList.toggle('dark:text-gray-400', !on);
        });
    },

    setFileEditorMode(mode, quiet) {
        const ed = this.fileEditor, ta = document.getElementById('file-editor');
        if (!ed || !ta) return;
        if (mode === 'visual') {
            const p = PropFile.parse(ta.value, ed.path);
            if (!p.ok) {
                if (!quiet) alert(`The Visual Editor can't show this file as it is now: ${p.line ? `line ${p.line}: ` : ''}${p.reason}. Fix it in Raw Text, or keep editing there.`);
                return;
            }
            let html = '', at = 0;
            const esc = t => this.escapeHtml(String(t ?? ''));
            for (const e of p.entries) {
                while (at < p.sections.length && p.sections[at].line < e.line) {
                    html += `<h4 style="grid-column: 1 / -1" class="file-prop-section text-sm font-bold text-gray-900 dark:text-white mt-2">${esc(p.sections[at++].name)}</h4>`;
                }
                html += this.propRowHtml(e.key, e.value, 'file-prop-input', `data-line="${e.line}"`);
            }
            document.getElementById('file-props').innerHTML = html;
        }
        ed.mode = mode;
        document.getElementById('file-visual').classList.toggle('hidden', mode !== 'visual');
        ta.classList.toggle('hidden', mode === 'visual');
        this.markFileEditorMode();
        if (mode === 'visual') this.filterFileProps(); else ta.focus();
        if (!quiet) { try { localStorage.setItem('rp_fileEditorMode', mode); } catch (e) {} }
    },

    // Live filter, as in Configuration: every word must be in the setting's name or value.
    filterFileProps() {
        const terms = (document.getElementById('file-prop-search').value || '').toLowerCase().split(/\s+/).filter(Boolean);
        const rows = document.querySelectorAll('#file-props .prop-row');
        let shown = 0;
        rows.forEach(row => {
            const input = row.querySelector('.file-prop-input');
            const match = terms.every(t => `${row.dataset.key} ${input ? input.value : ''}`.toLowerCase().includes(t));
            row.style.display = match ? '' : 'none';
            if (match) shown++;
        });
        // A section heading shows only while one of its settings does
        document.querySelectorAll('#file-props .file-prop-section').forEach(h => {
            let n = h.nextElementSibling, any = false;
            for (; n && !n.classList.contains('file-prop-section'); n = n.nextElementSibling) any = any || n.style.display !== 'none';
            h.style.display = any ? '' : 'none';
        });
        document.getElementById('file-prop-count').innerText = terms.length ? `${shown} of ${rows.length}` : `${rows.length} settings`;
        document.getElementById('file-prop-empty').classList.toggle('hidden', !(terms.length && rows.length && shown === 0));
    },

    async saveFileEditor() {
        const ed = this.fileEditor;
        if (!ed) return;
        const res = await api(this.filesUrl('/text'), {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: ed.path, content: this.fileEditorText(), modified: ed.modified })
        });
        if (!res.ok) return alert(`Couldn't save: ${await this.apiError(res)}`);
        const r = await res.json();
        ed.modified = r.modified;
        ed.dirty = false;
        document.getElementById('file-editor-status').innerText = `Saved ${new Date().toLocaleTimeString()}`;
    },

    closeFileEditor() {
        if (this.fileEditor && this.fileEditor.dirty && !confirm('Discard your unsaved changes?')) return;
        this.loadFiles(this.filesPath);
    },

    async newFolder() {
        const name = prompt('New folder name:');
        if (!name) return;
        const res = await api(this.filesUrl('/mkdir'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: this.joinPath(this.filesPath, name) })
        });
        if (!res.ok) alert(`Couldn't create the folder: ${await this.apiError(res)}`);
        this.loadFiles(this.filesPath);
    },

    newTextFile() {
        const name = prompt('New file name (for example config.yml):');
        if (!name) return;
        if (name.includes('/')) return alert('Choose a name without slashes.');
        if (this.filesData.entries.some(e => e.name === name)) return alert(`'${name}' already exists.`);
        this.openFileEditor(this.joinPath(this.filesPath, name), true);
    },

    async renameFile(path) {
        const old = path.split('/').pop();
        const name = prompt(`Rename '${old}' to:`, old);
        if (!name || name === old) return;
        const res = await api(this.filesUrl('/rename'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, name })
        });
        if (!res.ok) alert(`Couldn't rename: ${await this.apiError(res)}`);
        this.loadFiles(this.filesPath);
    },

    async deleteFile(path, type) {
        const what = type === 'dir' ? `the folder '${path}' and everything in it` : `'${path}'`;
        if (!confirm(`Permanently delete ${what}? This can't be undone.`)) return;
        const res = await api(this.filesUrl('', { path }), { method: 'DELETE' });
        if (!res.ok) alert(`Couldn't delete: ${await this.apiError(res)}`);
        this.loadFiles(this.filesPath);
    },

    async uploadFiles(fileList) {
        const status = document.getElementById('files-status');
        const folder = this.filesPath;
        for (const f of Array.from(fileList)) {
            if (this.filesData && f.size > this.filesData.max_upload) {
                alert(`${f.name} is larger than ${this.formatSize(this.filesData.max_upload)}; it can't be uploaded here.`);
                continue;
            }
            const row = this.progressRow(status, f.name);
            const send = overwrite => this.xhrUpload(this.filesUrl('/upload', { path: folder, overwrite }), f, p => row.set(p));
            let r = await send(false);
            if (r.status === 409 && confirm(`'${f.name}' already exists here. Replace it?`)) r = await send(true);
            if (r.ok) row.done('done');
            else {
                row.done(r.status === 409 ? 'skipped' : 'failed', true);
                if (r.status !== 409) alert(`Couldn't upload ${f.name}: ${r.data.detail || 'HTTP ' + r.status}`);
            }
        }
        this.loadFiles(folder);
    },

    // --- Mods, plugins and Bedrock add-ons (admins): drag and drop, and the installed list ---
    addonsData: null,
    addonsChanged: {},          // server -> true after an install/enable/remove this session

    // Java server types (1.20): plugins in plugins/ (Paper, Purpur) or mods in mods/ (the rest)
    LOADER_NAMES: { vanilla: 'Vanilla', paper: 'Paper', purpur: 'Purpur', fabric: 'Fabric', quilt: 'Quilt', forge: 'Forge', neoforge: 'NeoForge' },
    PLUGIN_KINDS: ['paper', 'purpur'],
    MOD_KINDS: ['fabric', 'quilt', 'forge', 'neoforge'],
    loaderName(kind) { return this.LOADER_NAMES[kind] || kind || ''; },
    isPluginKind(kind) { return this.PLUGIN_KINDS.includes(kind); },
    isJavaAddonKind(kind) { return this.PLUGIN_KINDS.includes(kind) || this.MOD_KINDS.includes(kind); },
    addonNoun(kind, plural = true) { return (this.isPluginKind(kind) ? 'plugin' : 'mod') + (plural ? 's' : ''); },

    addonKind(srv) {
        if (!srv) return null;
        if (srv.edition === 'Bedrock') return 'bedrock';
        return this.isJavaAddonKind(srv.loader) ? srv.loader : null;
    },

    addonsUrl(params) {
        const q = new URLSearchParams(params || {}).toString();
        return apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/addons${q ? '?' + q : ''}`);
    },

    // The Mods / Plugins / Add-ons tab: admins and accounts with Mods & plugins, on Fabric, Paper and Bedrock servers
    updateAddonsTab(srv) {
        const btn = document.getElementById('tab-btn-addons');
        if (!btn) return;
        const kind = this.addonKind(srv);
        const show = kind && this.can('mods', srv);
        btn.classList.toggle('hidden', !show);
        btn.innerText = kind === 'bedrock' ? 'Add-ons' : this.isPluginKind(kind) ? 'Plugins' : 'Mods';
    },

    // Uploads one file with a progress callback; resolves {status, ok, data}
    xhrUpload(url, file, onProgress) {
        return new Promise(resolve => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
            xhr.onload = () => {
                if (xhr.status === 401) return window.location.replace('/static/login.html');
                let data = {};
                try { data = JSON.parse(xhr.responseText); } catch (e) {}
                resolve({ status: xhr.status, ok: xhr.status >= 200 && xhr.status < 300, data });
            };
            xhr.onerror = () => resolve({ status: 0, ok: false, data: { detail: 'The upload was interrupted.' } });
            const form = new FormData();
            form.append('file', file, file.name);
            xhr.send(form);
        });
    },

    // One row per file in the progress box (#upload-progress inside the current tab)
    progressRow(box, name) {
        const row = document.createElement('div');
        row.className = 'text-xs text-gray-700 dark:text-gray-300';
        row.innerHTML = `<div class="flex justify-between gap-2"><span class="truncate"></span><span class="pct">0%</span></div>
            <div class="w-full h-1.5 rounded bg-gray-200 dark:bg-gray-700 overflow-hidden"><div class="bar h-1.5 bg-theme" style="width:0%"></div></div>`;
        row.querySelector('.truncate').innerText = name;
        if (box) box.appendChild(row);
        return {
            set: p => { row.querySelector('.bar').style.width = `${Math.round(p * 100)}%`; row.querySelector('.pct').innerText = `${Math.round(p * 100)}%`; },
            done: (text, bad) => { row.querySelector('.pct').innerText = text; row.querySelector('.bar').className = `bar h-1.5 ${bad ? 'bg-red-500' : 'bg-green-500'}`; row.querySelector('.bar').style.width = '100%'; },
        };
    },

    // Installs files through the add-ons endpoint (jars sorted into mods/ or plugins/, packs unpacked)
    async installAddons(fileList, box) {
        const server = this.activeServer.name;
        const results = [];
        for (const f of Array.from(fileList)) {
            const row = this.progressRow(box, f.name);
            const send = overwrite => this.xhrUpload(this.addonsUrl({ overwrite }), f, p => row.set(p));
            let r = await send(false);
            if (r.status === 409 && confirm(r.data.detail || `${f.name} is already installed. Replace it?`)) r = await send(true);
            if (r.ok) {
                this.addonsChanged[server] = true;
                const what = (r.data.installed || []).map(i => i.name || i.file).join(', ');
                row.done(r.data.fits === false ? 'installed, check version' : 'installed');
                results.push(`✅ ${what || f.name}` + (r.data.replaced && r.data.replaced.length ? ` (replaced ${r.data.replaced.join(', ')})` : '')
                    + (r.data.fits === false ? ' — made for a different Minecraft version' : ''));
            } else if (r.status === 409) {
                row.done('skipped', true);
            } else {
                row.done('refused', true);
                results.push(`⛔ ${f.name}: ${r.data.detail || 'HTTP ' + r.status}`);
            }
        }
        return results;
    },

    async loadAddons() {
        const box = document.getElementById('addons-content');
        if (!this.activeServer || !box) return;
        const res = await api(this.addonsUrl());
        if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
        this.addonsData = await res.json();
        if (this.addonsFor !== this.activeServer.name) {
            this.addonsFor = this.activeServer.name;
            this.addonsView = 'installed';
            this.addonUpdates = null;
            this.addonSearch = null;
            this.addonCatalog = null;
            this.addonDetail = null;
        }
        if (this.addonsView === 'browse') return this.renderAddonBrowser();
        if (this.addonsView === 'pack' && this.MOD_KINDS.includes(this.addonsData.kind)) return this.loadPlayerPack();
        if (this.addonsView === 'pack') this.addonsView = 'installed';
        this.renderAddons();
        if (this.isJavaAddonKind(this.addonsData.kind)) this.loadAddonUpdates();
        if (this.addonsData.kind === 'bedrock') this.loadPhantom();
    },

    // --- Server tools (Bedrock): Phantom, which makes a server on another port show up as a LAN game
    phantomUrl(tail) {
        return apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/phantom${tail || ''}`);
    },

    async loadPhantom() {
        const server = this.activeServer && this.activeServer.name;
        if (!server) return;
        const res = await api(this.phantomUrl());
        const data = res.ok ? await res.json() : { error: await this.apiError(res) };
        if (!this.activeServer || this.activeServer.name !== server) return;
        this.phantomData = { ...data, for: server };
        this.renderPhantom();
    },

    renderPhantom() {
        const box = document.getElementById('phantom-card');
        if (box) box.innerHTML = this.phantomCard();
    },

    phantomCard() {
        const p = this.phantomData, esc = t => this.escapeHtml(String(t ?? ''));
        const small = 'px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white disabled:opacity-50';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        if (!p || p.for !== this.activeServer.name) return `<p class="p-3 ${note}">Checking Phantom…</p>`;
        if (p.error) return `<p class="p-3 text-xs text-red-500">${esc(p.error)}</p>`;
        const busy = this.phantomBusy ? 'disabled' : '';
        const state = u => {
            const on = u.active === 'active';
            const color = on ? 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
                : u.active === 'failed' ? 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'
                : 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300';
            return `<span class="text-xs px-1.5 py-0.5 rounded ${color}">${esc(u.active)}</span>`
                + (u.enabled === 'enabled' ? ` <span class="${note}">starts at boot</span>` : '');
        };
        const buttons = (u, removeLabel) => {
            const on = u.active === 'active';
            const b = (action, label) => `<button ${busy} onclick="ServerApp.phantomAction('${action}', ${this.jsArg(u.unit)})" class="${small}">${label}</button>`;
            return [on ? b('stop', 'Stop') : b('start', 'Start'), on ? b('restart', 'Restart') : '',
                `<button ${busy} onclick="ServerApp.phantomAction('remove', ${this.jsArg(u.unit)}, ${u.managed ? 'true' : 'false'})" class="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white disabled:opacity-50">${removeLabel}</button>`].join(' ');
        };
        const problems = u => (u.problems || []).map(t => `<p class="text-xs mt-1 text-yellow-700 dark:text-yellow-300">⚠️ ${esc(t)}</p>`).join('');
        const unitRow = (u, removeLabel, extra) => `
            <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <p class="text-sm text-gray-900 dark:text-white"><span class="font-mono">${esc(u.unit)}</span> ${state(u)}</p>
                    <p class="${note}">Forwards to ${esc(u.target)} · listens on UDP ${(u.listens || []).map(esc).join(' and ')}${u.managed ? '' : ' · made outside the panel'}</p>
                    ${problems(u)}${extra || ''}
                </div>
                <div class="flex gap-2 whitespace-nowrap">${buttons(u, removeLabel)}</div>
            </div>`;
        let body;
        if (p.phantom) {
            const logs = (p.phantom.logs || []).length
                ? `<details class="mt-2"><summary class="${note} cursor-pointer">Recent log</summary><pre class="mt-1 p-2 rounded bg-gray-100 dark:bg-gray-900 text-xs overflow-x-auto max-h-48">${p.phantom.logs.map(esc).join('\n')}</pre></details>` : '';
            const repair = p.phantom.target_port !== p.port || p.phantom.active === 'failed'
                ? `<p class="mt-2"><button ${busy} onclick="ServerApp.phantomAction('install')" class="${small}">Repair</button></p>` : '';
            body = unitRow(p.phantom, 'Uninstall', logs + repair);
        } else if (p.can_install) {
            body = `<div class="flex flex-wrap items-center justify-between gap-3">
                <p class="${note} flex-1">Not installed. Install downloads Phantom ${esc(p.version)} from GitHub (checked against a pinned SHA-256), runs it as the service phantom-${esc(p.server)}, and opens UDP 19132 and a proxy port in ufw.</p>
                <button ${busy} onclick="ServerApp.phantomAction('install')" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white disabled:opacity-50">${this.phantomBusy === 'install' ? 'Installing…' : 'Install'}</button></div>`;
        } else {
            body = `<p class="text-xs text-gray-600 dark:text-gray-300">${esc(p.blocker)}</p>`;
        }
        const others = (p.others || []).map(u => `<div class="pt-3 mt-3 border-t border-gray-200 dark:border-gray-700">${unitRow(u, 'Remove unit')}</div>`).join('');
        return `<div class="p-3">
            <p class="font-bold text-gray-900 dark:text-white">Phantom <span class="text-xs font-normal text-gray-500">LAN proxy · jhead/phantom ${esc(p.version)}</span></p>
            <p class="${note} mb-3">${p.transport === 'nethernet'
                ? 'Phantom makes a RakNet server on a port other than 19132 appear under Friends → LAN Games. This server uses NetherNet, which does that by itself.'
                : `Makes this server (port ${esc(p.port)}) appear under Friends → LAN Games on consoles and phones on your network. Only a RakNet server on port 19132 shows up there on its own.`}</p>
            ${body}${others}
        </div>`;
    },

    async phantomAction(action, unit, managed) {
        const server = this.activeServer.name;
        let url;
        if (action === 'remove') {
            const msg = managed
                ? `Uninstall Phantom for ${server}? Its service is stopped and deleted, and its ports are closed in ufw. The game server is not touched.`
                : `Remove ${unit}? It was made outside the panel. The service is stopped and its unit file deleted; the phantom program it runs stays where it is.`;
            if (!confirm(msg)) return;
            url = `/api/servers/${encodeURIComponent(server)}/phantom-remove`;
        } else url = this.phantomUrl(`/${action}`);
        this.phantomBusy = action;
        this.renderPhantom();
        try {
            const res = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ unit: unit || null }) });
            if (!res.ok) alert(`Phantom: ${await this.apiError(res)}`);
            else if (this.activeServer && this.activeServer.name === server) this.phantomData = { ...(await res.json()), for: server };
        } finally {
            this.phantomBusy = null;
            if (this.activeServer && this.activeServer.name === server) this.renderPhantom();
        }
    },

    renderAddons(messages) {
        messages = messages || this.addonMessages;
        const d = this.addonsData, esc = t => this.escapeHtml(String(t ?? ''));
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const small = 'px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const title = d.kind === 'bedrock' ? 'Add-ons' : this.isPluginKind(d.kind) ? 'Plugins' : 'Mods';
        const accepts = d.accepts.join(', ');
        const where = d.kind === 'bedrock' ? 'behavior_packs/ or resource_packs/, and enabled for the world'
            : `${d.folder}/`;
        const items = d.items.map(i => {
            const warn = [];
            if (i.problem) warn.push(`⛔ ${esc(i.problem)}`);
            if (i.fits === false) warn.push(`⚠️ Made for Minecraft ${esc(i.minecraft)}; this server runs ${esc(d.minecraft)}.`);
            if (i.missing && i.missing.length) warn.push(`⚠️ Needs ${i.missing.map(esc).join(', ')}, which ${i.missing.length === 1 ? "isn't" : "aren't"} installed.`);
            const u = this.addonUpdates && this.addonUpdates.items ? this.addonUpdates.items[i.file] : null;
            const src = u ? `<a href="${esc(this.addonProjectUrl(u))}" target="_blank" rel="noopener noreferrer" class="underline">${u.source === 'hangar' ? 'Hangar' : 'Modrinth'}</a>` : '';
            const sub = [i.version ? `v${esc(i.version)}` : '', i.authors && i.authors.length ? `by ${i.authors.slice(0, 3).map(esc).join(', ')}` : '',
                d.kind === 'bedrock' ? `${esc(i.type)} pack` : esc(i.file), i.size ? esc(this.formatSize(i.size)) : '', src].filter(Boolean).join(' · ');
            const upd = u && u.update && i.enabled
                ? `<button onclick="ServerApp.updateAddons([${esc(JSON.stringify(i.file))}])" class="px-2 py-1 rounded text-xs bg-theme hover-bg-theme text-white">Update to ${esc(u.update.version)}${u.update.channel && u.update.channel !== 'release' ? ` (${esc(u.update.channel)})` : ''}</button>` : '';
            return `<div class="flex flex-wrap items-start justify-between gap-3 p-3 border-t border-gray-200 dark:border-gray-700 first:border-t-0 ${i.enabled ? '' : 'opacity-60'}">
                <div class="min-w-0 flex-1">
                    <p class="font-bold text-gray-900 dark:text-white break-words">${esc(i.name || i.file)}${i.enabled ? '' : ' <span class="text-xs font-normal px-1.5 py-0.5 rounded bg-gray-200 dark:bg-gray-700">disabled</span>'}</p>
                    <p class="${note} break-all">${sub}</p>
                    ${i.description ? `<p class="text-xs text-gray-600 dark:text-gray-300 mt-1">${esc(String(i.description).slice(0, 200))}</p>` : ''}
                    ${warn.map(w => `<p class="text-xs mt-1 text-yellow-700 dark:text-yellow-300">${w}</p>`).join('')}
                </div>
                <div class="flex gap-2 whitespace-nowrap">
                    ${upd}
                    ${i.folder !== null || d.kind !== 'bedrock' ? `<button onclick="ServerApp.toggleAddon(${this.jsArg(i.file)}, ${!i.enabled})" class="${small}">${i.enabled ? 'Disable' : 'Enable'}</button>` : ''}
                    <button onclick="ServerApp.removeAddon(${this.jsArg(i.file)}, ${this.jsArg(i.name || i.file)})" class="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white">Remove</button>
                </div>
            </div>`;
        }).join('');
        const changed = this.addonsChanged[this.activeServer.name];
        document.getElementById('addons-content').innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-2">
                <h3 class="text-lg font-bold text-gray-900 dark:text-white">${title}${d.minecraft ? ` <span class="text-sm font-normal text-gray-500">for ${esc(this.loaderName(d.kind))} ${esc(d.minecraft)}</span>` : ''}</h3>
                <span class="${note}">${d.items.length} installed</span>
            </div>
            ${this.addonViewSwitch()}
            ${this.addonUpdatesBar()}
            <div id="addons-drop" class="${card} border-2 border-dashed p-6 text-center cursor-pointer" onclick="document.getElementById('addons-upload').click()">
                <p class="font-bold text-gray-900 dark:text-white">Drop ${esc(accepts)} files here</p>
                <p class="${note} mt-1">or click to choose them. They go into ${esc(where)}.${d.kind === 'fabric' ? ' Most Fabric mods also need the Fabric API mod.' : d.kind === 'quilt' ? ' Quilt also loads Fabric mods; most need Quilted Fabric API (QFAPI/QSL).' : d.kind === 'neoforge' ? ' NeoForge loads Forge mods only up to Minecraft 1.20.4.' : ''}</p>
                <input type="file" id="addons-upload" multiple accept="${esc(accepts)}" class="hidden" onchange="ServerApp.dropOnAddons(this.files)">
            </div>
            <div id="upload-progress" class="space-y-2"></div>
            ${(messages || []).length ? `<div class="${card} p-3 text-sm space-y-1 text-gray-800 dark:text-gray-200">${messages.map(m => `<p>${esc(m)}</p>`).join('')}</div>` : ''}
            ${changed ? `<div class="p-3 rounded text-sm bg-blue-50 dark:bg-blue-900/30 border border-blue-300 dark:border-blue-800 text-gray-900 dark:text-gray-100">Restart the server to load your changes. <button onclick="ServerApp.sendPower('restart')" class="underline text-theme">Restart now</button></div>` : ''}
            <div class="${card}">${items || `<p class="p-4 text-sm text-gray-500 dark:text-gray-400">No ${esc(title.toLowerCase())} yet.</p>`}</div>
            ${d.kind === 'bedrock' ? `<h3 class="text-lg font-bold text-gray-900 dark:text-white pt-2">Server tools</h3>
            <div id="phantom-card" class="${card}">${this.phantomCard()}</div>` : ''}
            <p class="${note}">${d.kind === 'bedrock'
                ? 'Packs are unpacked into the server folder and added to the active world. Disable takes a pack out of the world; Remove deletes it. Resource packs reach players only if they accept server packs.'
                : `Disable renames the file to .jar.disabled, which the server skips. Files are read, never run, to check they suit this server.`}</p>`;
    },

    // --- Modrinth and Hangar (Fabric and Paper servers)
    addonView(view) {
        this.addonsView = view;
        if (view === 'browse') {
            if (!this.addonSearch) this.searchAddons();
            else this.renderAddonBrowser();
        } else if (view === 'pack') this.loadPlayerPack();
        else this.loadAddons();
    },

    addonViewSwitch() {
        const d = this.addonsData;
        if (!this.isJavaAddonKind(d.kind)) return '';
        const b = (v, label) => `<button onclick="ServerApp.addonView('${v}')" class="px-3 py-1.5 rounded text-sm ${this.addonsView === v
            ? 'bg-theme text-white' : 'bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white hover:bg-gray-300 dark:hover:bg-gray-600'}">${label}</button>`;
        return `<div class="flex flex-wrap gap-2">${b('installed', 'Installed')}${b('browse', `Find ${this.addonNoun(d.kind)}`)}${this.MOD_KINDS.includes(d.kind) ? b('pack', 'Player pack') : ''}</div>`;
    },

    // --- Player pack (1.21.6): the mods players need, worked out from this server's mods folder
    playerPackUrl(tail) {
        return apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/player-pack${tail || ''}`);
    },

    async loadPlayerPack(fresh) {
        const box = document.getElementById('addons-content');
        if (!this.activeServer || !box) return;
        const server = this.activeServer.name;
        if (!this.playerPack || this.playerPack.server !== server) {
            this.playerPack = null;
            this.renderPlayerPack();
        }
        const res = await api(this.playerPackUrl(fresh ? '?fresh=true' : ''));
        if (!this.activeServer || this.activeServer.name !== server || this.addonsView !== 'pack') return;
        if (!res.ok) return this.renderPlayerPack(await this.apiError(res));
        this.playerPack = await res.json();
        this.renderPlayerPack();
    },

    async savePlayerPack(body) {
        const server = this.activeServer.name;
        const res = await api(this.playerPackUrl(), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!this.activeServer || this.activeServer.name !== server) return;
        if (!res.ok) return this.renderPlayerPack(await this.apiError(res));
        this.playerPack = await res.json();
        this.renderPlayerPack();
    },

    addPlayerPackExtra() {
        const box = document.getElementById('pack-extra');
        const name = box ? box.value.trim() : '';
        if (name) this.savePlayerPack({ add_extra: name });
    },

    renderPlayerPack(error) {
        const box = document.getElementById('addons-content');
        if (!box) return;
        const d = this.addonsData, p = this.playerPack, esc = t => this.escapeHtml(String(t ?? ''));
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const small = 'px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const field = 'px-2 py-1 rounded text-sm border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white';
        const head = `<div class="flex flex-wrap items-center justify-between gap-2">
                <h3 class="text-lg font-bold text-gray-900 dark:text-white">Player pack${d.minecraft ? ` <span class="text-sm font-normal text-gray-500">for ${esc(this.loaderName(d.kind))} ${esc(d.minecraft)}</span>` : ''}</h3>
                ${p ? `<span class="${note}">pack ${esc(p.version)}</span>` : ''}
            </div>
            ${this.addonViewSwitch()}
            ${error ? `<div class="p-3 rounded text-sm bg-red-50 dark:bg-red-900/30 border border-red-300 dark:border-red-800 text-gray-900 dark:text-gray-100">${esc(error)}</div>` : ''}`;
        if (!p) {
            box.innerHTML = head + (error ? '' : `<p class="${note}">Working out which mods players need…</p>`);
            return;
        }
        const c = p.counts, given = c.in + c.optional + c.bundled;
        const badge = { in: ['In the pack', 'bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200'],
            optional: ['Optional', 'bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-200'],
            bundled: ['File in the pack', 'bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200'],
            out: ['Left out', 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300'] };
        const choices = [['auto', 'Automatic'], ['in', 'Include'], ['optional', 'Optional'], ['out', 'Leave out']];
        const row = (m, extra) => {
            const b = badge[m.state] || badge.out;
            const name = m.slug && (m.on_modrinth || extra) ? `<a href="https://modrinth.com/mod/${encodeURIComponent(m.slug)}" target="_blank" rel="noopener noreferrer" class="hover:underline">${esc(m.name)}</a>` : esc(m.name);
            const pick = extra
                ? `<button onclick="ServerApp.savePlayerPack({ remove_extra: ${this.jsArg(m.key)} })" class="${small}">Remove</button>`
                : `<select aria-label="What players get of ${esc(m.name)}" onchange="ServerApp.savePlayerPack({ mods: { [${this.jsArg(m.file)}]: this.value } })" class="${field}">${choices.map(([v, label]) => `<option value="${v}"${m.choice === v ? ' selected' : ''}>${label}</option>`).join('')}</select>`;
            return `<div class="flex flex-wrap items-start justify-between gap-3 p-3 border-t border-gray-200 dark:border-gray-700 first:border-t-0">
                <div class="min-w-0 flex-1">
                    <p class="font-bold text-gray-900 dark:text-white break-words">${name} <span class="text-xs font-normal px-1.5 py-0.5 rounded ${b[1]}">${b[0]}</span></p>
                    <p class="${note} break-all">${[m.version ? `v${esc(m.version)}` : '', extra ? '' : esc(m.file), esc(m.why)].filter(Boolean).join(' · ')}</p>
                </div>
                <div class="whitespace-nowrap">${pick}</div>
            </div>`;
        };
        const jp = p.join_page || {};
        const onNode = !!this.activeServer.node;
        const where = !p.active ? ''
            : onNode ? 'Players download it from this server\'s join page (Join pages in the sidebar of the cluster master).'
            : jp.enabled && jp.site ? `Players download it from <a class="underline text-theme" target="_blank" rel="noopener noreferrer" href="${esc(`${jp.url || `${jp.scheme}://${location.hostname}:${jp.port}`}/join/${encodeURIComponent(jp.path)}`)}">this server's join page</a>.`
            : 'Players download it from this server\'s join page, which is off. Turn it on under <button class="underline text-theme" onclick="ServerApp.openJoin()">Join pages</button>.';
        const state = p.active
            ? `The player pack is <b>on</b>${p.mode === 'auto' ? ' (automatic)' : ''}: ${given} mod${given === 1 ? '' : 's'}${c.optional ? `, ${c.optional} optional` : ''}; ${c.out} left out.`
            : p.mode === 'off' ? 'The player pack is <b>off</b>.'
            : 'The player pack is <b>off</b> (automatic): this server was made from a Modrinth modpack, so its join page links to that pack. Turn the player pack on if you have added or removed mods since.';
        const modes = [['auto', 'Automatic'], ['on', 'On'], ['off', 'Off']];
        box.innerHTML = head + `
            <div class="${card} p-4 space-y-3">
                <p class="text-sm text-gray-800 dark:text-gray-200">${state} ${where}</p>
                <div class="flex flex-wrap items-end gap-3">
                    <label class="text-xs text-gray-600 dark:text-gray-300">Player pack<br>
                        <select onchange="ServerApp.savePlayerPack({ mode: this.value })" class="${field} mt-1">${modes.map(([v, label]) => `<option value="${v}"${p.mode === v ? ' selected' : ''}>${label}</option>`).join('')}</select></label>
                    <label class="text-xs text-gray-600 dark:text-gray-300">Name players see<br>
                        <input id="pack-name" maxlength="60" value="${esc(p.name)}" placeholder="The server's name" class="${field} mt-1 w-56"></label>
                    <button onclick="ServerApp.savePlayerPack({ name: document.getElementById('pack-name').value })" class="${small}">Save name</button>
                    <span class="flex-1"></span>
                    ${p.active && given ? `<a href="${esc(this.playerPackUrl('/file'))}" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white">Download .mrpack</a>` : ''}
                    <button onclick="ServerApp.loadPlayerPack(true)" class="${small}">Check again</button>
                </div>
                ${(p.problems || []).map(x => `<p class="text-xs text-yellow-700 dark:text-yellow-300">⚠️ ${esc(x)}</p>`).join('')}
                ${p.loader_version ? '' : `<p class="text-xs text-yellow-700 dark:text-yellow-300">⚠️ This server's record doesn't say which ${esc(p.loader_name)} version it runs, so the pack leaves the loader to the player's launcher.</p>`}
            </div>
            <div class="${card}">${p.mods.map(m => row(m, false)).join('') || `<p class="p-4 text-sm text-gray-500 dark:text-gray-400">No mods are installed yet.</p>`}</div>
            <h3 class="text-base font-bold text-gray-900 dark:text-white pt-2">Extras for players</h3>
            <p class="${note}">Mods the server doesn't run but players may like, such as shaders or a minimap. They go in the pack as optional.</p>
            <div class="${card}">${p.extras.map(m => row(m, true)).join('')}
                <div class="flex flex-wrap gap-2 p-3 border-t border-gray-200 dark:border-gray-700 first:border-t-0">
                    <input id="pack-extra" placeholder="A Modrinth mod, such as sodium, or its page address" class="${field} flex-1 min-w-0" onkeydown="if (event.key === 'Enter') ServerApp.addPlayerPackExtra()">
                    <button onclick="ServerApp.addPlayerPackExtra()" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white">Add</button>
                </div>
            </div>
            <p class="${note}">The panel asks Modrinth which mod each file is and which side of the game it runs on. Mods that run only on the server are left out; a file Modrinth doesn't know is left out unless you choose Include, which copies the file itself into the pack. The pack is rebuilt whenever the mods folder changes.</p>`;
    },

    addonProjectUrl(u) {
        if (u.source === 'hangar') return `https://hangar.papermc.io/search?query=${encodeURIComponent(u.slug || u.project)}`;
        const kind = this.addonsData ? this.addonNoun(this.addonsData.kind, false) : 'mod';
        return `https://modrinth.com/${kind}/${encodeURIComponent(u.slug || u.project)}`;
    },

    addonUpdatesBar() {
        const u = this.addonUpdates, esc = t => this.escapeHtml(String(t ?? ''));
        const box = 'p-3 rounded text-sm border flex flex-wrap items-center justify-between gap-2';
        const paper = this.isPluginKind(this.addonsData.kind);
        if (!this.isJavaAddonKind(this.addonsData.kind)) return '';
        if (!u) return `<p class="text-xs text-gray-500 dark:text-gray-400">Checking Modrinth${paper ? ' and Hangar' : ''} for updates…</p>`;
        if (u.error) return `<p class="text-xs text-yellow-700 dark:text-yellow-300">Couldn't check for updates: ${esc(u.error)}</p>`;
        const n = Object.values(u.items || {}).filter(i => i.update).length;
        let html = '';
        if (u.undo) html += `<div class="${box} bg-gray-50 dark:bg-gray-800 border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100">
            <span>The last update replaced ${u.undo.files.length} file${u.undo.files.length === 1 ? '' : 's'}. The old ones are kept until the next update.</span>
            <button onclick="ServerApp.undoAddonUpdate()" class="px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600">Undo</button></div>`;
        if (n) html += `<div class="${box} bg-blue-50 dark:bg-blue-900/30 border-blue-300 dark:border-blue-800 text-gray-900 dark:text-gray-100">
            <span>${n} update${n === 1 ? '' : 's'} available.</span>
            <span class="flex items-center gap-3"><label class="text-xs flex items-center gap-1"><input type="checkbox" id="addons-backup" checked> Back up the world first</label>
            <button onclick="ServerApp.updateAddons([])" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white">Update all</button></span></div>`;
        else {
            const known = Object.keys(u.items || {}).length, unknown = (u.unknown || []).length;
            const parts = [];
            if (known) parts.push(`Everything from Modrinth${paper ? ' or Hangar' : ''} is up to date.`);
            if (unknown) parts.push(`${unknown} file${unknown === 1 ? '' : 's'} not found on Modrinth${paper ? ' or Hangar' : ''}, so ${unknown === 1 ? "it isn't" : "they aren't"} checked for updates.`);
            if (parts.length) html += `<p class="text-xs text-gray-500 dark:text-gray-400">${parts.join(' ')}</p>`;
        }
        return html;
    },

    async loadAddonUpdates() {
        const server = this.activeServer.name;
        const res = await api(this.addonsUrl().replace(/\?.*$/, '') + '/updates');
        if (!this.activeServer || this.activeServer.name !== server) return;
        this.addonUpdates = res.ok ? await res.json() : { error: await this.apiError(res) };
        if (this.addonsView === 'installed' && document.getElementById('addons-drop')) this.renderAddons();
        this.addonMessages = null;
    },

    async updateAddons(fileList) {
        const box = document.getElementById('addons-backup');
        const backup = fileList.length === 0 && box ? box.checked : false;
        const what = fileList.length ? fileList.join(', ') : 'every mod and plugin with an update';
        if (!confirm(`Update ${what} on ${this.activeServer.name}?${backup ? ' The world is backed up first.' : ''} The old files are kept so you can undo.`)) return;
        this.addonUpdates = null;
        this.renderAddons(['Updating…']);
        const res = await api(this.addonsUrl().replace(/\?.*$/, '') + '/update', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: fileList, backup })
        });
        let messages = [];
        if (!res.ok) messages = [`⛔ ${await this.apiError(res)}`];
        else {
            const r = await res.json();
            if (r.updated.length) this.addonsChanged[this.activeServer.name] = true;
            messages = r.updated.map(u => `✅ ${u.title || u.file} ${u.from || ''} → ${u.to}`)
                .concat(r.failed.map(f => `⛔ ${f.file}: ${f.error}`))
                .concat(r.backup ? [`World backed up first: ${r.backup}`] : []);
            if (!messages.length) messages = ['Nothing to update.'];
        }
        this.addonMessages = messages;
        await this.loadAddons();
    },

    async undoAddonUpdate() {
        if (!confirm('Put back the files the last update replaced?')) return;
        const res = await api(this.addonsUrl().replace(/\?.*$/, '') + '/undo', { method: 'POST' });
        if (!res.ok) return alert(await this.apiError(res));
        const r = await res.json();
        this.addonsChanged[this.activeServer.name] = true;
        this.addonMessages = [`↩️ Put back ${r.restored.join(', ')}`];
        this.addonUpdates = null;
        await this.loadAddons();
    },

    // --- The catalog (Fabric and Paper): starter set and bundles, search with category and sort, details
    addonApi(path, params) {
        const q = new URLSearchParams(params || {}).toString();
        return apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/addons/${path}${q ? '?' + q : ''}`);
    },

    async loadAddonCatalog() {
        const server = this.activeServer.name;
        this.addonCatalog = { loading: true, for: server };
        const res = await api(this.addonApi('catalog'));
        const data = res.ok ? await res.json() : { error: await this.apiError(res) };
        if (!this.activeServer || this.activeServer.name !== server) return;
        this.addonCatalog = { ...data, for: server };
        if (this.addonsView === 'browse') this.renderAddonBrowser();
    },

    addonHome() {
        const s = this.addonSearch;
        return !s || (!s.q && !s.category && !s.sort);
    },

    async searchAddons(more) {
        const d = this.addonsData;
        const s = this.addonSearch || { source: 'modrinth', q: '', category: '', sort: '', hits: [], total: 0 };
        const val = id => { const el = document.getElementById(id); return el ? el.value : null; };
        if (val('addon-q') !== null) s.q = val('addon-q').trim();
        if (val('addon-source') !== null && val('addon-source') !== s.source) { s.source = val('addon-source'); s.category = ''; s.categories = null; }
        else if (val('addon-category') !== null) s.category = val('addon-category');
        if (val('addon-sort') !== null) s.sort = val('addon-sort');
        if (!more) s.hits = [];
        s.loading = true; s.error = null;
        this.addonSearch = s;
        if (!this.addonCatalog || this.addonCatalog.for !== this.activeServer.name) this.loadAddonCatalog();
        this.renderAddonBrowser();
        const res = await api(this.addonApi('search', { source: s.source, q: s.q, offset: s.hits.length, category: s.category, sort: s.sort }));
        s.loading = false;
        if (!res.ok) s.error = await this.apiError(res);
        else {
            const r = await res.json();
            s.hits = s.hits.concat(r.hits);
            s.total = r.total;
            s.sources = r.sources;
            s.categories = r.categories;
            s.sorts = r.sorts;
        }
        if (this.addonsView === 'browse' && this.addonsData === d) this.renderAddonBrowser();
    },

    addonFilter(category) {
        const el = document.getElementById('addon-category');
        if (el) el.value = category;
        this.searchAddons();
    },

    clearAddonSearch() {
        const s = this.addonSearch;
        if (s) { s.q = ''; s.category = ''; s.sort = ''; }
        ['addon-q', 'addon-category', 'addon-sort'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
        this.searchAddons();
    },

    // Projects with an update waiting, from the Installed view's update check
    addonUpdateKeys() {
        const keys = new Set();
        Object.values((this.addonUpdates && this.addonUpdates.items) || {}).forEach(u => {
            if (u.update) { keys.add(`${u.source}:${u.project}`); if (u.slug) keys.add(`${u.source}:${u.slug}`); }
        });
        return keys;
    },

    addonBadge(h, updates) {
        const pill = (cls, text) => `<span class="text-xs px-2 py-0.5 rounded whitespace-nowrap ${cls}">${text}</span>`;
        if (h.installed && (updates.has(`${h.source}:${h.project}`) || updates.has(`${h.source}:${h.slug}`)))
            return pill('bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-200', 'Update available');
        if (h.installed) return pill('bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200', 'Installed');
        if (h.fits === false) return pill('bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300', this.escapeHtml(h.not_fit || 'Not for this version'));
        return '';
    },

    addonCard(h, updates, compact) {
        const esc = t => this.escapeHtml(String(t ?? ''));
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const fmt = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n || 0);
        const icon = h.icon && /^https:\/\//.test(h.icon)
            ? `<img src="${esc(h.icon)}" alt="" loading="lazy" referrerpolicy="no-referrer" class="w-10 h-10 rounded shrink-0 bg-gray-100 dark:bg-gray-700">`
            : '<div class="w-10 h-10 rounded shrink-0 bg-gray-100 dark:bg-gray-700"></div>';
        const open = `ServerApp.openAddonDetail(${this.jsArg(h.source)}, ${this.jsArg(h.slug || h.project)})`;
        const badge = this.addonBadge(h, updates);
        const action = h.installed || h.fits === false ? badge
            : `<button onclick="event.stopPropagation(); ServerApp.installFromCatalog(${this.jsArg(h.source)}, ${this.jsArg(h.project)}, ${this.jsArg(h.title || h.project)})" class="px-3 py-1 rounded text-sm bg-theme hover-bg-theme text-white whitespace-nowrap">Install</button>`;
        const cats = (h.categories || []).slice(0, 3).map(c => `<span class="px-1.5 rounded bg-gray-100 dark:bg-gray-700">${esc(this.addonCategoryLabel(c, h.source))}</span>`).join(' ');
        return `<div onclick="${open}" class="flex items-start gap-3 p-3 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/40 ${compact ? 'rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800' : 'border-t border-gray-200 dark:border-gray-700 first:border-t-0'} ${h.fits === false ? 'opacity-70' : ''}">
            ${icon}
            <div class="min-w-0 flex-1">
                <p class="font-bold text-gray-900 dark:text-white break-words">${esc(h.title)}${h.author ? ` <span class="${note} font-normal">by ${esc(h.author)}</span>` : ''}</p>
                <p class="text-xs text-gray-600 dark:text-gray-300 mt-0.5 ${compact ? 'line-clamp-2' : ''}">${esc(String(h.description || '').slice(0, 220))}</p>
                <p class="${note} mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">${fmt(h.downloads)} downloads${h.updated ? ` · updated ${esc(String(h.updated).slice(0, 10))}` : ''} ${cats}</p>
            </div>
            <div class="flex flex-col items-end gap-1">${action}</div>
        </div>`;
    },

    addonCategoryLabel(id, source) {
        const lists = [this.addonSearch && this.addonSearch.categories, this.addonCatalog && this.addonCatalog.categories];
        for (const l of lists) { const c = (l || []).find(c => c.id === id); if (c) return c.label; }
        return String(id).replace(/[-_]/g, ' ');
    },

    async openAddonDetail(source, project) {
        const server = this.activeServer.name;
        this.addonDetail = { source, project, loading: true };
        this.renderAddonBrowser();
        const el = document.getElementById('addon-detail');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        const res = await api(this.addonApi('details', { source, project }));
        const data = res.ok ? await res.json() : { error: await this.apiError(res) };
        if (!this.activeServer || this.activeServer.name !== server || !this.addonDetail || this.addonDetail.project !== project) return;
        this.addonDetail = { source, project, data };
        if (this.addonsView === 'browse') this.renderAddonBrowser();
    },

    closeAddonDetail() {
        this.addonDetail = null;
        this.renderAddonBrowser();
    },

    addonDetailCard() {
        const det = this.addonDetail;
        if (!det) return '';
        const esc = t => this.escapeHtml(String(t ?? ''));
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const safeUrl = u => /^https:\/\//.test(String(u || '')) ? esc(u) : '#';
        const close = `<button onclick="ServerApp.closeAddonDetail()" class="px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Close</button>`;
        const box = inner => `<div id="addon-detail" class="p-4 rounded border-2 border-theme bg-white dark:bg-gray-800 space-y-3">${inner}</div>`;
        if (det.loading) return box(`<div class="flex justify-between"><p class="${note}">Loading ${esc(det.project)}…</p>${close}</div>`);
        const h = det.data;
        if (h.error) return box(`<div class="flex justify-between gap-2"><p class="text-sm text-red-500">${esc(h.error)}</p>${close}</div>`);
        const fmt = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n || 0);
        const site = h.source === 'hangar' ? 'Hangar' : 'Modrinth';
        const linkNames = { source_url: 'Source code', issues_url: 'Issues', wiki_url: 'Wiki', discord_url: 'Discord' };
        const links = [`<a href="${safeUrl(h.url)}" target="_blank" rel="noopener noreferrer" class="underline">${site} page</a>`]
            .concat(Object.entries(h.links || {}).map(([k, u]) => `<a href="${safeUrl(u)}" target="_blank" rel="noopener noreferrer" class="underline">${linkNames[k] || k}</a>`)).join(' · ');
        const ins = h.install || {};
        let plan;
        if (ins.error) plan = `<p class="text-sm text-yellow-700 dark:text-yellow-300">⚠️ ${esc(ins.error)}</p>`;
        else plan = `<div class="text-sm text-gray-800 dark:text-gray-200 space-y-1">
                <p>Install puts <b>${esc(ins.version)}</b>${ins.channel && ins.channel !== 'release' ? ` (${esc(ins.channel)})` : ''} on this server: <span class="font-mono text-xs">${esc(ins.file)}</span>${ins.size ? ` · ${esc(this.formatSize(ins.size))}` : ''}</p>
                ${(ins.also || []).length ? `<p>It also installs what it needs: ${ins.also.map(a => `<b>${esc(a.title)}</b> ${esc(a.version || '')}`).join(', ')}</p>` : ''}
                ${(ins.notes || []).map(n => `<p class="text-yellow-700 dark:text-yellow-300">⚠️ ${esc(n)}</p>`).join('')}
            </div>`;
        const button = h.installed ? this.addonBadge(h, this.addonUpdateKeys())
            : ins.error ? '' : `<button onclick="ServerApp.installFromCatalog(${this.jsArg(h.source)}, ${this.jsArg(h.project)}, ${this.jsArg(h.title || h.project)})" class="px-4 py-2 rounded text-sm bg-theme hover-bg-theme text-white">Install</button>`;
        return box(`
            <div class="flex items-start gap-3">
                ${h.icon && /^https:\/\//.test(h.icon) ? `<img src="${esc(h.icon)}" alt="" referrerpolicy="no-referrer" class="w-14 h-14 rounded shrink-0 bg-gray-100 dark:bg-gray-700">` : ''}
                <div class="min-w-0 flex-1">
                    <p class="text-lg font-bold text-gray-900 dark:text-white">${esc(h.title)}</p>
                    <p class="text-sm text-gray-700 dark:text-gray-300">${esc(h.description)}</p>
                    <p class="${note} mt-1">${fmt(h.downloads)} downloads · ${fmt(h.follows)} ${h.source === 'hangar' ? 'stars' : 'followers'}${h.updated ? ` · updated ${esc(String(h.updated).slice(0, 10))}` : ''}${h.license ? ` · ${esc(h.license)}` : ''}</p>
                    <p class="${note} mt-1">${links}</p>
                </div>
                ${close}
            </div>
            ${plan}
            <div class="flex justify-end">${button}</div>`);
    },

    addonBundles() {
        const c = this.addonCatalog;
        if (!c || !(c.bundles || []).length) return '';
        const esc = t => this.escapeHtml(String(t ?? ''));
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const cards = c.bundles.map(b => {
            const chips = b.items.map(i => `<span class="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded ${i.installed ? 'bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200' : i.fits ? 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200' : 'bg-gray-100 dark:bg-gray-700 text-gray-500 line-through'}">${i.installed ? '✓ ' : ''}${esc(i.title)}</span>`).join(' ');
            const n = b.missing.length;
            const action = n
                ? `<button onclick="ServerApp.installBundle(${this.jsArg(b.id)}, ${this.jsArg(b.title)})" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white whitespace-nowrap">Install ${n === b.items.length ? 'all' : n} </button>`
                : `<span class="${note}">${b.items.length ? 'All installed' : 'Nothing for this version'}</span>`;
            return `<div class="p-3 rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex flex-col gap-2">
                <p class="font-bold text-gray-900 dark:text-white">${esc(b.title)}</p>
                <p class="text-xs text-gray-600 dark:text-gray-300 flex-1">${esc(b.description)}</p>
                <div class="flex flex-wrap gap-1">${chips}</div>
                ${b.unavailable.length ? `<p class="${note}">Not available for ${esc(c.minecraft)} yet: ${b.unavailable.map(esc).join(', ')}.</p>` : ''}
                <div class="flex justify-end">${action}</div>
            </div>`;
        }).join('');
        return `<div class="space-y-2">
            <div class="flex flex-wrap items-center justify-between gap-2"><h4 class="font-bold text-gray-900 dark:text-white">Bundles</h4>
            <label class="text-xs flex items-center gap-1 text-gray-700 dark:text-gray-300"><input type="checkbox" id="bundle-backup" ${this.bundleBackup === false ? '' : 'checked'} onchange="ServerApp.bundleBackup = this.checked"> Back up the world before installing a bundle</label></div>
            <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">${cards}</div></div>`;
    },

    addonGroups(updates) {
        const c = this.addonCatalog;
        if (!c) return '';
        if (c.loading) return `<p class="text-xs text-gray-500 dark:text-gray-400">Loading the catalog…</p>`;
        if (c.error) return `<p class="text-xs text-yellow-700 dark:text-yellow-300">Couldn't load the catalog: ${this.escapeHtml(c.error)}</p>`;
        return (c.groups || []).map(g => `<div class="space-y-2">
            <h4 class="font-bold text-gray-900 dark:text-white">${this.escapeHtml(g.title)}</h4>
            <div class="grid gap-2 md:grid-cols-2">${g.items.map(h => this.addonCard(h, updates, true)).join('')}</div></div>`).join('');
    },

    renderAddonBrowser(messages) {
        const d = this.addonsData, s = this.addonSearch || { source: 'modrinth', q: '', category: '', sort: '', hits: [] };
        const esc = t => this.escapeHtml(String(t ?? ''));
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const field = 'bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded px-2 py-1.5 text-sm text-gray-900 dark:text-white';
        const noun = this.addonNoun(d.kind);
        const loader = this.loaderName(d.kind);
        const sources = s.sources || (this.isPluginKind(d.kind) ? ['modrinth', 'hangar'] : ['modrinth']);
        const cat = this.addonCatalog && this.addonCatalog.for === this.activeServer.name ? this.addonCatalog : null;
        const categories = s.categories || (s.source === 'modrinth' && cat ? cat.categories : null) || [];
        const sorts = s.sorts || (cat && cat.sorts) || [];
        const updates = this.addonUpdateKeys();
        const home = this.addonHome();
        const rows = s.hits.map(h => this.addonCard(h, updates, false)).join('');
        const site = s.source === 'hangar' ? 'Hangar' : 'Modrinth';
        const sortLabel = (sorts.find(x => x.id === s.sort) || {}).label;
        const catLabel = (categories.find(x => x.id === s.category) || {}).label;
        const resultsTitle = home ? `Most downloaded on ${site}`
            : [s.q ? `“${esc(s.q)}”` : '', catLabel ? esc(catLabel) : '', sortLabel ? esc(sortLabel.toLowerCase()) : ''].filter(Boolean).join(' · ') + ` on ${site}`;
        const changed = this.addonsChanged[this.activeServer.name];
        const chips = home && categories.length ? `<div class="flex flex-wrap gap-1">${categories.slice(0, 10).map(c => `<button onclick="ServerApp.addonFilter(${this.jsArg(c.id)})" class="px-2 py-1 rounded-full text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">${esc(c.label)}</button>`).join('')}</div>` : '';
        document.getElementById('addons-content').innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-2">
                <h3 class="text-lg font-bold text-gray-900 dark:text-white">Find ${noun} <span class="text-sm font-normal text-gray-500">for ${loader} ${esc(d.minecraft || '')}</span></h3>
            </div>
            ${this.addonViewSwitch()}
            <form class="flex flex-wrap gap-2" onsubmit="event.preventDefault(); ServerApp.searchAddons()">
                <input id="addon-q" type="search" value="${esc(s.q)}" placeholder="Search ${noun}" class="flex-1 min-w-[12rem] ${field} px-3">
                ${sources.length > 1 ? `<select id="addon-source" onchange="ServerApp.searchAddons()" class="${field}" aria-label="Site">
                    ${sources.map(x => `<option value="${x}" ${x === s.source ? 'selected' : ''}>${x === 'hangar' ? 'Hangar' : 'Modrinth'}</option>`).join('')}</select>` : ''}
                <select id="addon-category" onchange="ServerApp.searchAddons()" class="${field}" aria-label="Category">
                    <option value="">All categories</option>
                    ${categories.map(c => `<option value="${esc(c.id)}" ${c.id === s.category ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}</select>
                <select id="addon-sort" onchange="ServerApp.searchAddons()" class="${field}" aria-label="Sort">
                    <option value="">${s.q ? 'Best match' : 'Most downloaded'}</option>
                    ${sorts.filter(x => s.q || x.id !== 'downloads').map(x => `<option value="${esc(x.id)}" ${x.id === s.sort ? 'selected' : ''}>${esc(x.label)}</option>`).join('')}</select>
                <button class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white">Search</button>
                ${home ? '' : `<button type="button" onclick="ServerApp.clearAddonSearch()" class="px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white">Back to the catalog</button>`}
            </form>
            ${chips}
            ${(messages || []).length ? `<div class="${card} p-3 text-sm space-y-1 text-gray-800 dark:text-gray-200">${messages.map(m => `<p>${esc(m)}</p>`).join('')}</div>` : ''}
            ${changed ? `<div class="p-3 rounded text-sm bg-blue-50 dark:bg-blue-900/30 border border-blue-300 dark:border-blue-800 text-gray-900 dark:text-gray-100">Restart the server to load your changes. <button onclick="ServerApp.sendPower('restart')" class="underline text-theme">Restart now</button></div>` : ''}
            ${this.addonDetailCard()}
            ${home ? this.addonBundles() + this.addonGroups(updates) : ''}
            <h4 class="font-bold text-gray-900 dark:text-white">${resultsTitle}${s.total && !home ? ` <span class="${note} font-normal">${s.total} found</span>` : ''}</h4>
            ${s.error ? `<p class="text-sm text-red-500">${esc(s.error)}</p>` : ''}
            <div class="${card}">${rows || `<p class="p-4 text-sm text-gray-500 dark:text-gray-400">${s.loading ? 'Searching…' : `No ${noun} found for this Minecraft version.`}</p>`}</div>
            ${s.hits.length && s.hits.length < (s.total || 0) ? `<button onclick="ServerApp.searchAddons(true)" class="px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white" ${s.loading ? 'disabled' : ''}>${s.loading ? 'Loading…' : 'More'}</button>` : ''}
            <p class="${note}">Only ${noun} with a build for ${loader} ${esc(d.minecraft || '')} that run on a server are listed (the starter set marks the ones not updated yet). Click one for details. Install picks the newest release build, checks its hash, and installs what it needs${d.kind === 'fabric' ? ' (such as Fabric API)' : ''} too.</p>`;
    },

    // After an install, every view that shows "Installed" is out of date
    addonsInstalled() {
        this.addonsChanged[this.activeServer.name] = true;
        this.addonUpdates = null;
        if (this.addonDetail && this.addonDetail.data) this.openAddonDetail(this.addonDetail.source, this.addonDetail.project);
        this.loadAddonCatalog();
    },

    async installFromCatalog(source, project, title) {
        const s = this.addonSearch;
        this.renderAddonBrowser([`Installing ${title}…`]);
        const res = await api(this.addonApi('install'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source, project })
        });
        let messages;
        if (!res.ok) messages = [`⛔ ${title}: ${await this.apiError(res)}`];
        else {
            const r = await res.json();
            messages = r.installed.map(i => `✅ ${i.name || i.file} ${i.version || ''}${i.channel && i.channel !== 'release' ? ` (${i.channel})` : ''}`
                + (i.fits === false ? ' — made for a different Minecraft version' : '')).concat(r.notes.map(n => `⚠️ ${n}`));
            if (s) s.hits.forEach(h => { if (h.project === project || r.installed.some(i => i.project === h.project)) h.installed = true; });
            this.addonsInstalled();
        }
        this.renderAddonBrowser(messages);
    },

    async installBundle(bundle, title) {
        const backup = this.bundleBackup !== false;
        if (!confirm(`Install the ${title} bundle on ${this.activeServer.name}?${backup ? ' The world is backed up first.' : ''} `
            + `Restart the server afterwards to load them.`)) return;
        this.renderAddonBrowser([`Installing ${title}${backup ? ' (backing up the world first)' : ''}…`]);
        const res = await api(this.addonApi('bundle'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bundle, backup })
        });
        let messages;
        if (!res.ok) messages = [`⛔ ${title}: ${await this.apiError(res)}`];
        else {
            const r = await res.json();
            messages = [];
            if (r.backup) messages.push(`💾 World backed up (${r.backup}).`);
            messages = messages.concat(r.installed.map(i => `✅ ${i.name || i.file} ${i.version || ''}`))
                .concat(r.skipped.length ? [`Already installed: ${r.skipped.join(', ')}`] : [])
                .concat(r.failed.map(f => `⛔ ${f.project}: ${f.error}`))
                .concat(r.notes.map(n => `⚠️ ${n}`));
            if (!r.installed.length && !r.failed.length) messages.push('Nothing to install: everything in it is already here.');
            if (r.installed.length) this.addonsInstalled();
        }
        this.renderAddonBrowser(messages);
    },

    async dropOnAddons(fileList) {
        const box = document.getElementById('upload-progress');
        const messages = await this.installAddons(fileList, box);
        await this.loadAddons();
        this.renderAddons(messages);
    },

    async toggleAddon(item, enabled) {
        const res = await api(this.addonsUrl().replace(/\?.*$/, '') + '/toggle', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item, enabled })
        });
        if (!res.ok) return alert(await this.apiError(res));
        this.addonsChanged[this.activeServer.name] = true;
        this.loadAddons();
    },

    async removeAddon(item, label) {
        if (!confirm(`Remove ${label} from ${this.activeServer.name}? The file is deleted.`)) return;
        const res = await api(this.addonsUrl({ item }), { method: 'DELETE' });
        if (!res.ok) return alert(await this.apiError(res));
        this.addonsChanged[this.activeServer.name] = true;
        this.loadAddons();
    },

    // Drag and drop onto the Files and Mods/Plugins tabs (set up once)
    setupDropZones() {
        const zone = (id, onDrop) => {
            const el = document.getElementById(id);
            if (!el) return;
            let depth = 0;
            const on = v => el.classList.toggle('ring-4', v);
            el.addEventListener('dragenter', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); depth++; on(true); } });
            el.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); });
            el.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; on(false); } });
            el.addEventListener('drop', e => {
                e.preventDefault(); depth = 0; on(false);
                if (e.dataTransfer.files.length) onDrop(e.dataTransfer.files);
            });
        };
        zone('tab-files', files => this.dropOnFiles(files));
        zone('tab-addons', files => this.dropOnAddons(files));
    },

    // Files tab: mods, plugins and packs dropped at the top or in their folder are installed
    // (sorted and checked); everything else is uploaded into the folder you're in.
    async dropOnFiles(fileList) {
        const kind = this.addonKind(this.activeServer);
        const folder = this.isPluginKind(kind) ? 'plugins' : this.MOD_KINDS.includes(kind) ? 'mods' : undefined;
        const asAddon = f => {
            const n = f.name.toLowerCase();
            if (kind === 'bedrock') return n.endsWith('.mcpack') || n.endsWith('.mcaddon');
            return !!folder && n.endsWith('.jar') && (this.filesPath === '' || this.filesPath === folder);
        };
        const all = Array.from(fileList);
        const addons = all.filter(asAddon), plain = all.filter(f => !asAddon(f));
        const status = document.getElementById('files-status');
        let messages = [];
        if (addons.length) {
            messages = await this.installAddons(addons, status);
        }
        this.filesNotice = messages;          // shown once, above the list, by renderFiles
        if (plain.length) await this.uploadFiles(plain);
        else await this.loadFiles(this.filesPath);
    },

    renderBackups() {
        const info = this.backupInfo;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const s = info.schedule;
        const isAdmin = this.currentUser && this.currentUser.role === 'admin';
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded p-5 shadow-sm';
        const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
        const freqOptions = Object.entries(info.frequencies)
            .map(([id, label]) => `<option value="${esc(id)}" ${id === s.frequency ? 'selected' : ''}>${esc(label)}</option>`).join('');
        const kindLabel = { scheduled: 'Scheduled', manual: 'Manual', 'pre-restore': 'Before restore', 'pre-change': 'Before mod changes', 'pre-update': 'Before update', 'pre-convert': 'Before type change' };

        let last = '';
        if (info.last) {
            const when = new Date(info.last.at * 1000).toLocaleString();
            last = info.last.ok
                ? `<p class="text-xs text-gray-500 dark:text-gray-400">Last backup: ${esc(when)}${info.last.consistent === false
                    ? ' <span class="text-yellow-600 dark:text-yellow-500">(copied while the server was writing: see the console)</span>' : ''}</p>`
                : `<p class="text-xs text-red-500">Last backup failed (${esc(when)}): ${esc(info.last.error)}</p>`;
        }
        const javaNoRcon = this.activeServer.edition === 'Java' && !/^\d+$/.test(String(this.activeServer.rcon_port || ''));

        const rows = info.backups.map(b => `
            <tr class="border-t border-gray-200 dark:border-gray-700">
                <td class="rp-card-head py-2 pr-3 whitespace-nowrap">${esc(new Date(b.created * 1000).toLocaleString())}</td>
                <td class="py-2 pr-3" data-label="Type">${esc(kindLabel[b.kind] || b.kind)}</td>
                <td class="py-2 pr-3 whitespace-nowrap" data-label="Made by" title="The server software that last wrote this backup's world">${b.made_by ? esc(b.made_by.label) : '<span class="text-gray-400">not recorded</span>'}</td>
                <td class="py-2 pr-3 text-right whitespace-nowrap" data-label="Size">${esc(this.formatSize(b.size))}</td>
                <td class="rp-card-actions py-2 text-right whitespace-nowrap">
                    <a href="${apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/${encodeURIComponent(b.file)}`)}" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white" title="Download">⬇</a>
                    ${isAdmin ? `<button onclick="ServerApp.restoreBackup('${esc(b.file)}')" class="px-2 py-1 rounded bg-yellow-600 hover:bg-yellow-700 text-white" title="Restore this world">Restore</button>
                    <button onclick="ServerApp.deleteBackup('${esc(b.file)}')" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-red-700 hover:text-white text-gray-900 dark:text-white" title="Delete">🗑</button>` : ''}
                </td>
            </tr>`).join('');

        document.getElementById('backups-content').innerHTML = `
            <div class="${card} max-w-xl">
                <div class="flex items-center justify-between mb-3">
                    <p class="text-sm font-bold text-gray-900 dark:text-white">Schedule</p>
                    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                        <input type="checkbox" id="backup-enabled" ${s.enabled ? 'checked' : ''}> Automatic backups
                    </label>
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-3">
                    <select id="backup-frequency" class="${field}">${freqOptions}</select>
                    <input type="time" id="backup-time" value="${esc(s.time)}" class="${field}" title="Time of day on the server${info.timezone ? ` (${esc(info.timezone)})` : ''}">
                    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">Keep
                        <input type="number" id="backup-keep" min="1" max="365" value="${esc(s.keep)}" class="${field} w-20"></label>
                </div>
                <label class="flex flex-wrap items-center gap-2 text-sm text-gray-700 dark:text-gray-300 mb-3">Also delete them after
                    <input type="number" id="backup-keep-days" min="0" max="3650" value="${s.keep_days ? esc(s.keep_days) : ''}" placeholder="never" class="${field} w-24"> days</label>
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">The time is the server's clock${info.timezone ? ` (${esc(info.timezone)})` : ''}, not your browser's. Runs as a systemd timer, so backups happen even while the panel is stopped. Only scheduled backups are pruned to the number (and age) kept, and the newest one always stays; manual ones stay until you delete them.</p>
                ${info.next_run && s.enabled ? `<p class="text-xs text-gray-500 dark:text-gray-400 mb-2">Next backup: ${esc(info.next_run)}</p>` : ''}
                ${last}
                <div class="flex flex-wrap gap-2 mt-3">
                    <button onclick="ServerApp.saveBackupSchedule(this)" class="bg-theme hover-bg-theme text-white px-4 py-2 rounded font-bold text-sm">Save schedule</button>
                    <button onclick="ServerApp.backupNow(this)" class="bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-4 py-2 rounded font-bold text-sm">Back up now</button>
                </div>
                ${javaNoRcon ? `<p class="text-xs text-yellow-600 dark:text-yellow-500 mt-3">RCON isn't set up for this server, so backups taken while it runs copy files it may be writing. Open the Live Chat tab once to set RCON up.</p>` : ''}
            </div>
            <div class="${card}">
                <div class="flex flex-wrap gap-x-3 items-center justify-between mb-2">
                    <p class="text-sm font-bold text-gray-900 dark:text-white">Backups</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400 break-all" title="${esc(info.directory)}">${info.backups.length} · ${esc(this.formatSize(info.total_size))} in ${esc(info.directory)}</p>
                </div>
                ${info.backups.length ? `
                <div class="overflow-x-auto -mx-3 sm:mx-0"><table class="rp-cards w-full text-sm text-gray-700 dark:text-gray-300">
                    <thead><tr class="text-left text-xs text-gray-500 dark:text-gray-400"><th class="pb-1">Taken</th><th class="pb-1">Type</th><th class="pb-1">Made by</th><th class="pb-1 text-right">Size</th><th></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-3">Restore replaces the current world with the backup's. The server is stopped while that happens, and the world it replaces is saved first as a "Before restore" backup. A world made by a newer Minecraft version than the server runs isn't restored, because the older version would damage it.</p>`
                : `<p class="text-sm text-gray-500 dark:text-gray-400">No backups yet.</p>`}
            </div>
            <div id="backup-copies-box"></div>
            <div id="offsite-box"></div>`;
    },

    async saveBackupSchedule(button) {
        const body = {
            enabled: document.getElementById('backup-enabled').checked,
            frequency: document.getElementById('backup-frequency').value,
            time: document.getElementById('backup-time').value,
            keep: parseInt(document.getElementById('backup-keep').value, 10) || 0,
            keep_days: parseInt(document.getElementById('backup-keep-days').value, 10) || 0,
        };
        button.disabled = true;
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/schedule`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        button.disabled = false;
        if (!res.ok) return alert(`Couldn't save the schedule: ${await this.apiError(res)}`);
        this.loadBackups();
    },

    async loadRestarts() {
        if (!this.activeServer) return;
        const name = this.activeServer.name;
        const res = await api(`/api/servers/${encodeURIComponent(name)}/restarts`);
        if (!this.activeServer || this.activeServer.name !== name) return;
        const box = document.getElementById('restart-content');
        if (!res.ok) {
            box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        const info = await res.json();
        const esc = t => this.escapeHtml(String(t ?? ''));
        const s = info.schedule;
        const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
        const freqOptions = Object.entries(info.frequencies)
            .map(([id, label]) => `<option value="${esc(id)}" ${id === s.frequency ? 'selected' : ''}>${esc(label)}</option>`).join('');
        const warnOptions = info.warn_choices
            .map(m => `<option value="${m}" ${m === s.warn ? 'selected' : ''}>${m ? `${m} min warning` : 'No warning'}</option>`).join('');
        const tz = info.timezone ? ` (${esc(info.timezone)})` : '';
        const javaNoRcon = this.activeServer.edition === 'Java' && !/^\d+$/.test(String(this.activeServer.rcon_port || ''));
        box.innerHTML = `
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded p-5 max-w-xl shadow-sm">
                <div class="flex items-center justify-between mb-3">
                    <p class="text-sm text-gray-600 dark:text-gray-300">Restart the server regularly, warning players in game first.</p>
                    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 whitespace-nowrap ml-3">
                        <input type="checkbox" id="restart-enabled" ${s.enabled ? 'checked' : ''}> On
                    </label>
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-3">
                    <select id="restart-frequency" class="${field}">${freqOptions}</select>
                    <input type="time" id="restart-time" value="${esc(s.time)}" class="${field}" title="Time of day on the server${tz}">
                    <select id="restart-warn" class="${field}">${warnOptions}</select>
                </div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">The time is the server's clock${tz}. Players see "Server restarts in N minutes" in chat, then again 1 minute and 10 seconds before. A stopped server stays stopped, and a restart waits for a running backup to finish.</p>
                ${info.next_restart && s.enabled ? `<p class="text-xs text-gray-500 dark:text-gray-400 mb-2">Next restart: ${esc(new Date(info.next_restart * 1000).toLocaleString())}</p>` : ''}
                ${javaNoRcon ? `<p class="text-xs text-yellow-600 dark:text-yellow-500 mb-2">RCON isn't set up for this server, so players can't be warned. Open the Live Chat tab once to set it up.</p>` : ''}
                <button onclick="ServerApp.saveRestartSchedule(this)" class="bg-theme hover-bg-theme text-white px-4 py-2 rounded font-bold text-sm mt-1">Save schedule</button>
            </div>`;
    },

    async saveRestartSchedule(button) {
        const body = {
            enabled: document.getElementById('restart-enabled').checked,
            frequency: document.getElementById('restart-frequency').value,
            time: document.getElementById('restart-time').value,
            warn: parseInt(document.getElementById('restart-warn').value, 10) || 0,
        };
        button.disabled = true;
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/restarts`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        button.disabled = false;
        if (!res.ok) return alert(`Couldn't save the restart schedule: ${await this.apiError(res)}`);
        this.loadRestarts();
    },

    async backupNow(button) {
        button.disabled = true;
        button.innerText = 'Backing up…';
        this.toggleConsole(true);
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups`, { method: 'POST' });
        if (!res.ok) alert(`Backup failed: ${await this.apiError(res)}`);
        else if (this.backupCopies && this.backupCopies.to) {
            // Copy it to the other member now, not at the master's next pass
            fetch(`/api/cluster/backup-copies/${encodeURIComponent(this.backupMember())}/${encodeURIComponent(this.activeServer.name)}/sync`, { method: 'POST' });
        }
        this.loadBackups();
    },

    async restoreBackup(file) {
        const b = this.backupInfo.backups.find(x => x.file === file);
        const when = b ? new Date(b.created * 1000).toLocaleString() : file;
        if (!confirm(`Replace ${this.activeServer.name}'s world with the backup from ${when}?\n\nThe server stops while this happens. The current world is saved first as a "Before restore" backup.`)) return;
        this.toggleConsole(true);
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/${encodeURIComponent(file)}/restore`, { method: 'POST' });
        if (!res.ok) alert(`Restore failed: ${await this.apiError(res)}`);
        else alert('World restored.');
        this.loadBackups();
    },

    async deleteBackup(file) {
        if (!confirm(`Delete the backup ${file}? This can't be undone.`)) return;
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/${encodeURIComponent(file)}`, { method: 'DELETE' });
        if (!res.ok) alert(`Delete failed: ${await this.apiError(res)}`);
        this.loadBackups();
    },

    // --- 1.16.11: copies on SSH or S3-compatible backup storage (Panel Settings → Backup storage) ---
    offsite: null,

    async loadOffsite() {
        const box = document.getElementById('offsite-box');
        if (!box || !this.activeServer) return;
        if (this.notYet('offsite')) { box.className = 'hidden'; return; }           // not in the Windows preview yet
        const name = this.activeServer.name;
        const res = await api(`/api/servers/${encodeURIComponent(name)}/offsite`);
        if (!this.activeServer || this.activeServer.name !== name) return;
        box.className = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded p-5 shadow-sm';
        if (res.status === 404 && (await res.clone().json().catch(() => ({}))).detail === 'Not Found') {
            box.innerHTML = `<p class="text-xs text-gray-500 dark:text-gray-400">Off-site copies need RedstonePanel 1.16.11 or later on ${this.escapeHtml(this.activeServer.node_name || 'this machine')}.</p>`;
            return;
        }
        if (!res.ok) {
            box.innerHTML = `<p class="text-sm text-red-500">Off-site copies: ${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        this.offsite = await res.json();
        this.renderOffsite();
    },

    renderOffsite() {
        const box = document.getElementById('offsite-box');
        const v = this.offsite;
        if (!box || !v) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const isAdmin = this.currentUser && this.currentUser.role === 'admin';
        const onNode = !!(this.activeServer && this.activeServer.node);
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const btn = 'px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const kindLabel = { scheduled: 'Scheduled', manual: 'Manual' };
        const head = `<p class="text-sm font-bold text-gray-900 dark:text-white mb-1">Off-site copies</p>`;
        if (!v.targets.length) {
            box.innerHTML = `${head}<p class="${note}">A backup on the same disk doesn't survive losing the disk. Copy these backups to an SSH server (a NAS, another PC, a VPS) or S3-compatible storage (Backblaze B2, Wasabi, Cloudflare R2, MinIO, AWS S3)${onNode
                ? `: add the storage in Panel Settings → Backup storage on ${esc(this.activeServer.node_name || 'the node')}'s own panel, since each machine copies its own servers' backups.`
                : isAdmin ? ` under <button onclick="ServerApp.openPanelSettings('storage')" class="underline text-theme">Panel Settings → Backup storage</button>.` : '. An admin adds it under Panel Settings → Backup storage.'}</p>`;
            return;
        }
        const blocks = v.targets.map(t => {
            const st = t.status;
            let state = '';
            if (t.enabled && st) {
                const when = new Date(st.at * 1000).toLocaleString();
                state = st.ok
                    ? `<p class="${note}">Last checked ${esc(when)}: every backup it keeps has its copy${st.last_copied_at ? `; last copied ${esc(new Date(st.last_copied_at * 1000).toLocaleString())}` : ''}.</p>`
                    : `<p class="text-xs text-red-500">Copying failed (${esc(when)}): ${esc(st.error)}</p>`;
            } else if (t.enabled) {
                state = `<p class="${note}">Not copied yet: the first copy starts within a few minutes.</p>`;
            }
            const rows = t.copies.map(c => `
                <tr class="border-t border-gray-200 dark:border-gray-700">
                    <td class="rp-card-head py-2 pr-3 whitespace-nowrap">${esc(new Date(c.created * 1000).toLocaleString())}</td>
                    <td class="py-2 pr-3" data-label="Type">${esc(kindLabel[c.kind] || c.kind)}</td>
                    <td class="py-2 pr-3 text-right whitespace-nowrap" data-label="Size">${esc(this.formatSize(c.size))}</td>
                    <td class="rp-card-actions py-2 text-right whitespace-nowrap">${isAdmin ? `
                        <button onclick="ServerApp.restoreOffsite(${this.jsArg(t.id)}, ${this.jsArg(c.file)})" class="px-2 py-1 rounded bg-yellow-600 hover:bg-yellow-700 text-white" title="Download it, check it, and restore its world">Restore</button>
                        <button onclick="ServerApp.deleteOffsite(${this.jsArg(t.id)}, ${this.jsArg(c.file)})" class="${btn} hover:bg-red-700 hover:text-white" title="Delete this copy">🗑</button>` : ''}</td>
                </tr>`).join('');
            const retention = `keeps ${esc(t.keep)} scheduled${t.keep_days ? `, at most ${esc(t.keep_days)} days old` : ''}, and every manual one`;
            return `<div class="border-t border-gray-200 dark:border-gray-700 pt-3 mt-3">
                <label class="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300 ${isAdmin ? 'cursor-pointer' : ''}">
                    <input type="checkbox" class="mt-1" ${t.enabled ? 'checked' : ''} ${isAdmin ? '' : 'disabled'} onchange="ServerApp.toggleOffsite(${this.jsArg(t.id)}, this.checked, this)">
                    <span>Copy to <b class="text-gray-900 dark:text-white">${esc(t.name)}</b> <span class="${note}">${t.type === 's3' ? 'S3' : 'SSH'} · <span class="break-all">${esc(t.where)}</span> · ${retention}</span></span>
                </label>
                ${state}
                ${t.error ? `<p class="text-xs text-red-500">Couldn't list the copies: ${esc(t.error)}</p>` : ''}
                ${t.copies.length ? `<div class="overflow-x-auto -mx-3 sm:mx-0 mt-2"><table class="rp-cards w-full text-sm text-gray-700 dark:text-gray-300">
                    <thead><tr class="text-left text-xs text-gray-500 dark:text-gray-400"><th class="pb-1">Taken</th><th class="pb-1">Type</th><th class="pb-1 text-right">Size</th><th></th></tr></thead>
                    <tbody>${rows}</tbody></table></div>` : t.enabled && !t.error ? `<p class="${note} mt-1">No copies there yet.</p>` : ''}
            </div>`;
        }).join('');
        const anyOn = v.targets.some(t => t.enabled);
        box.innerHTML = `${head}
            <p class="${note}">Copied after each scheduled or manual backup, checked with SHA-256 when it arrives and again before a restore. Copies run in the background, so a slow upload never holds the server up. Old scheduled copies are deleted there by the storage's own Keep; the newest copy always stays.</p>
            ${blocks}
            ${anyOn ? `<div class="flex flex-wrap gap-2 mt-3"><button onclick="ServerApp.offsiteCopyNow(this)" class="${btn} text-sm font-bold px-3 py-1.5">Copy now</button></div>` : ''}
            ${isAdmin && anyOn ? `<p class="${note} mt-2"><b>Restore</b> downloads the copy into the backups above, checks it, then restores its world as usual (the current world is saved first).</p>` : ''}`;
    },

    async toggleOffsite(tid, enabled, input) {
        input.disabled = true;
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/offsite/${encodeURIComponent(tid)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled })
        });
        if (!res.ok) alert(`Couldn't change it: ${await this.apiError(res)}`);
        this.loadOffsite();
    },

    async offsiteCopyNow(button) {
        button.disabled = true;
        button.innerText = 'Copying…';
        this.toggleConsole(true);
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/offsite/sync`, { method: 'POST' });
        if (!res.ok) { alert(await this.apiError(res)); button.disabled = false; button.innerText = 'Copy now'; return; }
        setTimeout(() => this.loadOffsite(), 4000);
    },

    async restoreOffsite(tid, file) {
        const name = this.activeServer.name;
        if (!confirm(`Restore ${name}'s world from the off-site copy ${file}?\n\nThe copy is downloaded into this server's backups and checked first. Then the server stops while its world is replaced; the current world is saved first as a "Before restore" backup.`)) return;
        this.toggleConsole(true);
        const res = await api(`/api/servers/${encodeURIComponent(name)}/offsite/${encodeURIComponent(tid)}/fetch`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file })
        });
        if (!res.ok) { alert(`Couldn't download the copy: ${await this.apiError(res)}`); return; }
        const got = await res.json();
        const r = await api(`/api/servers/${encodeURIComponent(name)}/backups/${encodeURIComponent(got.file)}/restore`, { method: 'POST' });
        if (!r.ok) alert(`The copy is in the backups list as ${got.file}, but restoring failed: ${await this.apiError(r)}`);
        else alert('World restored from the off-site copy.');
        this.loadBackups();
    },

    async deleteOffsite(tid, file) {
        if (!confirm(`Delete the off-site copy ${file}? This can't be undone.`)) return;
        const res = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/offsite/${encodeURIComponent(tid)}/${encodeURIComponent(file)}`, { method: 'DELETE' });
        if (!res.ok) alert(`Delete failed: ${await this.apiError(res)}`);
        this.loadOffsite();
    },

    // Import → From backup storage: a new server from a copy (the machine it came from is gone)
    async openStorageImport() {
        const box = document.getElementById('storage-import');
        if (!box) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const field = 'w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white text-sm mb-2';
        box.innerHTML = `<p class="text-xs text-gray-500">Loading…</p>`;
        const res = await fetch('/api/settings/backup-storage');
        if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${esc(await this.apiError(res))}</p>`; return; }
        const d = await res.json();
        if (!d.targets.length) {
            box.innerHTML = `<p class="text-xs text-gray-500">No backup storage yet. Add the storage the old machine copied to under <button onclick="ServerApp.openPanelSettings('storage')" class="underline text-theme">Panel Settings → Backup storage</button>, then come back here.</p>`;
            return;
        }
        box.innerHTML = `<select id="storage-import-target" onchange="ServerApp.browseStorage(this.value)" class="${field}">
                ${d.targets.map(t => `<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.where)})</option>`).join('')}
            </select>
            <div id="storage-import-list"><p class="text-xs text-gray-500">Loading…</p></div>`;
        this.browseStorage(d.targets[0].id);
    },

    async browseStorage(tid) {
        const list = document.getElementById('storage-import-list');
        const esc = t => this.escapeHtml(String(t ?? ''));
        list.innerHTML = `<p class="text-xs text-gray-500">Loading…</p>`;
        const res = await fetch(`/api/settings/backup-storage/${encodeURIComponent(tid)}/browse`);
        if (!res.ok) { list.innerHTML = `<p class="text-sm text-red-500">${esc(await this.apiError(res))}</p>`; return; }
        const d = await res.json();
        this.storageCopies = d.copies;
        if (!d.copies.length) { list.innerHTML = `<p class="text-xs text-gray-500">No backups there.</p>`; return; }
        const field = 'w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white text-sm mb-2';
        list.innerHTML = `<select id="storage-import-copy" class="${field}">
                ${d.copies.map((c, i) => `<option value="${i}">${esc(c.server)} · ${esc(new Date(c.created * 1000).toLocaleString())} · ${esc(this.formatSize(c.size))} · from ${esc(c.panel)}${c.panel === d.panel_id ? ' (this machine)' : ''}</option>`).join('')}
            </select>
            <p class="text-xs text-gray-500 mb-2">The server gets its old name if it's free here. Its server software is downloaded again (a Java server at the same Minecraft version and type), then it starts.</p>
            <button onclick="ServerApp.importFromStorage(this)" class="w-full bg-blue-600 hover:bg-blue-700 py-2 rounded text-white font-bold transition">Import from backup storage</button>`;
    },

    async importFromStorage(button) {
        const tid = document.getElementById('storage-import-target').value;
        const c = this.storageCopies[parseInt(document.getElementById('storage-import-copy').value, 10)];
        if (!c) return;
        button.disabled = true;
        button.innerText = 'Downloading, checking and setting up…';
        this.toggleConsole(true);
        const res = await fetch(`/api/settings/backup-storage/${encodeURIComponent(tid)}/import`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ panel: c.panel, server: c.server, file: c.file })
        });
        if (!res.ok) {
            alert(`Import failed: ${await this.apiError(res)}`);
            button.disabled = false;
            button.innerText = 'Import from backup storage';
            return;
        }
        const r = await res.json();
        document.getElementById('modal-overlay').classList.add('hidden');
        alert(`Imported as ${r.name} (port ${r.port}).${r.old_port ? ` Its old port ${r.old_port} is in use here.` : ''}`);
        this.loadServers && this.loadServers();
    },

    // Panel Settings → Backup storage
    storage: null,
    storageEdit: null,

    async loadStorageSettings() {
        const box = document.getElementById('set-storage');
        if (!box) return;
        const res = await fetch('/api/settings/backup-storage');
        if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
        this.storage = await res.json();
        this.renderStorageSettings();
    },

    renderStorageSettings(message, ok) {
        const box = document.getElementById('set-storage');
        const d = this.storage;
        if (!box || !d) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const small = 'text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-3 py-1.5 rounded font-bold transition';
        const rows = d.targets.map(t => `
            <div class="border border-gray-200 dark:border-gray-700 rounded p-3 mb-2">
                <div class="flex flex-wrap items-center justify-between gap-2">
                    <p class="text-sm text-gray-900 dark:text-white"><b>${esc(t.name)}</b> <span class="${note}">${t.type === 's3' ? 'S3' : 'SSH'} · <span class="break-all">${esc(t.where)}</span></span></p>
                    <div class="flex flex-wrap gap-1">
                        <button onclick="ServerApp.testStorage(${this.jsArg(t.id)}, this)" class="${small}">Test</button>
                        <button onclick="ServerApp.editStorage(${this.jsArg(t.id)})" class="${small}">Edit</button>
                        <button onclick="ServerApp.removeStorage(${this.jsArg(t.id)})" class="${small}">Remove</button>
                    </div>
                </div>
                <p class="${note} mt-1">${t.servers === 'all' ? 'Every server' : t.servers.length ? `${t.servers.length} server${t.servers.length === 1 ? '' : 's'}: ${esc(t.servers.join(', '))}` : 'No servers yet: tick it on a server\'s Backups tab'} ·
                    keeps ${t.keep_count ? `${esc(t.keep_count)} scheduled copies` : 'as many scheduled copies as each server keeps'}${t.keep_days ? `, at most ${esc(t.keep_days)} days old` : ''}</p>
                ${t.type === 'ssh' ? `<p class="${note} mt-1">Host key: ${t.host_key ? `<span class="font-mono break-all">${esc(t.host_key)}</span> <button onclick="ServerApp.forgetHostKey(${this.jsArg(t.id)})" class="underline text-theme">Forget host key</button>` : 'not seen yet (recorded on the first connection)'}</p>` : ''}
            </div>`).join('');
        box.innerHTML = `
            <p class="${note} mb-2">Copies of the game servers' backups off this machine, so they survive losing its disk. Each server's <b>Backups</b> tab says which storage gets its copies and lets you restore from them. A server can also be brought back on a new machine from here (Import → From backup storage).</p>
            ${rows || `<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">No backup storage yet.</p>`}
            ${message ? `<p class="text-sm mb-2 ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'}">${esc(message)}</p>` : ''}
            ${d.public_key ? `<details class="mb-2"><summary class="text-sm cursor-pointer text-gray-700 dark:text-gray-300">This panel's SSH key</summary>
                <p class="${note} mt-1">For SSH storage, add this line to <span class="font-mono">~/.ssh/authorized_keys</span> of the user the panel signs in as there (one line, on that machine):</p>
                <textarea readonly rows="2" onclick="this.select()" class="w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-xs font-mono text-gray-900 dark:text-white mt-1">${esc(d.public_key)}</textarea>
            </details>` : `<p class="${note} mb-2">SSH storage needs openssh-client and rsync: <span class="font-mono">sudo apt install openssh-client rsync</span></p>`}
            ${d.tools && !d.tools.rsync ? `<p class="text-xs text-yellow-600 dark:text-yellow-500 mb-2">rsync isn't installed, so SSH copies can't run. Install it with: <span class="font-mono">sudo apt install rsync</span></p>` : ''}
            <div id="storage-form">${this.storageEdit ? this.storageFormHtml(this.storageEdit) : `<button onclick="ServerApp.editStorage(null)" class="${small}">Add storage</button>`}</div>`;
    },

    storageFormHtml(t) {
        const esc = t => this.escapeHtml(String(t ?? ''));
        const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white mt-1';
        const label = 'text-xs text-gray-500 dark:text-gray-400';
        const small = 'text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-3 py-1.5 rounded font-bold transition';
        const s3 = t.type === 's3';
        const radio = (v, text) => `<label class="flex items-center gap-1 text-sm text-gray-700 dark:text-gray-300"><input type="radio" name="st-type" value="${v}" ${t.type === v ? 'checked' : ''} onchange="ServerApp.storageType(this.value)"> ${text}</label>`;
        return `<div class="border border-gray-300 dark:border-gray-600 rounded p-3">
            <p class="text-sm font-bold text-gray-900 dark:text-white mb-2">${t.id ? `Edit ${esc(t.name)}` : 'Add storage'}</p>
            ${t.id ? '' : `<div class="flex gap-4 mb-2">${radio('ssh', 'SSH server (rsync)')}${radio('s3', 'S3-compatible')}</div>`}
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <label class="${label} sm:col-span-2">Name<input id="st-name" value="${esc(t.name)}" placeholder="${s3 ? 'Backblaze B2' : 'NAS'}" class="${field}"></label>
                ${s3 ? `
                <label class="${label} sm:col-span-2">Endpoint<input id="st-endpoint" value="${esc(t.endpoint)}" placeholder="https://s3.eu-central-003.backblazeb2.com" class="${field} font-mono"></label>
                <label class="${label}">Bucket<input id="st-bucket" value="${esc(t.bucket)}" class="${field} font-mono"></label>
                <label class="${label}">Folder in the bucket (optional)<input id="st-prefix" value="${esc(t.prefix)}" placeholder="minecraft" class="${field} font-mono"></label>
                <label class="${label}">Region<input id="st-region" value="${esc(t.region || '')}" placeholder="us-east-1 (R2: auto)" class="${field} font-mono"></label>
                <label class="${label} flex items-end gap-2 pb-2"><input type="checkbox" id="st-path-style" ${t.path_style === false ? '' : 'checked'}> Path-style addresses (MinIO, most providers)</label>
                <label class="${label}">Access key<input id="st-access" autocomplete="off" placeholder="${t.id ? 'unchanged' : ''}" class="${field} font-mono"></label>
                <label class="${label}">Secret key<input id="st-secret" type="password" autocomplete="new-password" placeholder="${t.id ? 'unchanged' : ''}" class="${field} font-mono"></label>`
                : `
                <label class="${label}">Host<input id="st-host" value="${esc(t.host)}" placeholder="nas.lan or 192.168.1.20" class="${field} font-mono"></label>
                <label class="${label}">Port<input id="st-port" type="number" min="1" max="65535" value="${esc(t.port || 22)}" class="${field}"></label>
                <label class="${label}">User<input id="st-user" value="${esc(t.user)}" placeholder="backup" class="${field} font-mono"></label>
                <label class="${label}">Folder<input id="st-folder" value="${esc(t.folder || 'redstonepanel-backups')}" class="${field} font-mono"></label>`}
                <label class="${label}">Scheduled copies to keep<input id="st-keep" type="number" min="0" max="365" value="${esc(t.keep_count || '')}" placeholder="same as each server" class="${field}"></label>
                <label class="${label}">Delete copies after (days)<input id="st-days" type="number" min="0" max="3650" value="${esc(t.keep_days || '')}" placeholder="never" class="${field}"></label>
                <label class="${label} sm:col-span-2 flex items-center gap-2"><input type="checkbox" id="st-all" ${t.servers === 'all' ? 'checked' : ''}> Copy every server's backups (or choose on each server's Backups tab)</label>
            </div>
            <p class="${label} mt-2">${s3 ? 'Give the key read, write, list and delete on the bucket only. The secret is kept on this machine (root only) and never shown again.'
                : 'The panel signs in with its own SSH key (above) and records the host key on the first connection, as ssh does. The folder is relative to that user\'s home unless it starts with /. That machine needs rsync.'}</p>
            <p id="st-msg" class="hidden text-sm mt-2"></p>
            <div class="flex flex-wrap gap-2 mt-3">
                <button onclick="ServerApp.saveStorage(this)" class="bg-theme hover-bg-theme text-white px-3 py-1.5 rounded font-bold text-xs">Save</button>
                <button onclick="ServerApp.testStorage(null, this)" class="${small}">Test</button>
                <button onclick="ServerApp.storageEdit = null; ServerApp.renderStorageSettings()" class="${small}">Cancel</button>
            </div>
        </div>`;
    },

    editStorage(tid) {
        const t = tid ? this.storage.targets.find(x => x.id === tid) : null;
        this.storageEdit = t ? { ...t } : { type: 'ssh', servers: 'all', port: 22, folder: 'redstonepanel-backups' };
        this.renderStorageSettings();
        const el = document.getElementById('storage-form');
        if (el) el.scrollIntoView({ block: 'nearest' });
    },

    storageType(type) {
        this.storageEdit = { ...this.storageFields(), type, servers: this.storageEdit.servers };
        document.getElementById('storage-form').innerHTML = this.storageFormHtml(this.storageEdit);
    },

    storageFields() {
        const val = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
        const t = this.storageEdit || {};
        const f = { type: t.type, name: val('st-name'), keep_count: parseInt(val('st-keep'), 10) || 0,
                    keep_days: parseInt(val('st-days'), 10) || 0 };
        const all = document.getElementById('st-all');
        f.servers = all && all.checked ? 'all' : (t.servers === 'all' ? [] : (t.servers || []));
        if (t.type === 's3') {
            Object.assign(f, { endpoint: val('st-endpoint'), bucket: val('st-bucket'), prefix: val('st-prefix'),
                region: val('st-region'), access_key: val('st-access'), secret_key: val('st-secret'),
                path_style: !!(document.getElementById('st-path-style') || {}).checked });
        } else {
            Object.assign(f, { host: val('st-host'), port: parseInt(val('st-port'), 10) || 22, user: val('st-user'), folder: val('st-folder') });
        }
        return f;
    },

    storageMessage(text, ok) {
        const el = document.getElementById('st-msg');
        if (!el) return this.renderStorageSettings(text, ok);
        el.innerText = text;
        el.className = `text-sm mt-2 ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`;
    },

    async saveStorage(button) {
        const t = this.storageEdit;
        button.disabled = true;
        const res = await fetch(t.id ? `/api/settings/backup-storage/${encodeURIComponent(t.id)}` : '/api/settings/backup-storage', {
            method: t.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.storageFields())
        });
        button.disabled = false;
        if (!res.ok) return this.storageMessage(await this.apiError(res), false);
        this.storage = await res.json();
        this.storageEdit = null;
        this.renderStorageSettings('Saved. Use Test to check it can write there.', true);
    },

    async testStorage(tid, button) {
        const old = button.innerText;
        button.disabled = true;
        button.innerText = 'Testing…';
        const body = tid ? { id: tid } : { id: this.storageEdit.id || null, fields: this.storageFields() };
        const res = await fetch('/api/settings/backup-storage/test', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        button.disabled = false;
        button.innerText = old;
        if (!res.ok) return this.storageMessage(await this.apiError(res), false);
        const r = await res.json();
        const done = r.steps.length ? ` (${r.steps.join(', ')})` : '';
        const text = r.ok ? `It works: ${r.steps.join(', ')}.${r.host_key ? ` Host key ${r.host_key}.` : ''}` : `${r.error}${done ? ` Done before that: ${r.steps.join(', ')}.` : ''}`;
        if (tid) {
            await this.loadStorageSettings();
            this.renderStorageSettings(text, r.ok);
        } else {
            this.storageMessage(text, r.ok);
        }
    },

    async removeStorage(tid) {
        const t = this.storage.targets.find(x => x.id === tid);
        if (!confirm(`Remove the backup storage ${t ? t.name : tid}?\n\nNo more copies go there. The copies already there stay there; delete them on the storage itself if you want them gone.`)) return;
        const res = await fetch(`/api/settings/backup-storage/${encodeURIComponent(tid)}`, { method: 'DELETE' });
        if (!res.ok) return this.renderStorageSettings(await this.apiError(res), false);
        this.storage = await res.json();
        this.renderStorageSettings('Removed.', true);
    },

    async forgetHostKey(tid) {
        if (!confirm('Forget the host key recorded for this storage?\n\nOnly do this if that machine was reinstalled or its SSH keys changed on purpose: the next connection accepts whatever key it finds then.')) return;
        const res = await fetch(`/api/settings/backup-storage/${encodeURIComponent(tid)}/forget-host-key`, { method: 'POST' });
        if (!res.ok) return this.renderStorageSettings(await this.apiError(res), false);
        this.storage = await res.json();
        this.renderStorageSettings('Host key forgotten. Use Test to connect and record the new one.', true);
    },

    // --- Found servers: Minecraft servers on this machine that the panel doesn't manage ---
    foundServers: [],
    takenOver: [],
    foundNotes: [],
    foundLooked: false,       // true once the panel has answered, so "none found" isn't shown too early

    async loadFoundServers(message, ok) {
        try {
            const res = this.isAdmin() ? await fetch('/api/found-servers') : { ok: false };
            const d = res.ok ? await res.json() : {};
            this.foundServers = d.servers || [];
            this.takenOver = d.taken_over || [];
            this.foundNotes = d.noticed || [];
            this.foundLooked = res.ok;
        } catch (e) {
            this.foundServers = [];
            this.takenOver = [];
            this.foundNotes = [];
            this.foundLooked = false;
        }
        const box = document.getElementById('found-servers');
        if (box) box.innerHTML = this.foundServersHtml(message, ok);
        return this.foundServers;
    },

    foundServersHtml(message, ok) {
        if (this.deployNode || !(this.foundLooked || this.foundServers.length || this.takenOver.length || message)) return '';
        const esc = t => this.escapeHtml(t == null ? '' : String(t));
        const button = 'px-3 py-1 rounded text-sm font-bold whitespace-nowrap disabled:opacity-50';
        const rows = this.foundServers.map((f, i) => `
                <div class="p-2 mb-2 rounded border border-gray-200 dark:border-gray-700 flex flex-col sm:flex-row sm:items-center gap-2">
                    <div class="flex-1 min-w-0">
                        <div class="text-sm text-gray-900 dark:text-white"><b>${esc(f.edition)}${f.version ? ' ' + esc(f.version) : ''}</b> on port ${esc(f.port)}${f.running ? '' : ' <span class="text-xs text-gray-500">(stopped)</span>'}</div>
                        <div class="text-xs text-gray-500 dark:text-gray-400 font-mono break-all">${esc(f.folder)}</div>
                        <div class="text-xs text-gray-500 dark:text-gray-400">Started by ${esc(f.started_by)}${f.user ? ', as ' + esc(f.user) : ''}${f.heap ? ', memory ' + esc(f.heap) : ''}</div>
                    </div>
                    <button onclick="ServerApp.manageFound(${i}, this)" class="${button} bg-theme hover-bg-theme text-white">Manage with RedstonePanel</button>
                </div>`).join('');
        const taken = this.takenOver.map(t => `
                <div class="p-2 mb-2 rounded border border-gray-200 dark:border-gray-700 flex flex-col sm:flex-row sm:items-center gap-2">
                    <div class="flex-1 min-w-0">
                        <div class="text-sm text-gray-900 dark:text-white"><b>${esc(t.name)}</b> <span class="text-xs text-gray-500">taken over${t.service ? ' from ' + esc(t.service) : ''}</span></div>
                        <div class="text-xs text-gray-500 dark:text-gray-400 font-mono break-all">${esc(t.folder)}</div>
                    </div>
                    <button onclick="ServerApp.undoFound(${this.jsArg(t.name)}, this)" class="${button} bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Undo</button>
                </div>`).join('');
        const notes = this.foundNotes.map(n => `
                <div class="p-2 mb-2 rounded border border-dashed border-gray-300 dark:border-gray-600">
                    <div class="text-sm text-gray-700 dark:text-gray-300"><b>${esc(n.title)}</b> <span class="text-xs text-gray-500">can't be managed yet</span></div>
                    ${n.folder ? `<div class="text-xs text-gray-500 dark:text-gray-400 font-mono break-all">${esc(n.folder)}</div>` : ''}
                    <div class="text-xs text-gray-500 dark:text-gray-400">${esc(n.reason)}</div>
                </div>`).join('');
        const none = !(rows || taken || notes) ? `<p class="text-sm text-gray-500 dark:text-gray-400 mb-2">No other Minecraft servers were found running on this computer. A server has to be running to be found.</p>` : '';
        return `
            <hr class="border-gray-200 dark:border-gray-700 my-4" />
            <label class="block text-sm text-gray-400 mb-1">Found on this computer</label>
            <p class="text-xs text-gray-500 mb-2">Minecraft servers here that were set up outside RedstonePanel. Nothing about them is changed until you choose Manage. Then the panel stops the service that runs one and turns it off (it isn't deleted), and runs the server from the same folder itself; no files are copied or moved, and Undo puts it back.</p>
            ${message ? `<p class="text-sm ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'} mb-2">${esc(message)}</p>` : ''}
            ${none}${rows}${taken}${notes}`;
    },

    async manageFound(index, button) {
        const f = this.foundServers[index];
        if (!f) return;
        const how = f.service ? `${f.service} will be stopped and turned off (not deleted). Players are disconnected for a moment.`
                              : 'It must already be stopped where you started it.';
        if (!confirm(`Hand the ${f.edition} server in ${f.folder} to RedstonePanel?\n\n${how}\nIts files stay where they are and are given to the minecraft account. Undo puts everything back.`)) return;
        button.disabled = true; button.innerText = 'Taking over…';
        const res = await fetch('/api/found-servers/manage', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder: f.folder })
        });
        if (!res.ok) return this.loadFoundServers(await this.apiError(res), false);
        const d = await res.json();
        await this.loadFoundServers(`${d.name} now runs the server in ${d.folder}.`, true);
        if (this.loadServers) this.loadServers();
    },

    async undoFound(name, button) {
        if (!confirm(`Hand ${name} back to how it ran before?\n\nThe panel's service for it is removed, with its backup and restart schedules. Its files and world stay, and the service that ran it before is started again.`)) return;
        button.disabled = true; button.innerText = 'Handing back…';
        const res = await fetch(`/api/found-servers/${encodeURIComponent(name)}/undo`, { method: 'POST' });
        if (!res.ok) return this.loadFoundServers(await this.apiError(res), false);
        await this.loadFoundServers(`${name} was handed back.`, true);
        if (this.loadServers) this.loadServers();
    },

    // --- Removed servers: uninstalled with "Keep World", files still in /opt ---
    removedServers: [],

    async loadRemovedServers() {
        try {
            // Removed servers are an admin's business (1.18)
            const res = this.isAdmin() ? await api('/api/servers/removed', { node: this.removedNode() }) : { ok: false };
            this.removedServers = res.ok ? await res.json() : [];
        } catch (e) {
            this.removedServers = [];
        }
        const hint = document.getElementById('removed-hint');
        if (hint) {
            const n = this.removedServers.length;
            hint.classList.toggle('hidden', n === 0);
            hint.innerHTML = n === 0 ? '' : `
                <button onclick="ServerApp.openDeployModal()" class="w-full text-left text-xs text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 underline">
                    ♻️ ${n} removed server${n === 1 ? '' : 's'}: restore</button>`;
        }
        const box = document.getElementById('removed-servers');
        if (box) box.innerHTML = this.removedServersHtml();
        return this.removedServers;
    },

    // Removed servers are listed for the New Server dialog's "Where" while it is open
    removedNode() {
        return document.getElementById('removed-servers') ? this.deployNode : null;
    },

    removedServersHtml() {
        if (!this.removedServers.length) return '';
        const esc = t => this.escapeHtml(String(t ?? ''));
        // Deleting a removed server's files can't be undone, so it's never done from the master
        const isAdmin = this.currentUser && this.currentUser.role === 'admin' && !this.removedNode();
        const rows = this.removedServers.map(s => {
            const details = [s.edition, s.port ? `port ${esc(s.port)}` : '', s.world ? `world “${esc(s.world)}”` : '']
                .filter(Boolean).join(' • ');
            let note = '';
            if (!s.can_restore) {
                note = `<p class="text-xs text-yellow-500 mt-1">Can't tell whether this is a Java or Bedrock server, so it can't be restored here.</p>`;
            } else if (!s.has_binary) {
                note = `<p class="text-xs text-yellow-500 mt-1">The server binary is missing; the latest ${esc(s.edition)} release will be downloaded.${s.edition === 'Java' ? ' A world from an older version is upgraded and can\'t go back, so export it first.' : ''}</p>`;
            }
            if (s.moved_to) {
                note += `<p class="text-xs text-gray-400 mt-1">Left behind by a move to ${esc(s.moved_to)}. Restore it only if the move went wrong.</p>`;
            }
            const when = s.removed_at ? ` • removed ${new Date(s.removed_at * 1000).toLocaleDateString()}` : '';
            const masterAdmin = this.isMaster() && this.currentUser.role === 'admin' && this.removedNode() && s.moved_to;
            return `
                <div class="border border-gray-200 dark:border-gray-700 rounded p-2 mb-2">
                    <div class="flex items-center justify-between gap-2">
                        <div class="min-w-0">
                            <div class="font-bold text-gray-900 dark:text-white truncate">${esc(s.name)}</div>
                            <div class="text-xs text-gray-400 truncate" title="${esc(s.directory)}">${details}${when}</div>
                        </div>
                        <div class="flex gap-1 shrink-0">
                            <button onclick="ServerApp.restoreRemoved('${esc(s.name)}', this)" ${s.can_restore ? '' : 'disabled'}
                                class="px-3 py-1 rounded text-sm font-bold text-white ${s.can_restore ? 'bg-green-600 hover:bg-green-700' : 'bg-gray-600 cursor-not-allowed'}">Restore</button>
                            ${isAdmin ? `<button onclick="ServerApp.deleteRemoved('${esc(s.name)}')" title="Delete its files permanently"
                                class="px-2 py-1 rounded text-sm bg-gray-200 dark:bg-gray-700 hover:bg-red-700 text-gray-900 dark:text-white hover:text-white">🗑</button>` : ''}
                            ${masterAdmin ? `<button onclick="ServerApp.deleteMovedCopy(${this.jsArg(this.removedNode())}, ${this.jsArg(s.name)}, this)" title="Delete the files this move left behind"
                                class="px-2 py-1 rounded text-sm bg-gray-200 dark:bg-gray-700 hover:bg-red-700 text-gray-900 dark:text-white hover:text-white">🗑</button>` : ''}
                        </div>
                    </div>
                    ${note}
                </div>`;
        }).join('');
        return `
            <hr class="border-gray-200 dark:border-gray-700 my-4" />
            <label class="block text-sm text-gray-400 mb-1">Removed servers</label>
            <p class="text-xs text-gray-500 mb-2">Uninstalled with "Keep World". Their files are still on this machine; restoring gives them a new service under the same name.</p>
            ${rows}`;
    },

    async restoreRemoved(name, button) {
        const srv = this.removedServers.find(s => s.name === name);
        if (!srv) return;
        this.logNode = this.removedNode();
        if (!srv.has_binary && !confirm(`${name} has no server binary. Download the latest ${srv.edition} release and restore it?`)) return;
        if (button) { button.disabled = true; button.innerText = 'Restoring…'; }
        this.toggleConsole(true);
        const node = this.removedNode();
        const send = () => api(`/api/servers/removed/${encodeURIComponent(name)}/restore`, { node, method: 'POST' });
        const res = await this.withJava(await send(), send, node);
        if (!res.ok) {
            alert(`Restore failed: ${await this.apiError(res)}`);
        } else {
            const data = await res.json();
            this.closeModal();
            alert(data.old_port
                ? `Restored ${name}. Its old port ${data.old_port} is used by another server, so it now uses port ${data.port}.`
                : `Restored ${name} on port ${data.port}.`);
        }
        await this.loadServers();
    },

    async deleteRemoved(name) {
        if (!confirm(`Permanently delete ${name} and all its worlds from this machine? This can't be undone.`)) return;
        const res = await fetch(`/api/servers/removed/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (!res.ok) alert(`Delete failed: ${await this.apiError(res)}`);
        await this.loadRemovedServers();
    },

    async executeDeploy() {
        const edition = document.getElementById('deploy-edition').value;
        const tempId = document.getElementById('deploy-template').value;
        const payload = { edition: edition };
        if (tempId) payload.template_id = tempId;
        if (edition === 'java') {
            payload.loader = document.getElementById('deploy-loader').value;
            const version = document.getElementById('deploy-version').value;
            if (version) payload.version = version;
        }
        const advanced = this.advancedPayload(edition);
        if (advanced) payload.advanced = advanced;
        const node = this.deployNode;
        if (node && tempId && !await this.pushTemplate(node, tempId)) return;
        document.getElementById('modal-overlay').classList.add('hidden');
        this.toggleConsole(true, node);

        const send = () => api('/api/servers/deploy', {
            node,
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const res = await this.withJava(await send(), send, node);
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (e) {}
            alert(`Deploy failed: ${detail}`);
        }
        this.loadServers();
    },

    exportServer() {
        if (!this.activeServer) return;
        window.location.href = apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/export`);
    },

    async executeImport() {
        const fileInput = document.getElementById('import-file');
        if (!fileInput.files.length) return alert("Select a server backup (.zip, .tar.gz or .tar) first.");

        const formData = new FormData();
        formData.append("file", fileInput.files[0]);

        const node = this.deployNode;
        document.getElementById('modal-overlay').classList.add('hidden');
        this.toggleConsole(true, node);

        const res = await api('/api/servers/import', {
            node,
            method: 'POST',
            body: formData
        });
        if (!res.ok) {
            alert(`Import failed: ${await this.apiError(res)}`);
        } else {
            const data = await res.json();
            if (data.server) alert(`Imported as ${data.server}.`);
        }

        this.loadServers();
    },
    
    // --- Modpacks (1.20): a server from a Modrinth modpack, a .mrpack or a server pack ---
    modpack: { view: 'modrinth', q: '', loader: '', hits: null, total: 0, error: null, pick: null, versions: null },

    openModpackDialog() {
        this.modpack = { view: 'modrinth', q: '', loader: '', hits: null, total: 0, error: null, pick: null, versions: null };
        this.renderModpackDialog();
        this.searchModpacks();
    },

    modpackOptionsHtml() {
        const input = 'w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white';
        const label = 'block text-sm text-gray-400 mb-1';
        return `<div class="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-3">
                <div><label class="${label}" for="pack-name">Server name</label>
                    <input id="pack-name" type="text" maxlength="30" autocapitalize="none" spellcheck="false" placeholder="Automatic" class="${input}"></div>
                <div><label class="${label}" for="pack-port">Game port</label>
                    <input id="pack-port" type="number" inputmode="numeric" min="1024" max="65535" placeholder="Automatic" class="${input}"></div>
                <div><label class="${label}" for="pack-memory">Memory (heap)</label>
                    <input id="pack-memory" type="text" maxlength="8" autocapitalize="characters" spellcheck="false" placeholder="Automatic (up to 4G)" class="${input}"></div>
            </div>`;
    },

    renderModpackDialog() {
        const m = this.modpack, esc = t => this.escapeHtml(String(t ?? ''));
        this.modalWide(true);
        const input = 'w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white';
        const label = 'block text-sm text-gray-400 mb-1';
        const tab = (v, text) => `<button onclick="ServerApp.modpackView('${v}')" class="flex-1 px-3 py-2 rounded text-sm font-bold ${m.view === v
            ? 'bg-theme text-white' : 'bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white hover:bg-gray-300 dark:hover:bg-gray-600'}">${text}</button>`;
        const where = this.deployNode ? ` on ${esc(this.nodeName(this.deployNode))}` : '';
        let body;
        if (m.view === 'upload') {
            body = `
                <label class="${label}" for="pack-file">Pack file (.mrpack or server pack .zip)</label>
                <input type="file" id="pack-file" accept=".mrpack,.zip,application/zip" class="${input} text-sm mb-2" />
                <p class="text-xs text-gray-500 mb-3">A <b>.mrpack</b> is a Modrinth modpack. A <b>server pack</b> is the zip most CurseForge packs offer under Files → Additional Files → Server Pack: download it there and upload it here. Its mods and config are used; its start scripts are never run, and the panel installs the loader itself.</p>
                <details class="mb-3 rounded border border-gray-200 dark:border-gray-700">
                    <summary class="cursor-pointer select-none px-3 py-2 text-sm font-bold text-gray-900 dark:text-white">If the pack doesn't say what it needs</summary>
                    <div class="px-3 pb-3">
                        <p class="text-xs text-gray-500 mb-2">Only for server packs, and only when the panel can't work it out from the pack. Leave empty otherwise.</p>
                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
                            <div><label class="${label}" for="pack-loader">Loader</label>
                                <select id="pack-loader" class="${input}"><option value="">From the pack</option><option value="forge">Forge</option><option value="neoforge">NeoForge</option><option value="fabric">Fabric</option><option value="quilt">Quilt</option></select></div>
                            <div><label class="${label}" for="pack-minecraft">Minecraft version</label>
                                <input id="pack-minecraft" type="text" maxlength="12" spellcheck="false" placeholder="From the pack" class="${input}"></div>
                            <div><label class="${label}" for="pack-loader-version">Loader version</label>
                                <input id="pack-loader-version" type="text" maxlength="40" spellcheck="false" placeholder="Recommended" class="${input}"></div>
                        </div>
                    </div>
                </details>
                ${this.modpackOptionsHtml()}
                <button onclick="ServerApp.createModpackServer('upload')" class="w-full bg-green-600 hover:bg-green-700 py-2 rounded text-white font-bold transition">Upload & create server${where}</button>`;
        } else if (m.pick) {
            const p = m.pick, vs = m.versions;
            body = `
                <button onclick="ServerApp.modpackBack()" class="text-sm text-theme underline mb-3 min-h-[44px] sm:min-h-0">← Back to the list</button>
                <div class="flex gap-3 items-start mb-3">
                    ${p.icon ? `<img src="${esc(p.icon)}" alt="" class="w-14 h-14 rounded shrink-0" loading="lazy" referrerpolicy="no-referrer">` : ''}
                    <div class="min-w-0">
                        <p class="font-bold text-gray-900 dark:text-white">${esc(p.title)}</p>
                        <p class="text-sm text-gray-600 dark:text-gray-300">${esc(p.description || '')}</p>
                        <a href="${esc(p.url)}" target="_blank" rel="noopener" class="text-xs text-theme underline">Open on Modrinth</a>
                    </div>
                </div>
                <label class="${label}" for="pack-version">Version</label>
                ${vs === null ? `<p class="text-sm text-gray-500 mb-3">Loading versions…</p>`
                  : !vs.length ? `<p class="text-sm text-yellow-700 dark:text-yellow-300 mb-3">This pack has no version the panel can install.</p>`
                  : `<select id="pack-version" class="${input} mb-3">${vs.map((v, i) => `<option value="${esc(v.id)}">${esc(v.version || v.name)}: ${esc((v.loaders || []).map(l => this.loaderName(l)).join('/'))} for Minecraft ${esc((v.minecraft || []).slice(-1)[0] || '?')}${v.channel !== 'release' ? ` (${esc(v.channel)})` : ''}</option>`).join('')}</select>`}
                ${this.modpackOptionsHtml()}
                <p class="text-xs text-gray-500 mb-3">Only the pack's server-side files are downloaded, each checked against the pack's hash. Big packs take several minutes; the console shows progress.</p>
                <button onclick="ServerApp.createModpackServer('modrinth')" ${vs && vs.length ? '' : 'disabled'} class="w-full bg-green-600 hover:bg-green-700 py-2 rounded text-white font-bold transition ${vs && vs.length ? '' : 'opacity-50'}">Create server${where}</button>`;
        } else {
            const rows = m.error ? `<p class="p-3 text-sm text-red-500">${esc(m.error)}</p>`
                : m.hits === null ? `<p class="p-3 text-sm text-gray-500">Searching Modrinth…</p>`
                : !m.hits.length ? `<p class="p-3 text-sm text-gray-500">No modpacks found.</p>`
                : m.hits.map((h, i) => `
                    <button onclick="ServerApp.pickModpack(${i})" class="w-full text-left flex gap-3 items-center p-3 min-h-[44px] border-b border-gray-200 dark:border-gray-700 last:border-b-0 hover:bg-gray-100 dark:hover:bg-gray-700">
                        ${h.icon ? `<img src="${esc(h.icon)}" alt="" class="w-10 h-10 rounded shrink-0" loading="lazy" referrerpolicy="no-referrer">` : '<span class="w-10 h-10 rounded shrink-0 bg-gray-200 dark:bg-gray-700"></span>'}
                        <span class="min-w-0 flex-1">
                            <span class="block font-bold text-gray-900 dark:text-white truncate">${esc(h.title)}</span>
                            <span class="text-xs text-gray-600 dark:text-gray-400 line-clamp-2">${esc(h.description || '')}</span>
                            <span class="block text-xs text-gray-500">${esc((h.loaders || []).map(l => this.loaderName(l)).join(', '))}${h.latest_minecraft ? ` · up to ${esc(h.latest_minecraft)}` : ''} · ${esc(this.compactCount(h.downloads))} downloads</span>
                        </span>
                    </button>`).join('');
            body = `
                <div class="flex flex-col sm:flex-row gap-2 mb-3">
                    <input id="pack-q" type="search" value="${esc(m.q)}" placeholder="Search modpacks on Modrinth" onkeydown="if (event.key === 'Enter') ServerApp.searchModpacks()" class="${input} flex-1">
                    <select id="pack-filter-loader" onchange="ServerApp.searchModpacks()" class="${input} sm:w-40">
                        <option value="">Any loader</option>${['neoforge', 'forge', 'fabric', 'quilt'].map(l => `<option value="${l}" ${m.loader === l ? 'selected' : ''}>${this.loaderName(l)}</option>`).join('')}
                    </select>
                    <button onclick="ServerApp.searchModpacks()" class="px-4 py-2 rounded bg-theme hover-bg-theme text-white font-bold">Search</button>
                </div>
                <div class="rounded border border-gray-200 dark:border-gray-700 mb-2 max-h-[50vh] overflow-y-auto">${rows}</div>
                <p class="text-xs text-gray-500 mb-3">Modpacks from Modrinth that can run on a server${m.hits && m.total > m.hits.length ? `; showing the first ${m.hits.length} of ${esc(this.compactCount(m.total))}` : ''}. CurseForge packs: download the pack's server pack and use Upload a pack.</p>`;
        }
        document.getElementById('modal-content').innerHTML = `
            <h3 class="text-xl font-bold mb-1">New server from a modpack</h3>
            <p class="text-xs text-gray-500 mb-3">${this.deployNode ? `It will be created on ${esc(this.nodeName(this.deployNode))}.` : ''}</p>
            <div class="flex gap-2 mb-4">${tab('modrinth', 'Modrinth')}${tab('upload', 'Upload a pack')}</div>
            ${body}
            <div class="flex gap-2 mt-3">
                <button onclick="ServerApp.openDeployModal()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white transition">Back</button>
                <button onclick="ServerApp.closeModal()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white transition">Cancel</button>
            </div>`;
        document.getElementById('modal-overlay').classList.remove('hidden');
    },

    compactCount(n) {
        n = Number(n) || 0;
        return n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
    },

    modpackView(view) { this.modpack.view = view; this.modpack.pick = null; this.renderModpackDialog(); },
    modpackBack() { this.modpack.pick = null; this.renderModpackDialog(); },

    async searchModpacks() {
        const m = this.modpack;
        const q = document.getElementById('pack-q'), l = document.getElementById('pack-filter-loader');
        if (q) m.q = q.value.trim();
        if (l) m.loader = l.value;
        m.hits = null; m.error = null;
        this.renderModpackDialog();
        const asked = `${m.q}|${m.loader}`;
        const res = await api(`/api/modpacks/search?q=${encodeURIComponent(m.q)}&loader=${encodeURIComponent(m.loader)}`, { node: this.deployNode });
        if (this.modpack !== m || asked !== `${m.q}|${m.loader}`) return;
        if (!res.ok) { m.error = `Couldn't search Modrinth: ${await this.apiError(res)}`; m.hits = []; }
        else { const d = await res.json(); m.hits = d.hits; m.total = d.total; }
        if (m.view === 'modrinth' && !m.pick) this.renderModpackDialog();
    },

    async pickModpack(i) {
        const m = this.modpack;
        m.pick = m.hits[i]; m.versions = null;
        this.renderModpackDialog();
        const res = await api(`/api/modpacks/versions?project=${encodeURIComponent(m.pick.project)}`, { node: this.deployNode });
        if (this.modpack !== m || !m.pick || m.pick.project !== m.hits[i].project) return;
        m.versions = res.ok ? (await res.json()).versions : [];
        this.renderModpackDialog();
    },

    async createModpackServer(how) {
        const v = id => { const el = document.getElementById(id); return el ? String(el.value || '').trim() : ''; };
        const form = new FormData();
        if (how === 'modrinth') {
            form.append('source', 'modrinth');
            form.append('project', this.modpack.pick.project);
            form.append('version_id', v('pack-version'));
        } else {
            const input = document.getElementById('pack-file');
            if (!input.files.length) return alert('Choose a .mrpack or a server pack .zip first.');
            const file = input.files[0];
            form.append('source', /\.mrpack$/i.test(file.name) ? 'mrpack' : 'serverpack');
            form.append('file', file);
            for (const [k, id] of [['loader', 'pack-loader'], ['minecraft', 'pack-minecraft'], ['loader_version', 'pack-loader-version']]) form.append(k, v(id));
        }
        for (const [k, id] of [['name', 'pack-name'], ['port', 'pack-port'], ['memory', 'pack-memory']]) form.append(k, v(id));
        const node = this.deployNode;
        this.closeModal();
        this.toggleConsole(true, node);
        const send = () => api('/api/servers/modpack', { node, method: 'POST', body: form });
        const res = await this.withJava(await send(), send, node);
        if (!res.ok) alert(`The modpack server wasn't created: ${await this.apiError(res)}`);
        else {
            const d = await res.json();
            alert(`Created ${d.name} from ${d.pack ? d.pack.name : 'the pack'}${d.pack && d.pack.version ? ' ' + d.pack.version : ''}.`
                + ` Its first start takes a few minutes.${(d.notes || []).length ? '\n\n' + d.notes.join('\n') : ''}`);
        }
        this.loadServers();
    },

    // Maintenance → Modpack: what this server was made from, and moving it to a newer version
    async loadModpack() {
        const card = document.getElementById('modpack-card'), body = document.getElementById('modpack-body');
        const srv = this.activeServer;
        if (!card || !srv) return;
        this.activePack = null;
        if (srv.edition !== 'Java') { card.classList.add('hidden'); return; }
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/modpack`);
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        const pack = res.ok ? (await res.json()).pack : null;
        this.activePack = pack;
        card.classList.toggle('hidden', !pack);
        if (!pack) return;
        const esc = t => this.escapeHtml(String(t ?? ''));
        const admin = this.isAdmin();
        const btn = 'px-3 py-2 rounded font-bold text-sm min-h-[44px] sm:min-h-0';
        body.innerHTML = `
            <p class="text-sm text-gray-900 dark:text-white"><b>${esc(pack.name)}</b>${pack.version ? ` ${esc(pack.version)}` : ''}</p>
            <p class="text-xs text-gray-600 dark:text-gray-400 mt-1">${esc(this.loaderName(pack.loader))} for Minecraft ${esc(pack.minecraft)} · ${pack.files} mod file${pack.files === 1 ? '' : 's'} from the pack · ${pack.kind === 'serverpack' ? 'an uploaded server pack' : pack.can_check ? `from <a href="${esc(pack.url || '#')}" target="_blank" rel="noopener" class="text-theme underline">Modrinth</a>` : 'an uploaded .mrpack'}</p>
            ${admin ? `
            <p class="text-xs text-gray-600 dark:text-gray-400 mt-3">Updating swaps the pack's mods for the new version's and installs the loader it needs. The world, player data, your settings and mods you added yourself are kept.</p>
            <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 mt-2"><input type="checkbox" id="modpack-backup" checked> Back up the server first</label>
            <div id="modpack-check" class="mt-3">${pack.can_check ? `<button onclick="ServerApp.checkModpackUpdate()" class="${btn} bg-theme hover-bg-theme text-white">Check for a newer version</button>` : ''}</div>
            <div class="mt-3 flex flex-col sm:flex-row gap-2 sm:items-center">
                <input type="file" id="modpack-file" accept=".mrpack,.zip,application/zip" class="flex-1 min-w-0 bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white">
                <button onclick="ServerApp.updateModpack('upload')" class="${btn} bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white shrink-0">Upload a newer version</button>
            </div>` : `<p class="text-xs text-gray-500 mt-2">An admin can update the pack.</p>`}`;
        const text = document.getElementById('update-text');
        if (text) text.innerText = `Runs ${this.loaderName(pack.loader)} ${pack.minecraft} for the ${pack.name} modpack. Update the pack under Modpack instead: a different Minecraft version here would break its mods. This only downloads the newest ${this.loaderName(pack.loader)} build.`;
    },

    async checkModpackUpdate() {
        const box = document.getElementById('modpack-check'), srv = this.activeServer;
        const esc = t => this.escapeHtml(String(t ?? ''));
        box.innerHTML = '<p class="text-sm text-gray-500">Asking Modrinth…</p>';
        const res = await api(`/api/servers/${encodeURIComponent(srv.name)}/modpack/check`, { method: 'POST' });
        if (!this.activeServer || this.activeServer.name !== srv.name) return;
        if (!res.ok) { box.innerHTML = `<p class="text-sm text-yellow-700 dark:text-yellow-300">Couldn't check: ${esc(await this.apiError(res))}</p>`; return; }
        const d = await res.json();
        if (!d.available) { box.innerHTML = `<p class="text-sm text-green-700 dark:text-green-400">✓ ${esc(d.current || 'This version')} is the newest version of the pack.</p>`; return; }
        const options = d.versions.map(v => `<option value="${esc(v.id)}" ${v.id === d.latest.id ? 'selected' : ''}>${esc(v.version || v.name)} for Minecraft ${esc((v.minecraft || []).slice(-1)[0] || '?')}${v.channel !== 'release' ? ` (${esc(v.channel)})` : ''}</option>`).join('');
        box.innerHTML = `
            <p class="text-sm text-gray-900 dark:text-white">⬆ <b>${esc(d.latest.version || d.latest.name)}</b> is available${d.current ? ` (this server has ${esc(d.current)})` : ''}.</p>
            <div class="mt-2 flex flex-col sm:flex-row gap-2">
                <select id="modpack-version" class="flex-1 min-w-0 bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-sm text-gray-900 dark:text-white">${options}</select>
                <button onclick="ServerApp.updateModpack('modrinth')" class="px-3 py-2 rounded font-bold text-sm bg-theme hover-bg-theme text-white min-h-[44px] sm:min-h-0 shrink-0">Update the pack</button>
            </div>`;
    },

    async updateModpack(how) {
        const srv = this.activeServer, pack = this.activePack;
        if (!srv || !pack) return;
        const form = new FormData();
        const backup = document.getElementById('modpack-backup').checked;
        form.append('backup', backup ? 'true' : 'false');
        let what = 'the uploaded version';
        if (how === 'modrinth') {
            const sel = document.getElementById('modpack-version');
            form.append('source', 'modrinth');
            form.append('version_id', sel.value);
            what = sel.selectedOptions[0].text;
        } else {
            const input = document.getElementById('modpack-file');
            if (!input.files.length) return alert("Choose the newer version's .mrpack or server pack .zip first.");
            form.append('source', /\.mrpack$/i.test(input.files[0].name) ? 'mrpack' : 'serverpack');
            form.append('file', input.files[0]);
        }
        if (!confirm(`Update ${srv.name} to ${what}?\n\n${backup ? 'A backup is taken first. ' : 'No backup will be taken. '}The server stops while the pack's mods are swapped${srv.status === 'online' ? ' and starts again afterwards' : ''}. Players need the same version of the pack to join.`)) return;
        this.toggleConsole(true);
        const send = () => api(`/api/servers/${encodeURIComponent(srv.name)}/modpack/update`, { method: 'POST', body: form });
        const res = await this.withJava(await send(), send);
        if (!res.ok) alert(`The pack wasn't updated: ${await this.apiError(res)}`);
        else {
            const d = await res.json();
            alert(`${srv.name} now has ${d.pack.name}${d.pack.version ? ' ' + d.pack.version : ''}.`
                + (d.backup ? `\nThe backup from before is ${d.backup}.` : '') + ((d.notes || []).length ? '\n\n' + d.notes.join('\n') : ''));
        }
        await this.loadServers();
        this.loadModpack();
    },

    logInterval: null,

    // node: whose panel log to show (undefined = where the open server lives, null = this panel)
    logNode: null,

    toggleConsole(forceOpen = false, node) {
        const dock = document.getElementById('console-dock');
        if (forceOpen) {
            const wasOpen = this.consoleOpen();
            this.logNode = node === undefined ? (this.activeServer ? this.activeServer.node || null : null) : node;
            const title = document.getElementById('console-node');
            if (title) title.innerText = this.logNode ? `(${this.nodeName(this.logNode)})` : '';
            dock.classList.remove('translate-y-full');
            this.setConsoleSpace(true);
            if (!wasOpen) this.revealLastClick();
            this.startLogPolling();
        } else {
            dock.classList.add('translate-y-full');
            this.setConsoleSpace(false);
            this.stopLogPolling();
        }
    },

    consoleOpen() {
        return !document.getElementById('console-dock').classList.contains('translate-y-full');
    },

    // The page beside the sidebar ends above the open console (see the <style> in index.html)
    setConsoleSpace(open) {
        const dock = document.getElementById('console-dock');
        document.documentElement.style.setProperty('--console-space', open ? `${dock.offsetHeight}px` : '0px');
    },

    // When a click just opened the console (Back up now, Save, Start…), keep that control in
    // view: once the page has made room, scroll it up above the console if it ended up behind it.
    revealLastClick() {
        const c = this.lastClick;
        if (!c || Date.now() - c.at > 2000) return;
        const dock = document.getElementById('console-dock');
        let done = false;
        const reveal = () => {
            if (done) return;
            done = true;
            if (!c.el.isConnected) return;
            const r = c.el.getBoundingClientRect();
            // Where the console's top ends up (offsetHeight ignores the slide's transform)
            if (r.height && r.bottom > window.innerHeight - dock.offsetHeight)
                c.el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        };
        // Once the console has slid up and the page has made room for it (same timing)
        dock.addEventListener('transitionend', reveal, { once: true });
        setTimeout(reveal, 1000);   // in case no transition runs
    },

    startLogPolling() {
        if (this.logInterval) return;
        this.fetchLogs();
        this.logInterval = setInterval(() => this.fetchLogs(), 1000);
    },

    stopLogPolling() {
        clearInterval(this.logInterval);
        this.logInterval = null;
    },

    async fetchLogs() {
        try {
            const res = await api('/api/logs', { node: this.logNode });
            const data = await res.json();
            const out = document.getElementById('console-output');
            
            const isScrolledToBottom = out.scrollHeight - out.clientHeight <= out.scrollTop + 10;
            out.innerHTML = this.colorizeLog(data.content || '');
            if (isScrolledToBottom) {
                out.scrollTop = out.scrollHeight;
            }
        } catch (e) {
            console.error("Log fetch failed");
        }
    },

    // One <div> per log line, coloured by what kind of line it is
    colorizeLog(text) {
        const rules = [
            [/\[(ERROR|CRASH)\]|\bERROR\b|\bFATAL\b|Exception|Traceback|\bfailed\b/i, 'text-red-400'],
            [/\[WARNING\]|\bWARN(ING)?\b/i, 'text-yellow-300'],
            [/^\s*\[SYSTEM\] Executing:/, 'text-gray-500'],
            [/\[(BACKUP|RESTART|RESTORE|NOTIFY)\]/, 'text-sky-300'],
            [/\[(AUTH|SETTINGS|CLUSTER|USERS?|FILES)\]/, 'text-purple-300'],
            [/\bINFO\b|\bDone\b|\bSaved\b|\bsuccess/i, 'text-green-300'],
        ];
        return text.split('\n').map(line => {
            const rule = rules.find(([re]) => re.test(line));
            return `<div class="${rule ? rule[1] : 'text-gray-300'}">${this.escapeHtml(line) || '&nbsp;'}</div>`;
        }).join('');
    },

    async sendPower(action) {
        if (!this.activeServer) return;
        this.toggleConsole(true);
        await api(`/api/servers/${this.activeServer.name}/action`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action })
        });
        this.loadServers();
    },

    async updateServer() {
        const select = document.getElementById('update-version');
        const version = this.activeServer.edition === 'Java' && select ? select.value : '';
        const target = version ? `Minecraft ${version}` : 'the newest version';
        let warn = '';
        const backupBox = document.getElementById('update-backup');
        const backupFirst = !backupBox || backupBox.checked;
        const kind = this.addonKind(this.activeServer);
        if (version && version !== this.activeServer.minecraft && this.isJavaAddonKind(kind) && this.currentUser && this.currentUser.role === 'admin') {
            const r = await api(`/api/servers/${encodeURIComponent(this.activeServer.name)}/addons/compatibility?minecraft=${encodeURIComponent(version)}`);
            if (r.ok) {
                const c = await r.json();
                const noun = this.addonNoun(kind);
                if (c.missing.length) warn += `\n\nThese ${noun} have no build for ${version} yet, so the server may not start:\n- ` + c.missing.map(m => m.title).join('\n- ');
                if (c.unknown.length) warn += `\n\nThese ${noun} couldn't be checked (not found on Modrinth or Hangar):\n- ` + c.unknown.map(m => m.title).join('\n- ');
            }
        }
        const sure = this.activeServer.edition === 'Bedrock'
            ? `Update ${this.activeServer.name} to the newest Bedrock release? It stops, is backed up first ("Before update" on the Backups tab), and goes back to the old version and world if the new one doesn't start.`
            : `Update ${this.activeServer.name} to ${target}? The server restarts. ${backupFirst
                ? 'It is backed up first ("Before update" on the Backups tab), and Roll back the last update puts that world and the old version back.'
                : 'No backup is taken first: a world opened by a newer version can\'t go back.'}${warn}`;
        if (!confirm(sure)) return;
        this.toggleConsole(true);
        const name = this.activeServer.name;
        const send = () => api(`/api/servers/${encodeURIComponent(name)}/update`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(version ? { version, backup: backupFirst } : { backup: backupFirst })
        });
        const res = await this.withJava(await send(), send);
        if (!res.ok) alert(`Update failed: ${await this.apiError(res)}`);
        await this.loadServers();
        this.loadUpdateOptions();
        this.loadAutoUpdate();
        this.loadConvert();
        this.loadJavaChoice();
    },

    async uninstallServer(keepWorld) {
        if(confirm(`WARNING: ${keepWorld ? 'Uninstall' : 'Completely purge'} ${this.activeServer.name}?`)) {
            this.toggleConsole(true);
            const res = await api(`/api/servers/${this.activeServer.name}?keep_world=${keepWorld}`, { method: 'DELETE' });
            if (!res.ok) return alert(`Uninstall failed: ${await this.apiError(res)}`);
            this.activeServer = null;
            this.showDashboard();
            this.loadServers();
        }
    },
	// Add inside your ServerApp object:

	exportWorld() {
		if (!this.activeServer) return;
		// Direct browser navigation forces the browser to download the payload as a file
		window.location.href = apiUrl(`/api/servers/${encodeURIComponent(this.activeServer.name)}/worlds/export`);
	},

	currentWorldName: null,

	// Shows the active world (level-name in server.properties) above the rename box
	async loadWorldName() {
		const label = document.getElementById('current-world-name');
		if (!label || !this.activeServer) return;
		const serverName = this.activeServer.name;
		this.currentWorldName = null;
		label.innerText = 'Loading...';
		try {
			const res = await api(`/api/servers/${serverName}/properties`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const props = await res.json();
			if (!this.activeServer || this.activeServer.name !== serverName) return;
			// The server falls back to "world" when level-name is not set
			this.currentWorldName = props['level-name'] || 'world';
			label.innerText = this.currentWorldName;
		} catch (e) {
			label.innerText = 'Unknown (could not read server.properties)';
		}
	},

	async renameWorld() {
		const newNameInput = document.getElementById('new-world-name');
		const newName = newNameInput.value.trim();

		if (!newName) return alert("Please enter a new world name.");
		if (newName === this.currentWorldName) return alert("That is already the current world name.");

		const from = this.currentWorldName ? `from '${this.currentWorldName}' ` : '';
		if(confirm(`Are you sure you want to rename the world ${from}to '${newName}'? The server will temporarily stop.`)) {
			this.toggleConsole(true);
			const res = await api(`/api/servers/${this.activeServer.name}/worlds/rename`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ new_name: newName })
			});
			if (!res.ok) {
				let detail = `HTTP ${res.status}`;
				try { detail = (await res.json()).detail || detail; } catch (e) {}
				return alert(`World not renamed: ${detail}`);
			}
			newNameInput.value = '';
			alert("World renamed successfully.");
			this.loadWorldName();
			this.loadServers();
		}
	},

	// --- Signed-in user, sign out, panel user management ---
	renderCurrentUser() {
		const u = this.currentUser;
		if (!u) return;
		const trusted = u.source === 'trusted';   // on a trusted LAN, not signed in
		document.getElementById('current-user-name').innerText = trusted ? 'Local network' : u.username;
		document.getElementById('current-user-detail').innerText = trusted
			? 'Not signed in · user'
			: `${u.source === 'linux' ? 'Linux account' : 'Panel account'} · ${u.role}`;
		const signOut = document.getElementById('btn-sign-out');
		signOut.innerText = trusted ? 'Sign in' : 'Sign out';
		signOut.onclick = trusted ? () => window.location.assign('/static/login.html') : () => this.logout();
		document.getElementById('btn-manage-users').classList.toggle('hidden', u.role !== 'admin');
		document.getElementById('btn-panel-settings').classList.toggle('hidden', u.role !== 'admin');
		document.getElementById('btn-change-password').classList.toggle('hidden', trusted);
		// Uninstall and purge are admin-only (the API refuses them too)
		document.querySelectorAll('.admin-only').forEach(el => el.classList.toggle('hidden', u.role !== 'admin'));
		document.getElementById('uninstall-admin-note').classList.toggle('hidden', u.role === 'admin');
		const banner = document.getElementById('https-error-banner');
		banner.innerText = u.https_error ? `⚠️ ${u.https_error} Check Panel Settings.` : '';
		banner.classList.toggle('hidden', !u.https_error);
		// Clusters: a master gets the Cluster page (signed-in users only); a node shows who manages it
		const c = u.cluster || { role: 'standalone' };
		document.getElementById('btn-cluster').classList.toggle('hidden', c.role !== 'master' || trusted);
		const badge = document.getElementById('cluster-badge');
		badge.innerText = c.role === 'node' ? `Node of ${c.name} · managed by ${c.master_name}`
			: c.role === 'master' ? `Master of cluster ${c.name}` : '';
		badge.classList.toggle('hidden', c.role === 'standalone');
	},

	// --- 1.18: Panel Settings → SFTP (its own switch; saving it never restarts the panel) ---
	async loadSftpSettings(message, ok) {
		const box = document.getElementById('set-sftp');
		if (!box) return;
		const res = await fetch('/api/sftp');
		if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const d = this.sftpInfo = await res.json();
		const esc = t => this.escapeHtml(String(t ?? ''));
		const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const state = !d.available ? `<p class="text-sm text-yellow-700 dark:text-yellow-300 mb-2">SFTP needs the ${esc(d.package)} package. Install it on this server with <span class="font-mono">sudo apt install ${esc(d.package)}</span>, then turn it on here.</p>`
			: d.enabled ? `<p class="${note} mb-2">${d.running ? `<span class="text-green-600 dark:text-green-400 font-bold">Running</span> on port ${esc(d.port)}.` : '<span class="text-red-500 font-bold">Not running.</span> Check <span class="font-mono">systemctl status redstonepanel-sftp</span>.'}${d.host_key ? ` Host key: <span class="font-mono break-all">${esc(d.host_key)}</span>` : ''}</p>` : '';
		box.innerHTML = `
			<label class="flex items-start gap-2 mb-2 text-sm ${d.available ? 'cursor-pointer' : ''}">
				<input type="checkbox" id="set-sftp-on" ${d.enabled ? 'checked' : ''} ${d.available ? '' : 'disabled'} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">Let people move files with FileZilla or WinSCP (SFTP)</b><br><span class="${note}">They sign in as <span class="font-mono">account.server</span> (shown on each server's Files tab) with their panel password or an SSH key, and see only that server's folder. Admins reach every server; other accounts need <b>Files</b> on it (👥 Users). Accounts with two-factor must use a key.</span></span>
			</label>
			<div class="flex flex-wrap items-end gap-2 mb-2">
				<label class="${note}">Port<br><input id="set-sftp-port" type="number" min="1" max="65535" value="${esc(d.port)}" ${d.available ? '' : 'disabled'} class="${field} w-28 mt-1"></label>
				<button onclick="ServerApp.saveSftpSettings(this)" ${d.available ? '' : 'disabled'} class="px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold text-sm disabled:opacity-50">Save SFTP</button>
			</div>
			${state}
			<p id="set-sftp-msg" class="${message ? '' : 'hidden'} text-sm ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'} mb-1">${esc(message || '')}</p>
			<p class="${note}">The port is opened in this server's firewall (ufw). People outside your home also need it forwarded on your router.</p>`;
	},

	async saveSftpSettings(button) {
		button.disabled = true;
		const res = await fetch('/api/settings/sftp', {
			method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled: document.getElementById('set-sftp-on').checked,
			                       port: parseInt(document.getElementById('set-sftp-port').value, 10) || 0 })
		});
		if (!res.ok) return this.loadSftpSettings(await this.apiError(res), false);
		const d = await res.json();
		this.loadSftpSettings(d.enabled ? `SFTP is on, port ${d.port}.` : 'SFTP is off.', true);
	},

	// --- 1.21.10: Panel Settings → Terminal (its own switch; saving never restarts the panel) ---
	async loadTerminalSettings(message, ok) {
		const box = document.getElementById('set-terminal');
		if (!box) return;
		const [st, ses] = await Promise.all([fetch('/api/terminal'), fetch('/api/terminal/sessions')]);
		if (!st.ok) return;
		const d = await st.json();
		document.getElementById('set-terminal-wrap').classList.toggle('hidden', !d.supported);
		if (!d.supported) return;
		const s = this.settingsData || {};
		const sessions = ses.ok ? (await ses.json()).sessions : [];
		const esc = t => this.escapeHtml(String(t ?? ''));
		const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const node = this.currentUser && this.currentUser.cluster && this.currentUser.cluster.role === 'node';
		const check = (id, on, title, text) => `
			<label class="flex items-start gap-2 mb-2 text-sm cursor-pointer">
				<input type="checkbox" id="${id}" ${on ? 'checked' : ''} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">${title}</b><br><span class="${note}">${text}</span></span>
			</label>`;
		box.innerHTML = `
			${check('set-term-on', d.enabled, 'Let admins open a terminal on this machine',
				'A <b>⌨️ Terminal</b> button on the Dashboard. The shell runs as a Linux account, never as the panel itself: an admin who signed in with their Linux account gets that account; others type a Linux account and its password. Accounts with <span class="font-mono">nologin</span> (like <span class="font-mono">minecraft</span>) can\'t log in. Only over HTTPS. Helpers never get a terminal.')}
			${check('set-term-root', s.terminal_allow_root, 'Allow logging in as root', 'Off is safer: log in as yourself and use <span class="font-mono">sudo</span>.')}
			${node ? check('set-term-master', s.terminal_from_master, 'Allow a terminal from the cluster master',
				'Admins on the master can open a terminal here. They still need a Linux account and password on this machine; the password passes through the master.') : ''}
			<div class="flex flex-wrap items-end gap-2 mb-2">
				<label class="${note}">Close after (minutes with nothing typed)<br><input id="set-term-idle" type="number" min="5" max="120" value="${esc(s.terminal_idle_minutes ?? 15)}" class="${field} w-28 mt-1"></label>
				<label class="${note}">Skip the password after a Linux sign-in for (minutes, 0 = always ask)<br><input id="set-term-recent" type="number" min="0" max="60" value="${esc(s.terminal_recent_minutes ?? 15)}" class="${field} w-28 mt-1"></label>
				<button onclick="ServerApp.saveTerminalSettings(this)" class="px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold text-sm">Save terminal</button>
			</div>
			<p id="set-term-msg" class="${message ? '' : 'hidden'} text-sm ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'} mb-1">${esc(message || '')}</p>
			${d.enabled && !d.secure ? '<p class="text-sm text-yellow-700 dark:text-yellow-300 mb-2">The terminal needs HTTPS (Connection, below), so the passwords and everything shown can\'t be read on the network.</p>' : ''}
			<p class="${note}">Every terminal that opens or closes, and every wrong password, is written to the panel log. What is typed is never recorded. Five wrong passwords in 10 minutes block that account and address for 10 minutes.</p>
			${sessions.length ? `<div class="mt-3"><p class="${note} mb-1">Open now:</p>${sessions.map(x => `
				<div class="flex items-center justify-between gap-2 text-sm py-1 border-t border-gray-100 dark:border-gray-700">
					<span class="text-gray-900 dark:text-gray-200 min-w-0 break-words"><b>${esc(x.panel_user)}</b> as <span class="font-mono">${esc(x.account || '(signing in)')}</span> from ${esc(x.ip)}${x.via ? ` ${esc(x.via)}` : ''}, ${esc(this.fmtWhen(x.started))}</span>
					<button onclick="ServerApp.endTerminal(${this.jsArg(x.id)})" class="px-3 py-1 rounded text-xs font-bold text-white bg-red-600 hover:bg-red-700">End</button>
				</div>`).join('')}</div>` : ''}`;
	},

	async saveTerminalSettings(button) {
		button.disabled = true;
		const get = id => document.getElementById(id);
		const body = { terminal_enabled: get('set-term-on').checked, terminal_allow_root: get('set-term-root').checked,
			terminal_idle_minutes: parseInt(get('set-term-idle').value, 10) || 0,
			terminal_recent_minutes: parseInt(get('set-term-recent').value, 10) || 0 };
		if (get('set-term-master')) body.terminal_from_master = get('set-term-master').checked;
		const res = await fetch('/api/settings/terminal', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
		if (!res.ok) return this.loadTerminalSettings(await this.apiError(res), false);
		const d = await res.json();
		Object.assign(this.settingsData || {}, d);
		this.loadTerminalSettings(d.terminal_enabled ? 'The terminal is on.' : 'The terminal is off.', true);
		this.loadDashboard();
	},

	async endTerminal(id) {
		await fetch(`/api/terminal/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
		this.loadTerminalSettings();
	},

	// --- Panel Settings: where new game servers are kept (1.21.1) ---
	async loadServersFolder(message, ok, typed) {
		const box = document.getElementById('set-servers-folder');
		if (!box) return;
		const res = await fetch('/api/settings/servers-folder');
		if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const d = await res.json(), esc = t => this.escapeHtml(t);
		const gb = n => n == null ? '' : (n / 1e9 >= 100 ? Math.round(n / 1e9) : (n / 1e9).toFixed(1)) + ' GB';
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const others = d.folders.filter(f => !f.current && f.servers);
		// Windows: a list of this computer's drives; picking one fills in the folder the panel suggests there
		const drives = d.drives || [], shown = typed != null ? typed : (d.is_default ? '' : d.folder);
		const onDrive = ((shown || d.default).match(/^[A-Za-z]:/) || [''])[0].toUpperCase();
		this.serversDrives = drives;
		const driveList = drives.length ? `
				<select id="set-servers-folder-drive" aria-label="Drive" onchange="ServerApp.pickServersDrive(this.value)" class="${field}" style="flex:0 0 auto;width:auto;max-width:100%">
					${drives.some(v => v.drive === onDrive) ? '' : `<option value="" selected>${esc(onDrive || 'Drive')}</option>`}
					${drives.map(v => `<option value="${esc(v.drive)}" ${v.drive === onDrive ? 'selected' : ''}>${esc(v.drive)}${v.label ? ' ' + esc(v.label) : ''} (${gb(v.free)} free)</option>`).join('')}
				</select>` : '';
		box.innerHTML = `
			<div class="flex flex-col sm:flex-row gap-2 mb-1">${driveList}
				<input id="set-servers-folder-path" value="${esc(shown)}" placeholder="${esc(d.default)}" spellcheck="false" autocomplete="off" class="${field} font-mono">
				<button onclick="ServerApp.saveServersFolder(this)" class="px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold text-sm disabled:opacity-50 whitespace-nowrap">Save folder</button>
			</div>
			<p id="set-servers-folder-msg" class="${message ? '' : 'hidden'} text-sm ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'} mb-1">${esc(message || '')}</p>
			<p class="${note} mb-1">New servers are made in <b class="font-mono">${esc(d.folder)}</b>${d.folders[0] && d.folders[0].free != null ? ` (${gb(d.folders[0].free)} free of ${gb(d.folders[0].total)})` : ''}. ${drives.length ? `Choose a drive and the panel fills in <span class="font-mono">\\Minecraft\\servers</span> on it, the same path whatever the drive letter. You can change the path, or` : 'Give the full path of a folder on the disk you want them on;'} leave it empty for the usual place, <span class="font-mono">${esc(d.default)}</span>. The folder is made if it isn't there.</p>
			<p class="${note}">Servers you already have stay where they are and keep working.${others.length ? ' Still in use: ' + others.map(f => `<span class="font-mono">${esc(f.folder)}</span> (${f.servers} server${f.servers === 1 ? '' : 's'})`).join(', ') + '.' : ''}</p>`;
	},

	pickServersDrive(drive) {
		const chosen = (this.serversDrives || []).find(v => v.drive === drive);
		if (chosen) document.getElementById('set-servers-folder-path').value = chosen.suggested;
	},

	async saveServersFolder(button) {
		button.disabled = true;
		const typed = document.getElementById('set-servers-folder-path').value.trim();
		const res = await fetch('/api/settings/servers-folder', {
			method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder: typed })
		});
		if (!res.ok) return this.loadServersFolder(await this.apiError(res), false, typed);
		const d = await res.json();
		this.loadServersFolder(`New servers will be made in ${d.folder}.`, true);
	},

	// --- Panel Settings: connection (HTTP/HTTPS) and sign-in (admins only) ---
	async openPanelSettings(section) {
		const overlay = document.getElementById('settings-overlay');
		const box = document.getElementById('settings-content');
		box.innerHTML = '<p class="text-gray-500">Loading settings...</p>';
		overlay.classList.remove('hidden');
		const res = await fetch('/api/settings');
		if (!res.ok) { box.innerHTML = `<p class="text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const d = await res.json();
		const s = this.settingsData = d.settings, esc = t => this.escapeHtml(t);
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const radio = (name, value, current, title, desc) => `
			<label class="flex gap-3 p-3 rounded border border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-900">
				<input type="radio" name="${name}" value="${value}" ${current === value ? 'checked' : ''} onchange="ServerApp.updateSettingsForm()" class="mt-1">
				<span><span class="font-bold text-gray-900 dark:text-white">${title}</span><br><span class="text-xs text-gray-500 dark:text-gray-400">${desc}</span></span>
			</label>`;
		const now = d.runtime.scheme ? `${d.runtime.scheme.toUpperCase()} on port ${d.runtime.port}` : 'unknown (not started by run.py)';
		const clusterRole = ((this.currentUser || {}).cluster || {}).role || 'standalone';
		const ss = d.self_signed;

		box.innerHTML = `
			<div class="flex justify-between items-start mb-4">
				<div>
					<h3 class="text-xl font-bold text-gray-900 dark:text-white">Panel Settings</h3>
					<p class="text-xs text-gray-500 dark:text-gray-400">Currently serving: <b>${esc(now)}</b></p>
				</div>
				<button onclick="ServerApp.closePanelSettings()" class="text-gray-500 hover:text-gray-900 dark:hover:text-white text-xl leading-none">✕</button>
			</div>
			${d.runtime.https_error ? `<p class="text-sm p-2 mb-4 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200">⚠️ ${esc(d.runtime.https_error)}</p>` : ''}

			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Panel name</h4>
			<input id="set-panel-name" maxlength="40" value="${esc(s.panel_name)}" placeholder="${esc(d.defaults.panel_name)}" class="${field} mb-1">
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-6">Tells this server's panel apart from others: shown in the sidebar, the browser tab and on the sign-in page (visible before signing in). Leave empty to use the server's host name, <b>${esc(d.defaults.panel_name)}</b>.</p>

			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Setup guide</h4>
			${this.setupSettingsRow()}

			<h4 id="set-section-notifications" class="font-bold text-gray-900 dark:text-white mb-2">Notifications</h4>
			<div id="set-notifications" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>

			<div class="${this.notYet('offsite') ? 'hidden' : ''}">
			<h4 id="set-section-storage" class="font-bold text-gray-900 dark:text-white mb-2">Backup storage</h4>
			<div id="set-storage" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>
			</div>

			<h4 id="set-section-servers-folder" class="font-bold text-gray-900 dark:text-white mb-2">Servers folder</h4>
			<div id="set-servers-folder" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>

			<div class="${this.notYet('sftp') ? 'hidden' : ''}">
			<h4 id="set-section-sftp" class="font-bold text-gray-900 dark:text-white mb-2">SFTP</h4>
			<div id="set-sftp" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>
			</div>

			<div id="set-terminal-wrap" class="hidden">
			<h4 id="set-section-terminal" class="font-bold text-gray-900 dark:text-white mb-2">Terminal</h4>
			<div id="set-terminal" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>
			</div>

			<div class="${this.notYet('cluster') ? 'hidden' : ''}">
			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Cluster</h4>
			<div id="set-cluster" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>
			</div>
			${this.notYet('cluster') ? `<h4 class="font-bold text-gray-900 dark:text-white mb-2">Windows preview</h4>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-6">RedstonePanel on Windows is a preview: every kind of server on one computer. Not here yet: clusters and SFTP (both come next). The terminal is Linux-only: on Windows, open a command prompt on the computer itself.</p>` : ''}

			<h4 id="set-section-connection" class="font-bold text-gray-900 dark:text-white mb-2">Connection</h4>
			<div class="space-y-2 mb-3">
				${clusterRole === 'master'
					? `${radio('https_mode', 'cluster', s.https_mode, 'HTTPS from the cluster', 'The cluster CA\'s certificate. Browsers warn until each device imports the cluster CA.')}
				${radio('https_mode', 'custom', s.https_mode, 'HTTPS with my own certificate', 'For a master on the internet: a certbot (Let\'s Encrypt) certificate for its domain name. Browsers trust it as it is; nodes keep their cluster certificates on private addresses, because only the master talks to them.')}`
					: clusterRole === 'node'
					? radio('https_mode', s.https_mode, s.https_mode, s.https_mode === 'custom' ? 'HTTPS with my own certificate' : 'HTTPS from the cluster', 'This panel is a cluster node, so its HTTPS settings are fixed. Leave the cluster to change them.')
					: `${radio('https_mode', 'off', s.https_mode, 'HTTP (simple)', 'Unencrypted. Fine on a home network you trust.')}
				${radio('https_mode', 'self_signed', s.https_mode, 'HTTPS with a self-signed certificate', 'Encrypted. Browsers warn unless each device imports the panel\'s CA certificate.')}
				${radio('https_mode', 'custom', s.https_mode, 'HTTPS with my own certificate', 'For a real domain name, e.g. a Let\'s Encrypt certificate from certbot.')}`}
			</div>
			<div class="grid grid-cols-2 gap-3 mb-3">
				<label class="text-xs text-gray-500 dark:text-gray-400">HTTP port<input id="set-http-port" type="number" min="1" max="65535" value="${esc(s.http_port)}" class="${field} mt-1"></label>
				<label class="text-xs text-gray-500 dark:text-gray-400" id="set-https-port-wrap">HTTPS port<input id="set-https-port" type="number" min="1" max="65535" value="${esc(s.https_port)}" class="${field} mt-1"></label>
			</div>
			<p id="set-redirect-note" class="text-xs text-gray-500 dark:text-gray-400 mb-3">While HTTPS is on, the HTTP port only redirects to HTTPS.</p>
			<div id="set-custom-cert" class="grid grid-cols-1 gap-2 mb-3">
				<label class="text-xs text-gray-500 dark:text-gray-400">Certificate file (full chain, PEM)<input id="set-cert-file" value="${esc(s.cert_file)}" placeholder="/etc/letsencrypt/live/example.com/fullchain.pem" class="${field} mt-1 font-mono"></label>
				<label class="text-xs text-gray-500 dark:text-gray-400">Private key file (PEM)<input id="set-key-file" value="${esc(s.key_file)}" placeholder="/etc/letsencrypt/live/example.com/privkey.pem" class="${field} mt-1 font-mono"></label>
			</div>
			<div id="set-self-signed-info" class="text-xs text-gray-500 dark:text-gray-400 mb-3">
				${ss ? `CA certificate: expires ${esc(ss.expires || '?')} · SHA-256 <span class="font-mono break-all">${esc(ss.sha256 || '?')}</span> · <a href="/api/settings/ca.crt" class="underline text-theme">Download to import on your devices</a>`
				     : 'A certificate is created automatically when you save.'}
			</div>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-6">Publishing the panel on the internet? Put a reverse proxy (e.g. Caddy or nginx with a real domain) in front of it instead of forwarding the panel's port directly.</p>

			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Sign-in</h4>
			<div class="space-y-2 mb-3">
				${radio('login_mode', 'required', s.login_mode, 'Always required (recommended)', this.notYet('system-accounts') ? 'Everyone signs in with a panel account.' : 'Everyone signs in with a Linux or panel account.')}
				${radio('login_mode', 'trusted_networks', s.login_mode, 'Not required on the local network', 'Devices on the networks below can manage servers without signing in. Users and these settings still need an admin sign-in.')}
			</div>
			<label class="flex items-start gap-2 mb-3 text-sm cursor-pointer">
				<input type="checkbox" id="set-require-2fa" ${s.require_2fa_admins ? 'checked' : ''} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">Require two-factor for admins</b><br><span class="text-xs text-gray-500 dark:text-gray-400">Admins without it are sent to set it up (🔑 Password) right after signing in, before they can do anything else. Set it up for your own account first.</span></span>
			</label>
			<div id="set-trusted" class="mb-3">
				<label class="text-xs text-gray-500 dark:text-gray-400">Trusted networks (private ranges only, one per line)
					<textarea id="set-trusted-networks" rows="4" class="${field} mt-1 font-mono">${esc(s.trusted_networks.join('\n'))}</textarea></label>
				<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Your address right now: <b>${esc(d.your_ip || '?')}</b>. Default: all private ranges (${esc(d.defaults.trusted_networks.join(', '))}).</p>
				<label class="text-xs text-gray-500 dark:text-gray-400 block mt-3">Extra host names for this server (optional, one per line)
					<textarea id="set-extra-hosts" rows="2" placeholder="minecraft.lan" class="${field} mt-1 font-mono">${esc(s.extra_hosts.join('\n'))}</textarea></label>
				<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Without signing in, the panel only answers to its own IP addresses and host name. Add any other local name you use to reach it.</p>
				<p class="text-xs text-yellow-700 dark:text-yellow-300 mt-2">⚠️ Don't use this if the panel sits behind a reverse proxy on the same network: everyone coming through the proxy would look local. (Requests carrying proxy headers are never trusted.)</p>
			</div>

			<p id="set-error" class="hidden text-sm text-red-500 mb-3"></p>
			<div class="flex gap-2">
				<button onclick="ServerApp.closePanelSettings()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Cancel</button>
				<button id="set-save" onclick="ServerApp.savePanelSettings()" class="flex-1 px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold">Save</button>
			</div>`;
		this.updateSettingsForm();
		document.getElementById('set-https-port').disabled = clusterRole !== 'standalone';
		this.loadClusterSettings();
		this.loadNotificationSettings();
		this.storageEdit = null;
		this.loadStorageSettings();
		this.loadSetupSettings();
		this.loadSftpSettings();
		this.loadTerminalSettings();
		this.loadServersFolder();
		const target = section && document.getElementById(`set-section-${section}`);
		if (target) target.scrollIntoView({ block: 'start' });
	},

	async loadSetupSettings() {
		try {
			const r = await fetch('/api/settings/setup');
			if (r.ok) this.setupData = await r.json();
		} catch (e) { return; }
		const el = document.getElementById('set-setup-guide');
		if (el) el.outerHTML = this.setupSettingsRow();
	},

	async loadNotificationSettings() {
		const box = document.getElementById('set-notifications');
		if (!box) return;
		const res = await fetch('/api/settings/notifications');
		if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const d = await res.json();
		const esc = t => this.escapeHtml(String(t ?? ''));
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const small = 'text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-3 py-1.5 rounded font-bold transition';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const checks = Object.entries(d.event_names).map(([id, label]) => `
			<label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
				<input type="checkbox" name="notify-event" value="${esc(id)}" ${d.events.includes(id) ? 'checked' : ''}> ${esc(label)}
			</label>`).join('');
		box.innerHTML = `
			<p class="${note} mb-2">Send a message to a Discord channel, or any web address that takes a JSON POST, when something happens to a server. Crashes are reported even while the panel itself is stopped.</p>
			<p class="text-sm mb-2 text-gray-700 dark:text-gray-300">${d.configured
				? `Webhook: <span class="font-mono">${esc(d.url_hint)}</span> (${d.kind === 'discord' ? 'Discord' : 'JSON'})`
				: 'No webhook set.'}</p>
			<input id="notify-url" type="password" autocomplete="off" placeholder="${d.configured ? 'Paste a new URL to replace it' : 'https://discord.com/api/webhooks/…'}" class="${field} mb-1">
			<p class="${note} mb-3">In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Anyone with the URL can post in the channel, so the panel never shows it again.</p>
			<div class="grid grid-cols-2 sm:grid-cols-3 gap-2 mb-3">${checks}</div>
			<p id="notify-msg" class="hidden text-sm mb-2"></p>
			<div class="flex flex-wrap gap-2">
				<button onclick="ServerApp.saveNotifications()" class="${small}">Save notifications</button>
				${d.configured ? `<button onclick="ServerApp.testNotifications(this)" class="${small}">Send a test</button>
				<button onclick="ServerApp.saveNotifications(true)" class="${small}">Remove webhook</button>` : ''}
			</div>`;
	},

	notifyMessage(text, ok) {
		const el = document.getElementById('notify-msg');
		el.innerText = text;
		el.className = `text-sm mb-2 ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`;
	},

	async saveNotifications(clear = false) {
		const url = document.getElementById('notify-url').value.trim();
		const events = [...document.querySelectorAll('input[name="notify-event"]:checked')].map(e => e.value);
		const res = await fetch('/api/settings/notifications', {
			method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(clear ? { clear: true } : { url: url || null, events })
		});
		if (!res.ok) return this.notifyMessage(await this.apiError(res), false);
		await this.loadNotificationSettings();
		this.notifyMessage(clear ? 'Webhook removed.' : 'Saved.', true);
	},

	async testNotifications(button) {
		button.disabled = true;
		const res = await fetch('/api/settings/notifications/test', { method: 'POST' });
		button.disabled = false;
		this.notifyMessage(res.ok ? 'Test message sent. Check your channel.' : await this.apiError(res), res.ok);
	},

	// Show only the fields that apply to the chosen options
	updateSettingsForm() {
		const mode = (document.querySelector('input[name="https_mode"]:checked') || {}).value;
		const login = (document.querySelector('input[name="login_mode"]:checked') || {}).value;
		document.getElementById('set-https-port-wrap').classList.toggle('hidden', mode === 'off');
		document.getElementById('set-redirect-note').classList.toggle('hidden', mode === 'off');
		document.getElementById('set-custom-cert').classList.toggle('hidden', mode !== 'custom');
		document.getElementById('set-self-signed-info').classList.toggle('hidden', mode !== 'self_signed');
		document.getElementById('set-trusted').classList.toggle('hidden', login !== 'trusted_networks');
	},

	closePanelSettings() {
		document.getElementById('settings-overlay').classList.add('hidden');
	},

	async savePanelSettings() {
		const val = id => document.getElementById(id).value;
		const lines = id => val(id).split(/[\s,]+/).map(x => x.trim()).filter(Boolean);
		const body = {
			https_mode: document.querySelector('input[name="https_mode"]:checked').value,
			http_port: Number(val('set-http-port')),
			https_port: Number(val('set-https-port')),
			cert_file: val('set-cert-file'),
			key_file: val('set-key-file'),
			login_mode: document.querySelector('input[name="login_mode"]:checked').value,
			require_2fa_admins: document.getElementById('set-require-2fa').checked,
			trusted_networks: lines('set-trusted-networks'),
			extra_hosts: lines('set-extra-hosts'),
			panel_name: val('set-panel-name'),
		};
		const err = document.getElementById('set-error');
		const btn = document.getElementById('set-save');
		err.classList.add('hidden');
		btn.disabled = true;
		btn.innerText = 'Saving...';
		const res = await fetch('/api/settings', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body)
		});
		if (!res.ok) {
			err.innerText = await this.apiError(res);
			err.classList.remove('hidden');
			btn.disabled = false;
			btn.innerText = 'Save';
			return;
		}
		const data = await res.json();
		this.showPanelName(data.name);
		if (!data.restart) {
			this.closePanelSettings();
			alert('Settings saved.');
			return;
		}
		this.showRestarting(data.url, body.https_mode === 'self_signed'
			? 'Your browser will warn about the self-signed certificate; that is expected.' : '');
	},

	// The panel restarts on its new address; give it a moment, then go there
	showRestarting(url, note) {
		const target = `${url}/static/index.html`;
		document.getElementById('settings-overlay').classList.remove('hidden');
		document.getElementById('settings-content').innerHTML = `
			<h3 class="text-xl font-bold mb-2 text-gray-900 dark:text-white">Restarting the panel...</h3>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-2">New address: <a class="underline text-theme font-mono" href="${this.escapeHtml(target)}">${this.escapeHtml(url)}</a></p>
			<p class="text-sm text-gray-500 dark:text-gray-400">You'll be taken there in a few seconds. ${this.escapeHtml(note || '')} Sign in again if asked.</p>`;
		setTimeout(() => window.location.assign(target), 4000);
	},

	// --- Dashboard (no server selected): this machine, its game servers and supporting services ---
	dashTimer: null,
	dashData: null,
	dashHosts: null,
	setupData: null,

	showDashboard() {
		if (this.fileEditor && this.fileEditor.dirty
			&& !confirm('Discard your unsaved changes to ' + this.fileEditor.path + '?')) return;
		this.fileEditor = null;
		this.activeServer = null;
		this.renderSidebar();
		this.closeCluster();
		document.getElementById('workspace').classList.add('hidden');
		document.getElementById('dashboard').classList.remove('hidden');
		this.setMobileTitle('📊 Dashboard', this.panelName);
		this.loadDashboard();
		clearInterval(this.dashTimer);
		this.dashTimer = setInterval(() => { if (!document.hidden) this.loadDashboard(); }, 10000);
	},

	hideDashboard() {
		this.closePorts();
		clearInterval(this.dashTimer);
		this.dashTimer = null;
		document.getElementById('dashboard').classList.add('hidden');
	},

	async loadDashboard() {
		const body = document.getElementById('dash-body');
		try {
			const res = await fetch('/api/host');
			if (!res.ok) throw new Error(await this.apiError(res));
			this.dashData = await res.json();
		} catch (e) {
			if (!this.dashData) body.innerHTML = `<p class="text-red-500">Couldn't load the dashboard: ${this.escapeHtml(e.message)}</p>`;
			return;
		}
		const cl = this.dashData.services.cluster;
		if (cl && cl.role === 'master') {
			try {
				const r = await fetch('/api/cluster/hosts');
				this.dashHosts = r.ok ? await r.json() : null;
			} catch (e) { /* keep the last answer */ }
		} else {
			this.dashHosts = null;
		}
		if (this.isAdmin() && !this.dashData.redacted) {
			try {
				const r = await fetch('/api/settings/setup');
				this.setupData = r.ok ? await r.json() : null;
			} catch (e) { /* keep the last answer */ }
		} else {
			this.setupData = null;
		}
		if (this.isAdmin()) {
			try {
				const r = await fetch('/api/terminal');
				this.terminalInfo = r.ok ? await r.json() : null;
			} catch (e) { /* keep the last answer */ }
		} else {
			this.terminalInfo = null;
		}
		await this.loadHostHistory();
		if (!document.getElementById('dashboard').classList.contains('hidden')) this.renderDashboard();
	},

	// --- 1.21.10: the terminal (static/js/terminal.js). Admins only, on a Linux machine, when on. ---
	openTerminal(nodeId, nodeName) {
		const t = this.terminalInfo || {};
		const name = nodeName || (this.dashData && this.dashData.host && (this.dashData.host.name || this.dashData.host.hostname));
		RPTerminal.start({ node: nodeId || '', name, idleMinutes: t.idle_minutes });
	},

	canOpenNodeTerminal(n) {
		const t = n.local ? (this.terminalInfo && this.terminalInfo.available)
			: n.terminal && n.terminal.supported && n.terminal.enabled && n.terminal.from_master;
		return !!(this.isAdmin() && n.state === 'ok' && t && (n.local || (n.cluster_api || 1) >= 8)
			&& this.terminalInfo && this.terminalInfo.secure);
	},

	// Formatting helpers
	fmtBytes(n) {
		if (n === null || n === undefined) return '—';
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		let i = 0;
		while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
		return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : 1)) + ' ' + units[i];
	},
	fmtRate(n) { return n === null || n === undefined ? '—' : this.fmtBytes(n) + '/s'; },
	fmtDuration(s) {
		if (s === null || s === undefined) return '—';
		s = Math.floor(s);
		const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
		if (d) return `${d} d ${h} h`;
		if (h) return `${h} h ${m} min`;
		return m ? `${m} min` : `${s} s`;
	},
	fmtWhen(t) {
		if (!t) return '—';
		const diff = t - Date.now() / 1000;
		if (Math.abs(diff) > 7 * 86400) return (diff >= 0 ? 'on ' : 'since ') + new Date(t * 1000).toLocaleDateString();
		const rel = this.fmtDuration(Math.abs(diff));
		return diff >= 0 ? `in ${rel}` : `${rel} ago`;
	},
	fmtPct(p) { return p === null || p === undefined ? '—' : `${Math.round(p)}%`; },
	barColor(p) { return p >= 90 ? 'rp-level rp-level-high' : p >= 75 ? 'rp-level rp-level-warn' : 'rp-level rp-level-ok'; },
	bar(p) {
		const w = Math.max(0, Math.min(100, p || 0));
		return `<div class="w-full h-2 rounded bg-gray-200 dark:bg-gray-700"><div class="h-2 rounded ${this.barColor(w)}" style="width:${w}%"></div></div>`;
	},

	openServerByName(name, tab) {
		const srv = this.servers.find(s => s.name === name);
		if (!srv) return;
		this.openWorkspace(srv);
		if (tab === 'access') {
			this.switchTab('properties');
			this.switchConfigSubTab('access');
		} else if (tab) this.switchTab(tab);
	},

	renderDashboard() {
		const d = this.dashData, esc = t => this.escapeHtml(t === null || t === undefined ? '' : t);
		const card = 'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow';
		const h3 = 'font-bold text-gray-900 dark:text-white';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const row = (k, v) => `<div class="flex justify-between gap-3 py-1 border-t border-gray-100 dark:border-gray-700 first:border-t-0"><span class="text-gray-500 dark:text-gray-400">${k}</span><span class="text-right text-gray-900 dark:text-gray-200 min-w-0 break-words">${v}</span></div>`;
		const svc = d.services || {};
		const hostLine = [d.host.os, d.host.uptime ? `up ${this.fmtDuration(d.host.uptime)}` : null].filter(Boolean).join(' · ');
		document.getElementById('dash-title').innerText = d.host.name || d.host.hostname;
		document.getElementById('btn-terminal').classList.toggle('hidden', !(this.terminalInfo && this.terminalInfo.available));
		document.getElementById('dash-subtitle').innerText = `${hostLine}${hostLine ? ' · ' : ''}refreshes every 10 seconds`;

		// Needs attention
		const icon = { error: '⛔', warn: '⚠️', info: 'ℹ️' };
		const tone = { error: 'border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/30',
			warn: 'border-yellow-300 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-900/30',
			info: 'border-blue-300 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30' };
		const nodeIssues = (this.dashHosts ? this.dashHosts.nodes : []).filter(n => !n.local && n.state !== 'ok')
			.map(n => ({ level: 'warn', text: `Cluster node ${n.name}: ${this.clusterStates[n.state] || n.state}` }));
		const attention = (d.attention || []).concat(nodeIssues);
		const attentionHtml = attention.length ? `
			<section class="${card} p-4">
				<h3 class="${h3} mb-2">Needs attention</h3>
				<div class="space-y-2">${attention.map(a => {
					const tab = a.where === 'backups' ? 'backups' : a.where === 'access' ? 'access' : '';
					const link = a.server ? ` <button onclick="ServerApp.openServerByName(${this.jsArg(a.server)}, ${this.jsArg(tab)})" class="underline text-theme whitespace-nowrap">Open ${esc(a.server)}</button>`
						: a.where === 'update' && this.isAdmin() ? ` <button onclick="ServerApp.startPanelUpdate()" class="underline text-theme whitespace-nowrap">Update now</button>` : '';
					return `<div class="text-sm p-2 rounded border ${tone[a.level] || tone.warn} text-gray-900 dark:text-gray-100">${icon[a.level] || '⚠️'} ${esc(a.text)}${link}</div>`;
				}).join('')}</div>
			</section>` : `
			<section class="${card} p-4 text-sm text-gray-700 dark:text-gray-300">✅ Nothing needs attention.</section>`;

		// Tiles
		const servers = d.servers || [];
		const srvDisk = (d.disks || []).find(x => x.holds.includes('Game servers')) || (d.disks || [])[0];
		const net = d.network || [];
		const rx = net.reduce((a, n) => a + n.rx, 0), tx = net.reduce((a, n) => a + n.tx, 0);
		const m = d.memory;
		const tile = (title, big, barP, lines) => `
			<div class="${card} p-4">
				<p class="${note} uppercase tracking-wider font-bold">${title}</p>
				<p class="text-2xl font-bold text-gray-900 dark:text-white my-1">${big}</p>
				${barP === null ? '' : this.bar(barP)}
				<p class="${note} mt-2">${lines}</p>
			</div>`;
		const tiles = `
			<section class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
				${tile('CPU', this.fmtPct(d.cpu.percent), d.cpu.percent, `${d.cpu.cores || '?'} cores · load ${d.cpu.load ? d.cpu.load.join(' ') : '—'}${d.cpu.model ? `<br>${esc(d.cpu.model)}` : ''}`)}
				${tile('Memory', `${this.fmtBytes(m.used)} <span class="text-sm font-normal text-gray-500">of ${this.fmtBytes(m.total)}</span>`, m.percent,
					`${this.fmtBytes(m.available)} available · swap ${m.swap_total ? `${this.fmtBytes(m.swap_used)} of ${this.fmtBytes(m.swap_total)}` : 'none'}`)}
				${srvDisk ? tile('Disk', `${this.fmtBytes(srvDisk.free)} <span class="text-sm font-normal text-gray-500">free</span>`, srvDisk.percent,
					`${this.fmtPct(srvDisk.percent)} of ${this.fmtBytes(srvDisk.total)} used on ${esc(srvDisk.mount)}`) : ''}
				${tile('Game servers', `${servers.filter(s => s.status === 'online').length} <span class="text-sm font-normal text-gray-500">of ${servers.length} online</span>`, null,
					`${servers.reduce((a, s) => a + (s.players || 0), 0)} players online (Java, over RCON) · network ↓ ${this.fmtRate(rx)} ↑ ${this.fmtRate(tx)}`)}
			</section>`;

		// Game servers
		const th = 'text-left font-bold px-3 py-2 whitespace-nowrap';
		const td = 'px-3 py-2 whitespace-nowrap';
		const serverRows = servers.map(s => {
			const crash = s.crash_loop;
			const dot = s.status === 'online' ? 'rp-lamp-on' : crash || s.state === 'failed' ? 'text-red-500' : 'text-gray-400';
			const stateText = crash ? 'crash loop' : s.state === 'failed' ? 'failed' : s.status;
			const lb = s.last_backup;
			const backupNote = lb ? (lb.ok ? `last ${this.fmtWhen(lb.at)}` : '<span class="text-red-500">last failed</span>') : 'none yet';
			return `
				<tr class="border-t border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer" onclick="ServerApp.openServerByName(${this.jsArg(s.name)})">
					<td class="${td}"><span class="${dot}">●</span> <b class="text-gray-900 dark:text-white">${esc(s.name)}</b><div class="${note}">${esc(s.edition)} ${esc(s.version || '')} · port ${esc(s.port)}${this.updateBadge(s)}</div></td>
					<td class="${td}" data-label="State">${esc(stateText)}<span class="${note} md:block">${s.uptime !== null ? ' up ' + this.fmtDuration(s.uptime) : ''}</span></td>
					<td class="${td}" data-label="CPU">${this.fmtPct(s.cpu_percent)}</td>
					<td class="${td}" data-label="Memory">${this.fmtBytes(s.memory)}<span class="${note} md:block">${s.heap && s.heap !== 'N/A' ? ' max heap ' + esc(s.heap) : ''}</span></td>
					<td class="${td}" data-label="Players">${s.players !== null ? `${s.players} / ${s.max_players}` : '—'}</td>
					<td class="${td}" data-label="TPS">${s.tps !== null && s.tps !== undefined ? `<span class="${this.tpsState(s.tps).cls}">${this.tpsState(s.tps).icon}</span> ${s.tps}` : '—'}</td>
					<td class="${td}" data-label="Folder">${s.folder_size !== null ? this.fmtBytes(s.folder_size) : '<span class="' + note + '">measuring…</span>'}</td>
					<td class="${td}" data-label="Backups">${s.backups ? `${s.backups} · ${this.fmtBytes(s.backups_size)}` : '—'}<span class="${note} md:block"> ${backupNote}</span></td>
					<td class="${td}" data-label="Next backup">${s.next_backup ? this.fmtWhen(s.next_backup) : '—'}</td>
					<td class="${td}" data-label="Next restart">${s.next_restart ? this.fmtWhen(s.next_restart) : '—'}</td>
				</tr>`;
		}).join('');
		const serverTable = `
			<section class="${card}">
				<h3 class="${h3} p-4 pb-2">Game servers</h3>
				${servers.length ? `<div class="overflow-x-auto"><table class="rp-cards w-full text-sm text-gray-700 dark:text-gray-300">
					<thead class="text-xs text-gray-500 dark:text-gray-400"><tr>
						<th class="${th}">Server</th><th class="${th}">State</th><th class="${th}">CPU</th><th class="${th}">Memory</th>
						<th class="${th}">Players</th><th class="${th}">TPS</th><th class="${th}">Folder</th><th class="${th}">Backups</th>
						<th class="${th}">Next backup</th><th class="${th}">Next restart</th>
					</tr></thead><tbody>${serverRows}</tbody></table></div>
					<p class="${note} px-4 py-2">CPU is a share of the whole machine. Folder sizes are measured in the background every 10 minutes.</p>`
				: `<p class="px-4 pb-4 text-sm text-gray-500 dark:text-gray-400">No Minecraft servers yet. Use ➕ New Server to make one.</p>`}
			</section>`;

		// Host, disks, services
		const host = `
			<section class="${card} p-4 text-sm">
				<h3 class="${h3} mb-2">Host</h3>
				${row('Host name', esc(d.host.hostname))}
				${d.host.os ? row('System', esc(d.host.os)) : ''}
				${d.host.kernel ? row('Kernel', esc(d.host.kernel)) : ''}
				${row('Up', this.fmtDuration(d.host.uptime))}
				${row('Time zone', esc(d.host.timezone))}
				${d.host.ips ? row('Addresses', d.host.ips.map(esc).join('<br>') || '—') : ''}
				${d.host.version ? row('RedstonePanel', esc(d.host.version) + (d.host.update ? ` <span class="text-blue-500">(${esc(d.host.update)} available)</span>` : '')
					+ (this.isAdmin() && !d.redacted ? (d.host.update
						? ` <button onclick="ServerApp.startPanelUpdate()" class="ml-1 px-2 py-0.5 rounded text-xs bg-theme hover-bg-theme text-white font-bold">Update now</button>`
						: `<br><button onclick="ServerApp.checkPanelUpdate(this)" class="underline text-xs text-theme">Check for updates</button>`) : '')) : ''}
				${net.length ? row('Network', net.map(n => `${esc(n.name)} ↓ ${this.fmtRate(n.rx)} ↑ ${this.fmtRate(n.tx)}`).join('<br>')) : ''}
			</section>`;
		const disks = `
			<section class="${card} p-4 text-sm">
				<h3 class="${h3} mb-2">Disks</h3>
				${(d.disks || []).map(x => `
					<div class="mb-3">
						<div class="flex justify-between gap-2"><span class="font-bold text-gray-900 dark:text-gray-200">${esc(x.mount)}</span><span>${this.fmtBytes(x.free)} free of ${this.fmtBytes(x.total)}</span></div>
						${this.bar(x.percent)}
						<p class="${note} mt-1">${esc(x.holds.join(', '))}${x.fstype ? ' · ' + esc(x.fstype) : ''}</p>
					</div>`).join('')}
				${servers.length ? `<p class="${note}">Game servers: ${this.fmtBytes(servers.reduce((a, s) => a + (s.folder_size || 0), 0))} · backups: ${this.fmtBytes(servers.reduce((a, s) => a + (s.backups_size || 0), 0))}</p>` : ''}
			</section>`;
		const ok = (good, text) => `<span class="${good ? 'text-green-600 dark:text-green-400' : 'text-yellow-600 dark:text-yellow-400'}">${good ? '●' : '▲'}</span> ${text}`;
		const p = svc.panel || {};
		const fw = svc.firewall, https = svc.https, notif = svc.notifications, cl = svc.cluster;
		const timers = servers.filter(s => s.next_backup || s.next_restart);
		const services = `
			<section class="${card} p-4 text-sm">
				<h3 class="${h3} mb-2">Supporting services</h3>
				${row('RedstonePanel', ok(p.state === 'active', `${esc(p.state)}${p.uptime ? ', up ' + this.fmtDuration(p.uptime) : ''}${p.memory ? ' · ' + this.fmtBytes(p.memory) : ''}`))}
				${row('Failed units', svc.failed_units && svc.failed_units.length ? ok(false, svc.failed_units.map(esc).join('<br>')) : ok(true, 'none'))}
				${svc.java ? row('Java', this.javaRow(svc, servers, d.redacted)) : ''}
				${row('screen', ok(svc.screen, svc.screen ? 'installed' : 'not installed (Bedrock chat and backups need it)'))}
				${fw ? row('Firewall (ufw)', !fw.installed ? 'not installed' : ok(true, fw.active ? 'on' : 'off')) : ''}
				${svc.phantoms && svc.phantoms.length ? row('Phantom proxies', svc.phantoms.map(x => ok(x.active === 'active', `${esc(x.unit)}: ${esc(x.active)}`)).join('<br>')) : ''}
				${row('Backup / restart timers', timers.length ? `${servers.filter(s => s.next_backup).length} backup, ${servers.filter(s => s.next_restart).length} restart` : 'none scheduled')}
				${https ? row('HTTPS', https.mode === 'off' ? 'off' : ok(!https.error, `${esc(https.mode.replace('_', '-'))}${https.cert_expires ? ', certificate expires ' + this.fmtWhen(https.cert_expires) : ''}`)) : ''}
				${notif ? row('Notifications', notif.configured ? ok(true, esc(notif.kind || 'webhook')) : 'not set up') : ''}
				${cl ? row('Cluster', cl.role === 'standalone' ? 'not in a cluster' : `${esc(cl.role)} of ${esc(cl.name || '?')}${cl.role === 'master' ? ` · ${cl.nodes} node${cl.nodes === 1 ? '' : 's'}` : ''}${cl.cert_expires ? `, certificate expires ${this.fmtWhen(cl.cert_expires)}` : ''}`) : ''}
			</section>`;

		// Cluster nodes (master)
		let nodes = '';
		if (this.dashHosts) {
			nodes = `
				<section>
					<h3 class="${h3} mb-2">Cluster: ${esc(this.dashHosts.cluster)}</h3>
					<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">${this.dashHosts.nodes.map(n => this.nodeCard(n)).join('')}</div>
				</section>`;
		}

		// 1.18: an account limited to some servers sees those, and a note when it has none yet
		if (d.limited && !this.servers.length) {
			document.getElementById('dash-body').innerHTML = `
				<section class="${card} p-6 text-sm text-gray-700 dark:text-gray-300">
					<h3 class="${h3} mb-1">No servers yet</h3>
					<p>Your account can't use any server on this panel yet. Ask an admin to give it access (👥 Users → Servers).</p>
				</section>`;
			return;
		}
		document.getElementById('dash-body').innerHTML = this.setupCard() + attentionHtml + tiles + this.hostHistoryHtml() + serverTable
			+ `<section class="grid grid-cols-1 lg:grid-cols-3 gap-4">${host}${disks}${d.limited ? '' : services}</section>` + nodes
			+ (d.redacted ? `<p class="${note}">You're on the trusted local network without signing in, so addresses, versions and settings are hidden.</p>` : '');
	},

	// Supporting services → Java: each installed Java and the servers on it, what's missing,
	// and (admins) install / remove
	javaRow(svc, servers, redacted) {
		const esc = t => this.escapeHtml(t === null || t === undefined ? '' : String(t));
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const link = 'underline text-xs text-theme';
		const admin = this.isAdmin() && !redacted;
		const job = (svc.java_job && svc.java_job.state === 'running') ? svc.java_job : this.javaJob;
		const lines = svc.java.map(j => {
			const users = j.servers.length ? `<span class="${note}">${j.servers.map(esc).join(', ')}</span>`
				: `<span class="${note} italic">no server</span>`;
			const remove = admin && !job && j.removable
				? ` <button onclick="ServerApp.removeJava(${j.major})" class="underline text-xs text-red-600 dark:text-red-400">Remove</button>` : '';
			return `Java ${esc(j.major)}${j.default ? ` <span class="${note}">(default)</span>` : ''}: ${users}${remove}`;
		});
		for (const n of svc.java_needed || []) {
			const install = admin && !job ? ` <button onclick="ServerApp.installJava(${n.major})" class="${link}">Install</button>` : '';
			lines.push(`<span class="text-red-600 dark:text-red-400">▲</span> Java ${esc(n.major)} needed by ${n.servers.map(esc).join(', ')}${install}`);
		}
		if (job) lines.push(`⏳ ${job.action === 'install' ? 'Installing' : 'Removing'} Java ${esc(job.major)}…`);
		if (!svc.java.length && !(svc.java_needed || []).length) {
			lines.push(servers.some(s => s.edition === 'Java') ? 'not installed' : `none <span class="${note}">(installed when a Java server needs it)</span>`);
		}
		if (admin && !job && (svc.java_installable || []).length) {
			lines.push(`<span class="${note}">Install: ${svc.java_installable.map(i => `<button onclick="ServerApp.installJava(${i.major})" class="${link}">${esc(i.major)}</button>`).join(' · ')}</span>`);
		}
		return lines.join('<br>');
	},

	// --- Updating RedstonePanel itself (admins). The upgrade runs in its own systemd unit
	// because the package restarts the panel; this page waits for it to come back, then reloads.
	async checkPanelUpdate(button) {
		if (button) { button.disabled = true; button.innerText = 'Checking…'; }
		const res = await fetch('/api/panel/update/check', { method: 'POST' });
		if (!res.ok) {
			alert(`Couldn't check for updates: ${await this.apiError(res)}`);
		} else {
			const st = await res.json();
			alert(st.available ? `RedstonePanel ${st.available} is available.` : `RedstonePanel ${st.version} is the newest version.`);
		}
		await this.loadDashboard();
	},

	async startPanelUpdate() {
		const h = (this.dashData && this.dashData.host) || {};
		if (!confirm(`Update RedstonePanel ${h.version || ''} to ${h.update || 'the newest version'}?\n\n`
			+ `The panel restarts during the update; this page reconnects and reloads by itself, usually within a minute. `
			+ `Game servers keep running.`)) return;
		const res = await fetch('/api/panel/update', { method: 'POST' });
		if (!res.ok) return alert(`The update didn't start: ${await this.apiError(res)}`);
		const st = await res.json();
		await this.watchPanelUpdate((st.upgrade && st.upgrade.to) || h.update);
	},

	// A box over the page with the update's progress and apt's last lines
	panelUpdateBox(message, lines, closable) {
		let el = document.getElementById('panel-update-overlay');
		if (!el) {
			el = document.createElement('div');
			el.id = 'panel-update-overlay';
			el.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4';
			document.body.appendChild(el);
		}
		const esc = t => this.escapeHtml(String(t));
		el.innerHTML = `
			<div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full p-5 text-sm text-gray-800 dark:text-gray-200">
				<h3 class="font-bold text-lg mb-2 text-gray-900 dark:text-white">Updating RedstonePanel</h3>
				<p class="mb-3">${closable ? '' : '⏳ '}${esc(message)}</p>
				${lines && lines.length ? `<pre class="text-xs bg-gray-100 dark:bg-gray-900 rounded p-2 max-h-64 overflow-auto whitespace-pre-wrap break-all">${lines.map(esc).join('\n')}</pre>` : ''}
				${closable ? `<div class="mt-3 text-right"><button onclick="document.getElementById('panel-update-overlay').remove()" class="bg-theme hover-bg-theme text-white px-4 py-2 rounded font-bold">Close</button></div>` : ''}
			</div>`;
	},

	async watchPanelUpdate(target) {
		const started = Date.now();
		let lines = [];
		let wentAway = false;
		this.panelUpdateBox(`Updating to ${target || 'the newest version'}…`, lines);
		while (Date.now() - started < 15 * 60 * 1000) {
			await new Promise(r => setTimeout(r, 2000));
			let st = null;
			try {
				const r = await fetch('/api/panel/update', { cache: 'no-store' });
				if (r.ok) st = await r.json();
			} catch (e) { /* restarting */ }
			if (!st) {
				wentAway = true;
				this.panelUpdateBox('The panel is restarting with the new version…', lines);
				continue;
			}
			const u = st.upgrade || {};
			lines = u.lines || lines;
			if (u.state === 'failed') {
				this.panelUpdateBox(`The update failed: ${u.error || 'see the log below'}. RedstonePanel ${st.version} is still running.`, lines, true);
				return;
			}
			if (u.state === 'done' && st.version === (u.to || target)) {
				this.panelUpdateBox(`Updated to ${st.version}. Reloading…`, lines);
				setTimeout(() => location.reload(), 1500);
				return;
			}
			this.panelUpdateBox(wentAway ? 'Finishing the update…' : `Updating ${u.from || ''} → ${u.to || target}…`, lines);
		}
		this.panelUpdateBox('The update is taking longer than expected. Reload this page later, or check with: sudo redstonepanel status', lines, true);
	},

	// First-run setup guide (admins): four steps, each ticked from the panel's real state
	// (GET /api/settings/setup) and each opening the dialog that already does the job
	setupCard() {
		const g = this.setupData;
		if (!g || g.dismissed) return '';
		const esc = t => this.escapeHtml(t === null || t === undefined ? '' : String(t));
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const btn = 'px-3 py-1 rounded text-xs bg-theme hover-bg-theme text-white font-bold';
		const link = 'underline text-xs text-theme';
		const step = Object.fromEntries(g.steps.map(x => [x.id, { ...x }]));
		// On a cluster master, servers on the nodes count too; each node's own guide covers their backups
		const nodeServers = (this.dashHosts ? this.dashHosts.nodes : []).filter(n => !n.local && n.summary)
			.reduce((a, n) => a + (n.summary.servers_total || 0), 0);
		if (!step.server.servers && nodeServers) {
			step.server.done = true;
			step.backups.done = true;
			step.backups.onNodes = true;
		}
		const done = Object.values(step).filter(x => x.done).length;
		if (done === g.total) return '';

		const fw = step.server.firewall;
		const unscheduled = step.backups.unscheduled || [];
		const plural = n => n === 1 ? '' : 's';
		const items = [
			{ s: step.server, title: 'Create your first server',
			  body: step.server.done
				? (step.server.servers ? `${step.server.servers} server${plural(step.server.servers)} on this panel.` : `${nodeServers} server${plural(nodeServers)} on the cluster's nodes.`)
				: `Java (Vanilla, Paper, Purpur, Fabric, Quilt, Forge or NeoForge, or a modpack) or Bedrock, on the version you pick. The panel installs the Java it needs.`
				  + (fw === 'missing' ? `<br><span class="text-yellow-700 dark:text-yellow-300">⚠️ The firewall ufw isn't installed, so the panel can't open game ports for you. If this machine has another firewall, open each server's port in it.</span>`
				   : fw === 'off' ? `<br><span class="text-yellow-700 dark:text-yellow-300">ℹ️ The firewall ufw is installed but off. The panel adds a rule for each server's ports, which takes effect when you turn ufw on.</span>` : ''),
			  action: step.server.done ? '' : `<button onclick="ServerApp.openDeployModal()" class="${btn}">➕ New Server</button>` },
			{ s: step.backups, title: 'Back up your worlds',
			  body: step.backups.onNodes ? `Backups of the nodes' servers are set on each node.`
				: step.backups.done ? 'Every server has a backup schedule.'
				: !step.server.done ? 'Once you have a server, give it a backup schedule.'
				: `Give each server a schedule on its <b>Backups</b> tab. Daily at 04:00, keeping 7, is a good start. Backups run even while the panel is stopped.`,
			  action: step.server.done && !step.backups.done ? `<div class="flex flex-wrap gap-2">${unscheduled.map(n =>
				`<button onclick="ServerApp.openServerByName(${this.jsArg(n)}, 'backups')" class="${btn}">${esc(n)}</button>`).join('')}</div>` : '' },
			{ s: step.connection, title: 'Choose how the panel is reached',
			  body: step.connection.cluster_role === 'node' ? 'HTTPS comes from the cluster.'
				: step.connection.https_mode !== 'off' ? 'The panel uses HTTPS.'
				: step.connection.http_ok ? 'You chose plain HTTP on a network you trust. Phones can only add it to the home screen as a bookmark; installing it as an app needs HTTPS.'
				: `The panel uses plain HTTP, so anyone who can watch your network can read passwords as you sign in. That's usually fine on a home network you trust. Elsewhere, turn on HTTPS. HTTPS also lets phones install the panel as an app.`,
			  action: step.connection.done ? ''
				: `<div class="flex flex-wrap gap-3 items-center"><button onclick="ServerApp.openPanelSettings('connection')" class="${btn}">Turn on HTTPS</button>
				   <button onclick="ServerApp.setupAnswer({ http_ok: true })" class="${link}">Keep HTTP, this is my home network</button></div>` },
			{ s: step.notifications, title: 'Get told when something breaks',
			  body: step.notifications.done ? 'Crash and backup alerts go to your webhook.'
				: `Send crashes and failed backups to a Discord channel or any webhook, even while the panel is stopped.`,
			  action: step.notifications.done ? '' : `<button onclick="ServerApp.openPanelSettings('notifications')" class="${btn}">Set up alerts</button>` },
		];
		const next = items.findIndex(i => !i.s.done);
		return `
			<section class="bg-white dark:bg-gray-800 rounded-lg border-2 border-theme shadow p-4">
				<div class="flex justify-between items-start gap-3 mb-3">
					<div>
						<h3 class="font-bold text-gray-900 dark:text-white">🚀 Set up RedstonePanel</h3>
						<p class="${note}">${done} of ${g.total} done. Each step opens the place where it's done, and ticks itself when it is.</p>
					</div>
					<button onclick="ServerApp.setupAnswer({ dismissed: true })" class="${note} underline whitespace-nowrap" title="Reopen it from Panel Settings">Hide guide</button>
				</div>
				<ol class="space-y-2">${items.map((i, n) => `
					<li class="flex gap-3 p-2 rounded ${n === next ? 'bg-gray-50 dark:bg-gray-900/50' : ''}">
						<span class="text-lg leading-6">${i.s.done ? '✅' : n === next ? '👉' : '⬜'}</span>
						<div class="min-w-0 flex-1">
							<p class="font-bold text-sm ${i.s.done ? 'text-gray-500 dark:text-gray-400' : 'text-gray-900 dark:text-white'}">${n + 1}. ${i.title}</p>
							<p class="${note} mt-0.5">${i.body}</p>
							${i.action ? `<div class="mt-2">${i.action}</div>` : ''}
						</div>
					</li>`).join('')}
				</ol>
			</section>`;
	},

	async setupAnswer(changes) {
		const res = await fetch('/api/settings/setup', {
			method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes)
		});
		if (!res.ok) return alert(await this.apiError(res));
		this.setupData = await res.json();
		if (this.dashData && !document.getElementById('dashboard').classList.contains('hidden')) this.renderDashboard();
		const el = document.getElementById('set-setup-guide');
		if (el) el.outerHTML = this.setupSettingsRow();
	},

	// Panel Settings: reopen (or close) the guide, and take back "keep HTTP"
	setupSettingsRow() {
		const g = this.setupData;
		const note = 'text-xs text-gray-500 dark:text-gray-400 mb-6';
		if (!g) return `<p id="set-setup-guide" class="${note}">Loading...</p>`;
		const conn = g.steps.find(x => x.id === 'connection') || {};
		const http = conn.http_ok ? ` You chose to keep plain HTTP: <button onclick="ServerApp.setupAnswer({ http_ok: false })" class="underline text-theme">ask again</button>.` : '';
		return g.dismissed
			? `<p id="set-setup-guide" class="${note}">The setup guide is hidden (${g.done} of ${g.total} steps done). <button onclick="ServerApp.setupAnswer({ dismissed: false })" class="underline text-theme">Show it on the dashboard</button>.${http}</p>`
			: `<p id="set-setup-guide" class="${note}">${g.complete ? 'All setup steps are done.' : `The setup guide is on the dashboard (${g.done} of ${g.total} steps done). <button onclick="ServerApp.setupAnswer({ dismissed: true })" class="underline text-theme">Hide it</button>.`}${http}</p>`;
	},

	// One panel of the cluster: the few numbers core.host.summary() gives
	nodeCard(n) {
		const esc = t => this.escapeHtml(t === null || t === undefined ? '' : t);
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const s = n.summary;
		const head = `
			<div class="flex justify-between gap-2 items-start">
				<div class="min-w-0">
					<p class="font-bold text-gray-900 dark:text-white truncate"><span class="${n.state === 'ok' ? 'text-green-500' : 'text-yellow-500'}">●</span> ${esc(n.name)}${n.local ? ` <span class="${note} font-normal">(this panel)</span>` : ''}</p>
					<p class="${note}">${s ? `RedstonePanel ${esc(s.version || '?')} · up ${this.fmtDuration(s.uptime)}` : esc(n.url || '')}</p>
				</div>
				${n.local ? '' : `<a href="${esc(n.url)}/" target="_blank" rel="noopener" class="text-xs underline text-theme whitespace-nowrap">Open ↗</a>`}
			</div>`;
		if (!s) {
			return `<div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow p-4 text-sm">${head}
				<p class="mt-2 text-sm p-2 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200">⚠️ ${esc(this.clusterStates[n.state] || n.state)}${n.state === 'too_old' ? ' (1.11 or newer shows its resources here)' : ''}</p></div>`;
		}
		const line = (label, p, text) => `
			<div class="mt-2"><div class="flex justify-between ${note}"><span>${label}</span><span>${text}</span></div>${this.bar(p)}</div>`;
		const mem = s.memory || {};
		return `<div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow p-4 text-sm">${head}
			${line('CPU', s.cpu_percent, `${this.fmtPct(s.cpu_percent)} of ${s.cores || '?'} cores`)}
			${line('Memory', mem.percent, `${this.fmtBytes(mem.available)} free of ${this.fmtBytes(mem.total)}`)}
			${s.disk ? line('Disk', s.disk.percent, `${this.fmtBytes(s.disk.free)} free of ${this.fmtBytes(s.disk.total)}`) : ''}
			<p class="mt-2 text-gray-700 dark:text-gray-300">${s.servers_online} of ${s.servers_total} servers online${s.attention.length ? ` · <span class="text-yellow-600 dark:text-yellow-400">${s.attention.length} need${s.attention.length === 1 ? 's' : ''} attention</span>` : ''}</p>
			${s.attention.slice(0, 3).map(a => `<p class="${note} mt-1">${a.level === 'error' ? '⛔' : '⚠️'} ${esc(a.text)}</p>`).join('')}
		</div>`;
	},

	// --- Cluster page (master): every panel's servers, with start/stop/restart ---
	clusterTimer: null,
	clusterData: null,
	clusterStates: {
		unreachable: "Can't reach this panel. Is it running and on the network?",
		rejected: 'This panel no longer accepts this master: it left the cluster, or another panel was promoted to master.',
		cert_error: 'Certificate problem. Renew its certificate in Panel Settings > Cluster.',
		too_old: 'Update RedstonePanel on this server.',
		error: 'Something went wrong talking to this panel.',
	},

	// --- Port forwarding (1.16.14, admins): what to forward on the internet router so players
	// outside the home can join, per server and per cluster member (core/portforward.py)
	portsData: null,
	portsCheck: null,
	portsRouter: (() => { try { return localStorage.getItem('rp_portsRouter') || 'pfsense'; } catch (e) { return 'pfsense'; } })(),

	openPorts() {
		this.closeJoin();
		this.hideDashboard();
		this.closeCluster();
		this.activeServer = null;
		this.renderSidebar();
		document.getElementById('workspace').classList.add('hidden');
		document.getElementById('ports-view').classList.remove('hidden');
		this.setMobileTitle('🔀 Port forwarding', this.panelName);
		this.loadPorts();
	},

	closePorts() {
		const v = document.getElementById('ports-view');
		if (v) v.classList.add('hidden');
		this.closeJoin();
	},

	// --- Join pages (1.21.5, admins): a public page per server that tells players how to join,
	// served on its own port (core/joinpage.py, core/joinsite.py)
	joinData: null,
	joinOpen: {},          // which servers' forms are open: { 'member/server': true }
	joinNote: null,        // { key, text, ok } shown under the form that was saved

	openJoin() {
		this.hideDashboard();
		this.closeCluster();
		this.activeServer = null;
		this.renderSidebar();
		document.getElementById('workspace').classList.add('hidden');
		document.getElementById('join-view').classList.remove('hidden');
		this.setMobileTitle('🔗 Join pages', this.panelName);
		this.loadJoin();
	},

	closeJoin() {
		const v = document.getElementById('join-view');
		if (v) v.classList.add('hidden');
	},

	async loadJoin() {
		const body = document.getElementById('join-body');
		if (!this.joinData) body.innerHTML = '<p class="text-gray-500">Loading...</p>';
		try {
			const res = await fetch('/api/join-pages');
			if (!res.ok) throw new Error(await this.apiError(res));
			this.joinData = await res.json();
		} catch (e) {
			body.innerHTML = `<p class="text-red-500">Couldn't load the join pages: ${this.escapeHtml(e.message)}</p>`;
			return;
		}
		this.renderJoin();
	},

	async saveJoinListener(button) {
		button.disabled = true;
		const res = await fetch('/api/settings/join', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled: document.getElementById('join-on').checked,
			                       port: parseInt(document.getElementById('join-port').value, 10) || 0,
			                       index: document.getElementById('join-index').checked }) });
		if (!res.ok) {
			this.joinNote = { key: '', text: await this.apiError(res), ok: false };
			return this.loadJoin();
		}
		this.joinData = await res.json();
		const l = this.joinData.listener;
		this.joinNote = { key: '', ok: !l.error, text: l.error || (l.enabled ? `Join pages are on, port ${l.port}.` : 'Join pages are off.') };
		this.renderJoin();
	},

	async saveJoinProxy(button) {
		button.disabled = true;
		const l = this.joinData.listener;
		const res = await fetch('/api/settings/join', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled: l.enabled, port: l.port, index: l.index,
			                       proxies: document.getElementById('join-proxies').value,
			                       proxy_only: document.getElementById('join-proxy-only').checked,
			                       url: document.getElementById('join-url').value.trim(),
			                       cloudflare: document.getElementById('join-cloudflare').checked }) });
		if (!res.ok) {
			this.joinNote = { key: 'proxy', text: await this.apiError(res), ok: false };
			return this.loadJoin();
		}
		this.joinData = await res.json();
		const p = this.joinData.proxy;
		const who = [...p.addresses, ...(p.cloudflare ? ['Cloudflare'] : [])];
		this.joinNote = { key: 'proxy', ok: true, text: who.length ? `Saved. Requests from ${who.join(', ')} are treated as your proxy.` : 'Saved. No proxy is set.' };
		this.renderJoin();
	},

	toggleJoinForm(key) {
		this.joinOpen[key] = !this.joinOpen[key];
		this.renderJoin();
	},

	async saveJoinPage(member, server, button, newLink) {
		const key = `${member}/${server}`, id = n => document.getElementById(`join-${n}-${this.joinId(key)}`);
		button.disabled = true;
		const res = await fetch('/api/join-pages/page', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ member, server, enabled: id('enabled').checked, slug: id('slug').value.trim(),
				unlisted: id('unlisted').checked, new_link: !!newLink, address: id('address').value.trim(),
				note: id('note').value, link_url: id('link-url').value.trim(), link_label: id('link-label').value.trim(),
				pack_url: id('pack-url') ? id('pack-url').value.trim() : '', show_players: id('players').checked }) });
		if (!res.ok) {
			button.disabled = false;
			const note = id('msg');
			note.className = 'text-sm text-red-500 mt-2';
			note.innerText = await this.apiError(res);
			return;
		}
		const d = await res.json();
		this.joinNote = { key, ok: true, text: d.page.enabled ? 'Saved. The page is public.' : 'Saved. The page is off.' };
		this.loadJoin();
	},

	joinId(key) { return key.replace(/[^A-Za-z0-9_-]/g, '_'); },

	copyJoinLink(url, button) {
		const done = () => { const old = button.innerText; button.innerText = 'Copied'; setTimeout(() => { button.innerText = old; }, 1500); };
		if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(url).then(done).catch(() => prompt('Copy the link:', url));
		else prompt('Copy the link:', url);
	},

	renderJoin() {
		const d = this.joinData, l = d.listener, esc = t => this.escapeHtml(String(t ?? ''));
		const card = 'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4';
		const h = 'text-lg font-bold text-gray-900 dark:text-white mb-2';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const btn = 'px-3 py-2 rounded text-sm font-bold transition';
		const grey = `${btn} bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-800 dark:text-white`;
		const warn = t => `<p class="text-sm p-3 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200 border border-yellow-300 dark:border-yellow-700">${t}</p>`;
		const msg = key => this.joinNote && this.joinNote.key === key
			? `<p class="text-sm ${this.joinNote.ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'} mt-2">${esc(this.joinNote.text)}</p>` : '';
		const base = d.public_url || d.lan_url;
		const multi = d.members.length > 1;

		const state = !l.enabled ? ''
			: l.error ? `<p class="text-sm text-red-500 mt-2"><b>Not running.</b> ${esc(l.error)}</p>`
			: !l.managed ? `<p class="${note} mt-2">The pages start with the panel's own service.</p>`
			: l.running ? `<p class="${note} mt-2"><span class="text-green-600 dark:text-green-400 font-bold">Running</span> on port ${esc(l.serving_port)} (${l.scheme === 'https' ? 'HTTPS, with the panel\'s certificate' : 'HTTP'}).</p>`
			: `<p class="text-sm text-red-500 mt-2"><b>Not running.</b> Restart the panel, or pick another port.</p>`;
		const listenerCard = `<div class="${card}">
			<h3 class="${h}">The public port</h3>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3">Join pages have their own port, so you can forward it on your router and keep the panel itself off the internet. That port answers nothing but the join pages: no sign-in page and none of the panel.</p>
			<label class="flex items-start gap-2 mb-2 text-sm cursor-pointer">
				<input type="checkbox" id="join-on" ${l.enabled ? 'checked' : ''} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">Turn join pages on</b><br><span class="${note}">Each server's page still has to be turned on below.</span></span>
			</label>
			<label class="flex items-start gap-2 mb-3 text-sm cursor-pointer">
				<input type="checkbox" id="join-index" ${l.index ? 'checked' : ''} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">Also list the public pages at /join</b><br><span class="${note}">Off: a page is found only by its own link.</span></span>
			</label>
			<div class="flex flex-wrap items-end gap-2">
				<label class="${note}">Port<br><input id="join-port" type="number" min="1" max="65535" value="${esc(l.port)}" class="${field} w-28 mt-1"></label>
				<button onclick="ServerApp.saveJoinListener(this)" class="${btn} bg-theme hover-bg-theme text-white">Save</button>
			</div>
			${state}${msg('')}
			<p class="${note} mt-2">The port is opened in this server's firewall. For players outside your home, forward TCP ${esc(l.port)} to this machine on your router as well.</p>
		</div>`;

		const px = d.proxy || { addresses: [], only: false, url: '', cloudflare: false, haproxy: '', cloudflared: '' };
		const proxyCard = `<div class="${card}">
			<h3 class="${h}">Through a proxy (HAProxy or Cloudflare, optional)</h3>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3">Already run HAProxy (for example on pfSense) or Cloudflare? Send <span class="font-mono">/join</span> on your own domain to the port above, and players get the pages with your domain and certificate. The port above keeps working too.</p>
			<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
				<label class="${note}">The proxy's address<input id="join-proxies" maxlength="300" value="${esc(px.addresses.join(', '))}" placeholder="10.0.0.1" class="${field} w-full mt-1 font-mono">
					<span class="block mt-1">The address HAProxy or a Cloudflare Tunnel connects from: pfSense's LAN address, or 127.0.0.1 for a tunnel on this machine. Only requests from it may say which player they are for, so each player keeps their own limit.</span></label>
				<label class="${note}">Address players open<input id="join-url" maxlength="200" value="${esc(px.url)}" placeholder="https://play.example.com" class="${field} w-full mt-1 font-mono">
					<span class="block mt-1">Used for the links the panel shows. Empty: the address from Port forwarding with the port above.</span></label>
			</div>
			<label class="flex items-start gap-2 mt-3 text-sm cursor-pointer">
				<input type="checkbox" id="join-cloudflare" ${px.cloudflare ? 'checked' : ''} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">Cloudflare's proxy (orange cloud) sends players here</b><br><span class="${note}">For a domain proxied by Cloudflare with a port forwarded on your router (Cloudflare carries 443, 8443 or 2053, not 8444). Requests from Cloudflare's own addresses may name the player. Not needed for a Cloudflare Tunnel.</span></span>
			</label>
			<label class="flex items-start gap-2 mt-3 text-sm cursor-pointer">
				<input type="checkbox" id="join-proxy-only" ${px.only ? 'checked' : ''} class="mt-1">
				<span><b class="text-gray-900 dark:text-white">Only through the proxy</b><br><span class="${note}">The port above answers nobody but the proxy. Don't forward it on your router then.</span></span>
			</label>
			<div class="flex flex-wrap gap-2 mt-3">
				<button onclick="ServerApp.saveJoinProxy(this)" class="${btn} bg-theme hover-bg-theme text-white">Save</button>
			</div>
			${msg('proxy')}
			<details class="mt-3"><summary class="text-sm font-bold text-gray-900 dark:text-white cursor-pointer">HAProxy settings to copy</summary>
				<pre id="join-haproxy" class="mt-2 p-3 rounded bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 text-xs font-mono whitespace-pre overflow-x-auto text-gray-800 dark:text-gray-200">${esc(px.haproxy)}</pre>
				<button onclick="ServerApp.copyJoinLink(document.getElementById('join-haproxy').innerText, this)" class="${grey} mt-2">Copy</button>
				<p class="${note} mt-2">On pfSense: Services &gt; HAProxy. The manual's "Join pages through a proxy" section shows each screen.</p>
			</details>
			<details class="mt-3"><summary class="text-sm font-bold text-gray-900 dark:text-white cursor-pointer">Cloudflare Tunnel settings to copy</summary>
				<pre id="join-cloudflared" class="mt-2 p-3 rounded bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 text-xs font-mono whitespace-pre overflow-x-auto text-gray-800 dark:text-gray-200">${esc(px.cloudflared)}</pre>
				<button onclick="ServerApp.copyJoinLink(document.getElementById('join-cloudflared').innerText, this)" class="${grey} mt-2">Copy</button>
				<p class="${note} mt-2">For cloudflared on this machine. In the Cloudflare dashboard instead: add a public hostname with path <span class="font-mono">^/join</span>, the service above, and No TLS Verify on. Then enter 127.0.0.1 as the proxy's address.</p>
			</details>
		</div>`;

		const addressCard = d.public_address ? '' : `<div class="${card}">${warn(`No internet address is set yet, so the pages can't tell players what to type. Set it under <button onclick="ServerApp.openPorts()" class="underline font-bold">🔀 Port forwarding</button>, or type an address for each server below.`)}</div>`;

		const row = (m, s) => {
			const key = `${m.id}/${s.name}`, id = this.joinId(key), p = s.page, open = !!this.joinOpen[key];
			const url = `${base}/${encodeURIComponent(s.path)}`;
			const kind = [s.edition, s.loader && s.loader !== 'Vanilla' ? s.loader : '', s.minecraft].filter(Boolean).join(' · ');
			const live = p.enabled && l.enabled;
			const badge = p.enabled
				? `<span class="text-xs font-bold px-2 py-0.5 rounded bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300">${p.unlisted ? 'Public, unlisted' : 'Public'}</span>`
				: `<span class="text-xs font-bold px-2 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">Off</span>`;
			const link = !p.enabled ? '' : `<div class="flex flex-wrap items-center gap-2 mt-2">
					<a href="${esc(url)}" target="_blank" rel="noopener" class="text-sm font-mono break-all underline text-theme">${esc(url)}</a>
					<button onclick="ServerApp.copyJoinLink('${esc(url)}', this)" class="${grey}">Copy link</button>
				</div>${live ? '' : `<p class="${note} mt-1">The link works once join pages are turned on above.</p>`}`;
			const form = !open ? '' : `<div class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 space-y-3">
				<label class="flex items-start gap-2 text-sm cursor-pointer">
					<input type="checkbox" id="join-enabled-${id}" ${p.enabled ? 'checked' : ''} class="mt-1">
					<span><b class="text-gray-900 dark:text-white">Make this server's join page public</b><br><span class="${note}">Anyone with the link sees the server's name, version, whether it's online, how many are playing and how to join.</span></span>
				</label>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
					<label class="${note}">Link name<input id="join-slug-${id}" maxlength="40" value="${esc(p.slug)}" class="${field} w-full mt-1 font-mono">
						<span class="block mt-1">The end of the link: /join/<b>${esc(p.slug)}</b></span></label>
					<label class="${note}">Address players type<input id="join-address-${id}" maxlength="260" value="${esc(p.address)}" placeholder="${esc(d.public_address || 'play.example.com')}" class="${field} w-full mt-1 font-mono">
						<span class="block mt-1">Empty: ${d.public_address ? `<b>${esc(d.public_address)}</b>, from Port forwarding,` : 'the address from Port forwarding'} with this server's port. Add <span class="font-mono">:port</span> if the router shows a different one outside.</span></label>
				</div>
				<label class="flex items-start gap-2 text-sm cursor-pointer">
					<input type="checkbox" id="join-unlisted-${id}" ${p.unlisted ? 'checked' : ''} class="mt-1">
					<span><b class="text-gray-900 dark:text-white">Unlisted: use a random link instead of the link name</b><br><span class="${note}">For a friends-only server. The page can't be guessed and never appears in the list at /join.${p.unlisted && p.enabled ? ` <button onclick="ServerApp.saveJoinPage('${esc(m.id)}', '${esc(s.name)}', this, true)" class="underline text-theme">Make a new link</button> (the old one stops working).` : ''}</span></span>
				</label>
				<label class="flex items-start gap-2 text-sm cursor-pointer">
					<input type="checkbox" id="join-players-${id}" ${p.show_players ? 'checked' : ''} class="mt-1">
					<span><b class="text-gray-900 dark:text-white">Show who is playing</b><br><span class="${note}">Off: only the number of players is shown, never their names.</span></span>
				</label>
				<label class="${note} block">A note for players (rules, when you play)<textarea id="join-note-${id}" rows="3" maxlength="600" class="${field} w-full mt-1">${esc(p.note)}</textarea></label>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
					<label class="${note}">A link of your own (Discord, a website)<input id="join-link-url-${id}" maxlength="300" value="${esc(p.link_url)}" placeholder="https://discord.gg/..." class="${field} w-full mt-1"></label>
					<label class="${note}">What the link's button says<input id="join-link-label-${id}" maxlength="40" value="${esc(p.link_label)}" placeholder="Join our Discord" class="${field} w-full mt-1"></label>
				</div>
				${s.mods ? `<label class="${note} block">Where players get the mods<input id="join-pack-url-${id}" maxlength="300" value="${esc(p.pack_url)}" placeholder="https://www.curseforge.com/minecraft/modpacks/..." class="${field} w-full mt-1">
					<span class="block mt-1">A server made from a Modrinth modpack links to that exact pack by itself. For anything else, paste the pack's page here.</span></label>` : ''}
				<div class="flex flex-wrap gap-2">
					<button onclick="ServerApp.saveJoinPage('${esc(m.id)}', '${esc(s.name)}', this)" class="${btn} bg-theme hover-bg-theme text-white">Save page</button>
					<button onclick="ServerApp.toggleJoinForm('${esc(key)}')" class="${grey}">Close</button>
				</div>
				<p id="join-msg-${id}" class="hidden"></p>
			</div>`;
			return `<div class="py-3 border-b border-gray-100 dark:border-gray-700/60 last:border-b-0">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<div class="min-w-0">
						<p class="font-bold text-gray-900 dark:text-white break-words">${esc(s.name)} ${badge}</p>
						<p class="${note}">${esc(kind)}${multi ? ` · ${m.local ? esc(m.name) : '🌐 ' + esc(m.name)}` : ''}</p>
					</div>
					<button onclick="ServerApp.toggleJoinForm('${esc(key)}')" class="${grey}">${open ? 'Close' : p.enabled ? 'Change' : 'Set up'}</button>
				</div>
				${link}${msg(key)}${form}
			</div>`;
		};
		const rows = d.members.flatMap(m => m.servers.map(s => row(m, s))).join('');
		const down = d.members.filter(m => m.state !== 'ok').map(m => `<li>🌐 ${esc(m.name)}: couldn't be reached (${esc(m.error || m.state)}), so its servers aren't listed. Their pages show as offline meanwhile.</li>`).join('');
		const serversCard = `<div class="${card}">
			<h3 class="${h}">Servers</h3>
			${rows || `<p class="text-sm text-gray-500">No servers yet.</p>`}
			${down ? `<ul class="${note} list-disc ml-5 mt-3">${down}</ul>` : ''}
		</div>`;

		document.getElementById('join-body').innerHTML = listenerCard + proxyCard + addressCard + serversCard;
		this.joinNote = null;
	},

	async loadPorts() {
		const body = document.getElementById('ports-body');
		if (!this.portsData) body.innerHTML = '<p class="text-gray-500">Loading...</p>';
		try {
			const res = await fetch('/api/port-forwarding');
			if (!res.ok) throw new Error(await this.apiError(res));
			this.portsData = await res.json();
		} catch (e) {
			body.innerHTML = `<p class="text-red-500">Couldn't load port forwarding: ${this.escapeHtml(e.message)}</p>`;
			return;
		}
		this.renderPorts();
	},

	setPortsRouter(kind) {
		this.portsRouter = kind;
		try { localStorage.setItem('rp_portsRouter', kind); } catch (e) { /* only a convenience */ }
		this.renderPorts();
	},

	async savePublicAddress() {
		const value = document.getElementById('ports-public').value.trim();
		const res = await fetch('/api/port-forwarding/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ public_address: value }) });
		if (!res.ok) { alert(await this.apiError(res)); return; }
		this.portsCheck = null;
		this.loadPorts();
	},

	async lookupPublicAddress(btn) {
		const note = document.getElementById('ports-lookup-note');
		btn.disabled = true;
		note.className = 'text-xs text-gray-500 mt-2';
		note.innerText = 'Asking the router...';
		const res = await fetch('/api/port-forwarding/lookup', { method: 'POST' });
		btn.disabled = false;
		if (!res.ok) {
			note.className = 'text-xs text-yellow-700 dark:text-yellow-300 mt-2';
			note.innerText = await this.apiError(res);
			return;
		}
		const d = await res.json();
		document.getElementById('ports-public').value = d.address;
		note.innerText = `The router at ${d.gateway} says its internet address is ${d.address} (asked with ${d.method}). Press Save to keep it.`;
	},

	async checkPorts(btn) {
		btn.disabled = true;
		btn.innerText = 'Checking...';
		const res = await fetch('/api/port-forwarding/check', { method: 'POST' });
		btn.disabled = false;
		btn.innerText = 'Check from here';
		if (!res.ok) { alert(await this.apiError(res)); return; }
		this.portsCheck = await res.json();
		this.renderPorts();
	},

	// Every row of every server on every member, with where it goes
	portsRows() {
		const out = [];
		for (const m of this.portsData.members) {
			for (const s of m.servers || []) {
				for (const r of s.rows || []) out.push({ m, s, r, ip: m.forward_to || '?' });
			}
			for (const r of m.machine || []) out.push({ m, s: null, r, ip: m.forward_to || '?' });
		}
		return out;
	},

	portsJoinAddress(s, rows) {
		const addr = this.portsData.public_address || 'your-public-address';
		const game = rows.find(x => x.s === s && x.r.kind === 'forward');
		if (!game) return null;
		if (s.edition === 'Java') return game.r.port === '25565' ? addr : `${addr}:${game.r.port}`;
		return `${addr}, port ${game.r.port}`;
	},

	portsText() {
		return this.portsRows().filter(x => x.r.kind === 'forward').map(x =>
			`${x.r.proto.toUpperCase()} ${x.r.port.replace(':', '-')} -> ${x.ip}:${x.r.inside.replace(':', '-')}  ${x.s.name} (${x.r.what})${x.m.local ? '' : ' on ' + x.m.name}`).join('\n');
	},

	copyPorts() {
		const text = this.portsText();
		if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).catch(() => prompt('Copy the list:', text));
		else prompt('Copy the list:', text);
	},

	// pfSense takes TCP and UDP for the same port in one rule (Protocol: TCP/UDP)
	portsRules() {
		const rules = [];
		for (const x of this.portsRows().filter(x => x.r.kind === 'forward')) {
			const same = rules.find(y => y.ip === x.ip && y.port === x.r.port && y.inside === x.r.inside && y.server === x.s.name);
			if (same) { if (!same.protos.includes(x.r.proto)) same.protos.push(x.r.proto); continue; }
			rules.push({ ip: x.ip, port: x.r.port, inside: x.r.inside, protos: [x.r.proto], server: x.s.name,
				member: x.m.local ? '' : x.m.name });
		}
		return rules;
	},

	portsCheckCell(x) {
		if (!this.portsCheck || !x.s) return '';
		const hit = this.portsCheck.results.find(c => c.member === x.m.id && c.server === x.s.name && c.port === x.r.port && c.proto === x.r.proto);
		if (!hit) return '';
		if (hit.result === 'reached') {
			return hit.same_server === false
				? `<span class="text-yellow-700 dark:text-yellow-300" title="${this.escapeHtml(hit.answer?.motd || '')}">⚠️ Another server answered</span>`
				: '<span class="text-green-700 dark:text-green-400">✅ Reached</span>';
		}
		if (hit.result === 'no_answer') return `<span class="text-gray-600 dark:text-gray-300" title="${this.escapeHtml(hit.error || '')}">❔ No answer</span>`;
		return '<span class="text-gray-400" title="UDP ports without a ping can\'t be tried this way">— Not tried</span>';
	},

	renderPorts() {
		const d = this.portsData, esc = t => this.escapeHtml(String(t ?? ''));
		const rows = this.portsRows();
		const fwd = rows.filter(x => x.r.kind === 'forward');
		const optional = rows.filter(x => x.r.kind === 'optional');
		const closed = rows.filter(x => ['lan', 'never', 'ipv6'].includes(x.r.kind));
		const card = 'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4';
		const h = 'text-lg font-bold text-gray-900 dark:text-white mb-2';
		const btn = 'px-3 py-2 rounded text-sm font-bold transition';
		const grey = `${btn} bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-800 dark:text-white`;
		const warn = t => `<p class="text-sm p-3 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200 border border-yellow-300 dark:border-yellow-700">${t}</p>`;
		const memberLabel = m => m.local ? esc(m.name) : `🌐 ${esc(m.name)}`;
		const multi = d.members.length > 1;
		const proto = p => p.toUpperCase();
		const port = p => esc(String(p).replace(':', '–'));

		document.getElementById('ports-subtitle').innerText = fwd.length
			? `${fwd.length} port${fwd.length === 1 ? '' : 's'} to forward on your internet router so players outside your home can join.`
			: 'What to set up on your internet router so players outside your home can join.';

		// 1. The public address
		const kindNote = {
			cgnat: warn(`<b>${esc(d.public_address)}</b> is a carrier-grade NAT address: your internet provider shares one address between many customers, so forwarding ports on your own router can't work. Ask the provider for a public IPv4 address (often called a static or dedicated IP).`),
			private: warn(`<b>${esc(d.public_address)}</b> is a private address, so there's another router in front of yours (double NAT), usually the provider's modem. Put the modem in bridge mode, or forward the same ports on the modem to your router's WAN address as well.`),
		}[d.public_kind] || '';
		const addressCard = `<div class="${card}">
			<h3 class="${h}">Your internet address</h3>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3">The address players type to reach you from outside: your router's WAN address, or a dynamic DNS name that points to it. The panel only asks your own router for it, never an outside website.</p>
			<div class="flex flex-wrap gap-2">
				<input id="ports-public" value="${esc(d.public_address)}" placeholder="e.g. 81.2.69.160 or play.example.com" class="flex-1 min-w-0 sm:min-w-[16rem] p-2 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white">
				<button onclick="ServerApp.savePublicAddress()" class="${btn} bg-theme hover-bg-theme text-white">Save</button>
				<button onclick="ServerApp.lookupPublicAddress(this)" class="${grey}">Ask the router</button>
			</div>
			<p id="ports-lookup-note" class="text-xs text-gray-500 mt-2">“Ask the router” uses NAT-PMP or UPnP on your router (${esc(d.members[0].gateway || 'no gateway found')}). On pfSense they're under Services &gt; UPnP &amp; NAT-PMP and are off by default; the WAN address is also on the pfSense Dashboard.</p>
			${kindNote ? `<div class="mt-3">${kindNote}</div>` : ''}
		</div>`;

		// 2. Ports two servers both need on the one public address
		const clashes = (d.conflicts || []).map(c => `<li><b>${proto(c.proto)} ${port(c.port)}</b>: ${c.users.map(u => `${esc(u.server)}${multi ? ' on ' + esc(u.member) : ''}`).join(' and ')}</li>`).join('');
		const clashCard = clashes ? `<div class="p-4 rounded-lg bg-red-50 dark:bg-red-900/30 border border-red-300 dark:border-red-700 text-sm text-red-800 dark:text-red-200">
			<p class="font-bold mb-1">Two servers want the same outside port</p>
			<p class="mb-2">You have one internet address, so the router can send each outside port to only one server. Give one of them another port in its Configuration tab (a Java server's players then add <code>:port</code> to the address).</p>
			<ul class="list-disc ml-5">${clashes}</ul></div>` : '';

		// 3. What to forward
		const issues = d.members.flatMap(m => (m.servers || []).flatMap(s => (s.issues || []).map(i => `<li><b>${esc(s.name)}</b>${multi ? ' on ' + memberLabel(m) : ''}: ${esc(i)}</li>`))).join('');
		const memberNotes = d.members.map(m => {
			if (m.state !== 'ok') return `<li>${memberLabel(m)}: couldn't be reached (${esc(m.error || m.state)}), so its servers aren't listed.</li>`;
			if (m.old) return `<li>${memberLabel(m)} runs an older RedstonePanel: only its game ports are listed, and its address comes from the cluster (${esc(m.url_host)}). Update it for the full list.</li>`;
			if (!m.lan_ip) return `<li>${memberLabel(m)}: couldn't tell which address the router reaches it on.</li>`;
			return '';
		}).join('');
		const table = (list, closed = false) => { const checks = this.portsCheck && !closed; return `<div class="overflow-x-auto -mx-3 sm:mx-0"><table class="rp-cards w-full text-sm text-gray-700 dark:text-gray-300">
			<thead><tr class="text-left text-xs uppercase text-gray-500 border-b border-gray-200 dark:border-gray-700">
				<th class="py-2 pr-3">Server</th><th class="py-2 pr-3">${closed ? 'Port' : 'Outside port'}</th><th class="py-2 pr-3">Protocol</th>
				<th class="py-2 pr-3">${closed ? 'Machine' : 'Forward to'}</th><th class="py-2 pr-3">What it's for</th>${checks ? '<th class="py-2 pr-3">From here</th>' : ''}</tr></thead>
			<tbody>${list.map(x => `<tr class="border-b border-gray-100 dark:border-gray-700/60 align-top">
				<td class="rp-card-head py-2 pr-3">${x.s ? esc(x.s.name) : 'This machine'}${multi ? `<div class="text-xs text-gray-500 font-normal">${memberLabel(x.m)}</div>` : ''}</td>
				<td data-label="${closed ? 'Port' : 'Outside port'}" class="py-2 pr-3 font-mono">${port(x.r.port)}</td>
				<td data-label="Protocol" class="py-2 pr-3 font-mono">${proto(x.r.proto)}</td>
				<td data-label="${closed ? 'Machine' : 'Forward to'}" class="py-2 pr-3 font-mono">${closed ? esc(x.ip) : `${esc(x.ip)}:${port(x.r.inside)}`}</td>
				<td data-label="For" class="py-2 pr-3">${esc(x.r.what)}${x.r.note ? `<div class="text-xs text-gray-500">${esc(x.r.note)}</div>` : ''}${x.r.ufw === false ? '<div class="text-xs text-yellow-700 dark:text-yellow-300">ufw on this machine doesn\'t allow it yet.</div>' : ''}</td>
				${checks ? `<td data-label="From here" class="py-2 pr-3">${this.portsCheckCell(x)}</td>` : ''}
			</tr>`).join('')}</tbody></table></div>`; };
		const joins = d.members.flatMap(m => (m.servers || []).map(s => ({ s, a: this.portsJoinAddress(s, rows) })))
			.filter(j => j.a).map(j => `<li><b>${esc(j.s.name)}</b> (${esc(j.s.edition)}): <code>${esc(j.a)}</code></li>`).join('');
		const forwardCard = `<div class="${card}">
			<div class="flex flex-wrap items-center justify-between gap-2 mb-2">
				<h3 class="text-lg font-bold text-gray-900 dark:text-white">Forward these</h3>
				<div class="flex flex-wrap gap-2">
					${fwd.length ? `<button onclick="ServerApp.copyPorts()" class="${grey}">Copy as text</button>
					<button onclick="ServerApp.checkPorts(this)" ${d.public_address ? '' : 'disabled title="Save your internet address first"'} class="${grey}">Check from here</button>` : ''}
				</div>
			</div>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3">One port forward per line, from your internet address to the machine the server runs on. Keep the outside port the same as the inside port unless you have a reason not to (Bedrock needs them the same).</p>
			${fwd.length ? table(fwd) : '<p class="text-sm text-gray-500">No servers yet, so there\'s nothing to forward.</p>'}
			${issues ? `<div class="mt-3">${warn('<b>Fix these first</b><ul class="list-disc ml-5 mt-1">' + issues + '</ul>')}</div>` : ''}
			${memberNotes ? `<ul class="list-disc ml-5 mt-3 text-sm text-gray-600 dark:text-gray-300">${memberNotes}</ul>` : ''}
			${this.portsCheck ? `<p class="text-xs text-gray-500 mt-3">Checked ${esc(new Date(this.portsCheck.checked * 1000).toLocaleTimeString())} by connecting from ${esc(d.members[0].name)} to ${esc(this.portsCheck.address)}. This only works when the router loops connections back to the inside (pfSense: NAT reflection). “No answer” doesn't prove the forward is wrong: the surest test is a phone on mobile data, with Wi-Fi off.</p>` : ''}
			${joins ? `<p class="text-sm font-bold text-gray-900 dark:text-white mt-4">Players outside join with</p><ul class="list-disc ml-5 text-sm text-gray-700 dark:text-gray-300">${joins}</ul>` : ''}
		</div>`;

		// 4. Optional and closed ports
		const optionalCard = optional.length ? `<div class="${card}"><h3 class="${h}">Only if you need them</h3>${table(optional, false)}</div>` : '';
		const web = d.members[0].web || {};
		const closedCard = `<div class="${card}">
			<h3 class="${h}">Leave these closed</h3>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3">Don't forward these. They're only for your home network, or they'd let strangers try to take control.</p>
			${closed.length ? table(closed, true) : ''}
			<div class="mt-3 text-sm text-gray-700 dark:text-gray-300">
				<p class="font-bold">The panel itself (${esc((web.scheme || 'http').toUpperCase())} ${esc(web.port || '')}): optional, and not recommended</p>
				<p class="mt-1">Forward it only if you must manage servers from outside, and then only with HTTPS, two-factor sign-in for every admin, and trusted-LAN sign-in off. A VPN into your home network (pfSense has WireGuard and OpenVPN) is safer: the panel then never faces the internet.${web.scheme !== 'https' ? ' <b class="text-red-600 dark:text-red-400">This panel is on plain HTTP: never forward it as it is.</b>' : ''}</p>
				${multi ? '<p class="mt-2">Cluster members talk to each other over the home network; there\'s nothing to forward for the cluster.</p>' : ''}
			</div>
		</div>`;

		// 5. How to do it on the router
		const tabBtn = (k, label) => `<button onclick="ServerApp.setPortsRouter('${k}')" class="py-2 px-4 text-sm font-bold ${this.portsRouter === k ? 'tab-active' : 'text-gray-500 dark:text-gray-400'}">${label}</button>`;
		const ips = [...new Set(d.members.filter(m => m.forward_to).map(m => `${m.local && !multi ? 'this machine' : m.name} (${m.forward_to})`))].join(', ');
		const rules = this.portsRules();
		const pfRules = rules.map(r => `<tr class="border-b border-gray-100 dark:border-gray-700/60">
			<td class="rp-card-head py-1.5 pr-3">${esc(r.server)}${r.member ? ` <span class="text-xs text-gray-500 font-normal">on ${esc(r.member)}</span>` : ''}</td>
			<td data-label="Protocol" class="py-1.5 pr-3 font-mono">${r.protos.length > 1 ? 'TCP/UDP' : proto(r.protos[0])}</td>
			<td data-label="Port range" class="py-1.5 pr-3 font-mono">${port(r.port)}</td>
			<td data-label="Target IP" class="py-1.5 pr-3 font-mono">${esc(r.ip)}</td>
			<td data-label="Target port" class="py-1.5 pr-3 font-mono">${port(r.inside)}</td>
			<td data-label="Description" class="py-1.5 pr-3">RedstonePanel ${esc(r.server.replace(/^minecraft-/, ''))}${r.member ? ' ' + esc(r.member) : ''}</td></tr>`).join('');
		const pfsense = `<ol class="list-decimal ml-5 space-y-2">
			<li><b>Give each machine a fixed address</b> so a forward keeps pointing at the right one: Services &gt; DHCP Server &gt; LAN &gt; DHCPv4 Static Mappings (pfSense with Kea: the same page) for ${esc(ips || 'each machine')}.</li>
			<li>Go to <b>Firewall &gt; NAT &gt; Port Forward</b> and press <b>Add</b> (the arrow pointing down adds at the bottom).</li>
			<li>For each line below set <b>Interface</b> WAN, <b>Address Family</b> IPv4, the <b>Protocol</b>, <b>Destination</b> “WAN address”, the <b>Destination port range</b> (From and To), the <b>Redirect target IP</b> (type “Address or Alias”) and <b>Redirect target port</b>, a <b>Description</b>, and leave <b>Filter rule association</b> on “Add associated filter rule”. Save.
				${pfRules ? `<div class="overflow-x-auto -mx-3 sm:mx-0 mt-2"><table class="rp-cards w-full text-sm"><thead><tr class="text-left text-xs uppercase text-gray-500 border-b border-gray-200 dark:border-gray-700">
				<th class="py-1.5 pr-3">Server</th><th class="py-1.5 pr-3">Protocol</th><th class="py-1.5 pr-3">Destination port range</th><th class="py-1.5 pr-3">Redirect target IP</th><th class="py-1.5 pr-3">Redirect target port</th><th class="py-1.5 pr-3">Description</th></tr></thead><tbody>${pfRules}</tbody></table></div>` : ''}</li>
			<li>Press <b>Apply Changes</b>. pfSense adds a matching pass rule under Firewall &gt; Rules &gt; WAN for each forward.</li>
			<li><b>Optional, for players at home who use the internet address:</b> System &gt; Advanced &gt; Firewall &amp; NAT, set <b>NAT Reflection mode for port forwards</b> to “Pure NAT”, or set it on each forward. It's also what lets “Check from here” work. Players at home can always use the LAN address instead.</li>
			<li><b>IPv6:</b> IPv6 isn't forwarded. If your provider gives you IPv6 and you want players to use it, add a pass rule on Firewall &gt; Rules &gt; WAN to each machine's IPv6 address for the same ports.</li>
			<li><b>If the WAN address changes</b> (most home connections), set up Services &gt; Dynamic DNS and give players the name instead.</li>
		</ol>`;
		const generic = `<ol class="list-decimal ml-5 space-y-2">
			<li>Sign in to your router (usually <code>http://${esc(d.members[0].gateway || '192.168.1.1')}</code>). The setting is called <b>Port Forwarding</b>, <b>Virtual Servers</b>, <b>NAT</b> or <b>Applications &amp; Gaming</b>, depending on the make.</li>
			<li><b>Give each machine a fixed address</b> first, so the forwards keep working after a restart: look for <b>DHCP reservation</b>, <b>Static lease</b> or <b>Address reservation</b>, for ${esc(ips || 'each machine')}.</li>
			<li>Add one forward per line in <b>Forward these</b>: the outside (external/public/WAN) port, the protocol, the inside (internal/private/LAN) address and port. If a line needs both TCP and UDP, pick <b>Both</b> or add two forwards.</li>
			<li>Leave <b>UPnP</b> as it is: the panel doesn't need it, and it doesn't open anything itself.</li>
			<li>Some routers have a separate firewall page; if so, allow the same ports there too.</li>
			<li>Test from outside: a phone on mobile data (Wi-Fi off), joining the address under <b>Players outside join with</b>. Many home routers can't loop back, so the internet address may not work from inside your home even when everything is right.</li>
		</ol>`;
		const howCard = `<div class="${card}">
			<h3 class="${h}">How to set it up</h3>
			<div class="rp-tabs flex border-b border-gray-200 dark:border-gray-700 mb-3">${tabBtn('pfsense', 'pfSense')}${tabBtn('generic', 'Other routers')}</div>
			<div class="text-sm text-gray-700 dark:text-gray-300">${this.portsRouter === 'generic' ? generic : pfsense}</div>
		</div>`;

		document.getElementById('ports-body').innerHTML = addressCard + clashCard + forwardCard + howCard + optionalCard + closedCard;
	},

	openCluster() {
		this.hideDashboard();
		this.activeServer = null;
		this.renderSidebar();
		document.getElementById('workspace').classList.add('hidden');
		document.getElementById('cluster-view').classList.remove('hidden');
		this.setMobileTitle('🌐 Cluster', this.panelName);
		this.loadCluster();
		clearInterval(this.clusterTimer);
		this.clusterTimer = setInterval(() => { if (!document.hidden) this.loadCluster(); }, 15000);
	},

	closeCluster() {
		this.closePorts();
		clearInterval(this.clusterTimer);
		this.clusterTimer = null;
		document.getElementById('cluster-view').classList.add('hidden');
	},

	async loadCluster() {
		const box = document.getElementById('cluster-nodes');
		const res = await fetch('/api/cluster/overview');
		if (!res.ok) {
			box.innerHTML = `<p class="text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`;
			return;
		}
		this.clusterData = await res.json();
		try {
			const h = await fetch('/api/cluster/hosts');
			this.clusterHosts = h.ok ? await h.json() : null;
		} catch (e) { this.clusterHosts = null; }
		this.renderCluster();
	},

	// A value for an inline onclick="...(here)" handler: JSON, then HTML-escaped
	jsArg(v) {
		return this.escapeHtml(JSON.stringify(String(v)));
	},

	renderCluster() {
		const d = this.clusterData, esc = t => this.escapeHtml(t);
		const all = d.nodes.flatMap(n => n.servers || []);
		document.getElementById('cluster-title').innerText = `Cluster: ${d.cluster}`;
		document.getElementById('cluster-subtitle').innerText =
			`${d.nodes.length} panel${d.nodes.length === 1 ? '' : 's'} · ${all.filter(s => s.status === 'online').length} of ${all.length} servers online · refreshes every 15 seconds`;
		const btn = 'px-3 py-1 rounded text-xs font-bold text-white transition';
		document.getElementById('cluster-nodes').innerHTML = d.nodes.map(n => {
			const ok = n.state === 'ok';
			const rows = (n.servers || []).map(s => `
				<div class="flex items-center justify-between gap-3 p-3 border-t border-gray-200 dark:border-gray-700">
					<div class="flex items-center gap-3 min-w-0">
						<span class="${s.status === 'online' ? 'rp-lamp-on' : 'rp-lamp-off'}">●</span>
						<div class="min-w-0">
							<div class="font-bold text-gray-900 dark:text-gray-200 truncate">${esc(s.name)}</div>
							<div class="text-xs text-gray-500">${esc(s.edition)}${s.version ? ' ' + esc(s.version) : ''} • Port ${esc(s.port)}${this.updateBadge(s)}</div>
						</div>
					</div>
					<div class="flex flex-wrap justify-end gap-2">
						${ok && (n.local || (n.cluster_api || 1) >= 2) ? `<button onclick="ServerApp.openClusterServer(${this.jsArg(n.local ? '' : n.id)}, ${this.jsArg(s.name)})" class="${btn} bg-gray-500 hover:bg-gray-600">Open</button>` : ''}
						${this.canMoveFrom(n) ? `<button onclick="ServerApp.openMoveDialog('move', ${this.jsArg(n.id)}, ${this.jsArg(s.name)})" title="Move it to another cluster member" class="${btn} bg-gray-500 hover:bg-gray-600">Move…</button>
						<button onclick="ServerApp.openMoveDialog('copy', ${this.jsArg(n.id)}, ${this.jsArg(s.name)})" title="Copy it, to another member or this one" class="${btn} bg-gray-500 hover:bg-gray-600">Copy…</button>` : ''}
						<button onclick="ServerApp.clusterPower(${this.jsArg(n.id)}, ${this.jsArg(s.name)}, 'start', this)" class="${btn} bg-green-600 hover:bg-green-700">Start</button>
						<button onclick="ServerApp.clusterPower(${this.jsArg(n.id)}, ${this.jsArg(s.name)}, 'restart', this)" class="${btn} bg-theme hover-bg-theme">Restart</button>
						<button onclick="ServerApp.clusterPower(${this.jsArg(n.id)}, ${this.jsArg(s.name)}, 'stop', this)" class="${btn} bg-red-600 hover:bg-red-700">Stop</button>
					</div>
				</div>`).join('');
			return `
				<div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow">
					<div class="flex items-center justify-between gap-3 p-4">
						<div class="min-w-0">
							<h3 class="font-bold text-lg text-gray-900 dark:text-white truncate">
								<span class="${ok ? 'text-green-500' : 'text-yellow-500'}">●</span> ${esc(n.name)}
								${n.local ? '<span class="text-xs font-normal text-gray-500 dark:text-gray-400">(this panel, the master)</span>' : ''}
							</h3>
							${n.local ? '' : `<p class="text-xs text-gray-500 dark:text-gray-400 font-mono">${esc(n.url)} · RedstonePanel ${esc(n.version || '?')}</p>`}
							${this.nodeResources(n.id)}
						</div>
						<div class="flex flex-wrap justify-end items-center gap-3">
						${this.canOpenNodeTerminal(n) ? `<button onclick="ServerApp.openTerminal(${this.jsArg(n.local ? '' : n.id)}, ${this.jsArg(n.name)})" title="A shell on this machine, as a Linux account" class="${btn} bg-gray-500 hover:bg-gray-600">⌨️ Terminal</button>` : ''}
						${n.local ? '' : `<a href="${esc(n.url)}/" target="_blank" rel="noopener" class="text-sm underline text-theme whitespace-nowrap">Open dashboard ↗</a>`}
						</div>
					</div>
					${ok ? '' : `<p class="mx-4 mb-3 text-sm p-2 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200">⚠️ ${esc(this.clusterStates[n.state] || n.state)}${n.error ? ` <span class="text-xs">(${esc(n.error)})</span>` : ''}</p>`}
					${rows || (ok ? '<p class="px-4 pb-4 text-sm text-gray-500 dark:text-gray-400">No Minecraft servers on this panel yet.</p>' : '')}
				</div>`;
		}).join('');
	},

	// --- 1.16: move or copy a server to another cluster member (master admins) ---
	canMoveFrom(n) {
		return this.currentUser && this.currentUser.role === 'admin' && n.state === 'ok'
			&& (n.local || (n.cluster_api || 1) >= 3);
	},

	moveTargets(kind, from) {
		return (this.clusterData ? this.clusterData.nodes : [])
			.filter(n => n.state === 'ok' && (n.local || (n.cluster_api || 1) >= 3) && (kind === 'copy' || n.id !== from));
	},

	// 1.16.13: a pre-copy needs both ends on cluster contract 5 (the master itself always is)
	precopyBlocked(from) {
		const old = n => n && !n.local && (n.cluster_api || 1) < 5;
		const nodes = this.clusterData ? this.clusterData.nodes : [];
		return old(nodes.find(n => n.id === from)) || nodes.some(n => old(n));
	},

	openMoveDialog(kind, from, server) {
		const esc = t => this.escapeHtml(t);
		const targets = this.moveTargets(kind, from);
		if (!targets.length) {
			return alert('No other cluster member can take it. Each one needs RedstonePanel 1.16 or later and must be reachable from the master.');
		}
		const src = this.clusterData.nodes.find(n => n.id === from) || { name: '?' };
		const move = kind === 'move';
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		document.getElementById('modal-content').innerHTML = `
			<h2 class="text-xl font-bold text-gray-900 dark:text-white mb-2">${move ? 'Move' : 'Copy'} ${esc(server)}</h2>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3">${move
				? `From <b>${esc(src.name)}</b>. The server stops while its folder is copied, then starts on the new member with the same name and ports if they're free there. Its memory, Java, backup and restart schedules and Phantom come along. The files on ${esc(src.name)} stay as a removed server until you delete them, so nothing is lost if something goes wrong.`
				: `From <b>${esc(src.name)}</b>, without stopping it (the world is saved first, as backups do). The copy gets its own name and ports where needed, and no schedules.`}</p>
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">To</label>
			<select id="move-to" class="${field} mb-3">${targets.map(n =>
				`<option value="${esc(n.id)}">${esc(n.name)}${n.local ? ' (this panel)' : ''}${n.id === from ? ' (same panel)' : ''}</option>`).join('')}</select>
			${move ? `<label class="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300 mb-3"><input type="checkbox" id="move-precopy" class="mt-1">
				<span><b>Pre-copy while it runs</b> (for big worlds): copy everything first while players stay on, then stop it and copy only what changed. The server is down for seconds instead of minutes.${this.precopyBlocked(from) ? ` <span class="text-xs text-gray-500 dark:text-gray-400">Needs RedstonePanel 1.16.13 or later on both members.</span>` : ''}</span></label>`
				: `<label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 mb-3"><input type="checkbox" id="move-start"> Start the copy when it's ready</label>`}
			<div class="flex gap-2">
				<button id="move-go" onclick="ServerApp.startMove(${this.jsArg(kind)}, ${this.jsArg(from)}, ${this.jsArg(server)})" class="flex-1 px-4 py-2 bg-theme hover-bg-theme text-white rounded font-bold">${move ? 'Move' : 'Copy'}</button>
				<button onclick="ServerApp.closeModal()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Cancel</button>
			</div>`;
		document.getElementById('modal-overlay').classList.remove('hidden');
	},

	async startMove(kind, from, server) {
		const to = document.getElementById('move-to').value;
		const startBox = document.getElementById('move-start');
		const preBox = document.getElementById('move-precopy');
		document.getElementById('move-go').disabled = true;
		const res = await fetch('/api/cluster/moves', {
			method: 'POST', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ kind, server, from_node: from, to_node: to, start: !!(startBox && startBox.checked),
				precopy: !!(preBox && preBox.checked) })
		});
		if (!res.ok) {
			document.getElementById('move-go').disabled = false;
			return alert(await this.apiError(res));
		}
		this.watchMove(await res.json());
	},

	async watchMove(job) {
		this.renderMoveJob(job);
		if (job.state !== 'running') {
			this.loadCluster();
			this.loadServers();             // the sidebar lists servers under their member
			return;
		}
		setTimeout(async () => {
			const res = await fetch(`/api/cluster/moves/${encodeURIComponent(job.id)}`);
			if (res.ok) this.watchMove(await res.json());
		}, 1500);
	},

	renderMoveJob(job) {
		const esc = t => this.escapeHtml(t);
		const box = document.getElementById('modal-content');
		if (!box) return;
		const move = job.kind === 'move';
		const pct = job.total ? Math.min(100, Math.round(job.bytes * 100 / job.total)) : null;
		let body;
		if (job.state === 'running') {
			body = `<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">${esc(job.step)}…</p>
				${pct !== null ? `<div class="w-full h-2 rounded bg-gray-200 dark:bg-gray-700 mb-1"><div class="h-2 rounded bg-theme" style="width:${pct}%"></div></div>
				<p class="text-xs text-gray-500 dark:text-gray-400 mb-3">${this.fmtBytes(job.bytes)} of ${this.fmtBytes(job.total)}</p>` : ''}
				<p class="text-xs text-gray-500 dark:text-gray-400">You can close this; it carries on, and the console shows each step.</p>`;
		} else if (job.state === 'done') {
			const r = job.result || {};
			const changes = [r.renamed ? `its name was taken there, so it's now <b>${esc(r.name)}</b>` : '',
				r.old_port ? `port ${esc(r.old_port)} was taken there, so it now uses port <b>${esc(r.port)}</b>` : ''].filter(Boolean);
			body = `<p class="text-sm text-green-600 dark:text-green-400 font-bold mb-2">${move ? 'Moved' : 'Copied'} to ${esc(job.to.name)} as ${esc(r.name)} (port ${esc(r.port)}).</p>
				${job.downtime !== null && job.downtime !== undefined ? `<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">It was down for ${esc(job.downtime < 60 ? `${job.downtime} s` : `${Math.floor(job.downtime / 60)} min ${job.downtime % 60} s`)}${job.precopy ? ' (with a pre-copy)' : ''}.</p>` : ''}
				${changes.length ? `<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">Note: ${changes.join('; ')}.</p>` : ''}
				${job.warning ? `<p class="text-sm p-2 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200 mb-2">⚠️ ${esc(job.warning)}</p>` : ''}
				${move && !job.warning ? `<p class="text-sm text-gray-700 dark:text-gray-300 mb-3">The old files on ${esc(job.from.name)} are kept as a removed server, ${esc(job.server)}. Once the server works on ${esc(job.to.name)}, delete them to free the space.</p>
				<button onclick="ServerApp.deleteMovedCopy(${this.jsArg(job.from.id)}, ${this.jsArg(job.server)}, this)" class="w-full mb-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded font-bold">Delete the old copy on ${esc(job.from.name)}</button>` : ''}`;
		} else {
			body = `<p class="text-sm text-red-500 font-bold mb-2">${move ? 'Moving' : 'Copying'} failed: ${esc(job.error)}</p>
				<p class="text-sm text-gray-700 dark:text-gray-300 mb-3">${esc(job.step)}</p>`;
		}
		box.innerHTML = `
			<h2 class="text-xl font-bold text-gray-900 dark:text-white mb-1">${move ? 'Moving' : 'Copying'} ${esc(job.server)}${job.precopy ? ' <span class="text-xs font-normal text-gray-500 dark:text-gray-400">with a pre-copy</span>' : ''}</h2>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-3">${esc(job.from.name)} → ${esc(job.to.name)}</p>
			${body}
			<button onclick="ServerApp.closeModal()" class="w-full px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Close</button>`;
	},

	async deleteMovedCopy(member, name, button) {
		if (!confirm(`Permanently delete the old copy of ${name} (the files the move left behind)? This can't be undone.`)) return;
		if (button) button.disabled = true;
		const res = await fetch(`/api/cluster/moved/${encodeURIComponent(member || 'local')}/${encodeURIComponent(name)}`, { method: 'DELETE' });
		if (!res.ok) {
			if (button) button.disabled = false;
			return alert(`Not deleted: ${await this.apiError(res)}`);
		}
		if (button) { button.innerText = 'Deleted'; }
		if (document.getElementById('removed-servers')) await this.loadRemovedServers();
	},

	// The master's template manager: copy a template to a node, or a node's template here
	async renderTemplateCopy(temp) {
		const box = document.getElementById('temp-copy');
		if (!box) return;
		const nodes = this.isMaster() ? this.clusterNodes.filter(n => n.state === 'ok' && (n.cluster_api || 1) >= 2) : [];
		box.innerHTML = nodes.length ? `
			<select id="temp-copy-to" class="bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white">
				${nodes.map(n => `<option value="${this.escapeHtml(n.id)}">${this.escapeHtml(n.name)}</option>`).join('')}</select>
			<button onclick="ServerApp.copyTemplateTo(${this.jsArg(temp.id)})" class="bg-gray-500 hover:bg-gray-600 text-white px-4 py-2 rounded font-bold">Copy to node</button>` : '';
	},

	async copyTemplateTo(id) {
		const node = document.getElementById('temp-copy-to').value;
		if (await this.pushTemplate(node, id)) {
			const n = this.clusterNodes.find(x => x.id === node);
			alert(`Copied to ${n ? n.name : 'the node'}.`);
		}
	},

	async renderNodeTemplates(aside) {
		if (!this.isMaster()) return;
		const esc = t => this.escapeHtml(t);
		const nodes = this.clusterNodes.filter(n => n.state === 'ok' && (n.cluster_api || 1) >= 3);
		const lists = await Promise.all(nodes.map(async n => {
			try {
				const r = await fetch(`/api/cluster/nodes/${encodeURIComponent(n.id)}/proxy/api/templates`);
				return { n, list: r.ok ? await r.json() : [] };
			} catch (e) { return { n, list: [] }; }
		}));
		const mine = new Set(this.templates.map(t => t.id));
		const html = lists.map(({ n, list }) => {
			const own = (Array.isArray(list) ? list : []).filter(t => t && !t.from_master && !mine.has(t.id));
			if (!own.length) return '';
			return `<h3 class="font-bold text-gray-500 mt-4 mb-2">On ${esc(n.name)}</h3>` + own.map(t => `
				<div class="p-3 mb-1 rounded border border-gray-200 dark:border-gray-800">
					<div class="font-bold text-gray-900 dark:text-gray-200">${esc(t.name)}</div>
					<div class="text-xs text-gray-500 mb-1">${esc(t.edition)} Template</div>
					<button onclick="ServerApp.copyTemplateHere(${this.jsArg(n.id)}, ${this.jsArg(t.id)})" class="text-xs underline text-theme">Copy to this panel</button>
				</div>`).join('');
		}).join('');
		if (html && document.getElementById('template-list-aside') === aside) aside.insertAdjacentHTML('beforeend', html);
	},

	async copyTemplateHere(node, id) {
		const res = await fetch('/api/cluster/nodes/local/templates', {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: id, from_node: node })
		});
		if (!res.ok) return alert(`Not copied: ${await this.apiError(res)}`);
		await this.fetchTemplates();
		this.renderTemplateSidebar();
	},

	// "CPU 12% · 3.1 GB memory free · 40 GB disk free" for a node on the cluster page
	nodeResources(id) {
		const h = this.clusterHosts && this.clusterHosts.nodes.find(x => x.id === id);
		const s = h && h.summary;
		if (!s) return '';
		const parts = [`CPU ${this.fmtPct(s.cpu_percent)}`, `${this.fmtBytes((s.memory || {}).available)} memory free`];
		if (s.disk) parts.push(`${this.fmtBytes(s.disk.free)} disk free`);
		if (s.attention.length) parts.push(`⚠️ ${s.attention.length} need${s.attention.length === 1 ? 's' : ''} attention`);
		return `<p class="text-xs text-gray-500 dark:text-gray-400">${parts.join(' · ')}</p>`;
	},

	// The cluster page's "Open": the server's own tabs, wherever it lives
	async openClusterServer(nodeId, name) {
		const find = () => this.servers.find(s => s.name === name && (s.node || '') === nodeId);
		if (!find()) await this.loadServers();
		const srv = find();
		if (srv) this.openWorkspace(srv);
	},

	async clusterPower(nodeId, server, action, button) {
		if (action === 'stop' && !confirm(`Stop ${server}? Players on it will be disconnected.`)) return;
		button.disabled = true;
		const res = await fetch(`/api/cluster/nodes/${encodeURIComponent(nodeId)}/servers/${encodeURIComponent(server)}/action`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ action })
		});
		if (!res.ok) alert(await this.apiError(res));
		setTimeout(() => this.loadCluster(), 1500);
	},

	// --- Panel Settings > Cluster (admins) ---
	fmtTime(t) {
		return t ? new Date(t * 1000).toLocaleString() : 'never';
	},

	async loadClusterSettings() {
		const box = document.getElementById('set-cluster');
		if (!box) return;
		const res = await fetch('/api/settings/cluster');
		if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const d = this.clusterSettings = await res.json();
		const esc = t => this.escapeHtml(t);
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const small = 'text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-3 py-1.5 rounded font-bold transition';
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		const caLine = d.ca ? `<p class="${note} mb-3">Cluster CA: SHA-256 <span class="font-mono break-all">${esc(d.ca.sha256 || '?')}</span> · <a href="/api/cluster/ca.crt" class="underline text-theme">Download</a> and import it on your devices once to trust every panel in the cluster. This panel's certificate expires ${esc(this.fmtTime(d.cert_expires))}.</p>` : '';

		if (d.role === 'standalone') {
			box.innerHTML = `
				<p class="${note} mb-3">A cluster lets one panel (the master) see and control the game servers of other RedstonePanels (its nodes). Cluster panels use HTTPS with certificates from the master, and restart when you create or join one.</p>
				<div class="grid grid-cols-2 gap-3">
					<div class="p-3 rounded border border-gray-200 dark:border-gray-700">
						<p class="font-bold text-sm text-gray-900 dark:text-white mb-2">Make this panel a master</p>
						<input id="cl-name" maxlength="40" placeholder="Cluster name, e.g. Home" class="${field} mb-2">
						<button onclick="ServerApp.createCluster()" class="${small}">Create cluster</button>
					</div>
					<div class="p-3 rounded border border-gray-200 dark:border-gray-700">
						<p class="font-bold text-sm text-gray-900 dark:text-white mb-2">Join a cluster</p>
						<textarea id="cl-code" rows="2" placeholder="rpjoin1:... (from the master: Add node)" class="${field} mb-2 font-mono"></textarea>
						<button onclick="ServerApp.joinCluster()" class="${small}">Join</button>
					</div>
				</div>
				<p id="cl-error" class="hidden text-sm text-red-500 mt-2"></p>`;
			return;
		}

		if (d.role === 'node') {
			box.innerHTML = `
				<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">This panel is a <b>node</b> of cluster <b>${esc(d.name)}</b>. Its master, <b>${esc(d.master_name)}</b> (<span class="font-mono">${esc(d.master_url)}</span>), can see and manage its game servers (every tab, creating servers, Java and Phantom). It can't change this panel's users, settings or HTTPS, or erase a server's files.</p>
				<p class="${note} mb-2">Last contact from the master: ${esc(this.fmtTime(d.last_used))}${d.last_ip ? ' from ' + esc(d.last_ip) : ''}.</p>
				${caLine}
				<button onclick="ServerApp.leaveCluster(false)" class="${small}">Leave cluster</button>
				<p id="cl-error" class="hidden text-sm text-red-500 mt-2"></p>`;
			return;
		}

		// master
		const nodeRows = d.nodes.map(n => `
			<div class="flex items-center justify-between gap-2 p-2 border-t border-gray-200 dark:border-gray-700">
				<div class="min-w-0">
					<p class="text-sm font-bold text-gray-900 dark:text-white truncate">${esc(n.name)} <span class="font-normal font-mono text-xs text-gray-500">${esc(n.url)}</span></p>
					<p class="${note}">RedstonePanel ${esc(n.version || '?')}${(n.cluster_api || 1) < 2 ? ' (update it to manage its servers from here)' : ''} · certificate expires ${esc(this.fmtTime(n.cert_expires))}${n.verify === 'cluster' ? ' (renewed automatically ' + esc(d.renew_within_days) + ' days before)' : ''} · last answered ${esc(this.fmtTime(n.last_ok))}</p>
					<p class="${note}">Certificate checked with
						<select onchange="ServerApp.setNodeVerify(${this.jsArg(n.id)}, this.value)" class="bg-transparent border border-gray-300 dark:border-gray-600 rounded text-xs">
							<option value="cluster" ${n.verify === 'cluster' ? 'selected' : ''}>the cluster CA</option>
							<option value="system" ${n.verify === 'system' ? 'selected' : ''}>the system's trust store (its own public certificate)</option>
						</select></p>
					${n.renew_error ? `<p class="text-xs text-red-500">Automatic renewal failed: ${esc(n.renew_error)}</p>` : ''}
				</div>
				<div class="flex gap-2">
					${n.verify === 'cluster' ? `<button onclick="ServerApp.renewNodeCert(${this.jsArg(n.id)}, this)" class="${small}">Renew cert</button>` : ''}
					<button onclick="ServerApp.removeNode(${this.jsArg(n.id)}, ${this.jsArg(n.name)})" class="${small}">Remove</button>
				</div>
			</div>`).join('');
		const joins = d.joins.map(j => `
			<p class="${note}">Join code ${esc(j.id)}… made by ${esc(j.created_by)}, valid until ${esc(this.fmtTime(j.expires))} · <a href="#" onclick="ServerApp.cancelJoin(${this.jsArg(j.id)}); return false;" class="underline">cancel</a></p>`).join('');
		box.innerHTML = `
			<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">This panel is the <b>master</b> of cluster <b>${esc(d.name)}</b>. Signed-in users manage every node's servers here: they're listed in the sidebar under each node's name, with all their tabs, and on the <b>🌐 Cluster</b> page. Admins here are admins on the nodes too (except for users, settings and cluster membership); users here have a user's rights there.</p>
			${caLine}
			<div class="rounded border border-gray-200 dark:border-gray-700 mb-3">
				<p class="p-2 text-sm font-bold text-gray-900 dark:text-white">Nodes (${d.nodes.length})</p>
				${nodeRows || `<p class="p-2 border-t border-gray-200 dark:border-gray-700 ${note}">No nodes yet.</p>`}
			</div>
			<div class="p-3 rounded border border-gray-200 dark:border-gray-700 mb-3">
				<p class="font-bold text-sm text-gray-900 dark:text-white mb-2">Add a node</p>
				<label class="${note}">Address nodes use to reach this master
					<input id="cl-join-url" value="${esc(d.join_url)}" class="${field} mt-1 mb-2 font-mono"></label>
				<div class="flex flex-wrap gap-2">
					<button onclick="ServerApp.makeJoinCode()" class="${small}">Make a join code</button>
					<button id="cl-find" onclick="ServerApp.findPanels()" class="${small}">Find panels on your network</button>
				</div>
				<div id="cl-found" class="hidden mt-3"></div>
				<div id="cl-join-out" class="hidden mt-3"></div>
				${joins ? `<div class="mt-2">${joins}</div>` : ''}
			</div>
			<div class="p-3 rounded border border-gray-200 dark:border-gray-700 mb-3">
				<p class="font-bold text-sm text-gray-900 dark:text-white mb-1">Backup</p>
				<p class="${note} mb-2">Everything needed to make another panel the master if this one is lost: the cluster CA, the nodes and their tokens, panel accounts and templates. Encrypted with your passphrase. Download a new one after adding or removing nodes, and keep it off this server.</p>
				<div class="flex gap-2">
					<input id="cl-pass" type="password" autocomplete="new-password" placeholder="Passphrase (10+ characters)" class="${field}">
					<button onclick="ServerApp.downloadClusterBackup()" class="${small} whitespace-nowrap">Download backup</button>
				</div>
			</div>
			${this.nightlyHtml(d.nightly, { field, small, note })}
			<div class="flex gap-2">
				<button onclick="ServerApp.renewNodeCert('local', this)" class="${small}">Renew this master's certificate</button>
				${d.nodes.length ? '' : `<button onclick="ServerApp.leaveCluster(true)" class="${small}">Dissolve cluster</button>`}
			</div>
			<p id="cl-error" class="hidden text-sm text-red-500 mt-2"></p>`;
	},

	// Nightly backups: an encrypted copy of the cluster state every night, kept on the master
	nightlyHtml(n, { field, small, note }) {
		const esc = t => this.escapeHtml(String(t ?? ''));
		const files = (n.files || []).map(f => `<a href="/api/settings/cluster/backups/${encodeURIComponent(f.file)}" class="underline text-theme">${esc(f.file.replace(/^redstonepanel-cluster-|\.rpbackup$/g, ''))}</a>`).join(' · ');
		return `
			<div class="p-3 rounded border border-gray-200 dark:border-gray-700 mb-3">
				<p class="font-bold text-sm text-gray-900 dark:text-white mb-1">Nightly backups ${n.enabled ? '<span class="text-green-600 text-xs">on</span>' : '<span class="text-gray-500 text-xs">off</span>'}</p>
				<p class="${note} mb-2">Every night after 3:00 the master writes an encrypted backup to its data folder and keeps the last ${esc(n.keep)}. The passphrase is stored on this master so it can do that unattended. These copies only help while this server's disk survives: still download one now and then.</p>
				${n.last_error ? `<p class="text-xs text-red-500 mb-2">Last nightly backup failed: ${esc(n.last_error)}</p>` : ''}
				${files ? `<p class="${note} mb-2">Download: ${files}</p>` : ''}
				<div class="flex gap-2">
					<input id="cl-nightly-pass" type="password" autocomplete="new-password" placeholder="${n.enabled ? 'New passphrase (optional)' : 'Passphrase (10+ characters)'}" class="${field}">
					<button onclick="ServerApp.setNightly(true)" class="${small} whitespace-nowrap">${n.enabled ? 'Change passphrase' : 'Turn on'}</button>
					${n.enabled ? `<button onclick="ServerApp.setNightly(false)" class="${small} whitespace-nowrap">Turn off</button>` : ''}
				</div>
			</div>`;
	},

	async setNightly(enabled) {
		const passphrase = enabled ? document.getElementById('cl-nightly-pass').value : null;
		if (enabled && !passphrase && !this.clusterSettings.nightly.enabled) return this.clusterError('Give the passphrase the backups are encrypted with.');
		if (enabled && !passphrase) return;
		const data = await this.clusterPost('/api/settings/cluster/nightly', { enabled, passphrase }, 'PUT');
		if (data) this.loadClusterSettings();
	},

	async setNodeVerify(id, verify) {
		const data = await this.clusterPost(`/api/settings/cluster/nodes/${encodeURIComponent(id)}`, { verify }, 'PATCH');
		if (data) this.loadClusterSettings();
	},

	clusterError(msg) {
		const el = document.getElementById('cl-error');
		if (!el) return alert(msg);
		el.innerText = msg;
		el.classList.toggle('hidden', !msg);
	},

	async clusterPost(url, body, method = 'POST') {
		const res = await fetch(url, {
			method,
			headers: { 'Content-Type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body)
		});
		if (!res.ok) { this.clusterError(await this.apiError(res)); return null; }
		return res.json();
	},

	async createCluster() {
		const name = document.getElementById('cl-name').value.trim();
		if (!name) return this.clusterError('Give the cluster a name.');
		if (!confirm(`Create cluster "${name}" with this panel as its master? The panel restarts on HTTPS.`)) return;
		const data = await this.clusterPost('/api/settings/cluster', { name });
		if (data) this.showRestarting(data.url, 'Your browser will warn about the certificate until you import the cluster CA from Panel Settings.');
	},

	async joinCluster() {
		const code = document.getElementById('cl-code').value.trim();
		if (!code) return this.clusterError('Paste the join code from the master.');
		if (!confirm('Join this cluster? Its master will be able to see and control this panel\'s game servers. The panel restarts on HTTPS.')) return;
		this.clusterError('Joining...');
		const data = await this.clusterPost('/api/settings/cluster/join', { code });
		if (data) this.showRestarting(data.url, 'Your browser will warn about the certificate until you import the cluster CA.');
	},

	async leaveCluster(dissolve) {
		if (!confirm(dissolve ? 'Dissolve the cluster? This panel goes back to standalone with self-signed HTTPS.'
			: 'Leave the cluster? The master loses access to this panel\'s servers, and this panel goes back to self-signed HTTPS. Remove it on the master too.')) return;
		const data = await this.clusterPost('/api/settings/cluster/leave', {});
		if (data) this.showRestarting(data.url, '');
	},

	// 1.16.13: Find panels on your network (RedstonePanels on this master's private networks)
	async findPanels() {
		const box = document.getElementById('cl-found'), button = document.getElementById('cl-find');
		const esc = t => this.escapeHtml(String(t ?? ''));
		const note = 'text-xs text-gray-500 dark:text-gray-400';
		box.innerHTML = `<p class="${note}">Looking on your network for other RedstonePanels (on ports 8443 and 8080). This takes up to half a minute…</p>`;
		box.classList.remove('hidden');
		button.disabled = true;
		const d = await this.clusterPost('/api/settings/cluster/discover');
		button.disabled = false;
		if (!d) { box.classList.add('hidden'); return; }
		const state = p => p.node ? `already a node here (${esc(p.node)})`
			: p.cluster === 'master' ? 'the master of another cluster'
			: p.cluster === 'node' ? 'a node of another cluster'
			: p.cluster === 'standalone' ? 'not in a cluster' : 'an older RedstonePanel (before 1.16.13)';
		const canAdd = p => !p.node && (p.cluster === 'standalone' || !p.cluster);
		const rows = d.panels.map(p => `
			<div class="flex items-center justify-between gap-2 py-2 border-t border-gray-200 dark:border-gray-700">
				<div class="min-w-0">
					<p class="text-sm font-bold text-gray-900 dark:text-white truncate">${esc(p.name)} <a href="${esc(p.url)}/" target="_blank" rel="noopener" class="font-normal font-mono text-xs underline text-theme">${esc(p.url)} ↗</a></p>
					<p class="${note}">${state(p)}</p>
				</div>
				${canAdd(p) ? `<button onclick="ServerApp.makeJoinCode(${this.jsArg(p.name)}, ${this.jsArg(p.url)})" class="text-xs bg-theme hover-bg-theme text-white px-3 py-1.5 rounded font-bold whitespace-nowrap">Add to cluster</button>` : ''}
			</div>`).join('');
		box.innerHTML = `
			<p class="${note} mb-1">Searched ${esc(d.networks.join(', '))} in ${esc(d.seconds)} s: ${d.panels.length ? `found ${d.panels.length} other panel${d.panels.length === 1 ? '' : 's'}.` : 'no other RedstonePanels answered. A panel behind a firewall, or on another port, can still join with a join code.'}</p>
			${rows}`;
	},

	async makeJoinCode(forName, forUrl) {
		const url = document.getElementById('cl-join-url').value.trim();
		const j = await this.clusterPost('/api/settings/cluster/joins', { url });
		if (!j) return;
		const esc = t => this.escapeHtml(t);
		const out = document.getElementById('cl-join-out');
		const where = forUrl ? `<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">To add <b>${esc(forName)}</b>: <a href="${esc(forUrl)}/" target="_blank" rel="noopener" class="underline text-theme">open it ↗</a>, sign in as an admin, and paste this code into <b>Panel Settings › Cluster › Join a cluster</b>. It shows up under Nodes here once it has joined.</p>` : '';
		out.innerHTML = where + `
			<p class="text-xs text-yellow-700 dark:text-yellow-300 mb-1">Shown only once. Works once, until ${esc(this.fmtTime(j.expires))}. Anyone with it can add a server to this cluster, so treat it like a password.</p>
			<p class="text-xs text-gray-500 dark:text-gray-400">On the new server (with RedstonePanel installed), run:</p>
			<textarea readonly rows="3" onclick="this.select()" class="w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-xs font-mono text-gray-900 dark:text-white mb-1">${esc(j.command)}</textarea>
			<p class="text-xs text-gray-500 dark:text-gray-400">or paste just the code into that panel's Panel Settings > Cluster > Join:</p>
			<textarea readonly rows="3" onclick="this.select()" class="w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-xs font-mono text-gray-900 dark:text-white">${esc(j.code)}</textarea>`;
		out.classList.remove('hidden');
	},

	async cancelJoin(id) {
		const res = await fetch(`/api/settings/cluster/joins/${encodeURIComponent(id)}`, { method: 'DELETE' });
		if (!res.ok) return this.clusterError(await this.apiError(res));
		this.loadClusterSettings();
	},

	async removeNode(id, name) {
		if (!confirm(`Remove ${name} from the cluster? It goes back to being a standalone panel (if it can be reached).`)) return;
		const res = await fetch(`/api/settings/cluster/nodes/${encodeURIComponent(id)}`, { method: 'DELETE' });
		if (!res.ok) return this.clusterError(await this.apiError(res));
		const d = await res.json();
		if (!d.node_confirmed) alert(`${name} was removed here, but couldn't be told (${d.problem}). Run "sudo redstonepanel leave" on it.`);
		this.loadClusterSettings();
	},

	async renewNodeCert(id, button) {
		if (id === 'local' && !confirm('Renew this master\'s certificate? The panel restarts.')) return;
		button.disabled = true;
		const data = await this.clusterPost(`/api/settings/cluster/nodes/${encodeURIComponent(id)}/renew`);
		button.disabled = false;
		if (!data) return;
		if (data.restart) return this.showRestarting(data.url, '');
		this.loadClusterSettings();
	},

	async downloadClusterBackup() {
		const passphrase = document.getElementById('cl-pass').value;
		const res = await fetch('/api/settings/cluster/backup', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ passphrase })
		});
		if (!res.ok) return this.clusterError(await this.apiError(res));
		const name = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
		const a = document.createElement('a');
		a.href = URL.createObjectURL(await res.blob());
		a.download = name ? name[1] : 'redstonepanel-cluster.rpbackup';
		document.body.appendChild(a);
		a.click();
		a.remove();
		document.getElementById('cl-pass').value = '';
		this.clusterError('');
	},

	async logout() {
		await fetch('/api/auth/logout', { method: 'POST' });
		window.location.replace('/static/login.html');
	},

	async apiError(res) {
		let detail = `HTTP ${res.status}`;
		try { detail = (await res.json()).detail || detail; } catch (e) {}
		return detail;
	},

	closeModal() {
		document.getElementById('modal-overlay').classList.add('hidden');
		this.modalWide(false);
	},

	// The modpack dialog is a list to read, so it gets a wider box than the other dialogs
	modalWide(on) {
		const c = document.getElementById('modal-content');
		c.classList.toggle('max-w-2xl', on);
		c.classList.toggle('max-w-md', !on);
	},

	openChangePasswordModal() {
		this.openAccountModal({});
	},

	// 🔑 Password: the signed-in account's password (panel accounts) and two-factor sign-in (all accounts)
	async openAccountModal(opts) {
		this.accountForced = !!(opts && opts.forced);
		const me = this.currentUser || {};
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-gray-900 dark:text-white mb-3';
		const grey = 'px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white';
		const password = me.source === 'panel' && !this.accountForced ? `<div id="pw-section">
			<h3 class="text-xl font-bold mb-4 text-gray-900 dark:text-white">Change Password</h3>
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">Current password</label>
			<input type="password" id="pw-current" autocomplete="current-password" class="${field}">
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">New password (8+ characters)</label>
			<input type="password" id="pw-new" autocomplete="new-password" class="${field}">
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">Repeat new password</label>
			<input type="password" id="pw-new2" autocomplete="new-password" class="${field}">
			<p id="pw-error" class="hidden text-sm text-red-500 mb-3"></p>
			<button onclick="ServerApp.changeOwnPassword()" class="w-full px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold mb-6">Change Password</button></div>`
			: (me.source === 'linux' && !this.accountForced ? `<div id="pw-section">
			<h3 class="text-xl font-bold mb-2 text-gray-900 dark:text-white">Your Account</h3>
			<p class="text-sm text-gray-500 dark:text-gray-400 mb-6">This is a Linux account: change its password on the server with <span class="font-mono">passwd</span>.</p></div>` : '');
		const forcedIntro = this.accountForced ? `
			<h3 class="text-xl font-bold mb-2 text-gray-900 dark:text-white">Set Up Two-Factor Sign-In</h3>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-4">This panel requires a second step at sign-in for admins. Set it up now to continue.</p>` : '';
		const footer = this.accountForced
			? `<button onclick="ServerApp.logout()" class="mt-4 w-full ${grey}">Sign out</button>`
			: `<button onclick="ServerApp.closeModal()" class="mt-4 w-full ${grey}">Close</button>`;
		document.getElementById('modal-content').innerHTML = `
			${password}${forcedIntro}
			<div id="tf-box"><p class="text-sm text-gray-500">Loading two-factor sign-in...</p></div>
			${this.accountForced ? '' : '<div id="ssh-box" class="mt-6"></div>'}
			${footer}`;
		document.getElementById('modal-overlay').classList.remove('hidden');
		const res = await fetch('/api/auth/2fa');
		if (!res.ok) { this.tfBox(`<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`); return; }
		this.tfStatus = await res.json();
		if (this.accountForced && !this.tfStatus.enabled) return this.startTwoFactorSetup();
		this.renderTwoFactor();
		this.loadSshKeys();
	},

	// --- 1.18: SSH keys for SFTP (🔑 Password → SSH keys) ---
	async loadSshKeys(message, ok) {
		const box = document.getElementById('ssh-box');
		if (!box) return;
		const res = await fetch('/api/auth/ssh-keys');
		if (!res.ok) { box.innerHTML = ''; return; }
		const d = await res.json();
		const esc = t => this.escapeHtml(String(t ?? ''));
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white mb-2';
		const keys = d.keys.map(k => `
			<div class="flex items-start justify-between gap-2 p-2 mb-2 rounded border border-gray-200 dark:border-gray-700">
				<div class="min-w-0 text-sm">
					<div class="font-bold text-gray-900 dark:text-white truncate">${esc(k.label || 'Key')} <span class="font-normal text-xs text-gray-500">${esc(k.type.replace('ssh-', '').replace('ecdsa-sha2-', 'ecdsa '))}</span></div>
					<div class="text-xs font-mono text-gray-500 dark:text-gray-400 break-all">${esc(k.fingerprint)}</div>
				</div>
				<button data-fp="${esc(k.fingerprint)}" onclick="ServerApp.removeSshKey(this.dataset.fp)" class="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white shrink-0">Remove</button>
			</div>`).join('');
		box.innerHTML = `
			<h4 class="font-bold text-gray-900 dark:text-white mb-1">SSH keys</h4>
			<p class="text-sm text-gray-500 dark:text-gray-400 mb-3">For SFTP (FileZilla, WinSCP): sign in with a key instead of your password.${(this.tfStatus || {}).enabled ? ' <b>With two-factor on, SFTP only accepts a key.</b>' : ''}</p>
			${keys}
			<textarea id="ssh-key" rows="3" placeholder="ssh-ed25519 AAAA… (your public key, the .pub file)" autocapitalize="none" autocomplete="off" spellcheck="false" class="${field} font-mono"></textarea>
			<input id="ssh-label" maxlength="60" placeholder="Name for this key (optional), e.g. Laptop" class="${field}">
			<p id="ssh-msg" class="${message ? '' : 'hidden'} text-sm ${ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'} mb-2">${esc(message || '')}</p>
			<button onclick="ServerApp.addSshKey()" class="w-full px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Add Key</button>`;
	},

	async addSshKey() {
		const res = await fetch('/api/auth/ssh-keys', {
			method: 'POST', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ key: document.getElementById('ssh-key').value, label: document.getElementById('ssh-label').value })
		});
		if (!res.ok) {
			const msg = document.getElementById('ssh-msg');
			msg.innerText = await this.apiError(res);
			msg.className = 'text-sm text-red-500 mb-2';
			return;
		}
		this.loadSshKeys('Key added.', true);
	},

	async removeSshKey(fingerprint) {
		if (!confirm('Remove this key? SFTP programs using it will stop signing in.')) return;
		const res = await fetch(`/api/auth/ssh-keys/${encodeURIComponent(fingerprint)}`, { method: 'DELETE' });
		this.loadSshKeys(res.ok ? 'Key removed.' : await this.apiError(res), res.ok);
	},

	// The password part hides while setting up or showing codes, so the dialog stays short
	tfBox(html, focused) {
		const box = document.getElementById('tf-box');
		if (box) box.innerHTML = html;
		const pw = document.getElementById('pw-section');
		if (pw) pw.classList.toggle('hidden', !!focused);
		const ssh = document.getElementById('ssh-box');
		if (ssh) ssh.classList.toggle('hidden', !!focused);
	},

	renderTwoFactor() {
		const st = this.tfStatus || {};
		const grey = 'px-3 py-2 text-sm bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white';
		const heading = '<h4 class="font-bold text-gray-900 dark:text-white mb-1">Two-factor sign-in</h4>';
		if (!st.enabled) {
			this.tfBox(`${heading}
				<p class="text-sm text-gray-500 dark:text-gray-400 mb-3">Off. Ask for a six-digit code from an authenticator app (Google Authenticator, Microsoft Authenticator, Aegis, 1Password, Bitwarden...) after your password.${st.required ? ' <b>Required for admins on this panel.</b>' : ''}</p>
				<p id="tf-error" class="hidden text-sm text-red-500 mb-2"></p>
				<button onclick="ServerApp.startTwoFactorSetup()" class="w-full px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold">Set Up Two-Factor</button>`);
			return;
		}
		const low = st.recovery_left <= 3;
		this.tfBox(`${heading}
			<p class="text-sm text-gray-500 dark:text-gray-400 mb-1"><span class="text-green-600 dark:text-green-400 font-bold">On.</span> You'll be asked for a code from your app after your password.</p>
			<p class="text-sm mb-3 ${low ? 'text-yellow-700 dark:text-yellow-300' : 'text-gray-500 dark:text-gray-400'}">${st.recovery_left} of 10 recovery codes left.${low ? ' Make new ones soon.' : ''}</p>
			<label class="block text-xs text-gray-500 dark:text-gray-400 mb-1">To change it, enter a current code from your app</label>
			<input id="tf-code" autocomplete="one-time-code" placeholder="123456" maxlength="20" class="w-full mb-2 bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 font-mono tracking-widest text-gray-900 dark:text-white">
			<p id="tf-error" class="hidden text-sm text-red-500 mb-2"></p>
			<div class="flex gap-2">
				<button onclick="ServerApp.newRecoveryCodes()" class="flex-1 ${grey}">New recovery codes</button>
				${st.required ? '' : `<button onclick="ServerApp.disableTwoFactor()" class="flex-1 px-3 py-2 text-sm bg-red-600 hover:bg-red-700 rounded text-white">Turn off</button>`}
			</div>
			${st.required ? '<p class="text-xs text-gray-500 dark:text-gray-400 mt-2">Required for admins on this panel, so it can\'t be turned off here. New phone? Another admin can reset it (Users), or run <span class="font-mono">sudo redstonepanel reset-2fa NAME</span>.</p>' : ''}`);
	},

	tfError(msg) {
		const err = document.getElementById('tf-error');
		if (!err) return alert(msg);
		err.innerText = msg;
		err.classList.toggle('hidden', !msg);
	},

	async startTwoFactorSetup() {
		const res = await fetch('/api/auth/2fa/setup', { method: 'POST' });
		if (!res.ok) return this.tfError(await this.apiError(res));
		const d = await res.json();
		const key = d.secret.replace(/(.{4})/g, '$1 ').trim();
		const grey = 'px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white';
		this.tfBox(`
			<h4 class="font-bold text-gray-900 dark:text-white mb-1">1. Scan this with your authenticator app</h4>
			<div class="w-48 h-48 mx-auto my-3 rounded overflow-hidden border border-gray-200 dark:border-gray-700">${d.qr_svg}</div>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-1">Can't scan? Add an account by key (time-based) and type:</p>
			<p class="font-mono text-sm text-center select-all break-all bg-gray-100 dark:bg-gray-900 rounded p-2 mb-4 text-gray-900 dark:text-white">${this.escapeHtml(key)}</p>
			<h4 class="font-bold text-gray-900 dark:text-white mb-1">2. Enter the code it shows</h4>
			<input id="tf-code" autocomplete="one-time-code" inputmode="numeric" placeholder="123456" maxlength="7"
				onkeydown="if (event.key === 'Enter') ServerApp.confirmTwoFactor()"
				class="w-full mb-2 bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 font-mono tracking-widest text-lg text-gray-900 dark:text-white">
			<p id="tf-error" class="hidden text-sm text-red-500 mb-2"></p>
			<div class="flex gap-2">
				${this.accountForced ? '' : `<button onclick="ServerApp.cancelTwoFactorSetup()" class="flex-1 ${grey}">Cancel</button>`}
				<button onclick="ServerApp.confirmTwoFactor()" class="flex-1 px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold">Turn On</button>
			</div>`, true);
		const input = document.getElementById('tf-code');
		if (input) input.focus();
	},

	async cancelTwoFactorSetup() {
		await fetch('/api/auth/2fa/disable', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: '' }) });
		this.renderTwoFactor();
	},

	async confirmTwoFactor() {
		const code = document.getElementById('tf-code').value;
		const res = await fetch('/api/auth/2fa/confirm', {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code })
		});
		if (!res.ok) return this.tfError(await this.apiError(res));
		const d = await res.json();
		this.tfStatus = Object.assign({}, this.tfStatus, { enabled: true, recovery_left: d.recovery_codes.length });
		if (this.currentUser) { this.currentUser.two_factor = true; this.currentUser.needs_2fa_setup = false; }
		this.showRecoveryCodes(d.recovery_codes, 'Two-factor sign-in is on.');
	},

	showRecoveryCodes(codes, title) {
		this.recoveryCodes = codes;
		this.tfBox(`
			<h4 class="font-bold text-green-600 dark:text-green-400 mb-1">${this.escapeHtml(title)}</h4>
			<p class="text-sm text-gray-600 dark:text-gray-300 mb-3"><b>Save these recovery codes</b> somewhere safe (a password manager, or printed). Each one signs you in once if you lose your phone. They won't be shown again.</p>
			<ol class="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-sm bg-gray-100 dark:bg-gray-900 rounded p-3 mb-3 text-gray-900 dark:text-white list-decimal list-inside">
				${codes.map(c => `<li>${this.escapeHtml(c)}</li>`).join('')}
			</ol>
			<div class="flex gap-2">
				<button onclick="ServerApp.copyRecoveryCodes()" class="flex-1 px-3 py-2 text-sm bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Copy</button>
				<button onclick="ServerApp.downloadRecoveryCodes()" class="flex-1 px-3 py-2 text-sm bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Download</button>
				<button onclick="ServerApp.recoveryCodesSaved()" class="flex-1 px-3 py-2 text-sm bg-theme hover-bg-theme rounded text-white font-bold">I saved them</button>
			</div>`, true);
	},

	recoveryCodesText() {
		const me = this.currentUser || {};
		return `RedstonePanel recovery codes for ${me.username || ''} on ${document.title}\n`
			+ `Each code signs in once, instead of a code from your authenticator app.\n\n`
			+ (this.recoveryCodes || []).join('\n') + '\n';
	},

	copyRecoveryCodes() {
		const text = this.recoveryCodesText();
		if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).catch(() => prompt('Copy the codes:', text));
		else prompt('Copy the codes:', (this.recoveryCodes || []).join(' '));
	},

	downloadRecoveryCodes() {
		const a = document.createElement('a');
		a.href = URL.createObjectURL(new Blob([this.recoveryCodesText()], { type: 'text/plain' }));
		a.download = 'redstonepanel-recovery-codes.txt';
		document.body.appendChild(a);
		a.click();
		a.remove();
	},

	recoveryCodesSaved() {
		this.recoveryCodes = null;
		if (this.accountForced) { window.location.reload(); return; }
		this.renderTwoFactor();
	},

	async newRecoveryCodes() {
		const code = document.getElementById('tf-code').value;
		const res = await fetch('/api/auth/2fa/recovery-codes', {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code })
		});
		if (!res.ok) return this.tfError(await this.apiError(res));
		const d = await res.json();
		this.tfStatus.recovery_left = d.recovery_codes.length;
		this.showRecoveryCodes(d.recovery_codes, 'New recovery codes. The old ones no longer work.');
	},

	async disableTwoFactor() {
		const code = document.getElementById('tf-code').value;
		if (!code.trim()) return this.tfError('Enter a current code from your app first.');
		if (!confirm('Turn off two-factor sign-in? Your password alone will sign you in.')) return;
		const res = await fetch('/api/auth/2fa/disable', {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code })
		});
		if (!res.ok) return this.tfError(await this.apiError(res));
		this.tfStatus = Object.assign({}, this.tfStatus, { enabled: false, recovery_left: 0 });
		if (this.currentUser) this.currentUser.two_factor = false;
		this.renderTwoFactor();
	},

	async changeOwnPassword() {
		const err = document.getElementById('pw-error');
		const show = msg => { err.innerText = msg; err.classList.remove('hidden'); };
		const current = document.getElementById('pw-current').value;
		const next = document.getElementById('pw-new').value;
		if (next !== document.getElementById('pw-new2').value) return show('The new passwords do not match.');
		const res = await fetch('/api/auth/password', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ current_password: current, new_password: next })
		});
		if (!res.ok) return show(await this.apiError(res));
		this.closeModal();
		alert('Password changed. Other signed-in sessions for this account were signed out.');
	},

	async openUsersModal() {
		const content = document.getElementById('modal-content');
		content.innerHTML = '<p class="text-gray-500">Loading users...</p>';
		document.getElementById('modal-overlay').classList.remove('hidden');
		const res = await fetch('/api/users');
		if (!res.ok) { content.innerHTML = `<p class="text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const data = await res.json();
		const esc = t => this.escapeHtml(t);
		const me = this.currentUser || {};
		const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const when = ts => ts ? new Date(ts * 1000).toLocaleString() : 'never';

		const rows = data.users.length ? data.users.map(u => {
			const self = me.source === 'panel' && me.username === u.username;
			return `
			<div class="p-3 border border-gray-200 dark:border-gray-700 rounded mb-2 ${u.disabled ? 'opacity-60' : ''}">
				<div class="flex items-center justify-between gap-2">
					<div class="min-w-0">
						<span class="font-bold text-gray-900 dark:text-white">${esc(u.username)}</span>
						${u.disabled ? '<span class="text-xs ml-1 px-2 py-0.5 rounded border border-red-400 text-red-500">disabled</span>' : ''}
						${u.two_factor ? '<span class="text-xs ml-1 px-2 py-0.5 rounded border border-green-500 text-green-600 dark:text-green-400" title="Two-factor sign-in is on">2FA</span>' : ''}
						${self ? '<span class="text-xs ml-1 text-gray-500">(you)</span>' : ''}
						<div class="text-xs text-gray-500 dark:text-gray-400">Last sign-in: ${esc(when(u.last_login))}</div>
						<div class="text-xs text-gray-500 dark:text-gray-400">${u.role === 'admin' ? 'Everything on every server' : esc(this.grantsSummary(u.grants))}</div>
					</div>
					<select data-user="${esc(u.username)}" onchange="ServerApp.updateUser(this.dataset.user, {role: this.value})" ${self ? 'disabled title="You can\'t change your own role"' : ''} class="${field}">
						<option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
						<option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option>
					</select>
				</div>
				<div class="flex flex-wrap gap-2 mt-2 text-xs">
					${u.role === 'user' ? `<button data-user="${esc(u.username)}" onclick="ServerApp.openUserGrants(this.dataset.user)" class="px-2 py-1 rounded bg-theme hover-bg-theme text-white font-bold">Servers</button>` : ''}
					<button data-user="${esc(u.username)}" onclick="ServerApp.resetUserPassword(this.dataset.user)" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Reset password</button>
					${u.two_factor && !self ? `<button data-user="${esc(u.username)}" onclick="ServerApp.resetUserTwoFactor(this.dataset.user)" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Reset two-factor</button>` : ''}
					${self ? '' : `<button data-user="${esc(u.username)}" onclick="ServerApp.updateUser(this.dataset.user, {disabled: ${!u.disabled}})" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">${u.disabled ? 'Enable' : 'Disable'}</button>
					<button data-user="${esc(u.username)}" onclick="ServerApp.deleteUser(this.dataset.user)" class="px-2 py-1 rounded bg-red-600 hover:bg-red-700 text-white">Delete</button>`}
				</div>
			</div>`;
		}).join('') : '<p class="text-sm text-gray-500 dark:text-gray-400 mb-2">No panel users yet.</p>';

		content.innerHTML = `
			<h3 class="text-xl font-bold mb-1 text-gray-900 dark:text-white">Panel Users</h3>
			${this.notYet('system-accounts') ? `<p class="text-xs text-gray-500 dark:text-gray-400 mb-4">These accounts are the panel's own, not Windows accounts. Locked out of two-factor? In an administrator prompt on the server: <span class="font-mono">redstonepanel reset-2fa NAME</span>.</p>`
			: `<p class="text-xs text-gray-500 dark:text-gray-400 mb-4">Linux accounts in the ${data.linux_groups.map(g => `<b>${esc(g)}</b>`).join(' or ')} group can also sign in (as admins); they are managed on the server, not here (two-factor for them: <span class="font-mono">sudo redstonepanel reset-2fa NAME</span>).</p>`}
			<div class="max-h-72 overflow-y-auto mb-4">${rows}</div>
			<div class="border-t border-gray-200 dark:border-gray-700 pt-4">
				<p class="text-sm font-bold mb-2 text-gray-900 dark:text-white">Add user</p>
				<input id="new-user-name" placeholder="Username" autocomplete="off" autocapitalize="none" class="w-full mb-2 ${field}">
				<input id="new-user-pass" type="password" placeholder="Password (8+ characters)" autocomplete="new-password" class="w-full mb-2 ${field}">
				<select id="new-user-role" class="w-full mb-2 ${field}">
					<option value="user">User: only the servers you choose</option>
					<option value="admin">Admin: everything, and panel users</option>
				</select>
				<p id="new-user-error" class="hidden text-sm text-red-500 mb-2"></p>
				<button onclick="ServerApp.createUser()" class="w-full bg-theme hover-bg-theme text-white py-2 rounded font-bold">Add User</button>
			</div>
			<button onclick="ServerApp.closeModal()" class="mt-3 w-full px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Close</button>`;
	},

	async createUser() {
		const err = document.getElementById('new-user-error');
		const res = await fetch('/api/users', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				username: document.getElementById('new-user-name').value,
				password: document.getElementById('new-user-pass').value,
				role: document.getElementById('new-user-role').value
			})
		});
		if (!res.ok) { err.innerText = await this.apiError(res); err.classList.remove('hidden'); return; }
		// A new user account can't do anything yet: go straight to choosing its servers
		const name = document.getElementById('new-user-name').value.trim().toLowerCase();
		if (document.getElementById('new-user-role').value === 'user') return this.openUserGrants(name);
		this.openUsersModal();
	},

	// --- 1.18: Users → Servers: what a `user` account may do on each server ---
	grantsSummary(grants) {
		const keys = Object.keys(grants || {});
		if (!keys.length) return 'No servers yet: choose them under Servers';
		const n = keys.filter(k => k !== '*').length;
		const all = grants['*'] ? `Every server (${grants['*'].length} of 8)` : '';
		const some = n ? `${n} server${n === 1 ? '' : 's'}` : '';
		return [all, some && (all ? `more on ${some}` : some)].filter(Boolean).join(', ');
	},

	async openUserGrants(username) {
		const content = document.getElementById('modal-content');
		content.innerHTML = '<p class="text-gray-500">Loading...</p>';
		document.getElementById('modal-overlay').classList.remove('hidden');
		const res = await fetch('/api/users');
		if (!res.ok) { content.innerHTML = `<p class="text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const data = await res.json();
		const u = data.users.find(x => x.username === username);
		if (!u) return this.openUsersModal();
		const grants = JSON.parse(JSON.stringify(u.grants || {}));
		const rows = [{ key: '*', label: 'Every server', note: 'Also servers added later' }];
		this.servers.forEach(s => rows.push({ key: s.node ? `${s.node}/${s.name}` : s.name, label: s.name,
			note: `${s.edition}${s.node ? ` · on ${s.nodeName}` : ''}` }));
		// Kept for servers that aren't listed right now (a node that's switched off)
		Object.keys(grants).forEach(k => { if (!rows.some(r => r.key === k)) rows.push({ key: k, label: k.split('/').pop(), note: 'Not reachable right now' }); });
		this.grantsEdit = { username, perms: data.perms, presets: data.presets, grants, rows, open: {} };
		this.renderUserGrants();
	},

	grantPreset(perms) {
		const g = this.grantsEdit, have = [...(perms || [])].sort().join(',');
		if (!have) return 'none';
		return Object.keys(g.presets).find(p => [...g.presets[p]].sort().join(',') === have) || 'custom';
	},

	renderUserGrants(error) {
		const g = this.grantsEdit, esc = t => this.escapeHtml(String(t ?? ''));
		const field = 'bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const names = { none: 'No access', moderator: 'Moderator', 'co-owner': 'Co-owner', custom: 'Custom' };
		const rows = g.rows.map((r, i) => {
			const perms = g.grants[r.key] || [];
			const preset = this.grantPreset(perms);
			const open = perms.length || g.open[r.key];
			const boxes = open ? `<div class="grid grid-cols-2 gap-x-3 mt-2">${g.perms.map(p => `
				<label class="flex items-center gap-2 py-2 text-sm text-gray-800 dark:text-gray-200 cursor-pointer">
					<input type="checkbox" ${perms.includes(p.id) ? 'checked' : ''} onchange="ServerApp.toggleGrant(${i}, '${p.id}', this.checked)"> ${esc(p.label)}</label>`).join('')}</div>` : '';
			return `
			<div class="p-3 border border-gray-200 dark:border-gray-700 rounded mb-2">
				<div class="flex items-center justify-between gap-2">
					<div class="min-w-0">
						<div class="font-bold text-gray-900 dark:text-white truncate">${esc(r.label)}</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 truncate">${esc(r.note)}</div>
					</div>
					<select aria-label="Access to ${esc(r.label)}" onchange="ServerApp.setGrantPreset(${i}, this.value)" class="${field} shrink-0">
						${['none', 'moderator', 'co-owner', 'custom'].map(p => `<option value="${p}" ${preset === p ? 'selected' : ''}>${names[p]}</option>`).join('')}
					</select>
				</div>${boxes}
			</div>`;
		}).join('');
		document.getElementById('modal-content').innerHTML = `
			<h3 class="text-xl font-bold mb-1 text-gray-900 dark:text-white">What ${esc(g.username)} may do</h3>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-3"><b>Moderator</b>: view, console and players. <b>Co-owner</b>: everything on the server except deleting it, restoring backups, Java and memory limits, which stay with admins. <b>Files</b> also lets them use SFTP.</p>
			<div class="mb-3">${rows}</div>
			<p id="grants-error" class="${error ? '' : 'hidden'} text-sm text-red-500 mb-2">${esc(error || '')}</p>
			<div class="flex gap-2">
				<button onclick="ServerApp.openUsersModal()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Back</button>
				<button onclick="ServerApp.saveUserGrants()" class="flex-1 px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold">Save</button>
			</div>`;
	},

	setGrantPreset(i, preset) {
		const g = this.grantsEdit, key = g.rows[i].key;
		if (preset === 'none') { delete g.grants[key]; g.open[key] = false; }
		else if (preset === 'custom') { g.open[key] = true; if (!g.grants[key]) g.grants[key] = ['view']; }
		else g.grants[key] = [...g.presets[preset]];
		this.renderUserGrants();
	},

	toggleGrant(i, perm, on) {
		const g = this.grantsEdit, key = g.rows[i].key;
		let perms = new Set(g.grants[key] || []);
		if (on) { perms.add(perm); perms.add('view'); }
		else if (perm === 'view') perms = new Set();          // nothing works without seeing the server
		else perms.delete(perm);
		g.open[key] = true;
		if (perms.size) g.grants[key] = g.perms.map(p => p.id).filter(p => perms.has(p)); else delete g.grants[key];
		this.renderUserGrants();
	},

	async saveUserGrants() {
		const g = this.grantsEdit;
		const res = await fetch(`/api/users/${encodeURIComponent(g.username)}/grants`, {
			method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grants: g.grants })
		});
		if (!res.ok) return this.renderUserGrants(await this.apiError(res));
		this.openUsersModal();
	},

	async updateUser(username, changes) {
		const res = await fetch(`/api/users/${encodeURIComponent(username)}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(changes)
		});
		if (!res.ok) alert(`Not changed: ${await this.apiError(res)}`);
		this.openUsersModal();
	},

	async resetUserPassword(username) {
		const pw = prompt(`New password for ${username} (8+ characters). They will be signed out everywhere.`);
		if (pw === null) return;
		await this.updateUser(username, { password: pw });
	},

	async resetUserTwoFactor(username) {
		if (!confirm(`Turn off two-factor sign-in for '${username}'? Use this when they lost their phone: they sign in with their password and set it up again.`)) return;
		const res = await fetch(`/api/users/${encodeURIComponent(username)}/reset-2fa`, { method: 'POST' });
		if (!res.ok) alert(`Not reset: ${await this.apiError(res)}`);
		this.openUsersModal();
	},

	async deleteUser(username) {
		if (!confirm(`Delete panel user '${username}'? They will be signed out immediately.`)) return;
		const res = await fetch(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
		if (!res.ok) alert(`Not deleted: ${await this.apiError(res)}`);
		this.openUsersModal();
	},
};

window.onload = () => ServerApp.init();