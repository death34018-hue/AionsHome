# 历次更新与问题修复（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

## 更新日志

### 2026-07-06 — Tavily 联网搜索 + 链接预览卡片

**背景**：给聊天模型补充按需联网能力，并让聊天里出现的网页地址自动变成紧凑可点击的小卡片；同时处理 Android WebView 内外链打不开的问题。

**改动内容**：
1. **`web_search.py` + `capabilities.py` — 联网搜索/网页读取能力**
   - 新增 Tavily Search/Extract 调用封装，支持 `[WEB_SEARCH:查询内容]` 和 `[WEB_EXTRACT:完整URL]`
   - Tavily API Key 从设置页保存到本机 `data/settings.json`，仓库不内置个人 Key，清理个人数据脚本会一并移除
   - 能力可通过现有能力开关关闭；未配置 Key 时不向模型注入联网指令
   - 搜索触发后插入可见 system 消息，原始工具指令从聊天气泡和 TTS 文本中过滤
2. **`routes/chat.py` / `routes/chatroom.py` — 主聊天与聊天室统一接入**
   - 私聊、群聊、重新生成等路径统一检测联网指令
   - 搜索结果作为下一条上下文回灌给模型，让模型基于清洗后的网页信息继续回复
   - SSE/WebSocket 增加 `web_search` 状态事件，方便前端展示搜索中的反馈
3. **`link_preview.py` + 前端渲染 — 链接小卡片**
   - 消息中出现 http/https URL 时自动抽取标题、摘要、来源、图标/预览图，保存为 `link_preview` 附件
   - 主聊天和聊天室都渲染为小尺寸卡片，点击可直接打开网页
   - 本机/内网地址只生成 fallback 卡片，不主动抓取页面，避免误探测本地服务
4. **`WebViewActivity.java` — Android 外链打开**
   - 注入 `AionExternal.open(url)` 原生桥，支持链接卡片、`target="_blank"` 和 `window.open`
   - 外部网站交给系统浏览器打开，同源页面继续留在 WebView 内

### 2026-06-15 — Gemini Lyria AI 生成歌曲接入

**背景**：在现有 Gemini 付费 Key 基础上接入 Lyria 歌曲生成，让聊天里的“写一首歌”可以从歌词/风格/声线描述直接变成可播放音频，并且私聊和群聊都能使用。

**改动内容**：
1. **`song_gen.py` — Lyria 歌曲生成模块**
   - 新增 `[SONG]...[/SONG]` 指令格式，要求 `Title`、`Style`、`Singer/Vocal`、`Duration`、`Prompt`、`Lyrics`
   - 使用 Gemini Lyria `lyria-3-pro-preview` 的 REST `generateContent` 接口，解析返回的 audio `inlineData`
   - 生成文件保存到 `data/songs/song_gen_{timestamp}.{ext}`，通过 `/songs/*` 访问
   - 清理 AI 误输出的歌词正文，避免聊天气泡里额外出现超长歌词

2. **`routes/chat.py` / `routes/chatroom.py` — 私聊和群聊统一支持**
   - 仅当「允许 AI 生成歌曲」开启时注入 SONG 能力提示
   - 检测 SONG 块后异步生成歌曲，正文统一为 `为你写的歌《歌名》`
   - 创建 `generated_song` 附件消息，保存歌曲 URL、标题、模型、歌词、原始 prompt
   - 生成期间广播 `song_gen_start` / `chatroom_song_gen_start`，完成或失败后广播对应 done/failed 事件

3. **前端歌曲卡片和播放器**
   - 私聊和聊天室都支持生成歌曲卡片，提供内联 audio 和「打开播放器」
   - 小播放器浮层展示歌名、模型、可滚动歌词、播放/暂停、时间和进度条
   - 进度条通过 audio 事件 + `requestAnimationFrame` 实时同步
   - 等待期间显示「歌曲谱写中....」，真正歌曲卡片到达后自动移除

4. **设置和存储整理**
   - 新增 `/api/settings/song-gen` GET/PUT，和聊天界面右上角开关联动
   - 新增 `SONGS_DIR = data/songs`，生成歌曲不再和普通上传混放在 `data/uploads/`
   - 已迁移旧的生成歌曲文件到 `data/songs/` 并修正对应附件 URL

### 2026-05-30 — Antigravity CLI 管线接入

**背景**：Google Antigravity CLI（`agy`）是 Gemini CLI 的升级替代品，支持 Google OAuth 免费调用 Gemini 模型。与 Gemini CLI 不同，agy 使用 Windows `WriteConsole()` API 直接写入控制台句柄，无法通过 stdout 管道或文件重定向捕获输出，需要特殊处理。

**改动内容**：
1. **`ai_providers.py` — Antigravity CLI 调用实现**
   - 新增 `_find_antigravity_binary()`：自动定位 agy.exe（PATH 搜索 + `%LOCALAPPDATA%\agy\bin\agy.exe` 回退）
   - 新增 `call_antigravity_cli()` 异步生成器：构建 PowerShell 脚本，使用 `Start-Transcript` 拦截 console buffer 输出，`CREATE_NEW_CONSOLE + SW_HIDE` 给 PowerShell 独立隐藏控制台窗口（解决从 uvicorn 进程 spawn 时 transcript 为空的问题），通过 `asyncio.to_thread` 包装同步 `subprocess.run` 调用
   - 新增 `_extract_transcript_body()`：解析 PowerShell Transcript 文件，过滤英文/中文系统头部，保留空行用于段落分隔
   - 新增 `_deduplicate_cjk()`：修复 PowerShell 5.1 Start-Transcript 的已知 Bug（CJK 字符每个被重复输出两次）
   - 新增 `_summarize_antigravity_log()` / `_is_antigravity_auth_prompt()`：日志诊断和认证检测
   - `CLI_STATUS_PREFIX` 状态消息前缀（`\x00CLI_STATUS:`），在流式输出中传递「AI正在思考…」等状态事件
   - 控制台缓冲区宽度设为 500 列，避免 AI 回复在默认 80 列处被硬换行

2. **`config.py` — MODELS 字典新增 Antigravity 条目**
   - 新增 `"Antigravity": {"provider": "antigravity_cli", "model": "", "vision": True}`
   - 模型选择由 agy CLI 内部管理（通过交互式 `/model` 命令切换，偏好存储在 Google 服务器端）

3. **`routes/theater.py` — CLI_STATUS 过滤**
   - 小剧场的两个 `_bg_generate` 函数（发送 + 重新生成）增加 `CLI_STATUS_PREFIX` 检测，将状态消息路由为 `cli_status` SSE 事件而非追加到回复文本

4. **`给朋友的部署教程.md` — 新增 Antigravity CLI 部署说明**
   - 安装命令、OAuth 登录步骤、使用说明、模型切换教程、注意事项

**技术要点**：
- agy 使用 `WriteConsole()` 直接写 Windows Console Handle，stdout pipe / 文件重定向 / cmd /c 包装均无法捕获输出
- 唯一可行方案：PowerShell `Start-Transcript` 拦截 console buffer → `Stop-Transcript` → 读取 transcript 文件
- 从 uvicorn 服务进程 spawn 时，必须 `CREATE_NEW_CONSOLE` 给 PowerShell 独立控制台 + `SW_HIDE` 隐藏窗口
- 不支持流式输出（transcript 文件只在 `Stop-Transcript` 后才可读取），等待完成后一次性返回
- agy `--print` 模式无 `--model` 参数，模型偏好需在服务器上用 `agy` 交互模式 → `/model` 命令切换

**使用方式**：聊天界面右上角切换模型到 `Antigravity`，不需要 API Key，使用 Google OAuth 免费调用。切换底层 Gemini 模型需在服务器终端运行 `agy` → 输入 `/model` → 选择模型 → `/exit`。

### 2026-05-10 — CLI 图片管线修复 + Connor 跨窗口上下文 + 多场景群聊集成

**背景**：
1. Connor 私聊窗口看不到群聊消息（`build_connor_1v1_prompt` 传了 1v1 的 room_id 给 `fetch_merged_timeline` 的 `room_id` 参数，但该参数是用于指定群聊房间的，导致 0 条群聊消息被合并）
2. 闹铃/监控/哨兵/cam_check 等触发场景只能看到私聊历史，无法感知群聊上下文
3. 通过 Gemini CLI 和 Codex CLI 发送图片全部报错（Gemini CLI: `Separator is not found, and chunk exceed the limit`；Codex CLI: `Input exceeds the maximum length of 1048576 characters`）

**改动内容**：

1. **`ai_providers.py` — `_build_cli_prompt` 图片/音频本地路径传递**
   - 旧方式：完全忽略 messages 中的 `attachments` 字段
   - 中间尝试：将图片转 base64 内嵌到 prompt 文本 → 失败（base64 编码后轻松超过 CLI stdin 的长度限制）
   - 最终方案：解析附件为本地绝对路径，直接写入 prompt 文本，由 CLI 自行读取文件
   - 支持 image/* 和 audio/* 两类附件，结构化附件（voice/video dict）跳过（已有 transcript 文本兜底）

2. **`chatroom.py` — `stream_connor_cli` 支持 messages 列表**
   - 新增 `messages` 参数，可直接传入完整消息列表（保留附件），不再强制转为纯文本
   - 传入 messages 时自动注入 Connor persona 作为 system 消息

3. **`chatroom.py` — `build_connor_1v1_prompt` → `build_connor_1v1_context`**
   - 从返回纯文本 prompt 改为返回 messages 列表，timeline 消息保留 attachments 字段
   - 修复 `room_id` 参数误传问题（不再传 1v1 room_id 给 `fetch_merged_timeline`）

4. **`routes/chatroom.py` — Connor 群聊/私聊管线改造**
   - `_reply_connor`（群聊）：不再手动将 history 转为纯文本（丢失附件），直接传 `connor_history` 给 `stream_connor_cli(messages=...)`
   - `_generate_connor_reply`（私聊）：改用 `build_connor_1v1_context` + `stream_connor_cli(messages=...)`

5. **`schedule.py` — 闹铃/监控触发集成群聊上下文**
   - `_fire_alarm`、`_fire_monitor`：使用 `fetch_merged_timeline` 替代原来只查私聊的逻辑，懒导入避免循环依赖

6. **`camera.py` — 哨兵/cam_check 集成群聊上下文**
   - `_call_core`（哨兵 Core 唤醒）、`perform_cam_check`（主动查看监控）：同样使用 `fetch_merged_timeline`，懒导入避免循环依赖

**不影响的线路**：硅基流动（`build_multimodal_messages`，base64 内嵌 API JSON）和 Gemini 原生 API（`build_gemini_contents`，base64 内嵌 `inline_data`）的图片处理方式不变

**踩坑记录**：见下方坑 10、坑 11、坑 12

### 2026-05-09 — 统一时间线上下文 + 统一记忆总结

**背景**：之前 Aion 私聊只能看到私聊历史，群聊只能看到群聊历史，两个 AI 的记忆总结也各自独立（Connor 私聊和群聊分别总结，群聊记忆还要同步一份到 Aion 主库）。改为统一时间线，让每个 AI 都能同时看到私聊和群聊内容，记忆总结也合并处理。

**改动内容**：

1. **新增 `context_builder.py`** — 统一上下文构建模块：
   - `fetch_merged_timeline(who, limit, *, conv_id, room_id)`：同时查询 `messages` 和 `chatroom_messages` 两张表，按 `created_at` 合并排序，返回统一时间线
   - `render_merged_timeline(merged, who)`：将合并时间线转为 AI 历史格式，私聊/群聊混合时自动插入场景切换标记 `[以下为群聊记录]` / `[以下为私聊记录]`，消息前缀带 `[群聊]` / `[私聊]` 标签
   - `build_ability_block()`、`build_memory_blocks()`、`strip_tool_commands()` 等工具函数从各处抽取统一

2. **`routes/chat.py`** — Aion 私聊上下文统一：
   - `send_message`、`edit_resend`、`regenerate` 三个函数的历史构建改为 `fetch_merged_timeline("aion")` + `render_merged_timeline()`，Aion 在私聊中也能看到群聊内容

3. **`chatroom.py`** — 群聊上下文 + Connor 记忆统一：
   - `build_aion_group_context()` / `build_connor_group_context()`：改用统一时间线，移除旧的跨窗口上下文注入
   - `digest_chatroom()`：合并 Connor 1v1 + 群聊消息统一总结，使用 `connor_unified` 锚点，scope 固定为 `"connor"`，删除"群聊记忆同步写入 Aion 主库"逻辑（两个 AI 各管各的记忆）
   - `_connor_1v1_auto_digest_loop()`：不再查找特定房间，直接调用 `digest_chatroom()` 统一总结
   - `connor_1v1_on_message()`：群聊消息也触发计时器重置

4. **`memory.py`** — Aion 记忆总结统一：
   - `_do_digest()`：在私聊消息基础上追加查询群聊 `chatroom_messages`，标记 `_source`（private/group），混合来源时消息格式带 `[群聊]` / `[私聊]` 标签

5. **`main.py`** — 自动总结空闲检测增强：
   - `_auto_digest_loop()`：同时检查 `messages` 和 `chatroom_messages` 两张表的最后用户消息时间，避免群聊活跃时误触发私聊自动总结

6. **`routes/chatroom.py`** — 触发器扩展：
   - `_save_msg()`：群聊消息也触发 `connor_1v1_on_message()` 重置自动总结计时器

7. **Bug 修复**：
   - 流式输出气泡残留原始指令：`aion_done` / `connor_done` 事件用服务端清洗后的内容替换 streamingText
   - 闹铃/日程创建时缺少系统消息：在 `process_schedule_commands` 之前预检测指令并插入系统提示
   - 音乐点歌后不自动播放：添加 `autoplay: True` 参数

### 2026-05-08 — Gemini CLI 本地调用接入

**背景**：Gemini CLI（`@google/gemini-cli`）支持通过 Google OAuth 免费调用 Gemini 模型，无需 API Key。将其作为第四种 AI 调用方式集成到项目中。

**改动内容**：
1. **`ai_providers.py`**：
   - 新增 `_find_gemini_script()`：自动定位全局安装的 gemini CLI 脚本（npm root -g 方式 + gemini.cmd 位置推导）
   - 新增 `_build_cli_prompt(messages)`：将 messages 列表拼成 `[System Instruction] / [User] / [Assistant]` 格式的完整 prompt
   - 新增 `call_gemini_cli()` 异步生成器：通过 `asyncio.create_subprocess_exec` 启动 CLI 子进程，stdin 传入 prompt（绕过 Windows 命令行 8K 长度限制），流式读取 stdout 并 yield
   - `stream_ai()` 新增 `gemini_cli` provider 路由分支
2. **`config.py`**：`MODELS` 字典新增 `CLI-2.5pro`、`CLI-3.1pro`、`CLI-2.5flash` 三个模型
3. **新增 `cli线部署教程.md`**：面向朋友的 CLI 线路部署指南

**使用方式**：聊天界面右上角切换模型到 `CLI-xxx`，其余（人设、记忆、指令解析、TTS）全部照常工作。不需要额外启动任何服务。

**部署前置**：`npm install -g @google/gemini-cli` + 首次运行 `gemini` 完成 OAuth 认证

### 2026-04-08 — UI 多页面拆分重构

**背景**：原 chat.html 单文件近 4000 行，所有功能（设置/世界书/记忆库/日程/摄像头/监控日志/定位）以模态弹窗形式耦合在聊天页内，维护和扩展困难。

**改动内容**：
1. **新建 7 个独立功能页面**：settings.html、worldbook.html、memory.html、schedule.html、camera.html、monitor-logs.html、location.html，每个页面独立完整（HTML+CSS+JS）
2. **新建共享层**：common.css（CSS 变量/子页面布局/组件样式/闹铃弹窗/toast）+ common.js（api() 封装/WebSocket 连接/闹铃弹窗/系统通知）
3. **chat.html 瘦身**：删除了 7 个模态弹窗的 HTML + 对应 JS 函数（摄像头控制/监控日志/WebSocket override/记忆库管理/日程管理/设置/世界书/定位），保留与聊天深度耦合的功能（语音唤醒/TTS/BLE密语/音乐/系统日志/[CAM_CHECK]）
4. **侧边栏简化**：移除 6 个功能导航按钮，仅保留「系统日志」「密语时刻」「返回主页」
5. **main.py 新增路由**：/settings、/worldbook、/memory、/schedule、/camera、/monitor-logs、/location
6. **home.html 更新**：APPS 注册表新增 camera/logs/location 入口，memory/worldbook/alarm/settings 绑定对应 URL
7. **文件管理器优化**：标题栏加关闭按钮，文件列表区域可滚动

**保留在 chat.html 的功能**：语音唤醒通话、TTS 语音合成、密语时刻(BLE)、音乐点歌、[CAM_CHECK] 主动查看监控、系统日志（服务端持久保留最近 30 条模型调用，包含后台监督、唤醒、哨兵及生图/歌曲；支持刷新恢复和手动清空）、文件管理器

**子页面共享机制**：每个子页面通过 `<link href="/static/common.css">` + `<script src="/static/common.js">` 引入共享层，调用 `connectCommonWS()` 建立独立 WebSocket 连接（用于接收闹铃弹窗），各页面自行管理 API 调用和渲染逻辑

### 2026-04-08 — 后台消息保障 + 子页面 iframe 浮层（防切页丢消息/TTS 中断）

**背景**：多页面拆分后，从 chat.html 导航到设置/主页/监控日志等页面会销毁聊天页，导致：① 正在等待的 AI 回复丢失（SSE 流中断，后端 generate() 生成器被关闭，DB 保存和 WS 广播永远不执行）；② TTS 语音播放立即停止（Audio 元素和队列被销毁）。手机上尤其明显，发消息后切到其他页面查看就会丢回复。

**改动内容**：

1. **后端：AI 生成解耦为后台任务**（`routes/chat.py` — `send_message` + `regenerate_message`）
   - 原架构：`generate()` 异步生成器内 AI 流式输出 → 后处理（指令检测、音乐搜索、日程解析）→ 存 DB → WS 广播，全在 `yield` 链路中，客户端断开则全部丢失
   - 新架构：拆为 `_bg_generate()` 后台任务 + `generate()` SSE 转发层
     - `_bg_generate()`：`asyncio.create_task()` 启动，AI 流式输出 + 全部后处理 + 存 DB + WS 广播，通过 `asyncio.Queue` 向 SSE 层推送事件，`try/finally` 确保始终运行到结束
     - `generate()`：仅从 Queue 读取并 `yield`，纯薄层转发。客户端断开时生成器正常关闭，后台任务不受影响
   - **效果**：即使客户端断开连接（切页/关闭/网络中断），AI 回复依然会完成生成、存入数据库、通过 WebSocket 广播到所有在线客户端

2. **前端：子页面 iframe 浮层**（`static/chat.html`）
   - 新增全屏 `#subPageOverlay`：包含顶部关闭栏 + `<iframe>` 容器
   - 侧栏「⚙ 设置」「🏠 返回主页」「⬅ 返回」全部改为 `openSubPage(url)` → 在浮层中打开目标页，chat.html 始终存活
   - `closeSubPage()`：关闭浮层 + 重新加载消息列表（补上浮层期间后台生成的新消息）
   - 浏览器返回键 (`popstate`) 自动关闭浮层
   - **效果**：SSE 流式接收、TTS 播放、WS 连接在浮层打开期间全部不中断

3. **home.html iframe 适配**
   - 当 home.html 在 iframe 中加载时，点击「聊天」→ `window.parent.closeSubPage()` 关闭浮层回到 chat.html
   - 点击「密语时刻」→ 关闭浮层 + 调用 `window.parent.openWhisper()`

**涉及文件**：`routes/chat.py`（后端核心）、`static/chat.html`（前端浮层 + 导航改造）、`static/home.html`（iframe 适配）

## 2026-10-04 — AGY 官方流式管线

Antigravity 接入使用官方 agy 的 stdin/stdout stream-json，替换历史控制台抓取和 SQLite/Protobuf 回复恢复。聊天专用 Agent 排除大部分默认开发组件及工具/技能/MCP 继承，保留应用人设、记忆和文本能力协议；官方运行时仍可能保留任务管理组件。短提示对照输入从 12,042 降至 673 tokens。模型目录来自 CLI 的真实 slug，调用时明确指定模型；旧 AGY-3.1pro 入口仍退休，使用新 AGY 模型入口。图片使用现有哨兵代看，语音使用转写。换账号请运行项目根目录的 scripts/agy_login.ps1 并自行完成官方交互登录。 AGY CLI 现使用全局 strict 权限和显式拒绝规则，禁止模型写文件、执行命令、调用 MCP 和浏览器操作；管线启动前检查规则齐全，否则停止调用。改动后由用户自行重启服务器。

## 2026-10-07 — 共享用户画像整理

`/user-profile` 可编辑长期画像、近期重点与待办、当前活动状态，并预览实际注入文本。完整画像背景最多1200字符（含标题、日期）；保存记录与注入预算分开，超量数量在页首显示。工具能力总开关继续有效。

用户消息触发后台配置的哨兵；同一事项用短标签更新，数据库编号由程序生成。普通近况默认3天，明确持续时长按实际时长保留；临时项目14天转冷、30天淡出，持续项目60/180天。每日维护时间状态，增长后后台合并精简；AI复述与整理不刷新用户确认时间。未完成待办不因到期、整理或同主题新任务而丢失；最早可做时间与截止时间独立记录。

单项错误保留其他有效更新，记录具体原因，最多3次延迟尝试。旧版资料首次迁移前完整备份到同库 `post_sentinel_backups`；原聊天和日常记忆保持原有存储。更新后重启后端并刷新页面，迁移和旧条目整理会在启动后执行。

画像总结与当前状态描述本人时使用世界书 `user_name`，姓名不写死；提示词明确要求，保存模型输出时也替换泛称“用户”。“用户画像”等技术术语及“其他用户”保持原意。2026-10-07 按当前配置姓名修正了24条现有画像与当前状态，完整原文备份在 `post_sentinel_backups` 的 `profile-subject-name-*` 记录中；来源、确认时间、有效期和手动保护保持原样。

画像页改为紧凑文字列表，每条只显示正文、最近写入时间和操作：删除在上、铅笔编辑在下；快捷删除会自动保存，点击铅笔才展开编辑、保存和取消。当前状态同样收起编辑区。列表不再展示来源、进展或是否进入背景等说明，简化的保存请求保留后台的日期、类型和进展。分类标题只在每组开头出现一次，标题与正文间距已缩紧；虚构资料验证了快捷删除保存后刷新不恢复。

按新的使用偏好取消手动保护：手动画像和状态也随较新的聊天自动更新、按原生命周期维护。旧保护标记在新后端读取时解除，解除前备份原文到 `post_sentinel_backups`，确认时间和来源不变；用户删除后的内部防恢复标记仍保留。38项相关测试通过，虚构资料浏览器验证了桌面/390px手机布局、编辑保存、取消和新增取消。重启后端并刷新页面后使用完整新逻辑。

## 2026-10-09 — 画像分类与收录整理

画像页和 AI 背景统一分为“用户画像”“用户偏好”“近期重点与待办”，每组按最后更新时间倒序展示；AI 背景开头仅说明资料基于历史聊天记录总结、供参考。稳定偏好不因最近刚提到而放入近期，已结束内容默认收起，并显示近期事项的进展。当前状态标明已结束或已过期。

自动整理仅保留稳定背景、偏好和对后续陪伴有用的重要近况；新增近期项必须给出 `reason` 说明后续参考价值，新增已完成的活动跳过，已有待办仍按本人确认更新完成状态。吃饭、遛狗回来、电影观感、游戏比分等一次性活动只更新当前状态。保存预算统一限制为100项/20000正文字符，实际发给AI仍最多1200字符。v3分类迁移前在 `post_sentinel_backups` 备份，保留来源和确认时间。

## 2026-10-09 — 记忆总结发送完整正文

Aion 与 Connor 的记忆总结均发送每条待总结消息的完整文本，移除原来的前300字符截断。仍按20～50条分批处理，保留时间、发送人和消息来源。长消息回归检查覆盖两条线路的私聊、群聊以及用户和AI正文；本次不重跑历史总结或调整总结锚点。后端重启后生效。

## 2026-10-09 — 伴侣画像 v4（替代上面的 v3 规则）

画像统一为核心画像、偏好与交互规则、动态记忆、事实档案，包含17个可选子类；页面与聊天背景都按分类、子类及更新时间组织。事实档案默认收起，按当前聊天话题检索。聊天背景按约2000 tokens选择完整条目，页面显示估算量和每条未选入的原因；保存上限独立为500项/100000正文字符，不会因聊天预算而删除存档。

提示词以恋人写给未来自己的伴侣札记为出发点，记住背景、关系、意义和相处边界，使用配置姓名；明确区分本人事实、推测及待确认冲突，不把AI台词或用户贴出的文档当成本人事实。小哨兵只维护新对话的明确变化；两条既有记忆总结线路在同一次模型调用中输出增量 `profile_updates`，不另设每日全量模型任务。结果先持久化再推进原记忆总结，失败更新可重放；延迟结果不覆盖更新过的条目，合并保留所有来源。

首次升级先备份画像，再由当前配置的 Connor 聊天模型整理已有重要记忆的原始证据及本人填写的背景。没有精确消息ID的旧记忆按来源时间和关键词追溯；完整消息分批，不截正文。进度写入数据库，页面显示进度并可继续；这是已有可追溯重要记忆的补齐，不是重新总结所有历史聊天。长期档案不会因久未提到而消失；过期动态先归档，30天后低价值临时记录清理、重要经历转入事实档案，原聊天仍保留。


### 近期生活与待办（2026-10-09，替代 v4 长期画像）

- 自动整理本人已经定下的安排、正在推进的事，以及未来几天仍有影响的持续状况。日常吃喝、随口的想法和偏好跳过，清单可以为空。
- 哨兵在用户消息后增量更新；常规校对复用记忆总结的同一次模型调用。事项保留稳定引用，更新进展与明确下一步；完成即退出发送内容。
- 哨兵每项变化需引用当前触发的本人消息，最近12条对话用于理解背景。记忆总结和手动整理最近一周也使用相同的收录规则。同一事项只更新进展。
- 临时想法、近期偏好和普通状态以本人最后确认时间计算，七天未提及淡出；AI复述与整理不续期。明确未完成待办保留为结果未确认，明确期限的进行中事项按期限处理。结束内容七天后从清单清理，原聊天不动。
- 聊天只发送值得跟进的近期清单和仍有效的此刻活动。旧的自动想法和偏好退出聊天背景，手动保留的资料仍可发送。全清单没有目标条数，硬上限为约2000估算tokens、24条。
- v4整份备份至post_sentinel_backups，旧历史补齐任务退出。仅手动初始化最近七天固定窗口，Codex-5.6-Sol，完整消息正文按约96000字符分批；保存游标与返回用量。普通重启不自动调用，硬中断后可手动继续。
- 页面 /user-profile 显示近期生活、下一步和实际发送预览；手动修改使用版本检查，迟到的旧总结不能覆盖更新进展。

## 2026-10-12 — AGY 原生看图

2026-10-19 修复：发送前完整解码图片，不再仅凭扩展名或 JPEG 头判断有效。缺少结束标记但可解码的 JPEG 会生成临时 PNG，供所选 Gemini / Claude 模型原生读取；原上传文件不改，已丢失的像素无法恢复。正常图片保持原字节，无法解码的附件提示重新上传。修复适用于历史消息里的图片，无须重新发送；后端重启后生效。原始问题截图已通过 Gemini 4 Argon (Medium) 和 Gemini 3.8 Flash (High) 真实看图验证；3.8 的首次 90 秒探针超时，180 秒复测成功，生产默认超时仍为 10 分钟。

AGY 的 Gemini / Claude 模型自动启用 `vision`，启动读取旧模型缓存时也会修正能力声明；GPT-OSS 仍走哨兵。私聊、群聊及朋友圈的 AGY 图片请求使用原生 `view_file` 读取临时副本，保留图片与各条消息的对应关系，结束或取消后删除副本。纯文字仍使用原来的无工具陪伴配置。

官方 CLI 的 `~/.gemini/antigravity-cli/settings.json` 使用 `request-review`（默认值，CLI 保存时可能省略此字段），并给项目的 `aion-chat/data/antigravity-login/images` 目录添加 `read_file(绝对路径)` 许可。保留 `write_file(*)`、`command(*)`、`unsandboxed(*)`、`mcp(*)`、`execute_url(*)` 五条拒绝规则；本机旧设置另有备份。1.2.16 的 `strict` 会在无界面调用时拒绝读图，因此替代之前的 strict 配置。

图片专用配置只列出 `view_file`；执行前钩子按本次图片清单批准读取并拒绝其他调用，兼容旧 CLI 的 `allowTool` / `denyReason` 字段。重启后端、刷新页面后生效。

## 2026-10-14 — 聊天猜拳

- 「工具与能力 → 猜拳」默认开启；关闭后不再注入猜拳能力提示，也不接收新的 AI 主动邀请。名字读取当前配置，已有邀请和结果保留。
- AI 用 `[猜拳:石头]`、`[猜拳:剪刀]` 或 `[猜拳:布]` 邀请并出拳。指令不会显示或朗读；双方出拳后才揭晓。私聊、另一位 AI 的单聊及群聊共用蓝色日夜主题弹窗。
- 聊天「＋ → 猜拳」可以主动邀请指定对象。自己的选择仅存在猜拳记录里，邀请消息不向 AI 透露；群聊只请求指定对象回复。
- 关闭或「稍后再玩」仅收起界面；消息下的邀请按钮可以重新打开。多个邀请按顺序处理，「放弃本局」单独取消，晚到的回复不会复活已取消的局。
- 收起时只暂停当时那批邀请，后续新邀请仍自动弹出；消息下的邀请入口使用无边框的小字。
- 揭晓结果写进当前聊天、进入后续上下文，并自动通知本局对手。状态保存在 SQLite，刷新后可继续。
- 验证：`python -m unittest test_rps`；`node test_rps_browser.js`。新增接口需要重启后端并刷新页面。
- Android 前端更新只在实际编辑或生成时推迟，不把正在阅读的群聊 iframe 当作编辑状态。恢复前台时补查猜拳记录，收到邀请消息时也可独立补取状态；后台私聊不在当前群聊上弹窗。手机页面更新后需完全关闭 App 再打开一次，以加载新版资源。


## 2026-10-17 — 猜拳约定与合并发送

- 每局填写「本局约定」，显示在弹窗原“小家的小对决”位置；AI 主动发起使用 `[猜拳:石头|谁输了谁是小狗]`，接招仍兼容 `[猜拳:石头]`。
- 用户选拳后先保存待发送邀请，在输入框上方展示、可撤掉；点击聊天发送时与正文或语音一起发送，无正文也可以发送邀请。群聊邀请只唤起指定对象。
- 双方出拳后即时揭晓并保存，不自动触发模型回复；结果和约定随当前聊天下一条用户消息进入上下文。待同步结果不进入其他聊天的时间线或历史检索。
- 状态存于数据库，刷新后恢复；旧局兼容为“随便玩一局”。需要手动重启后端并刷新页面加载新资源。
- 轻量验收：`python -m unittest test_rps test_context_builder test_memory_search_evidence`、`node test_rps_browser.js`；浏览器测试可加 `RPS_TEST_IFRAME=1` 验证手机嵌入页面。


## 2026-10-17 — 猜拳消息归属与自动揭晓

- 邀请展示“我／对方发起了猜拳 · 主题”，结束后追加“查看结果”；用户邀请靠右并跟随发起消息，AI 邀请靠左。长主题自动换行、完整展示；结果只显示双方出拳表情及输赢，约定仍保留在聊天记录和模型上下文中。
- 当前聊天收到用户所发起局的实时结果时自动揭晓一次；刷新、重连补查与后台结果不自动弹窗。
- 揭晓即结束，结果直接留在普通聊天上下文，不再挂起待同步结果或显示输入框提示，也不额外调用模型。下一条消息沿用正常聊天回复规则。
- 启动时兼容上一版尚未同步的结果；这次更新仍需要用户自行重启服务器、刷新页面。

## 2026-10-19 — 娱乐室吹牛骰子与德州扑克

- 娱乐室新增 `/liars-dice` 和 `/texas-holdem`，可选与任一伴侣双人玩，或三个人一起玩，支持日夜主题与手机布局。
- 吹牛骰子每人五颗，输一轮少一颗；默认 1 点百搭，叫过 1 后本轮百搭失效，下轮恢复，也可开局选择不百搭。叫数按「数量增加，或数量相同而点数增加」推进。
- 德州支持过牌、跟注、加注、弃牌、全下、边池、平分与连续发牌；金额模式盲注 ¥0.50/¥1，0 元免费练习则每人 500 筹码、盲注 5/10。
- 每个 AI 回合沿用斗地主的角色上下文与最近群聊构建流程，额外携带最近桌边发言和该玩家可见的牌局。动作和数字由模型决定，服务器校验；对手暗骰、底牌、剩余牌堆不会进入其视图。模型失败时保留回合并显示重试。
- 状态保存于主 SQLite 的 `tabletop_tables`，与钱包出入账共用事务；兼容读取旧 `data/table_liars-dice.json`、`data/table_texas-holdem.json`。回合通过牌局 ID 与版本号防止重复提交或迟到的 AI 回复覆盖新局。
- 轻量验证：`python -m unittest test_tabletop test_doudizhu -q`，并以隔离服务和模拟 AI 完成浏览器操作验收。新增接口需要重启后端、刷新娱乐室；手机如仍显示旧资源，完全关闭 App 后重开。

### 同日补充：牌桌仪式、聊天与语音

- 游戏主界面按手机一屏布局，开局设置与玩法说明展开查看；聊天和每轮过程收进右上角「聊聊」窗口，不再沿主页面向下堆叠。
- 开局/下一轮播放骰盅摇动、骰子翻滚落定；德州洗牌后逐张发底牌，翻牌、转牌、河牌和亮牌分别呈现。动画完成前锁住出招；刷新续局不重复洗牌或摇骰，减少动态效果设置下跳过大幅运动。
- `static/table-voice.js` 复用台球 `billiards/static/voice.js` 的录音流程，支持 Android AionAudio 桥与浏览器录音，松手转写发送、滑开取消；转写仍走 `/api/voice/transcribe`。
- 两种游戏共用 `table-companion.js` 的独立桌边聊天、双方音色、自动 TTS、单句重播与停止朗读，TTS 仍走 `/api/tts`。刷新不会补读旧消息，录音/隐藏页面时停止播放。
- 桌边聊天保存于本桌，不推进游戏版本；模型行动提交时合并最新聊天。聊天发送失败可重试，同条消息不会重复记入；换桌或换轮后的迟到回复不会落入新轮。每次模型调用带最近 24 条桌边消息与本人的可见牌局。
- 音效暂用现有 `/public/掷骰子.mp3` 与 `/public/card/` 的洗牌、出牌、轮到你和胜负提示，在 `table-atmosphere.js` 的 `files` 中集中配置。右上角音符可静音，AI 朗读时压低音效。
- 钱包：开局选择金额，伴侣从各自现有钱包带入，禁止负余额入桌或重复带入。骰子整局结束后每位输家支付约定金额给最终赢家；轮间取消约定可全额退回。德州各自带入不超过余额，用户虚拟钱包上限在开桌时取两位伴侣中较高余额；下注最多使用桌上剩余金额，不够跟注时全下，未跟注部分和边池按实际金额处理。赢来的金额正常留桌，手间收桌时退回各自钱包。用户只记录本桌金额、不新建持久钱包；旧局和 0 元局不影响钱包。
- 每轮自动在最后活跃的日常聊天中留下小战报，包含本轮输赢、金额变化与累计盈亏。骰子未终局时明确待结算；德州区分桌上金额与已退回钱包。复用台球的私聊/群聊路由与同步事件，`tabletop_reports` 保存待投递快照，稳定消息 ID 防止重试重复发卡。
- 需要重启后端并刷新页面。验收使用临时钱包/聊天数据库、模拟 AI、ASR/TTS 与录音桥，不操作实际钱包；手机实机权限和音色仍沿用已有设置。

## 2026-10-20 — 短局与牌桌可读性

- 德州每次发底牌算一手，最多 3 手；翻牌/转牌/河牌仍属同一手。第 3 手结束或只剩一位有余额时自动收桌退回金额，结算面板展示各人带入、累计盈亏、退回金额，以及每手盈亏和轮后余额。可重新打开「本场明细」。
- 新开的吹牛骰子改为 3 轮制：每轮每人重新摇 5 颗，不再淘汰。开骰时只在叫数者与质疑者之间判断胜负，输家付约定金额给赢家，第三位金额不变。开局暂存 3 轮金额，逐轮计入本桌余额，第 3 轮或提前收桌时退回剩余；0 元练习按胜负积分。已有旧骰子局继续原规则，避免中途改变约定。
- 两桌共用更紧凑的绿色桌面，设置放右上角，聊天与按住说话移到底部外层；语音发送后留在牌桌。AI 气泡保留完整文字，可在发言区独立滚动，不再截字。移除骰子桌中央的装饰骰子，叫数改为文字展示。
- 德州跟注明确显示「再付」金额，加注框标注「加注后合计」，旁边实时展示已经支付、本次再付与合计金额。需要重启后端并刷新页面。

### 同日补充：黑金牌室重排

- 两桌按参考图统一为黑金牌室：生成的环境与绿毡素材存于 `public/table-room/`；人物继续读取聊天共用的 `UserIcon.png`、`gropicon1.png`、`codexicon.png`，显示名沿用接口配置。牌室固定暗色以保持素材与控件一致，不修改全站主题偏好。
- 重新组织座位、发言、中央桌面和底部操作区；顶部三个工具等尺寸，底部三个入口等高，语音手势提示独占下一行。手机结算固定三列总账；旧局没有逐轮记录时明确说明，不再显示“完成 0 轮”。
- 圆箭头改为“刷新”，手动刷新显示同步反馈；不重新发牌或摇骰。
- 此次仅静态界面和素材更新，刷新游戏页面生效。采用临时数据库与模拟 AI/语音完成五手德州、三轮骰子、聊天和外层按住说话验收；360×640、390×844、1024×800 检查布局。

### 同日补充：使用自带桌面的新底图

- 使用用户替换的 `public/table-room/lounge-v1.png`，移除界面额外绘制的绿毡和桌沿。背景与公共牌、叫数、座位使用同一视口坐标；长聊天在独立区域滚动，不推动桌面内容。
- 顶部合为标题与工具栏，标题左侧黑桃可返回娱乐室。两款游戏加入聊天浮层的沉浸页面模式，取消父层固定 34px 纯色条；App 内由游戏按钮避让状态栏，底图仍绘制至顶部。返回娱乐室时恢复普通页面留白，无需更改 Android 程序。此变更需要刷新主聊天页，以加载新的浮层逻辑。
- 用模拟 App 状态栏的浏览器检查两桌长消息、公共牌定位、返回导航与小屏结算；实际系统状态栏的图标呈现仍以手机为准。

### 同日补充：仅收桌后发送群聊总战报

- 德州三手或骰子三轮结束收桌后，仅向最近活跃群聊发送一次本场累计输赢与收桌余额；小局结果只留在游戏里。提前收桌同样发送一次总战报。
- 消息目标仅选群聊，不再跟随私聊。收桌记录与待投递战报一起保存，失败可重试，同一桌使用固定消息 ID 防止重复；旧版未发送的小局战报不再补发，已发送消息保留。
- 此项涉及后端，需重启服务器；刷新聊天页面后显示新的“本场结算”卡片标题。

### 2026-10-21：完整场次总账与随时重开

- 只有按规则完整结束的牌桌才向群聊发送一次全员累计输赢及收桌余额。提前结束不发群，旧的未投递小局或提前收桌战报也不再补发。
- 设置可在 AI 思考或任意回合中结束、重开：保留已完成的输赢，未完成的德州手退回各自下注，骰子未开骰的轮次作废；免费练习同样可结束。
- 新桌默认德州带入 ¥200，小盲 ¥5 / 大盲 ¥10；骰子每轮 ¥50（入桌暂存三轮金额）。已有牌桌维持原约定。
- 同伴座位上移，德州底池金额移至公共牌下，轮数移至底部提示旁；桌边聊天的头像、姓名和播放同排，聊天与玩法使用半透明黑底。
- 验证：`python -m unittest test_tabletop -q`；隔离浏览器模拟两桌的结束、AI 思考中重开及手机布局。需重启后端并刷新页面生效。

### 2026-10-21：AGY 与中转聊天的超时修复

- AGY 的无正文进度事件也传递活动信号；收到官方 `SUCCESS` 结果后保存最终正文与用量并清理进程，不再等待输出管道关闭。错误结果、正文不一致和空回复仍报错。
- AGY 上游日志出现过空闲 5 分钟后等待 4 秒重试。私聊、群聊的 AGY 空闲等待单独改为 660 秒，让 CLI 原有 10 分钟期限及 30 秒传输余量先结束；聊天总上限仍为 900 秒，用户取消仍立即清理。其他线路的超时数值不变。此项解决已复现的计时冲突，不证明每次历史失败都由它导致。
- 自定义 OpenAI 中转、AiPro、硅基流动在正常 `stop` 后收到当前非空 `usage` 时完成，Gemini 原生对应 `STOP` 与 `usageMetadata`；先保留该帧正文及用量，再关闭连接。旧的中间用量不作为结束依据，标准 DONE/EOF 及真实传输错误仍沿用原处理。
- 补齐思考帧、被 `<think>` 隐藏的正文帧，以及六个私聊/群聊入口的真实 CLI 状态活动；不发送定时假心跳，不新增自动重试，不切换来源。
- 离线回归覆盖完成后连接保持打开、活动传递、重试窗口、总时限和取消清理；未以真实账号重放私人聊天。后端重启后生效。
