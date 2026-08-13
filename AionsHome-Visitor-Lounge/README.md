# AionsHome Visitor Lounge

AionsHome Visitor Lounge（访客会客室）是一个小型、私密、本机运行的娱乐服务。它与原 AionsHome 使用不同的 Python 包、虚拟环境、数据库、进程、端口、日志和 Codex 工作目录；模型层只读复用 AionsHome 已有的项目内 Codex、聊天认证与精简调用配置，停止脚本也只会终止通过路径和命令行身份校验的会客室进程。

这是经过轻量公网收尾的娱乐服务，不是企业级安全平台。管理员可以查看访客聊天记录、摘要、模型用量和审计记录；把 Key 发给访客前，应明确告知对话会被记录。管理端没有网络登录层，只允许从本机回环地址访问。

## 前置条件

- Windows PowerShell 5.1 或更高版本。
- Python 3.11 或更高版本。
- AionsHome 项目内的 `Connor-Codex` 已安装且原有 Codex 聊天认证可用；会客室不安装全局 CLI，也不创建第二套登录。

以下命令均在本目录 `AionsHome-Visitor-Lounge` 中运行。

## 本地首次启动

1. 创建独立虚拟环境，并显式使用其中的 Python 安装项目和开发/验收依赖：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   ```

   当前运行时仅支持从项目根目录进行 editable 安装和运行；不支持 wheel 部署。请保留项目目录结构，并从本目录调用 `scripts` 中的运维脚本。

2. 复制环境模板，并为三个密钥生成互不相同的新值：

   ```powershell
   Copy-Item .env.example .env
   .\.venv\Scripts\python.exe -c "import secrets; from cryptography.fernet import Fernet; print('VISITOR_LOUNGE_KEY_PEPPER=' + secrets.token_urlsafe(32)); print('VISITOR_LOUNGE_MASTER_KEY=' + Fernet.generate_key().decode()); print('VISITOR_LOUNGE_SESSION_SECRET=' + secrets.token_urlsafe(32))"
   ```

   把输出的三个值分别填入 `.env`。不要复用 AionsHome、其他服务或全局配置中的密钥。在创建任何真实邀请之前完成密钥生成和最终轮换，并把 `.env` 当作敏感文件保存。

3. 确认 AionsHome 原有 Codex 聊天线路可用。会客室会自动定位项目内 `Connor-Codex`，复用原有认证和精简调用配置，不需要再次登录。

4. 启动两个独立服务：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/start.ps1
   ```

5. 打开管理端 <http://127.0.0.1:8002/admin> 创建邀请 Key，再用隐私窗口打开访客端 <http://127.0.0.1:8001>。

不要把 8001 或 8002 配置为路由器端口转发。公网访客入口只通过现有 Cloudflare Tunnel 到达回环地址上的 8001；8002 管理后台始终只允许本机访问。

## 配置

`.env` 存放三项本机敏感配置：

- `VISITOR_LOUNGE_KEY_PEPPER`：邀请 Key 摘要用 pepper。运行后更换会让已有 Key 无法登录。
- `VISITOR_LOUNGE_MASTER_KEY`：必须是 Fernet key，用于加密可揭示的邀请 Key。运行后更换会让已有 Key 无法再揭示；应改为轮换邀请 Key，而不是直接替换此值。
- `VISITOR_LOUNGE_SESSION_SECRET`：浏览器会话签名密钥。更换后已有会话失效。

`config/visitor-lounge.toml` 存放本机配置：

- 访客端固定为 `127.0.0.1:8001`，管理端固定为 `127.0.0.1:8002`。不要把任一服务改成局域网监听；公网入口由 Cloudflare Tunnel 单独负责。
- `admin.timezone` 控制后台按日统计的时区。
- `queue.max_generations` 默认 `1`，代码硬上限为 `2`；`queue.max_waiting` 默认且硬上限为 `3`。
- `identity.host_display_name`、`reserved_visitor_names` 和 `recording_disclosure` 控制展示名称、禁用访客名及对话记录告知文本，可按本机用途调整。

`config/persona.md` 是会客室专用 persona。不要在其中放密码、令牌或原 AionsHome 的私密上下文。

### 后台接待设置

打开 `http://127.0.0.1:8002/admin/settings` 可以编辑接待人全局人设、首次/再次欢迎语、固定提示模板、每位访客每 12 小时聊天额度和会客室总开关。欢迎语可使用 `{访客名字}` 和 `{接待人名字}`；设置保存在会客室自己的 SQLite 数据库中，保存后立即生效，不需要重启。

每位访客同时只有一个有效 Key；重新创建或轮换会立即使旧 Key 失效。首次认领与离开 30 分钟后的再次欢迎不会调用 Codex，也不占 12 小时聊天额度。超长、额度耗尽和会客室暂停等固定模板同样不占额度；只有产生可见 AI 回复的真实 Codex 调用才占一条。

会客室采用轻量、娱乐用途的访客边界：普通敏感话题、安全教育、朋友式安慰和首次关系越界会被正常回答或礼貌提醒；明确攻击、恶意代码、凭据套取、隐私窃取、身份冒充、持续纠缠或恶意辱骂会触发 24 小时安全冷静期，期满自动恢复，管理员也可提前解锁。访客昵称始终只是访客昵称，即使与主人同名也不会获得主人的身份、关系或权限。疑似真实密码、私钥或访问令牌会在落库和调用模型前被拒绝，不消耗额度；审计只记录凭据类别，不保存被拒绝的原文。这是轻量防护，不代表企业级或高安全等级的审核系统。

访客页现已通过现有 AionsHome Cloudflare Tunnel 发布到 <https://visitor.aionshome.com>，公网入口仍必须先通过会客室邀请 Key。访客服务只监听 `127.0.0.1:8001`，管理后台只监听 `127.0.0.1:8002`，后台没有公网路由。

Windows 下可直接双击项目根目录中的两个快捷入口：

- `START-Visitor-Lounge.cmd`：启动访客页、管理后台和会客室 supervisor；成功后窗口自动关闭。
- `STOP-Visitor-Lounge.cmd`：只停止并清理经过身份校验的会客室进程；不会停止共享的 Cloudflared Windows 服务，也不会影响原 AionsHome。

Cloudflared 由原 AionsHome 的自动启动 Windows 服务统一负责，不要为 Visitor Lounge 再运行第二份。

## Remote MCP

受邀的人类或外部 AI 可以使用同一把 Visitor Key 从标准 MCP 客户端访问会客室：

```text
URL: https://visitor.aionshome.com/mcp
Authentication: Authorization: Bearer <Visitor Key>
Transport: Streamable HTTP
Content: 只接受纯文本；单次输入最多 500 个 Unicode 字符，回复最多 800 个 Unicode 字符
Identity: 一把 Key = 一个固定访客身份 = 一段共享对话 = 一份滚动记忆 = 一份额度
Tools: get_lounge_info, claim_identity, begin_visit, talk_to_host,
       get_visit_state, end_visit
```

Key 应保存在 MCP 客户端的认证/header 配置中，不能粘贴进聊天消息或作为工具参数传给 AI。首次使用未认领的 Key 时，先调用 `claim_identity` 固定名字；名字之后不能自行修改，但管理员可在本机后台调整。网页与 MCP 使用同一把 Key 时会看到同一份最近 30 条上下文和滚动记忆，显示名允许与别人重名，唯一身份始终由 Key 决定。

只有 `talk_to_host` 会调用模型；其他五个工具仅查询或更新本地会客状态。一次调用失败后不会自动重试，也不会终止这把 Key 之后的正常会谈；失败的访客消息会保留给管理员查看，但不会进入 Connor 的后续上下文或滚动记忆。服务端无法替 MCP 客户端关闭其自身的工具确认、权限弹窗或计划限制，能否在无人干预时访问取决于朋友使用的客户端配置。

普通 MCP 客户端仍可直接使用 Visitor Key Bearer 认证。ChatGPT 网页版使用同一个地址 `https://visitor.aionshome.com/mcp`，但通过 OAuth 2.1 配对：在 Developer mode / Plugins 中添加该地址，首次连接跳转到会客室授权页后输入主人发放的 Visitor Key。ChatGPT 获得的是一小时访问令牌和可轮换的 30 天刷新令牌，不会收到原始 Key；两种认证最终都绑定同一个 visitor ID、固定名字、最近 30 条上下文、滚动记忆和 12 小时额度。

无需提前在会客室网页登录，也不要把 Visitor Key 写进聊天、工具参数或插件说明。若该 Key 尚未认领名字，连接后先调用 `get_lounge_info`，再调用 `claim_identity` 固定名字；之后按 `begin_visit`、`talk_to_host`、`get_visit_state` 和 `end_visit` 使用。轮换或撤销 Key 会立即使它建立的 OAuth 令牌失效；删除访客会级联清除其令牌。OAuth 仍运行在现有 8001 服务、现有数据库和现有 Cloudflare Tunnel 中，没有第二套账号、端口或进程。

## 日常启动、检查和停止

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
powershell -ExecutionPolicy Bypass -File scripts/status.ps1
powershell -ExecutionPolicy Bypass -File scripts/diagnose.ps1
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

- `start.ps1` 在两个健康检查都成功后才报告启动完成；重复启动会被拒绝。
- `status.ps1` 报告 visitor、admin、launcher 和 supervisor 的 PID 状态，不会接管身份不匹配的进程。
- `diagnose.ps1` 使用本项目 `.venv` 中的 Python 和真实 `Settings.load` 验证配置，再检查 SQLite 完整性、两个回环端口、磁盘空间、共享 AionsHome Codex 运行层和两个健康端点。健康响应必须同时满足 HTTP 200、JSON content type、`status: ok` 和正确的 `service`。它只输出固定的配置成功/失败状态，不输出密钥值或解析异常详情。缺少 `.venv`、配置无效、服务尚未启动或共享 Codex 线路不可用时，诊断会报告问题并返回非零状态。
- `stop.ps1` 只停止经过会客室身份校验的进程。原 AionsHome 进程不会被导入、读取、修改或停止。

健康端点是 <http://127.0.0.1:8001/healthz> 和 <http://127.0.0.1:8002/healthz>。

## 从空库完成一次会话

首次启动会自动创建 `data/visitor-lounge.sqlite3`。

1. 在管理首页点击创建邀请；立即复制只短暂展示的 Key，并安全地发给一名访客。
2. 访客在隐私窗口打开访客端，输入 Key 登录，填写昵称并明确同意记录说明，完成 claim。
3. 访客发送短消息；页面显示排队位置并流式展示回复。聊天页只显示该访客自己的最近 30 条消息。
4. 访客 30 分钟无活动后，会话自动挂起；下次用仍有效的 Key 登录会恢复访问，但不会清空历史或额度。模型也可用 `suspend` 动作结束当前访问；安全锁会在 24 小时后自动解除，管理员也可提前解锁。
5. 同一访客至少积累 15 条尚未整理的访客消息且静默 20 分钟后，后台以低优先级更新该访客唯一的一份滚动记忆。后台每 30 分钟检查一次；每个新批次只调用一次，失败不自动重试，也不会抢占正在运行或等待的聊天。
6. 管理员可在访客详情查看按 100 条分页的完整历史、网页/MCP 来源、失败状态、唯一滚动记忆、聊天与记忆更新的模型调用、延迟、token usage、额度窗口和审计记录；页面时间均按北京时间显示。后台还可暂停/解锁/挂起访客、修改固定名称与访客类型、揭示/轮换/撤销 Key、重置额度、添加备注、导出或确认删除访客。

管理员能看到完整聊天记录。访客之间按 visitor identity 隔离；验证时应使用两个不同浏览器配置文件或一个普通窗口加一个隐私窗口，不能共用 cookie。

后台首页的“访客管理”会显示每位访客的消息总数和最近一句，并可直接进入按每页 100 条分页的完整聊天。访客 ID 是不可修改的内部身份，Key 不能改绑给另一位访客：轮换 Key 会使旧 Key 失效但保留身份和历史，撤销 Key 会关闭入口但同样保留记录。单个或批量“永久删除”会删除所选访客的 ID、所有 Key、登录与访问记录、聊天、滚动记忆、额度、任务、模型调用、通知、备注及可识别审计数据，无法恢复；操作必须勾选访客并准确输入 `DELETE`，正在排队或生成的访客不会被删除。

## 额度、长度、并发和冷却

- 每名访客第一次提交生成请求时开启独立的 12 小时窗口；默认最多 15 次生成，管理员可在接待设置中把上限调整为 1–500 并立即应用到当前窗口。窗口到期后的下一条消息开启新的 12 小时窗口。排队/运行会先预留额度，失败或取消按运行时规则结算；后台仍可在访客详情页单独重置额度。
- 单条访客输入最多 500 个 Unicode 字符且最多 600 tokens；完整 prompt 最多 6000 tokens。
- 单次可见回复最多 800 个 Unicode 字符。超过上限会截断并中止该次模型输出。
- 访客页和 prompt 最多带最近 30 条消息；prompt 还会带入该访客唯一的一份滚动记忆。若完整 prompt 超过 6000 tokens，则从最旧的原始消息开始裁剪。管理端仍保留并显示完整原始记录。
- 默认只有 1 个生成槽，最多允许 2 个；默认最多 3 个等待者。队列等待和生成各自最多 120 秒。
- Key 登录是进程内全局 token bucket：容量 10，每分钟补充 10；发消息按访客计，容量 20，每分钟补充 20（约每 3 秒补充 1 次机会）。这不是固定的逐消息冷却时间；同一访客的消息会严格按提交顺序处理，最多一个运行、三个等待，避免不同客户端串乱上下文。

## 数据、日志和运行目录

- `data/visitor-lounge.sqlite3`：访客、聊天、摘要、额度、模型用量与审计记录。
- `logs/visitor.stdout.log`、`logs/visitor.stderr.log`、`logs/admin.stdout.log`、`logs/admin.stderr.log`：两个服务的输出与错误日志。
- `.runtime/`：PID 文件、supervisor 状态及必须保持空白的 `codex-workdir/`。
- `.env`：会客室本机密钥，属于敏感数据。

这些路径均已被 Git 忽略。不要提交或发送数据库、日志、`.env` 或 `.runtime` 内容。

### 备份

为得到一致的 SQLite 快照，先停止会客室，再复制数据库到仓库外的受保护目录。不要在服务运行时复制数据库：

```powershell
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath '.').ProviderPath
powershell -ExecutionPolicy Bypass -File (Join-Path $projectRoot 'scripts\stop.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Visitor Lounge stop failed; backup aborted.' }
$database = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'data\visitor-lounge.sqlite3')).ProviderPath
$backupDir = Join-Path $env:USERPROFILE 'Documents\Visitor-Lounge-Backups'
[void](New-Item -ItemType Directory -Force -Path $backupDir)
$backupDir = (Resolve-Path -LiteralPath $backupDir).ProviderPath
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath $database -Destination (Join-Path $backupDir "visitor-lounge-$stamp.sqlite3")
```

备份完成后可重新启动。恢复时先停止服务，把当前数据库另存为回退副本，再把选定备份复制回 `data/visitor-lounge.sqlite3`，然后运行 `diagnose.ps1` 检查 SQLite 完整性并启动。

### 可恢复重置

不要直接删除数据库。先停止服务，再把它移动到仓库外作为可恢复备份；下一次启动会建立空库。不要在服务运行时移动数据库：

```powershell
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath '.').ProviderPath
powershell -ExecutionPolicy Bypass -File (Join-Path $projectRoot 'scripts\stop.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Visitor Lounge stop failed; reset aborted.' }
$databasePath = Join-Path $projectRoot 'data\visitor-lounge.sqlite3'
if (Test-Path -LiteralPath $databasePath -PathType Leaf) {
    $database = (Resolve-Path -LiteralPath $databasePath).ProviderPath
    $backupDir = Join-Path $env:USERPROFILE 'Documents\Visitor-Lounge-Backups'
    [void](New-Item -ItemType Directory -Force -Path $backupDir)
    $backupDir = (Resolve-Path -LiteralPath $backupDir).ProviderPath
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Move-Item -LiteralPath $database -Destination (Join-Path $backupDir "visitor-lounge-reset-$stamp.sqlite3")
} else {
    Write-Output 'Database does not exist; reset move skipped.'
}
powershell -ExecutionPolicy Bypass -File (Join-Path $projectRoot 'scripts\start.ps1')
```

重置数据库不会影响 AionsHome 原有 Codex 登录，也不会轮换 `.env` 密钥。若数据库还不存在，跳过 `Move-Item`。

## 故障排查

- **提示缺少 `.venv`**：确认在本目录创建虚拟环境，并使用 `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"` 安装。
- **提示 `.env` 缺少键或密钥格式错误**：重新复制模板并按“本地首次启动”生成值。不要把命令输出贴进日志或问题报告。
- **Codex runtime unavailable / 原线路不可用**：先停止会客室，确认 AionsHome 自身的 Codex 聊天线路可用，再运行诊断。不要安装全局 Codex，也不要为会客室单独登录。
- **8001 或 8002 已占用**：用 `status.ps1` 判断是否为会客室进程；若不是，不要强行停止它。释放端口后重试。脚本和应用固定使用这两个回环端口。
- **启动超时或健康检查失败**：查看 `logs/*.stderr.log`，再运行 `diagnose.ps1`。修复配置、端口、磁盘或共享 Codex 线路问题后重新启动。
- **排队已满/长时间等待**：默认只运行一个生成且最多等待三人；等待超过 120 秒会超时。不要为了绕过限制启动第二套同端口实例。
- **模型回复失败**：在管理端访客详情查看模型调用状态、延迟和 token usage；没有 usage 表示 Codex 未报告，不能据此推算精确费用。
- **Codex workdir 非空**：停止服务，检查 `.runtime/codex-workdir/` 中的异常文件并保留证据；适配器会拒绝在非空目录启动模型请求。不要把其中内容复制到原 AionsHome。

## 本地验收清单

在真实使用前由本机管理员完成并记录结果：

- 已安装 `.[dev]` 后，在本目录运行以下命令；它们分别执行测试、Python compile 和 PowerShell 5.1 对全部 6 个脚本的解析：

  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q
  .\.venv\Scripts\python.exe -m compileall -q src tests
  $scripts = @(Get-ChildItem -LiteralPath 'scripts' -Filter '*.ps1' | Sort-Object Name)
  if ($scripts.Count -ne 6) { throw "Expected 6 PowerShell scripts; found $($scripts.Count)." }
  $parseFailed = $false
  foreach ($script in $scripts) {
      $tokens = $null
      $errors = $null
      [void][Management.Automation.Language.Parser]::ParseFile(
          $script.FullName,
          [ref]$tokens,
          [ref]$errors
      )
      if ($errors.Count -gt 0) {
          $parseFailed = $true
          Write-Error "$($script.Name): $($errors -join '; ')"
      }
  }
  if ($parseFailed) { throw 'PowerShell parser validation failed.' }
  ```

- 从空数据库启动后，两个 `/healthz` 都返回各自的 `service` 和 `status: ok`，`diagnose.ps1` 不泄露密钥，`stop.ps1` 关闭两个端口且不影响原 AionsHome。
- 仅在 AionsHome 原有 Codex 聊天线路可用时发送一次极短真实消息；确认回复不超过 800 个 Unicode 字符、模型调用行有延迟/token usage，且 `.runtime/codex-workdir/` 仍为空。否则记录 `SKIPPED` 和原因，不触发登录或重复调用。
- 创建 A/B 两个邀请并用两个浏览器配置文件登录，近同时发送可区分消息；确认只显示各自最近 30 条消息，一个运行时另一个显示队列位置，额度只变化在正确访客，管理详情历史完全分离。
- 确认 `git status --short` 中没有数据库、日志、认证、PID 或 `.env` 文件。

## 当前范围明确不包含

当前版本不包含推送通知、远程管理员登录、业务数据桥接或同步，也不提供企业级公网安全承诺。OAuth 仅用于把 ChatGPT 网页版配对到既有 Visitor Key 身份，不是新的账户体系。模型层仅复用既有 Codex 运行线路；访客公网入口限定为 `visitor.aionshome.com`，管理后台不开放公网，也不要配置路由器端口转发。
