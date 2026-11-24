"""Offline browser regression for the Vercel Shared LIVE Fetch result route."""

import json
import subprocess
import tempfile
from pathlib import Path

from browser_support import find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
FETCH_ID = "f" * 32
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
        "deployment_environment": "production",
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
const dataset = {
  status: 'PARTIAL', sources: [source.source_id], fetched_at: '2026-11-19T12:21:00Z',
  snapshot_retained_keyword_count: 1, retained_keyword_count: 1,
  assigned_keyword_count: 0, eligible_keyword_count: 1, unassigned_keyword_count: 1,
  provider_record_count: 1, source_manifest: [source],
};
window.fetchRequests = [];
window.statusReads = 0;
window.collectionsReads = 0;
window.collectionRecoveryPosts = Number(localStorage.getItem('collectionRecoveryPosts') || 0);
window.fetch = (path, options = {}) => {
  const value = String(path).split('?')[0];
  const method = options.method || 'GET';
  window.fetchRequests.push({path: value, method, headers: options.headers || {}, body: options.body || null});
  let body = {};
  if (value === '/api/health') body = {status: 'ok'};
  else if (value === '/api/v1/capabilities') body = capabilities;
  else if (value === '/api/v1/fetches' && method === 'POST') {
    body = {fetch_id: fetchId, status: 'QUEUED', data_mode: 'LIVE'};
  } else if (value === '/api/v1/fetches/' + fetchId) {
    window.statusReads += 1;
    if (window.statusReads <= 3) {
      return Promise.resolve(new Response(JSON.stringify({detail: {code: 'TEMPORARY_STATUS_FAILURE'}}),
        {status: 503, headers: {'content-type': 'application/json'}}));
    }
    body = {fetch_id: fetchId, status: 'COMPLETED', terminal: true,
      data_mode: 'LIVE', discovery, sources: [
        source,
        {source_id: 'google_web_serp', status: 'SUCCESS'},
        {source_id: 'bing_web_serp', status: 'UNAVAILABLE'},
        {source_id: 'youtube_video_search', status: 'SUCCESS'},
      ],
      finished_at: '2026-11-19T12:21:00Z'};
  } else if (value === '/api/v1/runs/history') {
    body = {items: []};
  } else if (value === '/api/v1/datasets/latest') {
    body = {item: window.collectionRecoveryPosts ? dataset : null};
  } else if (value === '/api/v1/analytics/overview') {
    body = {keyword_totals: {unique: 1, classified: 0, unclassified: 1}, active_sources: [],
      source_volume: [], algorithm_distribution: []};
  } else if (value === '/api/v1/algorithms') {
    body = {algorithms: []};
  } else if (value === '/api/v1/collections/jobs/' + fetchId) {
    body = {status: window.collectionRecoveryPosts ? 'COMPLETED' : 'NOT_PROCESSED',
      eligible: 1, processed: 1, assigned: 0, created_collections: 0,
      cached_embeddings: 0, unassigned: 1, skipped: 0};
  } else if (value === '/api/v1/fetches/' + fetchId + '/collections' && method === 'POST') {
    window.collectionRecoveryPosts += 1;
    localStorage.setItem('collectionRecoveryPosts', String(window.collectionRecoveryPosts));
    body = {status: 'COMPLETED'};
  } else if (value === '/api/v1/collections') {
    window.collectionsReads += 1;
    body = {items: [], suggestions: [], mode: 'AUTO_CREATE', summary: {
      eligible_keywords: 1, assigned_keywords: 0, unclassified_keywords: 1,
      excluded_keywords: 0, classified_keywords: 0, ambiguous_keywords: 0,
      unknown_keywords: 1, ineligible_topic_keywords: 0, ai_assigned_keywords: 0,
      manually_managed_keywords: 0, ai_created_collections: 0, sources: [source.source_id],
    }};
  } else if (value.startsWith('/api/v1/datasets/latest/keywords')) {
    body = {items: [], total: 1, next_offset: null};
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
  if (sessionStorage.getItem('vercel-shared-fetch-reload-verified') === '1') {
    const reloadDeadline = Date.now() + 4000;
    while (Date.now() < reloadDeadline
      && document.querySelector('#keyword-count').textContent.trim() !== '1') await wait(100);
    check(document.querySelector('#keyword-count').textContent.trim() === '1',
      'saved dataset remains visible after a full page reload');
    check(window.collectionRecoveryPosts === 1,
      'reload reads the existing snapshot without replaying recovery');
    check(!window.fetchRequests.some((request) => request.method === 'POST'),
      'reload does not start another Fetch or Collections recovery POST');
    document.body.setAttribute('data-vercel-shared-fetch-result',
      errors.length ? 'FAIL: ' + errors.join('; ') : 'PASS');
    return;
  }
  await wait(200);
  check(!window.fetchRequests.some((request) => request.path === '/api/v1/collections'),
    'opening Overview does not eagerly load the hidden Collections workbench');
  const button = document.querySelector('#open-fetch');
  const readyDeadline = Date.now() + 4000;
  while (button?.disabled && Date.now() < readyDeadline) await wait(50);
  check(Boolean(button && !button.disabled), 'Vercel Shared LIVE Fetch is enabled');
  button?.click();
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline && window.statusReads < 4) await wait(100);
  while (Date.now() < deadline && document.querySelector('#keyword-count').textContent.trim() !== '1') await wait(100);
  check(window.statusReads === 4, 'Fetch automatically resumes after transient status failures');
  const submissions = window.fetchRequests.filter((request) =>
    request.path === '/api/v1/fetches' && request.method === 'POST');
  check(submissions.length === 3,
    'one enqueue and two bounded Vercel continuations finish the durable Fetch');
  check(submissions.length === 3 && submissions.every((request) =>
    request.headers['Idempotency-Key'] === submissions[0].headers['Idempotency-Key']),
  'all automatic continuations reuse the idempotency key');
  check(submissions.length === 3
    && submissions[0].headers['X-TrendScout-Fetch-Continuation'] === undefined
    && submissions.slice(1).every((request) =>
      request.headers['X-TrendScout-Fetch-Continuation'] === '1'),
  'only Vercel continuation requests carry the continuation marker');
  check(window.collectionRecoveryPosts === 1, 'saved source checkpoints are persisted without provider calls');
  const recoveryIndex = window.fetchRequests.findIndex((request) =>
    request.path === '/api/v1/fetches/' + fetchId + '/collections' && request.method === 'POST');
  check(recoveryIndex >= 0 && !window.fetchRequests.slice(0, recoveryIndex).some((request) =>
    request.path === '/api/v1/collections/jobs/' + fetchId),
  'terminal Fetch starts idempotent recovery without a preceding Collections job read');
  const recoveryRequests = window.fetchRequests.slice(recoveryIndex + 1);
  check(recoveryRequests.filter((request) => request.path === '/api/v1/datasets/latest').length === 1,
    'recovery refreshes the dataset once');
  check(recoveryRequests.filter((request) => request.path === '/api/v1/analytics/overview').length === 1,
    'recovery refreshes analytics once');
  check(window.location.hash === '#overview', 'completed Shared LIVE Fetch stays on Overview');
  document.querySelector('[data-nav="collections"]').click();
  await wait(250);
  check(window.collectionsReads >= 1, 'persisted Collections snapshot is readable');
  document.querySelector('[data-nav="overview"]').click();
  await wait(50);
  check(document.querySelector('#keyword-count').textContent.trim() === '1',
    'recovered cached result remains visible in the dashboard');
  check(document.querySelector('#demo-discovery-list').textContent.includes('Shared cache result'),
    'cached LIVE discovery evidence is visible');
  check(!document.querySelector('[data-view="overview"]').hidden,
    'Overview remains the active page');
  if (errors.length) {
    document.body.setAttribute('data-vercel-shared-fetch-result',
      'FAIL: ' + errors.join('; ') + ' | debug: ' + JSON.stringify({
      statusReads: window.statusReads, collectionRecoveryPosts: window.collectionRecoveryPosts,
      buttonDisabled: button?.disabled,
      requests: window.fetchRequests.map((request) => [request.method, request.path]),
      }));
    return;
  }
  sessionStorage.setItem('vercel-shared-fetch-reload-verified', '1');
  window.location.reload();
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
            "--virtual-time-budget=15000",
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
            marker = result.stdout[result.stdout.find("data-vercel-shared-fetch-result=") - 200:][:2400]
            raise SystemExit(
                f"Vercel Shared LIVE Fetch Chrome E2E failed: {marker}; "
                f"browser stderr={result.stderr[-700:]}"
            )
    print("Vercel Shared LIVE Fetch completion stays on Overview and renders cached data: PASS")


if __name__ == "__main__":
    main()
