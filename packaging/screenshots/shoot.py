"""Takes the user guide's screenshots from the demo cluster that demo.sh builds.

Runs in the Playwright image on the demo's Docker network (demo.sh starts it). BASE is the
master's address, RP_USER/RP_PASS the demo admin. Every picture is 1440x900 in the dark theme
unless noted, written to /out (docs/guide/images). Close-ups that need hand-picked content
(mods-details, file-editor, configuration-filter and similar) aren't retaken here.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
OUT = os.environ.get("OUT", "/out")
taken, failed = [], []


def shot(page, name, full=False, clip=None):
    page.wait_for_timeout(600)
    page.screenshot(path=f"{OUT}/{name}.png", full_page=full, clip=clip)
    taken.append(name)


def step(name, fn):
    try:
        fn()
    except Exception as e:  # keep going: one missing picture shouldn't stop the rest
        failed.append(f"{name}: {e}")
        print(f"FAIL  {name}: {e}")


def open_server(page, name, node=False, tab="overview"):
    page.evaluate("""([name, node]) => {
        const s = ServerApp.servers.find(s => s.name === name && !!s.node === node);
        ServerApp.openWorkspace(s);
    }""", [name, node])
    page.wait_for_timeout(2500)
    if tab != "overview":
        page.click(f"#tab-btn-{tab}")
        page.wait_for_timeout(2500)


def close_modals(page):
    page.keyboard.press("Escape")
    page.evaluate("""() => document.querySelectorAll('#modal-overlay, .modal-overlay, [id$="-modal"]')
                       .forEach(e => e.classList.add('hidden'))""")
    page.wait_for_timeout(300)


def scroll_to_text(page, text):
    page.evaluate("""(text) => {
        const el = [...document.querySelectorAll('h2, h3, h4, label, div, p')]
            .find(e => e.childElementCount === 0 && e.textContent.trim() === text);
        if (el) el.scrollIntoView({block: 'start'});
    }""", text)
    page.wait_for_timeout(500)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900},
                              color_scheme="dark")

    page = ctx.new_page()
    page.goto(BASE + "/static/login.html")
    page.wait_for_timeout(1500)
    step("sign-in", lambda: page.locator("form").first.screenshot(path=f"{OUT}/sign-in.png"))

    r = ctx.request.post(BASE + "/api/auth/login",
                         data={"username": os.environ["RP_USER"], "password": os.environ["RP_PASS"]})
    if not r.ok:
        sys.exit("sign-in failed")
    page.goto(BASE + "/static/index.html")
    page.wait_for_timeout(5000)

    step("dashboard", lambda: shot(page, "dashboard", full=True))
    step("hero", lambda: shot(page, "hero"))

    def cluster():
        page.evaluate("ServerApp.openCluster()")
        page.wait_for_timeout(3000)
        shot(page, "cluster")
    step("cluster", cluster)

    def cluster_move():
        # The Move dialog only (cluster-moved.png, a finished move, isn't retaken: it would
        # change the demo)
        page.locator("button", has_text="Move…").first.click()
        shot(page, "cluster-move")
        page.evaluate("ServerApp.closeModal()")
    step("cluster-move", cluster_move)

    def master_java():
        open_server(page, "minecraft-java")
        shot(page, "overview")
        for tab, name in (("chat", "live-chat"), ("properties", "configuration"), ("maintenance", "maintenance"),
                          ("backups", "backups"), ("world", "world"), ("files", "files"), ("addons", "plugins")):
            open_server(page, "minecraft-java", tab=tab)
            shot(page, name)
    step("master java tabs", master_java)

    def bedrock():
        open_server(page, "minecraft-bedrock")
        shot(page, "overview-bedrock")
        for tab, name in (("backups", "backups-bedrock"), ("addons", "addons-bedrock")):
            open_server(page, "minecraft-bedrock", tab=tab)
            shot(page, name)
    step("bedrock tabs", bedrock)

    def node_java():
        open_server(page, "minecraft-java", node=True)
        shot(page, "node-server")
        open_server(page, "minecraft-java", node=True, tab="addons")
        shot(page, "mods")
    step("node server tabs", node_java)

    def new_server():
        page.evaluate("ServerApp.openDeployModal()")
        page.wait_for_timeout(2500)
        page.select_option("#deploy-where", index=1)
        page.wait_for_timeout(2500)
        shot(page, "new-server")
        close_modals(page)
    step("new server", new_server)

    def console():
        page.evaluate("ServerApp.toggleConsole(true)")
        page.wait_for_timeout(2500)
        shot(page, "console")
        page.evaluate("ServerApp.toggleConsole(false)")
    step("console", console)

    def templates():
        page.evaluate("ServerApp.openTemplateManager()")
        page.wait_for_timeout(2000)
        shot(page, "templates")
        close_modals(page)
    step("templates", templates)

    def users():
        page.evaluate("ServerApp.openUsersModal()")
        page.wait_for_timeout(2000)
        shot(page, "users")
        close_modals(page)
    step("users", users)

    def settings():
        page.reload()
        page.wait_for_timeout(4000)
        page.evaluate("ServerApp.openPanelSettings()")
        page.wait_for_timeout(3500)
        shot(page, "panel-settings")
        scroll_to_text(page, "Connection")    # on a master this also shows Nightly backups
        shot(page, "panel-settings-1")
        close_modals(page)
    step("settings", settings)

    browser.close()

print("taken:", ", ".join(taken))
if failed:
    print("failed:\n  " + "\n  ".join(failed))
sys.exit(1 if failed else 0)
