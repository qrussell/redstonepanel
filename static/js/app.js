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
        const ver = document.getElementById('panel-version');
        if (ver && this.currentUser.panel_version) {
            ver.innerText = 'v' + this.currentUser.panel_version;
            ver.title = `RedstonePanel ${this.currentUser.panel_version}`;
        }

        this.applyTheme();
        this.setupDropZones();
        this.fetchTemplates();
        const sortDropdown = document.getElementById('server-sort');
        if (sortDropdown) sortDropdown.value = this.sortOrder;
        
        await this.loadServers();
    },

    async loadServers() {
        const res = await fetch('/api/servers');
        this.servers = await res.json();
        
        this.applySort();
        this.renderSidebar();
        this.loadRemovedServers();

        // Nothing selected: show the dashboard (unless the cluster page is open)
        if (!this.activeServer && document.getElementById('dashboard').classList.contains('hidden')
            && document.getElementById('cluster-view').classList.contains('hidden')) {
            this.showDashboard();
        }

        if (this.activeServer) {
            const srv = this.servers.find(s => s.name === this.activeServer.name);
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
    themeColor: localStorage.getItem('rp_themeColor') || 'diamond',

    applyTheme() {
        document.documentElement.classList.toggle('dark', this.themeMode === 'dark');
        
        document.documentElement.setAttribute('data-theme', this.themeColor);
    },

    openSettingsModal() {
        const modal = document.getElementById('modal-overlay');
        document.getElementById('modal-content').innerHTML = `
            <h3 class="text-xl font-bold mb-4 text-gray-900 dark:text-white">Panel Settings</h3>
            
            <div class="mb-4">
                <label class="block text-sm text-gray-600 dark:text-gray-400 mb-1">Color Mode</label>
                <select id="setting-mode" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white">
                    <option value="dark" ${this.themeMode === 'dark' ? 'selected' : ''}>Dark Mode</option>
                    <option value="light" ${this.themeMode === 'light' ? 'selected' : ''}>Light Mode</option>
                </select>
            </div>

            <div class="mb-6">
                <label class="block text-sm text-gray-600 dark:text-gray-400 mb-1">Accent Palette</label>
                <select id="setting-color" class="w-full bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-2 text-gray-900 dark:text-white">
                    <option value="diamond" ${this.themeColor === 'diamond' ? 'selected' : ''}>Diamond Blue</option>
                    <option value="redstone" ${this.themeColor === 'redstone' ? 'selected' : ''}>Redstone Red</option>
                    <option value="emerald" ${this.themeColor === 'emerald' ? 'selected' : ''}>Emerald Green</option>
                    <option value="amethyst" ${this.themeColor === 'amethyst' ? 'selected' : ''}>Amethyst Purple</option>
                </select>
            </div>

            <div class="flex justify-end gap-3">
                <button onclick="document.getElementById('modal-overlay').classList.add('hidden')" class="px-4 py-2 bg-gray-300 dark:bg-gray-700 text-gray-900 dark:text-white rounded transition">Cancel</button>
                <button onclick="ServerApp.saveSettings()" class="px-4 py-2 bg-theme hover-bg-theme text-white rounded transition">Save Settings</button>
            </div>
        `;
        modal.classList.remove('hidden');
    },

    saveSettings() {
        const mode = document.getElementById('setting-mode').value;
        const color = document.getElementById('setting-color').value;
        
        this.themeMode = mode;
        this.themeColor = color;
        
        localStorage.setItem('rp_themeMode', mode);
        localStorage.setItem('rp_themeColor', color);
        
        this.applyTheme();
        document.getElementById('modal-overlay').classList.add('hidden');
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

    renderSidebar() {
        const list = document.getElementById('server-list');
        list.innerHTML = '';

        const javaServers = this.servers.filter(s => s.edition === 'Java');
        const bedrockServers = this.servers.filter(s => s.edition === 'Bedrock');
        const otherServers = this.servers.filter(s => s.edition !== 'Java' && s.edition !== 'Bedrock');

        const renderGroup = (title, serversGroup) => {
            if (serversGroup.length === 0) return;
            
            list.innerHTML += `<div class="text-xs font-bold text-gray-500 uppercase tracking-wider mt-4 mb-1 px-3">${title}</div>`;
            
            serversGroup.forEach(srv => {
                const dot = srv.status === 'online' ? 'text-green-500' : 'text-red-500';
                const isActive = this.activeServer && this.activeServer.name === srv.name ? 'bg-gray-200 dark:bg-gray-800' : 'hover:bg-gray-100 dark:hover:bg-gray-800';
                
                list.innerHTML += `
                    <div onclick="ServerApp.openWorkspace(${JSON.stringify(srv).replace(/"/g, '&quot;')})" 
                         class="p-3 rounded cursor-pointer transition flex items-center gap-3 ${isActive}">
                        <span class="${dot}">●</span>
                        <div>
                            <div class="font-bold text-gray-900 dark:text-gray-200">${srv.name}${this.hasProxyProblem(srv) || this.hasConnectProblem(srv) ? ' <span class="text-yellow-600" title="Players may not be able to connect: see Overview">⚠</span>' : ''}</div>
                            <div class="text-xs text-gray-500">${srv.edition}${srv.version ? ' ' + this.escapeHtml(srv.version) : ''} • Port ${srv.port}</div>
                        </div>
                    </div>
                `;
            });
        };

        renderGroup('Java Edition', javaServers);
        renderGroup('Bedrock Edition', bedrockServers);
        renderGroup('Unknown / Other', otherServers);
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
        document.getElementById('ws-title').innerText = srv.name;
        document.getElementById('ws-subtitle').innerText = srv.directory;
        document.getElementById('ws-status').innerText = srv.status === 'online' ? '🟢' : '🔴';
        
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
        document.getElementById('overview-stats').innerHTML = `
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Status</p><p class="font-bold uppercase ${srv.status === 'online' ? 'text-green-500' : 'text-red-500'}">${srv.status}</p></div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Engine</p><p class="font-bold text-blue-600 dark:text-blue-400">${srv.edition}</p></div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Minecraft Version</p><p class="font-bold text-gray-900 dark:text-gray-200">${srv.version ? esc(srv.version) : 'Unknown'}</p>${versionNote}</div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Port (TCP/UDP)</p><p class="font-bold text-gray-900 dark:text-gray-200">${srv.port}</p></div>
            <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-4 rounded shadow-sm"><p class="text-gray-500 dark:text-gray-400 text-xs">Max RAM</p><p class="font-bold text-gray-900 dark:text-gray-200">${srv.ram}</p></div>
        `;
        document.getElementById('overview-notes').innerHTML = this.serverNotes(srv);

        // A search typed for the previous server shouldn't hide this one's properties
        const propSearch = document.getElementById('prop-search');
        if (propSearch) propSearch.value = '';

        this.switchTab('overview');
        this.switchConfigSubTab('visual');
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
        
        // Clear chat polling if we navigate away
        if (this.chatInterval) {
            clearInterval(this.chatInterval);
            this.chatInterval = null;
        }
        
        if (tabId === 'properties') this.switchConfigSubTab('visual');
        if (tabId === 'maintenance') { this.refreshApplyTemplates(); this.loadRestarts(); this.loadUpdateOptions(); this.loadJavaChoice(); }
        if (tabId === 'world') this.loadWorldName();
        if (tabId === 'backups') this.loadBackups();
        if (tabId === 'files') this.loadFiles(this.filesServer === this.activeServer.name ? this.filesPath : '');
        if (tabId === 'addons') this.loadAddons();
        if (tabId === 'chat') {
            this.loadChat();
            this.chatInterval = setInterval(() => this.loadChat(), 3000); // Poll every 3 seconds
        }
    },

    async clearChat() {
        if (!this.activeServer) return;
        this.rconOutputs = [];
        this.chatHistoryIndex = -1;
        // Tell the backend, otherwise the next 3-second poll just repaints the old history
        await fetch(`/api/servers/${this.activeServer.name}/chat/clear`, { method: 'POST' });
        document.getElementById('chat-history').innerHTML = '<p class="text-gray-500 italic">Chat cleared...</p>';
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
            const res = await fetch(`/api/servers/${serverName}/chat`);
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
		
		if (event.key === 'Enter') {
			this.sendChat();
		} else if (event.key === 'ArrowUp') {
			event.preventDefault(); 
			if (this.chatHistory[serverName].length > 0) {
				this.chatHistoryIndex = Math.min(this.chatHistoryIndex + 1, this.chatHistory[serverName].length - 1);
				input.value = this.chatHistory[serverName][this.chatHistoryIndex];
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
		}
	},

    async sendChat() {
		const input = document.getElementById('chat-input');
		const msg = input.value.trim();
		if (!msg || !this.activeServer) return;
		
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
        input.value = ''; 

        // 2. Send the payload to the backend API
        const res = await fetch(`/api/servers/${this.activeServer.name}/chat`, {
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

    switchConfigSubTab(subId) {
		['visual', 'raw', 'players', 'permissions'].forEach(t => {
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
        const res = await fetch(`/api/servers/${this.activeServer.name}/permissions/raw`);
        const data = await res.json();
        document.getElementById('permissions-editor').value = data.content;
    },

    async saveRawPermissions() {
        const content = document.getElementById('permissions-editor').value;
        await fetch(`/api/servers/${this.activeServer.name}/permissions/raw`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
        });
        alert("Permissions file saved. Restart server to apply.");
    },

    async loadProperties(serverName) {
        const res = await fetch(`/api/servers/${serverName}/properties`);
        const props = await res.json();
        const editor = document.getElementById('properties-editor');
        editor.innerHTML = '';

        const enums = {
            'difficulty': ['peaceful', 'easy', 'normal', 'hard'],
            'gamemode': ['survival', 'creative', 'adventure', 'spectator']
        };

        // Values go into HTML attributes, so quotes etc. must be escaped (e.g. a motd containing ")
        const esc = t => this.escapeHtml(t);
        let html = '';
        for (const [key, value] of Object.entries(props)) {
            let inputHtml = '';
            const inputClasses = 'prop-input w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-gray-900 dark:text-white';

            if (value === 'true' || value === 'false') {
                inputHtml = `<select data-key="${esc(key)}" class="${inputClasses}">
                                <option value="true" ${value === 'true' ? 'selected' : ''}>True</option>
                                <option value="false" ${value === 'false' ? 'selected' : ''}>False</option>
                            </select>`;
            } else if (enums[key]) {
                const options = enums[key].map(opt => `<option value="${opt}" ${value === opt ? 'selected' : ''}>${opt}</option>`).join('');
                inputHtml = `<select data-key="${esc(key)}" class="${inputClasses}">${options}</select>`;
            } else if (!isNaN(value) && value !== '') {
                inputHtml = `<input type="number" data-key="${esc(key)}" value="${esc(value)}" class="${inputClasses}">`;
            } else {
                inputHtml = `<input type="text" data-key="${esc(key)}" value="${esc(value)}" class="${inputClasses}">`;
            }

            html += `
                <div class="prop-row bg-white dark:bg-gray-800 p-4 rounded border border-gray-200 dark:border-gray-700 flex justify-between items-center gap-4 shadow-sm" data-key="${esc(key)}">
                    <div class="flex-1"><label class="text-sm font-bold text-gray-700 dark:text-gray-300">${esc(key)}</label></div>
                    <div class="w-1/2">${inputHtml}</div>
                </div>`;
        }
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
        await fetch(`/api/servers/${this.activeServer.name}/properties`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ properties: updatedProps })
        });
        alert("Properties saved. Restart server to apply.");
    },

    async loadRawProperties() {
        const res = await fetch(`/api/servers/${this.activeServer.name}/properties/raw`);
        const data = await res.json();
        document.getElementById('raw-editor').value = data.content;
    },

    async saveRawProperties() {
        const content = document.getElementById('raw-editor').value;
        await fetch(`/api/servers/${this.activeServer.name}/properties/raw`, {
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
			const res = await fetch(`/api/servers/${serverName}/players`);
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
			<div class="flex justify-between items-center gap-4 p-3 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm">
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

    async setPlayerRole(select) {
        const player = select.dataset.player;
        const role = select.value;
        select.disabled = true;
        let ok = false, detail = '';
        try {
            const res = await fetch(`/api/servers/${this.activeServer.name}/players/role`, {
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
        const res = await fetch(`/api/servers/${this.activeServer.name}/template/save`, {
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

        const res = await fetch(`/api/servers/${this.activeServer.name}/template/apply`, {
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
                    <div class="font-bold text-gray-900 dark:text-gray-200">${t.name}</div>
                    <div class="text-xs text-gray-500">${t.edition} Template</div>
                </div>
            `;
        });
    },

    openTemplateEditor(id) {
        this.editingTemplateId = id;
        this.renderTemplateSidebar();
        
        const temp = this.templates.find(t => t.id === id);
        if (!temp) return;
        
        document.getElementById('template-editor-main').classList.remove('hidden');
        document.getElementById('editing-temp-title').innerText = `Edit Template: ${temp.name}`;
        
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
        await this.fetchTemplates(); // Ensure templates are loaded for the dropdown
        this.loadRemovedServers();   // fills the "Removed servers" section when it arrives
        const modal = document.getElementById('modal-overlay');
        
        // Only templates for the chosen edition; the list follows the edition dropdown
        const templateOptions = this.templateOptionsHtml('java', 'None (Standard Defaults)');

        document.getElementById('modal-content').innerHTML = `
            <h3 class="text-xl font-bold mb-4">Deploy or Import Server</h3>

            <div class="mb-4">
                <label class="block text-sm text-gray-400 mb-1">Engine Edition</label>
                <select id="deploy-edition" onchange="ServerApp.onDeployEditionChange(this.value)" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white mb-2">
                    <option value="java">Java Edition (Auto-Port)</option>
                    <option value="bedrock">Bedrock Edition (Auto-Port)</option>
                </select>

                <div id="deploy-java-options">
                    <div class="grid grid-cols-2 gap-2">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Server Type</label>
                            <select id="deploy-loader" onchange="ServerApp.loadDeployVersions()" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white mb-2">
                                <option value="vanilla">Vanilla (Mojang)</option>
                                <option value="paper">Paper (plugins)</option>
                                <option value="fabric">Fabric (mods)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Minecraft Version</label>
                            <select id="deploy-version" onchange="ServerApp.showJavaRequirement(this.value || this.dataset.newest, 'deploy-java-note')" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white mb-2">
                                <option value="">Loading…</option>
                            </select>
                        </div>
                    </div>
                    <p id="deploy-java-note" class="text-xs text-gray-400 mb-2"></p>
                </div>
                
                <label class="block text-sm text-gray-400 mb-1">Base Template (Optional)</label>
                <select id="deploy-template" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white mb-2">
                    ${templateOptions}
                </select>
                
                <button onclick="ServerApp.executeDeploy()" class="mt-2 w-full bg-green-600 hover:bg-green-700 py-2 rounded text-white font-bold transition">Deploy Fresh Server</button>
            </div>
            <hr class="border-gray-700 my-4" />
            <div class="mb-6">
                <label class="block text-sm text-gray-400 mb-1">Import Existing (.zip, .tar.gz, .tar)</label>
                <input type="file" id="import-file" accept=".zip,.tar.gz,.tgz,.tar,application/zip,application/gzip,application/x-tar" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white text-sm mb-2" />
                <p class="text-xs text-gray-500 mb-2">A backup wrapped in a single folder is fine; its contents are used.</p>
                <button onclick="ServerApp.executeImport()" class="w-full bg-blue-600 hover:bg-blue-700 py-2 rounded text-white font-bold transition">Upload & Import</button>
            </div>
            <div id="removed-servers" class="mb-6">${this.removedServersHtml()}</div>
            <button onclick="document.getElementById('modal-overlay').classList.add('hidden')" class="w-full px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white transition">Cancel</button>
        `;
        modal.classList.remove('hidden');
        this.loadDeployVersions();
    },

    onDeployEditionChange(edition) {
        document.getElementById('deploy-template').innerHTML = this.templateOptionsHtml(edition, 'None (Standard Defaults)');
        document.getElementById('deploy-java-options').classList.toggle('hidden', edition !== 'java');
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
            select.dataset.newest = versions[0] || '';
            select.innerHTML = `<option value="">Newest (${esc(versions[0] || '?')})</option>`
                + versions.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
            if (versions[0]) this.showJavaRequirement(versions[0], 'deploy-java-note');
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
        const res = await fetch(`/api/java/requirement?version=${encodeURIComponent(version)}`);
        if (note.dataset.version !== version) return;
        if (!res.ok) { note.innerText = ''; return; }
        const r = await res.json();
        const base = noteId === 'deploy-java-note' ? 'text-xs mb-2 ' : 'text-xs mt-1 ';
        if (r.java_used === null) {
            const verb = noteId === 'deploy-java-note' ? 'create the server' : 'update';
            note.className = base + (this.isAdmin() ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-400');
            note.innerText = `Minecraft ${r.minecraft} needs Java ${r.java_required}, which isn't installed. `
                + (this.isAdmin() ? `You'll be asked to install it (${r.package}) when you ${verb}.`
                                  : `Ask an admin to install it, or: sudo apt install ${r.package}`);
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

    async installJava(major, why) {
        if (!confirm(`${why ? why + '\n\n' : ''}Install Java ${major} (openjdk-${major}-jre-headless) now? `
            + `apt downloads it from this machine's package sources (roughly 50–100 MB); it takes a minute or two.`)) return false;
        return this.runJavaJob('install', major);
    },

    async removeJava(major) {
        if (!confirm(`Remove Java ${major} (openjdk-${major}-jre-headless)? No server uses it.\n\n`
            + `Anything else on this machine that runs Java ${major} stops working. A server that needs it later `
            + `can install it again.`)) return;
        await this.runJavaJob('remove', major);
    },

    async runJavaJob(action, major) {
        const done = action === 'install' ? 'installed' : 'removed';
        this.toggleConsole(true);
        const res = await fetch(`/api/java/runtimes/${action}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ major })
        });
        if (!res.ok) { alert(`Java ${major} wasn't ${done}: ${await this.apiError(res)}`); return false; }
        this.javaJob = (await res.json());
        this.refreshJavaViews();
        let job = this.javaJob;
        try {
            while (job && job.state === 'running') {
                await new Promise(r => setTimeout(r, 1500));
                const r = await fetch('/api/java/runtimes/job');
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
    async withJava(res, send) {
        if (res.status !== 409 || !this.isAdmin()) return res;
        let data = {};
        try { data = await res.clone().json(); } catch (e) { return res; }
        if (!data.java) return res;
        if (!await this.installJava(data.java.required, data.detail)) return res;
        return send();
    },

    // Maintenance tab: which Java this server runs on, and switching to another installed one
    async loadJavaChoice() {
        const card = document.getElementById('java-card');
        const body = document.getElementById('java-card-body');
        const srv = this.activeServer;
        if (!card || !srv) return;
        card.classList.toggle('hidden', srv.edition !== 'Java' || !this.isAdmin());
        if (srv.edition !== 'Java' || !this.isAdmin()) return;
        const res = await fetch('/api/java/runtimes');
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
        body.innerHTML = html;
    },

    async useJava(major) {
        const srv = this.activeServer;
        if (!srv) return;
        if (major === srv.java_installed) return;
        if (srv.status === 'online' && !confirm(`Run ${srv.name} on Java ${major}? It restarts now.`)) return;
        const res = await fetch(`/api/servers/${encodeURIComponent(srv.name)}/java`, {
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
        if (srv.edition !== 'Java') {
            select.classList.add('hidden');
            text.innerText = 'Download the newest Bedrock Dedicated Server release.';
            return;
        }
        const loader = srv.loader || 'vanilla';
        const kind = { vanilla: 'Vanilla', paper: 'Paper', fabric: 'Fabric' }[loader] || loader;
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
        const res = await fetch(`/api/servers/${encodeURIComponent(name)}/backups`);
        if (!this.activeServer || this.activeServer.name !== name) return;   // switched server meanwhile
        const box = document.getElementById('backups-content');
        if (!res.ok) {
            box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`;
            return;
        }
        this.backupInfo = await res.json();
        this.renderBackups();
    },

    // --- Files tab (admins): browse, edit text, upload, download, rename, delete ---
    filesPath: '',
    filesServer: null,
    filesData: null,
    fileEditor: null,     // {path, modified, dirty}

    filesUrl(suffix, params) {
        const q = new URLSearchParams(params || {}).toString();
        return `/api/servers/${encodeURIComponent(this.activeServer.name)}/files${suffix}${q ? '?' + q : ''}`;
    },

    joinPath(...parts) {
        return parts.filter(p => p).join('/');
    },

    async loadFiles(path) {
        const box = document.getElementById('files-content');
        if (!this.activeServer || !box) return;
        this.fileEditor = null;
        const res = await fetch(this.filesUrl('', { path: path || '' }));
        if (!res.ok) {
            const msg = await this.apiError(res);
            if (path) return this.loadFiles('');           // the folder went away: back to the top
            box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(msg)}</p>`;
            return;
        }
        this.filesData = await res.json();
        this.filesPath = this.filesData.path;
        this.filesServer = this.activeServer.name;
        this.renderFiles();
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
                <td class="py-1.5 pl-3 pr-2 w-6">${icon}</td>
                <td class="py-1.5 pr-3 break-all">${name}</td>
                <td class="py-1.5 pr-3 text-right whitespace-nowrap text-gray-500 dark:text-gray-400">${e.size === null ? '' : esc(this.formatSize(e.size))}</td>
                <td class="py-1.5 pr-3 whitespace-nowrap text-gray-500 dark:text-gray-400">${esc(new Date(e.modified * 1000).toLocaleString())}</td>
                <td class="py-1.5 pr-3 text-right whitespace-nowrap">${actions}</td>
            </tr>`;
        }).join('');

        document.getElementById('files-content').innerHTML = `
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
                <table class="w-full text-sm">
                    <tbody>${rows || '<tr><td class="p-3 text-gray-500">This folder is empty.</td></tr>'}</tbody>
                </table>
            </div>
            ${d.truncated ? '<p class="text-xs text-yellow-600 mt-2">Only the first 5000 entries are shown.</p>' : ''}
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">Text files up to ${esc(this.formatSize(d.max_text))} can be edited here; anything else, download and upload. Symlinks are listed but never followed. Most changes apply when the server restarts.</p>`;
    },

    async openFileEditor(path, isNew) {
        let data = { path, content: '', modified: null };
        if (!isNew) {
            const res = await fetch(this.filesUrl('/text', { path }));
            if (!res.ok) return alert(`Can't open ${path}: ${await this.apiError(res)}`);
            data = await res.json();
        }
        this.fileEditor = { path, modified: data.modified, dirty: false };
        const esc = t => this.escapeHtml(String(t ?? ''));
        document.getElementById('files-content').innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
                <div class="text-sm font-mono break-all text-gray-900 dark:text-gray-200">${esc(this.activeServer.name)}/${esc(path)}${isNew ? ' <span class="text-xs text-gray-500">(new)</span>' : ''}</div>
                <div class="flex gap-2">
                    <span id="file-editor-status" class="text-xs text-gray-500 dark:text-gray-400 self-center"></span>
                    <button onclick="ServerApp.saveFileEditor()" class="px-3 py-1.5 rounded text-sm bg-green-600 hover:bg-green-700 text-white">Save</button>
                    <button onclick="ServerApp.closeFileEditor()" class="px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Close</button>
                </div>
            </div>
            <textarea id="file-editor" spellcheck="false" class="w-full min-h-[400px] font-mono text-sm bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded p-3 text-gray-900 dark:text-gray-200 outline-none" style="height: 65vh; tab-size: 4;"></textarea>`;
        const ta = document.getElementById('file-editor');
        ta.value = data.content;
        ta.addEventListener('input', () => {
            this.fileEditor.dirty = true;
            document.getElementById('file-editor-status').innerText = 'Unsaved changes';
        });
        ta.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); this.saveFileEditor(); }
        });
        ta.focus();
    },

    async saveFileEditor() {
        const ed = this.fileEditor;
        if (!ed) return;
        const res = await fetch(this.filesUrl('/text'), {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: ed.path, content: document.getElementById('file-editor').value, modified: ed.modified })
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
        const res = await fetch(this.filesUrl('/mkdir'), {
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
        const res = await fetch(this.filesUrl('/rename'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, name })
        });
        if (!res.ok) alert(`Couldn't rename: ${await this.apiError(res)}`);
        this.loadFiles(this.filesPath);
    },

    async deleteFile(path, type) {
        const what = type === 'dir' ? `the folder '${path}' and everything in it` : `'${path}'`;
        if (!confirm(`Permanently delete ${what}? This can't be undone.`)) return;
        const res = await fetch(this.filesUrl('', { path }), { method: 'DELETE' });
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

    addonKind(srv) {
        if (!srv) return null;
        if (srv.edition === 'Bedrock') return 'bedrock';
        return ['fabric', 'paper'].includes(srv.loader) ? srv.loader : null;
    },

    addonsUrl(params) {
        const q = new URLSearchParams(params || {}).toString();
        return `/api/servers/${encodeURIComponent(this.activeServer.name)}/addons${q ? '?' + q : ''}`;
    },

    // The Mods / Plugins / Add-ons tab: admins, on Fabric, Paper and Bedrock servers
    updateAddonsTab(srv) {
        const btn = document.getElementById('tab-btn-addons');
        if (!btn) return;
        const kind = this.addonKind(srv);
        const show = kind && this.currentUser && this.currentUser.role === 'admin';
        btn.classList.toggle('hidden', !show);
        btn.innerText = { fabric: 'Mods', paper: 'Plugins', bedrock: 'Add-ons' }[kind] || 'Mods';
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
        const res = await fetch(this.addonsUrl());
        if (!res.ok) { box.innerHTML = `<p class="text-sm text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
        this.addonsData = await res.json();
        if (this.addonsFor !== this.activeServer.name) {
            this.addonsFor = this.activeServer.name;
            this.addonsView = 'installed';
            this.addonUpdates = null;
            this.addonSearch = null;
        }
        if (this.addonsView === 'browse') return this.renderAddonBrowser();
        this.renderAddons();
        if (['fabric', 'paper'].includes(this.addonsData.kind)) this.loadAddonUpdates();
    },

    renderAddons(messages) {
        messages = messages || this.addonMessages;
        const d = this.addonsData, esc = t => this.escapeHtml(String(t ?? ''));
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const small = 'px-2 py-1 rounded text-xs bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const title = { fabric: 'Mods', paper: 'Plugins', bedrock: 'Add-ons' }[d.kind] || 'Mods';
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
                <h3 class="text-lg font-bold text-gray-900 dark:text-white">${title}${d.minecraft ? ` <span class="text-sm font-normal text-gray-500">for ${esc(d.kind === 'paper' ? 'Paper' : 'Fabric')} ${esc(d.minecraft)}</span>` : ''}</h3>
                <span class="${note}">${d.items.length} installed</span>
            </div>
            ${this.addonViewSwitch()}
            ${this.addonUpdatesBar()}
            <div id="addons-drop" class="${card} border-2 border-dashed p-6 text-center cursor-pointer" onclick="document.getElementById('addons-upload').click()">
                <p class="font-bold text-gray-900 dark:text-white">Drop ${esc(accepts)} files here</p>
                <p class="${note} mt-1">or click to choose them. They go into ${esc(where)}.${d.kind === 'fabric' ? ' Most Fabric mods also need the Fabric API mod.' : ''}</p>
                <input type="file" id="addons-upload" multiple accept="${esc(accepts)}" class="hidden" onchange="ServerApp.dropOnAddons(this.files)">
            </div>
            <div id="upload-progress" class="space-y-2"></div>
            ${(messages || []).length ? `<div class="${card} p-3 text-sm space-y-1 text-gray-800 dark:text-gray-200">${messages.map(m => `<p>${esc(m)}</p>`).join('')}</div>` : ''}
            ${changed ? `<div class="p-3 rounded text-sm bg-blue-50 dark:bg-blue-900/30 border border-blue-300 dark:border-blue-800 text-gray-900 dark:text-gray-100">Restart the server to load your changes. <button onclick="ServerApp.sendPower('restart')" class="underline text-theme">Restart now</button></div>` : ''}
            <div class="${card}">${items || `<p class="p-4 text-sm text-gray-500 dark:text-gray-400">No ${esc(title.toLowerCase())} yet.</p>`}</div>
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
        } else this.loadAddons();
    },

    addonViewSwitch() {
        const d = this.addonsData;
        if (!['fabric', 'paper'].includes(d.kind)) return '';
        const b = (v, label) => `<button onclick="ServerApp.addonView('${v}')" class="px-3 py-1.5 rounded text-sm ${this.addonsView === v
            ? 'bg-theme text-white' : 'bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white hover:bg-gray-300 dark:hover:bg-gray-600'}">${label}</button>`;
        return `<div class="flex flex-wrap gap-2">${b('installed', 'Installed')}${b('browse', `Find ${d.kind === 'paper' ? 'plugins' : 'mods'}`)}</div>`;
    },

    addonProjectUrl(u) {
        if (u.source === 'hangar') return `https://hangar.papermc.io/search?query=${encodeURIComponent(u.slug || u.project)}`;
        const kind = this.addonsData && this.addonsData.kind === 'paper' ? 'plugin' : 'mod';
        return `https://modrinth.com/${kind}/${encodeURIComponent(u.slug || u.project)}`;
    },

    addonUpdatesBar() {
        const u = this.addonUpdates, esc = t => this.escapeHtml(String(t ?? ''));
        const box = 'p-3 rounded text-sm border flex flex-wrap items-center justify-between gap-2';
        const paper = this.addonsData.kind === 'paper';
        if (!['fabric', 'paper'].includes(this.addonsData.kind)) return '';
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
        const res = await fetch(this.addonsUrl().replace(/\?.*$/, '') + '/updates');
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
        const res = await fetch(this.addonsUrl().replace(/\?.*$/, '') + '/update', {
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
        const res = await fetch(this.addonsUrl().replace(/\?.*$/, '') + '/undo', { method: 'POST' });
        if (!res.ok) return alert(await this.apiError(res));
        const r = await res.json();
        this.addonsChanged[this.activeServer.name] = true;
        this.addonMessages = [`↩️ Put back ${r.restored.join(', ')}`];
        this.addonUpdates = null;
        await this.loadAddons();
    },

    async searchAddons(more) {
        const d = this.addonsData;
        const s = this.addonSearch || { source: 'modrinth', q: '', hits: [], total: 0 };
        const input = document.getElementById('addon-q'), sel = document.getElementById('addon-source');
        if (input) s.q = input.value.trim();
        if (sel) s.source = sel.value;
        if (!more) s.hits = [];
        s.loading = true; s.error = null;
        this.addonSearch = s;
        this.renderAddonBrowser();
        const res = await fetch(this.addonsUrl().replace(/\?.*$/, '') + '/search?' + new URLSearchParams({ source: s.source, q: s.q, offset: s.hits.length }));
        s.loading = false;
        if (!res.ok) s.error = await this.apiError(res);
        else {
            const r = await res.json();
            s.hits = s.hits.concat(r.hits);
            s.total = r.total;
            s.sources = r.sources;
        }
        if (this.addonsView === 'browse' && this.addonsData === d) this.renderAddonBrowser();
    },

    renderAddonBrowser(messages) {
        const d = this.addonsData, s = this.addonSearch || { source: 'modrinth', q: '', hits: [] };
        const esc = t => this.escapeHtml(String(t ?? ''));
        const card = 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-sm';
        const note = 'text-xs text-gray-500 dark:text-gray-400';
        const noun = d.kind === 'paper' ? 'plugins' : 'mods';
        const loader = d.kind === 'paper' ? 'Paper' : 'Fabric';
        const sources = s.sources || (d.kind === 'paper' ? ['modrinth', 'hangar'] : ['modrinth']);
        const fmt = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n || 0);
        const safeUrl = u => /^https:\/\//.test(String(u || '')) ? esc(u) : '#';
        const rows = s.hits.map(h => `<div class="flex items-start gap-3 p-3 border-t border-gray-200 dark:border-gray-700 first:border-t-0">
                ${h.icon && /^https:\/\//.test(h.icon) ? `<img src="${esc(h.icon)}" alt="" loading="lazy" referrerpolicy="no-referrer" class="w-10 h-10 rounded shrink-0 bg-gray-100 dark:bg-gray-700">` : '<div class="w-10 h-10 rounded shrink-0 bg-gray-100 dark:bg-gray-700"></div>'}
                <div class="min-w-0 flex-1">
                    <p class="font-bold text-gray-900 dark:text-white break-words"><a href="${safeUrl(h.url)}" target="_blank" rel="noopener noreferrer" class="hover:underline">${esc(h.title)}</a> <span class="${note} font-normal">by ${esc(h.author)}</span></p>
                    <p class="text-xs text-gray-600 dark:text-gray-300 mt-0.5">${esc(String(h.description || '').slice(0, 220))}</p>
                    <p class="${note} mt-0.5">${fmt(h.downloads)} downloads${h.updated ? ` · updated ${esc(String(h.updated).slice(0, 10))}` : ''}</p>
                </div>
                ${h.installed ? `<span class="text-xs px-2 py-1 rounded bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200 whitespace-nowrap">Installed</span>`
                : `<button onclick="ServerApp.installFromCatalog(${esc(JSON.stringify(h.source))}, ${esc(JSON.stringify(h.project))}, ${esc(JSON.stringify(h.title || h.project))})" class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white whitespace-nowrap">Install</button>`}
            </div>`).join('');
        const changed = this.addonsChanged[this.activeServer.name];
        document.getElementById('addons-content').innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-2">
                <h3 class="text-lg font-bold text-gray-900 dark:text-white">Find ${noun} <span class="text-sm font-normal text-gray-500">for ${loader} ${esc(d.minecraft || '')}</span></h3>
            </div>
            ${this.addonViewSwitch()}
            <form class="flex flex-wrap gap-2" onsubmit="event.preventDefault(); ServerApp.searchAddons()">
                <input id="addon-q" type="search" value="${esc(s.q)}" placeholder="Search ${noun}" class="flex-1 min-w-[12rem] bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded px-3 py-1.5 text-sm text-gray-900 dark:text-white">
                ${sources.length > 1 ? `<select id="addon-source" onchange="ServerApp.searchAddons()" class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded px-2 py-1.5 text-sm text-gray-900 dark:text-white">
                    ${sources.map(x => `<option value="${x}" ${x === s.source ? 'selected' : ''}>${x === 'hangar' ? 'Hangar' : 'Modrinth'}</option>`).join('')}</select>` : ''}
                <button class="px-3 py-1.5 rounded text-sm bg-theme hover-bg-theme text-white">Search</button>
            </form>
            ${(messages || []).length ? `<div class="${card} p-3 text-sm space-y-1 text-gray-800 dark:text-gray-200">${messages.map(m => `<p>${esc(m)}</p>`).join('')}</div>` : ''}
            ${changed ? `<div class="p-3 rounded text-sm bg-blue-50 dark:bg-blue-900/30 border border-blue-300 dark:border-blue-800 text-gray-900 dark:text-gray-100">Restart the server to load your changes. <button onclick="ServerApp.sendPower('restart')" class="underline text-theme">Restart now</button></div>` : ''}
            ${s.error ? `<p class="text-sm text-red-500">${esc(s.error)}</p>` : ''}
            <div class="${card}">${rows || `<p class="p-4 text-sm text-gray-500 dark:text-gray-400">${s.loading ? 'Searching…' : `No ${noun} found for this Minecraft version.`}</p>`}</div>
            ${s.hits.length && s.hits.length < (s.total || 0) ? `<button onclick="ServerApp.searchAddons(true)" class="px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white" ${s.loading ? 'disabled' : ''}>${s.loading ? 'Loading…' : 'More'}</button>` : ''}
            <p class="${note}">Only ${noun} with a build for ${loader} ${esc(d.minecraft || '')} that run on a server are shown. Install picks the newest release build, checks its hash, and installs what it needs${d.kind === 'fabric' ? ' (such as Fabric API)' : ''} too.</p>`;
    },

    async installFromCatalog(source, project, title) {
        const s = this.addonSearch;
        this.renderAddonBrowser([`Installing ${title}…`]);
        const res = await fetch(this.addonsUrl().replace(/\?.*$/, '') + '/install', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source, project })
        });
        let messages;
        if (!res.ok) messages = [`⛔ ${title}: ${await this.apiError(res)}`];
        else {
            const r = await res.json();
            this.addonsChanged[this.activeServer.name] = true;
            messages = r.installed.map(i => `✅ ${i.name || i.file} ${i.version || ''}${i.channel && i.channel !== 'release' ? ` (${i.channel})` : ''}`
                + (i.fits === false ? ' — made for a different Minecraft version' : '')).concat(r.notes.map(n => `⚠️ ${n}`));
            if (s) s.hits.forEach(h => { if (h.project === project || r.installed.some(i => i.project === h.project)) h.installed = true; });
            this.addonUpdates = null;
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
        const res = await fetch(this.addonsUrl().replace(/\?.*$/, '') + '/toggle', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item, enabled })
        });
        if (!res.ok) return alert(await this.apiError(res));
        this.addonsChanged[this.activeServer.name] = true;
        this.loadAddons();
    },

    async removeAddon(item, label) {
        if (!confirm(`Remove ${label} from ${this.activeServer.name}? The file is deleted.`)) return;
        const res = await fetch(this.addonsUrl({ item }), { method: 'DELETE' });
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
        const folder = { fabric: 'mods', paper: 'plugins' }[kind];
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
        const kindLabel = { scheduled: 'Scheduled', manual: 'Manual', 'pre-restore': 'Before restore', 'pre-change': 'Before mod update' };

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
                <td class="py-2 pr-3 whitespace-nowrap">${esc(new Date(b.created * 1000).toLocaleString())}</td>
                <td class="py-2 pr-3">${esc(kindLabel[b.kind] || b.kind)}</td>
                <td class="py-2 pr-3 text-right whitespace-nowrap">${esc(this.formatSize(b.size))}</td>
                <td class="py-2 text-right whitespace-nowrap">
                    <a href="/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/${encodeURIComponent(b.file)}" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white" title="Download">⬇</a>
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
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">The time is the server's clock${info.timezone ? ` (${esc(info.timezone)})` : ''}, not your browser's. Runs as a systemd timer, so backups happen even while the panel is stopped. Only scheduled backups are pruned to the number kept; manual ones stay until you delete them.</p>
                ${info.next_run && s.enabled ? `<p class="text-xs text-gray-500 dark:text-gray-400 mb-2">Next backup: ${esc(info.next_run)}</p>` : ''}
                ${last}
                <div class="flex gap-2 mt-3">
                    <button onclick="ServerApp.saveBackupSchedule(this)" class="bg-theme hover-bg-theme text-white px-4 py-2 rounded font-bold text-sm">Save schedule</button>
                    <button onclick="ServerApp.backupNow(this)" class="bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-4 py-2 rounded font-bold text-sm">Back up now</button>
                </div>
                ${javaNoRcon ? `<p class="text-xs text-yellow-600 dark:text-yellow-500 mt-3">RCON isn't set up for this server, so backups taken while it runs copy files it may be writing. Open the Live Chat tab once to set RCON up.</p>` : ''}
            </div>
            <div class="${card}">
                <div class="flex items-center justify-between mb-2">
                    <p class="text-sm font-bold text-gray-900 dark:text-white">Backups</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400" title="${esc(info.directory)}">${info.backups.length} · ${esc(this.formatSize(info.total_size))} in ${esc(info.directory)}</p>
                </div>
                ${info.backups.length ? `
                <div class="overflow-x-auto"><table class="w-full text-sm text-gray-700 dark:text-gray-300">
                    <thead><tr class="text-left text-xs text-gray-500 dark:text-gray-400"><th class="pb-1">Taken</th><th class="pb-1">Type</th><th class="pb-1 text-right">Size</th><th></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-3">Restore replaces the current world with the backup's. The server is stopped while that happens, and the world it replaces is saved first as a "Before restore" backup.</p>`
                : `<p class="text-sm text-gray-500 dark:text-gray-400">No backups yet.</p>`}
            </div>`;
    },

    async saveBackupSchedule(button) {
        const body = {
            enabled: document.getElementById('backup-enabled').checked,
            frequency: document.getElementById('backup-frequency').value,
            time: document.getElementById('backup-time').value,
            keep: parseInt(document.getElementById('backup-keep').value, 10) || 0,
        };
        button.disabled = true;
        const res = await fetch(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/schedule`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        button.disabled = false;
        if (!res.ok) return alert(`Couldn't save the schedule: ${await this.apiError(res)}`);
        this.loadBackups();
    },

    async loadRestarts() {
        if (!this.activeServer) return;
        const name = this.activeServer.name;
        const res = await fetch(`/api/servers/${encodeURIComponent(name)}/restarts`);
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
        const res = await fetch(`/api/servers/${encodeURIComponent(this.activeServer.name)}/restarts`, {
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
        const res = await fetch(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups`, { method: 'POST' });
        if (!res.ok) alert(`Backup failed: ${await this.apiError(res)}`);
        this.loadBackups();
    },

    async restoreBackup(file) {
        const b = this.backupInfo.backups.find(x => x.file === file);
        const when = b ? new Date(b.created * 1000).toLocaleString() : file;
        if (!confirm(`Replace ${this.activeServer.name}'s world with the backup from ${when}?\n\nThe server stops while this happens. The current world is saved first as a "Before restore" backup.`)) return;
        this.toggleConsole(true);
        const res = await fetch(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/${encodeURIComponent(file)}/restore`, { method: 'POST' });
        if (!res.ok) alert(`Restore failed: ${await this.apiError(res)}`);
        else alert('World restored.');
        this.loadBackups();
    },

    async deleteBackup(file) {
        if (!confirm(`Delete the backup ${file}? This can't be undone.`)) return;
        const res = await fetch(`/api/servers/${encodeURIComponent(this.activeServer.name)}/backups/${encodeURIComponent(file)}`, { method: 'DELETE' });
        if (!res.ok) alert(`Delete failed: ${await this.apiError(res)}`);
        this.loadBackups();
    },

    // --- Removed servers: uninstalled with "Keep World", files still in /opt ---
    removedServers: [],

    async loadRemovedServers() {
        try {
            const res = await fetch('/api/servers/removed');
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

    removedServersHtml() {
        if (!this.removedServers.length) return '';
        const esc = t => this.escapeHtml(String(t ?? ''));
        const isAdmin = this.currentUser && this.currentUser.role === 'admin';
        const rows = this.removedServers.map(s => {
            const details = [s.edition, s.port ? `port ${esc(s.port)}` : '', s.world ? `world “${esc(s.world)}”` : '']
                .filter(Boolean).join(' • ');
            let note = '';
            if (!s.can_restore) {
                note = `<p class="text-xs text-yellow-500 mt-1">Can't tell whether this is a Java or Bedrock server, so it can't be restored here.</p>`;
            } else if (!s.has_binary) {
                note = `<p class="text-xs text-yellow-500 mt-1">The server binary is missing; the latest ${esc(s.edition)} release will be downloaded.${s.edition === 'Java' ? ' A world from an older version is upgraded and can\'t go back, so export it first.' : ''}</p>`;
            }
            const when = s.removed_at ? ` • removed ${new Date(s.removed_at * 1000).toLocaleDateString()}` : '';
            return `
                <div class="border border-gray-700 rounded p-2 mb-2">
                    <div class="flex items-center justify-between gap-2">
                        <div class="min-w-0">
                            <div class="font-bold text-white truncate">${esc(s.name)}</div>
                            <div class="text-xs text-gray-400 truncate" title="${esc(s.directory)}">${details}${when}</div>
                        </div>
                        <div class="flex gap-1 shrink-0">
                            <button onclick="ServerApp.restoreRemoved('${esc(s.name)}', this)" ${s.can_restore ? '' : 'disabled'}
                                class="px-3 py-1 rounded text-sm font-bold text-white ${s.can_restore ? 'bg-green-600 hover:bg-green-700' : 'bg-gray-600 cursor-not-allowed'}">Restore</button>
                            ${isAdmin ? `<button onclick="ServerApp.deleteRemoved('${esc(s.name)}')" title="Delete its files permanently"
                                class="px-2 py-1 rounded text-sm bg-gray-700 hover:bg-red-700 text-white">🗑</button>` : ''}
                        </div>
                    </div>
                    ${note}
                </div>`;
        }).join('');
        return `
            <hr class="border-gray-700 my-4" />
            <label class="block text-sm text-gray-400 mb-1">Removed servers</label>
            <p class="text-xs text-gray-500 mb-2">Uninstalled with "Keep World". Their files are still on this machine; restoring gives them a new service under the same name.</p>
            ${rows}`;
    },

    async restoreRemoved(name, button) {
        const srv = this.removedServers.find(s => s.name === name);
        if (!srv) return;
        if (!srv.has_binary && !confirm(`${name} has no server binary. Download the latest ${srv.edition} release and restore it?`)) return;
        if (button) { button.disabled = true; button.innerText = 'Restoring…'; }
        this.toggleConsole(true);
        const send = () => fetch(`/api/servers/removed/${encodeURIComponent(name)}/restore`, { method: 'POST' });
        const res = await this.withJava(await send(), send);
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
        document.getElementById('modal-overlay').classList.add('hidden');
        this.toggleConsole(true);

        const send = () => fetch('/api/servers/deploy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const res = await this.withJava(await send(), send);
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (e) {}
            alert(`Deploy failed: ${detail}`);
        }
        this.loadServers();
    },

    exportServer() {
        if (!this.activeServer) return;
        window.location.href = `/api/servers/${this.activeServer.name}/export`;
    },

    async executeImport() {
        const fileInput = document.getElementById('import-file');
        if (!fileInput.files.length) return alert("Select a server backup (.zip, .tar.gz or .tar) first.");

        const formData = new FormData();
        formData.append("file", fileInput.files[0]);

        document.getElementById('modal-overlay').classList.add('hidden');
        this.toggleConsole(true);

        const res = await fetch('/api/servers/import', {
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
    
    logInterval: null,

    toggleConsole(forceOpen = false) {
        const dock = document.getElementById('console-dock');
        if (forceOpen) {
            dock.classList.remove('translate-y-full');
            this.startLogPolling();
        } else {
            dock.classList.add('translate-y-full');
            this.stopLogPolling();
        }
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
            const res = await fetch('/api/logs');
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
        await fetch(`/api/servers/${this.activeServer.name}/action`, {
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
        const kind = this.addonKind(this.activeServer);
        if (version && version !== this.activeServer.minecraft && ['fabric', 'paper'].includes(kind) && this.currentUser && this.currentUser.role === 'admin') {
            const r = await fetch(`/api/servers/${encodeURIComponent(this.activeServer.name)}/addons/compatibility?minecraft=${encodeURIComponent(version)}`);
            if (r.ok) {
                const c = await r.json();
                const noun = kind === 'paper' ? 'plugins' : 'mods';
                if (c.missing.length) warn += `\n\nThese ${noun} have no build for ${version} yet, so the server may not start:\n- ` + c.missing.map(m => m.title).join('\n- ');
                if (c.unknown.length) warn += `\n\nThese ${noun} couldn't be checked (not found on Modrinth or Hangar):\n- ` + c.unknown.map(m => m.title).join('\n- ');
            }
        }
        if (!confirm(`Update ${this.activeServer.name} to ${target}? The server restarts. Take a backup first: a world opened by a newer version can't go back.${warn}`)) return;
        this.toggleConsole(true);
        const name = this.activeServer.name;
        const send = () => fetch(`/api/servers/${encodeURIComponent(name)}/update`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(version ? { version } : {})
        });
        const res = await this.withJava(await send(), send);
        if (!res.ok) alert(`Update failed: ${await this.apiError(res)}`);
        await this.loadServers();
        this.loadUpdateOptions();
        this.loadJavaChoice();
    },

    async uninstallServer(keepWorld) {
        if(confirm(`WARNING: ${keepWorld ? 'Uninstall' : 'Completely purge'} ${this.activeServer.name}?`)) {
            this.toggleConsole(true);
            const res = await fetch(`/api/servers/${this.activeServer.name}?keep_world=${keepWorld}`, { method: 'DELETE' });
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
		window.location.href = `/api/servers/${this.activeServer.name}/worlds/export`;
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
			const res = await fetch(`/api/servers/${serverName}/properties`);
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
			const res = await fetch(`/api/servers/${this.activeServer.name}/worlds/rename`, {
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
		document.getElementById('btn-change-password').classList.toggle('hidden', u.source !== 'panel');
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

	// --- Panel Settings: connection (HTTP/HTTPS) and sign-in (admins only) ---
	async openPanelSettings() {
		const overlay = document.getElementById('settings-overlay');
		const box = document.getElementById('settings-content');
		box.innerHTML = '<p class="text-gray-500">Loading settings...</p>';
		overlay.classList.remove('hidden');
		const res = await fetch('/api/settings');
		if (!res.ok) { box.innerHTML = `<p class="text-red-500">${this.escapeHtml(await this.apiError(res))}</p>`; return; }
		const d = await res.json();
		const s = d.settings, esc = t => this.escapeHtml(t);
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-sm text-gray-900 dark:text-white';
		const radio = (name, value, current, title, desc) => `
			<label class="flex gap-3 p-3 rounded border border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-900">
				<input type="radio" name="${name}" value="${value}" ${current === value ? 'checked' : ''} onchange="ServerApp.updateSettingsForm()" class="mt-1">
				<span><span class="font-bold text-gray-900 dark:text-white">${title}</span><br><span class="text-xs text-gray-500 dark:text-gray-400">${desc}</span></span>
			</label>`;
		const now = d.runtime.scheme ? `${d.runtime.scheme.toUpperCase()} on port ${d.runtime.port}` : 'unknown (not started by run.py)';
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

			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Notifications</h4>
			<div id="set-notifications" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>

			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Cluster</h4>
			<div id="set-cluster" class="mb-6"><p class="text-xs text-gray-500 dark:text-gray-400">Loading...</p></div>

			<h4 class="font-bold text-gray-900 dark:text-white mb-2">Connection</h4>
			<div class="space-y-2 mb-3">
				${s.https_mode === 'cluster'
					? radio('https_mode', 'cluster', s.https_mode, 'HTTPS from the cluster', 'This panel is in a cluster, so its certificate comes from the cluster and its HTTPS settings are fixed. Leave the cluster to change them.')
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
				${radio('login_mode', 'required', s.login_mode, 'Always required (recommended)', 'Everyone signs in with a Linux or panel account.')}
				${radio('login_mode', 'trusted_networks', s.login_mode, 'Not required on the local network', 'Devices on the networks below can manage servers without signing in. Users and these settings still need an admin sign-in.')}
			</div>
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
		document.getElementById('set-https-port').disabled = s.https_mode === 'cluster';
		this.loadClusterSettings();
		this.loadNotificationSettings();
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

	showDashboard() {
		if (this.fileEditor && this.fileEditor.dirty
			&& !confirm('Discard your unsaved changes to ' + this.fileEditor.path + '?')) return;
		this.fileEditor = null;
		this.activeServer = null;
		this.renderSidebar();
		this.closeCluster();
		document.getElementById('workspace').classList.add('hidden');
		document.getElementById('dashboard').classList.remove('hidden');
		this.loadDashboard();
		clearInterval(this.dashTimer);
		this.dashTimer = setInterval(() => { if (!document.hidden) this.loadDashboard(); }, 10000);
	},

	hideDashboard() {
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
		if (!document.getElementById('dashboard').classList.contains('hidden')) this.renderDashboard();
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
	barColor(p) { return p >= 90 ? 'bg-red-500' : p >= 75 ? 'bg-yellow-500' : 'bg-green-500'; },
	bar(p) {
		const w = Math.max(0, Math.min(100, p || 0));
		return `<div class="w-full h-2 rounded bg-gray-200 dark:bg-gray-700 overflow-hidden"><div class="h-2 rounded ${this.barColor(w)}" style="width:${w}%"></div></div>`;
	},

	openServerByName(name, tab) {
		const srv = this.servers.find(s => s.name === name);
		if (!srv) return;
		this.openWorkspace(srv);
		if (tab) this.switchTab(tab);
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
					const tab = a.where === 'backups' ? 'backups' : '';
					const link = a.server ? ` <button onclick="ServerApp.openServerByName(${this.jsArg(a.server)}, ${this.jsArg(tab)})" class="underline text-theme whitespace-nowrap">Open ${esc(a.server)}</button>` : '';
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
			const dot = s.status === 'online' ? 'text-green-500' : crash || s.state === 'failed' ? 'text-red-500' : 'text-gray-400';
			const stateText = crash ? 'crash loop' : s.state === 'failed' ? 'failed' : s.status;
			const lb = s.last_backup;
			const backupNote = lb ? (lb.ok ? `last ${this.fmtWhen(lb.at)}` : '<span class="text-red-500">last failed</span>') : 'none yet';
			return `
				<tr class="border-t border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer" onclick="ServerApp.openServerByName(${this.jsArg(s.name)})">
					<td class="${td}"><span class="${dot}">●</span> <b class="text-gray-900 dark:text-white">${esc(s.name)}</b><div class="${note}">${esc(s.edition)} ${esc(s.version || '')} · port ${esc(s.port)}</div></td>
					<td class="${td}">${esc(stateText)}<div class="${note}">${s.uptime !== null ? 'up ' + this.fmtDuration(s.uptime) : ''}</div></td>
					<td class="${td}">${this.fmtPct(s.cpu_percent)}</td>
					<td class="${td}">${this.fmtBytes(s.memory)}<div class="${note}">${s.heap && s.heap !== 'N/A' ? 'max heap ' + esc(s.heap) : ''}</div></td>
					<td class="${td}">${s.players !== null ? `${s.players} / ${s.max_players}` : '—'}</td>
					<td class="${td}">${s.folder_size !== null ? this.fmtBytes(s.folder_size) : '<span class="' + note + '">measuring…</span>'}</td>
					<td class="${td}">${s.backups ? `${s.backups} · ${this.fmtBytes(s.backups_size)}` : '—'}<div class="${note}">${backupNote}</div></td>
					<td class="${td}">${s.next_backup ? this.fmtWhen(s.next_backup) : '—'}</td>
					<td class="${td}">${s.next_restart ? this.fmtWhen(s.next_restart) : '—'}</td>
				</tr>`;
		}).join('');
		const serverTable = `
			<section class="${card}">
				<h3 class="${h3} p-4 pb-2">Game servers</h3>
				${servers.length ? `<div class="overflow-x-auto"><table class="w-full text-sm text-gray-700 dark:text-gray-300">
					<thead class="text-xs text-gray-500 dark:text-gray-400"><tr>
						<th class="${th}">Server</th><th class="${th}">State</th><th class="${th}">CPU</th><th class="${th}">Memory</th>
						<th class="${th}">Players</th><th class="${th}">Folder</th><th class="${th}">Backups</th>
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
				${d.host.version ? row('RedstonePanel', esc(d.host.version) + (d.host.update ? ` <span class="text-blue-500">(${esc(d.host.update)} available)</span>` : '')) : ''}
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

		document.getElementById('dash-body').innerHTML = attentionHtml + tiles + serverTable
			+ `<section class="grid grid-cols-1 lg:grid-cols-3 gap-4">${host}${disks}${services}</section>` + nodes
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

	openCluster() {
		this.hideDashboard();
		this.activeServer = null;
		this.renderSidebar();
		document.getElementById('workspace').classList.add('hidden');
		document.getElementById('cluster-view').classList.remove('hidden');
		this.loadCluster();
		clearInterval(this.clusterTimer);
		this.clusterTimer = setInterval(() => { if (!document.hidden) this.loadCluster(); }, 15000);
	},

	closeCluster() {
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
						<span class="${s.status === 'online' ? 'text-green-500' : 'text-red-500'}">●</span>
						<div class="min-w-0">
							<div class="font-bold text-gray-900 dark:text-gray-200 truncate">${esc(s.name)}</div>
							<div class="text-xs text-gray-500">${esc(s.edition)}${s.version ? ' ' + esc(s.version) : ''} • Port ${esc(s.port)}</div>
						</div>
					</div>
					<div class="flex gap-2">
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
						${n.local ? '' : `<a href="${esc(n.url)}/" target="_blank" rel="noopener" class="text-sm underline text-theme whitespace-nowrap">Open dashboard ↗</a>`}
					</div>
					${ok ? '' : `<p class="mx-4 mb-3 text-sm p-2 rounded bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-200">⚠️ ${esc(this.clusterStates[n.state] || n.state)}${n.error ? ` <span class="text-xs">(${esc(n.error)})</span>` : ''}</p>`}
					${rows || (ok ? '<p class="px-4 pb-4 text-sm text-gray-500 dark:text-gray-400">No Minecraft servers on this panel yet.</p>' : '')}
				</div>`;
		}).join('');
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
				<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">This panel is a <b>node</b> of cluster <b>${esc(d.name)}</b>. Its master, <b>${esc(d.master_name)}</b> (<span class="font-mono">${esc(d.master_url)}</span>), can see and start, stop or restart its game servers. It can't change this panel's users or settings.</p>
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
					<p class="${note}">RedstonePanel ${esc(n.version || '?')} · certificate expires ${esc(this.fmtTime(n.cert_expires))} · last answered ${esc(this.fmtTime(n.last_ok))}</p>
				</div>
				<div class="flex gap-2">
					<button onclick="ServerApp.renewNodeCert(${this.jsArg(n.id)}, this)" class="${small}">Renew cert</button>
					<button onclick="ServerApp.removeNode(${this.jsArg(n.id)}, ${this.jsArg(n.name)})" class="${small}">Remove</button>
				</div>
			</div>`).join('');
		const joins = d.joins.map(j => `
			<p class="${note}">Join code ${esc(j.id)}… made by ${esc(j.created_by)}, valid until ${esc(this.fmtTime(j.expires))} · <a href="#" onclick="ServerApp.cancelJoin(${this.jsArg(j.id)}); return false;" class="underline">cancel</a></p>`).join('');
		box.innerHTML = `
			<p class="text-sm text-gray-700 dark:text-gray-300 mb-2">This panel is the <b>master</b> of cluster <b>${esc(d.name)}</b>. Signed-in users manage every node's servers from the <b>🌐 Cluster</b> page.</p>
			${caLine}
			<div class="rounded border border-gray-200 dark:border-gray-700 mb-3">
				<p class="p-2 text-sm font-bold text-gray-900 dark:text-white">Nodes (${d.nodes.length})</p>
				${nodeRows || `<p class="p-2 border-t border-gray-200 dark:border-gray-700 ${note}">No nodes yet.</p>`}
			</div>
			<div class="p-3 rounded border border-gray-200 dark:border-gray-700 mb-3">
				<p class="font-bold text-sm text-gray-900 dark:text-white mb-2">Add a node</p>
				<label class="${note}">Address nodes use to reach this master
					<input id="cl-join-url" value="${esc(d.join_url)}" class="${field} mt-1 mb-2 font-mono"></label>
				<button onclick="ServerApp.makeJoinCode()" class="${small}">Make a join code</button>
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
			<div class="flex gap-2">
				<button onclick="ServerApp.renewNodeCert('local', this)" class="${small}">Renew this master's certificate</button>
				${d.nodes.length ? '' : `<button onclick="ServerApp.leaveCluster(true)" class="${small}">Dissolve cluster</button>`}
			</div>
			<p id="cl-error" class="hidden text-sm text-red-500 mt-2"></p>`;
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

	async makeJoinCode() {
		const url = document.getElementById('cl-join-url').value.trim();
		const j = await this.clusterPost('/api/settings/cluster/joins', { url });
		if (!j) return;
		const esc = t => this.escapeHtml(t);
		const out = document.getElementById('cl-join-out');
		out.innerHTML = `
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
	},

	openChangePasswordModal() {
		const field = 'w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded p-2 text-gray-900 dark:text-white mb-3';
		document.getElementById('modal-content').innerHTML = `
			<h3 class="text-xl font-bold mb-4 text-gray-900 dark:text-white">Change Password</h3>
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">Current password</label>
			<input type="password" id="pw-current" autocomplete="current-password" class="${field}">
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">New password (8+ characters)</label>
			<input type="password" id="pw-new" autocomplete="new-password" class="${field}">
			<label class="block text-sm text-gray-500 dark:text-gray-400 mb-1">Repeat new password</label>
			<input type="password" id="pw-new2" autocomplete="new-password" class="${field}">
			<p id="pw-error" class="hidden text-sm text-red-500 mb-3"></p>
			<div class="flex gap-2">
				<button onclick="ServerApp.closeModal()" class="flex-1 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 rounded text-gray-900 dark:text-white">Cancel</button>
				<button onclick="ServerApp.changeOwnPassword()" class="flex-1 px-4 py-2 bg-theme hover-bg-theme rounded text-white font-bold">Change</button>
			</div>`;
		document.getElementById('modal-overlay').classList.remove('hidden');
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
						${self ? '<span class="text-xs ml-1 text-gray-500">(you)</span>' : ''}
						<div class="text-xs text-gray-500 dark:text-gray-400">Last sign-in: ${esc(when(u.last_login))}</div>
					</div>
					<select data-user="${esc(u.username)}" onchange="ServerApp.updateUser(this.dataset.user, {role: this.value})" ${self ? 'disabled title="You can\'t change your own role"' : ''} class="${field}">
						<option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
						<option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option>
					</select>
				</div>
				<div class="flex gap-2 mt-2 text-xs">
					<button data-user="${esc(u.username)}" onclick="ServerApp.resetUserPassword(this.dataset.user)" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">Reset password</button>
					${self ? '' : `<button data-user="${esc(u.username)}" onclick="ServerApp.updateUser(this.dataset.user, {disabled: ${!u.disabled}})" class="px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white">${u.disabled ? 'Enable' : 'Disable'}</button>
					<button data-user="${esc(u.username)}" onclick="ServerApp.deleteUser(this.dataset.user)" class="px-2 py-1 rounded bg-red-600 hover:bg-red-700 text-white">Delete</button>`}
				</div>
			</div>`;
		}).join('') : '<p class="text-sm text-gray-500 dark:text-gray-400 mb-2">No panel users yet.</p>';

		content.innerHTML = `
			<h3 class="text-xl font-bold mb-1 text-gray-900 dark:text-white">Panel Users</h3>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-4">Linux accounts in the ${data.linux_groups.map(g => `<b>${esc(g)}</b>`).join(' or ')} group can also sign in (as admins); they are managed on the server, not here.</p>
			<div class="max-h-72 overflow-y-auto mb-4">${rows}</div>
			<div class="border-t border-gray-200 dark:border-gray-700 pt-4">
				<p class="text-sm font-bold mb-2 text-gray-900 dark:text-white">Add user</p>
				<input id="new-user-name" placeholder="Username" autocomplete="off" autocapitalize="none" class="w-full mb-2 ${field}">
				<input id="new-user-pass" type="password" placeholder="Password (8+ characters)" autocomplete="new-password" class="w-full mb-2 ${field}">
				<select id="new-user-role" class="w-full mb-2 ${field}">
					<option value="user">User: manage servers</option>
					<option value="admin">Admin: manage servers and panel users</option>
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

	async deleteUser(username) {
		if (!confirm(`Delete panel user '${username}'? They will be signed out immediately.`)) return;
		const res = await fetch(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
		if (!res.ok) alert(`Not deleted: ${await this.apiError(res)}`);
		this.openUsersModal();
	},
};

window.onload = () => ServerApp.init();