"""Offline Chrome checks for the bilingual, read-only dashboard."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from browser_support import find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
RUN_A = "a" * 32

FAKE = r"""<script>
window.testRequests=[];window.pwned=false;
window.fetch=(path,opts={})=>{
  const request={path:String(path),method:opts.method||'GET',headers:opts.headers||{}};
  testRequests.push(request);
  const p=request.path;
  if(p==='/api/timeout-json')return Promise.resolve({ok:true,json:()=>opts.signal.aborted?Promise.reject(new Error('aborted')):new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('aborted'))))});
  if(p==='/api/network-fail')return Promise.reject(new TypeError('offline network failure'));
  if(p==='/api/auth-fail')return Promise.resolve(new Response('{}',{status:401}));
  if(p==='/api/status-403')return Promise.resolve(new Response('{}',{status:403}));
  if(p==='/api/status-409')return Promise.resolve(new Response('{}',{status:409}));
  if(p==='/api/status-429')return Promise.resolve(new Response('{}',{status:429}));
  if(p==='/api/status-500')return Promise.resolve(new Response('{}',{status:500}));
  if(p==='/api/status-503')return Promise.resolve(new Response('{}',{status:503}));
  if(p==='/api/invalid-json')return Promise.resolve(new Response('{',{status:200}));
  if(p==='/api/v1/capabilities')return Promise.resolve(new Response(JSON.stringify({
    data_mode:'LIVE',local_live_demo:true,environment_mode:'local',read_only:false,
    fetch_enabled:false,public_fetch_enabled:false,authentication_required:false,
    sources:[
      {source_id:'wikimedia_pageviews_top_per_country',adapter_status:'IMPLEMENTED',approval_status:'PENDING_LIVE_APPROVAL',configuration_status:'NOT_REQUIRED',local_live_verification_status:'LOCAL_LIVE_VERIFIED',production_readiness:'NOT_PRODUCTION_READY',enabled_for_current_operation:false},
      {source_id:'youtube_video_search',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false},
      {source_id:'google_web_serp',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false},
      {source_id:'bing_web_serp',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false}
    ]}),{status:200}));
  if(p==='/api/health')return Promise.resolve(new Response(JSON.stringify({status:'ok'}),{status:200}));
  if(p==='/api/runs/'+'d'.repeat(32))return Promise.resolve(new Response('{}',{status:404}));
  if(p.includes('/keywords/'))return Promise.resolve(new Response(JSON.stringify({
    keyword_id:'c'.repeat(64),display_keyword:'<img src=x onerror=window.pwned=true>',
    canonical_keyword:'safe-title',source:'Wikimedia',project:'en.wikipedia',
    observation_date:'2026-01-08',score:null,status:'INSUFFICIENT_DATA',
    current_views_ceil:0,baseline:null,ratio:null,observed_baseline_days:2,
    missing_baseline_dates:[{date:'2026-01-07',reason:'DAY_UNAVAILABLE'}],
    evidence_url:'javascript:alert(1)',source_url:'javascript:alert(1)',
    score_components:null,baseline_observations:[{date:'2026-01-06',views_ceil:0}],
    classification_evidence:[{category:'fashion',matched_text:'<script>',rule_id:'fixture'}],
    quality_flags:['<svg onload=window.pwned=true>']
  }),{status:200}));
  if(p.includes('/keywords?')){
    const q=new URL(p,'https://local.invalid').searchParams;
    const empty=q.get('q')==='empty';
    const title=q.get('q')==='slow'?'OLD RESULT':q.get('q')==='fast'?'NEW RESULT':'<img src=x onerror=window.pwned=true>';
    return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify({
      items:empty?[]:[{keyword_id:'c'.repeat(64),display_keyword:title,project:'en.wikipedia',
        score:null,status:'INSUFFICIENT_DATA',current_views_ceil:0,baseline:null,
        ratio:null,observation_date:'2026-01-08',quality_flags:['<script>']}],
      total:empty?0:25,next_offset:q.get('offset')==='0'?20:null,
      empty_reason:empty?'NO_MATCH':null
    }),{status:200})),q.get('q')==='slow'?80:1));
  }
  if(p.startsWith('/api/runs/')){
    const id=p.split('/')[3];
    const payload={run_id:id,status:id==='b'.repeat(32)?'FETCHING':'PARTIAL',keyword_count:1234,scored_count:3,
      data_date:'2026-01-08',mode:'synthetic',fetched_at:'2026-01-09T00:00:00Z',
      coverage:[{date:'2026-01-07',status:'DAY_UNAVAILABLE',http_status:404}],
      algorithm_version:'wikimedia-trend-v0.1'};
    return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify(payload),{status:200})),id==='a'.repeat(32)?70:1));
  }
  return Promise.resolve(new Response('{}',{status:404}));
};
</script>"""

CHECK = r"""<script>
(async()=>{
  const errors=[];const check=(value,message)=>{if(!value)errors.push(message)};
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  try{
    await wait(300);
    if(window.name==='d2-reload'){
      check(document.documentElement.dataset.theme==='dark','theme persisted after reload');
      check(document.documentElement.lang==='en','locale persisted after reload');
      check(document.querySelector('[data-nav=overview]').textContent.includes('Overview'),'English navigation after reload');
      check(!document.querySelector('#operator-token'),'no operator token control');
      check(localStorage.getItem('trendscout.theme')==='dark','dark preference stored');
      check(localStorage.getItem('trendscout.locale')==='en','English preference stored');
      check(!Object.keys(localStorage).some(key=>/token|secret|credential/i.test(key)),'no credential in local storage');
      document.body.setAttribute('data-browser-result',errors.length?'FAIL: '+errors.join('; '):'PASS');
      window.name='';
      return;
    }
    check(document.documentElement.dataset.theme==='light','light default');
    check(document.documentElement.lang==='vi','Vietnamese default');
    check(document.querySelectorAll('[data-nav]').length===8,'eight supported destination views remain available');
    check(document.querySelectorAll('#workspace-nav > .nav-link[data-nav]').length===5,'five primary navigation items');
    check(document.querySelector('.nav-advanced summary').textContent.includes('Nâng cao'),'technical navigation grouped under Advanced');
    check(document.querySelector('#open-fetch').disabled,'fetch disabled');
    check(!document.querySelector('#operator-token'),'no token input');
    check(!document.querySelector('#fetch-form'),'no fetch submit form');
    check(document.querySelectorAll('#source-registry-list .source-card').length===4,'four runtime source descriptors rendered from metadata');
    check(document.querySelectorAll('[data-dashboard-tab]').length===2,'two semantic dashboard tabs');
    check(document.querySelector('#metric-active-sources').textContent==='0','active source count comes from runtime capabilities');
    const wikimediaCard=[...document.querySelectorAll('#source-registry-list .source-card')].find(card=>card.querySelector('h2')?.textContent.includes('wikimedia_pageviews'));
    check(Boolean(wikimediaCard),'runtime metadata includes Wikimedia');
    check(wikimediaCard?.textContent.includes('\u0110\u00e3 x\u00e1c minh trong l\u1ea7n ch\u1ea1y LIVE local tr\u01b0\u1edbc \u0111\u00e2y'),'historical verification status is localized');
    check(wikimediaCard?.textContent.includes('Ch\u01b0a s\u1eb5n s\u00e0ng production'),'production readiness stays separate');
    check([...document.querySelectorAll('#source-registry-list .source-card')].filter(card=>!card.querySelector('h2')?.textContent.includes('wikimedia_pageviews')).every(card=>card.textContent.includes('Đã xác minh adapter ngoại tuyến')),'offline adapter states are reported from runtime metadata');
    const go=async name=>{
      if(innerWidth<=900 && !document.querySelector('#app-shell').classList.contains('menu-open'))document.querySelector('#menu-toggle').click();
      const link=document.querySelector('[data-nav="'+name+'"]');
      const group=link.closest('details');if(group)group.open=true;
      link.click();
      if(location.hash!=='#'+name)location.hash='#'+name;
      window.dispatchEvent(new Event('hashchange'));
      await wait(40);
    };
    for(const link of document.querySelectorAll('[data-nav]')){
      await go(link.dataset.nav);
      check(!document.querySelector('[data-view=\"'+link.dataset.nav+'\"]')?.hidden,'view '+link.dataset.nav);
      check(link.getAttribute('aria-current')==='page','aria current '+link.dataset.nav);
      check(document.documentElement.scrollWidth<=innerWidth+1,'horizontal overflow '+link.dataset.nav);
    }
    await go('runs');
    history.replaceState(null,'',location.pathname+'?q=renewables#keywords');window.dispatchEvent(new Event('hashchange'));
    check(location.hash==='#collections','legacy keyword deep link redirects to Collections');
    check(new URLSearchParams(location.search).get('q')==='renewables','legacy keyword deep link preserves the search query');
    check(!document.querySelector('[data-view=collections]').hidden,'legacy keyword deep link opens the keyword workspace');
    check(document.querySelector('[data-view=overview]').hidden,'legacy keyword deep link does not expose a second explorer');
    check(location.search==='?q=renewables','legacy route preserves query string');
    history.replaceState(null,'',location.pathname+'#runs');window.dispatchEvent(new Event('hashchange'));
    document.querySelector('#run-id').value=RUN_A;document.querySelector('#load-run').click();await wait(180);
    check(document.querySelector('#runs-current').textContent===RUN_A,'run lookup');
    check(document.querySelector('#run-note').textContent.includes('Lần phân tích'),'Vietnamese dynamic run label');
    check(document.querySelector('#run-status').textContent.includes('một phần'),'partial run status');
    check(document.querySelector('#keyword-count').textContent==='1.234','Vietnamese number format');
    check(document.querySelector('#quality-coverage').textContent.includes('404'),'partial source coverage');
    check(document.querySelector('#quality-staleness').textContent.includes('3 ngày'),'stale observation');
    check(document.querySelector('#global-notice').textContent.includes('SYNTHETIC'),'synthetic mode warning');

    await go('overview');await wait(20);
    check(document.querySelector('#rows').textContent.includes('<img'),'untrusted keyword rendered as text');
    check(!window.pwned&&!document.querySelector('#rows img'),'keyword XSS blocked');
    check(document.querySelector('#rows').textContent.includes('—'),'null score remains missing');
    const detailButton=document.querySelector('#rows button');
    detailButton.focus();detailButton.click();await wait(20);
    check(document.querySelector('#detail-dialog').open,'detail dialog opens');
    check(document.querySelector('#detail-content').textContent.includes('DAY_UNAVAILABLE'),'missing date evidence');
    check(!document.querySelector('#detail-content a'),'unsafe evidence link rejected');
    check(document.activeElement.closest('dialog')!==null,'dialog focus');
    check(document.querySelector('#detail-content').textContent.includes('Điểm / trạng thái'),'Vietnamese detail labels');
    const {applyLocale}=await import(I18N_MODULE_URI);
    applyLocale('en');
    check(document.querySelector('#detail-content').textContent.includes('Score / status'),'open detail relocalizes to English');
    applyLocale('vi');
    document.querySelector('#detail-dialog [data-close]').click();
    check(!document.querySelector('#detail-dialog').open,'dialog closes');
    check(document.activeElement===detailButton,'dialog restores focus');

    document.querySelector('#next').click();await wait(20);
    check(testRequests.some(request=>request.path.includes('offset=20')),'pagination request');
    const search=document.querySelector('#search');search.value='empty';
    search.dispatchEvent(new Event('input',{bubbles:true}));await wait(430);
    check(!document.querySelector('#empty').hidden,'empty search state');

    await go('research');
    check(Boolean(document.querySelector('#research-form')&&document.querySelector('#research-records')),'persistent research workflow form and saved-record list');
    check(document.querySelector('[data-view=research]').textContent.includes('nhu cầu thị trường'),'research page keeps commercial evidence boundaries visible');
    check(!document.querySelector('[data-nav=refresh]'),'scheduler navigation is absent');
    check(!document.querySelector('[data-nav=usage]'),'unmeasured AI usage navigation is absent');
    await go('overview');
    const requestsBeforeTab=testRequests.length;
    document.querySelector('[data-dashboard-tab=analysis]').click();
    check(document.querySelector('[data-dashboard-tab=analysis]').getAttribute('aria-selected')==='true','Analysis semantic tab selected');
    check(new URLSearchParams(location.search).get('q')==='empty','tab route preserves the active query parameter');
    check(testRequests.length===requestsBeforeTab,'dashboard tab does not request data');
    document.querySelector('[data-dashboard-tab=overview]').focus();
    document.querySelector('[data-dashboard-tab=overview]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
    check(document.querySelector('[data-dashboard-tab=analysis]').getAttribute('aria-selected')==='true','ArrowRight changes tabs');
    await go('settings');
    document.querySelector('[data-theme-toggle]').click();
    document.querySelector('[data-locale-toggle]').click();
    await wait(300);
    check(document.documentElement.dataset.theme==='dark','dark theme toggle');
    check(document.documentElement.lang==='en','English toggle');
    check(document.querySelector('#keyword-count').textContent==='1,234','English number format');
    check(document.querySelector('#run-note').textContent.includes('Run '),'English dynamic run label');
    check(document.querySelector('[data-nav=overview]').textContent.includes('Overview'),'English navigation translation');
    check(document.querySelector('#rows').closest('table').querySelector('th[data-i18n="table.topic"]').textContent.includes('TOPIC'),'English Discover table translation');

    document.querySelector('[data-locale-toggle]').click();
    check(document.documentElement.lang==='vi','switch back to Vietnamese');
    document.querySelector('[data-theme-toggle]').click();
    check(document.documentElement.dataset.theme==='light','switch back to light');
    document.querySelector('#run-id').value='d'.repeat(32);
    document.querySelector('#load-run').click();await wait(40);
    check(document.querySelector('#global-notice').textContent.includes('Không tìm thấy'),'read auth/not-found error visible');

    const apiUri=API_MODULE_URI;
    const {getJson}=await import(apiUri);
    try{await getJson('/api/auth-fail');check(false,'401 must fail closed');}
    catch(error){check(String(error).includes('không có quyền'),'localized 401');}
    try{await getJson('/api/status-403');check(false,'403 must fail closed');}
    catch(error){check(String(error).includes('bị từ chối'),'localized 403');}
    try{await getJson('/api/status-409');check(false,'409 must fail');}
    catch(error){check(String(error).includes('đang chạy'),'409 response');}
    try{await getJson('/api/status-429');check(false,'429 must fail');}
    catch(error){check(String(error).includes('giới hạn'),'429 response');}
    try{await getJson('/api/status-503');check(false,'5xx must fail');}
    catch(error){check(String(error).includes('Backend hoặc storage'),'localized 503 response');}
    try{await getJson('/api/status-500');check(false,'unmapped 5xx must fail');}
    catch(error){check(String(error).includes('HTTP 500'),'generic 5xx response');}
    try{await getJson('/api/network-fail');check(false,'network failure must fail');}
    catch(error){check(String(error).includes('Không kết nối'),'localized network failure');}
    try{await getJson('/api/invalid-json');check(false,'invalid JSON rejected');}
    catch(error){check(String(error).includes('không hợp lệ'),'invalid JSON message');}
    try{await getJson('/api/timeout-json',{timeoutMs:20});check(false,'request timeout');}
    catch(error){check(String(error).includes('quá thời gian'),'body timeout message');}
    const nativeTimeout=window.setTimeout;
    window.setTimeout=(callback,delay,...args)=>nativeTimeout(callback,delay===2000?0:delay,...args);
    document.querySelector('#run-id').value='b'.repeat(32);document.querySelector('#load-run').click();await wait(120);
    check(testRequests.filter(request=>request.path==='/api/runs/'+'b'.repeat(32)).length===31,'bounded run polling');
    check(document.querySelector('#global-notice').textContent.includes('30 lần kiểm tra'),'polling stop status');
    window.setTimeout=nativeTimeout;
    check(testRequests.every(request=>request.method==='GET'),'all dashboard requests are GET');
    check(!testRequests.some(request=>/token|secret|credential/i.test(JSON.stringify(request.headers))),'no credential header');
    check(!testRequests.some(request=>request.path==='/api/config'),'no auth-capability inference from config');
    check(localStorage.getItem('trendscout.theme')==='light','light preference persisted');
    check(localStorage.getItem('trendscout.locale')==='vi','Vietnamese preference persisted');
    document.querySelector('[data-theme-toggle]').click();
    document.querySelector('[data-locale-toggle]').click();
    check(localStorage.getItem('trendscout.theme')==='dark','theme saved');
    check(localStorage.getItem('trendscout.locale')==='en','locale saved');
    check(!Object.keys(localStorage).some(key=>/token|secret|credential/i.test(key)),'no credential storage');
    check(document.documentElement.scrollWidth<=innerWidth+1,'document horizontal overflow');
    if(INVALID_PREFERENCE_TEST){
      document.body.setAttribute('data-browser-result',errors.length?'FAIL: '+errors.join('; '):'PASS');
      return;
    }
    window.name='d2-reload';location.reload();return;
  }catch(error){errors.push('exception: '+String(error));}
  document.body.setAttribute('data-browser-result',errors.length?'FAIL: '+errors.join('; '):'PASS');
})();
</script>"""


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
    invalid_preferences = """<script>try{localStorage.setItem('trendscout.theme','sepia');localStorage.setItem('trendscout.locale','fr')}catch{}</script>"""
    html = html.replace('href="/dashboard/styles.css"', "<style>" + css + "</style>")
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    check = CHECK.replace("RUN_A", json.dumps("a" * 32))
    check = check.replace("API_MODULE_URI", json.dumps((PUBLIC / "modules" / "api.js").as_uri()))
    check = check.replace("I18N_MODULE_URI", json.dumps((PUBLIC / "modules" / "i18n.js").as_uri()))
    with tempfile.TemporaryDirectory(prefix="trendscout-browser-") as directory:
        page = Path(directory) / "index.html"
        for width in (320, 390, 768, 1024, 1440, 1920):
            page_html = html
            if width == 768:
                page_html = page_html.replace(
                    "<script>\n    (() => {", invalid_preferences + "<script>\n    (() => {", 1
                )
            width_check = check.replace(
                "INVALID_PREFERENCE_TEST", "true" if width == 768 else "false"
            )
            page.write_text(page_html.replace("</body>", FAKE + width_check + "</body>"),
                            encoding="utf-8")
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
                "--user-data-dir=" + directory + "/profile-" + str(width),
                "--window-size=" + str(width) + ",1000",
                "--virtual-time-budget=12000",
                "--dump-dom",
                page.as_uri(),
            ]
            env = {
                key: value
                for key, value in os.environ.items()
                if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
            }
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=40, env=env)
            if result.returncode or 'data-browser-result="PASS"' not in result.stdout:
                marker = (
                    result.stdout.split("data-browser-result=", 1)[-1][:800]
                    if "data-browser-result=" in result.stdout
                    else "missing browser result"
                )
                raise SystemExit(
                    "Chrome "
                    + str(width)
                    + " failed: "
                    + marker
                    + " (exit "
                    + str(result.returncode)
                    + ")"
                )
            print(
                "Chrome "
                + str(width)
                + "px, locale/theme persistence, all views, read-only API and XSS: PASS"
            )


if __name__ == "__main__":
    main()
