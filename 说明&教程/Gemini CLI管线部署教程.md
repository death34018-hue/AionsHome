# Gemini CLI 管线完整部署教程

> 本教程面向朋友，帮你从零开始部署 Aion Chat 项目并接通 Gemini CLI 管线。
> CLI 线路**完全免费**，走 Google 个人账号 OAuth 认证，不需要付费、不需要 API Key。
> 你需要用自己的 Google 账号做一次登录授权，之后程序自动复用。

---

## 📌 整体思路

Aion Chat 有多条 AI 管线（Gemini REST API / 硅基流动 / 中转站 / Gemini CLI / Codex CLI）。其中 **Gemini CLI** 管线的特点是：

- **完全免费**，通过 Google OAuth 用你自己的 Google 账号调用 Gemini 模型
- **不需要 API Key**，不走 REST API，而是在本地启动一个 `gemini` 子进程
- 支持 `gemini-2.5-pro`、`gemini-3.1-pro-preview`、`gemini-2.5-flash` 等模型
- 项目所有功能（人设 / 记忆 / 指令解析 / TTS / 监控 / 多端同步）全部照常工作
- 每次发消息时自动创建 CLI 子进程、用完即销毁，不需要你手动启动任何额外服务

---

## 🔧 第一部分：Aion Chat 基础环境安装

> 如果你已经按照「给朋友的部署教程.md」完成了基础安装，可以直接跳到第二部分。

### 1.1 安装 Python

1. 前往 https://www.python.org/downloads/ 下载 **Python 3.10 ~ 3.13**（不要装 3.14）
2. 安装时 **务必勾选底部 「Add Python to PATH」**
3. 验证安装：`Win + R` → 输入 `cmd` → 执行 `python --version`，看到 `Python 3.1x.x` 即可

> ⚠️ 不要从 Microsoft Store 安装 Python，会导致虚拟环境出问题。

### 1.2 安装 C++ 编译工具（如果从没装过 Visual Studio）

1. 打开 https://visualstudio.microsoft.com/zh-hans/visual-cpp-build-tools/
2. 下载运行 `vs_BuildTools.exe`
3. 勾选 **「使用 C++ 的桌面开发」**，安装后重启电脑

### 1.3 安装 Python 依赖

**方式一（推荐，离线）**：双击 **「离线安装环境.bat」**，所有依赖从 `vendor/` 文件夹本地安装，不需要联网。

**方式二（联网）**：双击 **「一键安装环境.bat」**，会自动联网下载依赖。

看到「安装完成」即可。

### 1.4 启动测试

1. 双击 **「一键启动.bat」**
2. 看到 `Uvicorn running on http://0.0.0.0:8080` 表示启动成功
3. 浏览器打开 `http://localhost:8080` 确认能看到主页
4. **先关掉**，继续下面的 CLI 安装

---

## 🚀 第二部分：安装 Gemini CLI（核心步骤）

### 2.1 安装 Node.js

Gemini CLI 是一个 npm 包，需要 Node.js 环境。

1. 前往 https://nodejs.org/ 下载 **LTS 版本**（推荐 v20 或 v22）
2. 安装时保持默认选项，确保 **「Add to PATH」** 被勾选
3. 安装完成后打开 **新的** 终端（PowerShell 或 CMD），验证：

```
node -v
npm -v
```

两个命令都能输出版本号即可（如 `v22.x.x` 和 `10.x.x`）。

> ⚠️ 安装后必须**重新打开**终端窗口，旧窗口的 PATH 不会自动更新。

### 2.2 安装 Gemini CLI 包

在终端执行：

```
npm install -g @google/gemini-cli
```

等待安装完成（可能需要几分钟），然后验证：

```
gemini --version
```

输出版本号（如 `0.41.2`）就说明安装成功了。

> 💡 如果 `npm install` 过程中报网络错误，确认你的科学上网工具对终端也生效（有些代理只代理浏览器流量）。可以设置终端代理：
> ```
> # PowerShell（把端口换成你代理软件的端口）
> $env:HTTP_PROXY = "http://127.0.0.1:7890"
> $env:HTTPS_PROXY = "http://127.0.0.1:7890"
> npm install -g @google/gemini-cli
> ```

### 2.3 首次 OAuth 认证（重要！用你自己的 Google 账号）

这是最关键的一步。Gemini CLI 通过你的 Google 账号做 OAuth 授权，之后所有调用都走你的个人免费额度。

1. 确保**科学上网**已开启（Google OAuth + CLI 调用都需要）
2. 在终端运行：

```
gemini
```

3. 首次运行会提示你选择认证方式，选择 **「Login with Google」**（Google 账号登录）
4. 它会自动打开浏览器，跳转到 Google 登录页
5. **用你自己的 Google 账号登录并授权**
6. 授权成功后回到终端，会看到一个交互式对话界面
7. 随便输入一句话测试（如 `hello`），确认 Gemini 能正常回复
8. 输入 `/quit` 退出交互模式

> ✅ 认证信息会保存在你的用户目录下，**只需要做一次**。之后 Aion Chat 调用 CLI 时会自动复用你的认证。
>
> ⚠️ 认证是**绑定你的 Google 账号**的，每个人用自己的账号登录就行。别人的认证不会影响你，你的认证也不会影响别人。

### 2.4 验证 CLI 能正常工作

在做完认证后，测试一下非交互模式：

```
gemini -p "你好，请用一句话介绍你自己"
```

如果能正常输出回复，说明 CLI 完全可用了。

---

## ⚙️ 第三部分：配置 Aion Chat 使用 CLI 管线

### 3.1 确认项目文件是最新的

你拿到的项目文件夹里，以下两个文件包含 CLI 管线代码：

- `aion-chat/config.py` — 模型列表里有 `CLI-2.5pro`、`CLI-3.1pro`、`CLI-2.5flash`
- `aion-chat/ai_providers.py` — 包含 `call_gemini_cli()` 函数

如果这两个文件是从我这边同步过来的最新版本，就不需要任何代码修改。

### 3.2 启动 Aion Chat

双击 **「一键启动.bat」**，等待服务启动。

### 3.3 切换到 CLI 模型

1. 浏览器打开 `http://localhost:8080/chat`
2. 点击右上角的**模型选择**区域
3. 在模型列表中选择 **`CLI-xxx`** 开头的模型：

| 模型名 | 对应的 Gemini 模型 | 说明 |
|--------|-------------------|------|
| **CLI-2.5pro** | gemini-2.5-pro | 推理最强，速度中等 |
| **CLI-3.1pro** | gemini-3.1-pro-preview | 最新 Pro，预览版 |
| **CLI-2.5flash** | gemini-2.5-flash | 最快，适合日常聊天 |

4. 选好后直接发消息就行了，**不需要配置任何 API Key**

### 3.4 可选但推荐：配置 Gemini Free Key（哨兵 + 向量记忆）

虽然 CLI 管线不需要 API Key 就能聊天，但 Aion Chat 的 **记忆系统**（向量 Embedding）和 **摄像头哨兵** 用的是 Gemini REST API，这两个功能需要一个 Gemini API Key。

如果你不需要记忆和监控功能，可以跳过这步。如果需要：

1. 打开 https://aistudio.google.com/apikey （需要科学上网）
2. 用你的 Google 账号登录
3. 点 **「Create API Key」** 创建一个免费 Key
4. 在 Aion Chat 的设置页面（`http://localhost:8080/settings`），把 Key 填到 **「Gemini Free Key（哨兵+向量）」** 框里

> 💡 这个 Free Key 是免费的 Gemini API Key，和 CLI 的 OAuth 认证是两套独立的东西。Free Key 用于后台轻量任务（向量化、哨兵分析），免费额度完全够用。

---

## 🌐 第四部分：网络环境要求

### 科学上网

Gemini CLI 每次调用都需要能连通 Google 服务。请确保：

1. **代理软件对终端/系统全局生效**，不只是浏览器
2. 推荐使用系统全局代理或 TUN 模式
3. 如果你用 Clash / V2Ray / Shadowrocket 等工具，确认有开启「系统代理」或「TUN 模式」

> 常见问题：浏览器能翻墙但终端不行 → 需要设置终端的代理环境变量，或者开启代理软件的系统全局模式。

### 如果终端走不通代理

在启动 Aion Chat 之前，设置环境变量（在 `一键启动.bat` 前面加，或者单独在终端执行）：

**PowerShell：**
```powershell
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
```

**CMD：**
```cmd
set HTTP_PROXY=http://127.0.0.1:7890
set HTTPS_PROXY=http://127.0.0.1:7890
```

把 `7890` 换成你代理软件实际使用的端口号。

---

## 📋 第五部分：完整步骤清单

按顺序走一遍就行：

- [ ] 安装 Python 3.10~3.13（勾选 Add to PATH）
- [ ] 安装 C++ 编译工具（如果没装过 Visual Studio）
- [ ] 双击「离线安装环境.bat」安装 Python 依赖
- [ ] 安装 Node.js LTS（勾选 Add to PATH）
- [ ] 终端执行 `npm install -g @google/gemini-cli`
- [ ] 终端执行 `gemini`，完成 Google OAuth 登录授权
- [ ] 测试 `gemini -p "你好"` 确认能正常回复
- [ ] 双击「一键启动.bat」启动 Aion Chat
- [ ] 浏览器打开 `http://localhost:8080/chat`
- [ ] 右上角切换模型到 `CLI-xxx`
- [ ] 发消息测试，确认能正常回复
- [ ] （可选）在设置页填写 Gemini Free Key 用于记忆和哨兵

---

## ❓ 常见问题

### Q: 终端报 `gemini 不是内部命令`
**A:** Node.js 或 npm 全局安装路径没加入 PATH。
1. 确认 Node.js 安装成功：`node -v` 能输出版本号
2. 重新执行 `npm install -g @google/gemini-cli`
3. **关掉终端再重新打开**，然后再试 `gemini --version`

### Q: 报错 "未找到 gemini CLI"
**A:** 这是 Aion Chat 后端的报错，说明 Python 进程找不到 gemini 命令。
1. 确认 `gemini --version` 在你的终端能正常运行
2. 如果你是装完 Node.js 之后没重启过电脑，试试**重启电脑**再启动 Aion Chat
3. 检查你的 PATH 环境变量里是否有 Node.js 和 npm 的路径

### Q: CLI 回复很慢（10-30 秒才开始）
**A:** 正常现象。CLI 模式下 Gemini 会有一个"冷启动"过程（创建子进程 + OAuth 验证 + 模型加载），首次响应延迟约 10-30 秒。之后内容会流式输出，速度正常。

### Q: 认证过期了怎么办
**A:** 重新在终端运行 `gemini`，它会自动刷新认证。然后 `/quit` 退出即可。

### Q: 报错 "not running in a trusted directory"
**A:** 代码里已经自动加了 `--skip-trust` 参数来跳过目录信任检查。如果还出现这个问题，说明你用的 `ai_providers.py` 不是最新版本。

### Q: npm install 报网络错误
**A:** npm 需要科学上网才能访问 Google 的包。在终端设置代理后重试：
```
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
npm install -g @google/gemini-cli
```

### Q: 发图片给 AI 报错
**A:** 图片通过 CLI 原生 `@路径` 语法传递，代码已自动处理。如果报错检查：
1. 图片文件是否存在于 `aion-chat/data/uploads/` 目录
2. 文件路径中是否有中文文件夹名（建议整个项目放在英文路径下）

### Q: 想用 CLI 以外的模型（REST API）
**A:** 在设置页面（`/settings`）填入对应的 API Key 即可，CLI 和 REST API 可以共存，随时在模型选择器切换。

### Q: 能不能和你用同一个 Google 账号
**A:** 可以但不推荐。每个人用自己的 Google 账号做 OAuth 认证，各自有独立的免费额度，互不影响。

---

## 💡 补充说明

- **不需要额外启动任何服务**。Aion Chat 的 Python 后端每次发消息时自动创建 gemini CLI 子进程，用完即销毁。
- **电脑重启后不需要重新认证**。OAuth 认证信息保存在本地用户目录下，持久有效。
- **CLI 管线和 REST API 管线可以共存**。你可以同时配置 Gemini API Key 和 CLI，在聊天页面随时切换模型。
- 所有功能（世界书人设、向量记忆、TTS 语音合成、摄像头监控、日程闹铃、音乐点歌、AI 生图、视频通话等）在 CLI 管线下全部正常工作。
- 如果需要 TTS 语音播报功能，还需要额外配置硅基流动 API Key（在设置页面填写）。



## 🚀 使用 Antigravity 管线（可选，免费额度）

Antigravity 是 Google 的 AI agent 平台，通过它的 CLI 工具可以免费使用 Gemini 3.5 Flash、Gemini 3.1 Pro、Claude Opus 4.6 等模型。**不需要 API Key**，只需要一个 Google 账号。

### 安装 Antigravity CLI

在 PowerShell 里运行（需要梯子/代理）：

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

装完后关闭 PowerShell 重新打开，输入 `agy --version` 确认安装成功。

### 登录

1. 在 PowerShell 里输入 `agy` 回车
2. 它会弹出浏览器让你登录 Google 账号（和平时登 Gmail 一样）
3. 授权完成后回到终端，看到聊天界面就说明登录成功了
4. 按 `Ctrl+C` 退出 agy

> 💡 登录一次就行，之后不需要重复登录。

### 在 Aion Chat 中使用

1. 启动 Aion Chat 后，在聊天窗口的**模型选择**下拉菜单里选择 **「Antigravity」**
2. 发消息就行了，它会通过 agy CLI 帮你调用模型

### 切换模型

Antigravity 支持多个模型，默认是 Gemini 3.5 Flash。如果想换模型：

1. 在 PowerShell 里输入 `agy` 进入交互模式
2. 输入 `/model` 查看和切换可用模型
3. 选好后退出（`Ctrl+C`），之后 Aion Chat 走 Antigravity 管线就会使用你选的模型

### 注意事项

- ⚠️ 安装和使用都**需要梯子**（agy 连接 Google 服务）
- ⚠️ 免费额度有限制，日常聊天够用，频繁刷可能触发限流
- ⚠️ 如果提示「未登录」，重新在 PowerShell 里运行 `agy` 登录一下就好


### AntiGravity线路如何切换模型
第一步：打开 PowerShell 或终端，输入：agy

第二步：等 agy 的 TUI 界面出来后，输入：/model

第三步：会弹出一个模型选择列表，用 ↑↓ 箭头选择你想要的模型（比如 Gemini 3.1 Pro），按回车确认。

第四步：输入 /exit 退出 agy。

完成。 你的选择会保存在 Google 服务器上，之后所有 agy --print 调用（包括我们项目里的 Antigravity 管线）都会自动使用新模型。不需要重启服务器。