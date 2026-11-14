"""Focused native Chrome check for Run History detail accessibility."""

import json
import tempfile
import time
from pathlib import Path

from browser_d2_visual_offline import FAKE
from browser_support import ChromeDevTools, find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
RUN_ID = "e" * 32


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")

    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'href="/dashboard/styles.css"',
        'href="' + (PUBLIC / "styles.css").as_uri() + '"',
    )
    html = html.replace(
        'src="/dashboard/app.js"',
        'src="' + (PUBLIC / "app.js").as_uri() + '"',
    )
    fallback = "  return Promise.resolve(new Response('{}',{status:404}));"
    detail = (
        "  if(value==='/api/v1/runs/history/'+ 'e'.repeat(32))"
        "return Promise.resolve(new Response(JSON.stringify({item:{"
        "fetch_id:'e'.repeat(32),status:'PARTIAL',data_mode:'SYNTHETIC',"
        "source_manifest:[{source_id:'wikimedia_pageviews_top_per_country',"
        "status:'PARTIAL',record_count:3}],provider_record_count:4,"
        "received_record_count:3,retained_record_count:2,rejected_record_count:1,"
        "observation_start:'2026-11-10',observation_end:'2026-11-10',"
        "fetched_at:'2026-11-11T03:00:00Z',quality_flags:['SYNTHETIC_FIXTURE']"
        "}}),{status:200}));"
    )
    if FAKE.count(fallback) != 1:
        raise RuntimeError("offline fixture fallback changed; update the focused G5 mock")
    fake = FAKE.replace(fallback, detail + chr(10) + fallback, 1)
    html = html.replace("</body>", fake + "</body>")

    with tempfile.TemporaryDirectory(prefix="trendscout-g5-run-detail-") as directory:
        page = Path(directory) / "index.html"
        page.write_text(html, encoding="utf-8")
        browser = ChromeDevTools(chrome, Path(directory) / "profile")
        try:
            call = browser.call
            target = call("Target.createTarget", {"url": "about:blank"})["targetId"]
            session = call(
                "Target.attachToTarget", {"targetId": target, "flatten": True}
            )["sessionId"]
            call("Page.enable", session=session)
            call("Runtime.enable", session=session)
            call("Accessibility.enable", session=session)
            call("Page.navigate", {"url": page.as_uri()}, session)

            def evaluate(expression):
                response = call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    session,
                )
                if "exceptionDetails" in response:
                    raise RuntimeError("Chrome evaluation failed: " + expression)
                return response.get("result", {}).get("value")

            def wait_for(expression, timeout=8):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if evaluate(expression):
                        return True
                    time.sleep(0.03)
                return False

            if not wait_for(
                "document.readyState==='complete' && "
                "!document.documentElement.classList.contains('js-pending')"
            ):
                raise RuntimeError("TrendScout did not initialize")
            # Let the initial offline capability responses settle before navigation.
            time.sleep(1)
            evaluate("document.querySelector('[data-nav=\\\"runs\\\"]').click()")
            time.sleep(0.8)
            if not wait_for("document.querySelectorAll('#run-history button').length > 0"):
                raise RuntimeError("mock Run History row did not render")

            evaluate("document.querySelector('#run-history button').click()")
            time.sleep(0.4)
            title = evaluate("document.querySelector('#run-history-detail h2').textContent")
            if title != "Chi tiết lần chạy":
                raise RuntimeError("Vietnamese detail title is not customer-facing")
            if evaluate("document.activeElement.id !== 'run-history-detail'"):
                raise RuntimeError("opening detail did not move focus to the detail region")

            root = call("DOM.getDocument", {"depth": -1}, session)["root"]["nodeId"]
            node = call(
                "DOM.querySelector",
                {"nodeId": root, "selector": "#run-history-detail"},
                session,
            )["nodeId"]
            backend_node = call("DOM.describeNode", {"nodeId": node}, session)[
                "node"
            ]["backendNodeId"]
            ax_node = call(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_node, "fetchRelatives": False},
                session,
            )["nodes"][0]
            if (
                ax_node.get("role", {}).get("value") != "region"
                or ax_node.get("name", {}).get("value") != title
            ):
                raise RuntimeError("Chrome accessibility tree has the wrong detail name")

            call(
                "Input.dispatchKeyEvent",
                {
                    "type": "rawKeyDown",
                    "key": "Escape",
                    "code": "Escape",
                    "windowsVirtualKeyCode": 27,
                    "nativeVirtualKeyCode": 27,
                },
                session,
            )
            call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "Escape",
                    "code": "Escape",
                    "windowsVirtualKeyCode": 27,
                    "nativeVirtualKeyCode": 27,
                },
                session,
            )
            state = evaluate(
                "JSON.stringify({hidden:document.querySelector('#run-history-detail').hidden,"
                "triggerFocused:document.activeElement.matches('#run-history button'),"
                "expanded:document.querySelector('#run-history button').getAttribute('aria-expanded')})"
            )
            state = json.loads(state)
            if state != {"hidden": True, "triggerFocused": True, "expanded": "false"}:
                raise RuntimeError("Escape did not close and restore the Run History trigger")

            evaluate("document.querySelector('#run-history button').click()")
            time.sleep(0.2)
            evaluate("document.querySelector('[data-locale-toggle]').click()")
            time.sleep(0.2)
            if not evaluate(
                "document.documentElement.lang==='en' && "
                "document.querySelector('#run-history-detail h2').textContent==='Run details'"
            ):
                raise RuntimeError("open Run History detail did not relocalize")
            print(
                "PASS: VI/EN detail names, Chrome AX region name, focus entry, "
                "Escape close and focus return, aria-expanded reset, live relocalization."
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
