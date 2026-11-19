"""Offline browser regression for the Vercel Shared LIVE Fetch result route."""

import json
import subprocess
import tempfile
from pathlib import Path

from browser_support import find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
FETCH_ID = "f" * 43
SOURCE_ID = "wikimedia_pageviews_top_per_country"


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")

    discovery = {
        "data_mode": "LIVE",
        "shown_count": 1,
        "candidate_count": 1,
        "candidates": [{
            "keyword": "Shared cache result",
            "categories": [],
            "status": "INSUFFICIENT_EVIDENCE",
            "evidence": [{
                "source_id": SOURCE_ID,
                "data_mode": "LIVE",
                "delivery_mode": "CACHED",
                "observed_at": "2026-11-18T00:00:00Z",
                "fetched_at": "2026-11-19T12:20:00Z",
                "approximate_views": 123,
            }],
        }],
    }
    source = {
        "source_id": SOURCE_ID,
        "display_name": "Wikimedia Pageviews",
        "enabled_for_current_operation": True,
        "adapter_status": "LIVE_CONFIGURED",
        "approval_status": "APPROVED_FOR_VERCEL_SHARED_RUNTIME",
        "delivery_state": "CACHED",
        "record_count": 1,
    }
    capabilities = {
        "contract_version": "1.0",
        "environment_mode": "vercel",
        "read_only": False,
        "authentication_required": False,
        "public_fetch_enabled": True,
        "fetch_enabled": True,
        "fetch_block_reason": None,
        "data_mode": "LIVE",
        "vercel_shared_live": True,
        "sources": [source],
    }

    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="/dashboard/styles.css">',
        "<style>" + css + "</style>",
    )
    html = html.replace(
        'href="/dashboard/phoenix.css"',
        'href="' + (PUBLIC / "phoenix.css").as_uri() + '"',
    )
    html = html.replace(
        'src="/dashboard/app.js"',
        'src="' + (PUBLIC / "app.js").as_uri() + '"',
    )
    bridge = """
<script>
const fetchId = '__FETCH_ID__';
const discovery = __DISCOVERY__;
const capabilities = __CAPABILITIES__;
const source = __SOURCE__;
window.fetchRequests = [];
window.statusReads = 0;
window.collectionsReads = 0;
window.fetch = (path, options = {}) => {
  const value = String(path).split('?')[0];
  const method = options.method || 'GET';
  window.fetchRequests.push({path: value, method});
  let body = {};
  if (value === '/api/health') body = {status: 'ok'};
  else if (value === '/api/v1/capabilities') body = capabilities;
  else if (value === '/api/v1/fetches' && method === 'POST') {
    body = {fetch_id: fetchId, status: 'QUEUED', data_mode: 'LIVE'};
  } else if (value === '/api/v1/fetches/' + fetchId) {
    window.statusReads += 1;
    if (window.statusReads === 1) {
      return Promise.resolve(new Response(JSON.stringify({detail: {code: 'STATUS_POLL_LIMIT'}}),
        {status: 429, headers: {'content-type': 'application/json'}}));
    }
    body = {fetch_id: fetchId, status: 'COMPLETED', terminal: true,
      data_mode: 'LIVE', discovery, sources: [source],
      finished_at: '2026-11-19T12:21:00Z'};
  } else if (value === '/api/v1/collections') {
    window.collectionsReads += 1;
    return Promise.resolve(new Response(JSON.stringify({detail: {code: 'STORAGE_NOT_READY'}}),
      {status: 503, headers: {'content-type': 'application/json'}}));
  }
  return Promise.resolve(new Response(JSON.stringify(body),
    {status: 200, headers: {'content-type': 'application/json', 'cache-control': 'no-store'}}));
};
</script>
""".replace("__FETCH_ID__", FETCH_ID)
    bridge = bridge.replace("__DISCOVERY__", json.dumps(discovery))
    bridge = bridge.replace("__CAPABILITIES__", json.dumps(capabilities))
    bridge = bridge.replace("__SOURCE__", json.dumps(source))
    check = """
<script>
(async () => {
  const errors = [];
  const check = (value, message) => { if (!value) errors.push(message); };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  await wait(200);
  const button = document.querySelector('#open-fetch');
  check(Boolean(button && !button.disabled), 'Vercel Shared LIVE Fetch is enabled');
  const initialCollectionsReads = window.collectionsReads;
  button?.click();
  await wait(250);
  check(window.statusReads === 1, 'first status check reaches the bounded polling limit');
  check(Boolean(button && !button.disabled && button.textContent.includes('Kiểm tra trạng thái')),
    'bounded polling exposes a Check status action');
  button?.click();
  await wait(400);
  check(window.statusReads === 2, 'Check status resumes the existing Fetch');
  check(window.fetchRequests.filter((request) =>
    request.path === '/api/v1/fetches' && request.method === 'POST').length === 1,
  'Check status does not submit a duplicate Fetch');
  check(window.location.hash === '#overview', 'completed Shared LIVE Fetch stays on Overview');
  check(window.collectionsReads === initialCollectionsReads,
    'Fetch completion does not open SQLite Collections');
  check(document.querySelector('#rows').textContent.includes('Shared cache result'),
    'cached LIVE result is visible in keyword rows');
  check(document.querySelector('#demo-discovery-list').textContent.includes('Shared cache result'),
    'cached LIVE discovery evidence is visible');
  check(!document.querySelector('[data-view="overview"]').hidden,
    'Overview remains the active page');
  document.body.setAttribute('data-vercel-shared-fetch-result',
    errors.length ? 'FAIL: ' + errors.join('; ') : 'PASS');
})();
</script>
"""
    html = html.replace("</body>", bridge + check + "</body>")
    with tempfile.TemporaryDirectory(prefix="trendscout-vercel-shared-fetch-") as directory:
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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode or 'data-vercel-shared-fetch-result="PASS"' not in result.stdout:
            marker = result.stdout[result.stdout.find("data-vercel-shared-fetch-result=") - 200:][:900]
            raise SystemExit(
                f"Vercel Shared LIVE Fetch Chrome E2E failed: {marker}; "
                f"browser stderr={result.stderr[-700:]}"
            )
    print("Vercel Shared LIVE Fetch completion stays on Overview and renders cached data: PASS")


if __name__ == "__main__":
    main()
