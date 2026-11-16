import { ApiError, getJson, requestJson } from './api.js';

const TERMINAL = new Set(['COMPLETED', 'PARTIAL', 'FAILED', 'ABORTED', 'EXPIRED']);
const POLL_INTERVAL_MS = 1000;
const MAX_POLLS_PER_ATTEMPT = 90;
const WIKIMEDIA_SOURCE_ID = 'wikimedia_pageviews_top_per_country';

const WIKIMEDIA_LIVE_MESSAGE_KEYS = {
  submitting: 'fetch.submitting-live',
  queued: 'fetch.queued-live',
  running: 'fetch.running-live',
  partial: 'fetch.partial-live',
  completed: 'fetch.completed-live',
  failed: 'fetch.failed-live',
};
const LOCAL_LIVE_MESSAGE_KEYS = {
  submitting: 'fetch.submitting-local-live',
  queued: 'fetch.queued-local-live',
  running: 'fetch.running-local-live',
  partial: 'fetch.partial-local-live',
  completed: 'fetch.completed-local-live',
  failed: 'fetch.failed-local-live',
};
const WIKIMEDIA_HISTORY_MESSAGE_KEYS = {
  submitting: 'fetch.history-submitting-live',
  queued: 'fetch.history-queued-live',
  running: 'fetch.history-running-live',
  partial: 'fetch.history-partial-live',
  completed: 'fetch.history-completed-live',
  failed: 'fetch.history-failed-live',
};

export function liveFetchMessageKey(kind, requestKind = 'latest', sourceIds = []) {
  if (requestKind === 'history') {
    return WIKIMEDIA_HISTORY_MESSAGE_KEYS[kind] || null;
  }
  const wikimediaOnly = Array.isArray(sourceIds)
    && sourceIds.length === 1 && sourceIds[0] === WIKIMEDIA_SOURCE_ID;
  const messages = wikimediaOnly ? WIKIMEDIA_LIVE_MESSAGE_KEYS : LOCAL_LIVE_MESSAGE_KEYS;
  return messages[kind] || null;
}

function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error('Secure randomness is unavailable.');
  }
  const bytes = new Uint8Array(24);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

const wait = (milliseconds) => new Promise((resolve) => {
  window.setTimeout(resolve, milliseconds);
});

export class PublicFetchController {
  constructor({ onState, onTerminal, locale, historyDays = null, sourceIds = null }) {
    if (historyDays !== null && (!Number.isInteger(historyDays) || historyDays < 1 || historyDays > 2)) {
      throw new RangeError('Historical Fetch is limited to one or two days.');
    }
    this.onState = onState;
    this.onTerminal = onTerminal;
    this.locale = locale;
    this.historyDays = historyDays;
    this.sourceIds = sourceIds;
    this.requestKind = historyDays === null ? 'latest' : 'history';
    this.key = null;
    this.fetchId = null;
    this.busy = false;
    this.stopped = false;
    this.retryTimer = null;
  }

  async activate() {
    if (this.busy || this.stopped) {
      return;
    }
    if (this.fetchId) {
      await this.poll();
      return;
    }
    this.busy = true;
    this.key = this.key || newIdempotencyKey();
    this.emit({ kind: 'submitting', busy: true });
    try {
      const selectedSources = this.historyDays === null && typeof this.sourceIds === 'function'
        ? this.sourceIds() : null;
      const body = this.historyDays !== null
        ? { days: this.historyDays }
        : Array.isArray(selectedSources) && selectedSources.length
          ? { sources: selectedSources } : {};
      const accepted = await requestJson(this.historyDays === null
        ? '/api/v1/fetches' : '/api/v1/fetches/history', {
        method: 'POST',
        body,
        headers: { 'Idempotency-Key': this.key },
        locale: this.locale(),
      });
      this.fetchId = accepted.fetch_id;
      this.emit({ kind: 'queued', busy: true, dataMode: accepted.data_mode });
      await this.poll();
    } catch (error) {
      this.busy = false;
      this.emit({ kind: this.kindForError(error), busy: false, error });
    }
  }

  async poll() {
    this.busy = true;
    let status = null;
    try {
      for (let attempt = 0; attempt < MAX_POLLS_PER_ATTEMPT && !this.stopped; attempt += 1) {
        status = await getJson('/api/v1/fetches/' + encodeURIComponent(this.fetchId), {
          locale: this.locale(),
        });
        this.emit({ kind: 'running', busy: true, fetch: status });
        if (TERMINAL.has(status.status)) {
          this.busy = false;
          this.key = null;
          this.fetchId = null;
          this.emit({ kind: status.status.toLowerCase(), busy: false, fetch: status });
          this.onTerminal(status.run_id || null, status);
          return;
        }
        await wait(POLL_INTERVAL_MS);
      }
      this.busy = false;
      this.emit({ kind: 'poll-limit', busy: false, fetch: status });
    } catch (error) {
      this.busy = false;
      this.emit({ kind: this.kindForError(error), busy: false, error, fetch: status });
    }
  }

  emit(presentation) {
    this.onState({ ...presentation, requestKind: this.requestKind });
  }

  kindForError(error) {
    if (error instanceof ApiError && error.status === 429) {
      if (error.body?.detail?.code === 'STATUS_POLL_LIMIT'
          || error.body?.code === 'STATUS_POLL_LIMIT') {
        return 'poll-limit';
      }
      return 'rate-limited';
    }
    if (error instanceof ApiError && error.status === 409) {
      return 'busy-error';
    }
    if (error instanceof ApiError && error.status === 404) {
      this.fetchId = null;
      this.key = null;
      return 'expired';
    }
    return this.fetchId ? 'poll-error' : 'retryable';
  }

  dispose() {
    this.stopped = true;
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer);
    }
  }
}
