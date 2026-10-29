'use strict';

const {randomBytes, timingSafeEqual} = require('node:crypto');
const {performance} = require('node:perf_hooks');
const {AuthorizationSession, SessionError} = require('./session.cjs');

const CONTEXT_OPTIONS = Object.freeze({
  viewport: Object.freeze({width: 520, height: 760}),
  serviceWorkers: 'block', acceptDownloads: false,
});
const ACTIONS = new Set(['choose_phone', 'choose_douyin', 'fill_phone', 'request_sms', 'fill_code']);
const KEYS = new Set(['Tab', 'Shift+Tab', 'Enter', 'Escape', 'Space', 'Backspace',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);
const PHASES = new Set(['created', 'starting', 'choose', 'phone', 'human', 'completing',
  'complete', 'unconfirmed', 'unsupported_origin', 'failed', 'closed', 'expired']);
const SESSION_ERRORS = new Set(['invalid_bootstrap', 'invalid_fixture', 'session_not_found',
  'session_closed', 'session_expired', 'already_started', 'official_page_unavailable',
  'action_not_available', 'origin_changed', 'wrong_phase', 'invalid_phone', 'sms_cooldown',
  'invalid_code', 'unknown_action', 'official_page_changed', 'frame_not_available',
  'frame_rate_limited', 'invalid_viewport', 'stale_frame', 'frame_unavailable',
  'input_not_available', 'invalid_input', 'invalid_pointer_state', 'input_unavailable',
  'input_rate_limited', 'invalid_key', 'invalid_scroll']);
const BROKER_ERRORS = new Set(['invalid_command', 'invalid_bootstrap', 'invalid_phone', 'invalid_code',
  'invalid_input', 'invalid_key', 'invalid_scroll', 'operation_timeout', 'worker_failed',
  'capacity_exceeded', 'owner_capacity_exceeded', 'invalid_context', 'worker_closed',
  'session_not_found', 'session_expired', 'shutdown_unconfirmed', 'session_closed',
  'session_busy', 'frame_too_large', 'worker_busy']);
const COMMON = ['id', 'command', 'owner', 'state', 'handle'];
const FIELDS = {
  open: ['id', 'command', 'owner', 'state', 'authUrl'],
  status: COMMON, capture: COMMON, cancel: COMMON, close: COMMON,
  action: [...COMMON, 'kind', 'value'],
  pointer: [...COMMON, 'frameId', 'type', 'x', 'y'],
  key: [...COMMON, 'frameId', 'key'],
  scroll: [...COMMON, 'frameId', 'x', 'y', 'deltaX', 'deltaY'],
};

class BrokerError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code) { throw new BrokerError(code); }
function same(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string') return false;
  const a = Buffer.from(left), b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}
function bound(value, fallback, min, max) {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result < min || result > max) fail('invalid_configuration');
  return result;
}
function validOrigin(origin) {
  try {
    const url = new URL(origin);
    return /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(origin) &&
      url.origin === origin && Number(url.port) <= 65535;
  } catch { return false; }
}
function requestId(request) {
  return Number.isSafeInteger(request?.id) && request.id >= 0 && request.id <= 2147483647 ? request.id : null;
}
function validate(request) {
  if (!request || Object.getPrototypeOf(request) !== Object.prototype || requestId(request) === null ||
      typeof request.command !== 'string' ||
      !Object.hasOwn(FIELDS, request.command)) fail('invalid_command');
  const fields = FIELDS[request.command];
  if (Object.keys(request).some(key => !fields.includes(key)) ||
      fields.filter(key => key !== 'value').some(key => !Object.hasOwn(request, key)) ||
      typeof request.owner !== 'string' || !request.owner || request.owner.length > 128 ||
      /[\u0000-\u001f\u007f]/.test(request.owner) ||
      typeof request.state !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(request.state)) fail('invalid_command');
  if (request.command === 'open') {
    if (typeof request.authUrl !== 'string' || request.authUrl.length > 16384) fail('invalid_bootstrap');
    return;
  }
  if (typeof request.handle !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(request.handle)) fail('invalid_command');
  if (request.command === 'action') {
    if (!ACTIONS.has(request.kind)) fail('invalid_command');
    if (request.kind === 'fill_phone') {
      if (typeof request.value !== 'string' || !/^1[3-9][0-9]{9}$/.test(request.value)) fail('invalid_phone');
    } else if (request.kind === 'fill_code') {
      if (typeof request.value !== 'string' || !/^[0-9]{4,8}$/.test(request.value)) fail('invalid_code');
    } else if (Object.hasOwn(request, 'value')) fail('invalid_command');
  }
  if (['pointer', 'key', 'scroll'].includes(request.command) &&
      (!Number.isSafeInteger(request.frameId) || request.frameId < 1)) fail('invalid_command');
  if (['pointer', 'scroll'].includes(request.command) &&
      (![request.x, request.y].every(Number.isFinite) || request.x < 0 || request.x >= 520 ||
       request.y < 0 || request.y >= 760)) fail('invalid_input');
  if (request.command === 'pointer' && !['down', 'move', 'up'].includes(request.type)) fail('invalid_input');
  if (request.command === 'key' && !KEYS.has(request.key)) fail('invalid_key');
  if (request.command === 'scroll' && (![request.deltaX, request.deltaY].every(Number.isFinite) ||
      Math.abs(request.deltaX) > 600 || Math.abs(request.deltaY) > 600)) fail('invalid_scroll');
}
function deadline(operation, ms) {
  let timer;
  return Promise.race([operation, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new BrokerError('operation_timeout')), ms);
  })]).finally(() => clearTimeout(timer));
}

/** Private, synthetic-only transport. It is not an authenticated Manager API. */
class FixtureBroker {
  constructor({fixtureOrigin, createContext, onCallback = async () => ({confirmed: false}),
    now = () => Date.now(), maxSessions, maxPerOwner, maxInFlight, operationTimeoutMs,
    ttlMs, maxFrameBytes} = {}) {
    if (!validOrigin(fixtureOrigin) || typeof createContext !== 'function' ||
        typeof onCallback !== 'function' || typeof now !== 'function') fail('fixture_runtime_required');
    this.origin = fixtureOrigin;
    this.createContext = createContext;
    this.onCallback = onCallback;
    this.now = now;
    this.maxSessions = bound(maxSessions, 2, 1, 4);
    this.maxPerOwner = bound(maxPerOwner, 1, 1, this.maxSessions);
    this.maxInFlight = bound(maxInFlight, 4, 1, 8);
    this.operationTimeoutMs = bound(operationTimeoutMs, 20000, 10, 20000);
    this.ttlMs = bound(ttlMs, 300000, 10, 300000);
    this.maxFrameBytes = bound(maxFrameBytes, 1048576, 128, 1048576);
    this.entries = new Map();
    this.inFlight = 0;
    this.closed = false;
  }

  stats() {
    return {sessions: this.entries.size, inFlight: this.inFlight,
      mode: 'synthetic_fixture', productionReady: false};
  }

  _status(session, owner, state, expiresAt = Infinity) {
    const status = session.status(owner, state);
    if (!PHASES.has(status.phase) || !Number.isSafeInteger(status.expiresAt) ||
        status.officialOrigin !== this.origin) fail('worker_failed');
    return {phase: status.phase, expiresAt: Math.min(status.expiresAt, expiresAt),
      officialOrigin: this.origin, qrConfirmed: false, productionReady: false};
  }

  _terminal(entry) {
    if (this.now() >= entry.expiresAt) { void this._retire(entry); return; }
    if (entry.cancelled && entry.contextClosed && !entry.creating) {
      clearTimeout(entry.timer);
      this.entries.delete(entry.handle);
      return;
    }
    if (!entry.session) return;
    if (['complete', 'unconfirmed'].includes(entry.session.phase)) {
      if (entry.contextClosed) {
        entry.terminalStatus = this._status(entry.session, entry.owner, entry.state, entry.expiresAt);
        entry.session = null;
        entry.context = null;
      }
      if (!entry.terminalAt) {
        entry.terminalAt = this.now() + 10000;
        clearTimeout(entry.timer);
        entry.timer = setTimeout(() => { void this._retire(entry); }, 10000);
        entry.timer.unref?.();
      }
    } else if (entry.session.closed || entry.contextClosed) void this._retire(entry);
  }

  async _retire(entry) {
    entry.cancelled = true;
    clearTimeout(entry.timer);
    if (entry.creating) return;
    if (!entry.retiring) {
      // Do not free a capacity slot until its context actually closes.
      entry.retiring = (async () => {
        try {
          if (entry.session) await entry.session.destroy('closed');
          else if (entry.context) await entry.context.close();
        } catch { /* The slot remains quarantined if shutdown failed. */ return; }
        if (!entry.context || entry.contextClosed) this.entries.delete(entry.handle);
      })();
    }
    return entry.retiring;
  }

  async _open(request) {
    const {owner, state, authUrl} = request;
    const until = performance.now() + this.operationTimeoutMs;
    const remaining = () => {
      const ms = Math.ceil(until - performance.now());
      if (ms <= 0) fail('operation_timeout');
      return ms;
    };
    // Validate the entire bootstrap before reserving a slot or launching anything.
    let url;
    try { url = new URL(authUrl); } catch { fail('invalid_bootstrap'); }
    if (url.origin !== this.origin || [...url.searchParams.keys()].some(key =>
      !['auth_callback_url', 'login_trace_id', 'code_challenge_method', 'code_challenge'].includes(key))) {
      fail('invalid_bootstrap');
    }
    new AuthorizationSession({context: null, owner, state, authUrl, now: this.now,
      fixtureOrigin: this.origin, onCallback() {}});
    if (this.entries.size >= this.maxSessions) fail('capacity_exceeded');
    if ([...this.entries.values()].filter(entry => same(entry.owner, owner)).length >= this.maxPerOwner) {
      fail('owner_capacity_exceeded');
    }
    const entry = {handle: randomBytes(32).toString('base64url'), owner, state,
      expiresAt: this.now() + this.ttlMs, creating: true, cancelled: false};
    this.entries.set(entry.handle, entry);
    entry.timer = setTimeout(() => { void this._retire(entry); }, this.ttlMs);
    entry.timer.unref?.();
    const creation = Promise.resolve().then(() => this.createContext({
      ...CONTEXT_OPTIONS, viewport: {...CONTEXT_OPTIONS.viewport},
    })).then(async context => {
      entry.context = context;
      entry.creating = false;
      if (!context || typeof context.newPage !== 'function' || typeof context.close !== 'function' ||
          typeof context.on !== 'function') fail('invalid_context');
      context.on('close', () => {
        entry.contextClosed = true;
        queueMicrotask(() => this._terminal(entry));
      });
      if (this.closed || entry.cancelled || this.now() >= entry.expiresAt) {
        await this._retire(entry);
        fail('worker_closed');
      }
      return context;
    }, error => {
      entry.creating = false;
      clearTimeout(entry.timer);
      this.entries.delete(entry.handle);
      throw error;
    });
    try {
      const context = await deadline(creation, remaining());
      const startTimeout = remaining();
      entry.session = new AuthorizationSession({context, owner, state, authUrl, now: this.now,
        fixtureOrigin: this.origin, onCallback: callback => {
          if (this.closed || entry.cancelled || this.now() >= entry.expiresAt) fail('worker_closed');
          return this.onCallback({owner, state, handle: entry.handle, callback});
        }});
      await deadline(entry.session.start(owner, state), startTimeout);
      if (this.closed || entry.cancelled || this.now() >= entry.expiresAt) fail('worker_closed');
      return {handle: entry.handle,
        ...(entry.terminalStatus || this._status(entry.session, owner, state, entry.expiresAt)),
        mode: 'synthetic_fixture'};
    } catch (error) {
      void this._retire(entry);
      throw error;
    }
  }

  async _dispatch(request) {
    if (request.command === 'open') {
      return this._open(request);
    }
    const entry = this.entries.get(request.handle);
    if (!entry || !same(entry.owner, request.owner) || !same(entry.state, request.state)) fail('session_not_found');
    if (entry.cancelled || this.now() >= entry.expiresAt ||
        entry.terminalAt && this.now() >= entry.terminalAt) {
      void this._retire(entry);
      fail('session_expired');
    }
    const {session} = entry, {owner, state, command} = request;
    if (command === 'close') {
      await deadline(this._retire(entry), this.operationTimeoutMs);
      if (entry.context && !entry.contextClosed) fail('shutdown_unconfirmed');
      return {closed: true};
    }
    if (command === 'status' && entry.terminalStatus) return {...entry.terminalStatus};
    if (!session) fail(entry.terminalStatus ? 'session_closed' : 'session_not_found');
    if (command === 'status') return this._status(session, owner, state, entry.expiresAt);
    if (entry.busy) fail('session_busy');
    entry.busy = true;
    try {
      const operation = (async () => {
        if (command === 'action') {
          await session.action(owner, state, request.kind, request.value);
          return this._status(session, owner, state, entry.expiresAt);
        }
        if (command === 'capture') {
          const frame = await session.capture(owner, state);
          if (!Buffer.isBuffer(frame.jpeg) || frame.jpeg.length > this.maxFrameBytes ||
              frame.origin !== this.origin || !Number.isSafeInteger(frame.id) || frame.id < 1 ||
              !Number.isSafeInteger(frame.expiresAt) || !Number.isSafeInteger(frame.width) ||
              !Number.isSafeInteger(frame.height) || frame.width < 1 || frame.width > 520 ||
              frame.height < 1 || frame.height > 760) fail('frame_too_large');
          return {id: frame.id, width: frame.width, height: frame.height, expiresAt: frame.expiresAt,
            origin: this.origin, mime: 'image/jpeg', jpegBase64: frame.jpeg.toString('base64')};
        }
        if (command === 'pointer') {
          await session.input(owner, state, {frameId: request.frameId, type: request.type, x: request.x, y: request.y});
        } else if (command === 'key') {
          await session.humanKey(owner, state, {frameId: request.frameId, key: request.key});
        } else if (command === 'scroll') {
          await session.scroll(owner, state, {frameId: request.frameId, x: request.x, y: request.y,
            deltaX: request.deltaX, deltaY: request.deltaY});
        } else if (command === 'cancel') await session.cancelInput(owner, state);
        return {accepted: true};
      })();
      return await deadline(operation, this.operationTimeoutMs);
    } catch (error) {
      if (error instanceof BrokerError && ['operation_timeout', 'frame_too_large'].includes(error.code)) {
        void this._retire(entry);
      }
      throw error;
    } finally {
      entry.busy = false;
      this._terminal(entry);
    }
  }

  async execute(request) {
    const id = requestId(request);
    let entered = false;
    try {
      validate(request);
      if (this.closed) fail('worker_closed');
      if (this.inFlight >= this.maxInFlight) fail('worker_busy');
      this.inFlight += 1;
      entered = true;
      const data = await this._dispatch(request);
      return {id, ok: true, data};
    } catch (error) {
      const code = error instanceof BrokerError && BROKER_ERRORS.has(error.code) ||
        error instanceof SessionError && SESSION_ERRORS.has(error.code)
        ? error.code : 'worker_failed';
      return {id, ok: false, error: {code}};
    } finally { if (entered) this.inFlight -= 1; }
  }

  async shutdown() {
    this.closed = true;
    const pending = [...this.entries.values()].map(entry => this._retire(entry));
    try { await deadline(Promise.allSettled(pending), this.operationTimeoutMs); }
    catch { /* A process supervisor is required for an unresponsive browser. */ }
  }
}

module.exports = {FixtureBroker, BrokerError};
