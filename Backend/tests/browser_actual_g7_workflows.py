"""Native Chrome workflows against the real local API and temporary SQLite stores."""

# ruff: noqa: E402

import json
import os
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from browser_support import ChromeDevTools, find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
os.sys.path.insert(0, str(ROOT / "Backend"))

from app.collections.service import CollectionService
from app.collections.store import CollectionStore
from app.main import create_app
from app.scheduling.persistent import PersistentScheduleStore
from app.settings import Settings

TOKEN = "synthetic-browser-operator-token-only-0001"


def seed_keyword(store):
    with store.connect() as db:
        for keyword_id, keyword, status in (
            ("g7-browser-keyword", "Browser workflow topic", "CLASSIFIED"),
            ("g7-browser-unknown", "Unknown context topic", "UNKNOWN"),
            ("g7-browser-ineligible", "Verified system page", "INELIGIBLE"),
        ):
            db.execute(
                """INSERT INTO keywords
                (id,keyword,normalized,source,project,observed_at,data_mode,evidence,context,eligible)
                VALUES(?,?,?,?,?,?,?,?,?,1)""",
                (keyword_id, keyword, keyword.casefold(), "wikimedia_pageviews_top_per_country",
                 "en.wikipedia", "2026-11-14", "SYNTHETIC", json.dumps({"views_ceil": 0}), ""),
            )
            db.execute(
                """INSERT INTO classification_results
                (keyword_id,entity_type,entity_description,topic_category_ids,subcategory_ids,
                classification_status,confidence_level,commercial_relevance,classification_method,
                classification_version,taxonomy_version,rule_evidence,source_evidence,review_status,classified_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (keyword_id, "topic", "Synthetic browser fixture", "[]", "[]", status, "NONE",
                 "NOT_ASSESSED", "TEST_FIXTURE", "fixture-v1", "topics-v1.5", "[]", "{}",
                 "UNREVIEWED", "2026-11-14T00:00:00Z"),
            )
    collection_id = store.create({"name": "Evidence-backed topics", "auto_match": False})
    with store.connect() as db:
        db.execute("INSERT INTO memberships VALUES(?,?, 'USER',0,NULL,'Browser fixture',?)",
                   (collection_id, "g7-browser-keyword", "2026-11-14T00:00:00Z"))


def seed_run_history(store):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with store.connect() as db:
        for index in range(25):
            fetch_id = f"{index + 1:032x}"
            stamp = (now - timedelta(minutes=25 - index)).isoformat()
            status = "COMPLETED" if index % 5 == 0 else "PARTIAL"
            count = index + 3
            manifest = json.dumps(
                [{"source_id": "wikimedia_pageviews_top_per_country", "status": status, "record_count": count}]
            )
            db.execute(
                """INSERT INTO fetch_jobs(fetch_id,status,available,processed,updated_at,stage,eligible)
                VALUES(?,?,?,?,?,?,?)""",
                (fetch_id, status, count, count - 1, stamp, "COMPLETED", count),
            )
            db.execute(
                """INSERT INTO dataset_snapshots
                (dataset_id,status,data_mode,source_manifest,provider_record_count,received_record_count,
                retained_keyword_count,rejected_record_count,observation_start,observation_end,fetched_at,
                quality_flags,processing_status,eligible_keyword_count,assigned_keyword_count,
                unassigned_keyword_count,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fetch_id, status, "SYNTHETIC", manifest, count + 1, count, count - 1, 1,
                 "2026-11-13", "2026-11-14", stamp, json.dumps(["SYNTHETIC_FIXTURE"]),
                 "COMPLETED", count, count - 1, 1, stamp),
            )


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")

    with tempfile.TemporaryDirectory(prefix="trendscout-actual-g7-") as directory:
        root = Path(directory)
        collection_store = CollectionStore(root / "collections.sqlite")
        seed_keyword(collection_store)
        seed_run_history(collection_store)
        schedule_store = PersistentScheduleStore(root / "schedules.sqlite")
        stale_schedule = schedule_store.create(
            name="Previously enabled fixture",
            frequency="DAILY",
            timezone_name="Asia/Ho_Chi_Minh",
            hour=9,
            minute=0,
        )
        schedule_store.set_enabled(stale_schedule["id"], True)

        settings = Settings.from_environment(
            {
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_READ_ONLY": "false",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
                "TREND_SCOUT_SCHEDULER_ENABLED": "false",
                "TREND_SCOUT_OPERATOR_TOKEN": TOKEN,
                "TREND_SCOUT_LOCK_PATH": str(root / "run.lock"),
            }
        )
        app = create_app(settings=settings, read_only=False, scheduler_store=schedule_store)
        app.state.collection_service = CollectionService(collection_store)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        browser = None
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.025)
            else:
                raise RuntimeError("temporary local API server did not start")

            with httpx.Client(timeout=5) as client:
                health = client.get(base_url + "/api/health")
                if health.status_code != 200:
                    raise RuntimeError("temporary app health endpoint failed")

            browser = ChromeDevTools(chrome, root / "chrome-profile")
            browser.socket.settimeout(40)
            target = browser.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            session = browser.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
                "sessionId"
            ]
            browser.call("Page.enable", session=session)
            browser.call("Runtime.enable", session=session)
            browser.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False},
                session,
            )
            browser.call(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """(()=>{const f=window.fetch.bind(window);window.__apiCalls=[];
                    window.fetch=(u,o={})=>{window.__apiCalls.push({url:String(u),method:o.method||'GET'});return f(u,o)};})();"""
                },
                session,
            )
            browser.call("Page.navigate", {"url": base_url + "/dashboard/#research"}, session)

            def evaluate(expression):
                result = browser.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True, "awaitPromise": True},
                    session,
                )
                if "exceptionDetails" in result:
                    raise RuntimeError("Chrome evaluation failed: " + expression)
                return result["result"].get("value")

            expression = r"""(async()=>{
              const errors=[];const check=(v,m)=>{if(!v)errors.push(m)};
              const delay=ms=>new Promise(r=>setTimeout(r,ms));
              const until=async fn=>{for(let n=0;n<150;n++){if(fn())return true;await delay(40)}return false};
              await until(()=>document.readyState==='complete'&&!document.documentElement.classList.contains('js-pending'));
              check(document.documentElement.lang==='vi','Research workflow starts in Vietnamese');
              const go=async name=>{const a=document.querySelector('[data-nav="'+name+'"]');a?.click();location.hash='#'+name;window.dispatchEvent(new Event('hashchange'));await delay(250)};
              await go('sources');
              check(await until(()=>document.querySelectorAll('#source-registry-list .source-card').length>0),'Source Center renders current capability descriptors');
              check(document.querySelectorAll('#source-registry-list .source-card').length===4,'Source Center lists the four configured source descriptors');
              check([...document.querySelectorAll('#source-registry-list .source-card')].every(card=>card.textContent.includes('Tắt')||card.textContent.includes('Đã tắt')),'Source Center reflects the backend disabled fetch policy');
              check(await until(()=>document.querySelectorAll('#source-library-rows tr').length>0),'Source Center loads the research inventory from the API');
              check(window.__apiCalls.some(x=>x.url==='/api/v1/capabilities')&&window.__apiCalls.some(x=>x.url==='/api/v1/sources/library'),'Source Center uses real capability and catalog endpoints');
              await go('overview');
              check(await until(()=>document.querySelector('#metric-records-stored').textContent==='3'),'Dashboard total comes from saved SQLite keywords');
              check(document.querySelector('#metric-topics-found').textContent==='1'&&document.querySelector('#metric-unclassified').textContent==='1','Dashboard separates classified and unclassified saved topics');
              await go('collections');
              check(await until(()=>document.querySelectorAll('#workbench-summary .workbench-stat').length===4),'Collections summary loads from the SQLite backend');
              const collectionCounts=[...document.querySelectorAll('#workbench-summary .workbench-stat strong')].map(x=>x.textContent);
              check(collectionCounts.join(',')==='3,1,1,0','Collection cards show 3 stored, 1 assigned, 1 unclassified and 0 excluded');
              check(document.querySelector('#workbench-summary').textContent.includes('1 kh\u00f4ng ph\u1ea3i ch\u1ee7 \u0111\u1ec1'),'Collection summary explains the ineligible topic count');
              await go('algorithms');
              check(await until(()=>document.querySelectorAll('#algorithm-catalog .algorithm-card').length>0),'Algorithm catalog loads its registered methods');
              check(Boolean(document.querySelector('#algorithm-evaluation-status').textContent.trim()),'Algorithm evaluation status is shown separately from the catalog');
              check(!document.querySelector('#algorithm-catalog').textContent.includes('INSUFFICIENT_DATA'),'Algorithm catalog does not expose raw evaluation status codes');
              check(window.__apiCalls.some(x=>x.url==='/api/v1/algorithms')&&window.__apiCalls.some(x=>x.url==='/api/v1/algorithms/results'),'Algorithm catalog and results use actual backend endpoints');
              await go('research');
              await until(()=>document.querySelector('#research-keyword option[value="g7-browser-keyword"]'));
              const selector=document.querySelector('#research-keyword');selector.value='g7-browser-keyword';
              document.querySelector('#research-subject').value='Browser workflow hypothesis';
              document.querySelector('#research-hypothesis').value='A saved keyword may support this product hypothesis.';
              document.querySelector('#research-category').value='Research tools';
              document.querySelector('#research-niche').value='Independent researchers';
              document.querySelector('#research-competitors').value='Competitor notes before edit';
              document.querySelector('#research-market-evidence').value='User-entered evidence only';
              document.querySelector('#research-findings').value='Initial finding';
              document.querySelector('#research-form').requestSubmit();
              check(await until(()=>document.querySelectorAll('#research-records .research-record').length===1&&document.querySelector('#research-records').textContent.includes('Browser workflow hypothesis')),'Research create is shown after API save');
              let card=document.querySelector('#research-records .research-record');
              card.querySelector('button').click();await delay(50);
              check(document.querySelector('#research-competitors').value==='Competitor notes before edit','saved Research edit form loads existing competitor notes');
              document.querySelector('#research-competitors').value='Competitor notes edited in Chrome';
              document.querySelector('#research-form').requestSubmit();
              check(await until(()=>document.querySelector('#research-records').textContent.includes('Competitor notes edited in Chrome')),'Research edit persists and refreshes the saved record');
              card=document.querySelector('#research-records .research-record');
              const researchStatus=card.querySelector('select');researchStatus.value='RESEARCHING';researchStatus.dispatchEvent(new Event('change',{bubbles:true}));
              await delay(180);
              const researchId=(await (await fetch('/api/v1/research?limit=10')).json()).items[0]?.id;
              check(Boolean(researchId),'Research API lists the created record');
              check(researchStatus.value==='RESEARCHING','Research status control accepts a status update');
              document.querySelector('[data-locale-toggle]').click();await delay(180);
              check(document.documentElement.lang==='en'&&document.querySelector('#research-heading').textContent.toLowerCase().includes('product research'),'Research page relocalizes to English');
              document.querySelector('[data-locale-toggle]').click();await delay(180);
              return {ok:errors.length===0,errors,researchId};
            })()""".replace("TOKEN_VALUE", TOKEN)
            first_result = evaluate(expression)
            if not first_result or not first_result.get("ok"):
                raise RuntimeError("Native Chrome Research workflow failed: " + json.dumps(first_result))

            browser.call("Page.reload", {"ignoreCache": True}, session)
            for _ in range(150):
                if evaluate("document.readyState==='complete'&&!document.documentElement.classList.contains('js-pending')"):
                    break
                time.sleep(0.04)

            after_reload = r"""(async()=>{
              const errors=[];const check=(v,m)=>{if(!v)errors.push(m)};
              const delay=ms=>new Promise(r=>setTimeout(r,ms));
              const until=async fn=>{for(let n=0;n<150;n++){if(fn())return true;await delay(40)}return false};
              const go=async name=>{const a=document.querySelector('[data-nav="'+name+'"]');a?.click();location.hash='#'+name;window.dispatchEvent(new Event('hashchange'));await delay(250)};
              await until(()=>document.querySelector('#research-records .research-record'));
              check(document.querySelectorAll('#research-records .research-record').length===1&&document.querySelector('#research-records').textContent.includes('Browser workflow hypothesis'),'saved Research survives page reload');
              let card=document.querySelector('#research-records .research-record');
              const archive=card.querySelector('select');archive.value='ARCHIVED';archive.dispatchEvent(new Event('change',{bubbles:true}));await delay(180);

              await go('runs');
              check(await until(()=>document.querySelectorAll('#run-history tbody tr').length===20),'Run History loads the first bounded page from SQLite');
              check(!document.querySelector('#run-history-more').hidden,'Run History exposes the next page when saved rows remain');
              document.querySelector('#run-history-more').click();
              check(await until(()=>document.querySelectorAll('#run-history tbody tr').length===25),'Run History cursor loads all saved rows without truncation');
              check(document.querySelector('#run-history-more').hidden,'Run History hides pagination after the last saved row');
              const historyStatus=document.querySelector('#run-history-status');historyStatus.value='PARTIAL';historyStatus.dispatchEvent(new Event('change',{bubbles:true}));
              check(await until(()=>document.querySelectorAll('#run-history tbody tr').length===20),'Run History status filter applies to saved records');
              check(!document.querySelector('#run-history').textContent.includes('LOCAL_LEDGER_ONLY'),'local-only limitation hides its internal status code');
              historyStatus.value='';historyStatus.dispatchEvent(new Event('change',{bubbles:true}));
              check(await until(()=>document.querySelectorAll('#run-history tbody tr').length===20),'clearing Run History filter restores the first saved page');
              document.querySelector('#run-history tbody tr button')?.click();
              check(await until(()=>!document.querySelector('#run-history-detail').hidden&&document.querySelector('#run-history-detail dl')),'Run History opens details from the real API');
              check(document.querySelector('#run-history-detail').textContent.includes('Wikimedia')&&!document.querySelector('#run-history-detail').textContent.includes('wikimedia_pageviews_top_per_country'),'Run History detail localizes the saved source manifest');

              await go('settings');
              check(await until(()=>document.querySelectorAll('#settings-runtime-details div').length>=8),'AI and model settings load the backend runtime facts');
              check(window.__apiCalls.some(x=>x.url==='/api/v1/runtime'),'AI settings use the real runtime endpoint');
              check(await until(()=>document.querySelector('#settings-schedules .schedule-item')),'Scheduler API renders the previously enabled saved schedule');
              const staleCard=[...document.querySelectorAll('#settings-schedules .schedule-item')].find(x=>x.textContent.includes('Previously enabled fixture'));
              const disable=staleCard?.querySelector('.schedule-action');
              check(Boolean(disable&&!disable.disabled),'user can disable a saved schedule while the backend scheduler is off');
              const scheduleForm=document.querySelector('#settings-schedule-form');
              scheduleForm.elements.namedItem('operator-key').value='TOKEN_VALUE';
              disable?.click();
              check(await until(()=>{const live=[...document.querySelectorAll('#settings-schedules .schedule-item')].find(x=>x.textContent.includes('Previously enabled fixture'));return live?.querySelector('.schedule-action')?.disabled===true;}),'disable action persists and refreshes the schedule');
              check(/disabled|tắt/i.test(document.querySelector('#settings-scheduler-status').textContent),'scheduler remains disabled by backend policy');

              const form=document.querySelector('#settings-schedule-form');
              form.elements.namedItem('name').value='New local schedule';
              form.elements.namedItem('frequency').value='DAILY';
              form.elements.namedItem('timezone').value='America/New_York';
              form.elements.namedItem('time').value='14:25';
              form.elements.namedItem('operator-key').value='TOKEN_VALUE';
              form.requestSubmit();
              check(await until(()=>[...document.querySelectorAll('#settings-schedules .schedule-item')].some(x=>x.textContent.includes('New local schedule'))),'schedule create succeeds through the real API');
              let scheduleCard=[...document.querySelectorAll('#settings-schedules .schedule-item')].find(x=>x.textContent.includes('New local schedule'));
              scheduleCard.querySelector('.schedule-edit').click();await delay(50);
              form.elements.namedItem('frequency').value='WEEKLY';
              form.elements.namedItem('timezone').value='Europe/London';
              form.elements.namedItem('time').value='08:40';
              form.elements.namedItem('operator-key').value='TOKEN_VALUE';
              form.requestSubmit();
              check(await until(()=>[...document.querySelectorAll('#settings-schedules .schedule-item')].find(x=>x.textContent.includes('New local schedule'))?.textContent.includes('Europe/London')),'schedule edit refreshes the saved timezone');
              check(form.elements.namedItem('operator-key').value==='','operator credential is cleared after the request');
              check([...document.querySelectorAll('#settings-schedules .schedule-item')].every(x=>x.querySelector('.schedule-action')?.disabled),'enable controls stay disabled by backend policy');
              check(window.__apiCalls.some(x=>x.url.includes('/api/v1/research/')&&x.method==='PATCH'),'Research archive uses the real backend endpoint');
              check(window.__apiCalls.some(x=>x.url==='/api/v1/scheduler'&&x.method==='POST')&&window.__apiCalls.filter(x=>x.url.includes('/api/v1/scheduler/')&&x.method==='PATCH').length>=2,'schedule create/edit/disable use the real backend endpoints');
              check(window.__apiCalls.every(x=>x.url.startsWith('/')&&!x.url.startsWith('/api/v1/fetches')&&!x.url.startsWith('/api/runs')),'workflow uses only same-origin app routes and no Fetch/run endpoint');
              return {ok:errors.length===0,errors,calls:window.__apiCalls,scheduleIds:[...document.querySelectorAll('#settings-schedules .schedule-item')].map(x=>x.textContent.slice(0,90))};
            })()""".replace("TOKEN_VALUE", TOKEN)
            browser_result = evaluate(after_reload)
            if not browser_result or not browser_result.get("ok"):
                raise RuntimeError("Native Chrome workflow failed: " + json.dumps(browser_result))

            browser.call("Page.reload", {"ignoreCache": True}, session)
            for _ in range(150):
                if evaluate("document.readyState==='complete'&&!document.documentElement.classList.contains('js-pending')"):
                    break
                time.sleep(0.04)
            persisted_ui = "{}"
            for _ in range(150):
                persisted_ui = evaluate(
                    "JSON.stringify({research:document.querySelector('[data-view=research]')?.textContent,"
                    "schedules:document.querySelector('#settings-schedules')?.textContent,"
                    "secret:document.querySelector('[name=operator-key]')?.value,"
                    "calls:window.__apiCalls||[]})"
                )
                if "New local schedule" in json.loads(persisted_ui or "{}").get("schedules", ""):
                    break
                time.sleep(0.04)
            persisted_ui = json.loads(persisted_ui or "{}")
            if "New local schedule" not in persisted_ui.get("schedules", "") or "Europe/London" not in persisted_ui.get("schedules", ""):
                raise AssertionError("Edited schedule did not survive a browser reload: " + json.dumps(persisted_ui))
            if persisted_ui.get("secret"):
                raise AssertionError("Operator credential reappeared after reload")
            if any(not call["url"].startswith("/") for call in persisted_ui.get("calls", [])):
                raise AssertionError("Browser workflow made a non-local request")

            with httpx.Client(timeout=5) as client:
                origin = {"Origin": base_url}
                records = client.get(base_url + "/api/v1/research?limit=10", headers=origin).json()["items"]
                archived = next((item for item in records if item["id"] == first_result["researchId"]), None)
                if not archived or archived["status"] != "ARCHIVED":
                    raise AssertionError("Research status/evidence did not persist in SQLite")
                if archived["competitor_notes"] != "Competitor notes edited in Chrome":
                    raise AssertionError("Research competitor edit did not persist in SQLite")
                schedules = client.get(base_url + "/api/v1/scheduler", headers=origin).json()["items"]
                created = next((item for item in schedules if item["name"] == "New local schedule"), None)
                if not created or (created["frequency"], created["timezone"], created["hour"], created["minute"], created["enabled"]) != ("WEEKLY", "Europe/London", 8, 40, False):
                    raise AssertionError("Schedule edit did not persist exact settings in SQLite")
                if not created["next_run_at"]:
                    raise AssertionError("Schedule next-run calculation is missing")
                next_run = datetime.fromisoformat(created["next_run_at"].replace("Z", "+00:00"))
                if next_run.tzinfo is None or next_run <= datetime.now(timezone.utc):
                    raise AssertionError("Schedule next-run calculation is not a future timezone-aware timestamp")
                denied = client.patch(
                    base_url + f"/api/v1/scheduler/{created['id']}",
                    headers={**origin, "Authorization": f"Bearer {TOKEN}"},
                    json={"enabled": True},
                )
                if denied.status_code != 409 or denied.json().get("detail", {}).get("code") != "SCHEDULER_DISABLED_IN_BACKEND_CONFIGURATION":
                    raise AssertionError("Backend did not reject enabling while scheduler policy is off")
                remaining = schedule_store.list()
                if len({item["id"] for item in remaining}) != len(remaining):
                    raise AssertionError("Schedule edit duplicated a saved schedule")
                reopened_schedules = PersistentScheduleStore(root / "schedules.sqlite").list()
                if len(reopened_schedules) != 2:
                    raise AssertionError("Schedule records did not persist when the SQLite store was reopened")
                with schedule_store._connect() as db:
                    occurrence_count = db.execute("SELECT count(*) FROM schedule_occurrences").fetchone()[0]
                if occurrence_count != 0:
                    raise AssertionError("Disabled synthetic schedules unexpectedly executed")

            print(
                "Native Chrome -> real FastAPI -> temporary SQLite: Source Center; algorithm registry/results; AI runtime settings; Dashboard/Collections counts; Run History cursor/filter/detail; "
                "Product Research create/edit/archive/reload; Scheduler disable/create/edit/reload and policy denial: PASS"
            )
        finally:
            if browser:
                browser.close()
            server.should_exit = True
            server_thread.join(timeout=8)
            if server_thread.is_alive():
                server.force_exit = True
                server_thread.join(timeout=2)


if __name__ == "__main__":
    main()
