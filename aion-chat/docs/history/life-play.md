# 共读、生活、游戏与好友串门（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

### 朋友圈（微信朋友圈风格，替代旧版心语）
162. **[MOMENT:xxx|true/false] 指令** — AI 在私聊或群聊回复中输出 `[MOMENT:朋友圈内容|true]` 发布朋友圈动态，`true`/`false` 控制是否期望其他人回复
163. **前端实时过滤** — 流式输出时实时 strip `[MOMENT:xxx]`，用户在聊天界面看不到原始指令
164. **三人参与** — 用户、Aion、Connor 三人均可发朋友圈（用户通过页面发布，AI 通过指令发布），均可点赞/点踩/评论
165. **AI 自动回复** — 新朋友圈发布后（expect_reply=true 或用户发布），自动触发 Aion 和 Connor 回复评论，回复顺序随机，1-3秒延迟，70% 概率同时点赞
166. **AI 回复上下文** — AI 回复时注入角色人设 + 最近 5 条记忆 + 最近 30 条聊天消息（私聊+群聊合并）+ 朋友圈内容及已有评论，确保回复自然贴合语境
167. **模型跟随主聊天** — Aion 回复朋友圈使用用户当前私聊会话选用的主模型（而非默认 lite 模型），Connor 使用 Codex CLI
168. **点赞/点踩** — 每人每条朋友圈只能选择点赞或点踩其一，再次点击取消，显示点赞/踩人名列表
169. **一级评论嵌套** — 支持回复评论（显示「A 回复 B」），用户评论后触发 AI 自动跟评
170. **未读红点** — 主页朋友圈图标显示红点徽章（每 60 秒检查 `/api/moments/unread`），进入朋友圈页自动标记已读
171. **发布弹窗** — 右上角"＋"按钮弹出悬浮窗口编辑发布，不占用列表空间
172. **评论弹窗** — 点击评论气泡弹出悬浮窗口输入评论，与发布交互一致
173. **实时同步** — WebSocket 广播 `moment_new`/`moment_comment`/`moment_reaction`/`moment_reaction_removed` 事件，多端实时刷新
174. **数据库设计** — 4 张新表：`moments`（动态）、`moment_comments`（评论）、`moment_reactions`（点赞/踩，UNIQUE约束）、`moment_read_anchor`（未读锚点）
175. **API 接口** — `GET /api/moments`（分页列表）、`POST /api/moments`（发布）、`DELETE /api/moments/{id}`（删除）、`POST /api/moments/{id}/react`（点赞/踩）、`POST /api/moments/{id}/comments`（评论）、`GET /api/moments/unread`（红点检查）、`POST /api/moments/mark-read`（标记已读）
176. **主页入口** — home.html「朋友圈」图标链接到 `/moments`，位于 Dock 栏

### 朋友圈工作流程
```
【AI 发朋友圈（私聊/群聊过程中）】
  AI 回复包含 [MOMENT:今天天气真好|true]
  → 后端 regex 检测 → 从显示文本和数据库中 strip 掉
  → 存入 moments 表（记录 author/source_conv）
  → WebSocket 广播 moment_new
  → expect_reply=true → 触发另一个 AI 回复评论（随机顺序，1-3秒延迟）

【用户发朋友圈（页面发布）】
  点击右上角 ＋ → 弹出发布窗口 → 输入内容 → 点击发布
  → POST /api/moments → 存入数据库
  → 触发 Aion + Connor 两个 AI 同时回复评论
  → 70% 概率同时点赞

【AI 回复评论】
  构建上下文：角色人设 + 用户人设 + 最近 5 条记忆 + 最近 30 条聊天消息 + 朋友圈内容及已有评论 + 任务指令
  → Aion：stream_ai()（用户当前私聊主模型）
  → Connor：send_to_connor() HTTP 优先，失败回退 stream_connor_cli()
  → 评论存入 moment_comments 表
  → WebSocket 广播 moment_comment

【未读红点】
  主页每 60秒 GET /api/moments/unread
  → 比较 moments/moment_comments 最新时间 vs moment_read_anchor
  → 有未读 → 朋友圈图标显示红点
  → 进入朋友圈页 → POST /api/moments/mark-read → 红点消失
```

### 日记本（用户手写 + AI 自动总结日记）
177. **数据存储** — 日记本数据存储在 SQLite `data/chat.db` 的 `diary_entries` 表中，作者使用内部标识 `user` / `aion` / `connor`，页面展示名从世界书和聊天室配置读取，不硬编码名称。
178. **AI 自动日记** — Aion/Connor 的自动记忆总结完成后，会额外调用一次模型生成私密日记并写入 `diary_entries`；同一次 JSON 输出可决定是否发布朋友圈，发布时复用 `moments` 表和 `moment_new` WebSocket 广播。
179. **用户手写日记** — `/diary` 页面标题栏右侧 `＋` 打开写日记弹窗，支持标题、心情、正文；保存后写入 `diary_entries(author='user')`。
180. **编辑与删除** — 每张日记卡右上角提供编辑和删除；编辑调用 `PUT /api/diaries/{id}`，删除调用 `DELETE /api/diaries/{id}`，并通过 WebSocket `diary_updated` / `diary_new` 做多端同步。
181. **入口位置** — Aion 私聊侧栏和 Connor 聊天室侧栏的钱包按钮下方都有「日记本」入口；主页也可通过 `/diary` 路由直接访问。
182. **API 接口** — `GET /api/diaries`（分页列表，可按 author 筛选）、`POST /api/diaries`（用户新增）、`PUT /api/diaries/{id}`（编辑）、`DELETE /api/diaries/{id}`（删除）。

### AI 陪伴阅读（EPUB 书架 + 双AI批注 + 选文多目标聊天 + 用户高亮标注）
185. **EPUB 导入** — 支持上传 `.epub` 格式电子书，后端使用 `ebooklib` 解析，自动提取目录、章节内容、封面与内嵌图片。书籍数据存储在 `data/books/{book_id}/` 目录下，每本书有独立的 SQLite 数据库（`book.db`）存储章节和批注
186. **书架界面** — `/reading` 页面上半部分为书架，网格展示已导入书籍（封面+标题+作者+章节数），支持上传新书和删除
187. **阅读器** — 点击书籍进入阅读器，显示章节标题、正文内容（含字数统计）、上/下章导航，阅读进度自动保存并恢复
188. **双AI批注系统** — 每章支持 Aion + Connor 并行逐段批注（`asyncio.create_task` 双任务），Aion 使用配置模型（默认 `gemini-3-flash`），Connor 通过 Codex CLI 生成。批注上下文包含世界书人设、合并聊天时间线（私聊+群聊 `fetch_merged_timeline`）、前章摘要。Connor 不可用时自动跳过并 toast 提示。每个 annotator 独立存储（`book_annotations` 表 `annotator` 字段区分）
189. **批注气泡** — 有批注的段落右侧显示双色气泡图标：Aion（暖橙）和 Connor（蓝色），点击分别弹出对应批注弹窗。段落摘要按钮也分 Aion/Connor 两个
190. **[MUSIC:xxx] 批注点歌** — AI 批注中可使用 `[MUSIC:歌曲名 歌手名]` 指令点歌，批注弹窗中以音乐卡片形式展示，点击通过网易云 API 搜索并在页内在线播放
191. **批注 SSE 流式生成** — 单段批注（`/api/books/{id}/chapters/{ch}/annotate`）和全章批注（`/annotate-all`）均使用 SSE 流式输出，前端逐条实时渲染批注气泡
192. **批注提示词** — 批注 Prompt 注入书名、世界书人设（AI+用户）、当前时间、前章摘要（最近 3 章、每章≤200字）、最近 15 条合并聊天时间线（私聊+群聊），让 AI 批注贴合人设和聊天氛围。Connor 使用独立 Prompt（注入 Connor persona）
193. **选文多目标提问** — 阅读器内选中文字后弹出三按钮工具栏：「💬 问Aion」/「💬 问Connor」（蓝色）/「💬 群里聊」（紫色），分别路由到 Aion 私聊（`/api/conversations/{conv_id}/send`）、Connor 1v1 房间（`/api/chatroom/rooms/{room_id}/send`）、群聊房间。群聊模式同时获取双 AI 回复
194. **嵌入式聊天面板** — 提问后在阅读页底部弹出聊天面板，根据目标加载最近 6 条对应消息（私聊/聊天室）作为上下文 + 分割线 + 当前对话。Aion 回复暖色气泡、Connor 回复蓝色气泡（含 codexicon 头像），群聊模式双气泡并行渲染。支持继续追问
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
  GET /api/books/{id}/chapters/{ch} → 返回章节内容（段落数组）+ Aion批注 + Connor批注 + AI名/Connor名/用户名
  → 前端渲染段落 + 双色批注气泡（Aion 暖橙 / Connor 蓝色）
  → 用户点击「AI 批注」→ POST /api/books/{id}/chapters/{ch}/annotate-all
  → 后端逐段处理（Aion + Connor 并行 asyncio.create_task）：
    ├ 加载世界书人设 + 查询书名
    ├ 获取前 3 章摘要（每章≤200字）
    ├ 获取最近 15 条合并聊天时间线（fetch_merged_timeline，私聊+群聊）
    ├ Aion: 构建批注 Prompt → stream_ai() 流式生成 → 解析 JSON 批注
    ├ Connor: 构建 Connor Prompt（注入 persona）→ stream_connor_cli() → 解析 JSON 批注
    └ 分别存入 book_annotations 表（annotator='aion'/'connor'）→ SSE 推送前端
  → 前端实时渲染双色批注气泡（Connor 不可用时仅渲染 Aion）

【选文聊天 + 高亮标注】
  选中文字 → 弹出三按钮工具栏「💬 问Aion」/「💬 问Connor」/「💬 群里聊」
  → 点击任一按钮 → 弹出提问窗（placeholder 显示对应目标名称）
  → 输入问题（可选）→ 提交
  → 打开聊天面板 → 根据目标加载上下文：
    ├ Aion: GET /api/conversations/{conv_id}/messages?limit=6（主聊天历史）
    ├ Connor: GET /api/chatroom/rooms/{connor_room_id}/messages?limit=6
    └ 群聊: GET /api/chatroom/rooms/{group_room_id}/messages?limit=6
  → 显示上下文 + 分割线 + 用户消息
  → 发送到对应目标：
    ├ Aion: POST /api/conversations/{conv_id}/send（SSE: chunk）
    ├ Connor: POST /api/chatroom/rooms/{connor_room_id}/send（SSE: connor_chunk）
    └ 群聊: POST /api/chatroom/rooms/{group_room_id}/send（SSE: aion_chunk + connor_chunk 双流）
  → AI 回复流式渲染为多气泡（按 \n\n 分段，Connor 蓝色气泡）
  → 回复完成后自动保存高亮：
    ├ POST /api/books/{id}/chapters/{ch}/highlights（存储段落索引+字符偏移+原文+提问+AI回复+annotator+connor_answer）
    ├ 立即在 DOM 中包裹橙色下划线（.user-hl）
    └ 下次打开章节时自动恢复所有高亮
  → 点击下划线文字 → 弹出详情面板（原文 + 提问 + AI回复，群聊模式显示双 AI 回复）
  → 支持删除单条标注（DELETE /api/books/{id}/highlights/{hl_id}）
  → 支持继续在面板内追问（同一目标）
  → 消息保存在对应聊天窗口（主聊天/聊天室）
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

**小说与对话共用小剧场界面（2026-09）** — 每个故事可切换对话/小说模式，旧对话仍保留。小说先与所选角色聊天讨论脑洞，再点“按讨论生成大纲”；生成故事共识和章节计划后，必须明确确认大纲才能开始写正文（默认目标 6500 字）。不满意可继续讨论、重新生成或恢复最近三版大纲；中途“商量剧情”后重新确认，不自动覆盖已有正文。目录支持选章阅读与收听。讨论、拟纲、写作均带所选小剧场角色的人设，没有完整人设时提示选择。讨论原文单独保存，超过12条尚未摘要的非空消息时整理早期讨论并保留最近8条用于后续讨论；正文请求只带确认后的故事共识，不带讨论原文。正文自动保存草稿，重写保留最近三版；修改或重写某章不改变后续已完成章节的状态。故事摘要只取已写正文，与日常聊天记忆隔离。章节上下文详见 [小说上下文说明](../../../docs/theater-novel-context.md)。

**首次大纲确认** — 新剧场讨论后生成的第一份大纲，确认后直接在当前剧场建立章节并进入写作，保留名称与讨论记录，不创建“重写版”。已有章节的作品重新规划全书时，确认框会明确提示创建新剧场以保留原作品；“复制大纲为新剧场”仍是独立入口。

**手动开启流式朗读** — 收听优先播放当前正文版本已有的完整录音，切换音色不会触发重新合成；只有没有可播放语音时，才弹出确认框，确认后开始合成。若只有部分录音，先播放已有片段，继续合成需点“重试”并确认。正文生成期间也可确认开启朗读：补送已有正文后跟随新增文字，每段约 300–500 字，不提前生成下一章语音。播放器支持暂停、在已就绪的连续片段内拖动、独立停止合成和失败续试。切换阅读章节不打断播放；当前设备保存阅读与收听位置。对话模式同样复用已有录音，首次合成需确认。

**命名人物锚点与插图** — 在“人物锚点”上传并命名三张照片，在“故事与大纲”选择本书角色。AI 在完成章节后挑选精彩场景配图，调用设置里的生图模型并附上对应照片与名字。跨图人物一致性仍取决于模型的多参考图能力。新增数据保存在同一 SQLite 的 `theater_studio` 表中，首次访问自动建表；语音片段存放于现有剧场音频目录。更新代码后需要重启后端并刷新页面。


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

### 爱的印记（AI 礼物系统）
317. **AI 自主送礼** — 每次记忆总结完成后，AI 在生成私密日记的同一次模型调用中，综合判断是否需要给用户送一份礼物，同时也可低概率决定发布朋友圈，不再为送礼判断额外调用一次模型。Prompt 注入当前精确时间（年月日星期时分秒）+ 本次总结的所有记忆摘要 + 最近聊天上下文 + 世界书人设，结构化 JSON 同时返回 `diary`、`post_moment`、`moment`、`givegift` 和 `gift`；仅在 `givegift=true` 时继续生图并入库
318. **硅基流动 Kolors 生图** — AI 决定送礼后，使用 `image_prompt` 调用硅基流动 `Kwai-Kolors/Kolors` 模型（免费）生成 1024×1024 图片。Prompt 约束为 cute cartoon style、不生成真实人物。图片 URL 1 小时过期，后端立即下载保存到 `data/uploads/gift_{timestamp}.png`
319. **礼物弹窗动画（全页面）** — 礼物生成后通过 WebSocket 广播 `gift_pending` 事件。前端任何页面（聊天页 chat.html + 所有子页面 common.js）收到后弹出全屏礼物动画。打开聊天页/子页面时也会自动检查 `GET /api/gift/pending` 并弹窗
320. **礼物盒开启流程** — ① SVG 礼物盒从底部弹跳入场 → ② 用户点击打开 → 播放「打开礼物.mp3」音效 → 盒盖飞走 + 60 个彩色礼花粒子爆炸 → ③ 图片从中心缩放淡入 → ④ 点击图片 → AI 的配图文字滑出 → 「💝 收下礼物」按钮出现 → ⑤ 点击收下 → 整体缩小飞走动画 → POST 标记 received
321. **爱的印记陈列馆** — `/gift` 页面（`gift.html`），暗色画廊风格，3 列缩略图网格展示所有已收到的礼物（缩略图+日期），点击打开详情弹窗（大图+日期+文字+删除按钮），按时间倒序排列
322. **礼物数据** — 存储在 SQLite `gifts` 表（id, image_path, message, created_at, status, received_at），status 为 `pending`（未领取）或 `received`（已领取）。删除礼物时同步清理本地图片文件
323. **不过度送礼** — 日记 Prompt 中明确要求朋友圈是小概率事件、送礼是更低概率事件，大部分时候应返回 `post_moment=false` 和 `givegift=false`
324. **测试按钮** — 爱的印记页面右上角「🎁 测试送礼」按钮，取最近 5 条记忆 + 最近 20 条上下文触发完整送礼流程（AI 判断 + 生图 + 入库 + WebSocket 推送）
325. **主页入口** — home.html APPS 数组增加「爱的印记」（`/public/funIcon_0018_爱的印记.png`）

### 爱的印记工作流程
```
【触发时机（记忆总结完成后）】
  _do_digest() 完成记忆总结
  → 构建日记 Prompt（人设 + 当前时间 + 记忆摘要 + 上下文）
  → 同一次模型调用返回日记 + 可选朋友圈 + 可选礼物 JSON
  → gift.send_gift_from_decision() 执行模型已做出的送礼决定，不再调用模型
  → givegift = false？→ 结束
  → givegift = true？→ 提取 image_prompt + message

【生图 + 入库】
  → POST https://api.siliconflow.cn/v1/images/generations
    model: Kwai-Kolors/Kolors, 1024x1024
  → 下载图片 → 保存 data/uploads/gift_{ts}.png
  → INSERT INTO gifts (status='pending')
  → WebSocket 广播 gift_pending 事件

【前端弹窗（chat.html + common.js）】
  页面加载 → GET /api/gift/pending → 有未领取？弹窗
  WebSocket 收到 gift_pending → 弹窗
  → SVG 礼物盒弹入 → 点击打开 → 播放音效 + 礼花 → 图片展示
  → 点击图片 → 显示配图文字 → 「收下礼物」
  → POST /api/gift/{id}/receive → 飞走动画

【陈列馆（gift.html）】
  GET /api/gift/list → 3列缩略图网格
  → 点击缩略图 → 详情弹窗（大图+文字+日期）
  → 可删除 → DELETE /api/gift/{id}（同步清理图片文件）
```

### 奥罗斯财团（基金持仓监控）
326. **持仓管理** — SQLite `fund_holdings` 表存储基金持仓信息（代码、名称、份额、平均成本、总成本、跌幅/涨幅预警阈值），前端支持添加/编辑/删除，持仓列表可折叠收起
327. **数据拉取（不涉及 AI）** — 使用 akshare 拉取所有持仓基金的最新净值和涨跌幅（`fund_open_fund_daily_em` + `fund_open_fund_info_em` 兜底），新浪财经接口拉取上证指数实时涨跌。根据持仓计算当前市值、浮盈亏金额/百分比，检查是否触发预警阈值。拉取结果缓存到 `fund_cache.json`，刷新页面后仍可显示
328. **历史走势查询** — `fund_open_fund_info_em` 拉取最近 N 天净值走势，分析时注入近 30 日走势摘要（最低/最高/整体趋势）
329. **AI 分析** — 将持仓数据 + 历史走势 + 大盘背景 + 用户投资倾向拼成结构化 prompt，连同世界书人设 + 最近 20 条聊天上下文一起发送给当前聊天模型。AI 回复作为 assistant 消息插入聊天 + TTS 语音播报 + WebSocket 广播
330. **每日定时分析** — `FundScheduler` 后台线程每 30 秒检查，交易日 14:45 自动触发分析。使用 `chinese_calendar.is_workday()` 判断交易日（自动处理周末 + 中国法定节假日含调休）
331. **功能开关** — 顶部 toggle 开关，状态持久化在 `fund_config.json`，关闭后定时任务不触发、不影响其他功能
332. **投资倾向** — 可编辑的文本框，保存后在 AI 分析时注入 prompt（如"计划买入黄金，等合适坑位"），帮助 AI 给出更贴合的建议
333. **手动操作** — 「🔄 刷新数据」仅拉取数据不调 AI；「📊 立即分析」拉数据 + 调 AI + TTS，操作不阻塞页面，可自由切换到其他页面
334. **主页入口** — home.html APPS 数组增加「奥罗斯财团」（`/fund`）

### 奥罗斯财团工作流程
```
【手动刷新数据（不调 AI）】
  点击「🔄 刷新数据」→ POST /api/fund/fetch
  → akshare 拉取全量基金日数据 + 新浪财经拉取上证指数
  → 逐只匹配持仓基金，计算盈亏、检查预警
  → 缓存到 fund_cache.json → 返回前端渲染

【手动/定时 AI 分析】
  点击「📊 立即分析」→ POST /api/fund/analyze（或 14:45 定时触发）
  → 拉取持仓数据 + 30 日历史走势
  → 生成分析 prompt（持仓明细 + 走势摘要 + 大盘 + 投资倾向）
  → 组装消息（世界书人设 + 20 条上下文 + prompt）
  → stream_ai() 流式调用当前模型
  → 插入系统消息「💰 奥罗斯财团 — 基金持仓分析」+ AI 回复
  → TTS 语音播报 + WebSocket 广播到聊天页

【定时任务（FundScheduler）】
  后台线程每 30 秒检查 → 14:45 + 交易日 + 开关开启 → 触发 run_fund_analysis()
  → 等待到 14:46 避免重复触发
```

### 动态壁纸（独立显示器全屏壁纸 + AI 气泡）
500. **全屏壁纸轮播** — 独立页面 `/wallpaper`，用于副屏全屏展示。支持图片和视频轮播，可配置轮换间隔（秒），视频播放完毕后自动切换下一个。全屏铺满（`object-fit: cover`），无黑边
501. **淡入淡出过渡** — 双缓冲架构（两个媒体层交替），切换时新层在旧层上方 1.5 秒淡入，旧层在过渡完成后移除，确保无黑屏闪烁
502. **资源就绪检测** — 图片通过轮询 `img.complete` + `naturalWidth`、视频通过轮询 `readyState >= 2` 确认资源就绪后再激活淡入，避免黑屏。保底超时（图片 2 秒 / 视频 3 秒）确保不会永远卡住
503. **键盘操控** — `←` / `→` 方向键手动切换上/下一张壁纸，`F` 键切换全屏，`ESC` 退出全屏或锚点编辑模式。600ms 冷却保护防止快速按键导致黑屏
504. **AI 聊天气泡** — 通过 WebSocket 接收 `msg_created` 事件，筛选 `role=assistant` 的消息，以半透明毛玻璃气泡显示在画面上。自动清洗 `[MUSIC:...]`、`[ALARM:...]`、`[REMINDER:...]`、`[Monitor:...]`、`[TOY:...]` 等指令标记和 Markdown 格式符号。气泡按 `\n\n` 自动拆分为多条，淡入显示，可配置自动消失时长（默认 12 秒）
505. **气泡锚点系统** — 每张壁纸可独立设置气泡显示位置（百分比坐标），适应不同图片的人物位置。设置面板点击「编辑当前锚点」进入编辑模式，点击画面设定锚点位置，ESC 或右键退出
506. **设置面板** — 鼠标移到屏幕底部边缘或双击打开设置面板，可配置：轮换间隔、气泡显示时长、启用/禁用单个壁纸文件、上传新壁纸、编辑气泡锚点。点击缩略图切换启用/禁用，右键缩略图跳转到该壁纸
507. **Chrome App 模式启动** — `启动壁纸.bat` 使用 `chrome.exe --app=... --start-fullscreen` 启动，无地址栏/标签栏/边框，等同于独立桌面程序。关闭壁纸窗口不影响服务器和聊天
508. **配置持久化** — 壁纸配置（轮换间隔、文件启用状态、气泡锚点坐标）存储在 `data/wallpaper_config.json`
509. **完全独立** — 壁纸页面独立运行，与聊天系统仅通过 WebSocket 单向接收消息，开关壁纸不影响任何其他功能

### 动态壁纸工作流程
```
【启动壁纸】
  双击 启动壁纸.bat → Chrome --app 模式全屏打开 /wallpaper
  → 加载 wallpaper_config.json + 扫描 public/wallpaper/ 目录
  → 构建播放列表（排除 enabled=false 的文件）
  → 显示第一张壁纸 → 启动轮换定时器 → 连接 WebSocket

【壁纸轮播】
  定时器到期（图片）/ 视频播放结束 → 切换下一个：
    ├ 新层加载资源 → 轮询就绪状态（img.complete / video.readyState ≥ 2）
    ├ 就绪后激活淡入（opacity 0→1，1.5 秒 CSS transition）
    ├ 旧层 1.6 秒后隐藏并清理 DOM
    └ 更新气泡锚点位置

【AI 气泡显示】
  WebSocket 收到 {type: "msg_created", data: {role: "assistant", content: "..."}}
  → cleanAIText() 清洗指令标记和 Markdown 格式
  → 按 \n\n 拆分为多条 → 逐条创建气泡 DOM（最多 5 条）
  → 气泡淡入显示 → N 秒后自动淡出消失

【锚点编辑】
  设置面板 → 编辑当前锚点 → 进入编辑模式（crosshair 光标）
  → 点击画面任意位置 → 保存百分比坐标到 config.files[name].bubble_anchor
  → 实时更新气泡容器位置 → ESC/右键退出编辑模式
```

### 钱包与转账（[转账：N元]）
510. **转账功能** — 聊天输入栏「+」菜单中的「💰 转账」入口，弹出转账对话框（含金额输入），确认后向输入框插入 `[转账：N元]` 标签随消息发送
511. **双向转账** — 支持正数（转账给对方）和负数（从钱包扣除），AI 也可在回复中使用 `[转账：N元]` 发起转账
512. **微信风格转账卡片** — 消息中的 `[转账：N元]` 渲染为圆角卡片：正数为橙色（#f89b40）+ 双箭头图标 + 「发起了一笔转账」，负数为绿色（#4caf50）+ X 图标 + 「钱包扣除」
513. **卡片独立显示** — 包含转账卡片的气泡自动去除背景/边框/阴影（CSS `:has(.transfer-card)` 规则），卡片视觉上独立于聊天气泡，像功能组件而非文字消息
514. **钱包面板** — 侧栏底部「💰 钱包」按钮打开浮层面板，显示当前余额和交易记录列表（每笔显示方向、金额、时间、参与双方名称）
515. **余额实时同步** — 转账操作后通过 WebSocket 广播 `wallet_update` 事件，钱包面板实时刷新；余额注入 AI prompt 的 `[系统能力]` 区块，AI 可感知当前余额
516. **数据存储** — 复用 `bookkeeping` 表，`record_type` 为 `wallet_user`（用户发起）/ `wallet_ai`（AI 发起），存储金额、描述、参与方名称
517. **TTS 过滤** — `[转账：N元]` 在 TTS 合成时自动剥除，不被语音朗读
518. **聊天室＋展开菜单** — 聊天室输入栏新增＋按钮，点击展开三项操作：上传图片、拍照、语音消息。点击菜单外区域自动收起
519. **聊天室拍照** — ＋菜单中「拍照」打开全屏相机遮罩，支持前后摄切换。浏览器端使用 getUserMedia，Android 端使用 AionCamera 原生桥（通过 `_getNativeBridge()` 从 iframe 穿透到顶层 WebView 获取）。拍照后自动上传并插入消息
520. **聊天室语音消息** — ＋菜单中「语音消息」切换为按住说话模式。长按录音，上滑取消，松手发送。浏览器端使用 MediaRecorder 录制 WebM，Android 端优先使用 AionAudio 原生桥录制（通过 iframe 穿透 + `window.top` 回调注册解决 WebView iframe 环境限制）。录音上传后通过硅基流动 ASR 自动转写，消息以橙色语音气泡展示（播放按钮 + 波形动画 + 时长），转写文字以小字显示在气泡下方。音频文件 URL 同时传递给 AI 模型用于理解语音内容
521. **聊天室音频上传** — 聊天室上传接口 `/api/chatroom/upload` 新增音频 MIME 类型支持（webm/wav/mp4/mpeg/ogg），后端 `_process_voice_attachments()` 将语音附件的 ASR 转写注入消息 content、保留音频 URL 供模型访问
522. **Connor 独立钱包** — Connor 拥有自己的钱包系统，数据与 AIon 钱包完全分离。使用 `connor_wallet_user`/`connor_wallet_ai` 两种 record_type 存储在 bookkeeping 表中
523. **Connor 钱包 API** — 新增 `/api/connor-wallet/balance`（余额查询）、`/api/connor-wallet/transactions`（记录列表）、`/api/connor-wallet/transfer`（转账入账）三个独立端点
524. **聊天室钱包面板** — 聊天室侧栏底部新增「💰 钱包」按钮，打开 Connor 专属钱包浮层面板（蓝色渐变主题区分于 AIon 橙色），显示余额和转账记录
525. **聊天室转账功能** — 聊天室＋菜单新增「💰 转账」入口，弹出转账弹窗，确认后插入 `[转账：N元]` 标签到输入框
526. **聊天室转账入账** — 用户消息中的 `[转账：N元]` 自动检测并记入 Connor 钱包（`connor_wallet_user`）；Connor AI 回复中的 `[转账：N元]` 记为 Connor 支出（`connor_wallet_ai`）
527. **聊天室转账卡片** — 聊天室消息中的 `[转账：N元]` 渲染为微信风格转账卡片（正数橙色转账卡、负数绿色扣除卡），卡片独占气泡且去除背景
528. **Connor 钱包余额感知** — `build_ability_block` 根据 `who` 参数区分角色，Connor 读取 `_get_connor_balance()`、AIon 读取 `_get_balance()`，各自 prompt 中注入自己的余额
529. **Connor 钱包实时刷新** — 转账操作后通过 WebSocket 广播 `connor_wallet_update` 事件，钱包面板开启时自动刷新余额和记录

### 斗地主牌桌（/doudizhu）
530. **独立牌桌入口** — `home.html` 主页新增「斗地主」入口，路由 `/doudizhu`，相关后端集中在 `routes/doudizhu.py`，前端集中在 `static/doudizhu.html/css/js`，尽量不侵入主聊天逻辑
531. **真实三人牌局状态** — 服务端负责洗牌、随机发牌、底牌、叫地主、出牌校验、手牌扣除、回合推进和结算，状态持久化到 `data/doudizhu_state.json`
532. **AI 私有手牌隔离** — Aion / Connor 回合调用现有群聊上下文构建逻辑（人设、私聊/群聊历史等保持一致），但只给当前 AI 注入自己的手牌和公共牌局信息，不泄露其他玩家手牌
533. **JSON 决策与台词** — 叫地主仍由 AI 返回 JSON 决策；出牌由本地策略确定，再沿用群聊人设和上下文生成与动作一致的 `speech`，接入原有语音。模型返回的其他动作不会覆盖本地出牌，模型失败时仍执行该动作并使用备用台词
534. **出牌策略增强** — 候选覆盖飞机带翅膀、四带二和不同带牌组合，按剩牌最少组合手数评分并保留炸弹、大牌；对手报单/报双时封堵，农民让牌或按轮次接应。算牌只使用自己的牌、公开弃牌和各家剩余张数，不读取其他玩家的牌面
535. **牌桌体验** — 支持新局先看牌确认、重新发牌、随机 Aion/Connor 座次、洗牌延迟、出牌/炸弹/胜负/轮到你/换人音效、AI `speech` 单独接入现有 TTS
536. **移动端牌桌布局** — 手机端做独立布局：AI 座位上移、底牌缩小贴近用户手牌区、用户手牌压叠排列、已出牌暗色弃牌堆、结算弹窗
537. **群聊战报** — 结算窗提供「继续玩」和「昭告天下」；昭告天下会把本局赢家、地主、剩余手牌、钱包结算等写入最新群聊，并触发群聊 AI 后续回应
538. **钱包结算** — 斗地主按阵营结算虚拟货币：地主赢则两个农民按剩牌扣款给地主；农民赢则地主按剩牌扣款并均分给两个农民。全场每成功打出一次炸弹或王炸，整局金额乘 2（一次 2 倍、两次 4 倍，依次累乘；四带二不计），新局恢复 1 倍。牌桌、结算弹窗和群聊战报展示倍数。用户金额只展示，不写入钱包；Aion/Connor 金额写入各自 `bookkeeping` 钱包记录

## AI 好友 / 串门

普通聊天只会向当前 AI 提供“已启用”且勾选了“允许自主串门”的好友。若当前 AI 没有任何符合条件的好友，好友资料、会客室说明和串门指令都不会进入聊天上下文；未勾选的好友仍可在「AI 好友 / 串门」页面由用户手动发起拜访。聊天或闲时自主串门真正开始后，聊天窗口会暂时显示一条小型出发提示；报告卡片成功返回后提示自动删除，若报告无法保存则提示会改为中断说明。

本地 AI 可以通过对方会客室的 MCP 服务进行短暂的纯文字拜访。使用前双方交换会客室地址和专用凭据：

```text
会客室 MCP 地址：https://你的域名/mcp
Visitor Key：每位来访 AI 单独一把
```

每位本地 AI 必须使用自己的 Visitor Key；首次认领后的来访身份固定，只能由对方会客室管理员修改。Key 只能填在本机「AI 好友 / 串门」管理页，绝不能粘贴到聊天、主题或提示词中。单条消息仅限纯文字，最多 500 个字符。

管理页可先「测试连接」：它只读取接待者、开放状态、身份状态和输入上限，不会调用或请求模型，也不消耗模型或聊天额度；不会开始聊天或写入访问记录。已启用的好友可由用户立即指定访问；开启「允许自主串门」后，空闲自主行动也只能从当前本地 AI 名下、已过冷却时间的好友中选择。单次访问受 1–8 回合上限约束，历史按本地 AI 分开查看，且不会显示 Key 或 Authorization 信息。

完整的登记、测试、访问与排查说明见 [AI 好友串门使用说明](../../../docs/AI好友串门使用说明.md)。
