"""Chrome E2E over the actual synthetic API, in Windows or loopback-only Linux."""

import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from browser_support import (
    ChromeDevTools,
    find_chrome,
    linux_version,
    require_loopback_only_network_isolation,
)

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "Backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from d6_wikimedia_synthetic_support import WIKIMEDIA_SOURCE_ID, build_synthetic_app  # noqa: E402


def _wait_for_app(base_url, server_thread, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_thread.is_alive():
            raise RuntimeError("synthetic ASGI server exited before becoming ready")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("synthetic ASGI server did not become ready")


def _evaluate(devtools, expression, *, await_promise=False, session=None):
    result = devtools.call("Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": await_promise,
        "returnByValue": True,
        "userGesture": True,
    }, session=session)
    if result.get("exceptionDetails"):
        raise AssertionError(result["exceptionDetails"].get("text", "browser evaluation failed"))
    return result.get("result", {}).get("value")


def main():
    isolation = require_loopback_only_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    chrome_version = subprocess.run(
        [chrome, "--version"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    with tempfile.TemporaryDirectory(prefix="d6-2-3-wikimedia-browser-") as directory:
        app = build_synthetic_app(directory)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            proxy_headers=False,
            server_header=False,
        ))
        server_thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        server_thread.start()
        devtools = None
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_for_app(base_url, server_thread)
            if app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID] != 0:
                raise AssertionError("dashboard application startup requested the fixture")
            with tempfile.TemporaryDirectory(prefix="d6-2-3-chrome-profile-") as profile:
                devtools = ChromeDevTools(chrome, profile)
                if isolation["kind"] == "linux-loopback-only-netns":
                    isolation = require_loopback_only_network_isolation(devtools.process.pid)
                try:
                    target = devtools.call("Target.createTarget", {"url": "about:blank"})
                    attached = devtools.call("Target.attachToTarget", {
                        "targetId": target["targetId"], "flatten": True,
                    })
                    session = attached["sessionId"]
                    devtools.call("Page.enable", session=session)
                    devtools.call("Runtime.enable", session=session)
                    devtools.call("Network.enable", session=session)
                    devtools.call("Page.addScriptToEvaluateOnNewDocument", {"source": r"""
(() => {
  try { localStorage.setItem('trendscout.locale', 'en'); } catch {}
  window.__d623Requests = [];
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = new URL(String(input), location.href);
    const method = String(init.method || 'GET').toUpperCase();
    const headers = new Headers(init.headers || {});
    const row = {url: url.href, path: url.pathname, method,
      headers: Object.fromEntries(headers.entries()), body: init.body || null};
    window.__d623Requests.push(row);
    if (url.pathname === '/api/v1/fetches' && method === 'POST') {
      const count = Number(sessionStorage.getItem('d6-test-post-count') || '0') + 1;
      sessionStorage.setItem('d6-test-post-count', String(count));
    }
    const pending = nativeFetch(input, init);
    if (/^\/api\/v1\/fetches\/[a-f0-9]{32}$/i.test(url.pathname) && method === 'GET') {
      pending.then((response) => response.clone().json())
        .then((payload) => { window.__d623StatusPayload = payload; });
    }
    return pending;
  };
})();
"""}, session=session)
                    devtools.call("Page.navigate", {"url": base_url + "/dashboard/"},
                                  session=session)

                    script = r"""
(async () => {
  const failures = [];
  let assertionCount = 0;
  const check = (condition, label) => {
    assertionCount += 1;
    if (!condition) failures.push(label);
  };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitFor = async (predicate, timeout = 10000) => {
    const until = Date.now() + timeout;
    while (Date.now() < until) {
      if (predicate()) return true;
      await wait(40);
    }
    return false;
  };

  check(await waitFor(() => document.querySelector('#open-fetch')
    && !document.querySelector('#open-fetch').disabled), 'synthetic Fetch enabled by actual capabilities');
  const button = document.querySelector('#open-fetch');
  button.click();
  button.click();
  check(await waitFor(() => !document.querySelector('#demo-discovery').hidden
    && document.querySelector('#demo-discovery-list strong')),
    'actual API status reaches dashboard and renders candidates');
  await wait(100);

  const requests = window.__d623Requests;
  const posts = requests.filter((item) => item.path === '/api/v1/fetches' && item.method === 'POST');
  const statusReads = requests.filter((item) => /^\/api\/v1\/fetches\/[a-f0-9]{32}$/i.test(item.path));
  check(posts.length === 1, 'two quick clicks create one Fetch job');
  check(posts[0]?.body === '{}', 'browser sends the server-default empty Fetch body');
  check(Boolean(posts[0]?.headers['idempotency-key']), 'Fetch request includes idempotency key');
  check(!/token|secret|credential|password/i.test(JSON.stringify(posts[0]?.headers || {})),
    'browser sends no credential');
  check(statusReads.length >= 1, 'dashboard polls status through public Fetch API');
  check(requests.every((item) => new URL(item.url).origin === location.origin),
    'browser Fetch requests use same-origin API only');
  check(!document.querySelector('#demo-discovery-list img'), 'untrusted article text creates no image element');
  check(!document.querySelector('#demo-discovery-list script'), 'untrusted article text creates no script element');
  check(!document.body.textContent.includes('D6_2_3_RAW_BODY_MUST_NOT_PERSIST'),
    'raw fixture body marker is not rendered');
  check(!Object.keys(localStorage).some((key) => /token|secret|credential|password/i.test(key)),
    'no provider credential in localStorage');
  check(!document.querySelector('#demo-discovery').textContent.includes('Search Volume'),
    'Pageviews is not relabeled Search Volume');

  const sourceText = document.querySelector('#demo-source-list').textContent;
  check(sourceText.includes('Wikimedia') && sourceText.includes('SYNTHETIC'),
    'Wikimedia source and synthetic mode shown');
  check(sourceText.includes('Google / SerpApi') && sourceText.includes('UNAVAILABLE'),
    'Google shows unavailable');
  check(sourceText.includes('Bing / SerpApi') && sourceText.includes('UNAVAILABLE'),
    'Bing shows unavailable');
  check(sourceText.includes('YouTube') && sourceText.includes('UNAVAILABLE'),
    'YouTube shows unavailable');
  check(!sourceText.includes('LIVE_VERIFIED'), 'no source is represented as live verified');
  check(document.querySelector('#demo-discovery-list').textContent.includes('INSUFFICIENT_EVIDENCE'),
    'single snapshot classification remains insufficient');
  check(await waitFor(() => window.__d623StatusPayload), 'status response is captured for semantic checks');
  const statusPayload = window.__d623StatusPayload;
  check(statusPayload.discovery.candidates.every((candidate) => candidate.score === null),
    'insufficient evidence keeps candidate scores null');
  check(!JSON.stringify(statusPayload).toLowerCase().includes('search_volume'),
    'API response does not fabricate search volume');
  check(!/YOUTUBE_API_KEY|SERPAPI_API_KEY/i.test(JSON.stringify(statusPayload)),
    'API status response exposes no provider credential names');

  const search = document.querySelector('#search');
  search.value = 'linen pants';
  search.dispatchEvent(new Event('input', {bubbles: true}));
  await wait(450);
  let shown = document.querySelector('#demo-discovery-list').textContent;
  check(shown.includes('linen pants') && !shown.includes('kitchen organizer'), 'keyword search filters locally');
  search.value = '';
  search.dispatchEvent(new Event('input', {bubbles: true}));
  await wait(450);
  document.querySelector('[data-category="fashion"]').click();
  shown = document.querySelector('#demo-discovery-list').textContent;
  check(shown.includes('linen pants') && !shown.includes('kitchen organizer'), 'Fashion filter works');
  document.querySelector('[data-category="home_goods"]').click();
  shown = document.querySelector('#demo-discovery-list').textContent;
  check(shown.includes('kitchen organizer') && !shown.includes('linen pants'), 'Home & Living filter works');
  const evidence = document.querySelector('#demo-discovery-list details');
  if (evidence) evidence.open = true;
  const evidenceText = document.querySelector('#demo-discovery-list').textContent;
  check(evidenceText.includes('wikimedia_pageviews_top_per_country'), 'evidence identifies Wikimedia source');
  check(evidenceText.includes('wikimedia:US:all-access:en.wikipedia'), 'evidence displays country/access/project scope');
  check(evidenceText.includes('Nov 10, 2026'), 'evidence displays fixed observation date');
  check(evidenceText.includes('SYNTHETIC'), 'evidence displays synthetic provenance');

  document.querySelector('[data-category="all"]').click();
  document.querySelector('[data-theme-toggle]').click();
  check(document.documentElement.dataset.theme === 'dark', 'dark theme toggle remains available');
  document.querySelector('[data-theme-toggle]').click();
  document.querySelector('[data-locale-toggle]').click();
  check(document.documentElement.lang === 'vi', 'Vietnamese locale toggle remains available');
  document.querySelector('[data-locale-toggle]').click();
  check(document.documentElement.lang === 'en', 'English locale toggle remains available');
  check(Number(sessionStorage.getItem('d6-test-post-count')) === 1, 'one browser job submitted before reload');
  window.__d623Result = {failures, postCount: posts.length, statusPolls: statusReads.length,
    requestCount: requests.length, assertionCount, sourceText, evidenceText};
  return window.__d623Result;
})()
"""
                    browser_result = _evaluate(devtools, script, await_promise=True,
                                               session=session)
                    if not browser_result or browser_result.get("failures"):
                        raise AssertionError(f"dashboard synthetic E2E failed: {browser_result}")

                    devtools.call("Emulation.setDeviceMetricsOverride", {
                        "width": 375, "height": 812, "deviceScaleFactor": 1, "mobile": False,
                    }, session=session)
                    mobile = _evaluate(devtools, "({width: innerWidth, app: !!document.querySelector('#app-shell')})",
                                       session=session)
                    if not mobile or mobile["width"] != 375 or not mobile["app"]:
                        raise AssertionError(f"dashboard did not retain mobile viewport layout: {mobile}")
                    devtools.call("Emulation.clearDeviceMetricsOverride", session=session)

                    devtools.call("Page.reload", {"ignoreCache": True}, session=session)
                    reload_result = _evaluate(devtools, r"""
(async () => {
  const until = Date.now() + 5000;
  while (Date.now() < until && !document.querySelector('#open-fetch'))
    await new Promise((resolve) => setTimeout(resolve, 25));
  await new Promise((resolve) => setTimeout(resolve, 250));
  return {postCount: Number(sessionStorage.getItem('d6-test-post-count') || '0'),
    discoveryHidden: document.querySelector('#demo-discovery').hidden};
})()
""", await_promise=True, session=session)
                    if not reload_result or reload_result["postCount"] != 1:
                        raise AssertionError(f"reload submitted a duplicate job: {reload_result}")
                    if app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID] != 1:
                        raise AssertionError("browser reload or polling repeated the Wikimedia fixture request")
                    if isolation["kind"] == "linux-loopback-only-netns":
                        isolation = require_loopback_only_network_isolation(devtools.process.pid)
                finally:
                    devtools.close()
                    devtools = None

            print(f"OS: {linux_version() if os.name != 'nt' else platform.platform()}")
            print(f"Chrome: {chrome_version}")
            print(f"Network isolation: {isolation}")
            print(
                "Actual dashboard/API synthetic Wikimedia flow: PASS; "
                f"one browser Fetch, {browser_result['statusPolls']} status poll(s), "
                f"one Wikimedia MockTransport request, {browser_result['assertionCount']} assertions, "
                f"{len(browser_result['failures'])} failures"
            )
        finally:
            if devtools is not None:
                devtools.close()
            server.should_exit = True
            server_thread.join(timeout=10)
            listener.close()
            if server_thread.is_alive():
                raise RuntimeError("synthetic ASGI server did not stop")


if __name__ == "__main__":
    main()
