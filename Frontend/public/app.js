const $ = (selector) => document.querySelector(selector);
const state = { runId: null, dataRunId: null, run: null, category: 'all', q: '', sort: 'score', offset: 0, limit: 20, next: null, poll: null, sequence: 0, runSequence: 0, submitting: false };
const statuses = { QUEUED:'Đang chờ', FETCHING:'Đang thu thập', NORMALIZING:'Đang chuẩn hóa', ANALYZING:'Đang phân tích', PERSISTING:'Đang lưu', COMPLETED:'Hoàn thành', PARTIAL:'Dữ liệu một phần', FAILED:'Thất bại', ABORTED:'Đã gián đoạn' };
const activeStatuses = new Set(['QUEUED','FETCHING','NORMALIZING','ANALYZING','PERSISTING']);
const labels = { EMERGING:'EMERGING', STABLE:'STABLE', FALLING:'FALLING', INSUFFICIENT_DATA:'INSUFFICIENT DATA' };
const num = (n) => typeof n === 'number' && Number.isFinite(n) ? new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 2 }).format(n) : '—';
const dateText = (s) => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) ? s.split('-').reverse().join('/') : '—';
const fetchedText = (s) => { const d = new Date(s); return s && !Number.isNaN(d.getTime()) ? `${new Intl.DateTimeFormat('vi-VN', {dateStyle:'short', timeStyle:'short'}).format(d)} (giờ máy)` : '—'; };
const el = (tag, text, className) => { const node = document.createElement(tag); if (text != null) node.textContent = String(text); if (className) node.className = className; return node; };
function notice(message, error = false) { const n = $('#notice'); n.hidden = !message; n.textContent = message || ''; n.classList.toggle('error', error); }
async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, { ...options, headers: { Accept:'application/json', ...options.headers } }); }
  catch { throw new Error('Không kết nối được backend. Kiểm tra server và thử lại.'); }
  let data; try { data = await response.json(); } catch { throw new Error('Backend trả về dữ liệu không hợp lệ.'); }
  if (!response.ok) {
    const messages = {401:'Token không hợp lệ hoặc không có quyền vận hành.', 404:'Không tìm thấy run hoặc keyword.', 409:'Run đang chạy hoặc kết quả chưa sẵn sàng.', 429:'Đang trong thời gian chờ giữa các lần chạy.', 503:'Backend hoặc storage chưa sẵn sàng.'};
    throw new Error(messages[response.status] || `Không thể hoàn tất yêu cầu (HTTP ${response.status}).`);
  }
  return data;
}
function showEmpty(title, detail) { const box = $('#empty'); box.hidden = false; box.querySelector('strong').textContent = title; box.querySelector('p').textContent = detail; }
function updateRun(run) {
  state.run = run;
  $('#run-status').textContent = statuses[run.status] || `Trạng thái: ${run.status || 'không rõ'}`;
  $('#run-note').textContent = run.run_id ? `Run ${run.run_id.slice(0, 10)}…` : 'Chưa có run';
  $('#data-date').textContent = dateText(run.data_date);
  $('#keyword-count').textContent = num(run.keyword_count);
  $('#scored-count').textContent = num(run.scored_count);
  const mode = run.mode === 'live' ? 'LIVE' : run.mode === 'synthetic' ? 'SYNTHETIC' : run.mode === 'replay' ? 'REPLAY' : 'CHƯA XÁC ĐỊNH';
  $('#source-meta').textContent = `US · All access · ${mode} · Fetch: ${fetchedText(run.fetched_at)}`;
  const warnings = [];
  if (run.mode === 'synthetic' || run.mode === 'replay') warnings.push(`${mode}: dữ liệu kiểm thử, không phải kết quả live.`);
  if (run.mode !== 'live' && run.mode !== 'synthetic' && run.mode !== 'replay') warnings.push('Chưa xác định chế độ dữ liệu.');
  if (run.status === 'PARTIAL') warnings.push('Run chỉ có dữ liệu một phần; kiểm tra các ngày nguồn hoặc lỗi lưu trữ.');
  if (run.status === 'FAILED' || run.status === 'ABORTED') warnings.push(`Run ${statuses[run.status].toLowerCase()}: ${run.error || 'không có chi tiết lỗi'}.`);
  const dataDate = Date.parse(`${run.data_date}T00:00:00Z`);
  if (Number.isFinite(dataDate) && Date.now() - dataDate > 3 * 86400000) warnings.push('Dữ liệu cũ: ngày quan sát cách hiện tại hơn 3 ngày.');
  const incomplete = Array.isArray(run.coverage) ? run.coverage.filter(x => x.status !== 'SUCCESS') : [];
  if (incomplete.length) warnings.push(`Ngày nguồn chưa đầy đủ: ${incomplete.map(x => `${x.date} (${x.http_status ?? x.status})`).join(', ')}.`);
  if (state.dataRunId && state.dataRunId !== state.runId) warnings.push('Bảng bên dưới vẫn thuộc run trước; kết quả mới chưa sẵn sàng.');
  notice(warnings.join('\n'), run.status === 'FAILED' || run.status === 'ABORTED');
  $('#open-fetch').disabled = activeStatuses.has(run.status) || state.submitting;
}
async function loadRun(id, { poll = false } = {}) {
  if (!/^[a-f0-9]{32}$/.test(id)) { notice('Run ID phải gồm 32 ký tự hex.', true); return; }
  if (!poll) { clearTimeout(state.poll); notice('Đang tải thông tin run…'); }
  const sequence = ++state.runSequence;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (sequence !== state.runSequence) return;
    if (!run || typeof run.status !== 'string') throw new Error('Dữ liệu trạng thái run không hợp lệ.');
    state.runId = id; $('#run-id').value = id; updateRun(run);
    if (activeStatuses.has(run.status)) { state.poll = setTimeout(() => loadRun(id, {poll:true}), 2000); return; }
    if ((run.status === 'COMPLETED' || run.status === 'PARTIAL') && Number.isInteger(run.keyword_count)) {
      state.dataRunId = id; state.offset = 0; renderRows([]); showEmpty('Đang tải kết quả', 'Vui lòng đợi.'); updateRun(run); await loadKeywords();
    }
  } catch (err) { if (sequence === state.runSequence) { notice(err.message, true); $('#open-fetch').disabled = false; } }
}
function makeCell(row, value, className) { const td = el('td'); td.append(el('span', value, className)); row.append(td); }
function safeEvidenceUrl(value) {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && /^(?:[a-z0-9-]+\.)*(?:wikipedia|wikimedia|wiktionary|wikiquote|wikibooks|wikinews|wikisource|wikiversity|wikivoyage|wikidata|mediawiki|wikifunctions)\.org$/i.test(url.hostname) ? url.href : null; } catch { return null; }
}
function renderRows(items) {
  const body = $('#rows'); body.replaceChildren();
  for (const item of items) {
    const row = el('tr'); const title = el('td'); title.append(el('strong', item.display_keyword || item.canonical_keyword || '—'), el('small', item.project || '—')); row.append(title);
    makeCell(row, item.score == null ? '—' : `${num(item.score)} / 100`, 'score');
    makeCell(row, labels[item.status] || item.status || '—', `badge ${['EMERGING','STABLE','FALLING','INSUFFICIENT_DATA'].includes(item.status) ? item.status : ''}`);
    makeCell(row, num(item.current_views_ceil)); makeCell(row, num(item.baseline)); makeCell(row, item.ratio == null ? '—' : `${num(item.ratio)}×`); makeCell(row, dateText(item.observation_date));
    const flags = Array.isArray(item.quality_flags) ? item.quality_flags : []; makeCell(row, flags.length ? `${flags.length} cờ` : '—');
    const action = el('td'); const button = el('button', 'Chi tiết ↗', 'row-button'); button.type = 'button'; button.setAttribute('aria-label', `Xem chi tiết ${item.display_keyword || item.canonical_keyword || 'keyword'}`); button.addEventListener('click', () => openDetail(item.keyword_id)); action.append(button); row.append(action); body.append(row);
  }
}
async function loadKeywords() {
  if (!state.dataRunId) return;
  const seq = ++state.sequence;
  const params = new URLSearchParams({ q:state.q, category:state.category, sort:state.sort, limit:String(state.limit), offset:String(state.offset) });
  try {
    const result = await api(`/api/runs/${encodeURIComponent(state.dataRunId)}/keywords?${params}`);
    if (seq !== state.sequence) return;
    if (!result || !Array.isArray(result.items) || !Number.isInteger(result.total)) throw new Error('Danh sách keyword không hợp lệ.');
    renderRows(result.items); state.next = result.next_offset;
    $('#results-count').textContent = num(result.total);
    $('#pagination-info').textContent = result.total ? `${num(state.offset + 1)}–${num(state.offset + result.items.length)} / ${num(result.total)} kết quả` : '0 kết quả';
    $('#prev').disabled = state.offset === 0; $('#next').disabled = result.next_offset == null;
    if (!result.items.length) showEmpty(result.empty_reason || 'Không có kết quả ở trang này', result.empty_reason ? 'Không có keyword phù hợp trong dữ liệu đã phân tích.' : 'Hãy quay lại trang trước.'); else $('#empty').hidden = true;
  } catch (err) { if (seq === state.sequence) { renderRows([]); notice(err.message, true); showEmpty('Không tải được kết quả', 'Kiểm tra kết nối hoặc thử lại.'); } }
}
function detailField(container, label, value, full = false) { const node = el('div', null, `detail-item${full ? ' full' : ''}`); node.append(el('small', label), el('div', value)); container.append(node); }
async function openDetail(id) {
  if (!state.dataRunId || !/^[a-f0-9]{64}$/.test(id || '')) return;
  const dialog = $('#detail-dialog'), box = $('#detail-content'); $('#detail-title').textContent = 'Đang tải…'; $('#detail-subtitle').textContent = ''; box.replaceChildren(); dialog.showModal();
  try {
    const d = await api(`/api/runs/${encodeURIComponent(state.dataRunId)}/keywords/${id}`);
    $('#detail-title').textContent = d.display_keyword || d.canonical_keyword || 'Keyword'; $('#detail-subtitle').textContent = `${d.source || '—'} · ${d.project || '—'} · ${dateText(d.observation_date)}`;
    detailField(box, 'Score / trạng thái', `${d.score == null ? 'Chưa đủ dữ liệu' : `${num(d.score)} / 100`} · ${d.status || '—'}`);
    detailField(box, 'Views ≈ / baseline ≈', `${num(d.current_views_ceil)} / ${num(d.baseline)}`);
    detailField(box, 'Ratio / ngày quan sát', `${d.ratio == null ? '—' : `${num(d.ratio)}×`} · ${num(d.observed_baseline_days)} / 7 ngày`);
    detailField(box, 'Thuật toán', d.algorithm_version || '—');
    const components = d.score_components; detailField(box, 'Thành phần score', components ? `Tăng trưởng G: ${num(components.growth)} · Độ phổ biến V: ${num(components.volume)} · Max views ≈ ${num(components.max_views_ceil)}` : 'Chưa đủ dữ liệu để tính.', true);
    const missing = Array.isArray(d.missing_baseline_dates) ? d.missing_baseline_dates.map(x => `${x.date}: ${x.reason}`).join(' · ') : ''; detailField(box, 'Ngày thiếu và lý do', missing || 'Không có ngày thiếu được báo cáo.', true);
    const observations = Array.isArray(d.baseline_observations) ? d.baseline_observations.map(x => `${x.date}: ${num(x.views_ceil)}`).join(' · ') : ''; detailField(box, 'Baseline quan sát ≈', observations || 'Chưa có quan sát.', true);
    const evidence = Array.isArray(d.classification_evidence) ? d.classification_evidence.map(x => `${x.category}: ${x.matched_text} (${x.rule_id})`).join(' · ') : ''; detailField(box, 'Bằng chứng phân loại', evidence || 'Chưa có bằng chứng phân loại.', true);
    detailField(box, 'Quality flags', Array.isArray(d.quality_flags) && d.quality_flags.length ? d.quality_flags.join(' · ') : 'Không có cờ được báo cáo.', true);
    const url = safeEvidenceUrl(d.evidence_url || d.source_url); if (url) { const cell = el('div', null, 'detail-item full'); cell.append(el('small','Nguồn bằng chứng')); const a = el('a', 'Mở trang Wikimedia ↗'); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; cell.append(a); box.append(cell); }
  } catch (err) { $('#detail-title').textContent = 'Không tải được chi tiết'; detailField(box, 'Lỗi', err.message, true); }
}
$('#load-run').addEventListener('click', () => loadRun($('#run-id').value.trim()));
$('#run-id').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadRun(e.currentTarget.value.trim()); });
$('#open-fetch').addEventListener('click', () => { $('#fetch-error').textContent = ''; $('#operator-token').value = ''; $('#fetch-dialog').showModal(); });
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => document.getElementById(button.dataset.close).close()));
$('#fetch-dialog').addEventListener('close', () => { $('#operator-token').value = ''; });
$('#fetch-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (state.submitting) return; state.submitting = true; $('#open-fetch').disabled = true;
  const token = $('#operator-token').value; $('#operator-token').value = ''; $('#fetch-error').textContent = '';
  try { const created = await api('/api/runs', { method:'POST', headers:{ Authorization:`Bearer ${token}` } }); if (!/^[a-f0-9]{32}$/.test(created.run_id || '')) throw new Error('Backend trả run ID không hợp lệ.'); $('#fetch-dialog').close(); await loadRun(created.run_id); }
  catch (err) { $('#fetch-error').textContent = err.message; }
  finally { state.submitting = false; $('#open-fetch').disabled = Boolean(state.run && activeStatuses.has(state.run.status)); }
});
document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => { document.querySelectorAll('.tab').forEach(x => { x.classList.remove('active'); x.setAttribute('aria-pressed','false'); }); button.classList.add('active'); button.setAttribute('aria-pressed','true'); state.category = button.dataset.category; state.offset = 0; loadKeywords(); }));
let searchTimer; $('#search').addEventListener('input', e => { clearTimeout(searchTimer); const value = e.currentTarget.value; searchTimer = setTimeout(() => { state.q = value; state.offset = 0; loadKeywords(); }, 350); });
$('#sort').addEventListener('change', e => { state.sort = e.currentTarget.value; state.offset = 0; loadKeywords(); });
$('#prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); loadKeywords(); });
$('#next').addEventListener('click', () => { if (state.next != null) { state.offset = state.next; loadKeywords(); } });
window.addEventListener('pagehide', () => { clearTimeout(state.poll); clearTimeout(searchTimer); });
api('/api/health').then(() => { $('#connection-dot').classList.add('good'); $('#connection-text').textContent = 'Backend đã kết nối'; }).catch(() => { $('#connection-dot').classList.add('error'); $('#connection-text').textContent = 'Backend chưa kết nối'; notice('Không kết nối được backend. Khởi động server để tiếp tục.', true); });
api('/api/config').then(config => {
  if (config.read_only) {
    $('#open-fetch').hidden = true;
    $('#connection-text').textContent = 'Demo chỉ đọc';
  }
}).catch(() => {});
