"""Offline Chrome check for anonymous, capability-gated one-click synthetic Fetch."""

import json
import subprocess
import tempfile
from pathlib import Path
from browser_support import find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
RUN_ID = "a" * 32
FETCH_ID = "f" * 43


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
    html = html.replace('href="/dashboard/styles.css"', "<style>" + css + "</style>")
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    bridge = f"""<script>
const fetchRequests=[];let statusReads=0;
window.fetch=(path,options={{}})=>{{
  const value=String(path).split('?')[0];const requestOptions=options||{{}};
  fetchRequests.push({{path:value,method:requestOptions.method||'GET',headers:requestOptions.headers||{{}},body:requestOptions.body||null}});
  let body={{}};
  if(value==='/api/health') body={{status:'ok'}};
  else if(value==='/api/v1/capabilities') body={{contract_version:'1.0',authentication_required:false,public_fetch_enabled:true,fetch_enabled:true,data_mode:'SYNTHETIC',fetch_block_reason:null,sources:[
    {{source_id:'wikimedia_pageviews_top_per_country',adapter_status:'IMPLEMENTED',approval_status:'PENDING_LIVE_APPROVAL',live_verification_status:'UNVERIFIED',enabled_for_current_operation:true}},
    {{source_id:'youtube_video_search',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',live_verification_status:'NOT_VERIFIED',supported_data_modes:['SYNTHETIC'],enabled_for_current_operation:false}},
    {{source_id:'google_web_serp',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',live_verification_status:'NOT_VERIFIED',supported_data_modes:['SYNTHETIC'],enabled_for_current_operation:false}},
    {{source_id:'bing_web_serp',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',live_verification_status:'NOT_VERIFIED',supported_data_modes:['SYNTHETIC'],enabled_for_current_operation:false}}
  ]}};
  else if(value==='/api/v1/fetches'&&requestOptions.method==='POST') body={{fetch_id:{json.dumps(FETCH_ID)},status:'QUEUED',data_mode:'SYNTHETIC'}};
  else if(value==='/api/v1/fetches/{FETCH_ID}'){{statusReads+=1;body={{fetch_id:{json.dumps(FETCH_ID)},run_id:{json.dumps(RUN_ID)},status:'PARTIAL',stage:'PARTIAL',data_mode:'SYNTHETIC',terminal:true}};}}
  else if(value==='/api/runs/{RUN_ID}') body={{run_id:{json.dumps(RUN_ID)},status:'PARTIAL',mode:'synthetic',data_date:'2026-11-06',keyword_count:1,scored_count:0,coverage:[],fetched_at:'2026-11-07T00:00:00Z',synthetic_discovery:{{data_mode:'SYNTHETIC',shown_count:1,candidate_count:1,sources:[{{source_id:'google_web_serp',status:'PARTIAL',record_count:1,stop_reason:'QUERY_CAP_REACHED'}}],candidates:[{{keyword:'<img src=x onerror=alert(1)>',categories:['fashion'],assessments:[{{classification:'INSUFFICIENT_EVIDENCE'}}],evidence:[{{source_id:'google_web_serp',data_mode:'SYNTHETIC',delivery_mode:'CACHED',fetched_at:'2026-11-07T00:00:00Z',scope:'US filter',query_evidence:['fixture']}}]}}]}}}};
  else if(value==='/api/runs/{RUN_ID}/keywords') body={{items:[{{keyword_id:'b'.repeat(64),display_keyword:'Synthetic phrase',canonical_keyword:'synthetic phrase',score:null,status:'INSUFFICIENT_DATA',categories:[],current_views_ceil:0}}],total:1,offset:0,limit:20,next_offset:null}};
  else return Promise.resolve(new Response('{{}}',{{status:404,headers:{{'content-type':'application/json'}}}}));
  return Promise.resolve(new Response(JSON.stringify(body),{{status:200,headers:{{'content-type':'application/json','cache-control':'no-store'}}}}));
}};
</script>"""
    check = """<script>
(async()=>{const errors=[];const check=(value,message)=>{if(!value)errors.push(message)};const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
await wait(200);
const button=document.querySelector('#open-fetch');
check(!button.disabled,'capability-enabled Fetch button');
check(!document.querySelector('[id*=login],[id*=logout],[id*=register]'),'no account controls');
button.click();button.click();await wait(300);
const posts=fetchRequests.filter(request=>request.path==='/api/v1/fetches'&&request.method==='POST');
check(posts.length===1,'duplicate click submits one POST');
check(posts[0]?.body==='{}','body is server-default empty object');
check(Boolean(posts[0]?.headers['Idempotency-Key']),'request has an idempotency key');
check(!fetchRequests.some(request=>/token|secret|credential|password/i.test(JSON.stringify(request.headers))),'no credential sent');
check(statusReads===1,'one bounded status read receives terminal state');
check(document.querySelector('#fetch-status').textContent.includes('độ phủ nguồn chưa đầy đủ'),'partial synthetic state announced');
check(document.querySelector('#runs-current').textContent===RUN_ID,'run results load without user-entered ID');
check(document.querySelector('#rows').textContent.includes('Synthetic phrase'),'synthetic result rendered');
check(document.querySelector('#demo-discovery-list').textContent.includes('INSUFFICIENT_EVIDENCE'),'discovery assessment rendered');
check(document.querySelector('#demo-discovery-list').textContent.includes('<img src=x onerror=alert(1)>'),'untrusted keyword rendered as text');
check(!document.querySelector('#demo-discovery-list img'),'untrusted keyword is not HTML');
check(document.querySelector('#demo-discovery-list').textContent.includes('CACHED'),'cache provenance rendered');
document.querySelector('[data-category=home_goods]').click();
check(!document.querySelector('#demo-discovery-list').textContent.includes('INSUFFICIENT_EVIDENCE'),'synthetic candidates respect category filter');
document.querySelector('[data-category=fashion]').click();
check(document.querySelector('#demo-discovery-list').textContent.includes('INSUFFICIENT_EVIDENCE'),'matching category restores candidate');
check(document.querySelector('[data-source=youtube]').dataset.sourceStatus==='implemented_offline_verified-not_verified','offline-implemented source remains live-disabled');
check(!Object.keys(localStorage).some(key=>/token|secret|credential|password/i.test(key)),'no credentials stored');
document.body.setAttribute('data-public-fetch-result',errors.length?'FAIL: '+errors.join('; '):'PASS');})();
</script>""".replace("RUN_ID", json.dumps(RUN_ID))
    html = html.replace("</body>", bridge + check + "</body>")
    with tempfile.TemporaryDirectory(prefix="trendscout-public-fetch-") as directory:
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
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30)
        if result.returncode or 'data-public-fetch-result="PASS"' not in result.stdout:
            marker = result.stdout[result.stdout.find("data-public-fetch-result=") - 200 :][:900]
            raise SystemExit(
                f"Public Fetch Chrome E2E failed: {marker}; browser stderr={result.stderr[-700:]}"
            )
    print(
        "Chrome public one-click synthetic Fetch, duplicate-click guard, polling, and result display: PASS"
    )


if __name__ == "__main__":
    main()
