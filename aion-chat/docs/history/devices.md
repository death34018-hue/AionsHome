# 摄像头、日程、定位与设备活动（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

### 摄像头智能监控（Sentinel/Core 双脑架构）
28. **摄像头集成** — OpenCV DirectShow 后端，支持多摄像头切换，绿屏检测，智能预热验证
28a. **ESP32-CAM 双摄** — 支持 ESP32-CAM 作为备选摄像头源，前端 tab 切换本地/ESP32，地址支持 IP 或 mDNS 名称（如 `espcam.local`）
28b. **自动桥接** — 服务器直连 ESP32 失败时，自动通知 App（AionPushService）启动桥接：App 从热点局域网拉帧→上传服务器。直连恢复时自动关闭桥接
28c. **户外模式** — 手机开热点 + ESP32 连热点，App 前台服务桥接帧数据到家里服务器（~1fps，约 80KB/帧），AI 仍可触发 Sentinel/[CAM_CHECK]/定时监控
28d. **零侵入** — 所有下游（Sentinel、Core、[CAM_CHECK]、定时监控、预览）通过 `get_frame_jpeg()` 取帧，无需感知帧来源。`active_source` 默认 `local`，不配置 ESP32 时行为完全不变
28e. **多画面监控合成** — 所有监控截图（哨兵巡逻/[CAM_CHECK]/定时监控/前端预览）默认合成摄像头画面（上）+ 电脑主屏幕截图（下）；若 Android App 已开启「手机屏幕监督」，提示音后会上传最近一帧手机屏幕，服务端将手机画面等高缩放后贴到电脑屏幕层左侧窄条，AI 可同时看到用户、电脑和手机当前状态
28f. **PC 关屏跳过截图** — 电脑屏幕截图前会先通过 `PCDisplayTracker` 检查 Windows 显示器电源状态和物理显示器 DDC/CI 电源模式（VCP 0xD6），显示器关闭或状态未知且键鼠长时间空闲时跳过 `PIL.ImageGrab.grab()`，避免夜间关屏后把无意义桌面/常驻程序发给 AI；手机截图不受影响
28g. **手机屏幕监督** — Android App 设置页可开启 MediaProjection 授权。收到 `monitor_alert` / `cam_check` 后，App 等待约 4.2 秒，确认屏幕亮且未锁屏后抓取一帧手机屏幕并 POST 到 `/api/phone-screen/upload`；锁屏、无授权、无可用帧时 POST `/api/phone-screen/skip` 记录原因，服务端继续使用摄像头/PC画面，不阻断监控流程
29. **Sentinel 哨兵** — 每次巡逻截图前先广播 `monitor_alert` 播放提示音并等待 5 秒（给用户准备时间），然后截图交由设置页选中的原线路或 Luna 分析，注入设备活动摘要（近 60 分钟 6 条）作为辅助判断依据，输出结构化 JSON（含概况摘要 summary + 唤醒原因 core_reason）
30. **Core 唤醒** — Sentinel 判断需要时唤醒 Core（当前聊天模型），直接复用哨兵截图（不再重新截图），Core 收到哨兵摘要+唤醒原因+最近5条日志+记忆召回+哨兵截图，主动在对话中联系用户
31. **监控日志系统** — 独立于聊天的 JSONL 日志，按日期存储，3 天自动清理
32. **日志查看器** — 侧边栏「📜 监控日志」按钮，按日期浏览，显示概况摘要和唤醒原因，WebSocket 实时推送新日志
33. **位置/天气摘要（chat_status）** — `data/chat_status.json` 只缓存当前位置与天气；巡逻哨兵直接读取近期聊天记录，不再依赖模型生成的聊天状态摘要

### Core 主动查看监控（[CAM_CHECK]）
34. **[CAM_CHECK] 指令** — prompt 中注入能力提示，Core 可在回复中输出 `[CAM_CHECK]` 指令主动请求查看监控画面；摄像头不可用时会尝试电脑屏幕 + 手机屏幕兜底
35. **前端实时过滤** — 流式输出时前端实时 strip `[CAM_CHECK]`，用户看不到原始指令
36. **提示音 + 5秒延迟** — 检测到指令后前端播放 `AionMonitoralart.mp3` 提示音，等待 5 秒给用户反应时间，然后再截图
37. **加载指示器** — 等待期间在 AI 消息下方显示「📷 {AI名} 正在查看监控 ● ● ●」弹跳动画，5秒后自动移除（renderMessages 重建后自动恢复）
38. **后台截图+AI分析** — 5秒后前端 POST `/api/cam-check-trigger`，后端截图并调用 Core 模型分析画面，结果作为新 assistant 消息保存并 WebSocket 广播
39. **画面兜底处理** — 若摄像头未开启或无可用帧，后端改用电脑屏幕/手机屏幕截图；若仍无可用画面，聊天室会写入系统提示而不是静默结束
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
  广播 monitor_alert → 前端播放提示音 → 等待 5 秒
  → 截图（摄像头画面 + 电脑屏幕；可叠加手机屏幕窄条，PC关屏时跳过电脑屏幕）
  → 获取设备活动摘要（近 60 分钟 6 条）→ Sentinel(flash-lite) 分析 → 输出 JSON:
    {
      "monitoringlog": "观察描述...",
      "summary": "这段时间的概况摘要...",
      "call_core": true/false,
      "core_reason": "唤醒原因（仅call_core=true时）"
    }
    ↓ 日志写入 monitor_logs/YYYY-MM-DD.jsonl + WebSocket 广播
    ↓ (如果 call_core = true)
    复用哨兵截图 + 组装提示词（含唤醒原因 + 概况摘要 + 最近5条日志 + 世界书 + 聊天上下文 + 记忆召回）
    → Core(当前模型) 生成回复 → 作为 assistant 消息插入对话

【Core 主动查看监控 [CAM_CHECK]】
  Core 回复包含 [CAM_CHECK] → 后端检测并发 SSE 事件 + WebSocket 广播 → 前端播放提示音
  → 等待 5 秒 → POST /api/cam-check-trigger → 后端截图（摄像头画面 + 电脑屏幕；可叠加手机屏幕窄条，PC关屏时跳过电脑屏幕）
  → 带人设+上下文+图片调用 Core → 结果作为新 assistant 消息保存+广播
```

### 日程 / 闹铃 / 定时监控系统
72. **AI 设置日程** — AI 在回复中使用文本指令创建日程/闹铃，格式：`[ALARM:2026-03-25T10:00|叫用户参加聚会]`、`[REMINDER:2026-04-09|还信用卡]`
73. **AI 删除日程** — AI 输出 `[SCHEDULE_DEL:日程id]` 删除指定日程/闹铃/定时监控
74. **日程列表注入** — 每次对话时将当前活跃日程列表注入到 Prompt 的 `[系统能力]` 区块，AI 可自然提起日程提醒
75. **闹铃自动触发** — 后台 ScheduleManager 每 30 秒扫描到期闹铃，组装完整 Prompt（世界书+记忆+上下文+当前时间+活跃日程列表）调用 Core 生成提醒回复。日程记录来源窗口（`origin` = aion/connor，`origin_room_id`），触发时自动路由回复到原始窗口（Aion 私聊 / 聊天室房间），若来源窗口无法确定则根据用户最后活跃窗口投递。Connor 来源的日程使用 Codex CLI 生成回复 + Connor 配置的 TTS 音色
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
89. **截图 + Core 分析** — 到时间后自动截取摄像头画面，组装 Prompt（人设+上下文+日程列表+截图+监控目的+设备活动摘要（近 120 分钟 12 条））调用 Core 生成回复，不经过哨兵模型，不召回记忆库。系统消息显示正确的 AI 名字（Aion 来源用 `ai_name`，Connor 来源用 `connor_name` 配置值）
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
99. **指令胶囊气泡** — AI 触发玩具指令后在消息气泡下方显示粉色胶囊：「❤️ 微风轻拂」「❤️ 停止」等，同时自动插入居中系统消息：「❤️ {AI名} · 心动3 · 暗流涌动」或「❤️ {AI名} 停止了玩具」
100. **BLE 连接保持** — 控制面板嵌入 chat.html 内部浮层，关闭面板后 BLE 连接不断，正常聊天时 AI 指令仍可直接控制玩具
101. **设备过滤** — BLE 扫描仅显示名称以 SOSEXY 开头的设备
102. **多端同步** — 玩具指令通过 SSE + WebSocket 双通道广播至所有连接的客户端
103. **主页快捷入口** — home.html 「密语时刻」图标点击跳转 `/chat?whisper=1`，自动弹出密语面板
103b. **BLE 状态跨页同步** — Aion 私聊页和聊天室页通过 `BroadcastChannel('toy_ble_state')` 实时同步 BLE 连接/断开状态，任一页面连接后另一页面自动更新 UI；打开密语面板时额外检查 `AionBle.isConnected()` 确保原生桥接状态准确
103c. **聊天室密语支持** — 聊天室页左侧侧栏独立「💗 密语时刻」按钮，完整 BLE 控制面板（连接/预设网格/编辑器/日志），密语模式开关打通后端能力注入，AI 发送 [TOY:x] 指令直接执行 + 显示胶囊气泡

### 高德地图定位系统

#### 核心模块
129. **GPS 心跳上报** — Android APK 每 10 分钟通过 `LocationManager` 获取 GPS 坐标（WGS84），POST 到 `/api/location/heartbeat`，服务端自动转换为 GCJ-02（高德坐标系）
130. **三级心跳研判** — 服务端对每次心跳执行三级处理，逐级递进，最大程度节省 API 调用：
   - **轻量级（lightweight）**：仅保存坐标，不调 API。条件：移动距离 < `movement_threshold`（默认 500m）且已有缓存地址
   - **刷新级（refresh）**：调用高德逆地理编码 + 天气 API 更新地址/天气。条件：移动超过阈值 或 无缓存地址
   - **完整级（full）**：刷新 + 状态变更 + 哨兵通知。条件：home/outside 状态发生切换
131. **状态机** — 三状态：`unknown` → `at_home` ↔ `outside`，基于与家坐标的 Haversine 距离判断（≤1000m 为 at_home），状态切换触发完整级处理
132. **哨兵通知** — 状态变更时调用哨兵模型（默认 `gemini-3.1-flash-lite`，支持自定义中转站）生成通知语，注入世界书人设 + 聊天状态 + 记忆召回 + 位置信息，作为 assistant 消息插入对话 + WebSocket 广播 + TTS
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
  → Core 生成回复 → 插入 system 消息「{AI名}查看了监控」 + assistant 回复 + TTS 广播
```

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

### 设备活动日志系统（PC + 手机）
170. **双设备活动采集** — 自动记录 PC 前台窗口和手机前台 App 的使用情况，存储为 JSONL 日志，按日期分文件，保留最近 3 小时
171. **PC 前台窗口采集** — 后台守护线程每 60 秒通过 `win32gui.GetForegroundWindow()` 获取当前窗口标题 + `psutil.Process.name()` 获取进程名，**每分钟无条件记录**（窗口没变也写入，确保摘要时长计算准确），自动过滤 Program Manager（桌面）
172. **Android 前台 App 上报** — `AionPushService` 中独立线程每 60 秒通过 `UsageEvents` API（主）/ `UsageStatsManager`（备）获取当前前台应用包名，POST 到 `/api/activity/report`，**每次轮询都上报**（无去重，服务端摘要层负责合并）；同时注册 `BroadcastReceiver` 监听屏幕开关事件
172a. **PC 显示器状态检测** — `PCDisplayTracker` 监听 Windows `GUID_CONSOLE_DISPLAY_STATE`，并通过 DDC/CI VCP `0xD6` 查询物理显示器电源模式；当显示器关闭，或状态未知且键鼠空闲超过阈值时，监控合成图跳过 PC 屏幕截图，只保留摄像头和可用手机截图，避免关屏睡觉时误把桌面常驻内容当成活动
172b. **手机屏幕截图缓存** — Android App 使用 MediaProjection 按需上传手机屏幕截图，服务端保存到 `data/phone_screens/` 并复制到 `data/uploads/`，仅保留最近 50 张；最近 15 秒内的手机截图会参与监控合成，过期或锁屏时自动跳过
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

【PC 显示器状态采集（activity.py PCDisplayTracker）】
  服务启动 → lifespan 中 pc_display_tracker.start()
  → 注册 Windows PowerSetting 通知 GUID_CONSOLE_DISPLAY_STATE
  → display_state=on/off/dimmed 写入内存状态
  → 每次监控截图前 refresh_physical_state()
    ├ DDC/CI VCP 0xD6 返回 on → 允许截取 PC 屏幕
    ├ DDC/CI VCP 0xD6 返回 standby/suspend/off/hard off → 跳过 PC 屏幕
    └ DDC 不可用/未知 → 回退到 Windows display_state + GetLastInputInfo 空闲时间判断

【Android 前台 App 上报（AionPushService activityThread）】
  服务启动 → startActivityThread()
  → 独立线程循环（60 秒间隔）：
    UsageEvents API 查询最近 120 秒事件 → 取最后一个 ACTIVITY_RESUMED 的包名
    ├ 成功 → 过滤自身包名 → 无条件 POST /api/activity/report {device:"phone", app:包名}
    └ 失败 → fallback UsageStatsManager queryUsageStats
  → BroadcastReceiver 监听 SCREEN_OFF/SCREEN_ON → 立即上报 "screen_off"/"screen_on"
  ⚠ 需要「使用情况访问权限」（Settings > Special access > Usage access）

【Android 手机屏幕监督（AionPushService MediaProjection）】
  设置页「手机屏幕监督」→ WebViewActivity 调起 MediaProjection 授权
  → AionPushService 以前台服务类型 mediaProjection 维持投屏会话
  → 收到 monitor_alert / cam_check → 等待约 4.2 秒
  → PowerManager + KeyguardManager 判断屏幕亮且未锁屏
    ├ 可截图 → ImageReader 抓一帧 → JPEG 压缩 → POST /api/phone-screen/upload
    └ 不可截图 → POST /api/phone-screen/skip 记录 no_projection / locked / no_frame 等原因

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

# 监控摄像头照片附件

定时监督、AI 主动 `[CAM_CHECK]` 和哨兵监控在成功取得本地、ESP32
或手机摄像头画面后，会把同一次拍摄的纯摄像头 JPEG 作为
`monitor_camera_snapshot` 附件保存在对应窗口的系统消息中。主聊天与聊天室
默认折叠该附件，点击后展开；电脑截屏和手机截屏仍只用于 AI 分析，不会作为
聊天照片展示。手机外出监控复用已经上传到服务器的原始 JPEG，不会发起第二次
手机上传。
