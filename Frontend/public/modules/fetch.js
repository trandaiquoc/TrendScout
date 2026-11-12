import { ApiError, getJson, requestJson } from './api.js';

const TERMINAL = new Set(['COMPLETED', 'PARTIAL', 'FAILED', 'ABORTED', 'EXPIRED']);
const POLL_INTERVAL_MS = 1000;
const MAX_POLLS_PER_ATTEMPT = 90;

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
  constructor({ onState, onTerminal, locale }) {
    this.onState = onState;
    this.onTerminal = onTerminal;
    this.locale = locale;
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
    this.onState({ kind: 'submitting', busy: true });
    try {
      const accepted = await requestJson('/api/v1/fetches', {
        method: 'POST',
        body: {},
        headers: { 'Idempotency-Key': this.key },
        locale: this.locale(),
      });
      this.fetchId = accepted.fetch_id;
      this.onState({ kind: 'queued', busy: true, dataMode: accepted.data_mode });
      await this.poll();
    } catch (error) {
      this.busy = false;
      this.onState({ kind: this.kindForError(error), busy: false, error });
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
        this.onState({ kind: 'running', busy: true, fetch: status });
        if (TERMINAL.has(status.status)) {
          this.busy = false;
          this.key = null;
          this.fetchId = null;
          this.onState({ kind: status.status.toLowerCase(), busy: false, fetch: status });
          this.onTerminal(status.run_id || null, status);
          return;
        }
        await wait(POLL_INTERVAL_MS);
      }
      this.busy = false;
      this.onState({ kind: 'poll-limit', busy: false, fetch: status });
    } catch (error) {
      this.busy = false;
      this.onState({ kind: this.kindForError(error), busy: false, error, fetch: status });
    }
  }

  kindForError(error) {
    if (error instanceof ApiError && error.status === 429) {
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
