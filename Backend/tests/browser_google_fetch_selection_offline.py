"""Chrome exercise of the real Fetch selector with offline API fixtures only."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from browser_support import find_chrome, require_linux_network_isolation
from browser_workspace_offline import FAKE

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"

FETCH_FIXTURE = r"""
window.fixtureFetches=[];
window.fixtureFetchResults={};
window.fetch=(function(original){
  return (path,options={})=>{
    const request={path:String(path),method:options.method||'GET',headers:options.headers||{},body:options.body||null};
    testRequests.push(request);
    const body=request.body?JSON.parse(request.body):{};
    if(request.path==='/api/v1/fetches'&&request.method==='POST'){
      const fetchId='a'.repeat(31)+(window.fixtureFetches.length+1).toString(16);
      const selected=Array.isArray(body.sources)?body.sources:[
        'wikimedia_pageviews_top_per_country','google_web_serp'
      ];
      const sources=selected.map(sourceId=>({
        source_id:sourceId,status:'SUCCESS',data_mode:'SYNTHETIC',delivery_state:'FRESH',record_count:1,
        records:[],quality_flags:[],coverage:sourceId==='google_web_serp'?{
          discovery_mode:'RSS_LIMITED_DISCOVERY',trending_candidate_count:1,
          distinct_keyword_count:1,serp_verified_candidate_count:0,
          provider_data_at:'2026-11-15T11:00:00Z',fetched_at:'2026-11-15T11:01:00Z'
        }:{provider_returned_records:1,accepted_records:1,rejected_records:0}
      }));
      const result={fetch_id:fetchId,status:'COMPLETED',terminal:true,data_mode:'SYNTHETIC',
        selected_sources:selected,sources,finished_at:'2026-11-15T12:00:00Z',
        discovery:{status:'SUCCESS',data_mode:'SYNTHETIC',candidate_count:1,shown_count:1,
          rejected_record_count:0,candidates:[],limitations:[]}};
      window.fixtureFetches.push({body,selected});
      window.fixtureFetchResults[fetchId]=result;
      return Promise.resolve(new Response(JSON.stringify({fetch_id:fetchId,data_mode:'SYNTHETIC'}),{status:202}));
    }
    const match=request.path.match(/^\/api\/v1\/fetches\/([a-f0-9]{32})$/);
    if(match&&request.method==='GET'){
      const result=window.fixtureFetchResults[match[1]];
      window.fixtureLastStatus=result||null;
      return Promise.resolve(new Response(JSON.stringify(result||{}),{status:result?200:404}));
    }
    const response=original(path,options);
    if(request.path==='/api/v1/capabilities')return response.then(async value=>{
      const payload=await value.json();window.fixtureCapabilities=payload;
      return new Response(JSON.stringify(payload),{status:200});
    });
    return response;
  };
})(window.fetch);
"""

CHECK = r"""<script>
(async()=>{
  const errors=[];const check=(value,message)=>{if(!value)errors.push(message)};
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const until=async predicate=>{for(let i=0;i<100;i++){if(predicate())return true;await wait(20)}return false};
  try{
    check(await until(()=>document.querySelector('#fetch-source-select').options.length===3
      &&!document.querySelector('#open-fetch').disabled),'capabilities make the source selector ready');
    const select=document.querySelector('#fetch-source-select');
    window.__fetchButtonClicks=0;
    document.querySelector('#open-fetch').addEventListener('click',()=>window.__fetchButtonClicks++,{capture:true});
    check(!document.querySelector('#fetch-source-control').hidden,'source selector is visible when multiple sources are enabled');
    check([...select.options].some(option=>option.value==='google_web_serp'),'Google is selectable');
    check([...select.options].some(option=>option.value==='wikimedia_pageviews_top_per_country'),'Wikimedia remains selectable');
    check(select.value==='','ALL is the default selection');

    window.__buttonDisabledBeforeClick=document.querySelector('#open-fetch').disabled;
    document.querySelector('#open-fetch').click();
    check(await until(()=>window.fixtureFetches.length===1),'ALL Fetch is submitted');
    check(await until(()=>document.querySelector('#demo-source-list').children.length===2),'combined result renders both enabled sources');
    check(Object.keys(window.fixtureFetches[0].body).length===0,'ALL selection submits the default empty body');
    check(window.fixtureFetches[0].selected.includes('google_web_serp')&&window.fixtureFetches[0].selected.includes('wikimedia_pageviews_top_per_country'),'ALL includes Google and Wikimedia when both are enabled');
    check(document.querySelector('#demo-source-list').textContent.includes('Google / SerpApi'),'combined result shows Google');
    check(document.querySelector('#demo-source-list').textContent.includes('Wikimedia'),'combined result preserves Wikimedia');
    check(document.querySelector('#fetch-status').textContent.includes('0 \u0111\u00e3 \u0111\u1ed1i chi\u1ebfu SERP'),'Google summary reports zero SERP verifications');
    check(document.querySelector('#fetch-status').textContent.includes('ngu\u1ed3n c\u1ea5p gi\u1edbi h\u1ea1n'),'Google summary identifies limited RSS discovery');
    check(document.querySelector('#overview-context').textContent.includes('T\u1ed4NG H\u1ee2P'),'synthetic result context is localized as synthetic');
    check(!document.querySelector('#overview-context').textContent.includes('LIVE'),'synthetic result context does not claim LIVE');
    check(window.__fetchTerminalError===undefined,'combined result rendering completes without callback error');

    select.value='google_web_serp';
    document.querySelector('#open-fetch').click();
    check(await until(()=>window.fixtureFetches.length===2),'Google-only Fetch is submitted');
    check(await until(()=>document.querySelector('#demo-source-list').children.length===1&&document.querySelector('#demo-source-list').textContent.includes('Google / SerpApi')),'Google-only result replaces the combined result');
    check(JSON.stringify(window.fixtureFetches[1].body)==='{"sources":["google_web_serp"]}','Google selection is sent in the Fetch request');
    check(!document.querySelector('#demo-source-list').textContent.includes('Wikimedia'),'Google-only result omits Wikimedia');
    check(window.__fetchTerminalError===undefined,'Google-only result rendering completes without callback error');
    check(window.fixtureFetches.every(item=>item.body.api_key===undefined),'no credential in Fetch body');
    check(!testRequests.some(request=>/api[_-]?key|serpapi/i.test(request.path)),'no provider or key endpoint requested by the browser');
  }catch(error){errors.push('browser exception: '+String(error));}
  if(errors.length)errors.push('diagnostic '+JSON.stringify({
    capabilities:{mode:window.fixtureCapabilities?.data_mode,localLive:window.fixtureCapabilities?.local_live_demo,
      sourceSelection:window.fixtureCapabilities?.source_selection_supported,
      enabled:window.fixtureCapabilities?.sources?.filter(source=>source.enabled_for_current_operation).map(source=>source.source_id)},
    selector:{hidden:document.querySelector('#fetch-source-control')?.hidden,
      disabled:document.querySelector('#fetch-source-select')?.disabled,
      options:[...(document.querySelector('#fetch-source-select')?.options||[])].map(option=>option.value),
      buttonDisabled:document.querySelector('#open-fetch')?.disabled,clicks:window.__fetchButtonClicks,
      buttonDisabledBeforeClick:window.__buttonDisabledBeforeClick,
      cryptoRandomUUID:typeof globalThis.crypto?.randomUUID},
    lastStatus:window.fixtureLastStatus,
    fetches:window.fixtureFetches,
    fetchRequests:window.testRequests.filter(request=>request.path.includes('/api/v1/fetches')),
    sourceList:document.querySelector('#demo-source-list')?.textContent,
    context:document.querySelector('#overview-context')?.textContent,
    status:document.querySelector('#fetch-status')?.textContent,
    terminalError:window.__fetchTerminalError,
  }));
  document.body.setAttribute('data-browser-result',errors.length?'FAIL: '+errors.join('; '):'PASS');
})();
</script>"""


def configured_fake():
    fake = FAKE
    fake = re.sub(
        r"data_mode:'LIVE',local_live_demo:true,environment_mode:'local',read_only:false,\s*"
        r"fetch_enabled:false,public_fetch_enabled:false,authentication_required:false,",
        "data_mode:'SYNTHETIC',local_live_demo:false,environment_mode:'local',read_only:false,"
        "source_selection_supported:true,fetch_enabled:true,public_fetch_enabled:true,"
        "authentication_required:false,",
        fake,
        count=1,
    )
    fake = fake.replace(
        "enabled_for_current_operation:false,last_saved_fetch",
        "enabled_for_current_operation:true,last_saved_fetch",
        1,
    )
    fake = re.sub(
        r"(source_id:'google_web_serp',[^}]*enabled_for_current_operation:)false",
        r"\1true",
        fake,
        count=1,
    )
    if "source_selection_supported:true" not in fake:
        raise AssertionError("capability fixture could not enable source selection")
    if fake.count("enabled_for_current_operation:true") != 2:
        raise AssertionError("fixture must enable only Google and Wikimedia")
    return fake.replace("</script>", FETCH_FIXTURE + "</script>", 1)


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
    phoenix_css = (PUBLIC / "phoenix.css").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="trendscout-google-fetch-browser-") as directory:
        root = Path(directory)
        public = root / "public"
        shutil.copytree(PUBLIC, public)
        fetch_module = public / "modules" / "fetch.js"
        fetch_source = fetch_module.read_text(encoding="utf-8")
        call = "this.onTerminal(status.run_id || null, status);"
        instrumented = "try { " + call + " } catch (error) { globalThis.__fetchTerminalError = String(error); throw error; }"
        if call not in fetch_source:
            raise AssertionError("Fetch controller terminal callback was not found")
        fetch_module.write_text(fetch_source.replace(call, instrumented, 1), encoding="utf-8")
        html = html.replace('<link rel="stylesheet" href="/dashboard/styles.css">', "<style>" + css + "</style>")
        html = html.replace('<link rel="stylesheet" href="/dashboard/phoenix.css">', "<style>" + phoenix_css + "</style>")
        html = html.replace('src="/dashboard/app.js"', 'src="' + (public / "app.js").as_uri() + '"')
        page = root / "index.html"
        page.write_text(html.replace("</body>", configured_fake() + CHECK + "</body>"),
                        encoding="utf-8")
        command = [
            chrome, "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-background-networking", "--disable-extensions", "--no-proxy-server",
            "--disable-sync", "--disable-component-update", "--allow-file-access-from-files",
            "--no-first-run", "--user-data-dir=" + directory + "/profile",
            "--window-size=1280,1000", "--virtual-time-budget=12000", "--dump-dom", page.as_uri(),
        ]
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30, env=env)
        if result.returncode or 'data-browser-result="PASS"' not in result.stdout:
            marker = (result.stdout.split("data-browser-result=", 1)[-1][:4000]
                      if "data-browser-result=" in result.stdout
                      else "missing browser result: " + result.stdout[-1500:])
            raise SystemExit("Chrome Google Fetch selection failed: " + marker)
    print("Chrome Google-only and ALL Google/Wikimedia Fetch selection, synthetic provenance: PASS")


if __name__ == "__main__":
    main()
