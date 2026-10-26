/* 极简 DOM shim：只覆盖 qtg 前端实际用到的 API，用于 Node 端渲染冒烟测试。 */
class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  add(...c) { c.forEach((x) => x && this.set.add(x)); this._sync(); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); this._sync(); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    this._sync();
    return on;
  }
  contains(c) { return this.set.has(c); }
  _sync() { this.el._class = [...this.set].join(' '); }
  toString() { return this.el._class; }
}

class Node2 {
  constructor() { this.children = []; this.parentNode = null; }
  get firstElementChild() { return this.children.find((c) => c.nodeType === 1) || null; }
  get lastElementChild() { return [...this.children].reverse().find((c) => c.nodeType === 1) || null; }
  append(...nodes) {
    for (const n of nodes) {
      if (n === null || n === undefined || n === false) continue;
      const node = typeof n === 'string' ? new TextNode(n) : n;
      if (node instanceof DocumentFragment2) { for (const c of [...node.children]) this.append(c); continue; }
      node.parentNode = this;
      this.children.push(node);
    }
  }
  appendChild(n) { this.append(n); return n; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter((c) => c !== this); this.parentNode = null; } }
}

class TextNode extends Node2 {
  constructor(text) { super(); this.nodeType = 3; this._text = String(text); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
}

class Element extends Node2 {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.attrs = new Map();
    this._class = '';
    this._text = '';
    this.style = new Proxy({}, { set: (o, k, v) => { o[k] = v; return true; }, get: (o, k) => o[k] });
    this.dataset = {};
    this._listeners = new Map();
    this.classList = new ClassList(this);
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
  }
  get className() { return this._class; }
  set className(v) { this._class = String(v || ''); this.classList.set = new Set(this._class.split(/\s+/).filter(Boolean)); }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
    if (k === 'class') this.className = v;
    if (k === 'id') this.id = String(v);
    if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
    if (k === 'hidden') this.hidden = true;
  }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  removeAttribute(k) { this.attrs.delete(k); }
  set id(v) { this.attrs.set('id', String(v)); this._id = String(v); }
  get id() { return this._id || this.attrs.get('id') || ''; }
  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent ?? '').join('');
  }
  set textContent(v) { this.children = []; this._text = String(v ?? ''); }
  set innerHTML(v) { this._html = String(v); this._text = String(v).replace(/<[^>]*>/g, ''); }
  get innerHTML() { return this._html || escText(this.textContent); }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const arr = this._listeners.get(type) || [];
    this._listeners.set(type, arr.filter((f) => f !== fn));
  }
  dispatchEvent(ev) { for (const fn of this._listeners.get(ev.type) || []) fn(ev); return true; }
  matches(sel) {
    if (sel.startsWith('#')) return this.id === sel.slice(1);
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    if (sel === '*') return true;
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if (c.nodeType === 1) { if (matchComplex(c, sel)) out.push(c); walk(c); }
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest() { return null; }
  focus() {}
  blur() {}
  click() { this.dispatchEvent({ type: 'click', currentTarget: this, target: this }); }
  scrollIntoView() {}
  select() {}
  setSelectionRange() {}
}

function matchComplex(node, sel) {
  // 支持 "#id", ".cls", "tag", "tag[attr]" 的简单组合（空格选择器取最后一个）
  const parts = sel.trim().split(/\s+/);
  const last = parts[parts.length - 1];
  const m = last.match(/^([a-zA-Z0-9-]*)((?:[.#][\w-]+)*)$/);
  if (!m) return false;
  const [, tag, rest] = m;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (const piece of rest.match(/[.#][\w-]+/g) || []) {
    if (piece[0] === '#' && node.id !== piece.slice(1)) return false;
    if (piece[0] === '.' && !node.classList.contains(piece.slice(1))) return false;
  }
  return true;
}

function escText(s) { return String(s ?? '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }

class DocumentFragment2 extends Node2 { constructor() { super(); this.nodeType = 11; } }

class Document2 extends Element {
  constructor() {
    super('#document');
    this.documentElement = new Element('html');
    this.hidden = false;
    this._byId = new Map();
  }
  createElement(tag) { return new Element(tag); }
  createElementNS(ns, tag) { const e = new Element(tag); e.ns = ns; return e; }
  createTextNode(t) { return new TextNode(t); }
  createDocumentFragment() { return new DocumentFragment2(); }
  querySelectorAll(sel) {
    const out = super.querySelectorAll(sel);
    if (this.documentElement) {
      if (matchComplex(this.documentElement, sel)) out.unshift(this.documentElement);
      out.push(...this.documentElement.querySelectorAll(sel));
    }
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getElementById(id) { return this.querySelector('#' + id); }
}

export function installDom() {
  const document = new Document2();
  const store = {};
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    location: { hash: '' },
  };
  class CustomEvent {
    constructor(type, opts = {}) { this.type = type; this.detail = opts.detail; }
  }
  globalThis.Node = Node2;
  globalThis.Element = Element;
  globalThis.Text = TextNode;
  globalThis.HTMLElement = Element;
  globalThis.DocumentFragment = DocumentFragment2;
  globalThis.document = document;
  globalThis.window = window;
  globalThis.localStorage = localStorage;
  globalThis.CustomEvent = CustomEvent;
  globalThis.location = window.location;
  try {
    Object.defineProperty(globalThis, 'navigator', {
      value: { clipboard: { writeText: async () => {} } },
      configurable: true, writable: true,
    });
  } catch { /* Node 内置 navigator 只读时忽略 */ }
  return { document, window, localStorage };
}
