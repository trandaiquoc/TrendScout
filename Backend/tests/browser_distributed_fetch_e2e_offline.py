"""Chrome check for the local demo's real distributed synthetic API response."""

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "Backend"
PUBLIC = ROOT / "Frontend" / "public"
sys.path.insert(0, str(BACKEND))

from app.demo_local import create_demo_app  # noqa: E402


async def demo_api_snapshot():
    application = create_demo_app()
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1"
        ) as client:
            capabilities = await client.get("/api/v1/capabilities")
            accepted = await client.post("/api/v1/fetches", json={}, headers={
                "Origin": "http://127.0.0.1",
                "Idempotency-Key": "browser-demo-public-fetch-0001",
            })
            if accepted.status_code != 202:
                raise AssertionError(f"demo Fetch returned {accepted.status_code}")
            status = await client.get(accepted.json()["status_url"])
            if status.status_code != 200 or not status.json().get("terminal"):
                raise AssertionError("demo Fetch did not return a terminal status")
            return capabilities.json(), status.json()


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
    capabilities, status = asyncio.run(demo_api_snapshot())
    fetch_id = status["fetch_id"]
    capability_payload = json.dumps(capabilities, ensure_ascii=True, separators=(",", ":"))
    status_payload = json.dumps(status, ensure_ascii=True, separators=(",", ":"))
    html = (PUBLIC / "index.html").read_text()
    css = (PUBLIC / "styles.css").read_text()
    html = html.replace('href="/dashboard/styles.css"', "<style>" + css + "</style>")
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    bridge = f"""<script>
const fetchRequests=[];let statusReads=0;
const demoCapabilities={capability_payload};
const demoStatus={status_payload};
window.fetch=(path,options={{}})=>{{
  const value=String(path).split('?')[0];const requestOptions=options||{{}};
  fetchRequests.push({{path:value,method:requestOptions.method||'GET',headers:requestOptions.headers||{{}},body:requestOptions.body||null}});
  let body={{}};
  if(value==='/api/health') body={{status:'ok'}};
  else if(value==='/api/v1/capabilities') body=demoCapabilities;
  else if(value==='/api/v1/fetches'&&requestOptions.method==='POST') body={{fetch_id:{json.dumps(fetch_id)},status:'QUEUED',data_mode:'SYNTHETIC'}};
  else if(value==='/api/v1/fetches/{fetch_id}'){{statusReads+=1;body=demoStatus;}}
  else return Promise.resolve(new Response('{{}}',{{status:404,headers:{{'content-type':'application/json'}}}}));
  return Promise.resolve(new Response(JSON.stringify(body),{{status:200,headers:{{'content-type':'application/json','cache-control':'no-store'}}}}));
}};
</script>"""
    check = """<script>
(async()=>{const errors=[];const check=(value,message)=>{if(!value)errors.push(message)};const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
await wait(200);const button=document.querySelector('#open-fetch');check(!button.disabled,'capability-enabled Fetch button');button.click();button.click();await wait(300);
const posts=fetchRequests.filter(request=>request.path==='/api/v1/fetches'&&request.method==='POST');
check(posts.length===1,'duplicate click submits one POST');check(posts[0]?.body==='{}','empty server-controlled request body');
check(Boolean(posts[0]?.headers['Idempotency-Key']),'idempotency key is present');check(statusReads===1,'one status poll reaches terminal result');
check(document.querySelector('#fetch-status').textContent.includes('Google Search'),'localized source status is visible');
check(document.querySelector('#fetch-status').textContent.includes('SYNTHETIC'),'synthetic origin is visible');
check(document.querySelector('#fetch-status').textContent.includes('FRESH'),'fresh delivery is visible');
check(document.querySelector('#rows').textContent.includes('quiet luxury'),'public Fetch populates keyword discovery table');
check(document.querySelector('#rows').textContent.includes('CHƯA ĐỦ DỮ LIỆU'),'single-snapshot candidate is labeled insufficient');
check(!document.querySelector('input[type="password"]'),'no password input is present');
check(![...document.querySelectorAll('button,a')].some(item=>/\b(login|log in|register|logout|sign up)\b/i.test(item.textContent)),'no login or account controls are present');
const search=document.querySelector('#search');search.value='quiet luxury';search.dispatchEvent(new Event('input',{bubbles:true}));await wait(450);
check(document.querySelector('#rows').textContent.includes('quiet luxury'),'keyword search filters public Fetch candidates');
document.querySelector('[data-category="fashion"]')?.click();await wait(50);
check(document.querySelector('#rows').textContent.includes('quiet luxury'),'category filter retains matching candidate');
document.querySelector('.row-detail')?.click();await wait(100);check(document.querySelector('#detail-content').textContent.includes('US_FILTER_ONLY'),'keyword detail shows source and market evidence');
check(!fetchRequests.some(request=>request.path.startsWith('/api/runs/')),'distributed result does not require an operator run lookup');
check(!/token|secret|credential|password|api_key/i.test(JSON.stringify(fetchRequests)),'no credentials sent from browser');
document.body.setAttribute('data-distributed-fetch-result',errors.length?'FAIL: '+errors.join('; '):'PASS');})();
</script>"""
    html = html.replace("</body>", bridge + check + "</body>")
    with tempfile.TemporaryDirectory(prefix="trendscout-distributed-fetch-") as directory:
        page = Path(directory) / "index.html"
        page.write_text(html)
        command = [
            chrome, "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-background-networking", "--disable-extensions", "--no-proxy-server",
            "--disable-sync", "--disable-component-update", "--allow-file-access-from-files",
            "--no-first-run", f"--user-data-dir={directory}/profile", "--window-size=1280,900",
            "--virtual-time-budget=5000", "--dump-dom", page.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode or 'data-distributed-fetch-result="PASS"' not in result.stdout:
            marker = result.stdout[result.stdout.find("data-distributed-fetch-result=") - 200:][:900]
            raise SystemExit(
                f"Distributed Fetch Chrome E2E failed: {marker}; browser stderr={result.stderr[-700:]}"
            )
    print("Chrome demo API → distributed synthetic Fetch → keyword detail: PASS")


if __name__ == "__main__":
    main()
