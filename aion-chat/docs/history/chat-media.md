# 聊天、记忆、通话与媒体（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

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
9. **语音消息** — 微信风格按住说话录音，松手发送。浏览器使用 MediaRecorder 录制 WebM，Android 使用原生 AudioBridge 录音。录音通过硅基流动 ASR 自动转写为文字，消息以语音气泡形式展示（显示时长 + 播放按钮），转写文本同时保存供记忆/上下文使用
9. **聊天记录文件管理** — 自动导出 .md，文件管理器弹窗查看/下载/删除
10. **API Key 管理** — 界面内设置面板，支持 Gemini + Gemini Free（哨兵+向量）+ 硅基流动 + 中转站 + Tavily 等 Key；个人 Key 仅保存到本机 `data/settings.json`，不写死进代码仓库，`清理个人数据.bat` 会一并清理
10a. **链接预览卡片** — 用户或 AI 消息中出现 http/https 地址时，后端自动抽取标题、摘要、来源、图标/预览图并保存为 `link_preview` 附件；内网/本机地址不主动抓取，仅生成可点击 fallback 卡片
11. **手机适配** — 侧栏抽屉式展开，聊天气泡布局，触屏友好，`@media (max-width: 768px)` 单独优化紧凑间距
62. **聊天头像** — 用户/AI 消息旁显示圆形头像（`public/UserIcon.png` / `public/AIIcon.png`），用户右侧、AI 左侧
63. **多气泡拆分** — AI 回复中 `\n\n` 自动拆分为多个独立消息气泡，像微信连发效果，流式输出实时拆分
64. **时间内联显示** — 消息时间显示在用户/AI 名字旁边，不再独占一行
12. **当前时间注入** — 每次发消息/重新生成时，将准确时间拼接到 prompt 前缀最后一条 assistant 回复中

### 联网搜索与网页读取
12a. **Tavily 联网搜索** — 配置 Tavily API Key 且开启「联网搜索/网页读取」能力后，模型可输出 `[WEB_SEARCH:查询内容]` 主动搜索最新网页信息，后端返回已清洗的标题、摘要、来源 URL 和答案上下文
12b. **网页读取** — 模型可输出 `[WEB_EXTRACT:完整URL]` 读取用户或 AI 分享的具体网页，适合“看一下这个链接内容”的场景；结果会作为下一轮上下文回灌给模型
12c. **指令隐藏与 TTS 过滤** — `[WEB_SEARCH]` / `[WEB_EXTRACT]` 原始指令会从聊天气泡和 TTS 文本中剥离，用户只看到自然回复与可见的系统提示
12d. **可见系统消息** — AI 使用搜索/网页读取时，会在聊天窗口插入一条 system 消息记录“发起了联网搜索/网页读取”，方便用户知道模型何时使用了外部信息
12e. **主聊天 + 聊天室共用** — 主聊天和聊天室共用同一套 Tavily Key、能力开关、指令解析和结果回灌逻辑，两个 AI 参与方都可以按需调用

### Debug 透明度面板
13. **Token 用量追踪** — Gemini usageMetadata + 硅基流动 usage，在 SSE 流中通过 debug 事件传回前端
14. **记忆召回可视化** — 每次发消息时召回的记忆条目，显示综合分数 + 三维分解（vec_sim / kw_score / importance）
15. **完整 Prompt 查看** — 可展开查看发送给模型的全部 prompt 消息（截断前500字/条）
16. **Debug 条** — 每条 AI 消息下方显示：模型名、输入/输出/总 token、召回记忆数，点击展开详情

### 向量记忆库（RAG 重构）
17. **记忆总结（手动 + 自动）** — 手动：用户点击「总结新记忆」按钮触发（无最低条数限制）。自动：每 30 分钟检测，若用户已 30 分钟未对话且未总结消息 ≥ 30 条则自动触发。两者共用同一套逻辑和锚点，不会重复总结。从锚点之后的消息开始，每 30 条一组串行处理（余数 <10 合并到最后一组），使用当前聊天的核心模型（而非 flash-lite）抽取多条原子记忆：一条记忆只记同一天的一件事，`content` 必须以 `YYYY-MM-DD，` 开头，日期也写入 keywords，确保日期参与 embedding 和关键词召回。Prompt 明确保留具体测试反馈、有趣场景、关系氛围和生活线索，但丢弃普通吃喝睡/一次性状态等流水账；模型必须为每条记忆输出真实 `source_message_ids`，后端把这些 id 写入 `source_msg_id` 挂载来源原文，并用它们派生这条记忆的发生时间。模型成功处理一组后更新锚点，即使该组没有值得写入的新记忆也不会反复总结噪音。全部总结完成后，再调用一次核心模型生成私密日记并存入日记本；模型可同时决定是否发布一条朋友圈，不再把总结感慨插入聊天窗口。
18. **本地前置路由（instant_digest）** — 每次用户发消息时以本地规则判断记忆指代、细节需求和群聊首位回复者，并直接用最新用户文本做向量召回查询，不再阻塞等待模型
19. **向量化存储** — 使用 Gemini `gemini-embedding-001`（3072维）将记忆向量化，存入 SQLite memories 表，每条记忆含 keywords（JSON 关键词数组）、importance（重要度）、source_start_ts/source_end_ts（来源发生时间范围）、source_msg_id（挂载原文 id）、unresolved（是否待办/未完成）。API 和 prompt 注入会派生 `memory_time/memory_time_label`，优先展示“发生”时间；无 source 时间时才显示“记录”时间。`evidence_summary` 仅为旧数据兼容字段，新总结不再依赖它。
20. **综合评分召回** — `final_score = vec_sim × 0.6 + kw_score × 0.3 + importance × 0.1`，threshold=0.45，Top 5。关键词匹配支持子串模糊命中
21. **记忆原文追溯（fetch_source_details）** — 当 `require_detail=true` 时，优先按召回记忆的 `source_msg_id` 注入挂载原文；只有旧记忆没有精确来源 id 时，才按 source 时间范围和关键词回退筛选原始对话记录。
22. **总结锚点管理** — 锚点持久化在 `data/digest_anchor.json`，UI 显示当前锚点时间 + 日期选择器可回退
23. **可视化管理** — 侧边栏「🧠 记忆库」按钮，支持搜索/添加/编辑/删除，编辑后自动重新向量化。每条记忆显示关键词标签 + 重要度分数 + 发生时间，编辑时可修改关键词和重要度。有 source 时间范围或精确来源的记忆可点击 📜 查看并重新筛选挂载原文。📌 按钮可切换记忆的“待办/未完成”状态，unresolved 的记忆以橙色高亮显示

### 语音合成 (TTS) — 服务端流式推送架构
24. **服务端流式 TTS** — AI 流式输出过程中，后端 `tts.py` 的 `TTSStreamer` 实时按句切分文本（100-200 字，按句号/问号/感叹号/换行等断句），每句异步调用硅基流动 CosyVoice2-0.5B 合成 mp3，合成完成后立即通过 WebSocket 或 SSE 推送 `tts_chunk` 事件给前端，前端收到即可开始播放，无需等待全文生成完毕。SSE 模式通过 `sse_queue` 参数支持，用于小剧场和奥罗斯幽林等独立 SSE 流场景
25. **多场景触发** — 用户发消息后的 AI 流式回复、重新生成、Core 主动发言（哨兵唤醒/闹铃/定时监控/[CAM_CHECK] 跟进）均自动创建 TTSStreamer 进行流式合成
26. **音色选择** — 齿轮配置面板内选择硅基流动账号下的自定义音色，通过 WebSocket `tts_state` 消息同步到服务端
27. **前端队列播放** — 前端维护 `ttsQueue`（Map 结构，key 为 msg_id），每条消息的分片按 seq 顺序播放，多条消息按到达顺序排队；播放完最后一片后服务端广播 `tts_done` 事件，前端清理队列并继续下一条
28. **多端 TTS 状态同步** — 前端开启 TTS 后通过 WebSocket 发送 `tts_state` 消息（含 enabled/voice/can_play/active_at），服务端 `ConnectionManager` 在 `tts_clients` 字典中跟踪各客户端状态，并将 `tts_chunk`/`tts_done` 定向给最近真实交互过的可播放客户端；页面进入后台、pagehide/freeze 时不会撤销播放资格或清空队列，手机端接管后最小化/切页仍可继续播放，直到电脑端或其他页面重新交互并刷新 `active_at` 后接管；同时 HTTP POST（send_message/regenerate）的 body 中也携带 `tts_enabled`/`tts_voice` 作为 `_tts_fallback` 回落，确保服务端发起的消息（cam_check/闹铃/监控）也能获取 TTS 状态
28b. **TTS 音频缓存** — 合成的音频分片存储在 `data/tts_cache/` 目录，文件名为 `{msg_id}_s{seq}.mp3`，前端可通过 `/api/tts/audio/{chunk_name}` 获取；点击 AI 消息的 🔊 图标可重播已缓存的 TTS 分片
28c. **TTS 重播** — 点击聊天气泡下的喇叭图标，前端通过 HEAD 请求探测 `{msg_id}_s0`、`{msg_id}_s1`... 是否存在，依次播放所有分片；支持 GET 和 HEAD 两种 HTTP 方法

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
204. **视频通话模式** — 摄像头预览 + 按住录制视频片段 + ASR 转写 + AI 多模态理解，实现「看到你 + 听到你 + 跟你说话」的完整通话体验。录制的视频片段直接发送给 Gemini（inline_data，最大 20MB），AI 可以看到画面和听到声音
205. **用户主动发起** — 聊天页面 📹 按钮发起视频通话，3 秒等待期间播放铃声动画，用户可取消
206. **AI 主动发起** — AI 回复包含 `[视频电话]` 指令时，后端延迟 10 秒后通过 WebSocket 定向推送给发送者客户端，前端弹出来电 UI（铃声 + 接听/挂断按钮）
207. **来电指示器** — AI 回复触发视频通话时，消息底部显示「📹 AI 正在发起视频通话...」动画指示器，10 秒后自动消失并触发来电 UI
208. **摄像头预览** — 全屏通话界面显示用户摄像头画面（主画面）和 AI 头像（画中画），支持点击切换大小画面位置
209. **按住录制** — 通话界面底部「🎙 按住录制」按钮，按住开始录制视频+音频，松手停止并自动发送。支持上滑取消录制，最长录制 60 秒（自动停止）
210. **浏览器双轨录制** — PC/手机 Chrome 使用双 MediaRecorder：一轨录制视频+音频（WebM），一轨单独录制音频（用于 ASR 转写），确保视频和转写文本同时获得
211. **Android 原生视频录制** — `VideoBridge.java` 使用 MediaCodec(H.264) + MediaCodec(AAC) + MediaMuxer 编码 MP4，复用 CameraBridge 的 NV21 预览帧和 AudioBridge 的 PCM 音频帧，录制期间摄像头预览不中断
212. **视频片段附件** — 录制完成后上传视频文件，ASR 转写音频，构建 `{type: "video_clip", url, duration, transcript}` 附件发送到聊天
213. **Gemini 视频理解** — 当前消息的视频以 inline_data 发送给 Gemini（支持 video/mp4、video/webm），AI 可以同时理解画面和音频内容；历史消息中的视频片段仅保留转写文本 `[视频通话] {transcript}`，不重复发送视频数据
214. **视频气泡** — 聊天记录中视频片段以渐变色气泡展示（📹 图标 + 时长），点击可全屏播放
215. **前后摄像头切换** — 通话界面 🔄 按钮支持前后摄像头切换
216. **不活跃自动挂断** — 120 秒内无任何录制操作自动挂断通话
217. **开关控制** — 聊天配置面板中「视频通话」开关控制 AI 是否具有发起视频通话的能力（`[视频电话]` 指令是否注入 prompt），用户主动发起不受开关影响
218. **前端指令过滤** — 流式输出时前端实时 strip `[视频电话]`，用户看不到原始指令

### AI 生图（[SELFIE:xxx] / [DRAW:xxx]）
设置 → 生图模型可选择 **Codex 登录额度**：通过现有 Codex CLI 的 App Server 调用内置生图，复用小家的 ChatGPT 登录态，不需要生图 API Key，也不会在失败后切换至付费 API。原有 API 配置继续保留，可随时切回。Codex 单次生图最多等待 600 秒（10 分钟），超时中止该任务。

设置 → 生图模型 → **管理人物锚点**，与小剧场的「人物锚点」打开同一个编辑器，直接读写原有的人物资料。名字或别名匹配聊天配置时自动识别身份，也可以手动绑定；修改一次，两边生效。私聊和群聊让模型在原指令内注明出场人物，例如 `[DRAW: characters=anchor_人物编号1,anchor_人物编号2 | English scene prompt]`；不含已保存人物时使用 `characters=none`。后端只附这些人物的照片，名字与照片逐张对应，服饰、动作、画风和环境由当前场景决定。

自拍优先使用共用人物锚点；没有对应人物时，才按内部身份使用 `public/生图锚点.jpg` 或 `public/2号机生图锚点.jpg`。已对应的人物照片失效会提示失败，不会悄悄换脸。小剧场继续按故事所选人物和场景人物姓名或别名筛选参考照。Codex、兼容 API 和 Gemini 共用相同的参考图选择结果，图片由统一相册保存并回填聊天或章节。

400. **AI 自主生图** — AI 在回复中输出 `[SELFIE:prompt]`（自拍）或 `[DRAW:prompt]`（自由创作）指令，后端自动检测并异步调用设置中选择的生图来源
401. **SELFIE 模式** — 按明确出场人物带照片；旧格式默认取发言者的共用锚点，无绑定时兼容原有自拍照片
402. **DRAW 模式** — 按指令中的出场人物附带参考照；`characters=none` 或旧纯文本格式不附带人物参考图
403. **异步非阻塞** — 生图任务通过 `asyncio.create_task()` 在后台执行，不阻塞聊天流和页面操作。生图期间用户可继续发消息、切换页面
404. **加载指示器** — 检测到生图指令后在 AI 消息下方显示「🎨 {AI名} 正在发送图片 ● ● ●」橙色主题弹跳动画（renderMessages 重建后自动恢复）
405. **图片消息** — 生图完成后由统一相册保存图片，创建新 assistant 消息（附带图片附件），通过 WebSocket 广播 `msg_created` + `image_gen_done` 事件
406. **图片查看器** — 聊天中的图片点击后弹出全屏 lightbox，支持保存图片（浏览器用 blob 下载，Android App 通过 `AionImageSaver` 原生桥接写入相册）
407. **前端指令过滤** — 流式输出时前端实时 strip `[SELFIE:xxx]` 和 `[DRAW:xxx]`，用户看不到原始指令
408. **TTS 过滤** — TTS 合成时自动剥除 `[SELFIE:...]` 和 `[DRAW:...]` 内容，不会被语音朗读
409. **开关控制** — 聊天配置面板中「AI 生图」开关控制 AI 是否具有生图能力（指令是否注入 prompt），关闭后 AI 不会尝试生图
410. **Gemini API** — 使用 `gemini-3.1-flash-lite-image` 模型，REST `generateContent` 端点，`responseModalities: ["IMAGE", "TEXT"]`，120 秒超时
411. **三处统一** — send_message、regenerate、Core/语音/定时触发三套 `_bg_generate` 函数均支持生图指令检测和异步生图

### AI 生图工作流程
```
【AI 触发生图（正常聊天 / 语音 / Core 主动发言）】
  AI 回复包含 [SELFIE:穿着白裙子在花园里] 或 [DRAW:一只飞翔的龙]
  → 后端 regex 检测 → 从显示文本和数据库中 strip 掉
  → SSE 发 image_gen_start 事件 + WebSocket 广播
  → 前端显示「🎨 正在发送图片」橙色指示器
  → asyncio.create_task(_do_image_gen(...))

【异步生图（_do_image_gen）】
  → generate_chat_image 解析 characters= 出场人物并读取共用锚点
  ├ 有出场人物：逐张带上名字、照片和固定外貌
  ├ 旧 SELFIE：优先匹配发言者，无绑定时使用原自拍照片
  ├ 无出场人物：仅发送场景描述
  → generate_image 调用所选生图来源（Codex / 兼容 API / Gemini）
  → 解析返回图片 → 保存到统一相册
  → 创建 assistant 消息（附带图片附件 URL）
  → WebSocket 广播 msg_created + image_gen_done
  → 前端移除指示器 + 渲染新消息（含图片）

【生图失败】
  → WebSocket 广播 image_gen_failed
  → 前端移除指示器并提示失败；群聊对超时、空结果及锚点失效均回填说明
  → Codex 超时保留 TimeoutError 到系统日志，避免丢失为笼统的“模型未返回结果”

【图片保存（全屏查看器）】
  点击图片 → 全屏 lightbox
  ├ 浏览器：fetch → blob → createObjectURL → <a download> 模拟点击
  └ Android App：fetch → blob → FileReader → base64 → AionImageSaver.save() → MediaStore 写入相册
```

### AI 生成歌曲（[SONG]...[/SONG]）
412. **AI 自主写歌** — 开启「允许 AI 生成歌曲」后，系统能力会注入 `[SONG]...[/SONG]` 写歌格式。AI 先在普通回复里给一句简短回应/歌名，再把完整歌曲生成参数放进隐藏 SONG 块
413. **歌词格式约束** — SONG 块使用 `Title`、`Style`、`Singer/Vocal`、`Duration`、`Prompt`、`Lyrics` 字段；歌词使用 `[Verse 1]`、`[Pre-Chorus]`、`[Chorus]`、`[Bridge]` 等分段标签，避免生成器拿到散乱文本
414. **声线控制** — `Singer/Vocal` 字段必须写明声线和唱法，例如 male baritone、male bass、male tenor、female alto、duet、choir、instrumental。用户要求男声时会明确避免女声
415. **异步生成** — 后端从 AI 回复中提取 SONG 块并清理显示文本，通过后台任务调用 Gemini Lyria，不阻塞继续聊天
416. **Lyria API** — 当前使用 `lyria-3-pro-preview`，走现有 Gemini 付费 Key，REST `generateContent` 端点，解析返回的 audio `inlineData`
417. **歌曲存储** — 生成音频保存到 `data/songs/song_gen_{timestamp}.{ext}`，通过 `/songs/*` 静态路由访问，不再混入 `data/uploads/`
418. **歌曲消息** — 生成完成后创建新的 assistant 消息，正文为 `为你写的歌《歌名》`，附件类型为 `generated_song`
419. **附件数据** — `generated_song` 包含 `url`、`title`、`mime_type`、`model`、`lyrics`、`prompt`、`description` 等字段，歌词只保存在附件/播放器中，不额外刷一条超长聊天消息
420. **音乐卡片** — 聊天和聊天室都会渲染生成歌曲卡片，显示歌名、模型、音频控件和「打开播放器」按钮
421. **播放器子页** — 点击卡片打开小播放器浮层，展示歌名、歌词滚动区、播放/暂停、时间和进度条；进度通过 audio 事件 + `requestAnimationFrame` 同步刷新
422. **等待提示** — 歌曲生成期间在触发消息下方显示「歌曲谱写中....」，收到完成/失败事件或新歌曲卡片后自动移除
423. **开关控制** — 聊天配置面板中的「允许 AI 生成歌曲」控制 SONG 能力是否注入 prompt；私聊和群聊/聊天室共用同一开关

### AI 生成歌曲工作流程
```
【AI 触发写歌（私聊 / 群聊）】
  用户要求写一首歌
  → AI 输出短回复 + [SONG]...[/SONG] 隐藏块
  → 后端提取 SONG prompt，清理可见回复和误输出歌词
  → SSE/WS 广播 song_gen_start 或 chatroom_song_gen_start
  → 前端显示「歌曲谱写中....」
  → asyncio.create_task(_do_song_gen / _chatroom_song_gen)

【异步生成（song_gen.generate_song）】
  → POST https://generativelanguage.googleapis.com/v1beta/models/lyria-3-pro-preview:generateContent
  → 解析 response → 提取 audio inlineData
  → 保存到 data/songs/song_gen_{timestamp}.{ext}
  → 创建 assistant 消息，attachments=[{type:"generated_song", ...}]
  → WebSocket 广播 msg_created + song_gen_done / chatroom_song_gen_done
  → 前端移除等待提示 + 渲染歌曲卡片

【生成失败】
  → WebSocket 广播 song_gen_failed / chatroom_song_gen_failed
  → 前端移除等待提示
```

215. **消息保存** — 通话中的视频片段正常保存到聊天记录，视频文件上传到 `data/uploads/`，转写文本存入附件数据
216. **Chrome 兼容** — PC/手机 Chrome 浏览器使用标准 `getUserMedia` API 获取摄像头+麦克风，无需原生桥；Android WebView 自动 fallback 到 `CameraBridge` + `AudioBridge` + `VideoBridge`

### 视频通话工作流程
```
【用户主动发起】
  点击 📹 按钮 → 3 秒等待（播放铃声动画）→ 进入通话界面
  → 启动摄像头（getUserMedia 或 AionCamera 原生桥）
  → 启动音频流（仅预热，不录制）
  → 底部显示「🎙 按住录制」按钮 + 挂断按钮

【按住录制（核心交互）】
  按下录制按钮 → 开始录制：
    浏览器：双 MediaRecorder（视频+音频 WebM / 纯音频 WebM）
    Android：AionVideo.startRecord() → MediaCodec H.264 + AAC + MediaMuxer
  → 界面显示录制计时 + 红色脉冲动画
  → 上滑进入「取消区域」→ 松手取消录制
  → 正常松手 → 停止录制：
    浏览器：双 Blob → 上传视频 + ASR 转写音频
    Android：AionVideo.stopRecord() → base64 MP4 → 上传 + 收集的 PCM 帧构建 WAV → ASR
  → 构建 {type:"video_clip", url, duration, transcript}
  → POST 发送消息到聊天（视频 inline_data 发给 Gemini）
  → AI 回复 + TTS → 录制按钮暂时禁用
  → TTS 播完 → 恢复录制按钮 → 等待下次按住

【AI 主动发起】
  AI 回复包含 [视频电话] → 后端检测 → 从显示文本 strip
  → 消息底部显示「📹 正在发起视频通话...」指示器
  → 10 秒延迟 → WebSocket 定向推送 video_call_ring 给发送者
  → 前端弹出来电 UI（铃声 + 接听/挂断）
  → 用户接听 → 进入通话界面（同上）
  → 用户挂断/超时 → 取消

【历史消息处理】
  当前消息 → 视频以 inline_data 发送给 Gemini（AI 看到画面+听到音频）
  历史消息 → 仅保留转写文本「[视频通话] {transcript}」（不重复发送视频）
  记忆总结/哨兵 → 仅使用转写文本

【Android 原生视频录制桥（VideoBridge.java）】
  JS 调用 AionVideo.startRecord(width, height)
  → 创建 MediaCodec 视频编码器（H.264, NV12）+ 音频编码器（AAC, 16kHz mono）
  → MediaMuxer 准备写入临时 MP4 文件
  → CameraBridge.processFrame() 中转发 NV21 帧 → NV21→NV12 转换 → 送入视频编码器
  → AudioBridge 录音线程中转发 PCM 帧 → 送入音频编码器
  → 编码输出同步写入 MediaMuxer
  → JS 调用 AionVideo.stopRecord() → flush + stop → 读取 MP4 文件 → base64 返回

【Android 原生摄像头桥（CameraBridge.java）】
  JS 调用 AionCamera.start("user"|"environment")
  → Camera.open() → 设置 640×480 NV21 预览
  → setPreviewCallbackWithBuffer（3 个预分配 buffer，零 GC）
  → 摄像头回调 → 复制数据到 inputBuf → 归还 camera buffer
  → 后台 ExecutorService 线程：
    ├ NV21 纯字节数组旋转（rotateNV21_CW90/270/180，~1ms）
    ├ YuvImage.compressToJpeg（已旋转的竖屏 JPEG）
    ├ Base64 编码 → 更新 lastFrameB64
    └ 录制中？→ 转发旋转后的 NV21 帧给 VideoBridge.onVideoFrame()
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
71c. **自动播放** — AI 点歌后浏览器自动开始播放第一首歌曲，无需用户手动点击；闹铃/定时监控触发时 AI 也可点歌并自动播放，实现音乐闹钟效果；音乐事件走独立 `music` 广播和播放器，不受 TTS 播放端 lease 限制
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

### 背景记忆浮现（替代旧版“近期记忆注入”）
104. **智能背景记忆浮现** — 每次发消息/重新生成时，通过三层策略构建背景记忆（最多 8 条）：① unresolved 记忆优先（最多 2 条，待办/未完成的事项）→ ② 话题相关浮现（用即时哨兵的 topic 做 embedding 匹配，Top 3）→ ③ 近期补充（最近 3 天，补满 8 条）。与 RAG 精确召回自动去重
105. **Unresolved 标记** — 记忆表新增 `unresolved` 字段，标记悬而未决的计划/约定/承诺。总结时核心模型自动判断，UI 中可通过 📌 按钮手动切换。unresolved 记忆在背景记忆中以 📌 前缀注入，确保 AI 记得追问

### 上下文系统消息注入
155. **选择性注入** — 发送给模型的上下文中，system 消息不再全部过滤，而是选择性保留「点歌」和「查看监控」相关的系统事件（如"Aion查看了监控画面"、"Aion搜索了周边的餐饮美食信息"），以 `[系统事件]` 前缀包装为 user 角色注入，让 AI 知道之前发生过什么
156. **范围限制** — 系统消息与普通消息共享同一个 `LIMIT` 查询，上下文默认 30 条窗口内的系统事件才会被带入，不会加载全部历史
157. **关键词过滤** — 只有包含"查看了监控"、"搜索了"等关键词的 system 消息才会保留，闹铃/日程/玩具等系统消息仍然不进入上下文

### 语音唤醒快速聊天模式
158. **fast_mode 参数** — `MsgCreate` 和 `regenerate_message` 新增 `fast_mode` 参数，语音唤醒发送消息时自动启用
159. **跳过记忆检索** — `fast_mode=True` 时跳过即时哨兵（`instant_digest`）、背景记忆浮现（`build_surfacing_memories`）、向量记忆召回（`recall_memories`），仅注入当前时间，大幅降低语音聊天延迟
160. **保留核心能力** — 快速模式下仍保留世界书人设、系统能力指令、日程列表、位置信息等静态注入，AI 回复质量不受太大影响
161. **消息正常保存** — 语音通话的消息仍正常存入数据库和导出聊天记录，仅跳过记忆检索环节

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
    fetch_source_details: 优先读取 source_msg_id 对应的挂载原文，旧记忆无精确来源 id 时才回退范围筛选
    → 记忆来源原文追加注入 prompt

【记忆总结 _do_digest — 手动点击按钮 / 自动定时触发】
  自动触发条件：每 30 分钟检测，用户已 30 分钟未对话 且 未总结消息 ≥ 30 条
  手动触发：无最低条数限制，共用锚点不会重复总结
  每组消息 → 核心模型输出 memories[] → 多条原子记忆分别写库
  每条记忆：content（YYYY-MM-DD 开头的单一事实）+ keywords（含 YYYY-MM-DD）+ source_msg_id（挂载原文 id）+ source_start_ts/source_end_ts（发生时间范围）
  从锚点时间之后取消息 → 每 30 条一组（余数<10合并）→ 串行处理：
    核心模型（当前聊天模型）分析，注入世界书 AI/用户人设 → 输出 JSON:
      {"summary": "...", "keywords": [...], "importance": 0.8, "unresolved": true/false}
    → embedding 向量化 → 存入 SQLite → 更新锚点
  全部组处理完毕后 → 核心模型带上下文生成私密日记 → 存入 diary_entries，可选写入 moments
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
  → 全部完成后生成私密日记，存入日记本；可选发布朋友圈
```
