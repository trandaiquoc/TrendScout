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
  const value=String(path).split('?')[0];
  if(value==='/api/health')return Promise.resolve(new Response(JSON.stringify({status:'ok'}),{status:200}));
  if(value==='/api/v1/capabilities')return Promise.resolve(new Response(JSON.stringify({contract_version:'1.0',environment_mode:'test',read_only:true,authentication_required:false,public_fetch_enabled:false,fetch_enabled:false,fetch_block_reason:'FEATURE_DISABLED',data_mode:'SYNTHETIC',sources:[
    {source_id:'wikimedia_pageviews_top_per_country',display_name:'Wikimedia Pageviews',icon:'W',category:'trends_search_interest',adapter_status:'IMPLEMENTED',approval_status:'PENDING_LIVE_APPROVAL',configuration_status:'NOT_REQUIRED',local_live_verification_status:'LOCAL_LIVE_VERIFIED',production_readiness:'NOT_PRODUCTION_READY',enabled_for_current_operation:false,metric:{name:'views_ceil',unit:'approx_pageviews_upper_bound',meaning:'Approximate pageviews in a censored source sample'},market_scope:'US country scoped top list',documentation_url:'https://doc.wikimedia.org/generated-data-platform/aqs/'},
    {source_id:'youtube_video_search',display_name:'YouTube',icon:'▶',category:'video_streaming',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false,metric:{name:'result_rank',unit:'ordinal result position',meaning:'Position in query-scoped video search results'},market_scope:'US region filter',documentation_url:'https://developers.google.com/youtube/v3/docs/search/list'},
    {source_id:'google_web_serp',display_name:'Google Search',icon:'G',category:'search_engines',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false,metric:{name:'result_rank',unit:'ordinal result position',meaning:'Position in sampled web search results'},market_scope:'US localization filter',documentation_url:'https://serpapi.com/search-api'},
    {source_id:'bing_web_serp',display_name:'Bing Search',icon:'B',category:'search_engines',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false,metric:{name:'result_rank',unit:'ordinal result position',meaning:'Position in sampled web search results'},market_scope:'US localization filter',documentation_url:'https://serpapi.com/search-api'}
  ]}),{status:200}));
  if(value==='/api/v1/analytics/overview')return Promise.resolve(new Response(JSON.stringify({keyword_totals:{unique:3,eligible:3,excluded:0,classified:2,ambiguous:0,unknown:1,ineligible:0,unclassified:1},active_sources:[],source_volume:[{source:'wikimedia_pageviews_top_per_country',keywords:3,first_seen:'2026-11-10',last_seen:'2026-11-10'}],algorithm_distribution:[{algorithm_id:'observed-volume-rank',version:'v3',status:'COMPLETED',count:3}],algorithm_effectiveness:{status:'NOT_YET_EVALUATED',reason:'No comparable labeled historical evaluation is stored.'},source_operations:[],multi_source_coverage:{status:'INSUFFICIENT_SOURCES',sources:[{source:'wikimedia_pageviews_top_per_country',keywords:3,first_seen:'2026-11-10',last_seen:'2026-11-10'}]},observations_over_time:[{source:'wikimedia_pageviews_top_per_country',observation_date:'2026-11-10',observed:3,unavailable:0}],top_observed:[{rank:1,keyword:'Example topic',metric:'views_ceil',unit:'approx_pageviews_upper_bound',observed_value:8400}],top_observed_context:{source:'wikimedia_pageviews_top_per_country',project:'en.wikipedia',observation_date:'2026-11-10',cohort_size:3,scope:['wikimedia_pageviews_top_per_country','US','all-access','en.wikipedia','views_ceil','approx_pageviews_upper_bound'],quality_flags:['CENSORED_TOP_LIST','SYNTHETIC_FIXTURE']},last_successful_fetch:{fetched_at:'2026-11-11T03:00:00Z',status:'PARTIAL',age_seconds:3600}}),{status:200}));
  if(value==='/api/v1/sources/library')return Promise.resolve(new Response(JSON.stringify({audit_date:'2026-11-13',items:Array.from({length:81},(_,i)=>({source_id:'candidate_'+String(i+1).padStart(2,'0'),display_name:'Research candidate '+(i+1),category:'Research library'}))}),{status:200}));
  if(value==='/api/v1/runtime')return Promise.resolve(new Response(JSON.stringify({embedding:{package:'fastembed',package_version:'0.9.1',model:'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',model_version:'fastembed-0.9.1',inference_device:'CPU',cache_vectors:3,vector_dimensions:'384',classification_policy_version:'contextual-classifier-v1.2',taxonomy_version:'topics-v1.4'},llm:{status:'NOT_CONFIGURED',usage_instrumented:false},scheduler:{status:'DISABLED',implemented:true,enabled:false,worker_running:false,reason:'Disabled in backend configuration.'}}),{status:200}));
  if(value==='/api/v1/scheduler')return Promise.resolve(new Response(JSON.stringify({items:[],implemented:true,enabled:false,worker_running:false}),{status:200}));
  if(value==='/api/v1/algorithms')return Promise.resolve(new Response(JSON.stringify({evaluation_status:'NOT_YET_EVALUATED',algorithms:[{algorithm_id:'observed-volume-rank',version:'v3',name:'Observed volume rank'}]}),{status:200}));
  if(value==='/api/v1/algorithms/results')return Promise.resolve(new Response(JSON.stringify({summary:[],items:[]}),{status:200}));
  if(value==='/api/v1/collections')return Promise.resolve(new Response(JSON.stringify({items:[],suggestions:[],mode:'SUGGEST_ONLY',summary:{}}),{status:200}));
  if(value==='/api/v1/collections/inbox')return Promise.resolve(new Response(JSON.stringify({items:[],total:0}),{status:200}));
  if(value==='/api/v1/research')return Promise.resolve(new Response(JSON.stringify({items:[],total:0}),{status:200}));
  if(value==='/api/v1/runs/history')return Promise.resolve(new Response(JSON.stringify({items:[],total:0,next_offset:null}),{status:200}));
  if(value==='/api/v1/datasets/latest/keywords')return Promise.resolve(new Response(JSON.stringify({items:[],total:0,next_offset:null}),{status:200}));
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
            evaluate("document.querySelector('#dashboard-analysis-tab').click()")
            if not evaluate("window.waitFor(()=>document.querySelector('#analysis-algorithms').textContent.includes('Xếp hạng theo lượng ghi nhận'))", await_promise=True):
                raise RuntimeError("algorithm analysis table exposed a technical identifier instead of the localized registry name")
            if not evaluate("document.querySelectorAll('#analysis-history-chart .chart-row').length===1 && Boolean(document.querySelector('#analysis-history-chart').getAttribute('aria-label'))"):
                raise RuntimeError("daily observation coverage chart did not render with an accessible label")
            evaluate("document.querySelector('[data-nav=settings]').click()")
            if not evaluate("window.waitFor(()=>document.querySelector('#settings-schedules .schedule-empty'))", await_promise=True):
                raise RuntimeError("settings did not show the honest empty scheduler state")
            if not evaluate("document.querySelector('#settings-scheduler-status').textContent.length>0"):
                raise RuntimeError("scheduler runtime status was not shown in settings")
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
