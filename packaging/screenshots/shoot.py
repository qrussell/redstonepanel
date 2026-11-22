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

The last step moves the demo's Bedrock server to the node and back (cluster-moved.png), with a
pre-copy (1.16.13) so the picture shows how long it was down, so it runs after everything else.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
OUT = os.environ.get("OUT", "/out")
THEMES = os.environ.get("THEMES", "dark light").split()
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
        if picture:
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
            shot("new-server")
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
            self.scroll_to_text("Connection")    # on a master this also shows Nightly backups
            shot("panel-settings-1")
            self.close_modals()
        step("settings", settings)

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
        s.move("minecraft-bedrock")
    if len(browser_shooters) % 2:
        failed.append("the Bedrock server was left on the node: run with an even number of themes")
    browser.close()

print("taken:", ", ".join(taken))
if skipped:
    print("skipped:\n  " + "\n  ".join(skipped))
if failed:
    print("failed:\n  " + "\n  ".join(failed))
sys.exit(1 if failed else 0)
