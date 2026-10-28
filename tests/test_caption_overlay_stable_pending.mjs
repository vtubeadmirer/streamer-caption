// 정확 우선 engine: its growing line (caption_pending with metadata.stable) is white, never
// blanks out when the same words arrive as a confirmed caption, and gray previews from the
// 기본 engine keep their old behavior.
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

function makeOverlay(captionConfig) {
  const elements = new Map(
    ["caption", "confirmedCaption", "pendingCaption", "status"].map((id) => [id, new FakeElement()])
  );
  const config = {
    caption: { ko_size: 34, max_lines: 2, prev_opacity: 0.55, history_max_items: 1000, ...captionConfig },
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
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/i)[1], context);
  const send = (message) => vm.runInContext("applyMessage(" + JSON.stringify(message) + ")", context);
  return { elements, send, context };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const fail = (message) => { throw new Error(message); };

// --- normal mode: the growing line turns into the confirmed line in one render
{
  const { elements, send } = makeOverlay({ just_chatting_mode: false });
  await sleep(20);
  const pending = elements.get("pendingCaption");
  const confirmed = elements.get("confirmedCaption");
  send({ type: "caption_pending", text: "오늘은", audio_offset_sec: 1.0, metadata: { stable: true } });
  if (!pending.innerHTML.includes("pending stable")) fail("stable line is not marked stable: " + pending.innerHTML);
  send({ type: "caption_pending", text: "오늘은 날씨가", audio_offset_sec: 1.5, metadata: { stable: true } });
  send({ type: "caption_confirmed", text: "오늘은 날씨가", audio_offset_sec: 1.501, metadata: { split_index: 0 } });
  if (!pending.innerHTML.includes("오늘은 날씨가")) fail("the stable line vanished before its confirmed line was shown");
  await sleep(20);
  if (pending.innerHTML) fail("stable line should be replaced after commit: " + pending.innerHTML);
  if (!confirmed.innerHTML.includes("오늘은 날씨가")) fail("confirmed line missing");

  // A confirmed sentence followed at once by the rest of the line: the rest stays.
  send({ type: "caption_pending", text: "좋네요. 그래서", audio_offset_sec: 3.0, metadata: { stable: true } });
  send({ type: "caption_confirmed", text: "좋네요.", audio_offset_sec: 3.001, metadata: { split_index: 0 } });
  send({ type: "caption_pending", text: "그래서", audio_offset_sec: 3.4, metadata: { stable: true } });
  await sleep(20);
  if (!pending.innerHTML.includes("그래서") || pending.innerHTML.includes("좋네요")) {
    fail("remainder of the line was lost: " + pending.innerHTML);
  }
  if (!confirmed.innerHTML.includes("좋네요.")) fail("sentence was not committed");

  // Gray previews (기본 engine) still clear as soon as their confirmed caption arrives.
  send({ type: "caption_pending", text: "회색 미리보기", audio_offset_sec: 10.0 });
  if (pending.innerHTML.includes("stable") || !pending.innerHTML.includes("회색")) fail("gray preview rendering changed");
  send({ type: "caption_confirmed", text: "확정 자막", audio_offset_sec: 10.5, metadata: { split_index: 0 } });
  if (pending.innerHTML) fail("gray preview must clear immediately");
}

// --- just chatting: confirmed lines wait for the reading hold; the stable line stays meanwhile
{
  const { elements, send, context } = makeOverlay({
    just_chatting_mode: true, just_chatting_min_line_hold_sec: 0.3, max_lines: 8,
  });
  await sleep(20);
  const pending = elements.get("pendingCaption");
  send({ type: "caption_confirmed", text: "첫 줄", audio_offset_sec: 1.0, metadata: { split_index: 0 } });
  await sleep(20);
  send({ type: "caption_pending", text: "둘째 줄 자라는 중", audio_offset_sec: 2.0, metadata: { stable: true } });
  send({ type: "caption_confirmed", text: "둘째 줄 자라는 중", audio_offset_sec: 2.001, metadata: { split_index: 0 } });
  await sleep(100);
  if (vm.runInContext("confirmedQueue.length", context) !== 1) fail("expected the second line to wait in the queue");
  if (!pending.innerHTML.includes("둘째 줄")) fail("stable line blanked while its confirmed line waited");
  await sleep(400);
  if (pending.innerHTML) fail("stable line should be gone once committed");
  if (!elements.get("confirmedCaption").innerHTML.includes("둘째 줄 자라는 중")) fail("second line not committed");
}

console.log("caption_overlay_stable_pending_ok");
