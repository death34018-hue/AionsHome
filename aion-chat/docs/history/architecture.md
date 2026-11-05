# 系统结构、模型与上下文（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

## 技术栈

指定语音（`[语音]表演指导与台词[/语音]`，合计最多 500 字符，由角色直接写给 ElevenLabs）的 MP3 长期保存在 `data/voice_keepsakes/`（语音珍藏），不参与日常 TTS 缓存清理。服务启动时会把 `data/tts_cache/` 中已有的 `*_expressive.mp3` 复制到珍藏目录，不覆盖已保存文件；旧聊天卡片的播放地址保持可用。备份时请一并保存此目录。

- **后端**：Python FastAPI + SQLite (aiosqlite) + WebSocket
- **前端**：多页面架构（原生 JS，无框架），暖光主题，手机/PC 自适应。chat.html/css/js 为主聊天页（结构/样式/逻辑分离），独立功能页通过 common.css/common.js 共享样式和工具函数
- **摄像头**：OpenCV (`cv2`) DirectShow 后端后台线程采集 + ESP32-CAM HTTP 远程抓帧（双摄切换 + App 桥接模式）
- **语音**：WebRTC VAD 语音检测 + 硅基流动 ASR (SenseVoiceSmall) + TTS (CosyVoice2) + 语音消息（按住录制）
- **聊天模型**支持硅基流动、Google Gemini、自定义 OpenAI 兼容线路，以及在本机配置的 Codex CLI 和 Antigravity CLI。可选模型以聊天页的模型列表为准。
- **AI 生图**：Gemini `gemini-3.1-flash-lite-image`（REST API generateContent，responseModalities=["IMAGE", "TEXT"]）
- **AI 生成歌曲**：Gemini Lyria `lyria-3-pro-preview`（REST API generateContent，返回 audio inlineData，保存到 `data/songs/`）
- **联网搜索/网页读取**：Tavily Search/Extract API（通过 httpx 调用），模型输出 `[WEB_SEARCH:...]` / `[WEB_EXTRACT:...]` 后由服务端清洗结果并回灌上下文
- **Embedding**：Gemini `gemini-embedding-001`（3072维）或 OpenAI 兼容向量模型（如硅基流动 `Qwen/Qwen3-Embedding-8B` 4096维），余弦相似度检索，支持设置页自定义切换
- **Android App**：Java，WebView + 前台推送服务（OkHttp 4.12.0 WebSocket）+ 原生录音桥 + 原生摄像头桥 + 原生视频录制桥（MediaCodec + MediaMuxer）+ 外链打开桥，compileSdk 34 / minSdk 24
- **音乐**：pyncm（网易云音乐 API，搜索/歌曲详情/音频URL，支持 MUSIC_U Cookie VIP 登录 + 服务端代理推流）
- **EPUB 解析**：ebooklib（EPUB 读取）+ BeautifulSoup4 / lxml（HTML 解析）
- **基金监控**：akshare（A股/基金数据拉取）+ chinese-calendar（中国节假日/交易日判断）
- **MCP 娱乐室**：mcp（Python MCP SDK，支持 Streamable HTTP / stdio 传输，接入外部服务如 AI 小镇）
- **聊天室**：三人群聊（用户 + Aion + Connor-Codex），Connor 代理通过 HTTP 轮询接入 Codex CLI 服务，随机回复顺序，统一时间线上下文（私聊+群聊合并排序，场景切换标记），统一记忆总结（Aion/Connor 各自合并私聊+群聊消息总结，独立锚点，1小时无新消息自动触发），图片收发（用户发图→CLI 管线通过本地绝对路径传递、API 管线通过 base64 内嵌，Codex 回复 `[[image:...]]` 标记→前端渲染，图片存储于 `Connor-Codex/uploads/YYYY-MM-DD/`），＋展开菜单（上传图片/拍照/语音消息/密语时刻，复用 Android 原生桥 AionCamera/AionAudio，iframe 穿透访问），拍照功能（getUserMedia + AionCamera 原生桥回退，前后摄切换），语音消息（按住说话 + 上滑取消 + MediaRecorder / AionAudio 原生桥录制 → 上传 → ASR 转写 → 橙色语音气泡 + 转写小字 + 播放动画，音频文件同时发送给 AI 模型），TTS 语音合成（Aion/Connor 独立音色配置，硅基流动 CosyVoice2 服务端流式切分+并行合成，通过 SSE 推送音频分段顺序播放，配置持久化服务端 `chatroom_config.json` + localStorage 双存），侧栏群聊/私聊分 Tab 筛选 + 新建房间自动日期命名，Connor 名字/人设统一配置（`chatroom_config.json` 中 `connor_name` + `connor_persona`，侧栏🎭人设按钮统一管理，所有群聊/私聊房间共享），聊天室内 [CAM_CHECK] 摄像头查看独立实现（提示音→延迟→截图→AI 分析→回复写入聊天室），日程/闹铃/定时监控按来源窗口路由回复（origin + origin_room_id 追踪，Connor 来源使用 Connor TTS 音色），音乐点歌（[MUSIC:xxx] 指令检测 + 音乐卡片 + 在线播放器 + 自动播放），密语时刻 BLE 控制（完整 BLE 连接/预设/编辑器 + 跨页面 BLE 状态同步 + 密语模式开关 + AI [TOY:x] 指令执行 + 胶囊气泡）
- **依赖库**：fastapi, uvicorn, httpx, aiosqlite, opencv-python, Pillow, sounddevice, numpy, webrtcvad-wheels, pyncm, pywin32, psutil, ebooklib, beautifulsoup4, lxml, akshare, chinese-calendar, mcp

## 模块化文件结构
以下列出主要目录和部分模块，完整路由以 [main.py](../../main.py) 中的注册为准。
```
项目根目录/
├── 一键启动.bat                  # 从项目所在目录启动服务
├── 清理个人数据.bat              # 清理本机个人配置/缓存/Key，开源分发前可执行
├── 模型预设.txt                  # 模型列表参考
├── public/                       # 公共静态资源
│   ├── BackGround.png
│   ├── icon.png                  # 原始图标（1024x941）
│   ├── icon-192.png              # PWA 图标 192x192（自动生成）
│   ├── icon-512.png              # PWA 图标 512x512（自动生成）
│   ├── AionMonitoralart.mp3      # Core 查看监控前的提示音
│   ├── AIonResponse.mp3          # 语音唤醒回复音频（"诶，我在呢"）
│   ├── UserIcon.png              # 用户聊天头像
│   ├── AIIcon.png                # AI 聊天头像
│   ├── card/                     # 斗地主牌桌音效（洗牌/出牌/炸弹/胜负/轮到你/换人出牌）
│   └── 生图锚点.jpg             # SELFIE 参考图（AI 人物一致性锚点）
│   └── wallpaper/                # 动态壁纸媒体文件（图片+视频）
├── AionApp/                      # Android WebView 原生壳（Java，Android Studio 项目）
│   ├── app/src/main/java/com/aion/chat/
│   │   ├── LauncherActivity.java # 启动页：双地址选择（家庭WiFi / Tailscale）+ 记住选择 + 启动推送服务
│   │   ├── WebViewActivity.java  # WebView 主页：全屏加载 chat.html，麦克风权限，前后台状态通知推送服务，AionExternal 外链打开桥
│   │   ├── AudioBridge.java      # 原生录音桥：AudioRecord 16kHz → base64 → JS 回调，录制时同步转发 PCM 给 VideoBridge
│   │   ├── CameraBridge.java     # 原生摄像头桥：legacy Camera API → NV21 字节旋转 → JPEG → JS 轮询（绕过 WebView HTTPS 限制），录制时转发帧给 VideoBridge
│   │   ├── VideoBridge.java      # 原生视频录制桥：MediaCodec(H.264) + MediaCodec(AAC) + MediaMuxer → MP4，复用 CameraBridge/AudioBridge 的帧数据
│   │   ├── (AionImageSaver)      # 图片保存桥（WebViewActivity 内匿名类）：JS base64 → MediaStore 写入相册
│   │   └── AionPushService.java  # 前台推送服务：独立 WebSocket 长连接 + 通知弹窗 + 断线重连 + WakeLock/WifiLock 保活 + ESP32-CAM 桥接 + 手机屏幕监督（MediaProjection）
│   └── build.gradle              # compileSdk 34, minSdk 24, Gradle 8.5 + AGP 8.2.2, OkHttp 4.12.0
├── LittleToy/                    # BLE 玩具逆向分析 & 独立 demo
│   ├── toy_control_v4.html       # 独立 BLE 控制页面（可单独使用）
│   └── 逆向分析笔记.md           # SOSEXY 设备协议逆向笔记
├── 启动壁纸.bat                  # Chrome App 模式无边框全屏启动动态壁纸
└── aion-chat/
    ├── main.py                   # 入口：lifespan、路由注册、静态挂载、WebSocket、PWA 路由、自动记忆总结定时任务（私聊+群聊空闲检测）、Connor自动总结定时任务
    ├── config.py                 # 全局路径、常量、settings/worldbook/chat_status/cam_config 读写、哨兵模型配置(get_sentinel_config)、向量模型配置(get_embedding_config)、MODELS 字典含 vision 字段标记是否支持图片输入
    ├── database.py               # SQLite 初始化（conversations/messages/memories/schedules/theater 等表 + 性能索引）
    ├── ws.py                     # WebSocket ConnectionManager 单例，含 tts_clients 状态追踪 + _tts_fallback HTTP 回落机制 + client_id 注册/定向推送 + 各AI最后活跃窗口追踪
    ├── ai_providers.py           # AI 调用：硅基流动/Gemini/AiPro中转站/GeminiCLI/AntigravityCLI 流式 + 非流式 + 多模态消息构建 + 哨兵模型图片描述回退（非 vision 模型自动调用哨兵识图后注入文字描述）
    ├── memory.py                 # 向量记忆：embedding（Gemini/OpenAI兼容）、综合评分召回、手动/自动总结（合并私聊+群聊消息）、即时哨兵(RAG路由)、原文追溯、重建向量索引
    ├── camera.py                 # 摄像头：CameraMonitor 类、Sentinel 分析（注入设备活动摘要）、Core 唤醒、[CAM_CHECK]、ESP32-CAM 双摄切换+App桥接
    ├── location.py               # 高德地图定位：GPS心跳处理、三级研判、状态机(at_home/outside)、哨兵通知、POI搜索
    ├── voice.py                  # 语音唤醒 + 半双工通话（WebRTC VAD + 硅基流动 ASR），通话中自动携带 TTS 参数
    ├── tts.py                    # 服务端流式 TTS：按句切分（100-200字）+ 异步并行合成 + WebSocket/SSE 推送音频分片
    ├── schedule.py               # 日程/闹铃/定时监控管理器：ScheduleManager、文本指令解析、闹铃触发Core唤醒、定时监控截图+Core分析（注入设备活动摘要）、origin来源路由（回复自动投递到原始窗口）
    ├── ghost_forest.py            # 奥罗斯幽林 TRPG 引擎：会话管理、AI 对话历史压缩、D20 骰子判定、角色属性/道具系统
    ├── gift.py                    # 礼物系统：AI 判断送礼 + 硅基流动 Kolors 生图 + 礼物数据 CRUD
    ├── fund.py                    # 基金持仓监控：akshare数据拉取、盈亏计算、上证指数、历史走势、AI分析prompt生成、每日14:45定时任务(FundScheduler)
    ├── book.py                    # EPUB 解析模块：书籍导入、章节拆分、段落标注、图片提取
    ├── image_gen.py               # AI 生图模块：Gemini 图片生成（SELFIE/DRAW 模式）
    ├── song_gen.py                # AI 生成歌曲模块：Gemini Lyria、[SONG] 指令解析、歌词清理、歌曲保存
    ├── mcp_client.py              # MCP 连接管理器：管理多个 MCP Server 连接（HTTP/stdio）、工具发现、统一 call_tool 接口、转换 OpenAI tools 格式
    ├── context_builder.py          # 统一上下文构建：fetch_merged_timeline（合并私聊+群聊消息时间线）、render_merged_timeline（场景切换标记渲染）、build_ability_block、build_memory_blocks、strip_tool_commands
    ├── web_search.py              # Tavily 搜索/网页读取封装：[WEB_SEARCH]/[WEB_EXTRACT] 解析、结果清洗、流式指令过滤、Key 可用性检查
    ├── link_preview.py            # 链接预览卡片：URL 检测、网页 metadata 抽取、内网/本机地址保护、fallback 卡片
    ├── chatroom.py                # 聊天室核心逻辑：Connor-Codex 代理调用（HTTP+taskId轮询+images）、统一时间线上下文构建、统一记忆总结（Connor 1v1+群聊合并，独立锚点 connor_unified）、1h无消息自动总结、Connor 人设统一读取（chatroom_config 优先，persona.md 兑底）
    ├── routes/
    │   ├── __init__.py
    │   ├── book.py               # 阅读功能 API：书籍上传/列表/章节/进度/删除/图片/AI批注（Aion+Connor并行，单段+全章SSE）/用户高亮（框选多目标提问持久化CRUD）
    │   ├── theater.py            # 小剧场 API：独立对话CRUD、消息CRUD、角色CRUD、SSE流式回复（无记忆/系统能力注入）+ TTS
    ├── chat.py               # 对话/消息 CRUD、send_message(SSE)、regenerate、cam-check-trigger、[MUSIC:xxx]/[SONG]...[/SONG]/[WEB_SEARCH:...]/[WEB_EXTRACT:...]/[ALARM:...]/[REMINDER:...]/[Monitor:...]/[TOY:x]/[查看动态:n]/[视频电话] 检测 + 链接预览附件
    │   ├── music.py              # 音乐搜索/详情/播放/代理推流 API（pyncm）
    │   ├── schedule.py           # 日程 CRUD API（列表/添加/删除）
    │   ├── cam.py                # 摄像头控制 + 监控日志 API + ESP32-CAM 画面源切换/桥接帧接收
    │   ├── location.py           # 定位 API：心跳上报、状态查询、POI搜索、配置管理、设置家位置
    │   ├── files.py              # 上传、聊天记录文件导出/管理
    │   ├── settings.py           # 设置、世界书、模型列表、TTS 代理、Tavily Key、视频通话开关、AI生图开关、AI生成歌曲开关
    │   ├── memories.py           # 记忆库 CRUD + 手动总结触发 + 原文查看 + 锚点管理 API
    │   ├── heart_whispers.py     # 心语 API（列表查询 + 删除，旧版兼容保留）
    │   ├── moments.py            # 朋友圈 API（发布/删除/点赞点踩/评论/AI自动回复/未读红点）
    │   ├── diary.py              # 日记本 API（用户手写/编辑/删除 + AI自动总结日记列表）
    │   ├── activity.py           # 活动日志 API（上报/查询/清理/状态诊断/10分钟摘要/AI联动开关配置）
    │   ├── voice.py              # 语音唤醒/通话控制 API
    │   ├── ghost_forest.py       # 奥罗斯幽林 TRPG API（16 个端点：人设/会话/剧情生成/选择/骰子/大结局）+ SSE 流式 TTS
    │   ├── gift.py               # 礼物系统 API（pending/receive/list/delete/test）
    │   ├── fund.py               # 基金监控 API：持仓CRUD、配置开关、数据拉取、手动触发AI分析、缓存读取、历史走势
    │   ├── wallet.py             # 钱包/转账 API：余额查询、交易记录、转账操作（复用 bookkeeping 表）
    │   ├── playground.py         # 娱乐室 API：MCP Server 连接/断开、tool calling 循环、SSE 流式行动日志、经历总结归档
    │   ├── doudizhu.py           # 斗地主 API：发牌/叫地主/出牌校验/AI JSON 决策/结算/钱包联动/群聊战报
    │   └── wallpaper.py          # 动态壁纸 API：文件列表/配置读写/上传/删除
    │   └── chatroom.py           # 聊天室 API：房间 CRUD、发消息(SSE)、AI 互聊(SSE)、记忆 CRUD、配置（connor_url/connor_name/connor_persona/TTS音色）、Connor 状态、总结记忆、图片/音频上传（/api/chatroom/upload，支持 image + audio MIME）+ 图片路径重写 + 语音附件预处理（转写注入+音频URL保留） + TTS流式合成（Aion/Connor独立音色） + 链接预览附件 + 聊天室内 [CAM_CHECK] 独立实现 + Connor 1v1 指令处理（[MUSIC:]/[WEB_SEARCH:]/[WEB_EXTRACT:]/[MEMORY:]/[TOY:]/[ALARM:] 等）+ 密语模式能力注入
    ├── activity.py               # 设备活动日志：JSONL 存储、自动清理（保留最近 3 小时）、PC 前台窗口采集、PC 显示器电源状态/空闲检测、App 包名→中文名映射、10分钟窗口摘要、AI联动开关+Prompt摘要生成
    ├── phone_screen.py           # 手机屏幕监督：Android MediaProjection 截图上传缓存、最近截图读取、自动清理
    ├── music.py                  # pyncm 封装层（搜索/歌曲详情/音频URL/MUSIC_U Cookie 登录/匿名登录）
    ├── README.md                 # 本文件
    ├── 监控流程.md               # Sentinel/Core 架构设计文档
    ├── static/
    │   ├── home.html             # 手机风格主页 → /（应用图标网格 + Dock 栏）
    │   ├── chat.html             # 主聊天页 → /chat（含语音唤醒/TTS/BLE/音乐/系统日志/debug面板）
    │   ├── chat.css              # 主聊天页样式（从 chat.html 拆分，含紧凑链接预览卡片）
    │   ├── chat.js               # 主聊天页逻辑（从 chat.html 拆分，含链接预览卡片渲染与外链打开）
    │   ├── common.css            # 子页面共享样式（CSS变量/布局/组件/闹铃弹窗/toast）
    │   ├── common.js             # 子页面共享工具（api()/WS连接/闹铃弹窗/系统通知）
    │   ├── settings.html         # 设置页 → /settings（API Key + Tavily Key + 哨兵模型 + 向量模型配置）
    │   ├── worldbook.html        # 世界书页 → /worldbook（AI/用户人设编辑）
    │   ├── memory.html           # 记忆库页 → /memory（CRUD/搜索/总结/锚点/原文追溯）
    │   ├── diary.html            # 日记本页 → /diary（用户手写日记 + Aion/Connor自动总结日记 + 编辑/删除）
    │   ├── schedule.html         # 日程管理页 → /schedule（列表/添加/删除）
    │   ├── camera.html           # 摄像头页 → /camera（预览/缩放/监控开关/配置）
    │   ├── monitor-logs.html     # 监控日志页 → /monitor-logs（按日期查看/实时WS推送）
    │   ├── location.html         # 定位页 → /location（状态/POI/配置）
    │   ├── heart-whispers.html   # 心语页 → /heart-whispers（AI秘密日记查看/删除，旧版保留）
    │   ├── moments.html          # 朋友圈页 → /moments（微信朋友圈风格，三人动态+点赞+评论+AI回复）
    │   ├── activity-logs.html    # 活动日志页 → /activity-logs（双设备活动查看/筛选/清理/10分钟摘要弹窗/AI联动开关）
    │   ├── reading.html          # 阅读页 → /reading（书架+阅读器+双AI批注+选文多目标聊天+用户高亮标注+音乐播放）
    │   ├── theater.html          # 小剧场页 → /theater（独立聊天+多角色管理+TTS，茶色暗色主题）
    │   ├── ghost-forest.html     # 奥罗斯幽林页 → /ghost-forest（TRPG 游戏：D20 骰子+角色扮演+AI DM）
    │   ├── gift.html              # 爱的印记页 → /gift（礼物陈列馆，缩略图网格+详情弹窗）
    │   ├── fund.html              # 奥罗斯财团页 → /fund（基金持仓监控、数据拉取、AI分析、持仓管理）
    │   ├── playground.html        # 娱乐室页 → /playground（MCP 服务连接 + AI 自主探索 + 行动日志 + 历史记录）
    │   ├── playground.css         # 娱乐室样式
    │   ├── playground.js          # 娱乐室前端逻辑（SSE 实时渲染 + 历史记录加载）
    │   ├── doudizhu.html          # 斗地主页 → /doudizhu（三人牌桌：用户+Aion+Connor，手机牌桌布局）
    │   ├── doudizhu.css           # 斗地主样式（桌面/手机自适应、手牌压叠、弃牌堆、结算弹窗）
    │   ├── doudizhu.js            # 斗地主前端逻辑（发牌预览、叫地主/出牌交互、AI回合推进、TTS/音效、昭告天下）
    │   ├── chatroom.html          # 聊天室页 → /chatroom（三人群聊 + Connor 私聊 + 房间管理 + 记忆库悬浮窗 + 图片/语音收发 + 拍照 + TTS设置 + 🎭人设统一管理面板 + 密语时刻面板）
    │   ├── chatroom.css           # 聊天室样式（暖色三人气泡、头像、双换行拆分气泡、图片预览/查看器/内联图片、紧凑链接预览卡片、＋展开菜单、拍照全屏遮罩、语音录制浮层、橙色语音气泡+播放动画+转写小字、TTS滑块开关、群聊/私聊Tab样式、音乐卡片+播放器、密语面板+编辑器+胶囊样式）
    │   ├── chatroom.js            # 聊天室前端逻辑（SSE流式、AI互聊、记忆CRUD、世界书人设继承、图片上传/粘贴/[[image:]]渲染、链接预览卡片渲染与外链打开、＋展开菜单（上传图片/拍照/语音消息/密语时刻）、拍照（getUserMedia+AionCamera原生桥+iframe穿透）、语音消息（按住说话+上滑取消+MediaRecorder/AionAudio原生桥+WAV转换+ASR转写+语音气泡渲染+播放）、TTS分段队列播放+音色配置持久化服务端+localStorage、侧栏群聊/私聊Tab筛选+自动日期命名、音乐卡片+在线播放器+自动播放、BLE密语控制系统+BroadcastChannel跨页同步+指令胶囊气泡）
    │   ├── wallpaper.html         # 动态壁纸页 → /wallpaper（全屏壁纸轮播+AI气泡，独立显示器使用）
    │   ├── video-call.js         # 视频通话模块：摄像头预览 + 按住录制视频 + ASR转写 + 来电/去电 UI
    │   ├── manifest.json         # PWA Web App Manifest（从 /manifest.json 提供）
    │   └── sw.js                 # PWA Service Worker（从 /sw.js 提供）
    └── data/                     # ★ 备份只需复制此文件夹
        ├── chat.db               # SQLite 数据库（聊天、记忆、朋友圈、日记本、礼物、钱包等核心表）
        ├── settings.json         # API Key + Tavily Key + 哨兵/向量模型配置持久化（仅本机保存）
        ├── worldbook.json        # 世界书（AI/用户人设+名称）
        ├── cam_config.json       # 摄像头监控配置（active_source/esp32_cam_url/本地摄像头/定时/静默时段）
        ├── chat_status.json      # 聊天状态摘要（供哨兵参考）
        ├── location_config.json  # 定位配置（高德Key、家坐标、开关、安静时段、阈值等）
        ├── location_status.json  # 定位状态缓存（当前坐标、状态、地址、天气、POI等）
        ├── digest_anchor.json    # 总结锚点（记录上次总结到哪条消息的时间戳）
        ├── uploads/              # 上传的图片/视频
        ├── songs/                # Gemini Lyria 生成歌曲音频
        ├── chats/                # 导出的 .md 聊天记录 + _index.json
        ├── screenshots/          # 摄像头截图（自动清理）
        ├── monitor_logs/         # Sentinel 监控日志（JSONL，按日期，3天自动清理）
        ├── activity_logs/        # 设备活动日志（JSONL，按日期，保留最近 3 小时）
        ├── books/                # EPUB 书籍数据（解析后的章节+图片+批注数据库）
        ├── theater_personas.json # 小剧场角色预设（多套人设，JSON数组）
        ├── fund_config.json      # 基金监控配置（开关、投资倾向）
        ├── fund_cache.json       # 基金数据缓存（最近一次拉取结果）
        ├── chatroom_config.json  # 聊天室配置（connor_url、connor_name、connor_persona、tts_aion_voice、tts_connor_voice）
        ├── doudizhu_state.json   # 斗地主当前牌局状态（手牌/底牌/回合/历史/结算）
        ├── mcp_servers.json      # MCP Server 配置（娱乐室服务地址列表）
        ├── wallpaper_config.json  # 动态壁纸配置（轮换间隔、文件启用状态、气泡锚点坐标）
        └── ghost_forest/          # 奥罗斯幽林 TRPG 数据
            ├── _personas.json     # DM/玩家人设预设
            └── {uuid}.json        # 游戏会话存档（每局一个文件）
```

## 路由
| 路径 | 说明 |
|------|------|
| `/` | home.html 手机风格主页（应用图标启动器） |
| `/chat` | chat.html 主聊天页 |
| `/settings` | settings.html 设置页（API Key + Tavily Key + 哨兵/向量模型配置） |
| `/worldbook` | worldbook.html 世界书页 |
| `/memory` | memory.html 记忆库页 |
| `/diary` | diary.html 日记本页（用户手写 + AI 自动总结日记） |
| `/schedule` | schedule.html 日程管理页 |
| `/camera` | camera.html 摄像头监控页 |
| `/monitor-logs` | monitor-logs.html 监控日志页 |
| `/location` | location.html 定位地图页 |
| `/heart-whispers` | heart-whispers.html 心语页（旧版兼容保留） |
| `/moments` | moments.html 朋友圈页（三人动态+点赞+评论+AI回复） |
| `/activity-logs` | activity-logs.html 活动日志页（双设备活动查看） |
| `/reading` | reading.html 阅读页（书架+阅读器+双AI批注+选文多目标聊天+用户高亮标注） |
| `/theater` | theater.html 小剧场页（独立聊天+多角色+TTS） |
| `/ghost-forest` | ghost-forest.html 奥罗斯幽林页（TRPG 冒险游戏） |
| `/gift` | gift.html 爱的印记页（礼物陈列馆） |
| `/fund` | fund.html 奥罗斯财团页（基金持仓监控） |
| `/playground` | playground.html 娱乐室页（MCP 服务接入 + AI 自主探索） |
| `/doudizhu` | doudizhu.html 斗地主牌桌（三人真实牌局：用户 + Aion + Connor） |
| `/wallpaper` | wallpaper.html 动态壁纸页（全屏壁纸轮播+AI气泡） |
| `/manifest.json` | PWA Web App Manifest |
| `/sw.js` | PWA Service Worker（根路径提供，作用域覆盖全站） |
| `/public/*` | 公共资源 |
| `/static/*` | 静态文件 |
| `/uploads/*` | data/uploads/（主聊天上传） |
| `/songs/*` | data/songs/（AI 生成歌曲音频） |
| `/cr-uploads/*` | Connor-Codex/uploads/（聊天室图片，按日期子目录） |
| `/api/*` | 后端 API |
| `/ws` | WebSocket 多端同步 |

## 模型配置

在「设置」填写服务商的 API Key，或通过「添加线路」保存 OpenAI 兼容服务的地址、密钥和模型 ID，再到聊天页选用。内置模型和启用的 CLI 模型见 [config.py](../../config.py)。

### 哨兵模型（可选原线路或 Codex Luna）
- 原线路 → 支持自定义 OpenAI 兼容接口（如硅基流动 `Qwen/Qwen3.6-35B-A3B`）；未配置时使用 `gemini-3.1-flash-lite`
- Luna 线路 → 本机 Codex / ChatGPT 登录态的 `gpt-5.6-luna`，固定 `reasoning_effort=none` 与低输出详细度
- 每条普通聊天消息的前置判断已改成本地规则，不再等待哨兵模型
- 记忆总结（手动/自动） → 当前聊天对话的核心模型（跟随用户选择）
- 聊天室记忆总结 → Codex CLI（Connor 专用）

设置页可以随时切换两条哨兵线路。切到 Luna 时原线路的地址、Key 和模型名会原样保留，切回即可继续使用；两条线路都关闭额外思考以优先保证巡逻速度。

### 向量模型（支持自定义中转站）
- 向量 Embedding → 默认 Gemini `gemini-embedding-001`（3072维）

向量模型同样支持自定义配置（如硅基流动 `Qwen/Qwen3-Embedding-8B` 4096维），切换后需在记忆库页点击「🔄 重建向量索引」重新生成向量（主记忆库 + 聊天室记忆库同时重建）。

## Prompt 注入顺序
```
1. [系统设定 - AI人设] + assistant 确认                                        ← 缓存命中
2. [系统设定 - 用户信息] + assistant 确认                                      ← 缓存命中
3. [系统能力] 合并能力提示 + 日程列表 + assistant 确认（不含时间）    ← 缓存命中
   - [MUSIC:歌曲名 歌手名]  — 点歌（始终可用）
   - [CAM_CHECK]            — 主动查看监控（仅摄像头开启时）
   - [WEB_SEARCH:查询内容]   — 联网搜索最新网页信息（仅配置 Tavily Key 且联网搜索能力开启时）
   - [WEB_EXTRACT:完整URL]   — 读取指定网页内容（仅配置 Tavily Key 且联网搜索能力开启时）
   - [POI_SEARCH:类型名]    — 搜索附近 POI（仅外出状态 + 定位开启时）
   - [ALARM:datetime|内容]  — 设置闹铃（始终可用）
   - [REMINDER:date|内容]  — 设置日程提醒（始终可用）
   - [SCHEDULE_DEL:id]      — 删除日程（始终可用）
   - [TOY:1]~[TOY:9]        — 控制玩具预设档位（仅密语模式开启时）
   - [TOY:STOP]             — 停止玩具（仅密语模式开启时）
   - [SELFIE:prompt]        — AI 自拍生图（附带参考图，仅 AI 生图开关开启时）
   - [DRAW:prompt]          — AI 自由画图（仅 AI 生图开关开启时）
   - [SONG]...[/SONG]       — AI 生成完整歌曲（仅 AI 生成歌曲开关开启时，要求 Title/Style/Singer/Vocal/Duration/Prompt/Lyrics 标准格式）
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
- **[CAM_CHECK] 流程**：后端在 SSE 中发 `cam_check` 事件 + WebSocket 广播 → 前端播放音频+5秒 setTimeout → POST trigger API → 后端 asyncio.create_task 异步截图+AI分析；聊天室异步路径优先取摄像头合成图，失败时回退到电脑屏幕/手机屏幕，并通过 `chatroom_ai_status` 显示非视觉模型的哨兵识图状态
- **cam_check 加载指示器**：前端用 `camCheckMsgId` 全局变量跟踪，`renderMessages()` 重建 DOM 后自动恢复指示器
- **语音唤醒架构**：voice.py 运行在独立线程，通过 `asyncio.run_coroutine_threadsafe` 桥接主事件循环；WebRTC VAD (mode=2) 做帧级人声检测（30ms/帧），不需要噪底校准
- **半双工协调**：`ai_speaking` 标志由服务端 `tts_done` WebSocket 事件驱动（前端收到后调用 `notifyVoiceAiSpeaking(false)`），暂停录音期间持续 `stream.read()` 丢弃数据防止缓冲区溢出；voice.py 的 `_async_send` 在 HTTP POST body 中携带 `tts_enabled`/`tts_voice` 参数
- **消息分页**：后端 `?limit=50&before=时间戳` 参数，前端 `loadOlderMessages()` 滚动到顶部自动加载，保持滚动位置
- **SSE + WS 双通道**：cam_check 和 debug 事件同时写入 SSE 流和 WebSocket 广播，确保语音发送的消息（无 SSE 流读取端）也能被前端接收
- **联网搜索架构**：`capabilities.py` 在 Tavily Key 可用且能力开关开启时注入 `[WEB_SEARCH:查询内容]` / `[WEB_EXTRACT:完整URL]`；`web_search.py` 统一调用 Tavily Search/Extract、清洗标题/摘要/URL/答案并记录 credits；`routes/chat.py` 与 `routes/chatroom.py` 负责剥离原始指令、插入可见 system 消息、广播 `web_search` 状态，并把搜索结果作为下一条上下文回灌给模型
- **联网指令过滤**：`WebCommandStreamFilter` 会在流式输出阶段隐藏 `[WEB_SEARCH]` / `[WEB_EXTRACT]` 块，最终保存和 TTS 都使用剥离后的自然文本，避免把工具指令读出来
- **链接预览架构**：`link_preview.py` 从消息文本中提取 http/https URL，读取 Open Graph/Twitter/title/description/favicon 等 metadata 后写入 `link_preview` 附件；私有 IP、本机地址、`.local` 等不主动抓取，只生成 fallback 卡片，避免误探测内网服务
- **链接预览前端渲染**：`static/chat.js` 与 `static/chatroom.js` 将 `link_preview` 附件渲染成紧凑可点击卡片；浏览器端默认 `_blank` 打开，Android WebView 端优先调用 `AionExternal.open(url)` 交给系统浏览器
- **文件导出**：消息变动自动同步到 `chats/{conv_id}.md`，含 YAML front matter，导出跳过 cam_* 角色
- **监控定时器**：基于时间戳比较（`_next_capture_at`），非 sleep 阻塞，间隔修改即时生效
- **摄像头 DirectShow + 验证机制**：所有 `cv2.VideoCapture` 使用 `CAP_DSHOW` 后端（Windows MSMF 后端对 USB 摄像头不稳定）；`_verify_camera()` 最多等 8 秒读到非垃圾帧（`frame.mean() > 5` 排除绿屏/黑屏）才算成功；`_capture_loop` 运行时也检测绿屏帧，连续 100 帧无效触发重连；重连逐个尝试 index 0-4 并验证，失败后 30 秒重试
- **监控多画面合成**：`camera.py` 的 `_combine_with_screen()` 以摄像头画面为上层，电脑屏幕为下层；电脑屏幕层会先经过 `_overlay_phone_screen()`，把 `phone_screen.get_recent_phone_screen_path(15)` 返回的手机截图等高缩放后贴到左侧窄条。若 `PCDisplayTracker.should_capture_screen()` 判定显示器关闭/长空闲，则跳过 `ImageGrab.grab()`，并在有手机截图时用 `_build_phone_only_layer()` 构建「手机窄条 + PC display off / idle」图层
- **PC 显示器状态检测**：`PCDisplayTracker` 通过隐藏窗口注册 Windows `GUID_CONSOLE_DISPLAY_STATE` 电源通知，同时在截图前使用 DDC/CI `GetVCPFeatureAndVCPFeatureReply(0xD6)` 查询物理显示器电源模式；若物理显示器返回 standby/suspend/off/hard off 或连续不可达，则跳过 PC 屏幕截图。`GetLastInputInfo()` 作为状态未知时的空闲兜底
- **手机屏幕监督架构**：`WebViewActivity` 注入 `window.AionPhoneScreen`，设置页调用 `requestPermission()` 拉起 MediaProjection 授权；`AionPushService` 使用 `FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION` 创建 `VirtualDisplay + ImageReader`。收到 `monitor_alert` / `cam_check` 后延迟约 4.2 秒，确认 `PowerManager.isInteractive()` 且 `KeyguardManager` 未锁屏后抓一帧，JPEG 压缩上传到 `/api/phone-screen/upload`
- **Sentinel 日志压缩**：哨兵每次分析时输出历史概况摘要（summary），避免 Core 唤醒时全量日志导致 token 过高
- **TTS 流式推送架构**：`tts.py` 的 `TTSStreamer` 在 AI 流式输出过程中实时接收文本（`feed()`），按标点（句号/问号/感叹号/换行等）切分为 100-200 字的片段（`_try_split()` + `_find_cut_position()`），每段 `asyncio.create_task` 异步调用硅基流动 CosyVoice2-0.5B 合成 mp3（`_synthesize()`），合成完成后通过 `_dispatch()` 将音频保存到 `data/tts_cache/{msg_id}_s{seq}.mp3` 并通过 `ConnectionManager.send_tts_event()` 定向推送 `tts_chunk` 事件给当前 TTS 播放端；`flush()` 在 AI 输出结束后处理剩余文本并等待所有合成任务完成，最后定向推送 `tts_done` 事件
- **TTS 多端状态同步**：前端开启 TTS 后通过 WebSocket 发送 `tts_state` 消息，`ConnectionManager.tts_clients` 字典跟踪各连接的 TTS 状态、播放资格和最近活跃时间；HTTP POST（send_message/regenerate）body 中的 `tts_enabled`/`tts_voice` 通过 `set_tts_fallback()` 存入 `_tts_fallback` 作为回落，确保 cam_check/闹铃/定时监控等服务端发起的消息也能正确获取 TTS 状态（`any_tts_enabled()` + `get_tts_voice()` 同时检查两处）
- **PC 活动采集**：`PCActivityTracker` 守护线程通过 `win32gui.GetForegroundWindow()` + `psutil.Process.name()` 每 60 秒记录前台窗口，通过 `asyncio.run_coroutine_threadsafe()` 桥接主事件循环上报；`PCDisplayTracker` 另起守护线程监听显示器状态。`pywin32` 和 `psutil` 必须安装在项目 `.venv` 中（系统 Python 中的无效）
- **App 名称解析**：服务端 `KNOWN_APPS` 字典映射 80+ 常见包名/进程名→中文名，`resolve_app_name()` 返回 `None` 表示需过滤的系统应用（桌面、SystemUI 等），读取历史日志时 `_resolve_entries()` 对旧条目重新解析确保名称一致
- **活动日志清理**：`cleanup_old_activity_logs()` 读取→过滤→重写 JSONL 文件，仅保留 `KEEP_HOURS=8` 小时内的条目，每次上报时顺带执行
- **TTS 前端播放流程**：前端 `ttsQueue`（Map，key=msg_id）维护各消息的播放队列，`playNextTTSChunk()` 按 seq 顺序取出分片 URL 播放；收到 `tts_done` WebSocket 事件后标记 `q.finished = true`，当最后一片播放完毕且队列标记结束时，调用 `finishTTSForMsg()` 清理并通知语音模块（`notifyVoiceAiSpeaking(false)`）恢复录音；前端只在用户关闭 TTS 开关时停止 live TTS 并清空队列，后台/隐藏/pagehide/freeze 只同步当前 lease，不主动停止手机端播放
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
- **AI 生成歌曲架构**：`song_gen.py` 封装 Gemini Lyria `lyria-3-pro-preview` 调用、SONG 指令格式、歌词清理和 `data/songs/` 文件保存；`routes/chat.py` / `routes/chatroom.py` 提取 `[SONG]...[/SONG]` 后异步生成歌曲，并创建 `generated_song` 附件消息
- **AI 生成歌曲前端渲染**：`buildGeneratedSongCard()` / `crBuildGeneratedSongCard()` 渲染歌曲卡片，播放器浮层读取附件内的 title/lyrics/url/model，进度条通过 audio 事件和 `requestAnimationFrame` 同步；`song_gen_start` / `chatroom_song_gen_start` 显示「歌曲谱写中....」等待态
- **日程/闹铃架构**：`schedule.py` 的 `ScheduleManager` 在独立线程运行（30 秒间隔），通过 `run_coroutine_threadsafe` 桥接主事件循环执行 DB 操作和 WebSocket 广播；`_fire_alarm` 复用 camera.py 相同的 Core 唤醒模式（世界书前缀+记忆+历史+触发提示）；`_parse_dt` 支持 6 种日期时间格式，仅日期时默认 09:00
- **日程系统消息**：`_sys_msg()` 辅助函数在日程创建/删除时插入 system 角色消息到当前对话，风格与哨兵唤醒消息（📷）一致，使用 📅/🗑️ 图标前缀
- **AionPushService 架构**：前台服务使用 OkHttp 4.12.0 维持独立 WebSocket 连接，与 WebView 内的 JS WebSocket 并行但互不干扰。通知通过 `NotificationManager` 发送，渠道 ID 区分优先级。心跳线程是纯 Java `Thread`（非 HandlerThread），`Thread.sleep()` 不依赖 Android Looper 消息队列，锁屏后仍能正常唤醒
- **推送与前端 WebSocket 的关系**：Service 的 WebSocket 仅用于接收消息并弹通知，不做任何 UI 操作。WebView 内的 JS WebSocket 负责完整的 UI 交互。两条连接同时连到服务端 `/ws`，`ConnectionManager.active` 列表中会有两个客户端
- **高德定位架构**：`location.py` 独立模块，`process_heartbeat(lng, lat, accuracy, is_gcj02, skip_sentinel)` 为核心入口。`skip_sentinel` 参数用于测试脚本避免触发哨兵通知。所有高德 API 调用使用 httpx 异步请求，Key 从 `data/location_config.json` 读取
- **WGS84→GCJ-02 坐标转换**：`wgs84_to_gcj02()` 实现完整的国测局加密偏移算法（含 Krasovsky 椭球参数），中国境内坐标最大偏移约 500-700 米。Android 端不做转换，统一由服务端处理
- **三级心跳研判**：`process_heartbeat` 维护 `last_api_lng/lat` 跟踪上次 API 调用的坐标，通过 Haversine 距离判断是否显著移动（≥`movement_threshold` 500m）。轻量级处理零 API 消耗，刷新级消耗 2 次 API（逆地理+天气），完整级额外消耗 1 次 AI 调用（哨兵通知）
- **状态机防误触**：家坐标为 (0,0) 或未设置时保持 `unknown` 状态不做研判；每次心跳先算距离再判状态，状态切换必须经过完整级处理
- **哨兵通知**：`_notify_sentinel()` 调用哨兵模型（默认 `gemini-3.1-flash-lite`，支持自定义中转站），注入世界书人设 + `chat_status.json` 聊天状态 + 记忆召回 + 详细位置上下文（距离/地址/天气），生成自然语言通知消息
- **POI 按需搜索**：`perform_poi_check()` 模式同 `perform_cam_check()`：异步执行，使用最新缓存坐标重新逆地理编码 + POI 搜索 → 构建 system 消息 → 调用 Core 生成跟进回复 → 插入对话 + WebSocket 广播
- **Android 定位线程**：`AionPushService` 中 `startLocationThread()` 启动独立 Java Thread（非 HandlerThread），`Thread.sleep(10min)` 循环，每次先 GET `/api/location/config` 检查 `active` 字段，`active = enabled && !is_location_quiet_hours()`，false 时完全跳过 GPS 采集
- **定位 UI**：chat.html 设置面板中「📍 定位追踪」为可折叠区块（默认收起），监控日志弹窗底部增加「📍 缓存定位」调试行（显示坐标/状态/地址/精度/更新时间）
- **POI 搜索指示器**：前端 `poiSearchMsgId` + `poiSearchCategories` 全局变量跟踪，`handlePoiSearch()` 创建蓝色弹跳动画指示器（样式同 cam-check 绿色），45 秒安全超时自动消失，新 assistant 消息到达时自动移除
- **前台服务类型扩展**：`AndroidManifest.xml` 中 `foregroundServiceType="dataSync|location|mediaProjection"`，`startForeground()` 按运行状态传入 `FOREGROUND_SERVICE_TYPE_DATA_SYNC | FOREGROUND_SERVICE_TYPE_LOCATION | FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION`，同时声明定位权限和 `FOREGROUND_SERVICE_MEDIA_PROJECTION`；MediaProjection 授权必须由前台 Activity 发起，进程重启/投屏会话停止后需要重新授权
- **服务端广播兼容**：`ws.py` 的 `broadcast()` 使用 `try/except` 逐连接发送，单个连接异常不影响其他连接。新增 `except Exception` 兜底确保 RST/EOF 等异常也能清理死连接
