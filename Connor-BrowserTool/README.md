# Connor Browser Tool

Standalone Playwright browser tool service for Connor/Aion.

This folder is intentionally separate from the main AionsHome code. It exposes a small local HTTP API that another agent can call later, similar in spirit to an MCP tool server, but simpler.

## What It Can Do

- Open allowlisted pages.
- Read the current page title, URL, and visible text.
- Save screenshots.
- Close the browser session.
- Optionally click or type when explicitly enabled.

## Safety Defaults

- Allowed hosts default to `www.xiaohongshu.com,xiaohongshu.com`.
- Click and type are disabled unless `ALLOW_BROWSER_MUTATIONS=1`.
- In normal mutation mode, click/type/press/scroll calls require `confirm: true`.
- In trusted local-agent mode, set `TRUST_AGENT_ACTIONS=1` so Connor/Aion can operate without adding `confirm: true` to every call.
- Mutation calls are rate-limited by default to 30 actions per minute and at least 1200ms between actions.
- The browser uses its own `browser-profile/` folder, separate from your normal Chrome profile.

## Setup

```powershell
cd F:\MyDreamWorld\trunk\AionsHome\Connor-BrowserTool
npm install
npm run install:browsers
```

If downloading Chromium is slow, you can use local Chrome instead:

```powershell
$env:USE_SYSTEM_CHROME="1"
npm start
```

## Start

Read-only mode:

```powershell
.\start-readonly.ps1
```

Interactive mode for Connor/Aion's dedicated account:

```powershell
.\start-interactive.ps1
```

You can also double-click:

- `启动只读模式.bat`
- `启动互动模式.bat`

Manual start:

```powershell
npm start
```

By default the service listens on `http://127.0.0.1:8787`.

## Example Calls

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

For screenshot support, `/health` should show `version` as `0.2.0` or higher and `capabilities.screenshotFile` as `true`.

Open a page:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/browser/open `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"url":"https://www.xiaohongshu.com"}'
```

Read visible text:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/browser/text
```

Take a screenshot:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/browser/screenshot -Method Post
```

Save a real PNG screenshot file:

```powershell
curl.exe -s -X POST http://127.0.0.1:8787/browser/screenshot-file -o screenshot.png
```

Do not save `/browser/screenshot` with `-o screenshot.png`; that endpoint returns JSON metadata with a `path`, not image bytes.

## Optional Riskier Actions

These are off by default. To enable controlled interaction manually:

```powershell
$env:ALLOW_BROWSER_MUTATIONS="1"
npm start
```

Then an API caller must still send `confirm: true` for click/type.

For a trusted local agent, `start-interactive.ps1` also sets:

```powershell
$env:TRUST_AGENT_ACTIONS="1"
```

That means Connor/Aion can click, type, press keys, and scroll without sending `confirm: true` every time.

Click:

```json
{
  "x": 120,
  "y": 260,
  "confirm": true
}
```

Type:

```json
{
  "text": "search words",
  "confirm": true
}
```

Press a key:

```json
{
  "key": "Enter",
  "confirm": true
}
```

Scroll:

```json
{
  "deltaY": 700,
  "confirm": true
}
```

In trusted local-agent mode, the `confirm` field can be omitted.

Mutation actions are logged to `logs/actions.log`. Typed text itself is not logged, only the number of characters.

Keep payment, order submission, private messages, account settings, and high-frequency engagement outside automation unless you add a stricter confirmation layer.

## Prompt for Connor/Aion

You can paste this into AionsHome:

```text
你可以调用本地浏览器工具服务：
http://127.0.0.1:8787

先 GET /health 检查状态。
打开网页：POST /browser/open，JSON: {"url":"https://www.xiaohongshu.com"}
读页面：GET /browser/text
读HTML：GET /browser/html
截图信息：POST /browser/screenshot，会返回JSON和真实图片path，不要把这个JSON当图片上传。
保存真实截图：POST /browser/screenshot-file，例如 curl.exe -s -X POST http://127.0.0.1:8787/browser/screenshot-file -o screenshot.png
点击：POST /browser/click，JSON: {"x":100,"y":200}
输入：POST /browser/type，JSON: {"text":"要输入的文字"}
按键：POST /browser/press，JSON: {"key":"Enter"}
滚动：POST /browser/scroll，JSON: {"deltaY":700}

这是我给你注册的专门账号。你可以在小红书上发帖、点赞、评论，但要慢一点、像真人一样操作。不要批量点赞、不要刷屏、不要发私信、不要下单付款、不要修改账号安全设置。遇到验证码、登录、付款、隐私信息、收货地址、银行卡、实名信息时停下来问我。
```
