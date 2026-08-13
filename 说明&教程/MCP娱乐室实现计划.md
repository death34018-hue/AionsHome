# MCP 娱乐室 实现计划

## 项目目标
为 AI 伴侣新增独立的「娱乐室」模块，通过 MCP 协议接入外部服务（如 AI 小镇、论坛等）。
完全不动现有聊天功能，独立页面、独立路由、独立 AI 调用链路。
框架通用化，接新 MCP 服务只需加一行配置。

## 核心流程
1. 出门前：压缩最近40条聊天为摘要 + 取最近8条记忆 + 读 AI 人设 → 组装 system prompt
2. 在外面：用户下达指令 → AI 带着工具列表思考 → tool_call → MCP 执行 → 结果返回 → 循环直到最终回复
3. 回家后：整段对话 AI 总结 → 写入 memories 表 → 主聊天插入一条系统消息

## 新增文件清单

### 后端
1. `aion-chat/mcp_client.py`         — MCP 连接管理器
2. `aion-chat/routes/playground.py`  — 娱乐室后端路由
3. `aion-chat/data/mcp_servers.json` — MCP Server 配置文件（自动创建）

### 前端
4. `aion-chat/static/playground.html` — 娱乐室页面
5. `aion-chat/static/playground.css`  — 样式
6. `aion-chat/static/playground.js`   — 前端逻辑

### 改动文件（极小改动）
7. `aion-chat/main.py`     — 注册路由 + 页面路径（约5行）
8. `aion-chat/static/home.html` — APPS 数组加一项（1行）

---

## 第一步：mcp_client.py — MCP 连接管理器

功能：
- 管理多个 MCP Server 的连接生命周期
- 支持两种传输：Streamable HTTP（远程）和 stdio（本地进程）
- 连接后自动调用 tools/list 获取工具列表
- 提供统一的 call_tool() 接口
- 将 MCP 工具 schema 转换为 OpenAI function calling 格式（供 AI 模型使用）

核心类：
```python
class MCPManager:
    async def connect(server_name: str) -> list[tool]     # 连接指定 server，返回工具列表
    async def disconnect(server_name: str)                 # 断开
    async def call_tool(server_name, tool_name, args)      # 调用工具
    def get_tools_for_ai(server_name) -> list[dict]        # 转成 OpenAI tools 格式
    def list_servers() -> list[dict]                       # 列出所有配置的 server
```

依赖：pip install mcp（Python MCP SDK）

---

## 第二步：data/mcp_servers.json — 配置文件

```json
{
  "servers": [
    {
      "name": "AI小镇",
      "type": "http",
      "url": "https://aisay.top/chatroom/mcp",
      "headers": {},
      "enabled": true
    }
  ]
}
```

后续加新服务只需在这里追加一项。

---

## 第三步：routes/playground.py — 后端路由

### API 列表：

```
GET  /api/playground/servers
  → 返回已配置的 MCP Server 列表及连接状态

POST /api/playground/connect
  body: { "server": "AI小镇" }
  → 连接指定 server，返回可用工具列表

POST /api/playground/disconnect
  body: { "server": "AI小镇" }
  → 断开连接

POST /api/playground/run（SSE 流式）
  body: { "server": "AI小镇", "instruction": "去逛逛，吐槽一下今天的事", "conv_id": "当前聊天对话ID" }
  → 执行完整流程：
     1. 从 conv_id 取最近40条消息 → 调用 AI 压缩为摘要
     2. 从 memories 表取最近8条记忆
     3. 从 worldbook 取人设
     4. 组装 system prompt + 用户指令 + MCP 工具列表
     5. 进入 tool calling 循环：
        SSE event: "thinking"  — AI 正在思考
        SSE event: "tool_call" — AI 要调用工具 { tool, args }
        SSE event: "tool_result" — 工具执行结果
        SSE event: "text"     — AI 的文字输出（流式 delta）
        SSE event: "done"     — 全部完成
     6. 循环结束后自动总结 → 写入 memories 表（type="playground"）
     7. 在 conv_id 对话中插入系统消息："🎮 AI去XX逛了一圈..."

POST /api/playground/stop
  → 中断当前正在执行的任务
```

### tool calling 循环核心逻辑（伪代码）：

```python
messages = [system_prompt, user_instruction]
tools = mcp_manager.get_tools_for_ai(server)

while True:
    response = await call_ai(messages, tools)  # 带 tools 的 AI 调用

    if response.has_tool_calls:
        for tool_call in response.tool_calls:
            yield SSE("tool_call", {tool_call.name, tool_call.args})
            result = await mcp_manager.call_tool(server, tool_call.name, tool_call.args)
            yield SSE("tool_result", result)
            messages.append(tool_call_message)
            messages.append(tool_result_message)
        continue  # 让 AI 继续处理
    else:
        yield SSE("text", response.content)  # 最终回复
        break

# 循环结束，写记忆、插系统消息
```

AI 调用独立实现在此文件中，直接用 httpx 调 SiliconFlow/Gemini，不走 ai_providers.py。
这样完全隔离，不影响主聊天的任何逻辑。

---

## 第四步：前端页面 playground.html/css/js

### 页面布局：
```
┌──────────────────────────────┐
│ ⬅ 娱乐室          [服务器▼] │  ← 顶栏：返回 + 选择 MCP Server
├──────────────────────────────┤
│                              │
│  ┌─ 连接状态卡片 ──────────┐ │  ← 显示当前连接的 server + 可用工具数
│  │ 🟢 AI小镇 已连接        │ │
│  │ 可用工具: 8个            │ │
│  └──────────────────────────┘ │
│                              │
│  ┌─ 行动日志 ──────────────┐ │  ← 实时显示 AI 的行动过程
│  │ 🤔 正在思考...           │ │
│  │ 🔧 调用工具: look_around │ │
│  │ 📋 结果: 你看到了广场... │ │
│  │ 🔧 调用工具: write_msg   │ │
│  │ 📋 结果: 留言成功        │ │
│  │ 💬 逛完啦！我在留言本... │ │  ← AI 最终回复
│  └──────────────────────────┘ │
│                              │
├──────────────────────────────┤
│ [输入指令...]        [出发] │  ← 输入框 + 发送按钮
└──────────────────────────────┘
```

### 前端逻辑：
- 进入页面 → GET /api/playground/servers 加载 server 列表
- 选择 server → POST /api/playground/connect 连接
- 输入指令点出发 → POST /api/playground/run 开始 SSE 监听
- 实时渲染行动日志（tool_call 显示为工具卡片，text 显示为 AI 说话）
- 任务结束后输入框恢复可用，可以继续下一轮指令

---

## 第五步：main.py 注册路由

添加：
```python
from routes import playground as playground_routes
app.include_router(playground_routes.router)

@app.get("/playground")
async def page_playground():
    return FileResponse(BASE_DIR / "static" / "playground.html")
```

---

## 第六步：home.html 加图标

在 APPS 数组中加一项：
```javascript
{ id: 'playground', name: '娱乐室', icon: '/public/funIcon_0020_娱乐室.png', url: '/playground' },
```
（图标先用现有的某个，后面再换）

---

## 依赖安装

```bash
pip install mcp
```
（MCP Python SDK，包含 ClientSession、streamablehttp_client、stdio_client 等）

---

## 实施顺序

1. 先装 mcp 依赖
2. 写 mcp_client.py（连接管理器）→ 写个测试脚本验证能连上 AI 小镇并拿到工具列表
3. 写 routes/playground.py（后端路由 + tool calling 循环）
4. 写前端三件套 playground.html/css/js
5. 改 main.py 注册路由
6. 改 home.html 加入口
7. 联调测试：连接 AI 小镇 → 下指令 → 看行动日志 → 确认记忆写入

每一步都可以独立验证，互不阻塞。

---

## 关键设计：带上下文出去 + 把经历带回来

### 出门时的「行囊」
- 从当前聊天对话取最近 40 条消息 → 用一次 AI 调用压缩为 200-300 字的生活摘要（省 token）
- 从 memories 表取最近 8 条记忆，直接注入
- 从 worldbook.json 读 AI 人设 + 用户人设

### 回家时的「记忆写入」
- 整段娱乐室对话 → AI 总结为一段记忆 → 写入 memories 表（type="playground"）
- 在主聊天对话中插入系统消息（类似音乐/视频通话的系统消息）
- 下次正常聊天时，build_surfacing_memories 会自动召回这条记忆
- AI 就能自然提起："我今天去小镇看到一个有趣的帖子……"

### 现有代码零改动
- memory.py — 不改（记忆系统按时间+向量召回，新记忆自动参与）
- ai_providers.py — 不改（娱乐室自己实现 AI 调用）
- routes/chat.py — 不改（主聊天逻辑完全隔离）
