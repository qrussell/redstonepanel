"""Takes the user manual's screenshots from the demo cluster that demo.sh builds, in the dark and
the light theme.

Runs in the Playwright image on the demo's Docker network (demo.sh starts it). BASE is the
master's address, RP_USER/RP_PASS the demo admin. Every picture is 1440x900 unless noted. Dark
pictures go to /out (docs/guide/images), light ones to /out/light; the docs show the one that
matches the reader's theme (see docs/guide/README.md, "About the screenshots").

Phone pictures (1.16.10) are 390x844, like a phone, and named phone-*.png. The QR code dialog
(phone-qr.png) is the desktop's.

update-alert.png and bedrock-auto-update.png (1.16.12) are the Maintenance tab's Update Server
card: demo.sh deploys Paper 1.21.4 so there is something newer, turns on Update automatically
for the Bedrock server and runs the update check.

terminal.png and phone-terminal.png (1.21.10) log in to the master's terminal as steve, the
Linux account demo.sh adds with the demo admin's password, and run a few commands.

The last step moves the demo's Bedrock server to the node and back (cluster-moved.png), with a
pre-copy (1.16.13) so the picture shows how long it was down, so it runs after everything else.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
OUT = os.environ.get("OUT", "/out")
# The public join pages (1.21.5) are on the same machine as the panel, on their own port
JOIN_BASE = BASE.rsplit(":", 1)[0] + ":8444" if BASE.count(":") == 2 else BASE + ":8444"
THEMES = os.environ.get("THEMES", "dark light").split()
# ONLY="new server,modpack": take just these steps (the names given to step()), to retake a few
# pictures on a demo kept with KEEP=1. The sign-in picture and the move are skipped too.
ONLY = [s.strip() for s in os.environ.get("ONLY", "").split(",") if s.strip()]
taken, failed, skipped = [], [], []


def folder(theme):
    d = OUT if theme == "dark" else os.path.join(OUT, theme)
    os.makedirs(d, exist_ok=True)
    return d


class Shooter:
    def __init__(self, browser, theme, phone=False):
        self.theme, self.out = theme, folder(theme)
        size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
        extra = {"is_mobile": True, "has_touch": True, "device_scale_factor": 2} if phone else {}
        self.ctx = browser.new_context(ignore_https_errors=True, viewport=size, color_scheme=theme, **extra)
        # The panel keeps its theme in localStorage (Settings → Theme); set it before any page runs
        self.ctx.add_init_script(f"try {{ localStorage.setItem('rp_themeMode', '{theme}') }} catch (e) {{}}")
        self.page = self.ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())   # "Delete the old copy" asks first

    def shot(self, name, full=False, element=None):
        self.page.wait_for_timeout(700)
        path = f"{self.out}/{name}.png"
        if element:
            self.page.locator(element).first.screenshot(path=path)
        else:
            self.page.screenshot(path=path, full_page=full)
        taken.append(f"{self.theme}/{name}")

    def tall_shot(self, name, element):
        """An element taller than the screen, in a scrolling pane: makes the screen tall enough to
        show all of it and hides the console dock waiting below the screen, then puts both back."""
        page = self.page
        size = page.viewport_size
        extra = page.evaluate("""() => [...document.querySelectorAll('body *')]
            .reduce((n, el) => Math.max(n, el.scrollHeight - el.clientHeight), 0)""")
        page.evaluate("document.getElementById('console-dock').style.visibility = 'hidden'")
        page.set_viewport_size({"width": size["width"], "height": size["height"] + extra})
        try:
            self.shot(name, element=element)
        finally:
            page.set_viewport_size(size)
            page.evaluate("document.getElementById('console-dock').style.visibility = ''")

    def step(self, name, fn):
        if ONLY and name not in ONLY:
            return
        try:
            fn()
        except Exception as e:  # keep going: one missing picture shouldn't stop the rest
            failed.append(f"{self.theme} {name}: {e}")
            print(f"FAIL  {self.theme} {name}: {e}")

    def open_server(self, name, node=False, tab="overview"):
        self.page.evaluate("""([name, node]) => {
            const s = ServerApp.servers.find(s => s.name === name && !!s.node === node);
            ServerApp.openWorkspace(s);
        }""", [name, node])
        self.page.wait_for_timeout(2500)
        if tab != "overview":
            self.page.click(f"#tab-btn-{tab}")
            self.page.wait_for_timeout(2500)

    def close_modals(self):
        self.page.keyboard.press("Escape")
        self.page.evaluate("""() => document.querySelectorAll('#modal-overlay, #settings-overlay, .modal-overlay, [id$="-modal"], #template-workspace')
                           .forEach(e => e.classList.add('hidden'))""")
        self.page.wait_for_timeout(300)

    def scroll_to_text(self, text):
        self.page.evaluate("""(text) => {
            const el = [...document.querySelectorAll('h2, h3, h4, label, div, p')]
                .find(e => e.childElementCount === 0 && e.textContent.trim() === text);
            if (el) el.scrollIntoView({block: 'start'});
        }""", text)
        self.page.wait_for_timeout(500)

    def sign_in(self, picture=True):
        page = self.page
        page.goto(BASE + "/static/login.html")
        page.wait_for_timeout(1500)
        if picture and not ONLY:
            self.step("sign-in", lambda: self.shot("sign-in", element="form"))
        r = self.ctx.request.post(BASE + "/api/auth/login",
                                  data={"username": os.environ["RP_USER"], "password": os.environ["RP_PASS"]})
        if not r.ok:
            sys.exit("sign-in failed")
        page.goto(BASE + "/static/index.html")
        page.wait_for_timeout(5000)

    def addons_browse(self, name, node, query=None, source=None):
        page = self.page
        self.open_server(name, node=node, tab="addons")
        page.evaluate("ServerApp.addonView('browse')")
        page.wait_for_timeout(4000)
        if source:
            page.select_option("#addon-source", source)
            page.wait_for_timeout(4000)
        if query:
            page.fill("#addon-q", query)
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)

    def terminal(self, commands):
        """Opens the master's terminal, logs in as steve at its login: prompt (the demo admin is a
        panel account) and runs commands."""
        page = self.page
        page.evaluate("ServerApp.openTerminal('', 'Survival Box')")
        page.wait_for_selector(".rp-terminal .xterm-rows:has-text('login:')", timeout=30000)
        page.keyboard.type("steve\n")
        page.wait_for_selector(".rp-terminal .xterm-rows:has-text('Password')", timeout=15000)
        page.keyboard.type(os.environ["RP_PASS"] + "\n")
        page.wait_for_selector(".rp-terminal .xterm-rows:has-text('steve@survival-box')", timeout=30000)
        page.keyboard.type("clear\n")
        page.wait_for_timeout(500)
        for c in commands:
            page.keyboard.type(c + "\n")
            page.wait_for_timeout(1500)
        page.wait_for_timeout(1000)

    def close_terminal(self):
        self.page.evaluate("document.querySelectorAll('.rp-terminal [data-act=close]').forEach(b => b.click())")
        self.page.wait_for_timeout(500)

    # ---------------------------------------------------------------------------
    def everything(self):
        page, step, shot = self.page, self.step, self.shot
        self.sign_in()

        # The setup guide (1.16.1): the demo has servers, backups and cluster HTTPS but no webhook,
        # so it shows 3 of 4. It's hidden again afterwards so the other dashboard pictures don't
        # change with it.
        def setup_guide():
            self.ctx.request.put(BASE + "/api/settings/setup", data={"dismissed": False})
            page.evaluate("ServerApp.showDashboard()")
            page.wait_for_selector("section:has-text('Set up RedstonePanel')", timeout=15000)
            shot("setup-guide", element="section:has-text('Set up RedstonePanel')")
        step("setup guide", setup_guide)
        self.ctx.request.put(BASE + "/api/settings/setup", data={"dismissed": True})
        page.evaluate("ServerApp.showDashboard()")
        page.wait_for_timeout(2500)
        step("dashboard", lambda: shot("dashboard", full=True))
        step("hero", lambda: shot("hero"))

        def accent_color():
            # 1.21.2: the colored dot beside the sun/moon switch, with its four choices open
            page.locator('#theme-corner button[aria-label="Accent color"]').click()
            page.wait_for_timeout(300)
            width = page.viewport_size["width"]
            page.screenshot(path=f"{self.out}/accent-color.png", clip={"x": width - 720, "y": 0, "width": 720, "height": 150})
            taken.append(f"{self.theme}/accent-color")
            page.keyboard.press("Escape")
        step("accent color", accent_color)

        def cluster():
            page.evaluate("ServerApp.openCluster()")
            page.wait_for_timeout(3500)
            shot("cluster")
            page.locator("button", has_text="Move…").first.click()
            shot("cluster-move")
            page.check("#move-precopy")                  # 1.16.13
            shot("cluster-precopy", element="#modal-content")
            page.evaluate("ServerApp.closeModal()")
        step("cluster", cluster)

        # Port forwarding (1.16.14): what to forward on the internet router, master and node
        def port_forwarding():
            page.evaluate("ServerApp.openPorts()")
            page.wait_for_selector("text=Forward these", timeout=30000)
            page.wait_for_timeout(1500)
            # The page scrolls inside #ports-body, so grow the window to fit it for one picture
            size = page.viewport_size
            tall = page.evaluate("document.getElementById('ports-body').scrollHeight + 200")
            page.set_viewport_size({"width": size["width"], "height": max(size["height"], tall)})
            shot("port-forwarding")
            page.set_viewport_size(size)
        step("port forwarding", port_forwarding)

        # Join pages (1.21.5): the admin's page, and the master's Java server's public page as a
        # player sees it (on the join port, 8444, same host as the panel)
        def join_pages():
            put = """async ([path, body]) => (await fetch(path, {method: 'PUT',
                     headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).status"""
            for path, body in (("/api/settings/join", {"enabled": True, "port": 8444, "index": False}),
                               ("/api/join-pages/page", {"member": "local", "server": "minecraft-java", "enabled": True,
                                                         "slug": "survival", "address": "play.example.org",
                                                         "note": "We play most evenings. Be kind, no griefing.",
                                                         "link_url": "https://discord.gg/example",
                                                         "link_label": "Join our Discord"})):
                code = page.evaluate(put, [path, body])
                if code != 200:
                    raise RuntimeError(f"{path} answered {code}")
            page.evaluate("ServerApp.openJoin()")
            page.wait_for_selector("text=The public port", timeout=30000)
            page.wait_for_timeout(1500)
            shot("join-pages")
            public = self.ctx.new_page()
            try:
                public.goto(JOIN_BASE + "/join/survival")
                public.wait_for_selector("text=How to join", timeout=30000)
                public.wait_for_timeout(700)
                public.screenshot(path=f"{self.out}/join-page.png")
                taken.append(f"{self.theme}/join-page")
            finally:
                public.close()
        step("join pages", join_pages)

        def master_java():
            self.open_server("minecraft-java")
            shot("overview")
            for tab, name in (("chat", "live-chat"), ("properties", "configuration"), ("maintenance", "maintenance"),
                              ("backups", "backups"), ("world", "world"), ("files", "files"), ("addons", "plugins")):
                self.open_server("minecraft-java", tab=tab)
                shot(name)
        step("master java tabs", master_java)

        # 1.17: the Performance card (tick health, history, suggestions) and Memory and limits.
        # The history is whatever the sampler measured since the demo started (one point a minute).
        def performance():
            self.ctx.request.put(BASE + "/api/servers/minecraft-java/resources", data={"memory": "auto", "cpu": 2})
            self.open_server("minecraft-java")
            page.wait_for_selector("#overview-perf h3", timeout=15000)
            for _ in range(12):                          # the first history point comes a minute after start
                if not page.locator("#overview-perf :text('Nothing measured yet')").count():
                    break
                page.wait_for_timeout(15000)
                self.open_server("minecraft-java")
                page.wait_for_selector("#overview-perf h3", timeout=15000)
            page.evaluate("document.getElementById('overview-perf').scrollIntoView({block: 'start'})")
            shot("performance")
            shot("performance-card", element="#overview-perf")
            self.open_server("minecraft-java", tab="maintenance")
            page.wait_for_selector("#res-limit-mode", timeout=15000)
            shot("memory-and-limits", element="#resources-card")
        step("performance", performance)

        def configuration():
            self.open_server("minecraft-java", tab="properties")
            page.fill("#prop-search", "difficulty")
            page.wait_for_timeout(800)
            shot("configuration-filter")
            page.fill("#prop-search", "")
            page.evaluate("ServerApp.switchConfigSubTab('raw')")
            page.wait_for_timeout(2000)
            shot("configuration-raw")
            page.evaluate("ServerApp.switchConfigSubTab('players')")
            page.wait_for_timeout(2500)
            shot("player-roles")
            page.evaluate("ServerApp.switchConfigSubTab('visual')")
        step("configuration", configuration)

        def restarts():
            self.open_server("minecraft-java", tab="maintenance")
            self.scroll_to_text("Scheduled Restarts")
            shot("scheduled-restarts")
        step("scheduled restarts", restarts)

        # 1.16.12: the Paper server (1.21.4 in the demo) has an update alert; Bedrock updates itself
        def updates():
            self.open_server("minecraft-java", tab="maintenance")
            page.wait_for_selector("#autoupdate-content div", timeout=15000)
            shot("update-alert", element="div:has(> #autoupdate-content)")
            self.open_server("minecraft-bedrock", tab="maintenance")
            page.wait_for_selector("#autoupdate-enabled", timeout=15000)
            shot("bedrock-auto-update", element="div:has(> #autoupdate-content)")
        step("updates", updates)

        # 1.19: the Server type card and the preview of changing the Paper server to Fabric
        # (the preview changes nothing, so the demo's server stays as it is)
        def server_type():
            self.open_server("minecraft-java", tab="maintenance")
            page.wait_for_selector("#convert-body button", timeout=15000)
            shot("server-type", element="#convert-card")
            page.evaluate("ServerApp.previewConvert('fabric')")
            page.wait_for_selector("#convert-go", timeout=60000)
            shot("change-type-preview", element="#modal-content")
            page.evaluate("ServerApp.closeModal()")
        step("server type", server_type)

        def file_editor():
            self.open_server("minecraft-java", tab="files")
            page.evaluate("ServerApp.openFileEditor('plugins/Essentials/config.yml')")
            page.wait_for_timeout(3000)
            shot("file-editor")
        step("file editor", file_editor)

        def hangar():
            self.addons_browse("minecraft-java", False, source="hangar")
            shot("plugins-hangar")
        step("hangar", hangar)

        def bedrock():
            self.open_server("minecraft-bedrock")
            shot("overview-bedrock")
            for tab, name in (("backups", "backups-bedrock"), ("addons", "addons-bedrock")):
                self.open_server("minecraft-bedrock", tab=tab)
                shot(name)
        step("bedrock tabs", bedrock)

        def node_java():
            self.open_server("minecraft-java", node=True)
            shot("node-server")
            self.open_server("minecraft-java", node=True, tab="addons")
            shot("mods")
            if "update available" in page.inner_text("body").lower():
                shot("mods-updates")
            else:
                skipped.append(f"{self.theme}/mods-updates (no update available in the demo)")
        step("node server tabs", node_java)

        # 1.21.6: the Mods tab's Player pack view, for the Fabric server on the node (asks Modrinth)
        def player_pack():
            self.open_server("minecraft-java", node=True, tab="addons")
            page.evaluate("ServerApp.addonView('pack')")
            page.wait_for_selector("#pack-extra", timeout=30000)
            page.wait_for_timeout(1000)
            shot("player-pack")
        step("player pack", player_pack)

        def catalog():
            self.addons_browse("minecraft-java", True)
            shot("mods-catalog")
            self.addons_browse("minecraft-java", True, query="chunky")
            shot("mods-browse")
            page.locator("#addon-results [onclick^='ServerApp.openAddonDetail'], [onclick^='ServerApp.openAddonDetail']").first.click()
            page.wait_for_selector("#addon-detail", timeout=15000)
            page.wait_for_timeout(2500)
            shot("mods-details", element="#addon-detail")
        step("catalog", catalog)

        def new_server():
            page.evaluate("ServerApp.openDeployModal()")
            page.wait_for_timeout(2500)
            page.select_option("#deploy-where", index=1)
            page.wait_for_timeout(2500)
            self.tall_shot("new-server", "#modal-content")   # taller than the screen since 1.20
            self.close_modals()
        step("new server", new_server)

        # 1.17.2: New server's Advanced options, opened and filled in for a Paper server on the
        # master (builds and Javas are the real lists), and Maintenance -> Java -> Startup flags.
        def new_server_advanced():
            page.evaluate("ServerApp.openDeployModal()")
            page.wait_for_timeout(2500)
            page.select_option("#deploy-where", index=0)
            page.select_option("#deploy-loader", "paper")
            page.evaluate("ServerApp.loadDeployVersions(); ServerApp.refreshAdvanced()")
            page.wait_for_timeout(3000)
            page.evaluate("document.getElementById('deploy-advanced').open = true")
            page.wait_for_timeout(2500)
            page.fill("#adv-name", "survival")
            page.fill("#adv-port", "25600")
            page.select_option("#adv-build", index=2)
            page.select_option("#adv-heap", "4G")
            page.select_option("#adv-flags", "aikar")
            page.select_option("#adv-memory-limit", "auto")
            page.fill("#adv-cpu", "2")
            page.fill("#adv-seed", "8675309")
            page.select_option("#adv-level-type", index=3)
            page.fill("#adv-motd", "Welcome to the SMP")
            page.select_option("#adv-difficulty", "hard")
            page.fill("#adv-max-players", "12")
            page.fill("#adv-whitelist", "Alex Sam")
            page.fill("#adv-ops", "Alex")
            self.tall_shot("new-server-advanced", "#modal-content")
            self.close_modals()
        step("new server advanced", new_server_advanced)

        # 1.20: New server -> From a modpack. The search is Modrinth's real list; the second picture
        # is the Modpack card on Maintenance, when the demo has a server made from a pack
        # (MODPACK_SERVER names it; demo.sh doesn't make one, because it would change every
        # picture with the server list in it).
        def modpack():
            page.evaluate("ServerApp.openDeployModal()")
            page.wait_for_timeout(2500)
            page.evaluate("ServerApp.openModpackDialog()")
            page.wait_for_selector("#pack-q")
            page.wait_for_timeout(5000)
            for _ in range(5):                           # the packs' icons come from Modrinth's CDN:
                missing = page.evaluate("""() => {       // ask again for any that didn't arrive
                    const bad = [...document.querySelectorAll('#modal-content img')].filter(i => !(i.complete && i.naturalWidth));
                    bad.forEach(i => { i.loading = 'eager'; const u = i.src; i.src = ''; i.src = u; });
                    return bad.length;
                }""")
                if not missing:
                    break
                page.wait_for_timeout(4000)
            shot("modpack-search")
            self.close_modals()
            server = os.environ.get("MODPACK_SERVER")
            if not server:
                skipped.append(f"{self.theme}/modpack-card (no MODPACK_SERVER)")
                return
            self.open_server(server, tab="maintenance")
            page.wait_for_selector("#modpack-card:not(.hidden)")
            page.wait_for_timeout(1500)
            shot("modpack-card", element="#modpack-card")
        step("modpack", modpack)

        def startup_flags():
            self.open_server("minecraft-java", tab="maintenance")   # chosen, not saved: no restart
            page.wait_for_selector("#flags-preset", timeout=15000)
            page.select_option("#flags-preset", "aikar")
            page.evaluate("document.getElementById('java-card').scrollIntoView({block: 'center'})")
            shot("startup-flags", element="#java-card")
        step("startup flags", startup_flags)

        def console():
            page.evaluate("ServerApp.toggleConsole(true)")
            page.wait_for_timeout(2500)
            shot("console")
            page.evaluate("ServerApp.toggleConsole(false)")
        step("console", console)

        def templates():
            page.evaluate("ServerApp.openTemplateManager()")
            page.wait_for_timeout(2500)
            shot("templates")
            self.close_modals()
        step("templates", templates)

        def users():
            page.evaluate("ServerApp.openUsersModal()")
            page.wait_for_timeout(2000)
            shot("users")
            self.close_modals()
        step("users", users)

        # 1.18: what a helper may do on each server, SFTP on the Files tab and in Panel Settings,
        # and SSH keys under Password
        def user_servers():
            page.evaluate("ServerApp.openUserGrants('alex')")
            page.wait_for_selector("#modal-content:has-text('Co-owner')", timeout=15000)
            page.wait_for_timeout(800)
            shot("user-servers")
            self.close_modals()
        step("user servers", user_servers)

        def sftp():
            self.open_server("minecraft-java", tab="files")
            page.wait_for_selector("#files-sftp:has-text('Username')", timeout=20000)
            shot("sftp", element="#files-sftp")
            page.evaluate("ServerApp.openAccountModal({})")
            page.wait_for_selector("#ssh-box", timeout=15000)
            page.wait_for_timeout(1500)
            page.evaluate("document.getElementById('ssh-box').scrollIntoView({block: 'center'})")
            shot("ssh-keys", element="#modal-content")
            self.close_modals()
        step("sftp", sftp)

        def settings():
            page.reload()
            page.wait_for_timeout(4000)
            page.evaluate("ServerApp.openPanelSettings()")
            page.wait_for_timeout(3500)
            shot("panel-settings")
            self.scroll_to_text("Notifications")
            shot("notifications")
            self.scroll_to_text("Backup storage")   # 1.16.11: the demo's SSH storage on Creative Lab
            shot("backup-storage")
            self.scroll_to_text("Servers folder")   # 1.21.1
            shot("servers-folder")
            self.scroll_to_text("Connection")    # on a master this also shows Nightly backups
            shot("panel-settings-1")
            self.close_modals()
        step("settings", settings)

        # 1.21.10: the terminal, and its section in Settings
        def terminal():
            self.terminal(["systemctl status minecraft-java --no-pager -n 0 | head -8", "df -h /opt", "ls /opt"])
            shot("terminal")
            self.close_terminal()
            page.evaluate("ServerApp.openPanelSettings()")
            page.wait_for_selector("#set-terminal input", timeout=20000)
            page.wait_for_timeout(1500)
            page.evaluate("document.getElementById('set-section-terminal').scrollIntoView({block: 'start'})")
            shot("terminal-settings", element="#set-terminal-wrap")
            self.close_modals()
        step("terminal", terminal)

        # 1.25: Start at boot (Maintenance tab), and API tokens with a new one shown once
        def api_tokens():
            self.open_server("minecraft-java", tab="maintenance")
            page.wait_for_selector("#boot-card:not(.hidden)", timeout=15000)
            shot("start-at-boot", element="#boot-card")
            page.evaluate("ServerApp.openPanelSettings()")
            page.wait_for_selector("#set-api-tokens details", timeout=20000)
            page.evaluate("""fetch('/api/settings/api-tokens', {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: 'Backup script', grants: {'*': ['view', 'backups']}, days: 90})})
                .then(r => r.json()).then(made => fetch('/api/settings/api-tokens', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'Discord bot',
                    grants: {'minecraft-java': ['view', 'power', 'console']}, days: null})}))
                .then(r => r.json()).then(made => ServerApp.loadApiTokens(made))""")
            page.wait_for_selector("#api-token-made", timeout=15000)
            page.evaluate("document.querySelector('#set-api-tokens details').open = true")
            page.evaluate("document.getElementById('set-section-api-tokens').scrollIntoView({block: 'start'})")
            page.wait_for_timeout(500)
            shot("api-tokens", element="#set-api-tokens")
            self.close_modals()
        step("api tokens", api_tokens)

        # 1.26: the Sleep Mode card, turned on for the shot and off again (on, it would stop the
        # demo's Paper server once it had sat empty for that long)
        def sleep_mode():
            self.ctx.request.put(BASE + "/api/servers/minecraft-java/sleep", data={"enabled": True, "minutes": 30})
            try:
                self.open_server("minecraft-java", tab="maintenance")
                page.wait_for_selector("#sleep-card:not(.hidden) #sleep-enabled:checked", timeout=15000)
                page.evaluate("document.getElementById('sleep-card').scrollIntoView({block: 'center'})")
                page.wait_for_timeout(500)
                shot("sleep-mode", element="#sleep-card")
            finally:
                self.ctx.request.put(BASE + "/api/servers/minecraft-java/sleep", data={"enabled": False})
        step("sleep mode", sleep_mode)

        # 1.16.11: off-site copies of the Paper server's backups on the SSH storage
        def offsite():
            self.open_server("minecraft-java", tab="backups")
            page.wait_for_selector("#offsite-box:has-text('Copy to')", timeout=20000)
            page.evaluate("document.getElementById('offsite-box').scrollIntoView({block: 'start'})")
            shot("offsite-copies")
        step("offsite copies", offsite)

        def find_panels():                               # 1.16.13: Settings > Cluster > Add a node
            page.evaluate("ServerApp.openPanelSettings()")
            page.wait_for_timeout(3500)
            page.click("#cl-find")
            page.wait_for_selector("#cl-found :text('Searched')", timeout=60000)
            page.locator("#cl-find").scroll_into_view_if_needed()
            shot("cluster-find", element="div.p-3:has(#cl-find)")
            self.close_modals()
        step("find panels", find_panels)

        def phone_qr():
            page.evaluate("ServerApp.openPhoneQr()")
            page.wait_for_timeout(1500)
            shot("phone-qr", element="#modal-content")
            self.close_modals()
        step("phone qr", phone_qr)

    # Phone-sized pictures for the manual (1.16.10), from a 390x844 Shooter
    def phone(self):
        page, step, shot = self.page, self.step, self.shot
        self.sign_in(picture=False)
        step("phone dashboard", lambda: shot("phone-dashboard"))

        def menu():                                      # the whole menu: logo, every server, account buttons
            page.evaluate("ServerApp.openNav()")
            size = page.viewport_size
            extra = page.evaluate("""() => [document.getElementById('sidebar'),
                    ...document.querySelectorAll('#sidebar *')]
                .reduce((n, el) => n + Math.max(0, el.scrollHeight - el.clientHeight), 0)""")
            page.set_viewport_size({"width": size["width"], "height": size["height"] + extra})
            try:
                shot("phone-menu")
            finally:
                page.set_viewport_size(size)
            page.evaluate("ServerApp.closeNav()")
        step("phone menu", menu)

        def server():
            self.open_server("minecraft-java")
            shot("phone-server")
            self.tall_shot("phone-performance", "#overview-perf")
            for tab in ("backups", "chat"):
                page.evaluate(f"ServerApp.switchTab('{tab}')")
                page.wait_for_timeout(2500)
                shot(f"phone-{tab}")
        step("phone server", server)

        def ports():
            page.evaluate("ServerApp.openPorts()")
            page.wait_for_selector("text=Forward these", timeout=30000)
            page.wait_for_timeout(1500)                  # the forwards for the master and the node
            self.tall_shot("phone-port-forwarding", "div:has(> div > h3:text-is('Forward these'))")
        step("phone port forwarding", ports)

        def terminal():
            page.goto(BASE + "/static/index.html")
            page.wait_for_timeout(4000)
            self.terminal(["df -h /opt", "ls /opt"])
            shot("phone-terminal")
            self.close_terminal()
        step("phone terminal", terminal)

        def join_page():                             # turned on by the desktop's "join pages" step
            public = self.ctx.new_page()
            try:
                public.goto(JOIN_BASE + "/join/survival")
                public.wait_for_selector("text=How to join", timeout=30000)
                public.wait_for_timeout(700)
                public.screenshot(path=f"{self.out}/phone-join-page.png", full_page=True)
                taken.append(f"{self.theme}/phone-join-page")
            finally:
                public.close()
        step("phone join page", join_page)

    def move(self, server):
        """Moves a server to the other panel from the cluster page, pictures the finished move,
        then deletes the old copy (so the next move can keep the server's name)."""
        page = self.page

        def run():
            page.goto(BASE + "/static/index.html")
            page.wait_for_timeout(4000)
            page.evaluate("ServerApp.openCluster()")
            page.wait_for_timeout(3500)
            page.evaluate("""(server) => {
                const n = ServerApp.clusterData.nodes.find(n => (n.servers || []).some(s => s.name === server));
                ServerApp.openMoveDialog('move', n.id, server);
            }""", server)
            page.wait_for_timeout(800)
            page.check("#move-precopy")
            page.click("#move-go")
            page.wait_for_selector("text=Delete the old copy", timeout=180000)
            self.shot("cluster-moved")
            page.locator("button", has_text="Delete the old copy").click()
            page.wait_for_selector("button:has-text('Deleted')", timeout=60000)
            page.evaluate("ServerApp.closeModal()")
        self.step("cluster-moved", run)


with sync_playwright() as p:
    browser = p.chromium.launch()
    browser_shooters = []
    for theme in THEMES:
        s = Shooter(browser, theme)
        s.everything()
        browser_shooters.append(s)
        phone = Shooter(browser, theme, phone=True)
        phone.phone()
        phone.ctx.close()
    # Last: a real move in each theme, to the node and back, so the demo ends as it began
    for s in browser_shooters:
        if not ONLY:
            s.move("minecraft-bedrock")
    if len(browser_shooters) % 2 and not ONLY:
        failed.append("the Bedrock server was left on the node: run with an even number of themes")
    browser.close()

print("taken:", ", ".join(taken))
if skipped:
    print("skipped:\n  " + "\n  ".join(skipped))
if failed:
    print("failed:\n  " + "\n  ".join(failed))
sys.exit(1 if failed else 0)
