"""Offline Chrome keyboard, dialog, and 320/375px viewport checks."""

import json
import os
import select
import subprocess
import tempfile
import time
from pathlib import Path

from browser_support import ChromeDevTools, find_chrome, require_linux_network_isolation
from browser_workspace_offline import FAKE, PUBLIC

if os.name != "nt":
    import fcntl


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
    html = html.replace('href="/dashboard/styles.css"', "<style>" + css + "</style>")
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    html = html.replace("</body>", FAKE + "</body>")
    with tempfile.TemporaryDirectory(prefix="trendscout-cdp-") as directory:
        page = Path(directory) / "index.html"
        page.write_text(html, encoding="utf-8")
        if os.name == "nt":
            browser = ChromeDevTools(chrome, Path(directory) / "profile")
            call = browser.call
        else:
            p2c_r, p2c_w = os.pipe()
            c2p_r, c2p_w = os.pipe()
            child_r = fcntl.fcntl(p2c_r, fcntl.F_DUPFD, 10)
            child_w = fcntl.fcntl(c2p_w, fcntl.F_DUPFD, 10)
            os.set_inheritable(child_r, True)
            os.set_inheritable(child_w, True)

            def child_setup():
                os.dup2(child_r, 3)
                os.dup2(child_w, 4)

            env = {
                key: value
                for key, value in os.environ.items()
                if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
            }
            command = [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                "--disable-background-networking", "--disable-extensions", "--no-proxy-server",
                "--disable-sync", "--disable-component-update", "--allow-file-access-from-files",
                "--no-first-run", "--remote-debugging-pipe",
                "--user-data-dir=" + directory + "/profile", "about:blank"]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=False,
                preexec_fn=child_setup, env=env)
            for fd in (p2c_r, c2p_w, child_r, child_w):
                os.close(fd)
            pending = b""
            counter = 0

            def call(method, params=None, session=None):
                nonlocal pending, counter
                counter += 1
                message = {"id": counter, "method": method, "params": params or {}}
                if session:
                    message["sessionId"] = session
                os.write(p2c_w, json.dumps(message).encode() + b"\0")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    while b"\0" in pending:
                        raw, pending = pending.split(b"\0", 1)
                        if not raw:
                            continue
                        reply = json.loads(raw)
                        if reply.get("id") == counter:
                            if "error" in reply:
                                raise RuntimeError(str(reply["error"]))
                            return reply.get("result", {})
                    ready, _, _ = select.select([c2p_r], [], [], max(0, deadline - time.monotonic()))
                    if not ready:
                        break
                    chunk = os.read(c2p_r, 65536)
                    if not chunk:
                        break
                    pending += chunk
                raise RuntimeError("Chrome DevTools response timeout")

        try:
            target = call("Target.createTarget", {"url": "about:blank"})["targetId"]
            session = call("Target.attachToTarget", {"targetId": target, "flatten": True})[
                "sessionId"
            ]
            call("Page.enable", session=session)
            call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 375, "height": 820, "deviceScaleFactor": 1, "mobile": True},
                session,
            )
            call("Page.navigate", {"url": page.as_uri()}, session)

            def evaluate(expression):
                result = call(
                    "Runtime.evaluate", {"expression": expression, "returnByValue": True}, session
                )
                if "exceptionDetails" in result:
                    raise RuntimeError("evaluation failed: " + expression)
                return result["result"].get("value")

            for _ in range(150):
                if evaluate(
                    "document.readyState==='complete' && !document.documentElement.classList.contains('js-pending')"
                ):
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError("dashboard module did not finish loading")
            if evaluate("visualViewport.width") != 375:
                raise RuntimeError("visual viewport is not 375px: " + str(evaluate("visualViewport.width")))
            if evaluate("document.documentElement.scrollWidth > innerWidth"):
                raise RuntimeError("375px page has horizontal overflow")

            evaluate("document.querySelector('#menu-toggle').click()")
            if not evaluate("document.querySelector('#app-shell').classList.contains('menu-open')"):
                raise RuntimeError("mobile menu did not open")
            evaluate("document.querySelector('[data-nav=overview]').focus()")
            for event in ("keyDown", "keyUp"):
                call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event,
                        "key": "Tab",
                        "code": "Tab",
                        "windowsVirtualKeyCode": 9,
                        "nativeVirtualKeyCode": 9,
                    },
                    session,
                )
            if evaluate("document.activeElement.dataset.nav") != "collections":
                raise RuntimeError("Tab did not advance to Collections")
            for event in ("keyDown", "keyUp"):
                call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event,
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                    },
                    session,
                )
            for _ in range(60):
                if evaluate("!document.querySelector('[data-view=collections]').hidden"):
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError("Enter did not navigate to Collections")
            if evaluate("document.querySelector('#app-shell').classList.contains('menu-open')"):
                raise RuntimeError("navigation did not close the mobile menu")

            evaluate("document.querySelector('#detail-dialog').showModal()")
            if not evaluate("document.querySelector('#detail-dialog').open"):
                raise RuntimeError("detail dialog did not open")
            for event in ("keyDown", "keyUp"):
                call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event,
                        "key": "Escape",
                        "code": "Escape",
                        "windowsVirtualKeyCode": 27,
                        "nativeVirtualKeyCode": 27,
                    },
                    session,
                )
            if evaluate("document.querySelector('#detail-dialog').open"):
                raise RuntimeError("Escape did not close the dialog")

            evaluate("location.hash='#overview';document.querySelector('[data-dashboard-tab=overview]').focus()")
            call("Input.dispatchKeyEvent", {"type":"keyDown","key":"ArrowRight","code":"ArrowRight","windowsVirtualKeyCode":39,"nativeVirtualKeyCode":39}, session)
            if not evaluate("document.querySelector('[data-dashboard-tab=analysis]').getAttribute('aria-selected')==='true'"):
                raise RuntimeError("ArrowRight did not select Analysis tab")
            for width in (320, 375):
                call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": 820, "deviceScaleFactor": 1, "mobile": True},
                    session,
                )
                for view in (
                    "overview",
                    "analysis",
                    "collections",
                    "sources",
                    "runs",
                    "algorithms",
                    "quality",
                    "research",
                    "settings",
                ):
                    evaluate(
                        "location.hash='"
                        + "#"
                        + view
                        + "';window.dispatchEvent(new Event('hashchange'))"
                    )
                    if evaluate("document.documentElement.scrollWidth > innerWidth"):
                        raise RuntimeError(str(width) + "px horizontal overflow on " + view)
            evaluate(
                "document.querySelector('[data-theme-toggle]').click();document.querySelector('[data-locale-toggle]').click()"
            )
            if evaluate(
                "document.documentElement.dataset.theme!=='dark' || document.documentElement.lang!=='en'"
            ):
                raise RuntimeError("theme or locale control failed on mobile")
            evaluate("document.querySelector('[data-theme-toggle]').focus()")
            for event in ("keyDown", "keyUp"):
                call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event,
                        "key": " ",
                        "code": "Space",
                        "windowsVirtualKeyCode": 32,
                        "nativeVirtualKeyCode": 32,
                        "text": " ",
                    },
                    session,
                )
            if evaluate("document.documentElement.dataset.theme!=='light'"):
                raise RuntimeError("Space did not activate the theme control")
            print(
                "Chrome DevTools: keyboard navigation, Escape dialog, 320/375px all views, theme and locale: PASS"
            )
        finally:
            if os.name == "nt":
                browser.close()
            else:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                os.close(p2c_w)
                os.close(c2p_r)


if __name__ == "__main__":
    main()
