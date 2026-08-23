import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { spawn } from "node:child_process";

const url = process.argv[2] || "http://127.0.0.1:8766/history";
const summaryMode = new URL(url).pathname.includes("/summary");
const validationMode = new URL(url).pathname.includes("/validate");
const outputDir = path.resolve(process.argv[3] || "reports/_test_phase23_ui");
const chromePath = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const debugPort = 9300 + (process.pid % 300);
const profileDir = path.join(outputDir, `chrome-profile-${process.pid}`);
fs.mkdirSync(outputDir, { recursive: true });

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getJson(requestPath) {
  return new Promise((resolve, reject) => {
    const request = http.get(
      { host: "127.0.0.1", port: debugPort, path: requestPath },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => (body += chunk));
        response.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(new Error(body || error.message));
          }
        });
      }
    );
    request.on("error", reject);
  });
}

async function waitForTarget() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const targets = await getJson("/json/list");
      const page = targets.find((target) => target.type === "page");
      if (page) return page;
    } catch (_error) {
      // Chrome is still starting.
    }
    await sleep(100);
  }
  throw new Error("Chrome DevTools endpoint did not start");
}

function openCdp(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  socket.nextId = 0;
  socket.pending = new Map();
  socket.errors = [];
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && socket.pending.has(message.id)) {
      const pending = socket.pending.get(message.id);
      socket.pending.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      socket.errors.push(
        message.params?.exceptionDetails?.exception?.description ||
          message.params?.exceptionDetails?.text ||
          "Runtime exception"
      );
    }
    if (message.method === "Log.entryAdded" && message.params?.entry?.level === "error") {
      socket.errors.push(message.params.entry.text);
    }
  });
  return new Promise((resolve, reject) => {
    socket.addEventListener("open", () => resolve(socket), { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
}

function request(socket, method, params = {}) {
  const id = ++socket.nextId;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    socket.pending.set(id, { resolve, reject });
  });
}

async function evaluate(socket, expression) {
  const result = await request(socket, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Evaluation failed");
  }
  return result.result.value;
}

async function waitForContent(socket) {
  const condition = validationMode
    ? `document.readyState === "complete" && document.querySelectorAll(".mark-button").length === 4 && document.querySelector("#markerList")`
    : summaryMode
      ? `document.readyState === "complete" && !document.body.classList.contains("empty") && document.querySelectorAll("#bullets li").length > 0`
      : `document.readyState === "complete" && document.querySelectorAll(".caption-row").length > 0`;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const ready = await evaluate(socket, condition);
    if (ready) return;
    await sleep(100);
  }
  throw new Error("UI content did not render within 10 seconds");
}

const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profileDir}`,
    "about:blank",
  ],
  { stdio: "ignore", windowsHide: true }
);

const results = [];
let socket;
try {
  const target = await waitForTarget();
  socket = await openCdp(target.webSocketDebuggerUrl);
  await request(socket, "Page.enable");
  await request(socket, "Runtime.enable");
  await request(socket, "Log.enable");

  for (const viewport of [
    { name: "desktop", width: 1440, height: 900, mobile: false },
    { name: "mobile", width: 390, height: 844, mobile: true },
  ]) {
    socket.errors.length = 0;
    await request(socket, "Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.mobile,
    });
    await request(socket, "Page.navigate", { url });
    await waitForContent(socket);
    await sleep(500);
    const metricsExpression = validationMode
      ? `(() => ({
          captionRows: 0,
          timelineRows: 0,
          summaryItems: 0,
          topicItems: 0,
          markerButtons: document.querySelectorAll(".mark-button").length,
          markerRows: document.querySelectorAll(".marker-row").length,
          horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
          viewportWidth: window.innerWidth,
          documentWidth: document.documentElement.scrollWidth
        }))()`
      : summaryMode
        ? `(() => ({
            captionRows: 0,
            timelineRows: 0,
            summaryItems: document.querySelectorAll("#bullets li").length,
            topicItems: 0,
            horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
            viewportWidth: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth
          }))()`
        : `(() => ({
            captionRows: document.querySelectorAll(".caption-row").length,
            timelineRows: document.querySelectorAll(".timeline-row").length,
            summaryItems: document.querySelectorAll("#summaryList li").length,
            topicItems: document.querySelectorAll("#topicList li").length,
            horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
            viewportWidth: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth
          }))()`;
    const metrics = await evaluate(socket, metricsExpression);
    if (
      (validationMode && metrics.markerButtons !== 4) ||
      (!validationMode && summaryMode && metrics.summaryItems < 1) ||
      (!validationMode && !summaryMode && (metrics.captionRows < 1 || metrics.timelineRows < 1))
    ) {
      throw new Error(`${viewport.name}: UI content did not render`);
    }
    if (metrics.horizontalOverflow) {
      throw new Error(
        `${viewport.name}: horizontal overflow ${metrics.documentWidth}/${metrics.viewportWidth}`
      );
    }
    if (socket.errors.length) {
      throw new Error(`${viewport.name}: browser errors: ${socket.errors.join(" | ")}`);
    }
    const layout = await request(socket, "Page.getLayoutMetrics");
    const screenshot = await request(socket, "Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
      clip: {
        x: 0,
        y: 0,
        width: viewport.width,
        height: Math.min(Math.ceil(layout.contentSize.height), 5000),
        scale: 1,
      },
    });
    const screenshotPath = path.join(outputDir, `${viewport.name}.png`);
    fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
    results.push({ ...viewport, ...metrics, screenshot: screenshotPath });
  }
} finally {
  if (socket && socket.readyState === WebSocket.OPEN) socket.close();
  chrome.kill();
}

console.log(JSON.stringify(results, null, 2));