@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   清理个人数据（打包给朋友前使用）
echo ========================================
echo.
echo   将删除以下内容:
echo     - 聊天数据库 (chat.db，含聊天/记忆/朋友圈/日记本/礼物/钱包等)
echo     - 导出的聊天记录 (chats/)
echo     - 监控日志 (monitor_logs/)
echo     - 摄像头截图 (screenshots/)
echo     - 上传的图片/视频 (uploads/)
echo     - 活动日志 (activity_logs/)
echo     - TTS 语音缓存 (tts_cache/)
echo     - 英语角语音缓存 (english_corner_audio/)
echo     - 临时文件 (tmp/)
echo     - 聊天状态 + 记忆锚点
echo     - 日记本数据 (存放在 chat.db)
echo     - 定位配置 + 定位状态（含家坐标/高德Key）
echo     - 世界书人设 (重置为空白)
echo     - 小剧场角色预设 (theater_personas.json)
echo     - 奥罗斯幽林游戏数据 (ghost_forest/)
echo     - 阅读书籍数据 (books/)
echo     - API Key (Gemini / 硅基流动 / Tavily 等，需要朋友自己填)
echo     - 虚拟环境 (朋友需重新安装)
echo     - 源码中硬编码的 API Key (重置为空)
echo     - Android App 中的局域网/Tailscale IP 与 Cloudflare 域名
echo     - Android 构建产物（可能内嵌旧地址）
echo     - AionsHome 服务日志 + Android 本机 SDK 路径
echo     - 火山引擎 TTS 配置 + 输出
echo     - 个人笔记/备份文件
echo     - .vscode 配置
echo     - 聊天室配置 + 聊天室图片
echo     - 基金配置 + 基金缓存
echo     - MCP 服务配置
echo     - Home Assistant 配置 + 令牌 + 设备别名
echo     - 壁纸配置
echo     - SSL 证书
echo     - Gemini CLI 调试日志 (cli_debug/)
echo     - Connor-Codex 聊天记录 + 人设 + 上传图片 + 日志
echo     - 会客室访客数据库、聊天/记忆、Visitor Key、OAuth 数据与日志
echo     - AI 好友地址、出站 Visitor Key 与会客上下文桥凭据
echo     - 会客室旧隔离备份、运行缓存和独立虚拟环境
echo.
echo   !! 请确认这是【复制出来的副本】!!
echo   !! 不要在你自己的原始文件夹里运行 !!
echo.
set /p CONFIRM=确认清理? 输入 Y 继续: 
if /i not "%CONFIRM%"=="Y" (
    echo 已取消。
    pause
    exit /b 0
)

echo.
echo 正在清理...

:: ── aion-chat/data/ ──
if exist "aion-chat\data\chat.db" del /q "aion-chat\data\chat.db"
if exist "aion-chat\data\chat_status.json" del /q "aion-chat\data\chat_status.json"
if exist "aion-chat\data\digest_anchor.json" del /q "aion-chat\data\digest_anchor.json"
if exist "aion-chat\data\cam_config.json" del /q "aion-chat\data\cam_config.json"
if exist "aion-chat\data\location_status.json" del /q "aion-chat\data\location_status.json"

if exist "aion-chat\data\chats" rmdir /s /q "aion-chat\data\chats"
mkdir "aion-chat\data\chats"
type nul > "aion-chat\data\chats\.gitkeep"

if exist "aion-chat\data\monitor_logs" rmdir /s /q "aion-chat\data\monitor_logs"
mkdir "aion-chat\data\monitor_logs"

if exist "aion-chat\data\screenshots" rmdir /s /q "aion-chat\data\screenshots"
mkdir "aion-chat\data\screenshots"

if exist "aion-chat\data\uploads" rmdir /s /q "aion-chat\data\uploads"
mkdir "aion-chat\data\uploads"
type nul > "aion-chat\data\uploads\.gitkeep"

if exist "aion-chat\data\activity_logs" rmdir /s /q "aion-chat\data\activity_logs"
mkdir "aion-chat\data\activity_logs"
type nul > "aion-chat\data\activity_logs\.gitkeep"

if exist "aion-chat\data\tts_cache" rmdir /s /q "aion-chat\data\tts_cache"
mkdir "aion-chat\data\tts_cache"
type nul > "aion-chat\data\tts_cache\.gitkeep"

if exist "aion-chat\data\english_corner_audio" rmdir /s /q "aion-chat\data\english_corner_audio"
mkdir "aion-chat\data\english_corner_audio"

if exist "aion-chat\data\tmp" rmdir /s /q "aion-chat\data\tmp"
mkdir "aion-chat\data\tmp"

:: ── 清理奥罗斯幽林游戏数据 ──
if exist "aion-chat\data\ghost_forest" rmdir /s /q "aion-chat\data\ghost_forest"
mkdir "aion-chat\data\ghost_forest"

:: ── 清理阅读书籍数据 ──
if exist "aion-chat\data\books" rmdir /s /q "aion-chat\data\books"
mkdir "aion-chat\data\books"

:: ── 清理小剧场角色预设 ──
if exist "aion-chat\data\theater_personas.json" del /q "aion-chat\data\theater_personas.json"

:: ── 清理聊天室配置 + 图片 ──
if exist "aion-chat\data\chatroom_config.json" del /q "aion-chat\data\chatroom_config.json"
if exist "aion-chat\data\chatroom_images" rmdir /s /q "aion-chat\data\chatroom_images"

:: ── 清理基金配置 + 缓存 ──
if exist "aion-chat\data\fund_config.json" del /q "aion-chat\data\fund_config.json"
if exist "aion-chat\data\fund_cache.json" del /q "aion-chat\data\fund_cache.json"

:: ── 清理 MCP 服务配置 ──
if exist "aion-chat\data\mcp_servers.json" del /q "aion-chat\data\mcp_servers.json"

:: ── 清理 Home Assistant / 智能家居配置 ──
if exist "aion-chat\data\home_assistant_mcp.json" del /q "aion-chat\data\home_assistant_mcp.json"
if exist "aion-chat\data\home_assistant_aliases.json" del /q "aion-chat\data\home_assistant_aliases.json"
if exist "aion-chat\data\homeassistant-config" rmdir /s /q "aion-chat\data\homeassistant-config"

:: ── 清理 Gemini CLI 调试日志 ──
if exist "aion-chat\data\cli_debug" rmdir /s /q "aion-chat\data\cli_debug"

:: ── 清理聊天室图片缓存 ──
if exist "aion-chat\data\chatroom_images" rmdir /s /q "aion-chat\data\chatroom_images"

:: ── 清理壁纸配置 ──
if exist "aion-chat\data\wallpaper_config.json" del /q "aion-chat\data\wallpaper_config.json"

:: ── 清理 SSL 证书 ──
if exist "aion-chat\data\cert.pem" del /q "aion-chat\data\cert.pem"
if exist "aion-chat\data\key.pem" del /q "aion-chat\data\key.pem"

:: ── 清理服务运行日志（可能包含访问端 IP）──
del /q "aion-chat\data\*.log" 2>nul
del /q "aion-chat\*.log" 2>nul

:: ── 清理 aion.db 旧数据库 ──
if exist "aion-chat\data\aion.db" del /q "aion-chat\data\aion.db"

:: ── AI 好友串门私密数据 ──
:: lounge_friends.json 包含好友地址和原始 Visitor Key；上下文桥 Key 也不得打包。
if exist "aion-chat\data\lounge_friends.json" del /q "aion-chat\data\lounge_friends.json"
del /q "aion-chat\data\.lounge_friends.json.*.tmp" 2>nul
if exist "aion-chat\data\lounge-context-bridge.key" del /q "aion-chat\data\lounge-context-bridge.key"

:: ── Visitor Lounge 私密数据与本机运行文件 ──
:: 保留程序、配置和头像，只清理密钥、访客资料、聊天记录、日志、备份与缓存。
if exist "AionsHome-Visitor-Lounge\.env" del /q "AionsHome-Visitor-Lounge\.env"

if exist "AionsHome-Visitor-Lounge\data" rmdir /s /q "AionsHome-Visitor-Lounge\data"
mkdir "AionsHome-Visitor-Lounge\data"

if exist "AionsHome-Visitor-Lounge\logs" rmdir /s /q "AionsHome-Visitor-Lounge\logs"
mkdir "AionsHome-Visitor-Lounge\logs"

if exist "AionsHome-Visitor-Lounge\.runtime" rmdir /s /q "AionsHome-Visitor-Lounge\.runtime"
if exist "AionsHome-Visitor-Lounge\.rollback-quarantine" rmdir /s /q "AionsHome-Visitor-Lounge\.rollback-quarantine"
if exist "AionsHome-Visitor-Lounge\.codex-home" rmdir /s /q "AionsHome-Visitor-Lounge\.codex-home"
if exist "AionsHome-Visitor-Lounge\.pytest_cache" rmdir /s /q "AionsHome-Visitor-Lounge\.pytest_cache"
if exist "AionsHome-Visitor-Lounge\.venv" rmdir /s /q "AionsHome-Visitor-Lounge\.venv"

:: ── Connor-Codex 个人数据 ──
if exist "Connor-Codex\messages.jsonl" del /q "Connor-Codex\messages.jsonl"
if exist "Connor-Codex\persona.md" (
    echo. > "Connor-Codex\persona.md"
)
if exist "Connor-Codex\auto-responder-state.json" del /q "Connor-Codex\auto-responder-state.json"
if exist "Connor-Codex\uploads" rmdir /s /q "Connor-Codex\uploads"
mkdir "Connor-Codex\uploads"
if exist "Connor-Codex\node_modules" rmdir /s /q "Connor-Codex\node_modules"
if exist "Connor-Codex\package-lock.json" del /q "Connor-Codex\package-lock.json"
del /q "Connor-Codex\*.log" 2>nul

:: 重置 settings.json（清空所有 API Key，包含 Tavily 联网搜索 Key）
echo {} > "aion-chat\data\settings.json"

:: 重置世界书人设
echo {"ai_persona": "", "user_persona": "", "ai_name": "AI", "user_name": ""} > "aion-chat\data\worldbook.json"

:: 重置定位配置（清空高德Key和家坐标）
echo {"amap_key": "", "home_lng": 0, "home_lat": 0, "home_threshold": 500, "heartbeat_outdoor_min": 5, "heartbeat_home_min": 30, "poi_types": {"餐饮美食": "050000", "风景名胜": "110000", "休闲娱乐": "100000", "购物": "060000"}, "poi_radius": 2000, "enabled": false, "quiet_hours_enabled": true, "quiet_hours_start": "00:00", "quiet_hours_end": "10:00", "movement_threshold": 500} > "aion-chat\data\location_config.json"

:: ── 删除个人笔记/备份文件 ──
if exist "自己看的存档.txt" del /q "自己看的存档.txt"
if exist "MW_RAG_Backup_2026-04-04.json" del /q "MW_RAG_Backup_2026-04-04.json"
if exist "import_mw_rag.py" del /q "import_mw_rag.py"
if exist "fix_schedules.py" del /q "fix_schedules.py"

:: ── 删除 Active 独立监控截图 ──
if exist "Active\screenshots" rmdir /s /q "Active\screenshots"
mkdir "Active\screenshots"

:: ── 删除 .vscode 配置 ──
if exist ".vscode" rmdir /s /q ".vscode"

:: ── 清理 Android App 网络地址 ──
:: 自动读取当前源码中的地址，再替换为 RFC/示例用途地址；脚本自身不保存个人地址。
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\sanitize-network-endpoints.ps1" -Root "%CD%"
if errorlevel 1 (
    echo.
    echo [错误] 网络地址清理或复查失败，请根据上方提示处理后再提交。
    pause
    exit /b 1
)

:: 编译产物会内嵌清理前的地址，不应进入开源副本。
if exist "AionApp\local.properties" del /q "AionApp\local.properties"
if exist "AionApp\app\build" rmdir /s /q "AionApp\app\build"
if exist "AionApp\build" rmdir /s /q "AionApp\build"
if exist "AionApp\.gradle" rmdir /s /q "AionApp\.gradle"
if exist "AionApp\.idea" rmdir /s /q "AionApp\.idea"

:: ── 删除虚拟环境 ──
if exist ".venv" rmdir /s /q ".venv"

echo.
echo ========================================
echo   清理完成!
echo   朋友拿到后按顺序操作:
echo   1. 双击「一键安装环境.bat」
echo   2. 双击「一键启动.bat」
echo   3. 浏览器打开 localhost:8080
echo   4. 设置里填 API Key
echo ========================================
echo.
pause
