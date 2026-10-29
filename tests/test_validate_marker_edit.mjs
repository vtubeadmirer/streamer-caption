import fs from "node:fs";
import vm from "node:vm";

// Runs the script of overlay/validate.html against a fake archive API. A row of 최근 표시 opens an
// input with the marker's note (or the caption shown when it was marked); Enter saves it through
// /api/archive/annotation-update, Esc closes it, and the 1.5 s refresh or a new marker must not
// throw away what is being typed. The rows are rendered with innerHTML, so a small parser turns
// that markup into fake elements the test can click and type into.

const html = fs.readFileSync(new URL("../overlay/validate.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/i);
if (!script) throw new Error("validate.html script not found");
const hint = html.match(/<p id="markerHint"([^>]*)>([^<]*)<\/p>/);
if (!hint) throw new Error("#markerHint not found in validate.html");

const IDS = ["statusDot", "statusText", "sessionText", "clock", "captionPreview", "note", "actionGrid",
  "markerCount", "markerHint", "markerList", "toast", "themeButton"];
const VOID = new Set(["input", "br", "img", "hr", "meta", "link"]);
const CAPTION_1 = '샌드위치 "먹었어요" & <끝>';

function decode(text) {
  return text.replace(/&quot;/g, "\"").replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

class FakeClassList {
  constructor(value = "") {
    this.values = new Set(value.split(/\s+/).filter(Boolean));
  }

  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    if (force ?? !this.values.has(name)) this.values.add(name);
    else this.values.delete(name);
  }
}

class FakeElement {
  constructor(doc, tag, attributes = {}) {
    this.doc = doc;
    this.tagName = tag.toUpperCase();
    this.attributes = attributes;
    this.children = [];
    this.parent = null;
    this.listeners = new Map();
    this.classList = new FakeClassList(attributes.class);
    this.hidden = "hidden" in attributes;
    this.disabled = "disabled" in attributes;
    this.value = decode(attributes.value ?? "");
    this.dataset = {};
    for (const [name, value] of Object.entries(attributes)) {
      if (name.startsWith("data-")) {
        this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = decode(value);
      }
    }
    this.text = null;
    this.html = "";
    this.selection = null;
  }

  get textContent() {
    if (this.text !== null) return this.text;
    return this.children.map((child) => (typeof child === "string" ? child : child.textContent)).join("");
  }

  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }

  get innerHTML() { return this.html; }

  set innerHTML(value) {
    this.html = String(value);
    this.text = null;
    this.children = parse(this.doc, this.html, this);
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  matches(selector) {
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    const attribute = selector.match(/^\[([^\]=]+)(?:="([^"]*)")?\]$/);
    if (attribute) {
      if (!(attribute[1] in this.attributes)) return false;
      return attribute[2] === undefined || decode(this.attributes[attribute[1]]) === attribute[2];
    }
    return this.tagName === selector.toUpperCase();
  }

  closest(selector) {
    for (let node = this; node; node = node.parent) {
      if (node.matches(selector)) return node;
    }
    return null;
  }

  querySelector(selector) {
    for (const child of this.children) {
      if (typeof child === "string") continue;
      if (child.matches(selector)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }

  focus() { this.doc.activeElement = this; }

  setSelectionRange(start, end) { this.selection = [start, end]; }
}

function parse(doc, markup, root) {
  const children = [];
  const stack = [{ tag: "", element: root, children }];
  const pattern = /<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/g;
  for (const match of markup.matchAll(pattern)) {
    const top = stack[stack.length - 1];
    if (match[1]) {
      while (stack.length > 1 && stack.pop().tag !== match[1].toLowerCase());
    } else if (match[2]) {
      const attributes = {};
      for (const attribute of match[3].matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) {
        attributes[attribute[1]] = attribute[2] ?? "";
      }
      const element = new FakeElement(doc, match[2], attributes);
      element.parent = top.element;
      top.children.push(element);
      if (!VOID.has(match[2].toLowerCase())) {
        stack.push({ tag: match[2].toLowerCase(), element, children: element.children });
      }
    } else {
      top.children.push(decode(match[4]));
    }
  }
  return children;
}

function newServer() {
  return {
    session: { id: "s1", title: "검증 방송", last_offset_sec: 12 },
    status: { last_caption: "지금 자막", last_audio: { offset_sec: 11, duration_sec: 1 } },
    annotations: [
      { id: 1, session_id: "s1", kind: "wrong_word", offset_sec: 5, note: "", caption_text: CAPTION_1 },
      { id: 2, session_id: "s1", kind: "missed", offset_sec: 8, note: "작게 말함", caption_text: "다른 자막" },
    ],
    nextId: 3,
    failUpdate: false,
    requests: [],
  };
}

function createPage(server) {
  const doc = { activeElement: null };
  const elements = new Map(IDS.map((id) => [id, new FakeElement(doc, id === "note" ? "input" : "div")]));
  elements.get("markerHint").hidden = /\shidden(\s|$)/.test(hint[1]);
  const markButtons = [...html.matchAll(/<button class="mark-button"[^>]*data-kind="([a-z_]+)"/g)]
    .map((match) => new FakeElement(doc, "button", { class: "mark-button", "data-kind": match[1] }));
  const intervals = [];
  const reply = (status, payload) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(payload)) });

  async function fetch(path, options = {}) {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    server.requests.push({ method, path, body });
    if (method === "GET" && path === "/api/status") return reply(200, server.status);
    if (method === "GET" && path === "/api/archive/latest") return reply(200, { session: server.session });
    if (method === "GET" && path.startsWith("/api/archive/annotations?")) {
      const sessionId = new URL(path, "http://127.0.0.1").searchParams.get("session_id");
      return reply(200, { annotations: server.annotations.filter((item) => item.session_id === sessionId) });
    }
    if (method === "POST" && path === "/api/archive/annotation-update") {
      const item = server.annotations.find((entry) => entry.id === body.annotation_id && entry.session_id === body.session_id);
      if (server.failUpdate || !item) return reply(404, { error: "annotation not found" });
      item.note = body.note.trim().slice(0, 1000);
      return reply(200, { annotation: item });
    }
    if (method === "POST" && path === "/api/archive/annotation") {
      const item = { id: server.nextId++, session_id: body.session_id, kind: body.kind, offset_sec: body.offset_sec,
        note: body.note, caption_text: body.caption_text };
      server.annotations.push(item);
      return reply(200, { annotation: item });
    }
    throw new Error(`unexpected ${method} ${path}`);
  }

  const context = vm.createContext({
    document: {
      documentElement: { dataset: {} },
      getElementById: (id) => elements.get(id) || null,
      querySelectorAll: (selector) => {
        if (selector !== ".mark-button") throw new Error("unexpected selector " + selector);
        return markButtons;
      },
    },
    localStorage: { getItem: () => null, setItem: () => {} },
    fetch,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    URL,
    console,
  });

  const settle = async () => {
    for (let round = 0; round < 8; round += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  const list = elements.get("markerList");

  async function dispatch(target, type, init = {}) {
    const event = { type, target, key: "", keyCode: 0, isComposing: false, defaultPrevented: false, ...init,
      preventDefault() { this.defaultPrevented = true; } };
    for (let node = target; node; node = node.parent) {
      for (const listener of node.listeners.get(type) || []) listener(event);
    }
    await settle();
    return event;
  }

  return {
    doc,
    server,
    hint: () => elements.get("markerHint"),
    async load() {
      vm.runInContext(script[1], context);
      await settle();
    },
    async tick() {
      intervals.forEach((fn) => fn());
      await settle();
    },
    rows: () => list.children.filter((child) => typeof child !== "string" && child.dataset.markerId),
    row(id) {
      const found = this.rows().find((child) => child.dataset.markerId === String(id));
      if (!found) throw new Error(`row ${id} not listed: ${list.innerHTML}`);
      return found;
    },
    detail(id) { return this.row(id).querySelector("[data-edit-id]"); },
    input: () => list.querySelector("[data-edit-input]"),
    markButton: (kind) => markButtons.find((button) => button.dataset.kind === kind),
    toast: () => elements.get("toast").textContent,
    updates: () => server.requests.filter((request) => request.path === "/api/archive/annotation-update"),
    click: (target) => dispatch(target, "click"),
    async type(text) {
      const input = list.querySelector("[data-edit-input]");
      if (!input) throw new Error("no open input to type into");
      input.value = text;
      await dispatch(input, "input");
    },
    async key(key, init = {}) {
      const input = list.querySelector("[data-edit-input]");
      if (!input) throw new Error("no open input for " + key);
      return dispatch(input, "keydown", { key, ...init });
    },
  };
}

function expect(condition, message, state) {
  if (!condition) throw new Error(message + (state === undefined ? "" : " :: " + JSON.stringify(state)));
}

const server = newServer();
const page = createPage(server);
expect(page.hint().hidden, "the hint starts hidden (no markers yet)");
await page.load();
expect(JSON.stringify(page.rows().map((row) => row.dataset.markerId)) === '["2","1"]', "newest marker first",
  page.rows().map((row) => row.dataset.markerId));
expect(!page.hint().hidden && hint[2].includes("줄을 누르면 고칠 수 있어요"), "the hint shows with markers", hint[2]);
expect(page.detail(1).textContent === CAPTION_1, "a marker without a note shows its caption", page.detail(1).textContent);

// 1. A click anywhere on the row opens the input with the caption shown then; Enter saves it as the note.
await page.click(page.row(1).querySelector(".marker-time"));
let input = page.input();
expect(input && input.value === CAPTION_1, "the input starts with the caption (quotes and & intact)", input && input.value);
expect(page.doc.activeElement === input && input.selection[0] === CAPTION_1.length, "the caret waits at the end", input.selection);
const fixed = "샌드위치 먹었어요 (샹도웨쭈로 들림)";
await page.type(fixed);
await page.tick();
input = page.input();
expect(input && input.value === fixed, "the 1.5 s refresh keeps the open input and what was typed", input && input.value);
await page.click(page.row(1).querySelector(".marker-kind"));
expect(page.input() && page.input().value === fixed, "another click on the open row keeps the typing", page.input()?.value);
await page.key("Enter", { isComposing: true, keyCode: 229 });
expect(page.updates().length === 0 && page.input(), "the Enter that ends a Korean composition does not save");
const enter = await page.key("Enter");
expect(enter.defaultPrevented, "Enter is handled by the page");
expect(page.updates().length === 1, "Enter saves", page.updates());
expect(JSON.stringify(page.updates()[0].body) === JSON.stringify({ session_id: "s1", annotation_id: 1, note: fixed }),
  "the update names the session, the marker and the new note", page.updates()[0].body);
expect(!page.input() && page.detail(1).textContent === fixed, "the row shows the new note", page.detail(1).textContent);
expect(page.doc.activeElement === page.detail(1), "the focus goes back to the row");
expect(page.toast() === "메모를 고쳤습니다.", "a short confirmation", page.toast());

// 2. Keyboard: the note is a button (Tab reaches it, Enter/Space click it); an existing note is
//    what the input starts with; Esc closes without saving.
expect(page.detail(2).tagName === "BUTTON", "rows are reachable with the keyboard");
await page.click(page.detail(2));
expect(page.input().value === "작게 말함", "the input starts with the existing note", page.input().value);
await page.type("다른 말");
const escape = await page.key("Escape");
expect(escape.defaultPrevented && !page.input(), "Esc closes the input");
expect(page.updates().length === 1 && page.detail(2).textContent === "작게 말함", "Esc saves nothing");
expect(page.doc.activeElement === page.detail(2), "the focus goes back to the row after Esc");

// 3. Enter with nothing changed sends nothing.
await page.click(page.detail(2));
await page.key("Enter");
expect(!page.input() && page.updates().length === 1, "an unchanged note is not sent");

// 4. A failed save keeps the input and the typing; the 저장 button saves with the mouse.
await page.click(page.row(2));
await page.type("새 메모");
server.failUpdate = true;
await page.click(page.row(2).querySelector("[data-save-id]"));
expect(page.updates().length === 2 && page.toast() === "annotation not found", "the error is shown", page.toast());
expect(page.input() && page.input().value === "새 메모", "a failed save keeps what was typed");
server.failUpdate = false;
await page.click(page.row(2).querySelector("[data-save-id]"));
expect(page.updates().length === 3 && !page.input() && page.detail(2).textContent === "새 메모", "저장 saves",
  page.detail(2).textContent);

// 5. Marking a new moment while an input is open redraws the list but keeps the input.
await page.click(page.row(1));
await page.type("고치는 중");
await page.click(page.markButton("hallucination"));
expect(page.rows().length === 3, "the new marker is listed");
expect(page.input() && page.input().value === "고치는 중", "the open input survives the redraw");
await page.click(page.row(1).querySelector("[data-close-id]"));
expect(!page.input() && page.updates().length === 3, "닫기 closes without saving");

// 6. A new broadcast replaces the list and drops the open input.
await page.click(page.row(1));
await page.type("이전 방송 메모");
server.session = { id: "s2", title: "다음 방송", last_offset_sec: 0 };
await page.tick();
expect(!page.input() && page.rows().length === 0 && page.updates().length === 3, "another session closes the input");
expect(page.hint().hidden, "no markers, no hint");

console.log("validate marker edit ok");
