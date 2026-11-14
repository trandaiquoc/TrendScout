"""Native Chrome workflows against the real local API and temporary SQLite stores."""

# ruff: noqa: E402

import json
import os
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
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
        db.execute(
            """INSERT INTO keywords
            (id,keyword,normalized,source,project,observed_at,data_mode,evidence,context,eligible)
            VALUES(?,?,?,?,?,?,?,?,?,1)""",
            (
                "g7-browser-keyword",
                "Browser workflow topic",
                "browser workflow topic",
                "wikimedia_pageviews_top_per_country",
                "en.wikipedia",
                "2026-11-14",
                "SYNTHETIC",
                json.dumps({"views_ceil": 0}),
                "",
            ),
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

              await go('settings');
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
                "Native Chrome -> real FastAPI -> temporary SQLite: Product Research create/edit/archive/reload; "
                "Scheduler disable/create/edit, next-run persistence, and backend enable-policy rejection: PASS"
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
