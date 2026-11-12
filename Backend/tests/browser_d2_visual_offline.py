"""Capture reviewed D2 screenshots from the offline dashboard fixture."""

import base64
import tempfile
import time
from pathlib import Path

from browser_support import ChromeDevTools, find_chrome, require_linux_network_isolation

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
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'href="/dashboard/styles.css"', 'href="' + (PUBLIC / "styles.css").as_uri() + '"'
    )
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    html = html.replace("</body>", FAKE + "</body>")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trendscout-d2-visual-") as directory:
        page = Path(directory) / "index.html"
        page.write_text(html, encoding="utf-8")
        browser = ChromeDevTools(chrome, Path(directory) / "profile")
        call = browser.call
        console_errors = []

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
                    destination.as_posix()
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
            for event in browser.events:
                if event.get("method") == "Runtime.exceptionThrown":
                    details = event.get("params", {}).get("exceptionDetails", {})
                    exception = details.get("exception", {})
                    console_errors.append(details.get("text") or exception.get("description") or "uncaught exception")
                if event.get("method") == "Runtime.consoleAPICalled":
                    params = event.get("params", {})
                    if params.get("type") == "error":
                        message = " ".join(
                            argument.get("value", argument.get("description", ""))
                            for argument in params.get("args", [])
                        )
                        console_errors.append(message or "console.error")
            if console_errors:
                raise RuntimeError("Browser console errors: " + " | ".join(console_errors))
            print("Visual evidence: four theme/locale and screen combinations captured.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
