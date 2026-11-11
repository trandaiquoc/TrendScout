"""Chrome DOM tests: run only under externally verified Internet-blocking seccomp."""
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
FAKE=r'''<script>
const requests=[]; const runA='a'.repeat(32),runB='b'.repeat(32),keyword='c'.repeat(64);window.pwned=false;
window.fetch=(path,opts={})=>{requests.push([String(path),opts.method||'GET',opts]);const p=String(path);let payload;
if(p==='/timeout-json')return Promise.resolve({ok:true,json:()=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('aborted'))))});
if(p==='/api-client-unauthorized')return Promise.resolve(new Response('{}',{status:401}));
if(p==='/api-client-invalid-json')return Promise.resolve(new Response('{',{status:200}));
if(p==='/api/health')payload={status:'ok'};else if(p==='/api/config')payload={read_only:false};
else if(p.includes('/keywords/'))payload={display_keyword:'<img src=x onerror=window.pwned=true>',source:'Wikimedia',project:'en.wikipedia',observation_date:'2026-01-08',score:null,status:'INSUFFICIENT_DATA',missing_baseline_dates:[{date:'2026-01-07',reason:'DAY_UNAVAILABLE'}],evidence_url:'javascript:alert(1)'};
else if(p.includes('/keywords?')){const q=new URL(p,'https://local.invalid').searchParams;payload={items:q.get('q')==='empty'?[]:[{keyword_id:keyword,display_keyword:q.get('q')==='slow'?'OLD RESULT':q.get('q')==='fast'?'NEW RESULT':'<img src=x onerror=window.pwned=true>',project:'en.wikipedia',score:null,status:'INSUFFICIENT_DATA',quality_flags:['<script>']}],total:q.get('q')==='empty'?0:25,next_offset:q.get('offset')==='0'?20:null,empty_reason:q.get('q')==='empty'?'NO_MATCH':null};}
else if(p===`/api/runs/${'d'.repeat(32)}`)return Promise.resolve(new Response('{}',{status:404}));
else if(p.startsWith('/api/runs/')){const id=p.split('/')[3];payload={run_id:id,status:'PARTIAL',keyword_count:25,scored_count:3,data_date:'2026-01-08',mode:'synthetic',fetched_at:'2026-01-09T00:00:00Z',coverage:[{date:'2026-01-07',status:'DAY_UNAVAILABLE',http_status:404}],algorithm_version:'wikimedia-trend-v0.1'};}
else return Promise.resolve(new Response('{}',{status:404}));const delay=p===`/api/runs/${runA}`||p.includes('q=slow')?80:1;
return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify(payload),{status:200})),delay));};
</script>'''
CHECK=r'''<script>
(async()=>{const errors=[];const check=(v,m)=>{if(!v)errors.push(m)};const wait=ms=>new Promise(r=>setTimeout(r,ms));
try{await wait(20);check(document.querySelectorAll('[data-nav]').length===10,'nav count');
for(const link of document.querySelectorAll('[data-nav]')){location.hash=link.hash;window.dispatchEvent(new Event('hashchange'));check(!document.querySelector(`[data-view="${link.dataset.nav}"]`).hidden,`nav ${link.dataset.nav}`);check(link.getAttribute('aria-current')==='page','aria current');link.focus();check(document.activeElement===link,'keyboard focus');check(document.documentElement.scrollWidth<=innerWidth+1,'overflow '+link.dataset.nav);}
document.querySelector('#compare-second').value='ewma';document.querySelector('#compare-second').dispatchEvent(new Event('change'));check(document.querySelector('#compare-description').textContent.includes('FAILED'),'algorithm failed');check(document.querySelector('#compare-description').textContent.includes('NOT_COMPARABLE'),'algorithm comparison');check(document.querySelector('[data-view="research"]').textContent.includes('OFFLINE DEMO / SYNTHETIC DATA'),'synthetic');check(document.querySelector('[data-view="research"]').textContent.includes('Chưa đủ bằng chứng thương mại'),'null opportunity');
check([...document.querySelectorAll('[data-view="refresh"] button')].every(x=>x.disabled),'refresh disabled');check(document.querySelector('[data-view="settings"] button[disabled]')!==null,'connection disabled');check(!document.querySelector('input[name="api_key"]'),'no key input');
document.querySelector('#run-id').value=runA;document.querySelector('#load-run').click();document.querySelector('#run-id').value=runB;document.querySelector('#load-run').click();await wait(160);
check(READ_ONLY?(document.querySelector('#open-fetch').hidden||document.querySelector('#open-fetch').disabled):!document.querySelector('#open-fetch').disabled,'read-only config');check(document.querySelector('#runs-current').textContent===runB,'run race');check(document.querySelector('#quality-coverage').textContent.includes('404'),'partial');check(document.querySelector('#notice').textContent.includes('Dữ liệu cũ'),'stale data');check(document.querySelector('[data-view="sources"]').textContent.includes('NOT_CONFIGURED'),'source disabled');check(document.querySelector('[data-view="usage"]').textContent.includes('COST_UNKNOWN'),'unknown cost');
location.hash='#keywords';window.dispatchEvent(new Event('hashchange'));await wait(5);check(document.querySelector('#rows').textContent.includes('<img'),'text render');check(!window.pwned&&!document.querySelector('#rows img'),'XSS');check(document.querySelector('#rows').textContent.includes('—'),'null score');
document.querySelector('#rows button').click();await wait(20);check(document.querySelector('#detail-dialog').open,'detail open');check(document.querySelector('#detail-content').textContent.includes('DAY_UNAVAILABLE'),'missing date');check(!document.querySelector('#detail-content a'),'unsafe URL');for(const url of ['javascript:alert(1)','data:text/html,hi','https://wikipedia.org.evil.invalid/x','/relative','https://user@en.wikipedia.org/x','https%3A%2F%2Fen.wikipedia.org'])check(safeEvidenceUrl(url)===null,'URL '+url);check(safeEvidenceUrl('https://en.wikipedia.org/wiki/Test')!==null,'valid URL');check(document.activeElement.closest('dialog')!==null,'dialog focus');document.querySelector('#detail-dialog [data-close]').click();check(!document.querySelector('#detail-dialog').open,'detail close');
state.q='slow';const oldRequest=loadKeywords();state.q='fast';const newRequest=loadKeywords();await Promise.all([oldRequest,newRequest]);check(document.querySelector('#rows').textContent.includes('NEW RESULT'),'search response race');state.q='';await loadKeywords();
document.querySelector('#next').click();await wait(20);check(requests.some(x=>x[0].includes('offset=20')),'pagination');document.querySelector('[data-category="fashion"]').click();await wait(20);check(requests.some(x=>x[0].includes('category=fashion')),'filter');
const search=document.querySelector('#search');search.value='empty';search.dispatchEvent(new Event('input',{bubbles:true}));await wait(500);check(!document.querySelector('#empty').hidden,'empty');
location.hash='#settings';window.dispatchEvent(new Event('hashchange'));document.querySelector('#draft-translation').click();document.querySelector('#reset-draft').click();check(!document.querySelector('#draft-translation').checked,'draft reset');
document.querySelector('#run-id').value='d'.repeat(32);document.querySelector('#load-run').click();await wait(20);check(document.querySelector('#notice').textContent.includes('Không tìm thấy'),'run error');
if(READ_ONLY){document.querySelector('#operator-token').value='synthetic-not-a-real-credential';document.querySelector('#fetch-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await wait(10);}
try {await api('/timeout-json');check(false,'body timeout');} catch(e){check(String(e).includes('thời gian chờ'),'body timeout');}
await api('/api/health',{headers:{'X-Test-Request':'kept'}});const request=requests.find(x=>x[0]==='/api/health'&&x[2].headers['X-Test-Request']);check(Boolean(request),'API client request');check(request&&request[2].headers.Accept==='application/json','API client Accept');
try {await api('/api-client-unauthorized');check(false,'HTTP auth error');} catch(e){check(String(e).includes('Token không hợp lệ'),'HTTP auth error');}
try {await api('/api-client-invalid-json');check(false,'invalid JSON error');} catch(e){check(String(e).includes('dữ liệu không hợp lệ'),'invalid JSON error');}
check(!requests.some(x=>x[1]==='POST'),'no automatic or read-only write');check(localStorage.length===0&&sessionStorage.length===0,'no browser credential storage');check(document.documentElement.scrollWidth<=innerWidth+1,`overflow ${document.documentElement.scrollWidth}/${innerWidth}`);check(document.querySelectorAll('table th').length>0,'table headers');check(!document.body.innerText.includes('RAW-SECRET-FAKE'),'no secret output');check(document.querySelector('#notice').getAttribute('aria-live')==='polite','aria live');
}catch(e){errors.push('exception:'+String(e))}document.body.setAttribute('data-viewport-width',String(innerWidth));document.body.setAttribute('data-browser-result',errors.length?'FAIL: '+errors.join('; '):'PASS');})();
</script>'''
def main():
    for family in (socket.AF_INET,socket.AF_INET6):
        try: s=socket.socket(family);s.close()
        except PermissionError: continue
        raise SystemExit('Internet blocker is not active')
    chrome=shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: raise SystemExit('Chrome unavailable')
    public=ROOT/'Frontend/public';html=(public/'index.html').read_text();js=(public/'app.js').read_text();css=(public/'styles.css').read_text()
    html=html.replace('<link rel="stylesheet" href="/dashboard/styles.css"><script type="module" src="/dashboard/app.js"></script>',f'<style>{css}</style>',1)
    with tempfile.TemporaryDirectory(prefix='trendscout-browser-') as d:
        page=Path(d)/'index.html'
        for width,readonly in ((400,False),(768,False),(1280,False),(1280,True),(1280,'config-error')):
            fake=FAKE.replace('read_only:false','read_only:true' if readonly is True else 'read_only:false')
            if readonly=='config-error': fake=fake.replace("else if(p==='/api/config')payload={read_only:false};","else if(p==='/api/config')return Promise.resolve(new Response('{}',{status:503}));")
            page.write_text(html.replace('</body>',fake+f'<script>{js}</script>'+CHECK.replace('READ_ONLY','true' if readonly else 'false')+'</body>'))
            cmd=[chrome,'--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--disable-extensions','--no-proxy-server','--disable-sync','--disable-component-update','--no-first-run',f'--user-data-dir={d}/profile-{width}-{readonly}',f'--window-size={width},900', '--virtual-time-budget=22000','--dump-dom',page.as_uri()]
            env={k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
            proc=subprocess.run(cmd,capture_output=True,text=True,timeout=30,env=env)
            if proc.returncode or 'data-browser-result="PASS"' not in proc.stdout:
                marker=proc.stdout.split('data-browser-result=',1)[-1][:500] if 'data-browser-result=' in proc.stdout else 'missing result'
                raise SystemExit(f'Chrome {width} failed: {marker} (exit {proc.returncode})')
            import re
            measured=re.search(r'data-viewport-width="(\d+)"',proc.stdout)
            print(f'Chrome requested={width}px actual={measured.group(1) if measured else "unknown"}px read_only={readonly}: PASS')
if __name__=='__main__': main()
