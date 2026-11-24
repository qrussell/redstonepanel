#!/usr/bin/env python3
"""A scripted browser for packaging/test/test-terminal.sh: signs in, gets a terminal ticket, opens
the terminal websocket and follows a script. Uses only the standard library and python3-websockets
(a dependency of the panel since 1.21.10).

    terminal-client.py BASE_URL PANEL_USER PANEL_PASSWORD [--node ID] [--cookie-out FILE]
                       [--cookie-in FILE] STEP...

Steps:  expect:TEXT   wait (up to 20 s) until TEXT is on the screen
        send:TEXT     type TEXT (\\r and \\n are understood)
        closed        wait (up to 90 s) until the panel closes the terminal
        sleep:N       wait N seconds
Prints the screen at the end; exits 1 when a step fails, 2 when the websocket is refused.
"""
import asyncio
import http.cookiejar
import json
import ssl
import sys
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def main():
    args = sys.argv[1:]
    base, user, password = args[:3]
    rest = args[3:]
    node = cookie_out = cookie_in = None
    steps = []
    while rest:
        a = rest.pop(0)
        if a == "--node":
            node = rest.pop(0)
        elif a == "--cookie-out":
            cookie_out = rest.pop(0)
        elif a == "--cookie-in":
            cookie_in = rest.pop(0)
        else:
            steps.append(a)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=ctx))
    headers = {"Content-Type": "application/json", "Origin": base}
    if cookie_in:
        cookie = open(cookie_in).read().strip()
    else:
        req = urllib.request.Request(base + "/api/auth/login", json.dumps({"username": user, "password": password}).encode(),
                                     headers)
        opener.open(req, timeout=20).read()
        cookie = next(c.value for c in jar if c.name == "rp_session")
    if cookie_out:
        open(cookie_out, "w").write(cookie)
    req = urllib.request.Request(base + "/api/terminal/ticket", json.dumps({"node": node} if node else {}).encode(),
                                 dict(headers, Cookie=f"rp_session={cookie}"))
    try:
        ticket = json.loads(opener.open(req, timeout=20).read())["ticket"]
    except urllib.error.HTTPError as e:
        print(f"TICKET {e.code} {e.read().decode(errors='replace')}")
        sys.exit(2)
    path = f"/api/cluster/nodes/{node}/terminal/ws" if node else "/api/terminal/ws"
    url = base.replace("https://", "wss://").replace("http://", "ws://") + f"{path}?ticket={ticket}"
    sys.exit(asyncio.run(run(url, {"Origin": base, "Cookie": f"rp_session={cookie}"}, steps)))


async def connect(url, headers):
    kw = {"ssl": ctx} if url.startswith("wss://") else {}
    try:
        from websockets.asyncio.client import connect as new_connect
        return await new_connect(url, additional_headers=headers, **kw)
    except ImportError:
        import websockets
        return await websockets.connect(url, extra_headers=headers, **kw)


async def run(url, headers, steps):
    try:
        ws = await connect(url, headers)
    except Exception as e:                       # noqa: BLE001
        print(f"REFUSED {e}")
        return 2
    screen = bytearray()
    closed = asyncio.Event()

    async def reader():
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    screen.extend(msg)
                else:
                    screen.extend(f"<{msg}>".encode())
        except Exception:                        # noqa: BLE001
            pass
        closed.set()
    task = asyncio.ensure_future(reader())
    code = 0
    for step in steps:
        kind, _, value = step.partition(":")
        value = value.replace("\\r", "\r").replace("\\n", "\n")
        if kind == "send":
            await ws.send(value.encode())
        elif kind == "sleep":
            await asyncio.sleep(float(value))
        elif kind == "expect":
            for _ in range(200):
                if value.encode() in screen:
                    break
                await asyncio.sleep(0.1)
            else:
                print(f"STEP FAILED: {step}")
                code = 1
                break
        elif kind == "closed":
            try:
                await asyncio.wait_for(closed.wait(), 90)
            except asyncio.TimeoutError:
                print("STEP FAILED: closed")
                code = 1
                break
    if not closed.is_set():
        await ws.close()
    await asyncio.wait([task], timeout=5)
    print(screen.decode("utf-8", "replace"))
    return code


if __name__ == "__main__":
    main()
