"""Capture reviewed D2 screenshots from the offline dashboard fixture."""

import base64
import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
OUTPUT = ROOT / "Documents" / "Reports" / "evidence" / "d2"
RUN_ID = "a" * 32
KEYWORD_ID = "c" * 64

FAKE = r"""<script>
window.fetch=(path,options={})=>{
  const value=String(path).split('?')[0];
  if(value==='/api/health')return Promise.resolve(new Response(JSON.stringify({status:'ok'}),{status:200}));
  if(value==='/api/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')return Promise.resolve(new Response(JSON.stringify({
    run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',status:'COMPLETED',keyword_count:124,
    scored_count:28,data_date:'2026-11-10',mode:'synthetic',fetched_at:'2026-11-11T03:00:00Z',
    coverage:[{date:'2026-11-10',status:'SUCCESS'}],algorithm_version:'wikimedia-trend-v0.1'
  }),{status:200}));
  if(value==='/api/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/keywords')return Promise.resolve(new Response(JSON.stringify({
    items:[{keyword_id:'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
      display_keyword:'Example topic',canonical_keyword:'example topic',project:'en.wikipedia',
      score:86.4,status:'EMERGING',current_views_ceil:8400,baseline:2400,ratio:3.5,
      observation_date:'2026-11-10',quality_flags:[]}],
    total:1,next_offset:null,empty_reason:null
  }),{status:200}));
  if(value==='/api/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/keywords/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc')return Promise.resolve(new Response(JSON.stringify({
    keyword_id:'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    display_keyword:'Example topic',canonical_keyword:'example topic',source:'Wikimedia',
    project:'en.wikipedia',observation_date:'2026-11-10',score:86.4,status:'EMERGING',
    current_views_ceil:8400,baseline:2400,ratio:3.5,observed_baseline_days:7,
    algorithm_version:'wikimedia-trend-v0.1',categories:['unclassified'],
    entity_type:'UNKNOWN',commercial_relevance:'NOT_ASSESSED',article_original:'Example topic',
    country_scope:'US',access:'all-access',score_components:{growth:0.78,volume:0.64,max_views_ceil:8400},
    missing_baseline_dates:[],baseline_observations:[{date:'2026-11-09',views_ceil:2400}],
    classification_evidence:[],quality_flags:[],evidence_url:'https://en.wikipedia.org/wiki/Example'
  }),{status:200}));
  return Promise.resolve(new Response('{}',{status:404}));
};
</script>"""


def main():
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            probe = socket.socket(family)
            probe.close()
        except PermissionError:
            continue
        raise SystemExit("Internet blocker is not active")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text()
    html = html.replace(
        'href="/dashboard/styles.css"', 'href="' + (PUBLIC / "styles.css").as_uri() + '"'
    )
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    html = html.replace("</body>", FAKE + "</body>")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trendscout-d2-visual-") as directory:
        page = Path(directory) / "index.html"
        page.write_text(html)
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        child_r = os.dup(to_chrome_r)
        child_w = os.dup(from_chrome_w)
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
        command = [
            chrome,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-extensions",
            "--no-proxy-server",
            "--disable-sync",
            "--disable-component-update",
            "--allow-file-access-from-files",
            "--no-first-run",
            "--remote-debugging-pipe",
            "--user-data-dir=" + directory + "/profile",
            "about:blank",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False,
            preexec_fn=child_setup,
            env=env,
        )
        for fd in (to_chrome_r, from_chrome_w, child_r, child_w):
            os.close(fd)
        pending = b""
        message_id = 0
        console_errors = []

        def call(method, params=None, session=None):
            nonlocal pending, message_id
            message_id += 1
            message = {"id": message_id, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            os.write(to_chrome_w, json.dumps(message).encode() + b"\0")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                while b"\0" in pending:
                    raw, pending = pending.split(b"\0", 1)
                    if not raw:
                        continue
                    response = json.loads(raw)
                    if response.get("id") == message_id:
                        if "error" in response:
                            raise RuntimeError(str(response["error"]))
                        return response.get("result", {})
                    if response.get("method") == "Runtime.exceptionThrown":
                        details = response.get("params", {}).get("exceptionDetails", {})
                        exception = details.get("exception", {})
                        console_errors.append(
                            details.get("text")
                            or exception.get("description")
                            or "uncaught exception"
                        )
                    if response.get("method") == "Runtime.consoleAPICalled":
                        params = response.get("params", {})
                        if params.get("type") == "error":
                            message = " ".join(
                                argument.get("value", argument.get("description", ""))
                                for argument in params.get("args", [])
                            )
                            console_errors.append(message or "console.error")
                ready, _, _ = select.select(
                    [from_chrome_r], [], [], max(0, deadline - time.monotonic())
                )
                if not ready:
                    break
                chunk = os.read(from_chrome_r, 65536)
                if not chunk:
                    break
                pending += chunk
            raise RuntimeError("Chrome DevTools response timeout: " + method)

        try:
            target = call("Target.createTarget", {"url": "about:blank"})["targetId"]
            session = call("Target.attachToTarget", {"targetId": target, "flatten": True})[
                "sessionId"
            ]
            call("Page.enable", session=session)
            call("Runtime.enable", session=session)
            call("Page.navigate", {"url": page.as_uri()}, session)

            def evaluate(expression, await_promise=False):
                result = call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": await_promise,
                    },
                    session,
                )
                if "exceptionDetails" in result:
                    raise RuntimeError("Chrome evaluation failed: " + expression)
                return result.get("result", {}).get("value")

            for _ in range(180):
                if evaluate(
                    "document.readyState==='complete' && !document.documentElement.classList.contains('js-pending')"
                ):
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError("dashboard did not load")

            async_def_js = """
window.waitFor=async function(p){for(let n=0;n<100;n++){if(p())return true;await new Promise(r=>setTimeout(r,20));}return false;};
"""
            evaluate(async_def_js)

            def set_viewport(width, height=900):
                call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": 1,
                        "mobile": width <= 500,
                    },
                    session,
                )
                time.sleep(0.12)
                actual = evaluate("innerWidth")
                overflow = evaluate("document.documentElement.scrollWidth > innerWidth")
                if actual != width:
                    raise RuntimeError(
                        "viewport mismatch: wanted " + str(width) + ", got " + str(actual)
                    )
                if overflow:
                    raise RuntimeError(str(width) + "px horizontal overflow")

            def set_preferences(theme, locale):
                current_theme = evaluate("document.documentElement.dataset.theme")
                current_locale = evaluate("document.documentElement.lang")
                if current_theme != theme:
                    evaluate("document.querySelector('[data-theme-toggle]').click()")
                if current_locale != locale:
                    evaluate("document.querySelector('[data-locale-toggle]').click()")
                time.sleep(0.12)
                if (
                    evaluate("document.documentElement.dataset.theme") != theme
                    or evaluate("document.documentElement.lang") != locale
                ):
                    raise RuntimeError("theme or locale did not update")

            def go(view):
                evaluate(
                    "location.hash='#" + view + "';window.dispatchEvent(new Event('hashchange'))"
                )
                time.sleep(0.08)

            def capture(name, width, full_page=True, configure=True):
                if configure:
                    set_viewport(width, 900 if width > 500 else 820)
                elif evaluate("innerWidth") != width:
                    raise RuntimeError("screenshot viewport changed unexpectedly")
                layout = call("Page.getLayoutMetrics", session=session)
                bounds = layout.get(
                    "cssContentSize", {"width": width, "height": 900 if width > 500 else 820}
                )
                params = {"format": "webp", "quality": 76, "captureBeyondViewport": full_page}
                if full_page:
                    params["clip"] = {
                        "x": 0,
                        "y": 0,
                        "width": min(width, bounds["width"]),
                        "height": min(5000, bounds["height"]),
                        "scale": 1,
                    }
                image_data = call("Page.captureScreenshot", params, session)["data"]
                destination = OUTPUT / name
                destination.write_bytes(base64.b64decode(image_data))
                print(
                    destination.relative_to(ROOT).as_posix()
                    + ": "
                    + str(destination.stat().st_size)
                    + " bytes"
                )

            set_viewport(1280)
            set_preferences("light", "vi")
            go("overview")
            capture("desktop-light-vi-overview.webp", 1280)

            set_preferences("dark", "en")
            capture("desktop-dark-en-overview.webp", 1280)

            set_preferences("light", "vi")
            set_viewport(375, 820)
            go("sources")
            capture("mobile-light-vi-sources.webp", 375)

            set_preferences("dark", "en")
            go("overview")
            capture("mobile-dark-en-overview.webp", 375)
            set_viewport(1280)
            go("runs")
            evaluate(
                "document.querySelector('#run-id').value='"
                + RUN_ID
                + "';document.querySelector('#load-run').click()"
            )
            ready = evaluate(
                "waitFor(()=>document.querySelector('#runs-current').textContent==='"
                + RUN_ID
                + "' && document.querySelector('#rows').children.length>0)",
                True,
            )
            if not ready:
                raise RuntimeError("keyword fixture did not load")
            go("keywords")
            evaluate("document.querySelector('#rows button').click()")
            dialog_ready = evaluate(
                "waitFor(()=>document.querySelector('#detail-content').children.length>0)", True
            )
            if not dialog_ready:
                raise RuntimeError("keyword detail fixture did not load")
            capture("desktop-dark-en-keyword-detail.webp", 1280, full_page=False)
            if console_errors:
                raise RuntimeError("Browser console errors: " + " | ".join(console_errors))
            print("Visual evidence: four theme/locale and screen combinations captured.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            os.close(to_chrome_w)
            os.close(from_chrome_r)


if __name__ == "__main__":
    main()
