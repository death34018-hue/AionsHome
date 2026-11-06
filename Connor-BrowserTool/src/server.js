import http from "node:http";
import { appendFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { clickAt, closeBrowser, openUrl, pageHtml, pageState, pressKey, screenshot, scrollBy, typeText, visibleText } from "./browser.js";
import { assertAllowedUrl, assertMutationAllowed, getAllowedHosts, getMutationPolicy } from "./policy.js";

const port = Number(process.env.PORT || 8787);
const host = process.env.HOST || "127.0.0.1";
const serviceVersion = "0.2.0";
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const logDir = path.join(rootDir, "logs");
const actionLogPath = path.join(logDir, "actions.log");

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw.trim()) return {};

  try {
    return JSON.parse(raw);
  } catch {
    const error = new Error("Request body must be valid JSON.");
    error.statusCode = 400;
    throw error;
  }
}

function sendJson(res, statusCode, body) {
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store"
  });
  res.end(JSON.stringify(body, null, 2));
}

function sendBinary(res, statusCode, contentType, body) {
  res.writeHead(statusCode, {
    "content-type": contentType,
    "cache-control": "no-store"
  });
  res.end(body);
}

async function logAction(action, metadata = {}) {
  await mkdir(logDir, { recursive: true });
  const currentState = await pageState().catch(() => ({ open: false }));
  const entry = {
    at: new Date().toISOString(),
    action,
    page: currentState,
    metadata
  };
  await appendFile(actionLogPath, `${JSON.stringify(entry)}\n`, "utf8");
}

async function route(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || `${host}:${port}`}`);

  if (req.method === "GET" && url.pathname === "/health") {
    sendJson(res, 200, {
      ok: true,
      service: "connor-browser-tool",
      version: serviceVersion,
      allowedHosts: getAllowedHosts(),
      mutationPolicy: getMutationPolicy(),
      capabilities: {
        html: true,
        screenshotJson: true,
        screenshotFile: true,
        click: true,
        type: true,
        press: true,
        scroll: true
      }
    });
    return;
  }

  if (req.method === "GET" && url.pathname === "/browser/state") {
    sendJson(res, 200, await pageState());
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/open") {
    const body = await readJson(req);
    const safeUrl = assertAllowedUrl(body.url);
    sendJson(res, 200, await openUrl(safeUrl));
    return;
  }

  if (req.method === "GET" && url.pathname === "/browser/text") {
    sendJson(res, 200, await visibleText());
    return;
  }

  if (req.method === "GET" && url.pathname === "/browser/html") {
    sendJson(res, 200, await pageHtml());
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/screenshot") {
    sendJson(res, 200, await screenshot());
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/screenshot-file") {
    const result = await screenshot();
    const image = await readFile(result.path);
    sendBinary(res, 200, "image/png", image);
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/click") {
    const body = await readJson(req);
    assertMutationAllowed(body, "click");
    const x = Number(body.x);
    const y = Number(body.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      sendJson(res, 400, { ok: false, error: "Click requires numeric x and y." });
      return;
    }
    const result = await clickAt(x, y);
    await logAction("click", { x, y });
    sendJson(res, 200, result);
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/type") {
    const body = await readJson(req);
    assertMutationAllowed(body, "type");
    const text = String(body.text || "");
    const result = await typeText(text);
    await logAction("type", { chars: text.length });
    sendJson(res, 200, result);
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/press") {
    const body = await readJson(req);
    assertMutationAllowed(body, "press");
    const key = String(body.key || "");
    if (!key) {
      sendJson(res, 400, { ok: false, error: "Press requires key." });
      return;
    }
    const result = await pressKey(key);
    await logAction("press", { key });
    sendJson(res, 200, result);
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/scroll") {
    const body = await readJson(req);
    assertMutationAllowed(body, "scroll");
    const deltaX = Number(body.deltaX || 0);
    const deltaY = Number(body.deltaY || 0);
    if (!Number.isFinite(deltaX) || !Number.isFinite(deltaY)) {
      sendJson(res, 400, { ok: false, error: "Scroll requires numeric deltaX and deltaY." });
      return;
    }
    const result = await scrollBy(deltaX, deltaY);
    await logAction("scroll", { deltaX, deltaY });
    sendJson(res, 200, result);
    return;
  }

  if (req.method === "POST" && url.pathname === "/browser/close") {
    sendJson(res, 200, await closeBrowser());
    return;
  }

  sendJson(res, 404, { ok: false, error: "Not found." });
}

const server = http.createServer(async (req, res) => {
  try {
    await route(req, res);
  } catch (error) {
    sendJson(res, error.statusCode || 500, {
      ok: false,
      error: error.message,
      details: error.details
    });
  }
});

server.listen(port, host, () => {
  console.log(`Connor Browser Tool listening on http://${host}:${port}`);
});
