"""Capture reviewed D2 screenshots from the offline dashboard fixture."""

import base64
import tempfile
import time
from pathlib import Path

from browser_support import ChromeDevTools, find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
OUTPUT = ROOT / "Documents" / "Reports" / "windows-evidence" / "overnight-g2-g7"
RUN_ID = "a" * 32
KEYWORD_ID = "c" * 64

FAKE = r"""<script>
window.fetch=(path,options={})=>{
  window.fetchRequests=(window.fetchRequests||[]).concat(String(path));
  window.fetchCalls=(window.fetchCalls||[]).concat({path:String(path),method:options.method||'GET',headers:options.headers||{},body:options.body||null});
  const value=String(path).split('?')[0];
  if(value==='/api/v1/scheduler'&&options.method==='POST')return Promise.resolve(new Response(JSON.stringify({item:{id:'f'.repeat(32),name:'Daily synthetic check',frequency:'DAILY',timezone:'Asia/Ho_Chi_Minh',hour:9,minute:0,enabled:false}}),{status:201}));
  if(value==='/api/health')return Promise.resolve(new Response(JSON.stringify({status:'ok'}),{status:200}));
  if(value==='/api/v1/capabilities')return Promise.resolve(new Response(JSON.stringify({contract_version:'1.0',environment_mode:'local',read_only:false,local_live_demo:true,authentication_required:false,public_fetch_enabled:false,fetch_enabled:false,fetch_block_reason:'FEATURE_DISABLED',data_mode:'LIVE',sources:[
    {source_id:'wikimedia_pageviews_top_per_country',display_name:'Wikimedia Pageviews',icon:'W',category:'trends_search_interest',adapter_status:'IMPLEMENTED',approval_status:'PENDING_LIVE_APPROVAL',configuration_status:'NOT_REQUIRED',local_live_verification_status:'LOCAL_LIVE_VERIFIED',production_readiness:'NOT_PRODUCTION_READY',enabled_for_current_operation:false,last_saved_fetch:{status:'PARTIAL',data_mode:'SYNTHETIC',record_count:4,fetched_at:'2026-11-11T03:00:00Z',observation_start:'2026-11-10',observation_end:'2026-11-10'},metric:{name:'views_ceil',unit:'approx_pageviews_upper_bound',meaning:'Approximate pageviews in a censored source sample'},market_scope:'US country scoped top list',documentation_url:'https://doc.wikimedia.org/generated-data-platform/aqs/'},
    {source_id:'youtube_video_search',display_name:'YouTube',icon:'▶',category:'video_streaming',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false,metric:{name:'result_rank',unit:'ordinal result position',meaning:'Position in query-scoped video search results'},market_scope:'US region filter',documentation_url:'https://developers.google.com/youtube/v3/docs/search/list'},
    {source_id:'google_web_serp',display_name:'Google Search',icon:'G',category:'search_engines',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false,metric:{name:'result_rank',unit:'ordinal result position',meaning:'Position in sampled web search results'},market_scope:'US localization filter',documentation_url:'https://serpapi.com/search-api'},
    {source_id:'bing_web_serp',display_name:'Bing Search',icon:'B',category:'search_engines',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false,metric:{name:'result_rank',unit:'ordinal result position',meaning:'Position in sampled web search results'},market_scope:'US localization filter',documentation_url:'https://serpapi.com/search-api'}
  ]}),{status:200}));
  if(value==='/api/v1/analytics/overview')return Promise.resolve(new Response(JSON.stringify({keyword_totals:{unique:3,eligible:3,excluded:0,classified:2,ambiguous:0,unknown:1,ineligible:0,unclassified:1},active_sources:[],source_volume:[{source:'wikimedia_pageviews_top_per_country',keywords:3,first_seen:'2026-11-10',last_seen:'2026-11-10'}],algorithm_distribution:[{algorithm_id:'observed-volume-rank',version:'v3',status:'COMPLETED',count:3},{algorithm_id:'observed-volume-rank',version:'v3',status:'SKIPPED',count:1}],algorithm_effectiveness:{status:'NOT_YET_EVALUATED',reason:'No comparable labeled historical evaluation is stored.'},source_operations:[{fetched_at:'2026-11-11T03:00:00Z',status:'PARTIAL',source_manifest:[{source_id:'wikimedia_pageviews_top_per_country',status:'PARTIAL',accepted_records:3,record_count:4}]}],multi_source_coverage:{status:'INSUFFICIENT_SOURCES',sources:[{source:'wikimedia_pageviews_top_per_country',keywords:3,first_seen:'2026-11-10',last_seen:'2026-11-10'}]},observations_over_time:[{source:'wikimedia_pageviews_top_per_country',observation_date:'2026-11-10',observed:3,unavailable:0}],top_observed:[{rank:1,keyword:'Example topic',metric:'views_ceil',unit:'approx_pageviews_upper_bound',observed_value:8400}],top_observed_context:{source:'wikimedia_pageviews_top_per_country',project:'en.wikipedia',observation_date:'2026-11-10',cohort_size:3,scope:['wikimedia_pageviews_top_per_country','US','all-access','en.wikipedia','views_ceil','approx_pageviews_upper_bound'],quality_flags:['CENSORED_TOP_LIST','SYNTHETIC_FIXTURE']},last_successful_fetch:{fetched_at:'2026-11-11T03:00:00Z',status:'PARTIAL',age_seconds:3600}}),{status:200}));
  if(value==='/api/v1/sources/library')return Promise.resolve(new Response(JSON.stringify({audit_date:'2026-11-13',items:Array.from({length:81},(_,i)=>({source_id:'candidate_'+String(i+1).padStart(2,'0'),display_name:'Research candidate '+(i+1),category:'Research library'}))}),{status:200}));
  if(value==='/api/v1/runtime')return Promise.resolve(new Response(JSON.stringify({embedding:{package:'fastembed',package_version:'0.9.1',model:'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',model_version:'fastembed-0.9.1',inference_device:'CPU',cache_vectors:3,vector_dimensions:'384',classification_policy_version:'contextual-classifier-v1.2',taxonomy_version:'topics-v1.4'},llm:{status:'NOT_CONFIGURED',usage_instrumented:false},scheduler:{status:'DISABLED',implemented:true,enabled:false,worker_running:false,reason:'Disabled in backend configuration.'},system:{status:'AVAILABLE',environment_profile:'local',run_mode:'synthetic',read_only:true,configured_sources:1},storage:{status:'AVAILABLE',engine:'SQLITE',integrity:'OK',keyword_records:124,database_size_bytes:153600,read_only:true}}),{status:200}));
  if(value==='/api/v1/scheduler')return Promise.resolve(new Response(JSON.stringify({items:[],implemented:true,enabled:false,worker_running:false}),{status:200}));
  if(value==='/api/v1/datasets/latest')return Promise.resolve(new Response(JSON.stringify({item:{dataset_id:'fixture-dataset',status:'PARTIAL',data_mode:'LIVE',sources:['wikimedia_pageviews_top_per_country'],provider_record_count:4,received_record_count:3,retained_keyword_count:3,snapshot_retained_keyword_count:3,eligible_keyword_count:3,assigned_keyword_count:2,unassigned_keyword_count:1,excluded_keyword_count:0,observation_start:'2026-11-10',observation_end:'2026-11-10',fetched_at:'2026-11-11T03:00:00Z',quality_flags:[],ledger_coverage_status:'SNAPSHOT_ONLY'}}),{status:200}));
  if(value==='/api/v1/algorithms')return Promise.resolve(new Response(JSON.stringify({evaluation_status:'NOT_YET_EVALUATED',algorithms:[{algorithm_id:'observed-volume-rank',version:'v3',name:'Observed volume rank'}]}),{status:200}));
  if(value==='/api/v1/algorithms/results')return Promise.resolve(new Response(JSON.stringify({summary:[],items:[]}),{status:200}));
  if(value==='/api/v1/collections')return Promise.resolve(new Response(JSON.stringify({items:[
    {id:'fixture-parent',name:'Home & Living',parent_id:null,membership_count:2,direct_membership_count:0,subtree_unique_keyword_count:2,created_by:'AI',auto_match:true},
    {id:'fixture-child',name:'Smart Home',parent_id:'fixture-parent',membership_count:2,direct_membership_count:2,subtree_unique_keyword_count:2,created_by:'AI',auto_match:true},
    {id:'fixture-sibling',name:'Seasonal Ideas',parent_id:null,membership_count:0,direct_membership_count:0,subtree_unique_keyword_count:0,created_by:'USER',auto_match:false}
  ],suggestions:[],mode:'SUGGEST_ONLY',summary:{eligible_keywords:3,assigned_keywords:2,unclassified_keywords:1,excluded_keywords:0,classified_keywords:2,ambiguous_keywords:0,unknown_keywords:1,ineligible_topic_keywords:0,ai_assigned_keywords:2,manually_managed_keywords:0,ai_created_collections:2}}),{status:200}));
  if(value==='/api/v1/collections/inbox')return Promise.resolve(new Response(JSON.stringify({items:[],total:0}),{status:200}));
  if(value==='/api/v1/research')return Promise.resolve(new Response(JSON.stringify({items:[],total:0}),{status:200}));
  if(value==='/api/v1/runs/history')return Promise.resolve(new Response(JSON.stringify({items:[{fetch_id:'e'.repeat(32),run_at:'2026-11-11T03:00:00Z',run_type:'FETCH',sources:['wikimedia_pageviews_top_per_country'],status:'PARTIAL',received_record_count:4,retained_keyword_count:3,rejected_record_count:1,mode:'SYNTHETIC',error_code:'SOURCE_PARTIAL',stage:'DONE',quality_flags:['SYNTHETIC_FIXTURE']}],total:1,next_offset:null}),{status:200}));
  if(value==='/api/v1/datasets/latest/keywords'){
    const keywords=[
      {id:'c'.repeat(64),keyword:'Kitchen organizer',source:'wikimedia_pageviews_top_per_country',observed_at:'2026-11-10',eligible:1,classification_status:'CLASSIFIED',memberships:[{id:'fixture-child',name:'Smart Home',origin:'LOCAL_AI'}],evidence:{views_ceil:8400}},
      {id:'d'.repeat(64),keyword:'Home energy monitor',source:'wikimedia_pageviews_top_per_country',observed_at:'2026-11-10',eligible:1,classification_status:'CLASSIFIED',memberships:[{id:'fixture-child',name:'Smart Home',origin:'LOCAL_AI'}],evidence:{views_ceil:4200}},
      {id:'e'.repeat(64),keyword:'Compact apartment storage',source:'wikimedia_pageviews_top_per_country',observed_at:'2026-11-10',eligible:1,classification_status:'UNKNOWN',memberships:[],evidence:{views_ceil:2100}}
    ];
    const params=new URLSearchParams(String(path).split('?')[1]||'');
    const collectionIds=params.getAll('collection_ids');
    const query=(params.get('q')||'').toLocaleLowerCase();
    const matching=keywords.filter(item=>(!collectionIds.length||item.memberships.some(m=>collectionIds.includes(m.id)))&&item.keyword.toLocaleLowerCase().includes(query));
    return Promise.resolve(new Response(JSON.stringify({items:matching,total:matching.length,next_offset:null}),{status:200}));
  }
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
            if not evaluate("window.waitFor(()=>document.querySelectorAll('#pageviews-chart .chart-row').length===1)", await_promise=True):
                raise RuntimeError("persisted comparable Top 5 did not render in the overview chart")
            if not evaluate("window.waitFor(()=>document.querySelector('#overview-context').textContent.length>0&&document.querySelector('#metric-topics-found').textContent==='2'&&document.querySelector('#metric-unclassified').textContent==='1')", await_promise=True):
                raise RuntimeError("saved dataset counts overwrote the distinct classification totals")
            evaluate("document.querySelector('#dashboard-analysis-tab').click()")
            if not evaluate("window.waitFor(()=>document.querySelector('#analysis-algorithms').textContent.includes('Xếp hạng theo lượng ghi nhận'))", await_promise=True):
                raise RuntimeError("algorithm analysis table exposed a technical identifier instead of the localized registry name")
            if not evaluate("document.querySelectorAll('#analysis-history-chart .chart-row').length===1 && Boolean(document.querySelector('#analysis-history-chart').getAttribute('aria-label'))"):
                raise RuntimeError("daily observation coverage chart did not render with an accessible label")
            if not evaluate("document.querySelectorAll('#analysis-source-volume-chart .chart-row').length===1 && document.querySelector('#analysis-source-volume-chart').getAttribute('aria-label').length>0"):
                raise RuntimeError("source volume chart did not render from persisted fixture data")
            if not evaluate("document.querySelectorAll('#analysis-algorithm-chart .chart-row').length===1 && document.querySelector('#analysis-algorithm-chart .chart-row').getAttribute('aria-label').includes('Bỏ qua')"):
                raise RuntimeError("algorithm distribution chart did not show localized outcome counts")
            if not evaluate("document.querySelectorAll('#analysis-operations-chart .chart-row').length===1 && document.querySelector('#analysis-operations-chart .chart-row').getAttribute('aria-label').includes('Một phần')"):
                raise RuntimeError("source operations chart did not show persisted partial run status")
            if not evaluate("document.querySelectorAll('#analysis-coverage-chart .coverage-heatmap-row').length===2 && Boolean(document.querySelector('#analysis-coverage-chart').getAttribute('aria-label'))"):
                raise RuntimeError("source/date coverage heatmap did not render with an accessible label")
            evaluate("document.querySelector('[data-nav=settings]').click()")
            if not evaluate("window.waitFor(()=>document.querySelector('#settings-schedules .schedule-empty'))", await_promise=True):
                raise RuntimeError("settings did not show the honest empty scheduler state")
            if not evaluate("document.querySelector('#settings-scheduler-status').textContent.length>0"):
                raise RuntimeError("scheduler runtime status was not shown in settings")
            if not evaluate("window.waitFor(()=>document.querySelectorAll('#settings-system-details > div').length===10)", await_promise=True):
                raise RuntimeError("system and storage status details were not rendered")
            details = evaluate("document.querySelector('#settings-system-details').textContent")
            if not all(label in details for label in ("SQLite", "Máy cá nhân", "Đã kiểm tra, đạt")):
                raise RuntimeError("localized system and storage status values were not shown")
            evaluate("document.querySelector('#settings-schedule-form').elements.namedItem('name').value='Daily synthetic check';document.querySelector('#settings-schedule-form').elements.namedItem('operator-key').value='offline-scheduler-key-0123456789';document.querySelector('#settings-schedule-form').requestSubmit()")
            if not evaluate("window.waitFor(()=>window.fetchCalls.some(call=>call.path==='/api/v1/scheduler'&&call.method==='POST'))", await_promise=True):
                raise RuntimeError("scheduler create control did not call the protected same-origin API")
            if not evaluate("window.fetchCalls.find(call=>call.path==='/api/v1/scheduler'&&call.method==='POST')?.headers?.Authorization==='Bearer offline-scheduler-key-0123456789'&&document.querySelector('#settings-schedule-form').elements.namedItem('operator-key').value===''&&!localStorage.length&&!sessionStorage.length"):
                raise RuntimeError("scheduler operator key was not transient or was not sent to the protected endpoint")
            if not evaluate("document.querySelector('#settings-schedule-feedback').textContent.includes('Đã tạo lịch ở trạng thái tắt')"):
                raise RuntimeError("scheduler create did not explain that the new schedule remains disabled")
            evaluate("document.querySelector('#settings-schedule-feedback').textContent=''")
            evaluate("document.querySelector('#dashboard-overview-tab').click()")

            def set_viewport(width, height=900):
                call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": 1,
                        "mobile": False,
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
                    details = evaluate("JSON.stringify([...document.querySelectorAll('body *')].map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,45),right:Math.round(e.getBoundingClientRect().right),width:Math.round(e.getBoundingClientRect().width)})).filter(x=>x.right>innerWidth+2).slice(0,8))")
                    raise RuntimeError(str(width) + "px horizontal overflow: " + str(details))

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

            for width in (320, 390, 768, 1024, 1440, 1920):
                for locale, theme in (("vi", "light"), ("vi", "dark"), ("en", "light"), ("en", "dark")):
                    set_viewport(width, 900 if width > 500 else 820)
                    set_preferences(theme, locale)
                    go("overview")
                    capture(f"overview-{width}-{locale}-{theme}.webp", width, configure=False)

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

            set_preferences("light", "vi")
            for view in ("analysis", "collections", "sources", "runs", "algorithms", "quality", "research", "settings"):
                for width in (320, 390, 768, 1024, 1440, 1920):
                    set_viewport(width, 900 if width > 500 else 820)
                    go(view)
                    if evaluate("document.documentElement.scrollWidth>innerWidth+1"):
                        raise RuntimeError(f"{width}px document horizontal overflow on {view} (vi/light)")
                    if view == "runs" and width <= 390 and not evaluate("window.waitFor(()=>Boolean(document.querySelector('#run-history tbody td[data-label]')))", await_promise=True):
                        raise RuntimeError("Run History fixture did not load for mobile layout review: " + str(evaluate("JSON.stringify({requests:window.fetchRequests.filter(path=>path.includes('/api/v1/runs/history')),content:document.querySelector('#run-history')?.textContent})")))
                    time.sleep(0.18)
                    if view == "runs" and width <= 390 and not evaluate("Boolean(document.querySelector('#run-history tbody td[data-label]'))"):
                        raise RuntimeError("mobile Run History rows are missing their column labels")
                    if view == "runs" and width == 390 and not evaluate("document.querySelector('#run-history').textContent.includes('Dữ liệu một phần') && !document.querySelector('#run-history').textContent.includes('SOURCE_PARTIAL') && !document.querySelector('#run-history').textContent.includes('SYNTHETIC_FIXTURE')"):
                        raise RuntimeError("Run History exposed raw diagnostic codes instead of a localized partial-data explanation")
                    if view == "analysis" and width <= 390 and not evaluate("Boolean(document.querySelector('#analysis-source-volume tbody td[data-label]'))"):
                        raise RuntimeError("mobile Analysis rows are missing their column labels")
                    if view == "sources" and not evaluate("!document.querySelector('#source-library-disclosure')?.open && document.querySelector('#source-library-audit')?.textContent.includes('81')"):
                        raise RuntimeError("locked research inventory was not collapsed with its candidate count visible")
                    if view == "collections" and width <= 390:
                        if not evaluate("waitFor(()=>Boolean(document.querySelector('#workbench-filter-toggle')))", await_promise=True):
                            raise RuntimeError("mobile Collection filter toggle was not mounted")
                        evaluate("document.querySelector('#workbench-filter-toggle')?.click()")
                        time.sleep(0.24)
                        drawer_width = evaluate("document.querySelector('#workbench-sidebar')?.getBoundingClientRect().width || 0")
                        if drawer_width < 280:
                            raise RuntimeError(f"mobile Collection drawer is clipped to {drawer_width}px")
                        drawer_left = evaluate("document.querySelector('#workbench-sidebar')?.getBoundingClientRect().left ?? -1")
                        if drawer_left < 0:
                            geometry = evaluate("JSON.stringify({innerWidth,scrollX,open:document.querySelector('#workbench-sidebar')?.className,expanded:document.querySelector('#workbench-filter-toggle')?.getAttribute('aria-expanded'),left:document.querySelector('#workbench-sidebar')?.getBoundingClientRect().left,transform:getComputedStyle(document.querySelector('#workbench-sidebar')).transform})")
                            raise RuntimeError(f"mobile Collection drawer starts outside the viewport at {drawer_left}px: {geometry}")
                        count_ready = evaluate("waitFor(()=>document.querySelector('[data-collection-id=fixture-parent] .workbench-tree-count')?.textContent==='2'&&document.querySelectorAll('#workbench-rows tr').length===3&&document.querySelector('#workbench-summary').textContent.includes('3')&&document.querySelector('#workbench-page-info').textContent.includes('3'))", await_promise=True)
                        if not count_ready:
                            raise RuntimeError("Collection summary, child membership count, result rows and pagination total disagree")
                        if not evaluate("document.querySelector('#workbench-filter-toggle')?.click();!document.querySelector('#workbench-sidebar').classList.contains('is-open')"):
                            raise RuntimeError("mobile Collection drawer remained open before the screen capture")
                        time.sleep(0.25)
                    capture(f"screen-{view}-{width}-vi-light.webp", width, configure=False)

            for locale, theme in (("vi", "dark"), ("en", "light"), ("en", "dark")):
                set_preferences(theme, locale)
                for view in ("analysis", "collections", "sources", "runs", "algorithms", "quality", "research", "settings"):
                    for width in (320, 390, 768, 1024, 1440, 1920):
                        set_viewport(width, 900 if width > 500 else 820)
                        go(view)
                        if evaluate("document.documentElement.scrollWidth>innerWidth+1"):
                            raise RuntimeError(f"{width}px document horizontal overflow on {view} ({locale}/{theme})")
                        time.sleep(0.18)
                        capture(f"screen-{view}-{width}-{locale}-{theme}.webp", width, configure=False)

            set_viewport(1280)
            set_preferences("light", "vi")
            go("collections")
            if not evaluate("window.waitFor(()=>Boolean(document.querySelector('[data-collection-id=fixture-parent] .workbench-context-button')))", await_promise=True):
                raise RuntimeError("Collection management action did not render for the merge picker screenshot")
            evaluate("document.querySelector('[data-collection-id=fixture-parent] .workbench-context-button').click()")
            if not evaluate("window.waitFor(()=>Boolean(document.querySelector('#workbench-collection-dialog .collection-picker-trigger')))", await_promise=True):
                raise RuntimeError("merge destination picker did not render with an eligible sibling Collection")
            evaluate("document.querySelector('#workbench-collection-dialog .collection-picker-trigger').click()")
            if not evaluate("window.waitFor(()=>!document.querySelector('.collection-picker-list').hidden&&document.querySelectorAll('.collection-picker-option').length===1)", await_promise=True):
                raise RuntimeError("merge destination list did not open with the single eligible sibling")
            capture("collection-merge-picker-1280-vi-light.webp", 1280, configure=False)
            evaluate("document.querySelector('.collection-picker-option').click();document.querySelector('.collection-manage-section:not(.collection-manage-delete) .button-primary').click()")
            if not evaluate("window.waitFor(()=>Boolean(document.querySelector('#workbench-collection-dialog h2'))&&!document.querySelector('#workbench-collection-dialog .collection-manage-panel'))", await_promise=True):
                raise RuntimeError("selected destination did not reach the merge review step")
            capture("collection-merge-review-1280-vi-light.webp", 1280, configure=False)

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
            print("Visual evidence: Overview and all eight screens captured at six widths in VI/EN light/dark, plus merge picker/review dialogs.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
