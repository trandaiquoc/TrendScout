import { ApiError, getJson, requestJson } from './api.js';

const TERMINAL = new Set(['COMPLETED', 'PARTIAL', 'FAILED', 'ABORTED', 'EXPIRED']);
const POLL_INTERVAL_MS = 3000;
const MAX_POLLS_PER_ATTEMPT = 110;
const MAX_STATUS_POLLS_PER_FETCH = 110;
const MAX_AUTOMATIC_CONTINUATIONS = 3;
const UNCERTAIN_SUBMISSION_RETRY_MS = 2000;
const STALLED_QUEUE_DISPATCH_MS = 6000;
const WIKIMEDIA_SOURCE_ID = 'wikimedia_pageviews_top_per_country';
const YOUTUBE_SOURCE_ID = 'youtube_video_search';
const YOUTUBE_POLICY_VERSION_HEADER = 'X-YouTube-Policy-Version';

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
  'auto-resuming': 'fetch.auto-resuming-local-live',
  'retry-wait': 'fetch.retry-wait-local-live',
};
const VERCEL_SHARED_LIVE_MESSAGE_KEYS = {
  submitting: 'fetch.submitting-vercel-live',
  queued: 'fetch.queued-vercel-live',
  running: 'fetch.running-vercel-live',
  partial: 'fetch.partial-vercel-live',
  completed: 'fetch.completed-vercel-live',
  failed: 'fetch.failed-vercel-live',
  'auto-resuming': 'fetch.auto-resuming-vercel-live',
  'retry-wait': 'fetch.retry-wait-vercel-live',
};
const WIKIMEDIA_HISTORY_MESSAGE_KEYS = {
  submitting: 'fetch.history-submitting-live',
  queued: 'fetch.history-queued-live',
  running: 'fetch.history-running-live',
  partial: 'fetch.history-partial-live',
  completed: 'fetch.history-completed-live',
  failed: 'fetch.history-failed-live',
};

export function liveFetchMessageKey(kind, requestKind = 'latest', sourceIds = [], runtimeScope = 'LOCAL') {
  if (requestKind === 'history') {
    return WIKIMEDIA_HISTORY_MESSAGE_KEYS[kind] || null;
  }
  const wikimediaOnly = Array.isArray(sourceIds)
    && sourceIds.length === 1 && sourceIds[0] === WIKIMEDIA_SOURCE_ID;
  const messages = wikimediaOnly ? WIKIMEDIA_LIVE_MESSAGE_KEYS
    : runtimeScope === 'VERCEL_SHARED_LIVE'
      ? VERCEL_SHARED_LIVE_MESSAGE_KEYS : LOCAL_LIVE_MESSAGE_KEYS;
  return messages[kind] || null;
}

function isUncertainSubmissionError(error) {
  return error instanceof ApiError
    && (error.status === null || [408, 425, 500, 502, 503, 504].includes(error.status));
}

function submissionRetryDelay(error) {
  const retryAfter = Number(error?.retryAfterSeconds);
  return Number.isFinite(retryAfter) && retryAfter > 0
    ? Math.min(Math.max(retryAfter * 1000, UNCERTAIN_SUBMISSION_RETRY_MS), 10_000)
    : UNCERTAIN_SUBMISSION_RETRY_MS;
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
  constructor({ onState, onTerminal, locale, historyDays = null, sourceIds = null,
    youtubeSelected = () => false, youtubePolicyVersion = () => null }) {
    if (historyDays !== null && (!Number.isInteger(historyDays) || historyDays < 1 || historyDays > 2)) {
      throw new RangeError('Historical Fetch is limited to one or two days.');
    }
    this.onState = onState;
    this.onTerminal = onTerminal;
    this.locale = locale;
    this.historyDays = historyDays;
    this.sourceIds = sourceIds;
    this.youtubeSelected = youtubeSelected;
    this.youtubePolicyVersion = youtubePolicyVersion;
    this.requestKind = historyDays === null ? 'latest' : 'history';
    this.key = null;
    this.fetchId = null;
    this.requestBody = null;
    this.activeYoutubePolicyVersion = null;
    this.includesYouTube = false;
    this.resumeNeeded = false;
    this.busy = false;
    this.stopped = false;
    this.retryTimer = null;
    this.automaticContinuations = 0;
    this.statusPolls = 0;
    this.lastAutomaticDispatchAt = 0;
  }

  async activate() {
    if (this.busy || this.stopped) {
      return;
    }
    if (this.fetchId && !this.resumeNeeded) {
      await this.poll();
      return;
    }
    if (this.resumeNeeded) this.fetchId = null;
    this.busy = true;
    this.emit({ kind: 'submitting', busy: true });
    try {
      if (!this.requestBody) {
        const selectedSources = this.historyDays === null && typeof this.sourceIds === 'function'
          ? this.sourceIds() : null;
        this.includesYouTube = Array.isArray(selectedSources)
          ? selectedSources.includes(YOUTUBE_SOURCE_ID) : this.youtubeSelected();
        const policyVersion = this.includesYouTube
          ? this.activeYoutubePolicyVersion || this.youtubePolicyVersion() : null;
        if (this.includesYouTube && !policyVersion) {
          const error = new Error('YouTube policy agreement is required before Fetch.');
          error.code = 'YOUTUBE_POLICY_CONSENT_REQUIRED';
          throw error;
        }
        if (!this.key) {
          this.key = newIdempotencyKey();
          this.automaticContinuations = 0;
          this.statusPolls = 0;
          this.lastAutomaticDispatchAt = 0;
        }
        this.activeYoutubePolicyVersion = this.includesYouTube ? policyVersion : null;
        this.requestBody = this.historyDays !== null
          ? { days: this.historyDays }
          : Array.isArray(selectedSources) && selectedSources.length
            ? { sources: selectedSources } : {};
      }
      if (this.includesYouTube && !this.activeYoutubePolicyVersion) {
        const error = new Error('YouTube policy agreement is required before Fetch.');
        error.code = 'YOUTUBE_POLICY_CONSENT_REQUIRED';
        throw error;
      }
      if (!this.key) {
        this.key = newIdempotencyKey();
        this.automaticContinuations = 0;
        this.statusPolls = 0;
        this.lastAutomaticDispatchAt = 0;
      }
      const headers = { 'Idempotency-Key': this.key };
      if (this.activeYoutubePolicyVersion) {
        headers[YOUTUBE_POLICY_VERSION_HEADER] = this.activeYoutubePolicyVersion;
      }
      let accepted;
      while (!accepted) {
        try {
          accepted = await requestJson(this.historyDays === null
            ? '/api/v1/fetches' : '/api/v1/fetches/history', {
            method: 'POST',
            body: this.requestBody,
            headers,
            locale: this.locale(),
          });
        } catch (error) {
          if (!isUncertainSubmissionError(error)
              || this.automaticContinuations >= MAX_AUTOMATIC_CONTINUATIONS) throw error;
          this.automaticContinuations += 1;
          this.emit({ kind: 'submitting', busy: true, error });
          await wait(submissionRetryDelay(error));
          if (this.stopped) {
            this.busy = false;
            return;
          }
        }
      }
      this.fetchId = accepted.fetch_id;
      this.resumeNeeded = false;
      this.emit({ kind: 'queued', busy: true, dataMode: accepted.data_mode });
      await this.poll();
    } catch (error) {
      this.busy = false;
      this.emit({ kind: this.kindForError(error), busy: false, error });
    }
  }

  async resumeAvailableJob({ sources, requestKind = 'LATEST', historyDays = null } = {}) {
    if (this.busy || this.stopped) return false;
    if (requestKind === 'WIKIMEDIA_HISTORY') {
      if (!Number.isInteger(historyDays) || historyDays < 1 || historyDays > 2) return false;
      this.historyDays = historyDays;
      this.requestKind = 'history';
      this.requestBody = { days: historyDays };
      this.includesYouTube = false;
      this.activeYoutubePolicyVersion = null;
    } else {
      if (!Array.isArray(sources) || !sources.length
          || sources.some((source) => typeof source !== 'string')
          || new Set(sources).size !== sources.length) return false;
      this.historyDays = null;
      this.requestKind = 'latest';
      this.requestBody = { sources: [...sources] };
      this.includesYouTube = sources.includes(YOUTUBE_SOURCE_ID);
      this.activeYoutubePolicyVersion = this.includesYouTube
        ? this.youtubePolicyVersion() : null;
    }
    this.key = null;
    this.fetchId = null;
    this.resumeNeeded = false;
    this.stopped = false;
    this.automaticContinuations = 0;
    this.statusPolls = 0;
    this.lastAutomaticDispatchAt = 0;
    await this.activate();
    return true;
  }

  async poll() {
    this.busy = true;
    let status = null;
    try {
      for (let attempt = 0; attempt < MAX_POLLS_PER_ATTEMPT
          && this.statusPolls < MAX_STATUS_POLLS_PER_FETCH && !this.stopped; attempt += 1) {
        status = await getJson('/api/v1/fetches/' + encodeURIComponent(this.fetchId), {
          headers: this.activeYoutubePolicyVersion
            ? { [YOUTUBE_POLICY_VERSION_HEADER]: this.activeYoutubePolicyVersion } : {},
          locale: this.locale(),
        });
        this.statusPolls += 1;
        this.emit({ kind: 'running', busy: true, fetch: status });
        if (TERMINAL.has(status.status)) {
          this.busy = false;
          this.key = null;
          this.fetchId = null;
          this.requestBody = null;
          this.activeYoutubePolicyVersion = null;
          this.includesYouTube = false;
          this.resumeNeeded = false;
          this.automaticContinuations = 0;
          this.statusPolls = 0;
          this.lastAutomaticDispatchAt = 0;
          this.emit({ kind: status.status.toLowerCase(), busy: false, fetch: status });
          this.onTerminal(status.run_id || null, status);
          return;
        }
        if (status.recovery_available === true && status.status === 'QUEUED') {
          this.resumeNeeded = true;
          if (this.automaticContinuations < MAX_AUTOMATIC_CONTINUATIONS) {
            this.automaticContinuations += 1;
            this.lastAutomaticDispatchAt = Date.now();
            this.emit({ kind: 'auto-resuming', busy: true, fetch: status });
            await wait(UNCERTAIN_SUBMISSION_RETRY_MS);
            if (this.stopped) {
              this.busy = false;
              return;
            }
            this.busy = false;
            await this.activate();
            return;
          }
          this.busy = false;
          this.emit({ kind: 'recovery-available', busy: false, fetch: status });
          return;
        }
        if (status.status === 'QUEUED' && typeof status.retry_at === 'string') {
          const retryAt = Date.parse(status.retry_at);
          if (Number.isFinite(retryAt)) {
            this.resumeNeeded = true;
            if (this.automaticContinuations < MAX_AUTOMATIC_CONTINUATIONS) {
              this.automaticContinuations += 1;
              this.lastAutomaticDispatchAt = Date.now();
              this.emit({ kind: 'retry-wait', busy: true, fetch: status });
              await wait(Math.max(0, retryAt - Date.now()));
              if (this.stopped) {
                this.busy = false;
                return;
              }
              this.busy = false;
              await this.activate();
              return;
            }
            this.busy = false;
            this.emit({ kind: 'recovery-available', busy: false, fetch: status });
            return;
          }
        }
        if (status.status === 'QUEUED' && status.recovery_available !== true
            && typeof status.retry_at !== 'string') {
          const updatedAt = Date.parse(status.updated_at || status.created_at || '');
          const stalledSince = Math.max(
            Number.isFinite(updatedAt) ? updatedAt : 0,
            this.lastAutomaticDispatchAt,
          );
          if (stalledSince > 0 && Date.now() - stalledSince >= STALLED_QUEUE_DISPATCH_MS
              && this.automaticContinuations < MAX_AUTOMATIC_CONTINUATIONS) {
            this.resumeNeeded = true;
            this.automaticContinuations += 1;
            this.lastAutomaticDispatchAt = Date.now();
            this.emit({ kind: 'auto-resuming', busy: true, fetch: status });
            await wait(UNCERTAIN_SUBMISSION_RETRY_MS);
            if (this.stopped) {
              this.busy = false;
              return;
            }
            this.busy = false;
            await this.activate();
            return;
          }
        }
        await wait(POLL_INTERVAL_MS);
      }
      this.busy = false;
      this.resumeNeeded = Boolean(this.fetchId);
      this.emit({ kind: 'poll-limit', busy: false, fetch: status });
    } catch (error) {
      this.busy = false;
      this.resumeNeeded = Boolean(this.fetchId);
      this.emit({ kind: this.kindForError(error), busy: false, error, fetch: status });
    }
  }

  emit(presentation) {
    this.onState({ ...presentation, requestKind: this.requestKind });
  }

  kindForError(error) {
    const code = error?.code || error?.body?.detail?.code || error?.body?.code;
    if (code === 'YOUTUBE_POLICY_NOT_APPROVED') {
      this.fetchId = null;
      this.key = null;
      this.requestBody = null;
      this.activeYoutubePolicyVersion = null;
      this.includesYouTube = false;
      this.resumeNeeded = false;
      return 'policy-not-approved';
    }
    if (code === 'YOUTUBE_POLICY_CONSENT_REQUIRED') {
      this.fetchId = null;
      this.key = null;
      this.requestBody = null;
      this.activeYoutubePolicyVersion = null;
      this.includesYouTube = false;
      this.resumeNeeded = false;
      return 'policy-consent-required';
    }
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
      this.requestBody = null;
      this.resumeNeeded = false;
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
