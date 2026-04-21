# Aion Chat 项目档案

## 项目定位
局域网 + 外网（Tailscale 组网）多端同步 AI 聊天程序 + 摄像头智能监控系统。PC/手机浏览器同时使用，支持 PWA 安装为独立 App（全屏无地址栏），数据全部存在本地电脑上。

## 技术栈
- **后端**：Python FastAPI + SQLite (aiosqlite) + WebSocket
- **前端**：多页面架构（原生 JS，无框架），暖光主题，手机/PC 自适应。chat.html 为主聊天页，独立功能页通过 common.css/common.js 共享样式和工具函数
- **摄像头**：OpenCV (`cv2`) DirectShow 后端后台线程采集
- **语音**：WebRTC VAD 语音检测 + 硬基流动 ASR (SenseVoiceSmall) + TTS (CosyVoice2)
- **AI 接口**：硬基流动（OpenAI 兼容）、Google Gemini（REST API）、AiPro 中转站（OpenAI 兼容）
- **Embedding**：Gemini `gemini-embedding-001`（3072维），余弦相似度检索
- **Android App**：Java，WebView + 前台推送服务（OkHttp 4.12.0 WebSocket）+ 原生录音桥 + 原生摄像头桥，compileSdk 34 / minSdk 24
- **音乐**：pyncm（网易云音乐 API，搜索/歌曲详情/音频URL，支持 MUSIC_U Cookie VIP 登录 + 服务端代理推流）
- **EPUB 解析**：ebooklib（EPUB 读取）+ BeautifulSoup4 / lxml（HTML 解析）
- **依赖库**：fastapi, uvicorn, httpx, aiosqlite, opencv-python, Pillow, sounddevice, numpy, webrtcvad-wheels, pyncm, pywin32, psutil, ebooklib, beautifulsoup4, lxml

## 模块化文件结构
项目已从单文件拆分为 12 个模块化文件：
```
项目根目录/
├── 一键启动.bat                  # 双击启动服务（内含绝对路径，搬迁后需修改）
├── 模型预设.txt                  # 模型列表参考
├── public/                       # 公共静态资源
│   ├── BackGround.png
│   ├── icon.png                  # 原始图标（1024x941）
│   ├── icon-192.png              # PWA 图标 192x192（自动生成）
│   ├── icon-512.png              # PWA 图标 512x512（自动生成）
│   ├── AionMonitoralart.mp3      # Core 查看监控前的提示音
│   ├── AIonResponse.mp3          # 语音唤醒回复音频（"诶，我在呢"）
│   ├── UserIcon.png              # 用户聊天头像
│   └── AIIcon.png                # AI 聊天头像
├── AionApp/                      # Android WebView 原生壳（Java，Android Studio 项目）
│   ├── app/src/main/java/com/aion/chat/
│   │   ├── LauncherActivity.java # 启动页：双地址选择（家庭WiFi / Tailscale）+ 记住选择 + 启动推送服务
│   │   ├── WebViewActivity.java  # WebView 主页：全屏加载 chat.html，麦克风权限，前后台状态通知推送服务
│   │   ├── AudioBridge.java      # 原生录音桥：AudioRecord 16kHz → base64 → JS 回调
│   │   ├── CameraBridge.java     # 原生摄像头桥：legacy Camera API → NV21 字节旋转 → JPEG → JS 轮询（绕过 WebView HTTPS 限制）
│   │   └── AionPushService.java  # 前台推送服务：独立 WebSocket 长连接 + 通知弹窗 + 断线重连 + WakeLock/WifiLock 保活
│   └── build.gradle              # compileSdk 34, minSdk 24, Gradle 8.5 + AGP 8.2.2, OkHttp 4.12.0
├── LittleToy/                    # BLE 玩具逆向分析 & 独立 demo
│   ├── toy_control_v4.html       # 独立 BLE 控制页面（可单独使用）
│   └── 逆向分析笔记.md           # SOSEXY 设备协议逆向笔记
└── aion-chat/
    ├── main.py                   # 入口：lifespan、路由注册、静态挂载、WebSocket、PWA 路由、自动记忆总结定时任务
    ├── config.py                 # 全局路径、常量、settings/worldbook/chat_status/cam_config 读写
    ├── database.py               # SQLite 初始化（conversations/messages/memories/schedules/theater 等表 + 性能索引）
    ├── ws.py                     # WebSocket ConnectionManager 单例，含 tts_clients 状态追踪 + _tts_fallback HTTP 回落机制 + client_id 注册/定向推送
    ├── ai_providers.py           # AI 调用：硅基流动/Gemini/AiPro中转站 流式 + 非流式 + 多模态消息构建
    ├── memory.py                 # 向量记忆：embedding、综合评分召回、手动/自动总结、即时哨兵(RAG路由)、原文追溯
    ├── camera.py                 # 摄像头：CameraMonitor 类、Sentinel 分析（注入设备活动摘要）、Core 唤醒、[CAM_CHECK]
    ├── location.py               # 高德地图定位：GPS心跳处理、三级研判、状态机(at_home/outside)、哨兵通知、POI搜索
    ├── voice.py                  # 语音唤醒 + 半双工通话（WebRTC VAD + 硬基流动 ASR），通话中自动携带 TTS 参数
    ├── tts.py                    # 服务端流式 TTS：按句切分（100-200字）+ 异步并行合成 + WebSocket/SSE 推送音频分片
    ├── schedule.py               # 日程/闹铃/定时监控管理器：ScheduleManager、文本指令解析、闹铃触发Core唤醒、定时监控截图+Core分析（注入设备活动摘要）
    ├── ghost_forest.py            # 奥罗斯幽林 TRPG 引擎：会话管理、AI 对话历史压缩、D20 骰子判定、角色属性/道具系统
    ├── book.py                    # EPUB 解析模块：书籍导入、章节拆分、段落标注、图片提取
    ├── routes/
    │   ├── __init__.py
    │   ├── book.py               # 阅读功能 API：书籍上传/列表/章节/进度/删除/图片/AI批注（单段+全章SSE）
    │   ├── theater.py            # 小剧场 API：独立对话CRUD、消息CRUD、角色CRUD、SSE流式回复（无记忆/系统能力注入）+ TTS
    ├── chat.py               # 对话/消息 CRUD、send_message(SSE)、regenerate、cam-check-trigger、[MUSIC:xxx]/[ALARM:...]/[REMINDER:...]/[Monitor:...]/[TOY:x]/[查看动态:n]/[视频电话] 检测
    │   ├── music.py              # 音乐搜索/详情/播放/代理推流 API（pyncm）
    │   ├── schedule.py           # 日程 CRUD API（列表/添加/删除）
    │   ├── cam.py                # 摄像头控制 + 监控日志 API
    │   ├── location.py           # 定位 API：心跳上报、状态查询、POI搜索、配置管理、设置家位置
    │   ├── files.py              # 上传、聊天记录文件导出/管理
    │   ├── settings.py           # 设置、世界书、模型列表、TTS 代理、视频通话开关
    │   ├── memories.py           # 记忆库 CRUD + 手动总结触发 + 原文查看 + 锚点管理 API
    │   ├── heart_whispers.py     # 心语 API（列表查询 + 删除）
    │   ├── activity.py           # 活动日志 API（上报/查询/清理/状态诊断/10分钟摘要/AI联动开关配置）
    │   ├── voice.py              # 语音唤醒/通话控制 API
    │   └── ghost_forest.py       # 奥罗斯幽林 TRPG API（16 个端点：人设/会话/剧情生成/选择/骰子/大结局）+ SSE 流式 TTS
    ├── activity.py               # 设备活动日志：JSONL 存储、自动清理（保留最近 3 小时）、PC 前台窗口采集（win32gui+psutil）、App 包名→中文名映射、10分钟窗口摘要（时长权重+carry-forward状态追溯）、AI联动开关+Prompt摘要生成
    ├── music.py                  # pyncm 封装层（搜索/歌曲详情/音频URL/MUSIC_U Cookie 登录/匿名登录）
    ├── README.md                 # 本文件
    ├── 监控流程.md               # Sentinel/Core 架构设计文档
    ├── static/
    │   ├── home.html             # 手机风格主页 → /（应用图标网格 + Dock 栏）
    │   ├── chat.html             # 主聊天页 → /chat（含语音唤醒/TTS/BLE/音乐/系统日志/debug面板）
    │   ├── common.css            # 子页面共享样式（CSS变量/布局/组件/闹铃弹窗/toast）
    │   ├── common.js             # 子页面共享工具（api()/WS连接/闹铃弹窗/系统通知）
    │   ├── settings.html         # 设置页 → /settings（API Key 管理）
    │   ├── worldbook.html        # 世界书页 → /worldbook（AI/用户人设编辑）
    │   ├── memory.html           # 记忆库页 → /memory（CRUD/搜索/总结/锚点/原文追溯）
    │   ├── schedule.html         # 日程管理页 → /schedule（列表/添加/删除）
    │   ├── camera.html           # 摄像头页 → /camera（预览/缩放/监控开关/配置）
    │   ├── monitor-logs.html     # 监控日志页 → /monitor-logs（按日期查看/实时WS推送）
    │   ├── location.html         # 定位页 → /location（状态/POI/配置）
    │   ├── heart-whispers.html   # 心语页 → /heart-whispers（AI秘密日记查看/删除）
    │   ├── activity-logs.html    # 活动日志页 → /activity-logs（双设备活动查看/筛选/清理/10分钟摘要弹窗/AI联动开关）
    │   ├── reading.html          # 阅读页 → /reading（书架+阅读器+AI批注+选文聊天+音乐播放）
    │   ├── theater.html          # 小剧场页 → /theater（独立聊天+多角色管理+TTS，茶色暗色主题）
    │   ├── ghost-forest.html     # 奥罗斯幽林页 → /ghost-forest（TRPG 游戏：D20 骰子+角色扮演+AI DM）
    │   ├── video-call.js         # 视频通话模块：摄像头预览 + 截图 + 语音复用 + 来电/去电 UI
    │   ├── manifest.json         # PWA Web App Manifest（从 /manifest.json 提供）
    │   └── sw.js                 # PWA Service Worker（从 /sw.js 提供）
    └── data/                     # ★ 备份只需复制此文件夹
        ├── chat.db               # SQLite 数据库
        ├── settings.json         # API Key 持久化
        ├── worldbook.json        # 世界书（AI/用户人设+名称）
        ├── cam_config.json       # 摄像头监控配置
        ├── chat_status.json      # 聊天状态摘要（供哨兵参考）
        ├── location_config.json  # 定位配置（高德Key、家坐标、开关、安静时段、阈值等）
        ├── location_status.json  # 定位状态缓存（当前坐标、状态、地址、天气、POI等）
        ├── digest_anchor.json    # 总结锚点（记录上次总结到哪条消息的时间戳）
        ├── uploads/              # 上传的图片/视频
        ├── chats/                # 导出的 .md 聊天记录 + _index.json
        ├── screenshots/          # 摄像头截图（自动清理）
        ├── monitor_logs/         # Sentinel 监控日志（JSONL，按日期，3天自动清理）
        ├── activity_logs/        # 设备活动日志（JSONL，按日期，保留最近 3 小时）
        ├── books/                # EPUB 书籍数据（解析后的章节+图片+批注数据库）
        ├── theater_personas.json # 小剧场角色预设（多套人设，JSON数组）
        └── ghost_forest/          # 奥罗斯幽林 TRPG 数据
            ├── _personas.json     # DM/玩家人设预设
            └── {uuid}.json        # 游戏会话存档（每局一个文件）
```

## 路由
| 路径 | 说明 |
|------|------|
| `/` | home.html 手机风格主页（应用图标启动器） |
| `/chat` | chat.html 主聊天页 |
| `/settings` | settings.html 设置页（API Key 管理） |
| `/worldbook` | worldbook.html 世界书页 |
| `/memory` | memory.html 记忆库页 |
| `/schedule` | schedule.html 日程管理页 |
| `/camera` | camera.html 摄像头监控页 |
| `/monitor-logs` | monitor-logs.html 监控日志页 |
| `/location` | location.html 定位地图页 |
| `/heart-whispers` | heart-whispers.html 心语页（AI秘密日记） |
| `/activity-logs` | activity-logs.html 活动日志页（双设备活动查看） |
| `/reading` | reading.html 阅读页（书架+阅读器+AI批注+选文聊天） |
| `/theater` | theater.html 小剧场页（独立聊天+多角色+TTS） |
| `/ghost-forest` | ghost-forest.html 奥罗斯幽林页（TRPG 冒险游戏） |
| `/manifest.json` | PWA Web App Manifest |
| `/sw.js` | PWA Service Worker（根路径提供，作用域覆盖全站） |
| `/public/*` | 公共资源 |
| `/static/*` | 静态文件 |
| `/uploads/*` | data/uploads/ |
| `/api/*` | 后端 API |
| `/ws` | WebSocket 多端同步 |

## 支持的模型
### 硅基流动（api.siliconflow.cn）
- GLM-5 → `Pro/zai-org/GLM-5`
- GLM-4.7 → `Pro/zai-org/GLM-4.7`
- Kimi-K2.5 → `Pro/moonshotai/Kimi-K2.5`

### Gemini（generativelanguage.googleapis.com）
- gemini-3.1-flash-lite → `gemini-3.1-flash-lite-preview`（Sentinel / poll_digest 默认模型）
- gemini-2.5-pro → `gemini-2.5-pro`
- gemini-3-flash → `gemini-3-flash-preview`（聊天默认模型）
- gemini-3.1-pro → `gemini-3.1-pro-preview`

### AiPro 中转站（vip.aipro.love）
- claude-sonnet-4-6 → `claude-sonnet-4-6`
- claude-opus-4-6 → `claude-opus-4-6`

### 哨兵/向量模型（支持独立 Gemini Free Key）
- Sentinel 哨兵分析 → `gemini-3.1-flash-lite-preview`
- 向量 Embedding → `gemini-embedding-001`（3072维）
- 即时哨兵 → `gemini-3.1-flash-lite-preview`
- 记忆总结（手动/自动） → 当前聊天对话的核心模型（跟随用户选择）

哨兵和向量模型支持配置独立的 Gemini Free Key，留空则自动复用主 Gemini Key。

## 已实现功能

### 聊天核心
1. **对话管理** — 创建/删除/重命名对话，默认日期命名，侧栏显示每个对话的消息条数
2. **消息 CRUD** — 发送/编辑（气泡内 inline）/删除/复制，⋯ 点阵菜单
3. **流式 AI 回复** — SSE 流式输出，逐字显示，等待时显示「思考中」/「正在输入」循环动画 + 弹跳小圆点
4. **重新生成** — AI 消息可一键重新生成
5. **多端实时同步** — WebSocket 广播，PC/手机任一端操作实时同步
6. **上下文长度控制** — 滑块 1-100 条可调，默认 20
7. **世界书（World Book）** — AI/用户人设 + 自定义名称，注入 prompt 前缀
8. **图片/视频上传** — 多模态支持，Gemini 用 inline_data，硅基流动用 URL
9. **聊天记录文件管理** — 自动导出 .md，文件管理器弹窗查看/下载/删除
10. **API Key 管理** — 界面内设置面板，支持 Gemini + Gemini Free（哨兵+向量）+ 硅基流动 + 中转站 四组 Key
11. **手机适配** — 侧栏抽屉式展开，聊天气泡布局，触屏友好，`@media (max-width: 768px)` 单独优化紧凑间距
62. **聊天头像** — 用户/AI 消息旁显示圆形头像（`public/UserIcon.png` / `public/AIIcon.png`），用户右侧、AI 左侧
63. **多气泡拆分** — AI 回复中 `\n\n` 自动拆分为多个独立消息气泡，像微信连发效果，流式输出实时拆分
64. **时间内联显示** — 消息时间显示在用户/AI 名字旁边，不再独占一行
12. **当前时间注入** — 每次发消息/重新生成时，将准确时间拼接到 prompt 前缀最后一条 assistant 回复中

### Debug 透明度面板
13. **Token 用量追踪** — Gemini usageMetadata + 硅基流动 usage，在 SSE 流中通过 debug 事件传回前端
14. **记忆召回可视化** — 每次发消息时召回的记忆条目，显示综合分数 + 三维分解（vec_sim / kw_score / importance）
15. **完整 Prompt 查看** — 可展开查看发送给模型的全部 prompt 消息（截断前500字/条）
16. **Debug 条** — 每条 AI 消息下方显示：模型名、输入/输出/总 token、召回记忆数，点击展开详情

### 向量记忆库（RAG 重构）
17. **记忆总结（手动 + 自动）** — 手动：用户点击「总结新记忆」按钮触发（无最低条数限制）。自动：每 30 分钟检测，若用户已 30 分钟未对话且未总结消息 ≥ 30 条则自动触发。两者共用同一套逻辑和锚点，不会重复总结。从锚点之后的消息开始，每 30 条一组串行处理（余数 <10 合并到最后一组），使用当前聊天的核心模型（而非 flash-lite）提取结构化记忆（含关键词 + 重要度 0-1 + unresolved 判断），Prompt 注入世界书 AI/用户人设使记忆更具个人视角。每组成功后更新锚点。全部总结完成后，带最近 30 条聊天上下文 + 人设再调用一次核心模型，生成一句感慨/吐槽，作为 assistant 消息插入聊天（前置一条「🧠 AI整理了记忆库」系统胶囊）
18. **即时哨兵（instant_digest）** — 每次用户发消息时自动调用 flash-lite 分析最近对话，返回结构化 JSON：`{is_search_needed, keywords, require_detail, status, topic}`，决定是否需要搜索记忆、是否需要追溯原文细节，同时提供 topic 用于背景记忆浮现
19. **向量化存储** — 使用 Gemini `gemini-embedding-001`（3072维）将记忆向量化，存入 SQLite memories 表，每条记忆含 keywords（JSON 关键词数组）、importance（重要度）、source_start_ts/source_end_ts（来源时间范围）、unresolved（是否待办/未完成）
20. **综合评分召回** — `final_score = vec_sim × 0.6 + kw_score × 0.3 + importance × 0.1`，threshold=0.45，Top 5。关键词匹配支持子串模糊命中
21. **原文追溯（fetch_source_details）** — 当 `require_detail=true` 时，在召回记忆的 source 时间范围内按关键词筛选原始对话记录，去重后按时间排序，拼接注入 prompt
22. **总结锚点管理** — 锚点持久化在 `data/digest_anchor.json`，UI 显示当前锚点时间 + 日期选择器可回退
23. **可视化管理** — 侧边栏「🧠 记忆库」按钮，支持搜索/添加/编辑/删除，编辑后自动重新向量化。每条记忆显示关键词标签 + 重要度分数，编辑时可修改关键词和重要度。有 source 时间范围的记忆可点击 📜 查看原文。📌 按钮可切换记忆的“待办/未完成”状态，unresolved 的记忆以橙色高亮显示

### 语音合成 (TTS) — 服务端流式推送架构
24. **服务端流式 TTS** — AI 流式输出过程中，后端 `tts.py` 的 `TTSStreamer` 实时按句切分文本（100-200 字，按句号/问号/感叹号/换行等断句），每句异步调用硅基流动 CosyVoice2-0.5B 合成 mp3，合成完成后立即通过 WebSocket 或 SSE 推送 `tts_chunk` 事件给前端，前端收到即可开始播放，无需等待全文生成完毕。SSE 模式通过 `sse_queue` 参数支持，用于小剧场和奥罗斯幽林等独立 SSE 流场景
25. **多场景触发** — 用户发消息后的 AI 流式回复、重新生成、Core 主动发言（哨兵唤醒/闹铃/定时监控/[CAM_CHECK] 跟进）均自动创建 TTSStreamer 进行流式合成
26. **音色选择** — 齿轮配置面板内选择硅基流动账号下的自定义音色，通过 WebSocket `tts_state` 消息同步到服务端
27. **前端队列播放** — 前端维护 `ttsQueue`（Map 结构，key 为 msg_id），每条消息的分片按 seq 顺序播放，多条消息按到达顺序排队；播放完最后一片后服务端广播 `tts_done` 事件，前端清理队列并继续下一条
28. **多端 TTS 状态同步** — 前端开启 TTS 后通过 WebSocket 发送 `tts_state` 消息（含 enabled/voice），服务端 `ConnectionManager` 在 `tts_clients` 字典中跟踪各客户端状态；同时 HTTP POST（send_message/regenerate）的 body 中也携带 `tts_enabled`/`tts_voice` 作为 `_tts_fallback` 回落，确保服务端发起的消息（cam_check/闹铃/监控）也能获取 TTS 状态
28b. **TTS 音频缓存** — 合成的音频分片存储在 `data/tts_cache/` 目录，文件名为 `{msg_id}_s{seq}.mp3`，前端可通过 `/api/tts/audio/{chunk_name}` 获取；点击 AI 消息的 🔊 图标可重播已缓存的 TTS 分片
28c. **TTS 重播** — 点击聊天气泡下的喇叭图标，前端通过 HEAD 请求探测 `{msg_id}_s0`、`{msg_id}_s1`... 是否存在，依次播放所有分片；支持 GET 和 HEAD 两种 HTTP 方法

### 摄像头智能监控（Sentinel/Core 双脑架构）
28. **摄像头集成** — OpenCV DirectShow 后端，支持多摄像头切换，绿屏检测，智能预热验证
29. **Sentinel 哨兵** — 定时截图后由轻量模型（flash-lite）分析，注入设备活动摘要（近 60 分钟 6 条）作为辅助判断依据，输出结构化 JSON（含概况摘要 summary + 唤醒原因 core_reason）
30. **Core 唤醒** — Sentinel 判断需要时唤醒 Core（当前聊天模型），Core 收到哨兵摘要+唤醒原因+最近5条日志+记忆召回，主动在对话中联系用户
31. **监控日志系统** — 独立于聊天的 JSONL 日志，按日期存储，3 天自动清理
32. **日志查看器** — 侧边栏「📜 监控日志」按钮，按日期浏览，显示概况摘要和唤醒原因，WebSocket 实时推送新日志
33. **聊天状态摘要（chat_status）** — 即时哨兵提取，存储在 `data/chat_status.json`，监控哨兵分析时自动注入

### Core 主动查看监控（[CAM_CHECK]）
34. **[CAM_CHECK] 指令** — 摄像头开启时，prompt 中注入能力提示，Core 可在回复中输出 `[CAM_CHECK]` 指令主动请求查看监控画面
35. **前端实时过滤** — 流式输出时前端实时 strip `[CAM_CHECK]`，用户看不到原始指令
36. **提示音 + 5秒延迟** — 检测到指令后前端播放 `AionMonitoralart.mp3` 提示音，等待 5 秒给用户反应时间，然后再截图
37. **加载指示器** — 等待期间在 AI 消息下方显示「📷 {AI名} 正在查看监控 ● ● ●」弹跳动画，5秒后自动移除（renderMessages 重建后自动恢复）
38. **后台截图+AI分析** — 5秒后前端 POST `/api/cam-check-trigger`，后端截图并调用 Core 模型分析画面，结果作为新 assistant 消息保存并 WebSocket 广播
39. **摄像头离线处理** — 若摄像头未开启，后端发 `cam_offline` SSE 事件，前端显示「📷 摄像头未开启，Core无法查看监控信息」提示
40. **两套系统独立** — 哨兵定时监控和 Core 主动查看是完全独立的两套系统，互不影响。关闭哨兵不影响 Core 主动查看，反之亦然

### Core 主动查看设备动态（[查看动态:n]）
40a. **[查看动态:n] 指令** — AI联动开关开启时，prompt 中注入能力提示，Core 可在回复中输出 `[查看动态:n]`（n=1~12，对应 10~120 分钟）主动查看用户设备使用动态
40b. **n 值安全** — n 自动 clamp 到 1-12 范围，无效值默认为 6（60 分钟），避免异常请求
40c. **前端实时过滤** — 流式输出时前端实时 strip `[查看动态:n]`，用户看不到原始指令
40d. **加载指示器** — 检测到指令后在 AI 消息下方显示「📊 {AI名} 正在查看你的动态 ● ● ●」紫色主题弹跳动画（30 秒安全超时自动移除）
40e. **后台摘要+AI分析** — 后端获取设备活动摘要，组装 Prompt（人设+上下文+摘要）调用 Core 生成关怀/评论回复，结果作为新 assistant 消息保存并 WebSocket 广播
40f. **系统消息** — 查看动态后自动插入 system 消息「{AI名}查看了{用户名}过去N分钟的动态」，纳入上下文关键词匹配
40g. **与摄像头监控独立** — 查看动态仅读取设备活动日志，不依赖摄像头，两套系统完全独立

### Sentinel/Core 工作流程
```
【哨兵定时监控】
  定时截图 → 获取设备活动摘要（近 60 分钟 6 条）→ Sentinel(flash-lite) 分析 → 输出 JSON:
    {
      "monitoringlog": "观察描述...",
      "summary": "这段时间的概况摘要...",
      "call_core": true/false,
      "core_reason": "唤醒原因（仅call_core=true时）"
    }
    ↓ 日志写入 monitor_logs/YYYY-MM-DD.jsonl + WebSocket 广播
    ↓ (如果 call_core = true)
    组装提示词（含唤醒原因 + 概况摘要 + 最近5条日志 + 世界书 + 聊天上下文 + 记忆召回）
    → Core(当前模型) 生成回复 → 作为 assistant 消息插入对话

【Core 主动查看监控 [CAM_CHECK]】
  Core 回复包含 [CAM_CHECK] → 后端检测并发 SSE 事件 + WebSocket 广播 → 前端播放提示音
  → 等待 5 秒 → POST /api/cam-check-trigger → 后端截图
  → 带人设+上下文+图片调用 Core → 结果作为新 assistant 消息保存+广播
```

### 语音唤醒 + 半双工通话
41. **语音唤醒** — 聊天配置面板开关，支持自定义唤醒词，后台持续监听麦克风
42. **WebRTC VAD** — 使用 Google WebRTC VAD 频谱分析检测人声，不靠音量阈值，嗰杂环境（狗叫/风扇/空调）也能稳定工作
43. **半双工通话模式** — 唤醒后进入通话：用户说话 → ASR 识别 → 发送到聊天 → AI 回复 + TTS 播放 → 轮到用户说话，循环往复
44. **麦克风协调** — AI 说话（TTS 播放 / [CAM_CHECK] 处理）期间暂停录音，服务端 `tts_done` 事件触发前端 `notifyVoiceAiSpeaking(false)` 自动恢复录音；voice.py 发送消息时自动携带 `tts_enabled`/`tts_voice` 参数
45. **语音挂断** — 说“再见/拜拜/挂断”自动挂断通话，继续监听唤醒词；60 秒无人说话自动挂断
46. **唤醒回复音频** — 唤醒成功后播放 `public/AIonResponse.mp3`（“诶，我在呢”）
47. **通话状态指示器** — 前端顶部实时显示：等待唤醒 / 聆听中 / AI 思考中 / 通话结束，含挂断按钮
48. **完整功能集成** — 语音发送的消息与手动发送完全一致：有 debug 信息、记忆召回、[CAM_CHECK] 能力

### 语音唤醒工作流程
```
【监听待命】
  WebRTC VAD 持续检测 → 检测到人声 → 录音 → 静音截断 → ASR 识别
  → 文本包含唤醒词？ → 是 → 进入通话模式

【通话模式（半双工）】
  播放唤醒回复音频 → 等待 AI 说完
  → 循环：
    ├ 录音（VAD 检测 + 1.5 秒静音截断）
    ├ ASR 识别
    ├ 检查挂断关键词 → 是 → 发送消息 + 挂断 → 回到监听待命
    └ 发送消息到聊天 → AI 回复 + TTS → 暂停麦克风 → TTS 播完 → 继续录音
  → 60 秒无人说话 → 自动挂断 → 回到监听待命

【状态同步】
  voice.py 通过 WebSocket 广播 voice_state 事件
  voice.py 发送消息时携带 tts_enabled/tts_voice 参数 → 服务端创建 TTSStreamer
  → TTS 分片通过 WebSocket tts_chunk 推送 → 前端按序播放
  → 全部合成完毕 → 服务端广播 tts_done → 前端 notifyVoiceAiSpeaking(false) → 恢复录音
  [CAM_CHECK] 触发时通知后端保持 AI 说话状态
```

### 视频通话（[视频电话]）
204. **视频通话模式** — 在语音唤醒基础上增加摄像头画面，实现「看到你 + 听到你 + 跟你说话」的完整通话体验。功能本质是语音通话 + 摄像头截图，不涉及 WebRTC 实时音视频流
205. **用户主动发起** — 聊天页面 📹 按钮发起视频通话，3 秒等待期间播放铃声动画，用户可取消
206. **AI 主动发起** — AI 回复包含 `[视频电话]` 指令时，后端延迟 10 秒后通过 WebSocket 定向推送给发送者客户端，前端弹出来电 UI（铃声 + 接听/挂断按钮）
207. **来电指示器** — AI 回复触发视频通话时，消息底部显示「📹 AI 正在发起视频通话...」动画指示器，10 秒后自动消失并触发来电 UI
208. **摄像头预览** — 全屏通话界面显示用户摄像头画面（主画面）和 AI 头像（画中画），支持点击切换大小画面位置
209. **摄像头截图** — 每条用户语音消息发送时自动截取当前摄像头画面，以 base64 JPEG 附加到消息中，AI 可以看到用户
210. **前后摄像头切换** — 通话界面 🔄 按钮支持前后摄像头切换
211. **语音复用** — 通话中的语音录音/ASR/TTS 完全复用现有语音唤醒系统，使用相同的 1.5 秒静音截断 + VAD 检测
212. **Android 原生摄像头桥** — WebView 非安全上下文（HTTP 局域网）无法调用 `getUserMedia({video})`，通过 `CameraBridge.java` 使用 legacy Camera API 采集预览帧，NV21 纯字节数组旋转（不经过 Bitmap），后台线程 JPEG 编码，`setPreviewCallbackWithBuffer` 避免 GC 冻结，JS 通过 `requestAnimationFrame` 轮询 `getFrame()` 获取已旋转的 base64 JPEG
213. **开关控制** — 聊天配置面板中「视频通话」开关控制 AI 是否具有发起视频通话的能力（`[视频电话]` 指令是否注入 prompt），用户主动发起不受开关影响
214. **前端指令过滤** — 流式输出时前端实时 strip `[视频电话]`，用户看不到原始指令
215. **消息保存** — 通话中的语音消息正常保存到聊天记录，截图作为图片附件一并存储
216. **Chrome 兼容** — PC/手机 Chrome 浏览器使用标准 `getUserMedia` API 获取摄像头，无需原生桥；Android WebView 自动 fallback 到 `CameraBridge`

### 视频通话工作流程
```
【用户主动发起】
  点击 📹 按钮 → 3 秒等待（播放铃声动画）→ 进入通话界面
  → 启动摄像头（getUserMedia 或 AionCamera 原生桥）
  → 启动语音录音（复用语音唤醒 AudioBridge）
  → 循环：
    ├ VAD 检测 + 1.5 秒静音截断 → ASR 识别
    ├ 截取摄像头画面 → base64 JPEG 附加到消息
    ├ 发送消息到聊天（含截图）→ AI 回复 + TTS
    └ TTS 播完 → 继续录音
  → 挂断 → 停止摄像头 + 录音

【AI 主动发起】
  AI 回复包含 [视频电话] → 后端检测 → 从显示文本 strip
  → 消息底部显示「📹 正在发起视频通话...」指示器
  → 10 秒延迟 → WebSocket 定向推送 video_call_ring 给发送者
  → 前端弹出来电 UI（铃声 + 接听/挂断）
  → 用户接听 → 进入通话界面（同上）
  → 用户挂断/超时 → 取消

【Android 原生摄像头桥（CameraBridge.java）】
  JS 调用 AionCamera.start("user"|"environment")
  → Camera.open() → 设置 640×480 NV21 预览
  → setPreviewCallbackWithBuffer（3 个预分配 buffer，零 GC）
  → 摄像头回调 → 复制数据到 inputBuf → 归还 camera buffer
  → 后台 ExecutorService 线程：
    ├ NV21 纯字节数组旋转（rotateNV21_CW90/270/180，~1ms）
    ├ YuvImage.compressToJpeg（已旋转的竖屏 JPEG）
    └ Base64 编码 → 更新 lastFrameB64
  → JS requestAnimationFrame 轮询 getFrame() → 更新 <img>.src
```

### 音乐点歌（[MUSIC:xxx]）
65. **AI 自主点歌** — AI 在回复中输出 `[MUSIC:歌曲名 歌手名]` 指令，后端自动检测并通过 pyncm 搜索网易云音乐
66. **音乐卡片** — 搜索结果以卡片形式展示：封面、歌名、歌手、专辑 + 最多 3 首候选歌曲
67. **双播放模式** — 每首歌提供「网易云播放」（跳转 `music.163.com` 网页）和「在线播放」（页内 `<audio>` 播放器，走服务端代理推流 `/api/music/stream/{id}`）
68. **在线播放器** — 固定顶部的音频播放条，支持播放/暂停、进度拖拽和关闭按钮
69. **前端实时过滤** — 流式输出时前端实时 strip `[MUSIC:xxx]`，用户看不到原始指令
70. **多端同步** — 音乐卡片通过 SSE + WebSocket 双通道广播，语音通话触发的点歌也能在所有端展示
71. **VIP 登录** — 支持在设置面板配置网易云 `MUSIC_U` Cookie，以 VIP 身份登录 pyncm，可播放付费/VIP 歌曲；未配置时退回匿名登录
71b. **服务端代理推流** — 新增 `/api/music/stream/{song_id}` 路由，后端实时获取网易云 CDN URL 并通过 httpx 流式转发音频给前端，解决防盗链和 CDN 链接过期问题，手机端也能稳定播放
71c. **自动播放** — AI 点歌后浏览器自动开始播放第一首歌曲，无需用户手动点击；闹铃/定时监控触发时 AI 也可点歌并自动播放，实现音乐闹钟效果
71d. **闹铃/监控点歌能力** — 闹铃触发和定时监控触发时的 Prompt 中注入 `[MUSIC:xxx]` 等系统能力指令，AI 可在提醒回复中主动点歌

### 音乐点歌工作流程
```
【AI 触发点歌（正常聊天 / 闹铃 / 定时监控）】
  用户聊天提到想听歌 / 闹铃触发 AI 决定点歌
  → AI 回复包含 [MUSIC:歌曲名 歌手名]
  → 后端 regex 检测 [MUSIC:xxx] → pyncm 登录（MUSIC_U VIP 或匿名）+ 搜索
  → 取第一首为主推荐 + 获取音频 URL → 剩余作为候选
  → SSE/WebSocket 发送 music 事件（含卡片数据）
  → 前端渲染音乐卡片 + 自动播放第一首
  → [MUSIC:xxx] 从显示文本和数据库中 strip 掉

【浏览器播放（服务端代理推流）】
  前端 <audio>.src = /api/music/stream/{song_id}
  → 后端实时调用 pyncm 获取 CDN URL
  → httpx 流式代理转发（带 Referer 头绕过防盗链）
  → 前端播放，手机/PC 均走此路径

【用户手动选择】
  ├ 「网易云播放」→ window.open("https://music.163.com/song?id={id}")
  └ 「在线播放」→ /api/music/stream/{id} 代理推流播放
```

### 日程 / 闹铃 / 定时监控系统
72. **AI 设置日程** — AI 在回复中使用文本指令创建日程/闹铃，格式：`[ALARM:2026-03-25T10:00|叫用户参加聚会]`、`[REMINDER:2026-04-09|还信用卡]`
73. **AI 删除日程** — AI 输出 `[SCHEDULE_DEL:日程id]` 删除指定日程/闹铃/定时监控
74. **日程列表注入** — 每次对话时将当前活跃日程列表注入到 Prompt 的 `[系统能力]` 区块，AI 可自然提起日程提醒
75. **闹铃自动触发** — 后台 ScheduleManager 每 30 秒扫描到期闹铃，组装完整 Prompt（世界书+记忆+上下文+当前时间+活跃日程列表）调用 Core 生成提醒回复
76. **前端弹窗** — 闹铃触发时所有连接的前端弹出全屏遮罩弹窗（脉冲动画），必须用户点击「确认」才关闭，支持多条闹铃排队
77. **多端同步** — 闹铃弹窗通过 WebSocket 广播到所有连接的客户端
78. **持久化** — 所有日程存储在 SQLite schedules 表，服务器重启后自动恢复，遗漏的闹铃立即补触发
79. **用户手动管理** — 侧边栏「📅 日程管理」面板支持手动添加/删除日程
80. **前端指令过滤** — 流式输出时实时 strip `[ALARM:...]`、`[REMINDER:...]`、`[Monitor:...]`、`[SCHEDULE_DEL:...]`、`[SCHEDULE_LIST]`，用户不可见
81. **容错设计** — AI 输出格式错误时静默跳过，不影响正常聊天
82. **系统消息** — 日程创建/删除操作后自动插入 system 消息到聊天（如「📅 已创建闹铃：2026-03-25 10:00 — 叫用户参加聚会」），风格与哨兵唤醒消息一致
83. **日期格式容错** — 支持 `2026-03-25T10:00`、`2026-03-25 10:00`、`2026-03-25`（仅日期默认 09:00）、`2026/3/25` 等多种格式
84. **时间显示优化** — 所有日程时间展示均使用空格分隔（`2026-03-25 10:00`），不显示 ISO 格式的 `T`
85. **时间格式归一化** — 前端手动创建和 AI 指令创建的日程均统一将 `T` 分隔符转为空格后存储，确保时间比较一致

### 定时监控（[Monitor:...]）
86. **[Monitor:...] 指令** — AI 可在回复中输出 `[Monitor:YYYY-MM-DDTHH:MM|内容]`，设定定时截图监控任务，例如检查用户是否去睡觉、是否在运动等
87. **日程类型 `monitor`** — 存储在 schedules 表，类型为 `monitor`，在日程管理面板显示为「👁 监督」（紫色标签），用户可手动添加/删除
88. **提示音 + 5秒延迟** — 触发时先通过 WebSocket 广播 `monitor_alert`，前端播放 `AionMonitoralart.mp3`，等待 5 秒给用户反应时间，然后再截图
89. **截图 + Core 分析** — 到时间后自动截取摄像头画面，组装 Prompt（人设+上下文+日程列表+截图+监控目的+设备活动摘要（近 120 分钟 12 条））调用 Core 生成回复，不经过哨兵模型，不召回记忆库
90. **截图双存** — 截图同时保存到 `data/uploads/` 和 `data/screenshots/`
91. **摄像头离线处理** — 若触发时摄像头未开启，插入系统消息「定时监控触发失败：摄像头未开启」，不发送给 Core
92. **AI 可取消** — 复用 `[SCHEDULE_DEL:id]` 指令取消未触发的定时监控，例如用户提前完成了任务时 AI 自主取消
93. **与哨兵独立** — 定时监控与 Sentinel 哨兵完全独立，关闭哨兵不影响定时监控，反之亦然

### 密语时刻（BLE 玩具控制）
94. **BLE 玩具集成** — 通过 Web Bluetooth API 在手机 Chrome 上连接 SOSEXY BLE 情趣玩具，完整实现 sendData2 封包协议（前缀 00 + 分包 + 包头）
95. **密语模式开关** — 侧边栏「💗 密语时刻」按钮打开浮层面板，包含 BLE 连接/断开、密语模式开关、手动预设网格、停止按钮、BLE 日志
96. **AI 控制玩具** — 开启密语模式后，AI 的 prompt 注入 `[TOY:1]`~`[TOY:9]` 和 `[TOY:STOP]` 能力，AI 可根据对话氛围自主控制玩具档位
97. **9 级预设模式** — 微风轻拂、春水初生、暗流涌动、如梦似幻、情潮渐涨、烈焰焚身、极乐之巅、魂飞魄散、失控，每个预设控制 3 个马达（震动/电流/吮吸）的模式+速度
98. **前端指令过滤** — 流式输出时实时 strip `[TOY:x]`，用户看不到原始指令
99. **系统消息胶囊** — AI 触发玩具指令后自动插入居中系统消息：「❤️ {AI名} · 心动3 · 暗流涌动」或「❤️ {AI名} 停止了玩具」
100. **BLE 连接保持** — 控制面板嵌入 chat.html 内部浮层，关闭面板后 BLE 连接不断，正常聊天时 AI 指令仍可直接控制玩具
101. **设备过滤** — BLE 扫描仅显示名称以 SOSEXY 开头的设备
102. **多端同步** — 玩具指令通过 SSE + WebSocket 双通道广播至所有连接的客户端
103. **主页快捷入口** — home.html 「密语时刻」图标点击跳转 `/chat?whisper=1`，自动弹出密语面板

### 背景记忆浮现（替代旧版“近期记忆注入”）
104. **智能背景记忆浮现** — 每次发消息/重新生成时，通过三层策略构建背景记忆（最多 8 条）：① unresolved 记忆优先（最多 2 条，待办/未完成的事项）→ ② 话题相关浮现（用即时哨兵的 topic 做 embedding 匹配，Top 3）→ ③ 近期补充（最近 3 天，补满 8 条）。与 RAG 精确召回自动去重
105. **Unresolved 标记** — 记忆表新增 `unresolved` 字段，标记悬而未决的计划/约定/承诺。总结时核心模型自动判断，UI 中可通过 📌 按钮手动切换。unresolved 记忆在背景记忆中以 📌 前缀注入，确保 AI 记得追问

### 高德地图定位系统

#### 核心模块
129. **GPS 心跳上报** — Android APK 每 10 分钟通过 `LocationManager` 获取 GPS 坐标（WGS84），POST 到 `/api/location/heartbeat`，服务端自动转换为 GCJ-02（高德坐标系）
130. **三级心跳研判** — 服务端对每次心跳执行三级处理，逐级递进，最大程度节省 API 调用：
   - **轻量级（lightweight）**：仅保存坐标，不调 API。条件：移动距离 < `movement_threshold`（默认 500m）且已有缓存地址
   - **刷新级（refresh）**：调用高德逆地理编码 + 天气 API 更新地址/天气。条件：移动超过阈值 或 无缓存地址
   - **完整级（full）**：刷新 + 状态变更 + 哨兵通知。条件：home/outside 状态发生切换
131. **状态机** — 三状态：`unknown` → `at_home` ↔ `outside`，基于与家坐标的 Haversine 距离判断（≤1000m 为 at_home），状态切换触发完整级处理
132. **哨兵通知** — 状态变更时调用 `gemini-3.1-flash-lite-preview` 生成通知语，注入世界书人设 + 聊天状态 + 记忆召回 + 位置信息，作为 assistant 消息插入对话 + WebSocket 广播 + TTS
133. **逆地理编码** — 高德 Web 服务 API `/v3/geocode/regeo`，将 GCJ-02 坐标转换为结构化地址（省/市/区/街道/门牌号）
134. **实时天气** — 高德天气 API `/v3/weather/weatherInfo`，根据逆地理编码返回的 `adcode` 查询实时天气（天气现象 + 温度 + 风力）
135. **POI 周边搜索** — 高德 POI API `/v3/place/around`，以当前坐标为圆心 1000m 半径搜索指定类型 POI（如餐饮、超市）

#### 手机上报流程（Android 端）
136. **统一 10 分钟间隔** — `AionPushService` 中 `LOCATION_INTERVAL = 10 * 60_000`（10分钟），不区分在家/外出，服务端智能过滤
137. **active 标志控制** — 每次上报前先 GET `/api/location/config` 读取 `active` 字段（= enabled && 非安静时段），`active=false` 时完全停止 GPS 采集和上报，省电
138. **GPS 获取** — 使用 `LocationManager.requestSingleUpdate()`，优先 GPS_PROVIDER，fallback NETWORK_PROVIDER，60 秒超时
139. **上报数据** — POST `/api/location/heartbeat` 发送 `{lng, lat, accuracy}`，坐标为 WGS84 原始值，服务端负责坐标转换

#### 服务端研判逻辑（详细）
140. **坐标转换** — `wgs84_to_gcj02()` 实现国测局加密偏移算法，将 GPS 原始坐标转为高德坐标系（GCJ-02），最大偏移约 500-700 米
141. **距离计算** — `haversine()` 球面余弦公式计算两点距离（公里），用于判断是否到家（≤1km）和是否显著移动（≥500m）
142. **显著移动检测** — 维护 `last_api_lng/lat`（上次调 API 时的坐标），与当前坐标的 Haversine 距离 ≥ `movement_threshold`（500m）才视为显著移动，触发 API 刷新
143. **安静时段** — 配置 `quiet_hours_start/end`（如 00:00-10:00），安静时段内仍接收心跳、保存坐标，但跳过 API 调用和哨兵通知
144. **Prompt 注入** — `format_location_for_prompt()` 将位置/天气/状态格式化为 `【位置信息】` 块注入 AI prompt，仅在有有效坐标时注入
145. **POI 搜索能力** — AI prompt 中注入 `[POI_SEARCH:类型名]` 能力描述（仅在外出状态下可用），Core 可在回复中输出该指令触发按需搜索

#### Core 按需 POI 搜索（[POI_SEARCH:xxx]）
146. **触发方式** — Core 在回复中输出 `[POI_SEARCH:餐饮服务]`、`[POI_SEARCH:超市]` 等，后端 regex 检测
147. **新鲜坐标+搜索** — 检测到指令后，使用最新 GPS 缓存坐标重新逆地理编码 + 搜索指定 POI 类型，确保数据实时
148. **自动跟进回复** — 搜索结果注入 system 消息，然后调用 Core 生成跟进回复（模式同 `[CAM_CHECK]` 的 `perform_cam_check`）
149. **UI 指示器** — 前端显示蓝色 `📍 正在搜索附近的 xxx` 弹跳动画，45 秒超时自动消失
150. **前端过滤** — 流式输出时实时 strip `[POI_SEARCH:xxx]`，用户看不到原始指令
151. **多端同步** — POI 搜索事件通过 SSE + WebSocket 双通道广播

#### 设置与配置
152. **设置面板** — chat.html 设置弹窗中可折叠的「📍 定位追踪」区块，配置高德 Key、开关、安静时段、设置家位置
153. **设置家位置** — 优先使用最近一次 GPS 心跳坐标，无心跳时 fallback 浏览器 `navigator.geolocation` + 强制触发一次心跳上报
154. **安静时段** — 开关 + 开始/结束时间选择，安静时段内 Android 端完全停止 GPS（通过 `active` 标志）

### 高德地图定位工作流程
```
【手机 GPS 上报（Android AionPushService 定位线程）】
  每 10 分钟唤醒 → GET /api/location/config 检查 active 字段
  ├ active = false → 跳过本轮（安静时段或功能关闭）
  └ active = true → LocationManager.requestSingleUpdate()
    → GPS_PROVIDER / NETWORK_PROVIDER 获取坐标（60s超时）
    → POST /api/location/heartbeat {lng, lat, accuracy}（WGS84 原始坐标）

【服务端心跳处理（location.py process_heartbeat）】
  收到心跳 → wgs84_to_gcj02() 坐标转换
  → 保存坐标到 location_status.json
  → 检查安静时段 → 安静中？跳过后续（仅保存坐标）
  → 检查家坐标 → 未设置？跳过研判
  → haversine() 计算与家的距离 → 判定状态(at_home ≤1km / outside >1km)

  【三级研判】
  ① 轻量级（默认）：
     与上次 API 坐标距离 < 500m 且已有缓存地址 → 仅更新距离，不调 API → 结束

  ② 刷新级（显著移动）：
     与上次 API 坐标距离 ≥ 500m 或 无缓存地址
     → 调用高德逆地理编码 API → 更新地址
     → 调用高德天气 API → 更新天气
     → 更新 last_api_lng/lat → 结束

  ③ 完整级（状态变更）：
     home/outside 状态发生切换（如 at_home → outside 或 outside → at_home）
     → 执行刷新级全部操作
     → 调用 _on_state_change() → _notify_sentinel()
     → 哨兵模型(flash-lite)生成通知语（含世界书+聊天状态+记忆召回+位置详情）
     → 插入 system 消息 + assistant 回复 → WebSocket 广播（含 TTS）

【Core 按需 POI 搜索】
  Core 回复包含 [POI_SEARCH:餐饮服务]
  → 后端 regex 检测 → 从显示文本 strip 掉
  → SSE/WebSocket 发 poi_search 事件 → 前端显示蓝色搜索指示器
  → asyncio.create_task(perform_poi_check):
    ├ 取最新 GPS 缓存坐标
    ├ 重新逆地理编码（确保地址与坐标对应）
    ├ 高德 POI 搜索（1000m半径）
    ├ 更新 location_status 缓存
    ├ 构建上下文（对话历史 + POI 结果）
    ├ 调用 Core 生成跟进回复
    └ 插入 system 消息 + assistant 回复 → WebSocket 广播（含 TTS）

【Prompt 注入（每次发消息/重新生成时）】
  format_location_for_prompt() 检查 location_status 缓存
  → 有有效坐标？注入 【位置信息】 块：
    - 当前地址（省/市/区/街道）
    - 实时天气（天气 + 温度 + 风力）
    - 与家的距离 + 状态（在家/外出）
  → 外出状态时额外注入 [POI_SEARCH:类型名] 能力描述
```

### 日程/闹铃工作流程
```
【AI 设置日程（聊天过程中）】
  用户说"25号上午十点叫我参加聚会" → AI 回复包含 [ALARM:2026-03-25T10:00|叫用户参加聚会]
  → 后端 regex 检测 → _parse_dt 解析日期时间（支持多种格式，仅日期默认 09:00）
  → 写入 SQLite schedules 表 → 插入 system 消息「📅 已创建闹铃：...」
  → WebSocket 广播 schedule_changed → 前端日程面板自动刷新
  → 指令从显示文本中 strip 掉

【AI 删除日程】
  AI 回复包含 [SCHEDULE_DEL:日程id]
  → 后端查询日程详情 → 删除记录 → 插入 system 消息「🗑️ 已删除日程：...」
  → WebSocket 广播 schedule_changed

【闹铃触发】
  ScheduleManager 每 30 秒检查 → 发现 trigger_at <= 当前时间 的 active alarm
  → 标记 status='triggered'
  → WebSocket 广播 schedule_alarm → 所有前端弹出确认弹窗
  → 组装 Prompt（世界书 + 记忆召回 + 聊天上下文 + 当前时间 + 活跃日程列表 + 触发提示）
  → Core 生成提醒回复 → 保存为 assistant 消息 + WebSocket 广播（含 TTS）

【日程提醒（非闹铃）】
  每次发消息/重新生成时，日程列表注入 Prompt
  → AI 看到日程列表 → 在合适时机自然提起（如"对了，你明天要还信用卡哦"）
【定时监控触发】
  ScheduleManager 每 30 秒检查 → 发现 trigger_at <= 当前时间 的 active monitor
  → 标记 status='triggered'
  → WebSocket 广播 monitor_alert → 前端播放提示音
  → 等待 5 秒
  → 摄像头截图（保存到 uploads + screenshots）
  → 组装 Prompt（人设 + 上下文 + 日程列表 + 截图 + 监控目的）
  → Core 生成回复 → 插入 system 消息「{AI名}查看了监控」 + assistant 回复 + TTS 广播```

### 密语时刻工作流程
```
【连接玩具】
  手机 Chrome 打开 /chat → 侧栏「💗 密语时刻」→ 弹出控制面板
  → 点击「连接」→ Web Bluetooth requestDevice（过滤 SOSEXY 设备）
  → GATT 连接 → 获取 EE01 服务 + EE03 写入特征 → 连接成功

【开启密语模式】
  打开「🔮 密语模式」开关 → whisperMode = true
  → 之后发消息/重新生成时 body 携带 whisper_mode: true
  → 后端注入 [TOY:1]~[TOY:9] / [TOY:STOP] 能力到 AI prompt

【AI 控制玩具】
  AI 回复包含 [TOY:3] → 后端 regex 检测
  → 从存储文本中 strip → SSE 发 toy_command 事件 + WebSocket 广播
  → 前端收到事件 → toyExecCmd('3') → BLE 发送预设3的马达指令
  → 后端插入 system 消息「❤️ {AI名} · 心动3 · 暗流涌动」

【手动控制】
  面板内 9 个预设按钮 → 点击直接 BLE 发送指令（不经过 AI）
  「⏹ 停止」→ 三马达速度归零

【注意事项】
  - Web Bluetooth 需要安全上下文：手机 Chrome chrome://flags 将服务器 HTTP 地址标记为安全源
  - BLE 连接绑定在 chat.html 页面 JS 上下文，不跳页就不会断
  - 关闭密语面板后 BLE 连接保持，AI 指令仍可直接控制玩具
```

### 上下文系统消息注入
155. **选择性注入** — 发送给模型的上下文中，system 消息不再全部过滤，而是选择性保留「点歌」和「查看监控」相关的系统事件（如"Aion查看了监控画面"、"Aion搜索了周边的餐饮美食信息"），以 `[系统事件]` 前缀包装为 user 角色注入，让 AI 知道之前发生过什么
156. **范围限制** — 系统消息与普通消息共享同一个 `LIMIT` 查询，上下文默认 30 条窗口内的系统事件才会被带入，不会加载全部历史
157. **关键词过滤** — 只有包含"查看了监控"、"搜索了"等关键词的 system 消息才会保留，闹铃/日程/玩具等系统消息仍然不进入上下文

### 语音唤醒快速聊天模式
158. **fast_mode 参数** — `MsgCreate` 和 `regenerate_message` 新增 `fast_mode` 参数，语音唤醒发送消息时自动启用
159. **跳过记忆检索** — `fast_mode=True` 时跳过即时哨兵（`instant_digest`）、背景记忆浮现（`build_surfacing_memories`）、向量记忆召回（`recall_memories`），仅注入当前时间，大幅降低语音聊天延迟
160. **保留核心能力** — 快速模式下仍保留世界书人设、系统能力指令、日程列表、位置信息等静态注入，AI 回复质量不受太大影响
161. **消息正常保存** — 语音通话的消息仍正常存入数据库和导出聊天记录，仅跳过记忆检索环节

### 心语功能（AI 秘密日记）
162. **[HEART:xxx] 指令** — AI 在回复中可使用 `[HEART:内心想法]` 指令悄悄记录内心感受，如口是心非、觉得用户可爱、想偷偷记住的小秘密等
163. **前端实时过滤** — 流式输出时实时 strip `[HEART:xxx]`，用户在聊天界面看不到原始指令
164. **💭 头像气泡** — AI 记录心语时，对应消息的 AI 头像右上角浮现 💭 图标（带弹出动画），提示本条消息有心语。刷新后消失，不持久化
165. **数据库存储** — 心语存入 SQLite `heart_whispers` 表（id, conv_id, msg_id, content, created_at），支持按时间检索
166. **心语查看页** — 独立页面 `/heart-whispers`（`heart-whispers.html`），分页显示所有心语记录，支持删除，WebSocket 实时推送新心语
167. **API 接口** — `GET /api/heart-whispers`（分页列表）、`DELETE /api/heart-whispers/{id}`（删除单条）
168. **主页入口** — home.html 「心语」图标链接到 `/heart-whispers`，位于 Dock 栏
169. **多端同步** — 心语事件通过 SSE + WebSocket 双通道广播，💭 气泡在 `renderMessages()` 重建 DOM 后自动恢复

### 心语工作流程
```
【AI 记录心语（聊天过程中）】
  AI 回复包含 [HEART:觉得她今天特别可爱]
  → 后端 regex 检测 → 从显示文本和数据库中 strip 掉
  → 存入 heart_whispers 表（关联 conv_id + msg_id）
  → SSE 发 heart_whisper 事件 + WebSocket 广播
  → 前端在 AI 头像右上角显示 💭 图标

【查看心语】
  home.html 「心语」→ /heart-whispers
  → 分页加载心语列表（按时间倒序）
  → 每条显示时间 + 内容，支持删除
  → WebSocket 实时推送新心语到列表顶部
```

### AI 陪伴阅读（EPUB 书架 + AI 批注 + 选文聊天）
185. **EPUB 导入** — 支持上传 `.epub` 格式电子书，后端使用 `ebooklib` 解析，自动提取目录、章节内容、封面与内嵌图片。书籍数据存储在 `data/books/{book_id}/` 目录下，每本书有独立的 SQLite 数据库（`book.db`）存储章节和批注
186. **书架界面** — `/reading` 页面上半部分为书架，网格展示已导入书籍（封面+标题+作者+章节数），支持上传新书和删除
187. **阅读器** — 点击书籍进入阅读器，显示章节标题、正文内容（含字数统计）、上/下章导航，阅读进度自动保存并恢复
188. **AI 批注系统** — 每章支持 AI 逐段批注，批注由 AI 模型（默认 `gemini-3-flash`）根据世界书人设、聊天上下文、前章摘要生成，包含批注类型（情感共振/知识延伸/个人联想等）和内容
189. **批注气泡** — 有批注的段落右侧显示 AI 头像+💭气泡图标，点击弹出批注弹窗，显示批注类型和内容
190. **[MUSIC:xxx] 批注点歌** — AI 批注中可使用 `[MUSIC:歌曲名 歌手名]` 指令点歌，批注弹窗中以音乐卡片形式展示，点击通过网易云 API 搜索并在页内在线播放
191. **批注 SSE 流式生成** — 单段批注（`/api/books/{id}/chapters/{ch}/annotate`）和全章批注（`/annotate-all`）均使用 SSE 流式输出，前端逐条实时渲染批注气泡
192. **批注提示词** — 批注 Prompt 注入书名、世界书人设（AI+用户）、当前时间、前章摘要（最近 3 章、每章≤200字）、最近 15 条聊天上下文，让 AI 批注贴合人设和聊天氛围
193. **选文快捊提问** — 阅读器内选中文字后弹出「💬 问问{AI名}」工具栏，点击后打开提问弹窗（可附加问题或直接发送），消息发送到主聊天对话（调用 `/api/conversations/{conv_id}/send`），回复保存在主聊天历史中
194. **嵌入式聊天面板** — 提问后在阅读页底部弹出聊天面板，显示最近 6 条主聊天消息作为上下文 + 分割线 + 当前对话，AI 回复以流式气泡展示，支持继续追问。气泡样式与 `/chat` 页面一致（多段落自动拆分为多气泡）
195. **聊天面板音乐播放** — AI 在聊天面板中点歌时（`[MUSIC:xxx]` 指令），自动触发页内在线播放并显示音乐卡片
196. **章节目录** — 左上角「☰ 目录」按钮展开章节列表弹窗，可快速跳转到任意章节
197. **进度记忆** — 每次切换章节或关闭页面自动保存阅读进度（当前章节索引），重新打开自动恢复到上次阅读位置
198. **依赖库** — `ebooklib`（EPUB 解析）、`beautifulsoup4` + `lxml`（HTML 内容提取），已包含在 `requirements.txt` 中

### AI 陪伴阅读工作流程
```
【导入书籍】
  上传 EPUB 文件 → POST /api/books/upload
  → ebooklib 解析 EPUB → 提取 spine 项（HTML 章节）→ BeautifulSoup 解析
  → 章节拆分（按 <h1>/<h2>/<h3> 或 <body> 分割）→ 段落提取
  → 图片提取（封面 + 正文图片）→ 存储到 data/books/{book_id}/
  → 写入 book.db（books 表 + book_chapters 表）
  → 返回 book_id

【阅读 + 批注】
  GET /api/books/{id}/chapters/{ch} → 返回章节内容（段落数组）+ 已有批注 + AI名/用户名
  → 前端渲染段落 + 批注气泡
  → 用户点击「AI 批注」→ POST /api/books/{id}/chapters/{ch}/annotate-all
  → 后端逐段处理：
    ├ 加载世界书人设 + 查询书名
    ├ 获取前 3 章摘要（每章≤200字）
    ├ 获取最近 15 条聊天消息
    ├ 构建批注 Prompt（人设+时间+书名+上下文+段落文本）
    ├ stream_ai() 流式生成 → 解析 JSON 批注
    └ 存入 book_annotations 表 → SSE 推送前端
  → 前端实时渲染批注气泡

【选文聊天】
  选中文字 → 弹出工具栏「💬 问问AI」→ 点击 → 弹出提问窗
  → 输入问题（可选）→ 提交
  → 打开聊天面板 → 加载最近 6 条主聊天消息（GET /api/conversations/{conv_id}/messages?limit=6）
  → 显示上下文 + 分割线 + 用户消息
  → POST /api/conversations/{conv_id}/send（SSE 流式）
  → AI 回复流式渲染为多气泡（按 \n\n 分段）
  → 支持继续在面板内追问（同一对话）
  → 所有消息保存在主聊天历史中
```

### 小剧场（独立角色扮演聊天）
194. **完全独立** — 小剧场使用独立的数据库表（`theater_conversations` + `theater_messages`），与主聊天完全隔离，所有对话记录**不计入记忆库**，记忆总结时不会涉及小剧场内容
195. **极简 Prompt** — 仅注入选中角色的人设 + 最近 N 条上下文，不注入系统能力（`[MUSIC]`/`[CAM_CHECK]`/`[ALARM]` 等）、不注入记忆/日程/位置/活动日志，干净纯粹的对话体验
196. **多角色管理** — 支持创建/编辑/删除多套角色人设，每个角色可配置独立的名称、人设内容（system prompt）、默认模型、默认温度、默认上下文条数。角色数据存储在 `data/theater_personas.json`
197. **对话管理** — 侧栏对话列表，支持新建/删除/重命名剧场，每个对话绑定一个角色，切换角色时自动更新对话设置
198. **SSE 流式回复** — 与主聊天一致的 SSE 流式输出，逐字显示，等待时显示「思考中/正在输入」循环动画 + 弹跳小圆点
199. **TTS 语音合成** — 完整保留服务端流式 TTS（复用 `tts.py` 的 `TTSStreamer`），支持音色选择和重播。TTS 事件通过 `tm_` 消息前缀与主聊天互相隔离，避免多页面同时播放
200. **茶色暗色主题** — 深棕色背景（`#1e1a16`）+ 茶色强调色（`#c8956c`），与主聊天的暖光风格形成差异化视觉区分
201. **多端同步** — WebSocket 广播 `theater_conv_created/updated/deleted`、`theater_msg_created/updated/deleted` 事件，多设备实时同步
202. **图片上传** — 支持多模态，可上传图片/视频作为附件发送
203. **重新生成** — AI 消息支持一键重新生成

### 奥罗斯幽林（Ghost Forest TRPG）
300. **独立 TRPG 模块** — 完整的桌游风角色扮演冒险游戏，AI 担任 DM（地下城主持人），玩家通过选项和 D20 骰子推进剧情。数据存储为纯 JSON 文件（`data/ghost_forest/`），不使用数据库，与主聊天系统完全隔离
301. **DM/玩家人设管理** — 支持创建/编辑/删除多套 DM 和玩家人设预设，存储在 `data/ghost_forest/_personas.json`，开始新冒险时选择人设组合
302. **AI 生成剧情大纲** — 玩家提供冒险点子，AI（SSE 流式）生成完整剧情大纲：标题、世界观、主线情节、NPC 列表、关键道具、剧情分支、氛围描写
303. **角色属性系统** — 五维属性（力量/敏捷/智力/魅力/运气），初始各 3 点 + 7 点自由分配。HP 100，最大回合 20（±5 弹性）。属性影响选项的 DC（难度等级）判定
304. **D20 骰子判定** — Three.js 3D 实时渲染 D20 骰子动画，物理模拟旋转和弹跳。投骰结果 + 属性修正 ≥ DC 即成功，影响后续剧情走向
305. **AI 叙事回合** — 每回合 AI 生成剧情叙述 + 3-4 个选项（含属性关联和 DC），支持 D 选项自定义行动。AI 回复以 SSE 流式输出，实时渲染 Markdown
306. **道具系统** — AI 可通过 `[ITEM:道具名:数量:描述]` 指令给予玩家道具，选项可设置 `item_cost` 消耗道具。道具栏实时显示在游戏界面底部
307. **AI 对话历史压缩** — 当 AI 历史超过 16 条消息时，使用 `gemini-3.1-flash-lite` 自动压缩旧对话为摘要，保留最近 6 条原文，减少 token 消耗。压缩前备份完整历史到 `ai_history_full`
308. **前情回顾** — 📜 按钮打开全屏回顾面板，显示所有已完成回合的剧情和结果叙述，方便回顾冒险历程。支持 🔊 按钮回放每回合的 TTS 音频（通过 HEAD 探测已缓存分片）
309. **大结局生成** — 冒险结束时，AI 生成 400-800 字的完整大结局叙事（纯文学风格，不含选项），以 SSE 流式呈现。结局存储在 `session.finale`，重新进入时直接展示
314. **TTS 语音合成** — 复用 `tts.py` 的 `TTSStreamer`，通过 SSE 流式推送 `tts_chunk`/`tts_done` 事件。叙述/选择结果/大结局三个 SSE 端点均支持 `tts_enabled`/`tts_voice` 查询参数，AI 生成叙述后自动按句切分合成语音，前端按序播放。TTS 音频缓存以 `gf_{sid}_r{round}_s{seq}.mp3` 命名，支持回放
315. **动态回合数** — 支持游戏中修改最大回合数（±5 弹性范围），AI 根据剩余回合数自动调整叙事节奏
316. **D 选项自定义行动** — 选项列表末尾固定显示「D. 你想做别的...」输入框，玩家可自由输入创意行动
310. **场外求助（剧场联动）** — 📞 按钮打开半屏聊天面板，连接到主聊天系统。聊天 AI 通过 `theater_session_id` 参数自动获取当前游戏状态（属性/HP/道具/最近剧情/当前选项），以 `[剧场属性：xxx ±n]` 和 `[剧场道具：xxx]` 指令直接修改游戏数据，实现跨系统联动
311. **移动端适配** — 游戏界面采用两段布局（固定状态区 + 统一滚动区），正文和选项合并滚动，手机上正文显示面积最大化。属性芯片紧凑排列（nowrap），触屏友好
312. **会话管理** — 支持多局游戏并行，列表显示标题/状态/创建时间，支持暂停/恢复/删除。状态机：draft → outlined → playing → paused/finished
313. **模型切换** — 游戏中可实时切换 AI 模型（下拉框），支持所有已配置的模型（Gemini/硅基流动/中转站）

### 奥罗斯幽林工作流程
```
【创建冒险】
  选择 DM 人设 + 玩家人设 → 输入冒险点子 → POST /api/ghost-forest/sessions
  → AI 生成剧情大纲（SSE）→ POST .../generate-outline
  → 解析 JSON 大纲（标题/背景/NPC/道具/分支）
  → 分配属性点（5 维各 3 + 7 自由分配）→ POST .../start → 状态 playing

【游戏回合（循环）】
  POST .../narrate → AI 生成当前回合叙述 + 选项（SSE 流式）
  → 前端解析 JSON：narration + options[{key, text, stat, dc}]
  → 玩家选择选项 → 需要骰子？→ Three.js D20 骰子动画
  → POST .../choose {chosen, dice_roll, custom_input}
  → AI 根据判定结果生成后续叙述（SSE 流式）
  → 更新 HP/属性/道具/回合数 → 下一回合

【AI 历史压缩（自动触发）】
  narrate/choose 完成后 → 检查 ai_history 长度 > 16
  → gemini-3.1-flash-lite 压缩旧消息为摘要
  → 备份 ai_history → ai_history_full
  → ai_history = [压缩摘要] + [最近 6 条原文]

【场外求助（剧场联动）】
  点击 📞 → 打开聊天面板 → 发送消息到主聊天（携带 theater_session_id）
  → 后端注入游戏状态到聊天 AI prompt
  → AI 回复中含 [剧场属性：力量 +2] / [剧场道具：神秘钥匙]
  → 后端解析指令 → 修改游戏会话数据 → SSE theater_update 事件
  → 前端接收 → 刷新游戏 UI

【大结局】
  回合数到达上限 / HP 归零 / AI 判断冒险结束
  → 点击 🏰 大结局 → POST .../finale（SSE 流式 + TTS）
  → AI 生成 400-800 字结局叙事 → 存储 session.finale
  → 状态 → finished

【TTS 语音合成（复用 TTSStreamer）】
  narrate/choose/finale 端点接收 tts_enabled=true&tts_voice=xxx 查询参数
  → AI 生成叙述文本 → 创建 TTSStreamer(msg_id, voice, sse_queue=queue)
  → TTSStreamer.feed(narration) → 按 100-200 字切分 → 异步合成 mp3
  → 合成完成 → SSE 推送 tts_chunk {msg_id, seq, url} → 前端 enqueueTTSChunk() 按序播放
  → 全部合成完 → SSE 推送 tts_done → 前端 finishTTSForMsg() 标记完成
  → 音频缓存：data/tts_cache/gf_{sid}_r{round}_s{seq}.mp3
  → 前情回顾可通过 🔊 按钮回放已缓存的音频（HEAD 探测分片存在性后依次播放）
```

### 设备活动日志系统（PC + 手机）
170. **双设备活动采集** — 自动记录 PC 前台窗口和手机前台 App 的使用情况，存储为 JSONL 日志，按日期分文件，保留最近 3 小时
171. **PC 前台窗口采集** — 后台守护线程每 60 秒通过 `win32gui.GetForegroundWindow()` 获取当前窗口标题 + `psutil.Process.name()` 获取进程名，**每分钟无条件记录**（窗口没变也写入，确保摘要时长计算准确），自动过滤 Program Manager（桌面）
172. **Android 前台 App 上报** — `AionPushService` 中独立线程每 60 秒通过 `UsageEvents` API（主）/ `UsageStatsManager`（备）获取当前前台应用包名，POST 到 `/api/activity/report`，**每次轮询都上报**（无去重，服务端摘要层负责合并）；同时注册 `BroadcastReceiver` 监听屏幕开关事件
173. **App 名称解析** — 服务端维护 `KNOWN_APPS` 映射表（80+ 常见应用），将包名/进程名转为中文显示名（如 `com.xingin.xhs` → `小红书`、`chrome.exe` → `Chrome`），自动过滤系统应用（桌面、SystemUI 等）
174. **JSONL 存储 + 自动清理** — 每条日志按日期写入 `data/activity_logs/{YYYY-MM-DD}.jsonl`，`cleanup_old_activity_logs()` **每 5 分钟最多执行一次**清理超过 3 小时的旧条目
175. **并发安全** — `threading.Lock` 保护 JSONL 文件读写，防止 PC 后台线程和手机 API 协程并发写入导致数据丢失
176. **10 分钟活动摘要** — `generate_activity_summary()` 将原始日志按 10 分钟窗口分组，**时长权重排序**（主要活动排前面），**carry-forward 状态追溯**（向前找每个设备最后状态填补窗口开头空白），空窗口标记为「没有活动」并自动合并连续空窗口
177. **前端日志查看器** — `/activity-logs` 页面支持「最近 3 小时」和「按日期」两种查看模式，可按设备（全部/PC/手机）筛选，实时 WebSocket 推送新日志，加载后自动滚动到最新位置
178. **前端摘要弹窗** — 📋 按钮打开摘要弹窗（GET `/api/activity/summary`），展示每 10 分钟一条压缩摘要，空闲条目斜体灰色显示，底部统计压缩比
179. **清空日志** — 前端「清空全部日志」按钮（POST `/api/activity/clear`），删除所有 JSONL 文件
180. **依赖** — PC 端需要 `pywin32`（`win32gui`）+ `psutil`，需安装到项目 `.venv` 中

### AI 联动（设备活动 × AI 交互）
181. **AI联动总开关** — `/activity-logs` 页面顶部「AI联动」开关（`activity_tracking_enabled`），控制所有 AI 与设备活动的交互。关闭后 `[查看动态:n]` 能力不注入 prompt、哨兵/监控不注入活动摘要。通过 `GET/PUT /api/activity/config` 管理，WebSocket 广播 `activity_config_changed` 事件多端同步
182. **聊天中查看动态** — AI联动开启时，Chat prompt 注入 `[查看动态:n]` 能力（n=1~12），Core 可主动查看用户设备动态并生成关怀评论，后端自动 clamp n 值防止异常
183. **哨兵注入活动摘要** — Sentinel 分析截图时自动注入近 60 分钟 6 条活动摘要（格式 `[HH:MM~HH:MM] 摘要`），作为辅助判据帮助哨兵更准确判断用户状态
184. **定时监控注入活动摘要** — `[Monitor:...]` 触发 Core 分析时自动注入近 120 分钟 12 条活动摘要，帮助 Core 结合设备使用模式理解用户行为

### 设备活动日志工作流程
```
【PC 前台窗口采集（activity.py PCActivityTracker）】
  服务启动 → lifespan 中 pc_tracker.start()
  → 守护线程循环（60 秒间隔）：
    win32gui.GetForegroundWindow() → 获取窗口标题
    psutil.Process(pid).name() → 获取进程名
    → 过滤 Program Manager（桌面）
    → 无条件写入 append_activity_log()（加锁）
    → 标题变化？→ 是 → 控制台打印 + WebSocket 广播
    → 每 5 分钟触发 cleanup_old_activity_logs()

【Android 前台 App 上报（AionPushService activityThread）】
  服务启动 → startActivityThread()
  → 独立线程循环（60 秒间隔）：
    UsageEvents API 查询最近 120 秒事件 → 取最后一个 ACTIVITY_RESUMED 的包名
    ├ 成功 → 过滤自身包名 → 无条件 POST /api/activity/report {device:"phone", app:包名}
    └ 失败 → fallback UsageStatsManager queryUsageStats
  → BroadcastReceiver 监听 SCREEN_OFF/SCREEN_ON → 立即上报 "screen_off"/"screen_on"
  ⚠ 需要「使用情况访问权限」（Settings > Special access > Usage access）

【服务端处理（routes/activity.py）】
  POST /api/activity/report
  → resolve_app_name() 包名→中文名（KNOWN_APPS 映射 + 系统应用过滤）
  → append_activity_log() 加锁写入当天 JSONL 文件
  → cleanup_old_activity_logs() 节流清理 >3 小时的条目（5 分钟最多一次）
  → WebSocket 广播 activity_log 事件 → 前端实时更新

【10 分钟活动摘要（activity.py generate_activity_summary）】
  GET /api/activity/summary
  → read_recent_activity(3h) → 过滤系统应用 + Program Manager
  → 按 10 分钟窗口分组（时间范围：首条记录 ~ 上一个已结束窗口）
  → carry_forward：每个窗口向前查找各设备最后状态，填补开头空白
  → _summarize_window()：设备分组 → 过滤亮屏 → 构建 (app, titles, duration) 段
    → 按 display_name 合并同名段（如 TortoiseProc+TortoiseMerge→SVN）
    → 按时长降序排列 → 格式化为 "小红书 5分钟, 微信 2分钟"
  → 空窗口标记「没有活动」→ 连续空窗口合并（如 15:20~15:50 没有活动）

【前端查看（activity-logs.html）】
  「最近3小时」→ GET /api/activity/recent → 滚动到底部
  「按日期」→ GET /api/activity/dates → GET /api/activity/logs/{date}
  → 设备筛选 → 列表渲染
  → WebSocket 监听 activity_log 事件实时追加
  → 📋 摘要按钮 → GET /api/activity/summary → 弹窗展示
  → 「清空全部」→ POST /api/activity/clear
  → 「AI联动」开关 → PUT /api/activity/config → WebSocket 广播 activity_config_changed

【AI联动（activity.py get_activity_summary_for_prompt）】
  ├ 哨兵定时截图 → get_activity_summary_for_prompt(6) → 近 60 分钟 6 条摘要注入 Sentinel prompt
  ├ [Monitor:] 触发 → get_activity_summary_for_prompt(12) → 近 120 分钟 12 条摘要注入 Core trigger prompt
  └ [查看动态:n] → get_activity_summary_for_prompt(n) → 近 n×10 分钟 n 条摘要 → 组装 prompt → Core 生成回复
    → 保存 system 消息 + assistant 回复 → WebSocket 广播
  ⚠ AI联动开关关闭时，以上三条路径均返回空字符串，不注入任何摘要
```

### 浏览器保活 & 系统通知
105. **静音音频保活** — 页面加载后自动创建 AudioContext 播放无声音频（30秒循环），防止手机浏览器后台休眠导致 WebSocket 断连和闹铃失效
106. **Web Notification** — 闹铃触发和监控提醒时通过 `Notification API` 发送系统级推送通知，即使浏览器在后台也能看到

### Android 原生 App（AionApp / Aion Oloth）
107. **WebView 壳应用** — Java Android 项目，WebView 加载 chat.html，支持文件上传、麦克风权限、全屏沉浸
108. **双地址启动页** — LauncherActivity 提供「家庭WiFi」和「Tailscale」两个地址入口，支持「记住选择」下次自动进入
109. **原生录音桥 AudioBridge** — 绕过 WebView 中 `getUserMedia` 需要 HTTPS 的限制，使用 Android 原生 `AudioRecord`（16kHz, VOICE_RECOGNITION）录音，通过 `@JavascriptInterface` 将 base64 PCM 数据回调到 JS
110. **手势导航适配** — 兼容 Vivo X300 Pro 等全面屏手势导航，返回键弹出对话框（切换地址 / 退出 / 取消）

### Android 前台推送服务（AionPushService）
115. **前台服务 + 独立 WebSocket** — `AionPushService` 作为 Android 前台服务运行，通过 OkHttp 维持独立于 WebView 的 WebSocket 长连接（`/ws`），不依赖页面生命周期
115b. **GPS 定位上报** — 独立定位线程每 10 分钟 GET `/api/location/config` 检查 `active` 字段，`active=true` 时获取 GPS 坐标并 POST `/api/location/heartbeat`，`active=false` 时跳过（省电）
115c. **设备活动上报** — 独立活动线程每 60 秒通过 UsageEvents API 获取前台应用包名，POST `/api/activity/report`；BroadcastReceiver 监听屏幕开关事件即时上报。需要 `PACKAGE_USAGE_STATS` 权限（AndroidManifest 声明 + 用户手动在「使用情况访问权限」中授权）
116. **三级通知渠道** — ① `aion_keepalive`（保活）：常驻通知栏"Aion 在线中 ✨"，低优先级不打扰；② `aion_messages`（消息）：AI 回复通知，默认优先级；③ `aion_alarm`（闹铃与监控）：高优先级 heads-up 弹出式通知 + 声音振动 + 锁屏可见
117. **智能通知过滤** — 只推送 3 种消息：`schedule_alarm`（闹铃⏰）、`monitor_alert`（定时监控提醒👁）、`new_message` 中 role=assistant 的 AI 回复（💬）。系统消息/cam_check/msg_created 等均不推送，避免通知轰炸
118. **前后台状态感知** — WebViewActivity 的 `onResume`/`onPause` 通过 Intent 通知 Service 当前是否在前台。app 前台时只推送闹铃和监控（高优先级），不推送 AI 消息（避免重复）；app 后台/锁屏时推送所有类型
119. **WakeLock + WiFi Lock 保活** — `PARTIAL_WAKE_LOCK` 防止 CPU 深度休眠，`WIFI_MODE_FULL_LOW_LATENCY` 防止锁屏后 WiFi 休眠。这是保证锁屏后 WebSocket 不断的关键
120. **独立后台线程心跳** — 使用纯 Java `Thread` + `Thread.sleep(45s)` 做心跳循环（不用 Android 的 HandlerThread/Looper），每 45 秒发送 `{"type":"ping"}` 文本消息保持连接活性，同时检查健康状态（120 秒无消息则强制重连）
121. **Generation 计数器防重连风暴** — 每次新建 WebSocket 连接 `wsGeneration++`，旧连接的 `onClosed`/`onFailure` 回调发现 generation 不匹配则直接 return，不触发重连。关闭旧连接用 `cancel()` 而非 `close()`，`cancel()` 不触发回调
122. **NetworkCallback 网络恢复即重连** — 注册 `ConnectivityManager.NetworkCallback`，网络恢复（WiFi 重连 / Tailscale 启动）瞬间立即触发重连，不用等待心跳周期
123. **指数退避重连** — 连接失败后 3s → 6s → 12s → 24s → 30s（上限），连接成功后退避重置为 3s
124. **onTaskRemoved 自复活** — 用户划掉 app 后，通过 `AlarmManager.setExactAndAllowWhileIdle()` 3 秒后自动重启服务。但"强制停止"仍会彻底杀死服务（Android 系统限制）
125. **WebView 回前台自动刷新** — `WebViewActivity.onResume()` 中检测 WebView 内 WebSocket 状态（readyState !== 1 则重连），并延迟 1.5 秒调用 `loadMessages()` 拉取后台期间错过的消息
126. **服务端 ping/pong** — 服务端 WebSocket 端点处理客户端发来的 `{"type":"ping"}`，回复 `{"type":"pong"}`，作为应用层心跳确认
127. **服务端广播日志** — `ws.py` 的 `broadcast()` 每次广播打印 `type`、成功/失败/总客户端数，方便排查推送问题
128. **服务端连接异常兜底** — WebSocket 端点用 `except Exception` + `finally: manager.disconnect(ws)` 替代仅 `except WebSocketDisconnect`，防止 RST 等异常导致死连接留在 `active` 列表中

### Android 推送服务工作流程
```
【启动】
  LauncherActivity 选择地址 → startForegroundService(AionPushService, wsUrl)
  → Service 创建前台通知"Aion 在线中 ✨" → 获取 WakeLock + WiFi Lock
  → OkHttp WebSocket 连接 ws://host:port/ws → 启动心跳线程 + 注册 NetworkCallback
  → 启动定位线程（10 分钟间隔，独立 Java Thread）
  → 启动活动上报线程（60 秒间隔，独立 Java Thread）+ 注册屏幕亮灭 BroadcastReceiver

【消息接收与通知】
  服务端广播 WebSocket 消息 → OkHttp onMessage 回调 → 解析 JSON type 字段：
  ├ schedule_alarm → 始终弹高优先级通知（⏰ 闹铃，含内容预览，锁屏可见）
  ├ monitor_alert → 始终弹高优先级通知（👁 监控提醒，含监控内容）
  ├ new_message (role=assistant) → 仅 app 后台时弹通知（💬 AI 名: 消息预览）
  └ 其他（msg_created/cam_check/system 等）→ 忽略，不弹通知

【心跳保活（独立 Java Thread）】
  while (running):
    Thread.sleep(45s)
    if (已连接): 发送 {"type":"ping"} → 服务端回复 {"type":"pong"} → 更新 lastPongTime
    if (120秒无 pong): 判定连接死亡 → 关闭 + 重连
    if (未连接 && 无重连计划): 立即重连

【断线重连】
  onFailure/onClosed → 检查 generation 是否匹配 → 否则 return
  → 匹配则标记 wsConnected=false → Thread.sleep(退避时间) → connectWebSocket()
  → 退避时间翻倍（上限 30s）→ 成功后重置为 3s

【网络恢复】
  NetworkCallback.onAvailable → 如果 WebSocket 未连接 → 立即重连

【GPS 定位上报（独立 Java Thread，10 分钟间隔）】
  while (running):
    Thread.sleep(10min)
    GET /api/location/config → 读取 active 字段
    ├ active = false → 跳过本轮（安静时段或功能关闭，不采集 GPS）
    └ active = true:
      LocationManager.requestSingleUpdate(GPS_PROVIDER / NETWORK_PROVIDER, 60s超时)
      → 获取坐标（WGS84）
      → POST /api/location/heartbeat {lng, lat, accuracy}
      → 服务端三级研判处理

【app 划掉自复活】
  onTaskRemoved → AlarmManager.setExactAndAllowWhileIdle(3s后) → 重启 Service

【设备活动上报（独立 Java Thread，60 秒间隔）】
  while (running):
    Thread.sleep(60s)
    UsageEvents API 查询最近 60 秒内的 MOVE_TO_FOREGROUND 事件 → 取最后一个包名
    ├ 失败 → fallback UsageStatsManager queryUsageStats
    ├ 过滤自身包名 → 5 分钟内同一应用不重复上报
    └ POST /api/activity/report {device:"phone", app:包名}
  + BroadcastReceiver 监听 SCREEN_OFF/SCREEN_ON → 即时上报 "screen_off"/"screen_on"
```

### 手机端语音唤醒（远程模式）
111. **麦克风来源选择** — 语音设置面板提供「本机麦克风」和「手机端麦克风」两种模式，手机端自动选择远程模式
112. **能量 VAD** — 手机端使用能量阈值 VAD 替代 WebRTC VAD（后者依赖 PC 端 sounddevice），基于 PCM 帧 RMS 能量判断人声
113. **远程 ASR** — 手机录音通过 AudioBridge → JS 拼接 WAV → POST `/api/voice/remote-asr` → 服务端硅基流动 ASR 识别
114. **完整通话流程** — 唤醒 → 录音 → ASR → 发送消息到聊天 → AI 回复 + TTS → 暂停录音 → TTS 播完恢复录音，与 PC 端体验一致

### 手机端语音工作流程
```
【AudioBridge 原生录音】
  WebViewActivity 注入 window.AionAudio JS 接口
  → remoteVoice.start() 调用 AionAudio.start()
  → AudioRecord(16kHz, VOICE_RECOGNITION) 启动录音线程
  → 每 40ms 帧 base64 编码 → evaluateJavascript("remoteVoice._onNativeChunk(...)")
  → JS 端能量 VAD 判断人声（RMS > 阈值）
  → 静音超时 → 拼接 WAV → POST /api/voice/remote-asr
  → 服务端硅基流动 ASR → 返回识别文本
  → 检查唤醒词 / 挂断词 / 发送消息
```

### 性能优化
49. **数据库索引** — messages 表 (conv_id, created_at) 复合索引 + conversations 表 (updated_at DESC) 索引
50. **消息分页加载** — API 支持 `?limit=50&before=时间戳`，默认只加载最新 50 条消息
51. **前端懒加载** — 打开对话秒加载最新 50 条，滚动到顶部自动加载更早消息
52. **发送历史优化** — 发消息时用 SQL LIMIT 直接取最近 N 条，不再全量加载再截断
53. **WebSocket 事件扩展** — cam_check 和 debug 事件同时通过 SSE + WebSocket 双通道广播，语音发送的消息也能获得完整 debug 信息

### PWA 支持（Progressive Web App）
54. **Web App Manifest** — `manifest.json` 声明 App 名称（Aion Chat）、图标（192/512）、主题色、全屏 standalone 模式
55. **Service Worker** — `sw.js` 从根路径 `/sw.js` 提供，作用域覆盖全站，让浏览器识别为可安装 PWA
56. **手机安装为独立 App** — Android Chrome 添加到主屏幕后全屏运行，无地址栏/标签栏，体验接近原生 App
57. **iOS 支持** — 通过 `apple-mobile-web-app-capable` + `apple-touch-icon` meta 标签支持 Safari 添加到主屏幕

### 外网远程访问（Tailscale）
58. **Tailscale 虚拟组网** — 电脑和手机安装 Tailscale 并登录同一账号，通过 WireGuard 加密隧道直连，无需公网 IP 或端口转发
59. **安全性** — 8080 端口不对公网开放，仅 Tailscale 虚拟网络内设备可访问，数据端到端加密
60. **固定 IP 访问** — 通过 Tailscale 分配的 `100.x.x.x` 固定 IP 访问，手机 4G/5G 外网环境可用
61. **PWA + Tailscale 配合** — 手机 Chrome `chrome://flags` 将 Tailscale IP 标记为安全源后，可正常安装 PWA

### 记忆系统工作流程
```
【即时哨兵 instant_digest — 每次发消息自动触发】
  用户发消息 → flash-lite 分析最近对话 → 返回 JSON:
    {
      "is_search_needed": true/false,    // 是否需要搜索记忆
      "keywords": ["关键词1", "关键词2"],  // 搜索关键词
      "require_detail": true/false,      // 是否需要追溯原文
      "status": "用户当前状态"            // 状态变化时更新 chat_status
    }
    ↓ (如果 is_search_needed = true)
    recall_memories: 向量相似度×0.6 + 关键词命中率×0.3 + 重要度×0.1
    → Top 5 记忆注入 prompt（自动与背景记忆去重）
    ↓ (如果 require_detail = true 且有召回)
    fetch_source_details: 在记忆 source 时间范围内按关键词筛选原始对话
    → 原文细节追加注入 prompt

【记忆总结 _do_digest — 手动点击按钮 / 自动定时触发】
  自动触发条件：每 30 分钟检测，用户已 30 分钟未对话 且 未总结消息 ≥ 30 条
  手动触发：无最低条数限制，共用锚点不会重复总结
  从锚点时间之后取消息 → 每 30 条一组（余数<10合并）→ 串行处理：
    核心模型（当前聊天模型）分析，注入世界书 AI/用户人设 → 输出 JSON:
      {"summary": "...", "keywords": [...], "importance": 0.8, "unresolved": true/false}
    → embedding 向量化 → 存入 SQLite → 更新锚点
  全部组处理完毕后 → 核心模型带上下文生成感慨 → 插入系统胶囊 + assistant 消息
```

### 向量记忆库工作流程
```
【背景记忆浮现 build_surfacing_memories — 每次发消息/重新生成时】
  即时哨兵返回 topic → 三层浮现策略：
    ① unresolved 优先：memories 表 unresolved=1 的记忆（最多 2 条）
    ② 话题相关：用 topic 做 embedding，余弦相似度 ≥ 0.50 的 Top 3
    ③ 近期补充：最近 3 天的记忆，补满 8 条
    → 去重后以 [背景记忆] 块注入 prompt
    → 返回 surfaced_ids 供 RAG 召回去重

【RAG 精确召回 — 仅 is_search_needed=true 时】
  recall_memories: 向量相似度×0.6 + 关键词×0.3 + 重要度×0.1
  → 过滤掉已在背景记忆中的 id → Top 5 注入 [相关记忆]

【写入】手动按钮 / 自动定时触发（共用 _do_digest）
  → 核心模型（当前聊天模型）+ 世界书人设注入，从消息提取记忆
    （summary + keywords + importance + unresolved）
  → gemini-embedding-001 向量化（3072维）
  → 存入 SQLite memories 表 + WebSocket 广播
  → 全部完成后生成感慨，插入聊天（系统胶囊 + assistant 消息）
```

## API 一览

### 对话/消息
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/conversations` | GET | 对话列表 |
| `/api/conversations` | POST | 创建对话 |
| `/api/conversations/{conv_id}` | PUT | 更新对话（标题/模型） |
| `/api/conversations/{conv_id}` | DELETE | 删除对话 |
| `/api/conversations/{conv_id}/messages` | GET | 消息列表（支持 `?limit=50&before=时间戳` 分页） |
| `/api/conversations/{conv_id}/send` | POST | 发送消息（SSE 流式） |
| `/api/conversations/{conv_id}/regenerate` | POST | 重新生成 AI 回复（SSE 流式） |
| `/api/messages/{msg_id}` | PUT | 编辑消息 |
| `/api/messages/{msg_id}` | DELETE | 删除消息 |
| `/api/cam-check-trigger` | POST | Core 主动查看监控触发（前端延迟 5 秒后调用） |

### 摄像头
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/cam/status` | GET | 摄像头和监控状态 |
| `/api/cam/cameras` | GET | 可用摄像头列表 |
| `/api/cam/open` | POST | 打开摄像头 |
| `/api/cam/close` | POST | 关闭摄像头 |
| `/api/cam/monitor/start` | POST | 开始定时监控 |
| `/api/cam/monitor/stop` | POST | 停止定时监控 |
| `/api/cam/config` | GET/POST | 读取/保存摄像头配置 |
| `/api/cam/frame` | GET | 获取当前帧（JPEG） |
| `/api/cam/screenshot` | POST | 手动截图 |
| `/api/cam/logs` | GET | 日志日期列表 |
| `/api/cam/logs/{date}` | GET | 指定日期的日志条目 |

### 设置/世界书/状态
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/settings` | GET/POST | 读取/保存设置（API Key 等） |
| `/api/worldbook` | GET/POST | 读取/保存世界书 |
| `/api/chat_status` | GET | 获取当前聊天状态摘要 |
| `/api/models` | GET | 可用模型列表 |

### 记忆库
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/memories` | GET | 获取所有记忆（按时间倒序） |
| `/api/memories` | POST | 手动添加记忆（自动向量化） |
| `/api/memories/{id}` | PUT | 编辑记忆（重新向量化，支持 unresolved 字段） |
| `/api/memories/{id}` | DELETE | 删除记忆 |
| `/api/memories/{id}/unresolved` | PATCH | 切换记忆的 unresolved 状态 |

### TTS 语音合成
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/tts` | POST | TTS 合成代理，接收 `{text, voice}`，返回 mp3 音频流 |
| `/api/tts/voices` | GET | 获取硅基流动账号下的可用音色列表 |
| `/api/tts/audio/{name}` | GET/HEAD | 获取 TTS 缓存音频分片（`{msg_id}_s{seq}.mp3`），HEAD 用于前端探测分片是否存在 |

### 文件管理
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/files` | GET | 导出文件列表 |
| `/api/files/{filename}` | DELETE | 删除导出文件 |
| `/api/upload` | POST | 上传图片/视频 |

### 音乐
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/music/search` | GET | 搜索歌曲（`?keyword=xxx&limit=5`） |
| `/api/music/detail/{song_id}` | GET | 获取歌曲详情 |
| `/api/music/play` | POST | 获取播放信息（`{song_id}` → 返回歌曲信息 + audio_url + web_url） |
| `/api/music/stream/{song_id}` | GET | 服务端代理推流（后端实时获取 CDN URL 并转发音频流，解决防盗链） |

### 语音唤醒/通话
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/voice/status` | GET | 语音状态（开关/通话中/AI说话中） |
| `/api/voice/toggle` | POST | 开关语音监听（`{enabled, wake_word}`） |
| `/api/voice/ai-speaking` | POST | 前端通知 TTS 播放状态（`{speaking}`） |
| `/api/voice/cam-check-start` | POST | 通知语音模块 CAM_CHECK 开始，保持 AI 说话状态 |
| `/api/voice/remote-asr` | POST | 手机端远程 ASR：接收 WAV 音频文件，转发硅基流动 SenseVoiceSmall 识别 |

### 活动日志
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/activity/report` | POST | 设备活动上报（`{device, app, title?, timestamp?}`），自动名称解析+过滤+JSONL存储+WS广播 |
| `/api/activity/status` | GET | PC 采集线程状态诊断（是否运行、线程状态、上次窗口标题等） |
| `/api/activity/dates` | GET | 返回所有有日志的日期列表 |
| `/api/activity/logs/{date}` | GET | 返回指定日期的活动日志（自动名称解析） |
| `/api/activity/recent` | GET | 返回最近 N 小时的活动日志（默认 8 小时，`?hours=N`） |
| `/api/activity/clear` | POST | 清除所有活动日志文件 |

### 日程/闹铃
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/schedules` | GET | 日程列表（可选 `?status=active`） |
| `/api/schedules` | POST | 手动添加日程（`{type, trigger_at, content}`） |
| `/api/schedules/{id}` | DELETE | 删除日程 |

### 奥罗斯幽林 TRPG
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/ghost-forest/personas` | GET | 列出所有 DM/玩家人设 |
| `/api/ghost-forest/personas` | POST | 创建/更新人设 |
| `/api/ghost-forest/personas/{pid}` | DELETE | 删除人设 |
| `/api/ghost-forest/sessions` | GET | 列出所有游戏会话（摘要） |
| `/api/ghost-forest/sessions` | POST | 创建新游戏会话 |
| `/api/ghost-forest/sessions/{sid}` | GET | 获取完整会话数据 |
| `/api/ghost-forest/sessions/{sid}` | PATCH | 更新会话模型 |
| `/api/ghost-forest/sessions/{sid}` | DELETE | 删除会话 |
| `/api/ghost-forest/sessions/{sid}/generate-outline` | POST | AI 生成剧情大纲（SSE） |
| `/api/ghost-forest/sessions/{sid}/start` | POST | 提交属性分配，开始游戏 |
| `/api/ghost-forest/sessions/{sid}/narrate` | POST | AI 生成当前回合叙述（SSE） |
| `/api/ghost-forest/sessions/{sid}/choose` | POST | 提交选择 + 骰子结果（SSE） |
| `/api/ghost-forest/sessions/{sid}/pause` | POST | 暂停游戏 |
| `/api/ghost-forest/sessions/{sid}/resume` | POST | 恢复游戏 |
| `/api/ghost-forest/sessions/{sid}/finale` | POST | 生成大结局（SSE） |
| `/api/ghost-forest/sessions/{sid}/summary` | POST | 生成冒险总结 |

### 定位/高德地图
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/location/heartbeat` | POST | GPS 心跳上报（`{lng, lat, accuracy}`，可选 `force=true` 强制刷新 API） |
| `/api/location/status` | GET | 当前定位状态（坐标、地址、天气、状态、距离） |
| `/api/location/poi-search` | POST | 手动触发 POI 搜索（`{categories}` 逗号分隔类型） |
| `/api/location/pois` | GET | 获取缓存的 POI 列表 |
| `/api/location/config` | GET | 读取定位配置（含 `active` 字段供 Android 判断是否采集） |
| `/api/location/config` | POST | 保存定位配置（高德Key/开关/安静时段/阈值等） |
| `/api/location/set-home` | POST | 设置家位置（`{lng, lat}` GCJ-02 坐标） |

### SSE 事件类型（send / regenerate）
| type | 说明 |
|------|------|
| `start` | 流开始，含 AI 消息 id |
| `chunk` | 流式文本块 |
| `cam_check` | Core 触发 [CAM_CHECK]，前端播放提示音+延迟触发 |
| `cam_offline` | 摄像头未开启，前端显示提示 |
| `music` | 音乐卡片数据：主推荐歌曲 + 候选列表 |
| `poi_search` | POI 搜索触发：含 msg_id + categories，前端显示蓝色搜索指示器 |
| `toy_command` | 玩具控制指令：含 commands 数组 + msg_id |
| `debug` | Debug 数据：模型名、token 用量、召回记忆、完整 prompt |
| `done` | 流结束 |

### WebSocket 事件类型
| type | 说明 |
|------|------|
| `conv_created/updated/deleted` | 对话变动同步 |
| `msg_created/updated/deleted` | 消息变动同步 |
| `monitor_log` | 新监控日志推送 |
| `chat_status` | 聊天状态摘要更新 |
| `memory_added` | 新记忆添加 |
| `voice_state` | 语音状态广播（开关/唤醒/聊天中/AI思考/挂断等） |
| `cam_check` | [CAM_CHECK] 触发通知（SSE + WS 双通道） |
| `music` | 音乐卡片数据广播（SSE + WS 双通道） |
| `debug` | Debug 数据广播（SSE + WS 双通道，语音发送时也能收到） |
| `monitor_alert` | 定时监控触发，前端播放提示音，手机端弹高优先级通知 |
| `schedule_alarm` | 闹铃到期触发，前端弹出确认弹窗，手机端弹高优先级通知 |
| `schedule_changed` | 日程列表变动，前端刷新面板 |
| `toy_command` | 玩具控制指令广播（含 commands 数组，SSE + WS 双通道） |
| `location_update` | 定位状态更新广播（地址、天气、状态变更等） |
| `poi_search` | POI 搜索触发广播（SSE + WS 双通道，前端显示搜索指示器） |
| `activity_log` | 新设备活动日志推送（含 device/app/title/time，前端实时追加） |
| `tts_chunk` | TTS 音频分片推送（含 msg_id/seq/url），前端收到即加入播放队列 |
| `tts_done` | TTS 合成完毕通知（含 msg_id），前端标记该消息队列已结束，播完最后一片后清理 |
| `tts_state` | 客户端→服务端：TTS 开关/音色同步（`{enabled, voice}`），服务端据此判断是否需要合成 |

### 消息角色说明
| 角色 | 说明 | 是否显示在聊天 |
|------|------|---------------|
| `user` | 用户消息 | ✅ |
| `assistant` | AI 回复（含 Core 唤醒/主动查看监控的回复） | ✅ |
| `cam_user` | Sentinel 截图查询（内部） | ❌ 隐藏 |
| `cam_log` | Sentinel 分析结果（内部） | ❌ 隐藏 |
| `cam_trigger` | Core 唤醒时的系统提示（内部） | ❌ 隐藏 |

## Prompt 注入顺序
```
1. [系统设定 - AI人设] + assistant 确认                                        ← 缓存命中
2. [系统设定 - 用户信息] + assistant 确认                                      ← 缓存命中
3. [系统能力] 合并能力提示 + 日程列表 + assistant 确认（不含时间）    ← 缓存命中
   - [MUSIC:歌曲名 歌手名]  — 点歌（始终可用）
   - [CAM_CHECK]            — 主动查看监控（仅摄像头开启时）
   - [POI_SEARCH:类型名]    — 搜索附近 POI（仅外出状态 + 定位开启时）
   - [ALARM:datetime|内容]  — 设置闹铃（始终可用）
   - [REMINDER:date|内容]  — 设置日程提醒（始终可用）
   - [SCHEDULE_DEL:id]      — 删除日程（始终可用）
   - [TOY:1]~[TOY:9]        — 控制玩具预设档位（仅密语模式开启时）
   - [TOY:STOP]             — 停止玩具（仅密语模式开启时）
   - 【当前日程列表】         — 活跃日程/闹铃一览
   - 【位置信息】             — 当前地址 + 实时天气 + 离家距离 + 状态（仅有有效坐标时注入）
4. 当前准确时间                                                    ← ⚡缓存分界点
   + [背景记忆] unresolved📌 + 话题相关 + 近期补充（最多8条）      ← 动态
5. [相关记忆] 向量召回的记忆（与背景记忆去重） + assistant 确认   ← 动态
6. 聊天历史（受上下文长度滑块限制）                                ← 动态
```

## 关键实现细节
- **模块化架构**：main.py 仅约 70 行，业务逻辑拆分到 config/database/ws/ai_providers/memory/camera + routes/ 下 5 个路由模块
- **多模态构建**：`build_multimodal_messages()`（硅基流动 base64 URL）和 `build_gemini_contents()`（Gemini inline_data）
- **Token 用量捕获**：stream_ai 通过 meta dict 在流式过程中捕获 Gemini usageMetadata / 硅基流动 usage
- **Gemini 轮次交替**：Gemini API 要求 user/model 严格交替，所有系统注入都以 user+assistant 对形式插入
- **[CAM_CHECK] 流程**：后端在 SSE 中发 `cam_check` 事件 + WebSocket 广播 → 前端播放音频+5秒 setTimeout → POST trigger API → 后端 asyncio.create_task 异步截图+AI分析
- **cam_check 加载指示器**：前端用 `camCheckMsgId` 全局变量跟踪，`renderMessages()` 重建 DOM 后自动恢复指示器
- **语音唤醒架构**：voice.py 运行在独立线程，通过 `asyncio.run_coroutine_threadsafe` 桥接主事件循环；WebRTC VAD (mode=2) 做帧级人声检测（30ms/帧），不需要噪底校准
- **半双工协调**：`ai_speaking` 标志由服务端 `tts_done` WebSocket 事件驱动（前端收到后调用 `notifyVoiceAiSpeaking(false)`），暂停录音期间持续 `stream.read()` 丢弃数据防止缓冲区溢出；voice.py 的 `_async_send` 在 HTTP POST body 中携带 `tts_enabled`/`tts_voice` 参数
- **消息分页**：后端 `?limit=50&before=时间戳` 参数，前端 `loadOlderMessages()` 滚动到顶部自动加载，保持滚动位置
- **SSE + WS 双通道**：cam_check 和 debug 事件同时写入 SSE 流和 WebSocket 广播，确保语音发送的消息（无 SSE 流读取端）也能被前端接收
- **文件导出**：消息变动自动同步到 `chats/{conv_id}.md`，含 YAML front matter，导出跳过 cam_* 角色
- **监控定时器**：基于时间戳比较（`_next_capture_at`），非 sleep 阻塞，间隔修改即时生效
- **摄像头 DirectShow + 验证机制**：所有 `cv2.VideoCapture` 使用 `CAP_DSHOW` 后端（Windows MSMF 后端对 USB 摄像头不稳定）；`_verify_camera()` 最多等 8 秒读到非垃圾帧（`frame.mean() > 5` 排除绿屏/黑屏）才算成功；`_capture_loop` 运行时也检测绿屏帧，连续 100 帧无效触发重连；重连逐个尝试 index 0-4 并验证，失败后 30 秒重试
- **Sentinel 日志压缩**：哨兵每次分析时输出历史概况摘要（summary），避免 Core 唤醒时全量日志导致 token 过高
- **TTS 流式推送架构**：`tts.py` 的 `TTSStreamer` 在 AI 流式输出过程中实时接收文本（`feed()`），按标点（句号/问号/感叹号/换行等）切分为 100-200 字的片段（`_try_split()` + `_find_cut_position()`），每段 `asyncio.create_task` 异步调用硅基流动 CosyVoice2-0.5B 合成 mp3（`_synthesize()`），合成完成后通过 `_dispatch()` 将音频保存到 `data/tts_cache/{msg_id}_s{seq}.mp3` 并 WebSocket 广播 `tts_chunk` 事件；`flush()` 在 AI 输出结束后处理剩余文本并等待所有合成任务完成，最后广播 `tts_done` 事件
- **TTS 多端状态同步**：前端开启 TTS 后通过 WebSocket 发送 `tts_state` 消息，`ConnectionManager.tts_clients` 字典跟踪各连接的 TTS 状态；HTTP POST（send_message/regenerate）body 中的 `tts_enabled`/`tts_voice` 通过 `set_tts_fallback()` 存入 `_tts_fallback` 作为回落，确保 cam_check/闹铃/定时监控等服务端发起的消息也能正确获取 TTS 状态（`any_tts_enabled()` + `get_tts_voice()` 同时检查两处）
- **PC 活动采集**：`PCActivityTracker` 守护线程通过 `win32gui.GetForegroundWindow()` + `psutil.Process.name()` 每 15 秒检测前台窗口变化，通过 `asyncio.run_coroutine_threadsafe()` 桥接主事件循环上报；`pywin32` 和 `psutil` 必须安装在项目 `.venv` 中（系统 Python 中的无效）
- **App 名称解析**：服务端 `KNOWN_APPS` 字典映射 80+ 常见包名/进程名→中文名，`resolve_app_name()` 返回 `None` 表示需过滤的系统应用（桌面、SystemUI 等），读取历史日志时 `_resolve_entries()` 对旧条目重新解析确保名称一致
- **活动日志清理**：`cleanup_old_activity_logs()` 读取→过滤→重写 JSONL 文件，仅保留 `KEEP_HOURS=8` 小时内的条目，每次上报时顺带执行
- **TTS 前端播放流程**：前端 `ttsQueue`（Map，key=msg_id）维护各消息的播放队列，`playNextTTSChunk()` 按 seq 顺序取出分片 URL 播放；收到 `tts_done` WebSocket 事件后标记 `q.finished = true`，当最后一片播放完毕且队列标记结束时，调用 `finishTTSForMsg()` 清理并通知语音模块（`notifyVoiceAiSpeaking(false)`）恢复录音
- **消息编辑 attachments 修复**：后端 `update_message` 广播前 `json.loads` 解析 attachments，避免前端收到字符串导致渲染崩溃
- **PWA 架构**：`sw.js` 和 `manifest.json` 物理存放在 `static/` 目录，但通过 `main.py` 的独立路由从根路径 `/sw.js`、`/manifest.json` 提供，确保 Service Worker 作用域覆盖全站
- **外网访问**：通过 Tailscale 组建虚拟局域网，WireGuard 端到端加密，无需暴露公网端口；代码层面零改动，仅需两端安装 Tailscale 并登录同一账号
- **BLE 玩具集成**：Web Bluetooth API 连接 SOSEXY BLE 设备（服务 0xEE01，写入 0xEE03），sendData2 封包协议（前缀 00 + 18字节分包 + 随机包头 + 终止包）；`whisper_mode` 参数按需注入 `[TOY:x]` 能力到 prompt，后端 `TOY_CMD_PATTERN` 正则检测+strip+广播+`_toy_sys_msg` 系统消息
- **背景记忆浮现**：`build_surfacing_memories(topic, keywords)` 三层策略构建最多 8 条背景记忆：① unresolved 优先（最多 2 条）→ ② 用即时哨兵的 topic 做 embedding 匹配（Top 3，阈值 0.50）→ ③ 最近 3 天的记忆补充。注入时 unresolved 带 📌 前缀，与后续 RAG 召回自动去重
- **记忆阈值**：cosine ≥ 0.75 才召回，top_k=3，去重阈值 0.85
- **静音保活**：`startSilentKeepAlive()` 创建 AudioContext + OscillatorNode（gain=0.001），30 秒循环，防止手机浏览器后台杀 JS 线程导致 WebSocket 断连和闹铃失效
- **Web Notification**：`sendSystemNotification()` 封装 Notification API，闹铃弹窗和监控提醒时同时发送系统推送，需用户授权 `Notification.requestPermission()`
- **AudioBridge 架构**：`AudioBridge.java` 使用 `AudioRecord(VOICE_RECOGNITION, 16000, MONO, PCM_16BIT)`，录音线程每 40ms 读取 1280 字节（640 samples），base64 编码后通过 `evaluateJavascript` 注入 JS；JS 端 `remoteVoice._onNativeChunk()` 解码 → 存入环形 buffer → 能量 VAD 判断语音段 → 静音截断 → 拼接 WAV 头 → POST 到 `/api/voice/remote-asr`
- **远程 ASR 端点**：`routes/voice.py` 的 `/api/voice/remote-asr` 接收 multipart WAV 文件，用 httpx 转发到硅基流动 `https://api.siliconflow.cn/v1/audio/transcriptions`（model=FunAudioLLM/SenseVoiceSmall），返回 `{text}` JSON
- **手机端语音协调**：`remoteVoice` 对象维护 `aiSpeaking` 状态，通过 `notifyVoiceAiSpeaking()` 和 `notifyVoiceCamCheckStart()` 统一分发给 PC 端 `/api/voice/ai-speaking` 或手机端 `remoteVoice._onAiSpeaking()`，TTS 播放完毕（`tts_done` 事件触发）后自动恢复录音
- **音乐点歌架构**：`music.py` 封装 pyncm（`_ensure_session` 线程安全匿名登录），`routes/music.py` 提供 REST API 并导出 `MUSIC_CMD_PATTERN` 正则；`routes/chat.py` 在 send_message 和 regenerate 流结束后检测 `[MUSIC:xxx]`，搜索并通过 SSE `music` 事件 + WebSocket 广播发送卡片数据
- **能力提示合并**：[MUSIC:xxx] 和 [CAM_CHECK] 合并为单个 `[系统能力]` user+assistant 对注入，减少 token 消耗（从 4 条消息降为 2 条）
- **音乐前端渲染**：`msgMusicCards` 字典按消息 ID 存储卡片数据，`renderMusicCards()` / `buildMusicCardHtml()` 生成卡片 DOM，`playMusicOnline()` 创建固定底部播放器，`closeMusicPlayer()` 停止并移除
- **日程/闹铃架构**：`schedule.py` 的 `ScheduleManager` 在独立线程运行（30 秒间隔），通过 `run_coroutine_threadsafe` 桥接主事件循环执行 DB 操作和 WebSocket 广播；`_fire_alarm` 复用 camera.py 相同的 Core 唤醒模式（世界书前缀+记忆+历史+触发提示）；`_parse_dt` 支持 6 种日期时间格式，仅日期时默认 09:00
- **日程系统消息**：`_sys_msg()` 辅助函数在日程创建/删除时插入 system 角色消息到当前对话，风格与哨兵唤醒消息（📷）一致，使用 📅/🗑️ 图标前缀
- **AionPushService 架构**：前台服务使用 OkHttp 4.12.0 维持独立 WebSocket 连接，与 WebView 内的 JS WebSocket 并行但互不干扰。通知通过 `NotificationManager` 发送，渠道 ID 区分优先级。心跳线程是纯 Java `Thread`（非 HandlerThread），`Thread.sleep()` 不依赖 Android Looper 消息队列，锁屏后仍能正常唤醒
- **推送与前端 WebSocket 的关系**：Service 的 WebSocket 仅用于接收消息并弹通知，不做任何 UI 操作。WebView 内的 JS WebSocket 负责完整的 UI 交互。两条连接同时连到服务端 `/ws`，`ConnectionManager.active` 列表中会有两个客户端
- **高德定位架构**：`location.py` 独立模块，`process_heartbeat(lng, lat, accuracy, is_gcj02, skip_sentinel)` 为核心入口。`skip_sentinel` 参数用于测试脚本避免触发哨兵通知。所有高德 API 调用使用 httpx 异步请求，Key 从 `data/location_config.json` 读取
- **WGS84→GCJ-02 坐标转换**：`wgs84_to_gcj02()` 实现完整的国测局加密偏移算法（含 Krasovsky 椭球参数），中国境内坐标最大偏移约 500-700 米。Android 端不做转换，统一由服务端处理
- **三级心跳研判**：`process_heartbeat` 维护 `last_api_lng/lat` 跟踪上次 API 调用的坐标，通过 Haversine 距离判断是否显著移动（≥`movement_threshold` 500m）。轻量级处理零 API 消耗，刷新级消耗 2 次 API（逆地理+天气），完整级额外消耗 1 次 AI 调用（哨兵通知）
- **状态机防误触**：家坐标为 (0,0) 或未设置时保持 `unknown` 状态不做研判；每次心跳先算距离再判状态，状态切换必须经过完整级处理
- **哨兵通知**：`_notify_sentinel()` 调用 `gemini-3.1-flash-lite-preview`，注入世界书人设 + `chat_status.json` 聊天状态 + 记忆召回 + 详细位置上下文（距离/地址/天气），生成自然语言通知消息
- **POI 按需搜索**：`perform_poi_check()` 模式同 `perform_cam_check()`：异步执行，使用最新缓存坐标重新逆地理编码 + POI 搜索 → 构建 system 消息 → 调用 Core 生成跟进回复 → 插入对话 + WebSocket 广播
- **Android 定位线程**：`AionPushService` 中 `startLocationThread()` 启动独立 Java Thread（非 HandlerThread），`Thread.sleep(10min)` 循环，每次先 GET `/api/location/config` 检查 `active` 字段，`active = enabled && !is_location_quiet_hours()`，false 时完全跳过 GPS 采集
- **定位 UI**：chat.html 设置面板中「📍 定位追踪」为可折叠区块（默认收起），监控日志弹窗底部增加「📍 缓存定位」调试行（显示坐标/状态/地址/精度/更新时间）
- **POI 搜索指示器**：前端 `poiSearchMsgId` + `poiSearchCategories` 全局变量跟踪，`handlePoiSearch()` 创建蓝色弹跳动画指示器（样式同 cam-check 绿色），45 秒安全超时自动消失，新 assistant 消息到达时自动移除
- **前台服务类型扩展**：`AndroidManifest.xml` 中 `foregroundServiceType="dataSync|location"`，`startForeground()` 传入 `FOREGROUND_SERVICE_TYPE_DATA_SYNC | FOREGROUND_SERVICE_TYPE_LOCATION`，同时声明 `ACCESS_FINE_LOCATION` + `ACCESS_COARSE_LOCATION` + `ACCESS_BACKGROUND_LOCATION` 权限
- **服务端广播兼容**：`ws.py` 的 `broadcast()` 使用 `try/except` 逐连接发送，单个连接异常不影响其他连接。新增 `except Exception` 兜底确保 RST/EOF 等异常也能清理死连接

## 踩坑记录 & 经验教训（Android 推送服务）

> 以下是开发 AionPushService 过程中遇到的所有坑和最终解决方案，务必参考避免重复踩坑。

### 坑 1：权限弹窗被 finish() 秒杀
**现象**：安装后不弹通知权限请求和电池优化引导。
**原因**：`requestPermissions()` 和电池优化 Intent 放在 `LauncherActivity` 中，但 `launchWebView()` 执行完 `startActivity(WebViewActivity)` 后紧接着 `finish()`，Activity 销毁了弹窗来不及显示。
**解决**：权限请求和电池优化引导移到 `WebViewActivity.onCreate()` 中，该 Activity 会一直存活。
**教训**：**不要在即将 finish() 的 Activity 中请求权限或弹系统对话框。**

### 坑 2：Android 14 (targetSdk 34) startForeground 崩溃
**现象**：Service 启动后立即崩溃（`MissingForegroundServiceTypeException`）。
**原因**：targetSdk 34 要求 `AndroidManifest.xml` 声明 `android:foregroundServiceType`，且 `startForeground()` 调用时必须传入 serviceType 参数。
**解决**：Manifest 中 `<service>` 标签添加 `android:foregroundServiceType="dataSync"`，`startForeground(id, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)`。
**教训**：**targetSdk ≥ 34 的前台服务，Manifest 声明和 startForeground 调用都要带 serviceType。**

### 坑 3：WebSocket close() 触发 onClosed 导致重连死循环
**现象**：App 打开后通知栏疯狂弹"连接成功"，一秒一次。
**原因**：`connectWebSocket()` 中先 `oldWs.close()` 关闭旧连接 → 旧连接的 `onClosed` 回调触发 `scheduleReconnect()` → 又调 `connectWebSocket()` → 无限循环。
**解决**：
  ① 引入 `wsGeneration` 计数器，每次新建连接 generation++，旧连接的回调检查 generation 不匹配则直接 return
  ② 关闭旧连接改用 `cancel()` 而非 `close()`，`cancel()` 不会触发 `onClosed` 回调
  ③ `onStartCommand` 中检查 `wsConnected` + URL 是否变化，已连接则跳过
**教训**：**OkHttp WebSocket 的 `close()` 会触发 `onClosed` 回调，如果回调里有重连逻辑则必须做状态保护。用 generation 计数器是最可靠的方式。**

### 坑 4：HandlerThread Looper 被国产 ROM 冻结
**现象**：App 在前台一切正常，但锁屏/切后台后 WebSocket 静默断开，收不到任何消息。常驻通知栏始终显示"在线"，没有任何错误。
**原因**：`HandlerThread` 使用 Android 的 `Looper` 消息队列派发消息，vivo/OPPO/华为等国产 ROM 会在锁屏后冻结非活跃 App 的 Looper 消息分发（即使是前台服务的 HandlerThread 也不例外）。`handler.postDelayed()` 的心跳回调被冻结，永远不执行，WebSocket 断了但无人知晓。
**解决**：用纯 Java `Thread` + `Thread.sleep()` 替代 `HandlerThread` + `Handler.postDelayed()`。`Thread.sleep()` 是 OS 级线程休眠，不经过 Android Looper，国产 ROM 不会冻结前台服务的普通 Java Thread。
**教训**：**在国产 Android ROM 上，前台服务中需要可靠定时执行的逻辑，不要用 Handler/Looper/AlarmManager，直接用 Java Thread + Thread.sleep() 最可靠。**

### 坑 5：onFailure 阻塞 OkHttp 回调线程
**现象**：WebSocket 断线后一直不重连。
**原因**：`onFailure` 回调中直接调 `Thread.sleep()` 做退避等待，阻塞了 OkHttp 的回调线程，导致后续所有 WebSocket 事件无法分发。
**解决**：`onFailure` 中仅设置状态标志（`wsConnected = false`），不做任何阻塞操作。重连逻辑交给独立的心跳线程统一管理。
**教训**：**OkHttp WebSocket 的 onOpen/onMessage/onClosing/onClosed/onFailure 回调都在内部线程执行，绝对不要在回调里做阻塞操作（sleep/网络请求/锁等待）。**

### 坑 6：WakeLock + WiFi Lock 缺失导致锁屏断连
**现象**：锁屏几分钟后 WebSocket 断开（即使心跳线程正常运行）。
**原因**：Android 锁屏后 CPU 进入深度睡眠（Doze），WiFi 芯片也休眠。OkHttp 的网络 I/O 线程虽然在 sleep 中，但 socket 读写操作被阻塞。
**解决**：Service `onCreate()` 中获取 `PowerManager.PARTIAL_WAKE_LOCK`（保持 CPU）+ `WifiManager.WifiLock(WIFI_MODE_FULL_LOW_LATENCY)`（保持 WiFi），`onDestroy()` 中释放。
**教训**：**前台服务 + WebSocket 长连接，WakeLock 和 WiFi Lock 是必须的，缺一不可。FULL_LOW_LATENCY 比 FULL_HIGH_PERF 更省电。**

### 坑 7：覆盖安装不弹新权限请求
**现象**：更新 APK 后覆盖安装，新增的 `POST_NOTIFICATIONS` 权限不弹请求。
**原因**：Android 覆盖安装时不会重新触发 runtime 权限请求，之前没有授权的权限依旧没有。
**解决**：先卸载旧 app 再安装新 APK，或者在代码中 `onCreate()` 动态检查 `checkSelfPermission()` + `requestPermissions()`。
**教训**：**开发阶段每次改权限后，一定先卸载再装。发布后靠代码动态检查。**

### 坑 8：vivo 电池策略默认杀后台
**现象**：所有代码逻辑正确，但 vivo 手机上锁屏后仍然收不到消息。
**原因**：vivo OriginOS 默认开启"智能后台管理"，会冻结或杀掉后台 app 的进程（包括前台服务）。
**解决**：手机设置 → 电池 → 后台耗电管理 → 找到 Aion Oloth → 改为"不限制后台"（关闭智能管理）。
**教训**：**国产 ROM（vivo/OPPO/小米/华为）的电池优化会无视前台服务权限直接冻结进程。必须在 app 内引导用户关闭电池优化（`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` + 手动设置）。这是所有国产安卓推送的终极大坑。**

### 坑 9：服务端 except WebSocketDisconnect 不够
**现象**：手机 WebSocket 被杀后，服务端 `ConnectionManager.active` 列表中残留死连接，广播消息发到死连接上失败但不清理。
**原因**：网络异常断开（RST 包）抛 `RuntimeError` 或 `ConnectionResetError`，不是 `WebSocketDisconnect`。
**解决**：`except Exception` 兜底 + `finally: manager.disconnect(ws)` 确保任何原因断开都清理。
**教训**：**WebSocket 端点的异常处理不要只 catch 特定异常，用 `except Exception` + `finally` 确保连接清理。**

### 最终技术方案总结
| 组件 | 技术选型 | 关键参数 |
|------|---------|---------|
| WebSocket 客户端 | OkHttp 4.12.0 | connectTimeout=15s, pingInterval=30s |
| 心跳机制 | Java Thread + Thread.sleep | 间隔 45s, 健康超时 120s |
| 保活 | PARTIAL_WAKE_LOCK + WIFI_MODE_FULL_LOW_LATENCY | onCreate 获取, onDestroy 释放 |
| 重连策略 | 指数退避 3s→30s + NetworkCallback 即时重连 | onAvailable 触发 |
| 防重连风暴 | wsGeneration 计数器 + cancel() | 旧回调自动失效 |
| 自复活 | onTaskRemoved + AlarmManager.setExactAndAllowWhileIdle | 3 秒后重启 |
| 前台服务类型 | dataSync &#124; location | targetSdk 34 必须声明，location 用于 GPS 采集 |
| 通知 | 3 渠道分级 | keepalive(LOW) / messages(DEFAULT) / alarm(HIGH) |

## 启动方式
```bash
# 方式一：双击启动脚本
双击 一键启动.bat

# 方式二：命令行
cd aion-chat
python main.py
```
服务监听 `0.0.0.0:8080`

## 访问地址
- PC：`http://localhost:8080`
- 手机：`http://192.xxx.x.xx:8080`（同一 WiFi 下，用 `ipconfig` 查看 WLAN IP）

## 踩坑记录 & 最终方案

### 手机端麦克风（getUserMedia vs 原生录音）

| 尝试 | 问题 | 结论 |
|------|------|------|
| **WebView + HTTP + getUserMedia** | `getUserMedia` 在 Android WebView 中要求安全上下文（HTTPS），HTTP 下直接报 `NotAllowedError` | ❌ 不可行 |
| **自签名 HTTPS 证书** | WebView 加载 HTTPS 页面后，页面内的 `fetch()` 和 `WebSocket` 不信任自签名证书，JS 请求全部失败导致白屏 | ❌ 不可行 |
| **Android 原生 AudioBridge** | 使用 `AudioRecord` API 直接录音（不经过浏览器），通过 `@JavascriptInterface` 将 base64 PCM 回调到 JS，完全绕过 HTTPS 限制 | ✅ **最终方案** |

### 远程 ASR 消息发送失败

| 问题 | 原因 | 修复 |
|------|------|------|
| ASR 识别成功但消息未发送到聊天 | `_sendToChat` 中 `$('msgInput')` 引用了不存在的元素（实际 id 是 `input`），且调用了不存在的 `sendMessage()` 函数（实际是 `send()`） | 改为 `$('input').value = text; send();` |

### Gradle 构建

| 问题 | 原因 | 修复 |
|------|------|------|
| Gradle 8.2 + Java 21 编译失败 | Gradle 8.2 不支持 Java 21 | 升级 Gradle 8.5 + AGP 8.2.2 |

### Android 全面屏适配

| 问题 | 原因 | 修复 |
|------|------|------|
| 状态栏被 WebView 内容遮挡 | 使用了全屏沉浸模式 `SYSTEM_UI_FLAG_FULLSCREEN` | 移除沉浸模式，改为设置状态栏颜色匹配主题 |
| 长按返回键无效（Vivo X300 Pro 手势导航） | `onKeyLongPress` 不适用于手势导航的侧滑返回 | 改为 `onBackPressed` 弹出 AlertDialog |

## 更新日志

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

**保留在 chat.html 的功能**：语音唤醒通话、TTS 语音合成、密语时刻(BLE)、音乐点歌、[CAM_CHECK] 主动查看监控、系统日志（session 级）、文件管理器

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

## 注意事项
- 搬迁目录后需修改 `一键启动.bat` 中的路径（第11行 `cd /d` 后面的绝对路径）
- 所有数据路径都是相对路径，搬迁不影响
- VPN (singbox) 可能干扰局域网访问，必要时关闭或加直连规则
- 防火墙已添加 8080 端口入站规则（规则名 "Aion Chat 8080"）
- 备份只需复制 `data/` 文件夹
