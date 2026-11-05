# Android、多端访问与排错（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

### 浏览器保活 & 系统通知
105. **静音音频保活** — 页面加载后自动创建 AudioContext 播放无声音频（30秒循环），防止手机浏览器后台休眠导致 WebSocket 断连和闹铃失效
106. **Web Notification** — 闹铃触发和监控提醒时通过 `Notification API` 发送系统级推送通知，即使浏览器在后台也能看到

### Android 原生 App（AionApp / Aion Oloth）
107. **WebView 壳应用** — Java Android 项目，WebView 加载 chat.html，支持文件上传、麦克风权限、全屏沉浸
108. **双地址启动页** — LauncherActivity 提供「家庭WiFi」和「Tailscale」两个地址入口，支持「记住选择」下次自动进入
109. **原生录音桥 AudioBridge** — 绕过 WebView 中 `getUserMedia` 需要 HTTPS 的限制，使用 Android 原生 `AudioRecord`（16kHz, VOICE_RECOGNITION）录音，通过 `@JavascriptInterface` 将 base64 PCM 数据回调到 JS。视频录制期间自动转发 PCM 帧给 VideoBridge
110. **原生视频录制桥 VideoBridge** — MediaCodec(H.264) + MediaCodec(AAC) + MediaMuxer 编码 MP4，复用 CameraBridge 的视频帧和 AudioBridge 的音频帧进行视频录制，录制期间摄像头预览不中断。JS 通过 `window.AionVideo` 接口控制录制（`startRecord`/`stopRecord`/`cancel`），`stopRecord` 返回 base64 编码的 MP4 数据
111. **外链打开桥 AionExternal** — `WebViewActivity` 注入 `window.AionExternal.open(url)`，链接预览卡片、`target="_blank"` 和 `window.open` 可调用系统浏览器打开外部网站；同源页面仍留在 WebView 内
112. **手势导航适配** — 兼容 Vivo X300 Pro 等全面屏手势导航，返回键弹出对话框（切换地址 / 退出 / 取消）

### Android 悬浮聊天

安装新版 APK 并重启服务端后，在设置页的「悬浮聊天」里开启，授予悬浮窗和麦克风权限。离开小家后保留 36dp 小头像，闲置时不透明度为 20%，点开后亮起；点头像打开或收起，拖动移动。仅收到最新活跃窗口的 AI 消息时自动显示一条半透明卡片，用户自己的消息不会弹出；仅展示正文，过滤心里嘀咕和内部标记，只有这些内容的消息不会弹出。默认预览五行正文，5 秒无操作后收回小头像；触摸后重新计时，按住录音时暂停，转写和发送进度不会延长停留。点正文向下展开，读取当前窗口最近 100 条记录中的 AI 正文并按时间显示，可上下滚动查看；新消息自动显示最新一条，展开时定位到最新消息。面板高度贴合内容和左侧控件，超过可用高度（最多 260dp）才上下滚动；主动展开后保持打开，点头像收起；展开面板底部显示文字输入框和右侧发送按钮，点输入框才弹出系统键盘，可使用输入法语音输入，原来的按住说话保留。文字回复和语音回复沿用同一发送目标与「一起看」开关，空白或忙碌时不重复发送，服务端接收消息后即清空该次草稿，无需等待 AI 回复结束；提交失败保留草稿。发送进度只显示在底部，不替换 AI 正文。消息底板不透明度约 69%。头像下方依次放屏幕分享开关和小麦克风，所有控件集中在左侧，右侧留给正文；按住说话、松开发送，上滑取消。录音会转写成文字，并通过原来的聊天发送接口进入最后一次成功发消息的窗口；只打开或切换窗口不会改变这个目标。头像、名字、群聊模型与语音播放设置沿用现有配置。

头像下方的屏幕图标是「一起看」开关，不显示文字：点亮时发送回复附上新的手机截图，灰色时只发消息；开关状态会记住。Android 14 及以上且无障碍服务已连接时，截取当前应用窗口，悬浮框保持显示；旧版或无障碍不可用时沿用暂时隐藏悬浮窗的屏幕截图方式。截图失败时不会使用旧画面或继续发送。第一版按交互读取画面，不持续采集视频或视频原声。

麦克风服务在小家前台开启，只有按住按钮时才录音；回到小家或锁屏会隐藏卡片并取消当前录音。关闭可用设置页或常驻通知的「关闭」按钮。系统结束服务后，下次打开小家会重新启动；悬浮触摸、后台录音和截图需要在具体手机上验证。

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

### 坑 10：CLI 管线图片不能用 base64 内嵌
**现象**：用户在 Aion 私聊（Gemini CLI）和 Connor 私聊/群聊（Codex CLI）发送图片时报错。Gemini CLI: `Separator is not found, and chunk exceed the limit`；Codex CLI: `Input exceeds the maximum length of 1048576 characters`。
**原因**：CLI 工具通过 stdin 管道接收 prompt，有长度限制（Codex CLI 明确 1MB 上限）。一张普通照片 base64 编码后几百 KB 到几 MB，直接内嵌必然超限。而 Gemini 原生 API 通过 HTTP POST JSON body 传输，几乎无大小限制，所以同样的 base64 方式在 API 调用中没问题。
**解决**：CLI 管线改为传本地文件绝对路径，让 CLI 自行读取文件。API 管线（硅基流动/Gemini 原生）保持 base64 不变。
**教训**：**不同 AI 调用方式对输入大小的限制不同。CLI stdin 有长度上限，不能简单复用 API 的 base64 方案。CLI 工具原生支持读取本地文件路径，应该利用这个能力。**

### 坑 11：`fetch_merged_timeline` 的 room_id 参数语义混淆
**现象**：Connor 私聊窗口合并时间线查询不到任何群聊消息，只能看到自己的私聊历史。
**原因**：`fetch_merged_timeline(who, limit, room_id=room_id)` 中的 `room_id` 参数用于指定**群聊房间**ID，但 `build_connor_1v1_prompt` 错误地传入了 1v1 私聊的 room_id。函数内部用这个 room_id 去 `chatroom_rooms` 表查 `type='group'` 的房间，自然查不到。
**解决**：Connor 1v1 调用时不传 `room_id`，让函数自动查找最新的群聊房间。
**教训**：**函数参数语义不明确时容易误传。`room_id` 这个参数名无法区分是私聊房间还是群聊房间，应该在文档/注释中明确标注参数用途。**

### 坑 12：Connor 群聊管线手动转纯文本丢失附件
**现象**：Connor 群聊回复时看不到用户发送的图片，即使 `_build_cli_prompt` 已经支持附件处理。
**原因**：`_reply_connor` 在调用 `stream_connor_cli` 之前，先把 `connor_history`（含 attachments）手动转为纯文本字符串（遍历 content 拼接），然后 `stream_connor_cli(prompt_text)` 再包装成 `[{"role": "user", "content": text}]`。附件信息在手动转文本这一步就已丢失，`_build_cli_prompt` 的附件处理逻辑永远触发不到。
**解决**：`stream_connor_cli` 新增 `messages` 参数，`_reply_connor` 直接传 `connor_history`（保留 attachments），跳过手动转文本步骤。
**教训**：**修复底层函数（`_build_cli_prompt`）时，必须检查整条调用链是否有中间层把信息提前丢弃了。"数据在到达修复点之前就已经丢失"是很隐蔽的 bug。**

### 坑 13：Android Studio Run/Deploy 不更新手机上的 APK（终极大坑）
**现象**：修改 Java 代码后在 Android Studio 中 Build 成功，但手机上运行的始终是旧版本。新加的 switch case 不生效、新加的权限不弹出、通知内容不变。反复修改代码、Clean Build、重启 Android Studio 均无效，误以为是代码逻辑问题或 vivo ROM 兼容性问题，浪费大量时间排查。
**原因**：Android Studio 的 Run 按钮（绿色三角）虽然显示 Build 成功，但实际上**没有将新 APK 推送到手机**。可能是 ADB 连接配置、部署配置（Run Configuration）或 Instant Run/Apply Changes 缓存问题。通过 `dexdump` 分析编译产物确认 DEX 文件中包含新代码，证明编译本身没问题，是部署环节断裂。
**解决**：放弃 Android Studio 的 Run 部署，改用 ADB 命令行直接安装：
```powershell
# 1. Android Studio 中 Build → Clean and Assemble Project
# 2. 命令行安装（覆盖安装用 -r，首次或改权限用先卸载再装）
& "C:\Users\32816\AppData\Local\Android\Sdk\platform-tools\adb.exe" install -r "F:\MyDreamWorld\trunk\AionsHome\AionApp\app\build\outputs\apk\debug\app-debug.apk"
```
**教训**：**永远不要假设 Android Studio 的 Run 按钮真的部署了新 APK。如果改了代码但手机上行为没变，第一件事不是怀疑代码逻辑，而是验证 APK 是否真的更新了（检查 versionCode / 加 debug 标记 / adb 命令行装）。ADB 命令行安装是最可靠的部署方式。**

### 坑 14：Python 端 workaround 广播导致 Android 重复通知
**现象**：聊天室每条 AI 消息弹出两条通知——一条发送者名字大写（来自 `msg_created`），一条小写（来自 `chatroom_msg_created`）。
**原因**：因为坑 13（APK 没更新），误以为 `chatroom_msg_created` 在 vivo 上不生效，在 Python 端 `_save_msg()` 和 `_save_to_chatroom()` 中加了 workaround——每次广播 `chatroom_msg_created` 后额外广播一条 `msg_created`。实际上 `chatroom_msg_created` 一直正常工作，只是手机跑的是旧 APK 没有对应的 case 处理。坑 13 解决后，两种广播都触发通知，造成重复。同时 `chatroom_msg_created` handler 中缺少 sender 首字母大写处理。
**解决**：
  ① 删除 Python 端 `chatroom.py` `_save_msg()` 和 `schedule.py` `_save_to_chatroom()` 中的 `msg_created` workaround 广播
  ② Java 端 `chatroom_msg_created` case 中添加 `sender = sender.substring(0, 1).toUpperCase() + sender.substring(1)` 首字母大写
**教训**：**不要在没找到根因的情况下加 workaround。workaround 会在根因修复后变成 bug（本例中造成重复通知）。应该先确认"代码是否真的跑在设备上"，再判断逻辑是否有误。**

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

## 注意事项
- 搬迁目录后需修改 `一键启动.bat` 中的路径（第11行 `cd /d` 后面的绝对路径）
- 所有数据路径都是相对路径，搬迁不影响
- VPN (singbox) 可能干扰局域网访问，必要时关闭或加直连规则
- 防火墙已添加 8080 端口入站规则（规则名 "Aion Chat 8080"）
- 备份只需复制 `data/` 文件夹
