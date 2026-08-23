import fs from "node:fs";
import vm from "node:vm";

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(name) {
    this.values.add(name);
  }

  toggle(name, force) {
    if (force) this.values.add(name);
    else this.values.delete(name);
  }
}

class FakeElement {
  constructor() {
    this.innerHTML = "";
    this.textContent = "";
    this.classList = new FakeClassList();
    this.style = {
      values: new Map(),
      setProperty: (name, value) => {
        this.style.values.set(name, value);
      },
    };
  }
}

const elements = new Map(
  ["caption", "confirmedCaption", "pendingCaption", "status"]
    .map((id) => [id, new FakeElement()])
);
const documentElement = new FakeElement();
const body = new FakeElement();

const document = {
  body,
  documentElement,
  getElementById(id) {
    return elements.get(id);
  },
};

const config = {
  caption: {
    ko_size: 34,
    max_lines: 8,
    prev_opacity: 0.55,
    just_chatting_mode: true,
    just_chatting_min_line_hold_sec: 0.05,
    history_max_items: 1000,
  },
  streaming: {
    confirmed_hold_sec: 6,
    pending_stale_sec: 3,
  },
  server: {
    port: 8765,
  },
};

class FakeWebSocket {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
    if (name === "open") setTimeout(callback, 0);
  }
}

const context = vm.createContext({
  document,
  location: {
    search: "?preview=0",
    protocol: "file:",
    origin: "null",
    hostname: "127.0.0.1",
  },
  URL,
  URLSearchParams,
  WebSocket: FakeWebSocket,
  fetch: async () => ({
    ok: true,
    json: async () => config,
  }),
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {},
  Date,
  Math,
  Number,
  String,
  Boolean,
  JSON,
  Set,
  Map,
  Array,
});

const html = fs.readFileSync("overlay/caption.html", "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/i);
if (!match) throw new Error("caption script not found");
vm.runInContext(match[1], context);
await new Promise((resolve) => setTimeout(resolve, 20));

for (let index = 0; index < 10; index += 1) {
  const message = {
    type: "caption_confirmed",
    text: "확정 자막 " + index,
    audio_offset_sec: index,
    metadata: { split_index: index },
  };
  vm.runInContext(
    "applyMessage(" + JSON.stringify(message) + ")",
    context
  );
}

await new Promise((resolve) => setTimeout(resolve, 25));
const earlyHistory = vm.runInContext(
  "confirmedHistory.length",
  context
);
const earlyQueue = vm.runInContext(
  "confirmedQueue.length",
  context
);
if (!(earlyHistory >= 1 && earlyHistory < 10 && earlyQueue > 0)) {
  throw new Error(
    "fast captions were not rate-limited: history="
    + earlyHistory
    + " queue="
    + earlyQueue
  );
}

const firstConfirmedHtml = elements.get("confirmedCaption").innerHTML;
vm.runInContext(
  "applyMessage(" + JSON.stringify({
    type: "caption_pending",
    text: "빠르게 바뀌는 임시 자막",
    audio_offset_sec: 20,
  }) + ")",
  context
);
if (elements.get("confirmedCaption").innerHTML !== firstConfirmedHtml) {
  throw new Error("pending caption rebuilt confirmed caption DOM");
}
if (!elements.get("pendingCaption").innerHTML.includes("임시 자막")) {
  throw new Error("pending caption was not rendered separately");
}

await new Promise((resolve) => setTimeout(resolve, 650));
const finalHistory = vm.runInContext(
  "confirmedHistory.length",
  context
);
const finalQueue = vm.runInContext(
  "confirmedQueue.length",
  context
);
const visibleLines = (
  elements.get("confirmedCaption").innerHTML.match(
    /class="line/g
  ) || []
).length;

if (finalHistory !== 10 || finalQueue !== 0) {
  throw new Error(
    "queued captions did not finish in order: history="
    + finalHistory
    + " queue="
    + finalQueue
  );
}
if (visibleLines !== 8) {
  throw new Error("expected 8 stable visible lines, got " + visibleLines);
}
if (!elements.get("confirmedCaption").innerHTML) {
  throw new Error("confirmed caption stack became blank");
}

const beforeBatch = vm.runInContext("confirmedHistory.length", context);
const sameJobTexts = ["batch one", "batch two", "batch three", "batch two"];
for (let index = 0; index < sameJobTexts.length; index += 1) {
  vm.runInContext(
    "applyMessage(" + JSON.stringify({
      type: "caption_confirmed",
      text: sameJobTexts[index],
      audio_offset_sec: 100,
      metadata: { split_index: index, split_count: 4 },
    }) + ")",
    context
  );
}
const queuedBatch = vm.runInContext("confirmedQueue.length", context);
if (queuedBatch !== 3) {
  throw new Error("same-offset duplicate was not suppressed: " + queuedBatch);
}
await new Promise((resolve) => setTimeout(resolve, 80));
const afterBatch = vm.runInContext("confirmedHistory.length", context);
const afterBatchQueue = vm.runInContext("confirmedQueue.length", context);
if (afterBatch !== beforeBatch + 3 || afterBatchQueue !== 0) {
  throw new Error(
    "same STT batch was not committed atomically: before="
    + beforeBatch
    + " after="
    + afterBatch
    + " queue="
    + afterBatchQueue
  );
}

console.log("caption_overlay_fast_speech_ok");