'use strict';

const {timingSafeEqual} = require('node:crypto');

const REDIRECT = 'http://127.0.0.1:17388/authorize';
const LIVE_ORIGIN = 'https://www.trae.cn';
const PUBLIC_RESOURCES = new Set([
  'https://www.trae.cn', 'https://lf-cdn.trae.com.cn', 'https://lf-c-flwb.bytetos.com',
]);
const HUMAN_KEYS = new Set(['Tab', 'Shift+Tab', 'Enter', 'Escape', 'Space',
  'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);

class SessionError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function reject(code) { throw new SessionError(code); }
function equal(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length > 256 || b.length > 256) return false;
  const left = Buffer.from(a), right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

/** Experimental finite-action controller. A caller must provide a PRIVATE context.
 * No HTTP listener, arbitrary locator/URL/JS commands, tracing, profile persistence
 * or credential logging. Real human verification uses frames + pointer input.
 */
class AuthorizationSession {
  constructor({context, owner, state, authUrl, now = () => Date.now(), fixtureOrigin, onCallback}) {
    let url;
    try { url = new URL(authUrl); } catch { reject('invalid_bootstrap'); }
    if (typeof owner !== 'string' || !owner || owner.length > 256 ||
        typeof state !== 'string' || !state || state.length > 256 || typeof onCallback !== 'function' ||
        url.href.length > 16384 || [...url.searchParams.keys()].length !== new Set(url.searchParams.keys()).size ||
        !equal(url.searchParams.get('auth_callback_url'), REDIRECT) ||
        !/^[a-f0-9]{32,128}$/i.test(url.searchParams.get('login_trace_id') || '') ||
        url.searchParams.get('code_challenge_method') !== 'S256' ||
        !/^[A-Za-z0-9_-]{43,128}$/.test(url.searchParams.get('code_challenge') || '') ||
        url.username || url.password || url.hash || url.pathname !== '/authorization' ||
        url.origin !== (fixtureOrigin || LIVE_ORIGIN)) reject('invalid_bootstrap');
    if (fixtureOrigin && !/^http:\/\/127\.0\.0\.1:\d+$/.test(fixtureOrigin)) reject('invalid_fixture');
    this.context = context;
    this.owner = owner;
    this.state = state;
    this.url = url;
    this.origin = fixtureOrigin || LIVE_ORIGIN;
    this.trace = url.searchParams.get('login_trace_id');
    this.now = now;
    this.onCallback = onCallback;
    this.expires = now() + 300000;
    this.phase = 'created';
    this.frameId = 0;
    this.pointerDown = false;
    this.smsAfter = 0;
    this.lastFrameAt = -Infinity;
    this.frame = null;
    this.pending = null;
    this.phoneFilled = false;
    this.closed = false;
    this.busy = false;
    this.navigation = 0;
    this.abortTimer = null;
    this.destroyPromise = null;
    this.resourcesPromise = null;
    this.inputBudget = {updatedAt: now(), tokens: 60};
  }

  check(owner, state) {
    if (!equal(owner, this.owner) || !equal(state, this.state)) reject('session_not_found');
    if (this.closed) reject('session_closed');
    if (this.now() >= this.expires) {
      void this.destroy('expired');
      reject('session_expired');
    }
  }

  async start(owner, state) {
    this.check(owner, state);
    if (this.phase !== 'created') reject('already_started');
    this.phase = 'starting';
    this.busy = true;
    this.abortTimer = setTimeout(() => { void this.destroy('expired'); }, Math.max(1, this.expires - this.now()));
    this.abortTimer.unref?.();
    try {
      this.page = await this.context.newPage();
      this.check(owner, state);
      this.page.on('framenavigated', frame => {
        if (frame !== this.page.mainFrame()) return;
        this.navigation += 1;
        this.frame = null;
        void this.releasePointer();
      });
      this.context.on('close', () => {
        if (!this.closed && !['complete', 'unconfirmed'].includes(this.phase)) {
          this.frame = null;
          this.pointerDown = false;
          this.phase = this.pending ? 'unconfirmed' : 'failed';
          this.closed = true;
          clearTimeout(this.abortTimer);
        }
      });
      await this.context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        if (this.closed || this.now() >= this.expires) return route.abort();
        if (url.origin + url.pathname === REDIRECT) {
          const trusted = request.isNavigationRequest() && request.method() === 'GET' &&
            request.frame() === this.page.mainFrame() && new URL(this.page.url()).origin === this.origin;
          const pairs = [...url.searchParams];
          const keys = pairs.map(([key]) => key);
          if (!trusted || this.pending || this.phase === 'complete' || this.closed || this.now() >= this.expires ||
              keys.length !== new Set(keys).size || !equal(url.searchParams.get('loginTraceID'), this.trace) ||
              url.hash || url.username || url.password || url.href.length > 16384) return route.abort();
          if (!['choose', 'phone', 'human'].includes(this.phase)) return route.abort();
          this.phase = 'completing';
          this.frame = null;
          // Never forward a loopback callback to a local listener or logging proxy.
          this.pending = Promise.resolve().then(async () => {
            await route.fulfill({contentType: 'text/html', headers: {'Cache-Control': 'no-store'},
              body: '<!doctype html><title>Authorization returned</title><p>Return to your gateway.</p>'});
            this.check(owner, state);
            return this.onCallback(url.href);
          }).then(async result => {
            if (!this.closed) this.phase = result?.confirmed === true ? 'complete' : 'unconfirmed';
            await this.shutdownBrowser();
          }).catch(async () => {
            if (!this.closed) this.phase = 'unconfirmed';
            await this.shutdownBrowser();
          });
          return this.pending;
        }
        if (!url.username && !url.password && (url.origin === this.origin ||
            (!request.isNavigationRequest() && !this.url.origin.startsWith('http:') && PUBLIC_RESOURCES.has(url.origin)))) {
          return route.continue();
        }
        // Additional login/QR origins must be reviewed from official navigation.
        if (request.isNavigationRequest() && request.frame() === this.page.mainFrame()) {
          this.phase = 'unsupported_origin';
          this.frame = null;
        }
        return route.abort();
      });
      this.page.on('download', download => { void download.cancel().catch(() => {}); });
      this.page.on('crash', () => { void this.destroy('failed'); });
      this.context.on('page', page => { if (page !== this.page) void page.close().catch(() => {}); });
      await this.page.goto(this.url.href, {waitUntil: 'domcontentloaded', timeout: 20000});
      this.check(owner, state);
      if (this.phase === 'starting') this.phase = 'choose';
      return this.status(owner, state);
    } catch (error) {
      await this.destroy('closed');
      if (error instanceof SessionError) throw error;
      reject('official_page_unavailable');
    } finally { this.busy = false; }
  }

  status(owner, state) {
    this.check(owner, state);
    return {phase: this.phase, expiresAt: this.expires, officialOrigin: this.origin,
      qrConfirmed: false, productionReady: false};
  }

  async action(owner, state, kind, value) {
    this.check(owner, state);
    if (!this.page || this.busy || this.pointerDown ||
        !['choose', 'phone', 'human'].includes(this.phase)) reject('action_not_available');
    if (new URL(this.page.url()).origin !== this.origin) reject('origin_changed');
    this.busy = true;
    try {
      this.frame = null;
      if (kind === 'choose_phone') {
        if (!['choose', 'phone', 'human'].includes(this.phase)) reject('wrong_phase');
        this.phase = 'phone';
      }
      else if (kind === 'choose_douyin') {
        if (this.phase !== 'choose') reject('wrong_phase');
        await this.page.getByText('抖音登录', {exact: true}).click({timeout: 3000});
        // Consent, additional origins and actual QR selectors are not yet verified.
        if (['choose', 'phone', 'human'].includes(this.phase) && !this.pending && !this.closed) this.phase = 'human';
      } else if (kind === 'fill_phone') {
        if (this.phase !== 'phone' || typeof value !== 'string' || !/^1[3-9][0-9]{9}$/.test(value)) reject('invalid_phone');
        await this.page.getByPlaceholder('输入手机号', {exact: true}).fill(value, {timeout: 3000});
        this.phoneFilled = true;
      } else if (kind === 'request_sms') {
        if (this.phase !== 'phone' || !this.phoneFilled) reject('wrong_phase');
        if (this.now() < this.smsAfter) reject('sms_cooldown');
        this.smsAfter = this.now() + 60000;
        // This is invoked only from an explicit user click. Official consent or
        // challenges stay intact and are completed through the real frame.
        await this.page.getByText('获取验证码', {exact: true}).click({timeout: 3000});
        if (['choose', 'phone', 'human'].includes(this.phase) && !this.pending && !this.closed) this.phase = 'human';
      } else if (kind === 'fill_code') {
        if (!['phone', 'human'].includes(this.phase) || typeof value !== 'string' || !/^[0-9]{4,8}$/.test(value)) reject('invalid_code');
        await this.page.getByPlaceholder('输入验证码', {exact: true}).fill(value, {timeout: 3000});
        if (['choose', 'phone', 'human'].includes(this.phase) && !this.pending && !this.closed) this.phase = 'human';
      } else reject('unknown_action');
      this.check(owner, state);
      return this.status(owner, state);
    } catch (error) {
      if (error instanceof SessionError) throw error;
      if (!this.closed && !this.pending && this.phase !== 'unsupported_origin') this.phase = 'human';
      reject('official_page_changed');
    } finally { this.busy = false; }
  }

  async capture(owner, state) {
    this.check(owner, state);
    if (!this.page || this.busy || this.pointerDown ||
        !['choose', 'phone', 'human'].includes(this.phase)) reject('frame_not_available');
    if (this.now() - this.lastFrameAt < 500) reject('frame_rate_limited');
    this.lastFrameAt = this.now();
    this.frame = null;
    this.busy = true;
    try {
      const viewport = this.page.viewportSize();
      if (!viewport || viewport.width > 1920 || viewport.height > 1200) reject('invalid_viewport');
      const url = this.page.url();
      const navigation = this.navigation;
      if (new URL(url).origin !== this.origin) reject('origin_changed');
      const jpeg = await this.page.screenshot({type: 'jpeg', quality: 70, timeout: 3000});
      this.check(owner, state);
      const after = this.page.viewportSize();
      if (this.page.url() !== url || navigation !== this.navigation ||
          after?.width !== viewport.width || after?.height !== viewport.height) reject('stale_frame');
      this.frame = {id: ++this.frameId, url, width: viewport.width, height: viewport.height,
        navigation, expiresAt: this.now() + 10000};
      // Full page URLs can contain OAuth state or authorization codes. They stay
      // private; the transport receives only the official origin and frame bounds.
      const {id, width, height, expiresAt} = this.frame;
      return {id, width, height, expiresAt, origin: this.origin, jpeg};
    } catch (error) {
      if (error instanceof SessionError) throw error;
      reject('frame_unavailable');
    } finally { this.busy = false; }
  }

  async input(owner, state, event) {
    this.check(owner, state);
    if (this.busy || !['choose', 'phone', 'human'].includes(this.phase)) reject('input_not_available');
    this.busy = true;
    try {
      const frame = await this.inputFrame(event);
      if (!['down', 'move', 'up'].includes(event.type) || !Number.isFinite(event.x) || !Number.isFinite(event.y) ||
          event.x < 0 || event.x >= frame.width || event.y < 0 || event.y >= frame.height) reject('invalid_input');
      if (event.type === 'down' && this.pointerDown || event.type === 'up' && !this.pointerDown) reject('invalid_pointer_state');
      await this.consumeInput();
      await this.page.mouse.move(event.x, event.y);
      if (event.type === 'down') { this.pointerDown = true; await this.page.mouse.down(); }
      if (event.type === 'up') { this.pointerDown = false; await this.page.mouse.up(); }
      this.check(owner, state);
    } catch (error) {
      await this.releasePointer();
      if (error instanceof SessionError) throw error;
      reject('input_unavailable');
    } finally { this.busy = false; }
    return {ok: true};
  }

  async inputFrame(event) {
    const frame = this.frame;
    const viewport = this.page?.viewportSize();
    if (!event || !frame || event.frameId !== frame.id || this.now() >= frame.expiresAt ||
        this.page.url() !== frame.url || this.navigation !== frame.navigation ||
        viewport?.width !== frame.width || viewport?.height !== frame.height) {
      await this.releasePointer();
      reject('stale_frame');
    }
    return frame;
  }

  async consumeInput() {
    const now = this.now();
    const elapsed = Math.max(0, now - this.inputBudget.updatedAt);
    this.inputBudget.tokens = Math.min(60, this.inputBudget.tokens + elapsed * 30 / 1000);
    this.inputBudget.updatedAt = now;
    if (this.inputBudget.tokens < 1) {
      this.frame = null;
      await this.releasePointer();
      reject('input_rate_limited');
    }
    this.inputBudget.tokens -= 1;
  }

  async humanKey(owner, state, event) {
    this.check(owner, state);
    if (this.busy || this.pointerDown || !['choose', 'phone', 'human'].includes(this.phase)) reject('input_not_available');
    this.busy = true;
    try {
      await this.inputFrame(event);
      if (!HUMAN_KEYS.has(event.key)) reject('invalid_key');
      await this.consumeInput();
      this.frame = null;
      // Fixed accessibility/navigation keys only, never arbitrary text or shortcuts.
      await this.page.keyboard.press(event.key);
      this.check(owner, state);
      return {ok: true};
    } catch (error) {
      if (error instanceof SessionError) throw error;
      reject('input_unavailable');
    } finally { this.busy = false; }
  }

  async scroll(owner, state, event) {
    this.check(owner, state);
    if (this.busy || this.pointerDown || !['choose', 'phone', 'human'].includes(this.phase)) reject('input_not_available');
    this.busy = true;
    try {
      const frame = await this.inputFrame(event);
      if (![event.x, event.y, event.deltaX, event.deltaY].every(Number.isFinite) ||
          event.x < 0 || event.x >= frame.width || event.y < 0 || event.y >= frame.height ||
          Math.abs(event.deltaX) > 600 || Math.abs(event.deltaY) > 600) reject('invalid_scroll');
      await this.consumeInput();
      this.frame = null;
      await this.page.mouse.move(event.x, event.y);
      await this.page.mouse.wheel(event.deltaX, event.deltaY);
      this.check(owner, state);
      return {ok: true};
    } catch (error) {
      if (error instanceof SessionError) throw error;
      reject('input_unavailable');
    } finally { this.busy = false; }
  }

  async close(owner, state) {
    if (!equal(owner, this.owner) || !equal(state, this.state)) reject('session_not_found');
    return this.destroy('closed');
  }

  async cancelInput(owner, state) {
    this.check(owner, state);
    if (this.busy) reject('input_not_available');
    this.busy = true;
    try {
      this.frame = null;
      await this.releasePointer();
      return {ok: true};
    } finally { this.busy = false; }
  }

  async releasePointer() {
    const pressed = this.pointerDown;
    this.pointerDown = false;
    if (pressed && this.page) {
      try { await this.page.mouse.up(); } catch { /* The context may already be gone. */ }
    }
  }

  async destroy(phase) {
    if (this.destroyPromise) return this.destroyPromise;
    this.closed = true;
    clearTimeout(this.abortTimer);
    this.frame = null;
    this.phase = phase;
    this.destroyPromise = this.shutdownBrowser();
    return this.destroyPromise;
  }

  async shutdownBrowser() {
    if (this.resourcesPromise) return this.resourcesPromise;
    this.frame = null;
    this.resourcesPromise = (async () => {
      await this.releasePointer();
      try { await this.context.close(); } catch { /* Shutdown is idempotent. */ }
    })();
    return this.resourcesPromise;
  }
}

module.exports = {AuthorizationSession, SessionError};
