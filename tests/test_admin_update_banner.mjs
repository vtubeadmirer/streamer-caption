import fs from "node:fs";
import vm from "node:vm";

// Runs the update-banner block of overlay/admin.html against a fake update server and a fake
// clock. 지금 업데이트 with nothing newer to install must end in a short "이미 최신 버전입니다"
// and then a hidden banner, never a banner stuck on "업데이트 준비 중…". (The CSS half of that
// bug, inline display beating the hidden attribute, is checked by tests/test_admin_hidden_css.py.)

const html = fs.readFileSync("overlay/admin.html", "utf8");
const start = html.indexOf("// ---- update banner");
const end = html.indexOf("// ---- end update banner");
if (start < 0 || end < 0 || end < start) throw new Error("update banner block markers not found in admin.html");
const block = html.slice(start, end);

const IDS = ["updateBanner", "updateBannerText", "updateInstall", "updateRestart", "updateSkip", "updateLater",
  "updateProgress", "updateProgressBar", "appVersion"];

function hiddenInMarkup(id) {
  const tag = html.match(new RegExp(`<[a-z]+\\b[^>]*\\bid="${id}"[^>]*>`));
  if (!tag) throw new Error(`#${id} not found in admin.html`);
  return /\shidden(\s|>|=)/.test(tag[0]);
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = hiddenInMarkup(id);
    this.textContent = "";
    this.title = "";
    this.style = {};
    this.listeners = new Map();
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  async click() {
    const listener = this.listeners.get("click");
    if (!listener) throw new Error(`#${this.id} has no click listener`);
    await listener();
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((ok) => { resolve = ok; });
  return { promise, resolve };
}

function createPage(server) {
  const elements = new Map(IDS.map((id) => [id, new FakeElement(id)]));
  let now = 1_000_000;
  let seq = 0;
  const timers = [];
  const storage = new Map();
  const snapshot = () => JSON.parse(JSON.stringify({
    app: { version: "1.2.2", managed: true },
    managed: true,
    check: server.check,
    install: server.install,
  }));
  const context = vm.createContext({
    document: { getElementById: (id) => elements.get(id) || null },
    sessionStorage: { getItem: (key) => (storage.has(key) ? storage.get(key) : null), setItem: (key, value) => storage.set(key, String(value)) },
    Date: { now: () => now },
    setTimeout: (fn, ms) => { timers.push({ due: now + (ms || 0), seq: seq++, fn }); return seq; },
    setInterval: (fn, ms) => { timers.push({ due: now + ms, seq: seq++, fn, every: ms }); return seq; },
    t: (koText) => koText,
    api: async (path) => {
      if (path !== "/api/update/status") throw new Error("unexpected GET " + path);
      if (server.holdStatus) {
        const held = deferred();
        const reply = snapshot();
        server.held.push(() => held.resolve(reply));
        return held.promise;
      }
      return snapshot();
    },
    postJson: async (path, payload) => {
      if (path === "/api/update/install") {
        server.installClicks += 1;
        server.install = { state: "starting", label: "", done: 0, total: 0, percent: 0 };
        return snapshot();
      }
      if (path === "/api/update/skip") {
        server.check = { ...server.check, available: false, skipped: true, skipped_version: payload.version };
        return snapshot();
      }
      throw new Error("unexpected POST " + path);
    },
  });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  const page = {
    elements,
    storage,
    async load() {
      vm.runInContext(block, context);
      await settle();
    },
    async advance(ms) {
      const target = now + ms;
      for (;;) {
        timers.sort((a, b) => a.due - b.due || a.seq - b.seq);
        const next = timers[0];
        if (!next || next.due > target) break;
        timers.shift();
        now = next.due;
        if (next.every) timers.push({ ...next, due: next.due + next.every, seq: seq++ });
        next.fn();
        await settle();
      }
      now = target;
      await settle();
    },
    settle,
    banner() {
      const visibleButtons = ["updateInstall", "updateRestart", "updateSkip", "updateLater"]
        .filter((id) => !elements.get(id).hidden);
      return {
        hidden: elements.get("updateBanner").hidden,
        text: elements.get("updateBannerText").textContent,
        buttons: visibleButtons,
        progressHidden: elements.get("updateProgress").hidden,
      };
    },
  };
  return page;
}

function expect(condition, message, state) {
  if (!condition) throw new Error(message + (state ? " :: " + JSON.stringify(state) : ""));
}

function newServer(check) {
  return { check, install: { state: "idle" }, installClicks: 0, holdStatus: false, held: [] };
}

// 1. Nothing newer: the banner starts hidden, the click shows progress, then "이미 최신 버전입니다"
//    for about three seconds, then the banner hides (the reported bug left "준비 중…" up forever).
{
  const server = newServer({ state: "done", available: false, current_version: "1.2.2" });
  const page = createPage(server);
  expect(page.elements.get("updateBanner").hidden, "the banner markup must start hidden");
  await page.load();
  expect(page.banner().hidden, "no update: banner must stay hidden after the first status poll", page.banner());

  await page.elements.get("updateInstall").click();
  let state = page.banner();
  expect(!state.hidden && state.text.includes("업데이트 준비 중"), "click must show the preparing line", state);
  expect(state.buttons.length === 0 && !state.progressHidden, "while preparing: no buttons, progress bar shown", state);

  // The server finishes within a second with nothing to install and starts a fresh check.
  server.install = { state: "idle", reason: "nothing_to_install" };
  server.check = { state: "checking", available: false, current_version: "1.2.2" };
  await page.advance(800);
  state = page.banner();
  expect(!state.hidden && state.text === "이미 최신 버전입니다.", "idle after the click must say 이미 최신 버전입니다", state);
  expect(state.buttons.length === 0 && state.progressHidden, "the up-to-date line has no buttons and no progress bar", state);

  server.check = { state: "done", available: false, current_version: "1.2.2" };
  await page.advance(1000);
  state = page.banner();
  expect(!state.hidden && state.text === "이미 최신 버전입니다.", "the up-to-date line stays up for a moment", state);

  await page.advance(2500);
  state = page.banner();
  expect(state.hidden, "the banner must hide again a few seconds after 이미 최신 버전입니다", state);
  expect(!state.text.includes("준비 중") || state.hidden, "the preparing line must not stay visible", state);

  // A later status poll (every minute) keeps it hidden.
  await page.advance(60_000);
  expect(page.banner().hidden, "the banner must stay hidden on later polls", page.banner());
}

// 2. A status reply requested before the click must not end the new install early.
{
  const server = newServer({ state: "done", available: false, current_version: "1.2.2" });
  server.holdStatus = true;
  const page = createPage(server);
  await page.load();  // the first poll is still waiting for its (idle) reply
  server.holdStatus = false;
  await page.elements.get("updateInstall").click();
  expect(page.banner().text.includes("업데이트 준비 중"), "click must show the preparing line", page.banner());
  server.held.forEach((release) => release());  // the stale idle reply arrives after the click
  await page.settle();
  let state = page.banner();
  expect(!state.hidden && state.text.includes("업데이트 준비 중"), "a stale idle reply must be ignored", state);
  server.install = { state: "idle", reason: "nothing_to_install" };
  await page.advance(800);
  state = page.banner();
  expect(state.text === "이미 최신 버전입니다." && !state.hidden, "the fresh idle reply ends the install", state);
  await page.advance(3100);
  expect(page.banner().hidden, "the banner hides after the notice", page.banner());
}

// 3. A new version: banner with install / skip / later; 나중에 hides it for the session.
{
  const server = newServer({ state: "done", available: true, latest_version: "1.2.3", current_version: "1.2.2", release_notes: "자막 고침\n둘째 줄" });
  const page = createPage(server);
  await page.load();
  let state = page.banner();
  expect(!state.hidden && state.text.includes("1.2.3") && state.text.includes("자막 고침") && !state.text.includes("둘째 줄"),
    "an available update shows the version and the first release-note line", state);
  expect(JSON.stringify(state.buttons) === JSON.stringify(["updateInstall", "updateSkip", "updateLater"]),
    "an available update offers install, skip and later", state);
  await page.elements.get("updateLater").click();
  expect(page.banner().hidden, "나중에 must hide the banner", page.banner());
  expect(page.storage.get("streamerCaptionUpdateLater") === "1", "나중에 is remembered for the session");
  await page.advance(60_000);
  expect(page.banner().hidden, "나중에 keeps the banner hidden on later polls", page.banner());
}

// 4. A failed install says so and offers a retry, without the up-to-date line.
{
  const server = newServer({ state: "done", available: true, latest_version: "1.2.3", current_version: "1.2.2" });
  const page = createPage(server);
  await page.load();
  await page.elements.get("updateInstall").click();
  server.install = { state: "failed", error: "RuntimeError: download interrupted" };
  await page.advance(800);
  let state = page.banner();
  expect(!state.hidden && state.text.includes("업데이트에 실패했습니다") && state.text.includes("download interrupted"),
    "a failed install must say so", state);
  expect(JSON.stringify(state.buttons) === JSON.stringify(["updateInstall", "updateLater"]), "failure offers retry and later", state);
  await page.advance(4000);
  state = page.banner();
  expect(!state.text.includes("이미 최신 버전"), "a failure is not reported as up to date", state);
}

// 5. A finished install offers the restart button.
{
  const server = newServer({ state: "done", available: true, latest_version: "1.2.3", current_version: "1.2.2" });
  const page = createPage(server);
  await page.load();
  await page.elements.get("updateInstall").click();
  server.install = { state: "downloading", label: "app", done: 1048576, total: 4194304, percent: 25 };
  await page.advance(800);
  let state = page.banner();
  expect(state.text.includes("받는 중") && !state.progressHidden, "download progress is shown", state);
  expect(page.elements.get("updateProgressBar").style.width === "25%", "the progress bar follows percent");
  server.install = { state: "done", current_version: "1.2.3", runtime_pending: false, percent: 100 };
  await page.advance(1000);
  state = page.banner();
  expect(!state.hidden && state.text.includes("설치 완료") && JSON.stringify(state.buttons) === JSON.stringify(["updateRestart", "updateLater"]),
    "a finished install offers restart", state);
  expect(state.progressHidden, "the progress bar hides once the install is done", state);
}

console.log("admin update banner ok");
