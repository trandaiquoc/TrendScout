"""Chrome checks dashboard rendering of rows returned by the real offline API pipeline."""

# The isolated browser runner adds Backend to sys.path before importing app modules.
# ruff: noqa: E402
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx
from browser_support import find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "Backend"
os.sys.path.insert(0, str(BACKEND))

from test_refresh_integration import SOURCE

from app.adapters.wikimedia import WikimediaAdapter
from app.main import create_app
from app.storage.fake import FakeStorage


def browser_page(run, keyword_page, detail):
    bundle = {
        "run": run,
        "keyword_page": keyword_page,
        "detail": detail,
    }
    payload = json.dumps(bundle, ensure_ascii=True, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"""<script>
const bundle={payload}; const requests=[];
window.fetch=(path,options={{}})=>{{
  const value=String(path).split('?')[0]; requests.push(value);
  let response={{}};
  if(value==='/api/health') response={{status:'ok'}};
  else if(value==='/api/config') response={{read_only:true}};
  else if(value===`/api/runs/${{bundle.run.run_id}}`) response=bundle.run;
  else if(value===`/api/runs/${{bundle.run.run_id}}/keywords`) response=bundle.keyword_page;
  else if(value===`/api/runs/${{bundle.run.run_id}}/keywords/${{bundle.detail.keyword_id}}`) response=bundle.detail;
  else return Promise.resolve(new Response('{{}}',{{status:404}}));
  return Promise.resolve(new Response(JSON.stringify(response),{{status:200,headers:{{'content-type':'application/json'}}}}));
}};
</script>"""


async def prepare_api_data():
    day = __import__("datetime").date.fromisoformat(SOURCE["analysis_date"])

    def source_response(request):
        observed = request.url.path[-10:].replace("/", "-")
        entry = SOURCE["days"].get(observed)
        if entry is None:
            return httpx.Response(404)
        if entry["http"] != 200:
            return httpx.Response(entry["http"], headers={"Retry-After": "0"})
        year, month, day_value = observed.split("-")
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        **SOURCE["scope"],
                        "year": year,
                        "month": month,
                        "day": day_value,
                        "articles": entry["articles"],
                    }
                ]
            },
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(source_response))
    adapter = WikimediaAdapter(upstream, max_retry_wait=0)

    class FixtureAdapter:
        async def collect(self):
            return await adapter.collect(day)

    with tempfile.TemporaryDirectory(prefix="trendscout-api-browser-") as directory:
        store = FakeStorage()
        app = create_app(
            storage=store,
            adapter_factory=FixtureAdapter,
            lock_path=str(Path(directory) / "lock"),
            operator_token="synthetic-browser-test-token-only",
            run_mode="synthetic",
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/runs",
                    headers={"Authorization": "Bearer synthetic-browser-test-token-only"},
                )
                if created.status_code != 202:
                    raise AssertionError(f"API create run returned {created.status_code}")
                run_id = created.json()["run_id"]
                for _ in range(200):
                    response = await client.get(f"/api/runs/{run_id}")
                    if response.status_code != 200:
                        raise AssertionError(f"API run read returned {response.status_code}")
                    run = response.json()
                    if run["status"] in ("COMPLETED", "PARTIAL", "FAILED", "ABORTED"):
                        break
                    await asyncio.sleep(0.005)
                else:
                    raise AssertionError("API run did not finish")
                if run["status"] != "PARTIAL":
                    raise AssertionError(
                        f"expected fixture partial run, got {run['status']}: {run.get('error')}"
                    )
                keyword_response = await client.get(
                    f"/api/runs/{run_id}/keywords",
                    params={"category": "all", "sort": "score", "limit": 100, "offset": 0},
                )
                if keyword_response.status_code != 200:
                    raise AssertionError("API keyword list did not return 200")
                keyword_page = keyword_response.json()
                if not keyword_page["items"]:
                    raise AssertionError("API keyword list is empty")
                chosen = keyword_page["items"][0]
                detail_response = await client.get(
                    f"/api/runs/{run_id}/keywords/{chosen['keyword_id']}"
                )
                if detail_response.status_code != 200:
                    raise AssertionError("API keyword detail did not return 200")
                detail = detail_response.json()
                if detail["keyword_id"] != chosen["keyword_id"]:
                    raise AssertionError("API list/detail identity mismatch")
                result = (run, keyword_page, detail)
    await upstream.aclose()
    return result


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    run, keyword_page, detail = asyncio.run(prepare_api_data())
    public = ROOT / "Frontend" / "public"
    html = (public / "index.html").read_text(encoding="utf-8")
    css = (public / "styles.css").read_text(encoding="utf-8")
    bridge = browser_page(run, keyword_page, detail)
    html = html.replace('href="/dashboard/styles.css"', f"<style>{css}</style>", 1)
    html = html.replace('src="/dashboard/app.js"', f'src="{(public / "app.js").as_uri()}"', 1)
    expectation = json.dumps(
        {
            "run_id": run["run_id"],
            "keyword_id": detail["keyword_id"],
            "display_keyword": detail.get("display_keyword"),
            "project": detail.get("project"),
            "score": detail.get("score"),
            "status": detail.get("status"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    check = """<script>
(async()=>{const e=EXPECTATION;const errors=[];const check=(x,m)=>{if(!x)errors.push(m)};const wait=ms=>new Promise(r=>setTimeout(r,ms));
await wait(200);location.hash='#runs';window.dispatchEvent(new Event('hashchange'));document.querySelector('#run-id').value=e.run_id;document.querySelector('#load-run').click();await wait(180);
check(document.querySelector('#runs-current').textContent===e.run_id,'run id');
check(document.querySelector('#run-status').textContent.includes('một phần'),'partial status');
check(document.querySelector('#rows').textContent.includes(e.display_keyword),'API title rendered');
const button=document.querySelector('#rows button');check(!!button,'row button');if(button)button.click();await wait(60);
check(document.querySelector('#detail-title').textContent===e.display_keyword,'detail title');
check(document.querySelector('#detail-subtitle').textContent.includes(e.project),'project provenance');
check(document.querySelector('#detail-content').textContent.includes(e.score===null?'Chưa đủ dữ liệu':String(e.score)),'score and sufficiency detail');
check(requests.includes('/api/runs/'+e.run_id+'/keywords/'+e.keyword_id),'detail API identity');
check(document.querySelector('#open-fetch').disabled,'fetch remains disabled');
document.body.setAttribute('data-e2e-requests',requests.join('|'));
document.body.setAttribute('data-e2e-state',JSON.stringify({run:document.querySelector('#runs-current').textContent,rows:document.querySelector('#rows').textContent,fetch:typeof window.fetch,scripts:document.scripts.length}));
document.body.setAttribute('data-e2e-result',errors.length?'FAIL: '+errors.join('; '):'PASS');})();
</script>""".replace("EXPECTATION", expectation)
    html = html.replace("</body>", bridge + check + "</body>")
    with tempfile.TemporaryDirectory(prefix="trendscout-browser-e2e-") as directory:
        page = Path(directory) / "index.html"
        page.write_text(html, encoding="utf-8")
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
            f"--user-data-dir={directory}/profile",
            "--window-size=1280,900",
            "--virtual-time-budget=5000",
            "--dump-dom",
            page.as_uri(),
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        }
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30, env=env)
        if result.returncode or 'data-e2e-result="PASS"' not in result.stdout:
            result_index = result.stdout.find('data-e2e-result=')
            body_index = result.stdout.rfind('<body', 0, result_index)
            marker = result.stdout[body_index : body_index + 1100] if result_index >= 0 else "missing browser result"
            raise SystemExit(
                f"Chrome pipeline/API/dashboard integration failed: {marker}; "
                f"browser stderr={result.stderr[-700:]}"
            )
    print(
        "Chrome real offline pipeline -> ASGI API -> dashboard rendered keyword "
        f"{detail['keyword_id']}: PASS"
    )


if __name__ == "__main__":
    main()
