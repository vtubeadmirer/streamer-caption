// 기본 engine gray previews (core/preview.py): the leading characters two previews in a row agreed
// on (metadata.stable_chars, counted in code points) are shown in the caption colour inside the
// gray line; everything else about previews stays as it was.
import fs from "node:fs";
import vm from "node:vm";

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  toggle(name, force) { if (force) this.values.add(name); else this.values.delete(name); }
}

class FakeElement {
  constructor() {
    this.innerHTML = "";
    this.textContent = "";
    this.classList = new FakeClassList();
    this.style = { setProperty: () => {} };
  }
}

function makeOverlay() {
  const elements = new Map(
    ["caption", "confirmedCaption", "pendingCaption", "status"].map((id) => [id, new FakeElement()])
  );
  const config = {
    caption: { ko_size: 34, max_lines: 2, prev_opacity: 0.55, history_max_items: 1000, just_chatting_mode: false },
    streaming: { confirmed_hold_sec: 6, pending_stale_sec: 3 },
    server: { port: 8765 },
  };
  class FakeWebSocket {
    addEventListener(name, callback) { if (name === "open") setTimeout(callback, 0); }
  }
  const context = vm.createContext({
    document: {
      body: new FakeElement(),
      documentElement: new FakeElement(),
      getElementById: (id) => elements.get(id),
    },
    location: { search: "?preview=0", protocol: "file:", origin: "null", hostname: "127.0.0.1" },
    URL, URLSearchParams, WebSocket: FakeWebSocket,
    fetch: async () => ({ ok: true, json: async () => config }),
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, Math, Number, String, Boolean, JSON, Set, Map, Array,
  });
  const html = fs.readFileSync("overlay/caption.html", "utf8");
  if (!/\.line\.pending \.agreed\s*\{[^}]*color:\s*var\(--text\)/.test(html)) {
    throw new Error("agreed preview words need the caption colour");
  }
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/i)[1], context);
  const send = (message) => vm.runInContext("applyMessage(" + JSON.stringify(message) + ")", context);
  return { elements, send };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const fail = (message) => { throw new Error(message); };

const { elements, send } = makeOverlay();
await sleep(20);
const pending = elements.get("pendingCaption");

send({ type: "caption_pending", text: "오늘은 날씨가 좋", audio_offset_sec: 1.7, metadata: { stable_chars: 0 } });
if (pending.innerHTML.includes("agreed")) fail("nothing is agreed on the first preview: " + pending.innerHTML);
if (!pending.innerHTML.includes("오늘은 날씨가 좋")) fail("first preview missing");

send({ type: "caption_pending", text: "오늘은 날씨가 좋네요", audio_offset_sec: 2.2, metadata: { stable_chars: 7 } });
if (!pending.innerHTML.includes('<span class="agreed">오늘은 날씨가</span> 좋네요')) {
  fail("agreed words should be wrapped: " + pending.innerHTML);
}
if (!pending.innerHTML.includes('class="line pending source-')) fail("the line must stay a gray preview");

// The same text with more agreement re-renders (same offset and text, new stable_chars).
send({ type: "caption_pending", text: "오늘은 날씨가 좋네요", audio_offset_sec: 2.2, metadata: { stable_chars: 11 } });
if (!pending.innerHTML.includes('<span class="agreed">오늘은 날씨가 좋네요</span>')) {
  fail("fully agreed preview: " + pending.innerHTML);
}

// Code points, not UTF-16 units: an emoji counts once, as in Python.
send({ type: "caption_pending", text: "좋아요 👍 최고", audio_offset_sec: 2.7, metadata: { stable_chars: 5 } });
if (!pending.innerHTML.includes('<span class="agreed">좋아요 👍</span> 최고')) fail("emoji split: " + pending.innerHTML);

// Out-of-range values are clamped; HTML in the text stays escaped.
send({ type: "caption_pending", text: "<b>굵게</b> 말고", audio_offset_sec: 3.2, metadata: { stable_chars: 999 } });
if (!pending.innerHTML.includes('<span class="agreed">&lt;b&gt;굵게&lt;/b&gt; 말고</span>')) {
  fail("clamp or escape failed: " + pending.innerHTML);
}
send({ type: "caption_pending", text: "음수는 무시", audio_offset_sec: 3.7, metadata: { stable_chars: -3 } });
if (pending.innerHTML.includes("agreed")) fail("negative stable_chars must show nothing agreed");

// A 정확 우선 stable line is white as a whole, without the agreed span.
send({ type: "caption_pending", text: "정확 우선 줄", audio_offset_sec: 4.2, metadata: { stable: true, stable_chars: 2 } });
if (!pending.innerHTML.includes("pending stable") || pending.innerHTML.includes("agreed")) {
  fail("stable lines keep their own style: " + pending.innerHTML);
}

// Gray previews still clear when their confirmed caption arrives.
send({ type: "caption_pending", text: "회색 미리보기 문장", audio_offset_sec: 5.0, metadata: { stable_chars: 6 } });
send({ type: "caption_confirmed", text: "회색 미리보기 문장입니다", audio_offset_sec: 5.5, metadata: { split_index: 0 } });
if (pending.innerHTML) fail("gray preview must clear when the confirmed caption arrives: " + pending.innerHTML);

console.log("caption_overlay_agreed_preview_ok");
