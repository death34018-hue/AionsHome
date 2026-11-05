# HTTP 接口、SSE 与 WebSocket 事件（历史档案）

本文保留当时的实现、示例和排错记录。当前用法见[功能说明索引](../../README.md)，其他旧记录见[历史档案目录](README.md)。

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
| `/api/settings` | GET/POST | 读取/保存设置（API Key、Tavily Key、模型配置、能力开关等） |
| `/api/worldbook` | GET/POST | 读取/保存世界书 |
| `/api/chat_status` | GET | 获取当前聊天状态摘要 |
| `/api/models` | GET | 可用模型列表 |
| `/api/settings/song-gen` | GET/PUT | 读取/更新 AI 生成歌曲开关（`{enabled}`），控制 SONG 能力是否注入 prompt |

### 记忆库
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/memories` | GET | 获取所有记忆（按时间倒序） |
| `/api/memories` | POST | 手动添加记忆（自动向量化） |
| `/api/memories/{id}` | PUT | 编辑记忆（重新向量化，支持 unresolved 字段） |
| `/api/memories/{id}` | DELETE | 删除记忆 |
| `/api/memories/{id}/unresolved` | PATCH | 切换记忆的 unresolved 状态 |

### 日记本
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/diaries` | GET | 分页获取日记，支持 `?author=user/aion/connor` 筛选 |
| `/api/diaries` | POST | 用户手写新增日记（标题/心情可选，正文必填） |
| `/api/diaries/{id}` | PUT | 编辑日记标题、心情和正文 |
| `/api/diaries/{id}` | DELETE | 删除日记 |

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
| `/api/activity/status` | GET | PC 采集线程状态诊断（窗口采集线程、显示器状态、物理显示器探测、键鼠空闲时间等） |
| `/api/phone-screen/upload` | POST | Android MediaProjection 上传手机屏幕截图（base64 JPEG），保存到 `data/phone_screens/` + `uploads/` |
| `/api/phone-screen/skip` | POST | Android 手机屏幕截图跳过原因上报（如 `no_projection` / `locked` / `no_frame`），用于诊断 |
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
| `web_search` | 联网搜索/网页读取触发状态：含 msg_id、kind、query/url，前端可显示搜索状态 |
| `poi_search` | POI 搜索触发：含 msg_id + categories，前端显示蓝色搜索指示器 |
| `toy_command` | 玩具控制指令：含 commands 数组 + msg_id |
| `image_gen_start` | AI 生图开始：含 msg_id + prompt + is_selfie，前端显示橙色生图指示器 |
| `song_gen_start` | AI 生成歌曲开始：含 conv_id + msg_id，前端显示「歌曲谱写中....」 |
| `song_gen_done` | AI 生成歌曲完成：前端移除等待提示 |
| `song_gen_failed` | AI 生成歌曲失败：前端移除等待提示 |
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
| `web_search` | 联网搜索/网页读取触发广播（SSE + WS 双通道） |
| `debug` | Debug 数据广播（SSE + WS 双通道，语音发送时也能收到） |
| `monitor_alert` | 定时监控触发，前端播放提示音，手机端弹高优先级通知 |
| `chatroom_ai_status` | 聊天室异步后续动作状态，如 [CAM_CHECK] 获取画面、非视觉模型等待哨兵识图 |
| `phone_screen_uploaded` | Android 手机屏幕截图已上传，供诊断/前端实时感知 |
| `phone_screen_skipped` | Android 手机屏幕截图跳过，携带 skip_reason/app/locked |
| `schedule_alarm` | 闹铃到期触发，前端弹出确认弹窗，手机端弹高优先级通知 |
| `schedule_changed` | 日程列表变动，前端刷新面板 |
| `toy_command` | 玩具控制指令广播（含 commands 数组，SSE + WS 双通道） |
| `location_update` | 定位状态更新广播（地址、天气、状态变更等） |
| `poi_search` | POI 搜索触发广播（SSE + WS 双通道，前端显示搜索指示器） |
| `activity_log` | 新设备活动日志推送（含 device/app/title/time，前端实时追加） |
| `tts_chunk` | TTS 音频分片推送（含 msg_id/seq/url/created_at/target_client_id），目标前端收到后按播放资格加入播放队列 |
| `tts_done` | TTS 合成完毕通知（含 msg_id/created_at/target_client_id），目标前端标记该消息队列已结束，播完最后一片后清理 |
| `tts_state` | 客户端→服务端：TTS 开关/音色/播放资格同步（`{enabled, voice, can_play, active_at}`），服务端据此判断是否需要合成和哪端负责播放 |
| `image_gen_start` | AI 生图开始广播（SSE + WS 双通道） |
| `image_gen_done` | AI 生图完成广播（含 conv_id），前端移除指示器 |
| `image_gen_failed` | AI 生图失败广播（含 conv_id），前端移除指示器 |
| `song_gen_start` | 私聊 AI 生成歌曲开始广播，前端显示「歌曲谱写中....」 |
| `song_gen_done` | 私聊 AI 生成歌曲完成广播，前端移除等待提示 |
| `song_gen_failed` | 私聊 AI 生成歌曲失败广播，前端移除等待提示 |
| `chatroom_song_gen_start` | 聊天室 AI 生成歌曲开始广播，前端显示「歌曲谱写中....」 |
| `chatroom_song_gen_done` | 聊天室 AI 生成歌曲完成广播，前端移除等待提示 |
| `chatroom_song_gen_failed` | 聊天室 AI 生成歌曲失败广播，前端移除等待提示 |

### 消息角色说明
| 角色 | 说明 | 是否显示在聊天 |
|------|------|---------------|
| `user` | 用户消息 | ✅ |
| `assistant` | AI 回复（含 Core 唤醒/主动查看监控的回复） | ✅ |
| `cam_user` | Sentinel 截图查询（内部） | ❌ 隐藏 |
| `cam_log` | Sentinel 分析结果（内部） | ❌ 隐藏 |
| `cam_trigger` | Core 唤醒时的系统提示（内部） | ❌ 隐藏 |
