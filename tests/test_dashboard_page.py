"""Dashboard tests (builder C).

1. Static: index.html is self-contained (no external URLs) and uses the contract endpoints.
2. Served by the stub server; the inline script passes ``node --check``.
3. Headless Edge/Chrome (CDP over websockets) against tests/dashboard_stub_server.py:
   live WS frames + feed + state card, every sabotage button posts the right spec
   and shows a toast (success and failure), polling fallback when the socket dies.
   Skipped when no Chromium browser is installed.

Run from demo/:  python -m pytest tests/test_dashboard_page.py -q
Set DASHBOARD_SCREENSHOT=<path.png> to keep a 1440x900 screenshot.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DEMO = TESTS.parent
for p in (str(DEMO), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

HTML = DEMO / "sabotage_demo" / "dashboard" / "index.html"


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read()


# --------------------------------------------------------------------------- static
def test_html_is_self_contained():
    html = HTML.read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html), "external resource referenced"
    assert "@import" not in html and "fonts.googleapis" not in html
    assert html.count("<script>") == 1 and "<script src" not in html
    for path in ("/ws", "/frames/latest.jpg", "/state", "/health", "/sabotage", "/events?since="):
        assert path in html, path
    for spec in ('kind: "freeze_joint"', 'kind: "cut_object_sensor"', 'kind: "teleport_cube"', 'kind: "clear"'):
        assert spec in html, spec
    assert "delta: [0.0, 0.15, 0.0]" in html


# --------------------------------------------------------------------------- stub server
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stub_server(tmp_path_factory):
    import uvicorn
    from dashboard_stub_server import Stub, build_app, default_run

    run_dir = default_run(tmp_path_factory.mktemp("stub"), short=True)
    stub = Stub(run_dir, speed=2.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(stub), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    t0 = time.time()
    while not server.started:
        if time.time() - t0 > 15:
            raise RuntimeError("stub server did not start")
        time.sleep(0.05)
    yield stub, f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


def test_served_page_and_js_syntax(stub_server, tmp_path):
    _, base = stub_server
    body = _get(base + "/").decode("utf-8")
    assert body == HTML.read_text(encoding="utf-8")
    health = json.loads(_get(base + "/health"))
    assert health["ok"] is True and health["scenario"]
    scripts = re.findall(r"<script>(.*?)</script>", body, re.S)
    assert len(scripts) == 1
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    js = tmp_path / "dashboard.js"
    js.write_text(scripts[0], encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- headless browser
def _browser() -> str | None:
    cands = [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser")]
    cands += [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    return next((c for c in cands if c and Path(c).exists()), None)


class CDP:
    """Minimal Chrome DevTools Protocol client (one page target)."""

    def __init__(self, ws_url: str) -> None:
        from contextlib import ExitStack

        from websockets.sync.client import connect

        self._stack = ExitStack()
        self.ws = self._stack.enter_context(connect(ws_url, max_size=None, open_timeout=10))
        self.n = 0
        self.events: list[dict] = []

    def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=20))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error evaluating {expr!r}: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    def wait(self, expr: str, timeout: float = 10.0, what: str = ""):
        t0 = time.time()
        val = None
        while time.time() - t0 < timeout:
            val = self.eval(expr)
            if val:
                return val
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what or expr} (last value {val!r})")

    def close(self) -> None:
        self._stack.close()


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def page(stub_server, tmp_path_factory):
    exe = _browser()
    if not exe:
        pytest.skip("no Edge/Chrome available for the headless check")
    stub, base = stub_server
    profile = tmp_path_factory.mktemp("browser-profile")
    proc = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions", "--remote-debugging-port=0", "--remote-allow-origins=*",
                             f"--user-data-dir={profile}", "--window-size=1440,900", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = profile / "DevToolsActivePort"
        t0 = time.time()
        while not (port_file.exists() and port_file.read_text().strip()):
            if time.time() - t0 > 20:
                pytest.skip("browser did not expose a DevTools port")
            time.sleep(0.1)
        port = int(port_file.read_text().splitlines()[0])
        targets = json.loads(_get(f"http://127.0.0.1:{port}/json/list"))
        target = next(t for t in targets if t.get("type") == "page")
        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        cdp.call("Page.navigate", url=base + "/")
        yield cdp, stub, base
        cdp.close()
    finally:
        _kill(proc)


def _js_errors(cdp: CDP) -> list:
    errs = [e for e in cdp.events if e.get("method") == "Runtime.exceptionThrown"]
    errs += [e for e in cdp.events if e.get("method") == "Runtime.consoleAPICalled"
             and e["params"].get("type") == "error"]
    return errs


def _click(cdp: CDP, action: str) -> None:
    cdp.eval(f"document.querySelector('button[data-action={action}]').click(), true")


def _toast_count(cdp: CDP, cls: str = "") -> int:
    return cdp.eval(f"document.querySelectorAll('.toast{cls}').length")


def test_live_page_in_headless_browser(page):
    cdp, stub, base = page
    cdp.wait("document.getElementById('conn').classList.contains('live')", 15, "LIVE websocket pill")
    cdp.wait("document.getElementById('frame').src.startsWith('data:image/jpeg')", 10, "WS frame in <img>")
    cdp.wait("document.getElementById('frame').naturalWidth === 960", 10, "decoded 960x720 frame")
    cdp.wait("document.querySelectorAll('#feed .e.tool_call').length >= 2", 15, "tool calls in feed")
    first_call = cdp.eval("document.querySelector('#feed .e.tool_call').textContent")
    assert first_call.startswith("> ")
    assert cdp.eval("document.querySelector('#feed .e.tool_call .p').textContent").startswith("> ")
    assert cdp.eval("document.querySelectorAll('#feed .e.tool_result').length") >= 1
    title = cdp.wait("(t => t && t !== 'Connecting…' ? t : '')(document.getElementById('title').textContent)", 10)
    assert title in (stub.run.title, stub.scenario)
    cdp.wait("/^\\d+\\.\\d\\d s$/.test(document.getElementById('sSim').textContent)", 5, "state sim time")
    assert cdp.eval("document.getElementById('sTcp').textContent").startswith("[")

    # layout must not move when the feed grows: viewport rect stays put
    rect = "JSON.stringify(document.getElementById('viewport').getBoundingClientRect())"
    r0 = cdp.eval(rect)
    n0 = cdp.eval("document.querySelectorAll('#feed .e').length")
    cdp.wait(f"document.querySelectorAll('#feed .e').length > {n0 + 3}", 15, "feed growth")
    assert cdp.eval(rect) == r0
    assert cdp.eval("document.documentElement.scrollHeight <= window.innerHeight + 1")

    # sabotage buttons -> POST /sabotage with the contract specs, success toasts
    n_ok = _toast_count(cdp, ":not(.fail)")
    _click(cdp, "cut")
    cdp.wait(f"document.querySelectorAll('.toast:not(.fail)').length > {n_ok}", 5, "success toast")
    assert stub.sabotage_posts[-1] == {"kind": "cut_object_sensor"}
    cdp.wait("document.getElementById('sCube').textContent.includes('TRACKER DEAD')", 5, "TRACKER DEAD")
    cdp.wait("document.getElementById('sFaults').textContent.includes('Object tracker cut')", 5, "fault listed")
    cdp.wait("document.querySelectorAll('#chips .chip').length >= 1", 5, "fault chip on frame")
    cdp.wait("[...document.querySelectorAll('#feed .e.sabotage')].some(e => e.textContent.includes('Object tracker cut'))",
             5, "sabotage line in feed")

    cdp.eval("document.getElementById('jointSel').value = '2', true")
    _click(cdp, "freeze")
    cdp.wait("document.querySelectorAll('#sJoints .joint')[2].classList.contains('frozen')", 5, "joint 3 frozen")
    assert stub.sabotage_posts[-1] == {"kind": "freeze_joint", "joint": 2}

    _click(cdp, "teleport")
    t0 = time.time()
    while stub.sabotage_posts[-1].get("kind") != "teleport_cube" and time.time() - t0 < 5:
        time.sleep(0.05)
    assert stub.sabotage_posts[-1] == {"kind": "teleport_cube", "delta": [0.0, 0.15, 0.0]}

    # failure toast carries the server's error text
    _post(base + "/_stub/fail_next", {"error": "stub refused this fault"})
    _click(cdp, "teleport")
    cdp.wait("[...document.querySelectorAll('.toast.fail')].some(t => t.textContent.includes('stub refused this fault'))",
             5, "failure toast")

    _click(cdp, "clear")
    cdp.wait("document.getElementById('sFaults').textContent.trim() === 'none'", 5, "faults cleared")
    assert stub.sabotage_posts[-1] == {"kind": "clear"}

    shot = cdp.call("Page.captureScreenshot", format="png")
    out = os.environ.get("DASHBOARD_SCREENSHOT")
    if out:
        import base64

        Path(out).write_bytes(base64.b64decode(shot["data"]))
    assert not _js_errors(cdp), _js_errors(cdp)


def test_polling_fallback_when_socket_dies(page):
    cdp, stub, base = page
    hits0 = stub.hits["/frames/latest.jpg"]
    _post(base + "/_stub/ws", {"accept": False})
    cdp.wait("document.getElementById('conn').classList.contains('polling')", 8, "POLLING pill")
    cdp.wait("document.getElementById('frame').src.startsWith('blob:')", 5, "polled frame")
    time.sleep(1.6)
    assert stub.hits["/frames/latest.jpg"] - hits0 >= 2       # every 500 ms
    assert stub.hits["/events"] >= 1
    n0 = cdp.eval("document.querySelectorAll('#feed .e').length")
    cdp.wait(f"document.querySelectorAll('#feed .e').length > {n0}", 10, "feed via /events polling")
    _post(base + "/_stub/ws", {"accept": True})
    cdp.wait("document.getElementById('conn').classList.contains('live')", 15, "reconnect to LIVE")
    seqs = cdp.eval("document.querySelectorAll('#feed .e').length")
    assert seqs <= len(stub.events) + 1                        # no duplicates after reconnect
    assert not _js_errors(cdp), _js_errors(cdp)
