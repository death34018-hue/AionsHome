import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const userDataDir = process.env.BROWSER_PROFILE_DIR || path.join(rootDir, "browser-profile");
const screenshotDir = path.join(rootDir, "screenshots");

let context;
let page;

export async function getPage() {
  if (page && !page.isClosed()) return page;

  await mkdir(userDataDir, { recursive: true });
  await mkdir(screenshotDir, { recursive: true });

  const launchOptions = {
    headless: process.env.HEADLESS === "1",
    viewport: { width: 1280, height: 900 }
  };

  if (process.env.USE_SYSTEM_CHROME === "1") {
    launchOptions.channel = "chrome";
  }

  context = await chromium.launchPersistentContext(userDataDir, launchOptions);
  page = context.pages()[0] || await context.newPage();
  return page;
}

export async function openUrl(url) {
  const currentPage = await getPage();
  await currentPage.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  return pageState(currentPage);
}

export async function pageState(targetPage = page) {
  if (!targetPage || targetPage.isClosed()) {
    return { open: false };
  }

  return {
    open: true,
    title: await targetPage.title(),
    url: targetPage.url()
  };
}

export async function visibleText() {
  const currentPage = await getPage();
  const text = await currentPage.locator("body").innerText({ timeout: 10000 }).catch(() => "");
  return {
    ...(await pageState(currentPage)),
    text
  };
}

export async function pageHtml() {
  const currentPage = await getPage();
  return {
    ...(await pageState(currentPage)),
    html: await currentPage.content()
  };
}

export async function screenshot() {
  const currentPage = await getPage();
  const filename = `screenshot-${new Date().toISOString().replace(/[:.]/g, "-")}.png`;
  const outputPath = path.join(screenshotDir, filename);
  await currentPage.screenshot({ path: outputPath, fullPage: false });
  return {
    ...(await pageState(currentPage)),
    path: outputPath
  };
}

export async function clickAt(x, y) {
  const currentPage = await getPage();
  await currentPage.mouse.click(x, y);
  return pageState(currentPage);
}

export async function typeText(text) {
  const currentPage = await getPage();
  await currentPage.keyboard.type(text);
  return pageState(currentPage);
}

export async function pressKey(key) {
  const currentPage = await getPage();
  await currentPage.keyboard.press(key);
  return pageState(currentPage);
}

export async function scrollBy(deltaX, deltaY) {
  const currentPage = await getPage();
  await currentPage.mouse.wheel(deltaX, deltaY);
  return pageState(currentPage);
}

export async function closeBrowser() {
  if (context) {
    await context.close();
  }
  context = undefined;
  page = undefined;
  return { closed: true };
}
