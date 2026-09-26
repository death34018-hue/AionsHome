# Codex CLI 日常聊天 Prompt 精简指南

适用于：把 Codex CLI 接入私聊、群聊或陪伴应用，且应用自己已经提供搜索、记忆和其他操作协议的项目。

目标：只让**聊天子进程**携带必要的工具说明；本例保留 `view_image`，移除开发工具上下文。用于写代码、维修或日常工作的 Codex 保持原有能力。

本文记录 AionsHome 在 **Codex CLI 0.156.0** 上的实现与本机验证。更新日期：2026-09-26。不同版本、模型和接入方式需要重新核对，尤其是模型目录中的工具字段。

## 直接交给你的 Codex

把本文件发给负责修改项目的 Codex，并附上这段话：

> 请参照本文优化我项目里日常私聊、群聊使用的 Codex CLI。先定位真实调用入口、当前 CLI 版本、图片输入和应用能力协议，再修改聊天线路。保留必要的图片能力、人格、记忆和应用内置功能；代码工作、维修室及桌面 Codex 的配置保持原样。不要只写“禁止使用工具”的提示词，要验证实际请求里的工具定义确实减少。按现有项目结构做小范围修改，在当前分支工作，保留已有无关改动。完成后说明改了哪些文件、工具清单和 token 估算、验证结果，以及是否需要重启。本文的具体路径和模型名是例子，请替换为本项目的实际值。

## 这次优化了什么

这里分为“之前已经做过的精简”和“这次补上的精简”。效果数字是本项目的实测估算，不能保证另一套项目有相同结果。

| 层次 | 做法 | 作用 |
|---|---|---|
| 之前：运行隔离 | 聊天使用独立 `CODEX_HOME` 和子进程环境，只复用本机认证 | 避免把桌面工作的配置、插件和规则一起带进聊天 |
| 之前：基础指令 | 用短的陪伴聊天指令替换开发型基础指令 | 减少软件开发工作流说明，保留自然对话方向 |
| 之前：上下文注入 | 关闭应用、权限、协作模式、环境上下文等 CLI 附加说明 | 不把聊天用不到的开发环境描述送给模型 |
| 之前：Skills | 将发现的 `SKILL.md` 精确路径设置为禁用 | 不注入技能目录及工作流程 |
| 本次：非必要工具 | 关闭原生搜索、目标管理、询问、计划、插件等 | 应用已有的能力协议继续负责这些功能 |
| 本次：模型目录 | 给聊天加载一份修改过工具元数据的模型目录副本 | 消除模型目录强制开启的执行包装、子代理、改文件等定义 |
| 本次：环境清理 | 移除继承自桌面任务的工具管道和任务标识 | 防止从 Codex 终端启动服务时混入桌面任务上下文 |
| 本次：实际验收 | 抓取文字和图片聊天请求，核对工具定义 | 确认最终只剩图片查看，不依赖“开关看起来已关闭” |

修改前，陪伴模式已经精简过，仍有 `exec/wait`、`web__run`、目标管理、改文件、子代理等工具说明。6-Sol 还带异步询问和时钟工具。

| 项目使用的模型 | 修改前工具定义压缩 JSON 的 token 估算 | 修改后 |
|---|---:|---:|
| `gpt-5.6-sol` | 6,532 | 124 |
| `gpt-6-sol` | 7,029 | 124 |

修改后只有 `view_image`：其描述正文约 **23 token**，连同参数与命名空间结构，压缩 JSON 约 **124 token**。按同一统计口径减少约 **98%**。这不是整段聊天上下文的减少比例，也不是服务端实际计费数；人设、历史、记忆、图片内容和应用能力说明均未包含在这个数字里。

## 1. 先分清聊天与工作线路

从私聊、群聊的路由一路追到创建 CLI 子进程的位置，找出：启动命令、环境变量、基础指令、开发者指令和图片输入。

另行核对维修室、代码工作入口。即使它们使用同一个 CLI 可执行文件，也可以采用不同的启动参数和 `CODEX_HOME`。**不要为了精简聊天，去改全局 `~/.codex/config.toml`，也不要删除全局 Skills 或修改 CLI 二进制。**

本项目的结构是：

```text
私聊 / 群聊 → call_codex_cli → 聊天命令与环境 → 临时 App Server 会话
维修室      → repair_codex  → 维修专用命令与环境 → 独立工作会话
```

若其他模块复用了聊天命令，例如访客接待，也会一起采用精简配置；修改前说明这些共享入口。若朋友的项目有必要的原生 MCP 或其他聊天工具，应保留它们，不能照搬“只留一个工具”的清单。

## 2. 图片输入和查看图片工具是两回事

App Server 可以在 `turn/start` 中直接收到本地图片：

```json
{
  "threadId": "实际线程 ID",
  "input": [
    {"type": "text", "text": "看看这张图片"},
    {"type": "localImage", "path": "本地图片的绝对路径"}
  ]
}
```

这种方式由 CLI 把图片转换为模型输入，正常看用户发来的图片不需要先调用 `view_image`。`view_image` 则用于模型主动读取另一张本地图片。本项目保留它作为唯一的原生聊天工具。

如果当前应用只把图片路径写进文字 prompt，没有真正附图，先修正或保留原来的读图方式。不能因为工具清单变少，就把看图功能弄丢。

搜索、记忆查询及其他操作也要沿调用链确认：本项目通过应用自己的文本协议执行，它们不依赖 CLI 的 `web__run`。精简时保留这些协议、解析器和执行逻辑。

## 3. 使用短的聊天基础指令

创建一个项目内的基础指令文件，例如 `prompts/companion_base.md`：

```text
You are responding inside a companion-chat application.

Prioritize natural conversation and the application-provided persona, relationship, memory, capabilities, and current state. Answer the latest user message directly and remain consistent with that context.

Do not inspect the workspace, run shell commands, edit files, or act as a software-development agent.

Use only application protocols explicitly described in developer context. Never invent protocol execution or results.
```

通过 `model_instructions_file` 加载它。这个参数用于替换内置基础指令，不等同于再追加一个 `AGENTS.md`。[官方配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)

保留项目原有的开发者级能力说明、角色配置、记忆和当前状态。基础文件不必重复所有人格信息，也不要照搬别人的伴侣名字。需要执行结果的能力，仍然要等待应用返回结果后再回应。

## 4. 仅向聊天子进程传入配置覆盖

以下是本项目在 0.156.0 上验证过的覆盖项。以 CLI 的 `-c key=value` 参数传入；不要拿它覆盖全局配置。先用本机版本的帮助、配置读取或试运行确认兼容性，未知参数不一定会报错。

```toml
model_instructions_file = "聊天基础指令文件的绝对路径"
model_catalog_json = "聊天专用 companion-models.json 的绝对路径"
web_search = "disabled"

features.shell_tool = false
features.multi_agent = false
features.multi_agent_v2 = false
features.code_mode = false
features.code_mode_only = false
features.goals = false
features.sleep_tool = false
features.plugins = false
features.apps = false
features.image_generation = false
features.skill_search = false
features.skill_mcp_dependency_install = false
features.remote_plugin = false
features.view_image = true

tools.experimental_request_user_input.enabled = false
tools.update_plan.enabled = false

include_apps_instructions = false
include_permissions_instructions = false
include_collaboration_mode_instructions = false
include_environment_context = false
```

`web_search="disabled"` 是官方支持的移除原生搜索方式。[官方配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)

这份清单不包括输出风格设置。原有模型名、推理档位和回复详细程度按项目需要保留；调低 `model_verbosity` 并不能替代清理工具定义。

例如用 Python 构造参数时，按参数列表启动进程，并对路径进行 TOML 字符串序列化：

```python
overrides = [
    "model_instructions_file=" + json.dumps(str(base_file), ensure_ascii=False),
    "model_catalog_json=" + json.dumps(str(catalog_file), ensure_ascii=False),
    'web_search="disabled"',
    # 其余覆盖项按上表添加；保留项目自己的 developer_instructions。
]
command = [node_path, codex_script]
for override in overrides:
    command.extend(["-c", override])
command.extend(["app-server", "--stdio"])
```

若项目直接启动 `codex.exe`，替换命令前缀即可。若用的是 `codex exec`，沿用它自己的传图和会话参数，不要为了套本文改掉整个传输层。

保留原有的沙箱、审批、临时会话、流式输出和取消机制。`include_permissions_instructions=false` 控制的是权限说明的注入，实际权限仍由会话配置决定，两者分别核对。

## 5. 核心处理：聊天专用模型目录副本

本机实测发现：部分模型元数据会强制启用工具。`features.multi_agent=false` 等开关虽然显示关闭，请求仍可能包含子代理或执行包装说明。因此，只加一组关闭开关并没有完成精简。

本项目通过 `model_catalog_json` 给聊天加载目录副本。该加载参数有官方文档；**下面五个内部字段的组合是 0.156.0 的实测实现，不是跨版本稳定保证。** [官方目录加载参数](https://learn.chatgpt.com/docs/config-file/config-reference)

获取原始目录的顺序：

1. 优先读聊天专用 `CODEX_HOME` 下现有的 `models_cache.json`。
2. 没有缓存时，运行实际使用的 CLI 的 `debug models --bundled`，读取它随程序附带的目录。这个命令不需要发起模型生成请求。[官方调试命令](https://learn.chatgpt.com/docs/developer-commands)
3. 保留每个模型的全部原始属性，仅修改下面的工具元数据，写到新的 `companion-models.json`。

转换函数可以参照：

```python
def build_chat_model_catalog(source: dict) -> dict:
    models = source.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("聊天模型目录为空")
    return {"models": [
        {
            **model,
            "tool_mode": "disabled",
            "multi_agent_version": None,
            "experimental_supported_tools": [],
            "apply_patch_tool_type": None,
            "supports_search_tool": False,
        }
        for model in models
    ]}
```

Python 的 `None` 写入 JSON 后是 `null`。`multi_agent_version` 不要简单改成 `v1`：实测这样仍可能带上 V1 子代理工具。`supports_search_tool` 对应工具发现能力，不要把它与应用自己的联网搜索混淆。

保留模型 `slug`、上下文窗口、图片输入、推理支持等其他字段。不要手写一个只剩五个字段的模型条目，不要改原始缓存，也不要把别人的目录文件当成自己的模型目录。

在启动聊天 App Server **之前**准备好文件，并保证聊天命令使用它；维修命令不引用它。写入可采用临时文件加替换，避免两个聊天进程同时看到半写入的 JSON。可按应用进程缓存转换结果；CLI 或模型目录升级后重建并复验。实际使用的模型必须包含在验证范围内，不能只测试默认模型。

## 6. 隔离环境，并禁用实际发现的 Skills

聊天使用独立 `CODEX_HOME`，例如应用自己的 `codex-chat` 目录。Windows 子进程还可将 `HOME`、`USERPROFILE` 指向这个聊天 profile 的父目录。只修改传给子进程的环境副本，不修改整台电脑的环境变量。

若复用本机登录，仅在本机同步认证到聊天 profile，不复制全局配置、工作历史和规则。本文不需要提供或分享认证文件。

清理子进程环境中的这些桌面任务变量：

```python
excluded = {
    "CODEX_APP_TOOLS_PIPE_PATH",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_PERMISSION_PROFILE",
}
env = {key: value for key, value in original_env.items() if key not in excluded}
```

独立 `CODEX_HOME` 不一定能阻止所有 Skills 发现。检查聊天 home、用户 `.codex/skills`、`.agents/skills`、插件缓存和项目技能目录，为实际发现的每个 `SKILL.md` 生成精确禁用项：

```toml
skills.config = [
  { path = "某个 SKILL.md 的规范化绝对路径", enabled = false }
]
```

首次初始化或 CLI 升级可能生成新的内置 Skills。初始化后重新发现这些路径，更新禁用列表，并核对实际 prompt；空目录上扫描一次、缓存一个空列表，不能作为“Skills 已去掉”的证明。不要删除技能文件，也不要为精简聊天把工作 Codex 的技能禁掉。

## 7. 用真实请求验收，而不是只看配置

先检查 `--version`、`features list` 和聊天参数，随后用 `debug prompt-input` 检查角色指令、Skills 等文本。[官方调试命令](https://learn.chatgpt.com/docs/developer-commands)

**`debug prompt-input` 不包含完整的工具定义。** 要证明工具清理到位，还需抓取 CLI 构造的实际模型请求。

推荐在临时 profile 中，把诊断模型接口接到本机模拟服务，用假认证和无私密内容的文字、图片完成检查。保留实际使用的 CLI、模型目录与聊天配置；模拟服务只接收请求，不做真实模型生成。别随便换成不支持原生搜索的自定义 provider，否则可能得到错误的“搜索工具已消失”结论。

0.156.0 可能使用 WebSocket，也可能有 `generate=false` 的启动预热。诊断器要支持实际传输方式，完成预热后继续检查真正的聊天请求；不能把预热请求误当成已验证的聊天回合。

工具可能出现在顶层 `tools`，也可能在 `input` 的 `additional_tools` 项里。需展开 namespace，同时检查执行包装描述中嵌入的工具声明。只找一个顶层 `web_search` 字段并不够。

建议只做这些关键验收：

- 每个实际使用的聊天模型，跑一条文字请求：最终工具清单只含必要工具。本例期望 `view_image`。
- 跑一条用户图片请求：确认图片真的进入模型请求，同时非必要工具没有重新出现。
- 检查应用基础指令、开发者级能力协议仍然存在，Skills 和工作流程说明已经去掉。
- 核对工作/维修入口没有引用聊天专用目录或覆盖项。

本项目这次通过了两种模型的请求抓取，以及 18 项相关聚焦检查。抓取验证证明请求内容正确，不等同于真实模型生成或每个应用功能都做过端到端测试。

统计 token 时统一使用一种 tokenizer，例如 `o200k_base`，分别列出说明正文和工具定义压缩 JSON 的估算。后者包含结构与转义开销，不等同于服务端实际计费；也不要将缓存命中或整段聊天 token 混入工具减量数字。

## 8. 生效、升级和回退

修改 Python/Node 后端后，运行中的聊天服务通常需要重启才能加载新逻辑。说明“代码已改”“验证通过”“服务已加载新配置”分别到哪一步，不把它们混为一谈。

遇到版本不支持的目录字段，先按该版本检查工具构造方式，调整聊天副本并重新验收。目录生成失败时应明确报错，不要静默回退到未精简的开发型 profile 后声称成功。升级 CLI 后也要复验，不能只看旧的测试数字。

回退时撤销聊天启动覆盖和目录加载逻辑，恢复原来的聊天配置。因全局配置、原始模型缓存和维修入口没有改动，无需回滚它们。

## AionsHome 实现位置，供对照

朋友的项目不需要有相同文件名。交付本文一份文件就可以让 Codex 按上述步骤定位自己的实现。

| 文件 | 本次相关内容 |
|---|---|
| `aion-chat/ai_providers.py` | 日常聊天命令、隔离环境、Skills 禁用、应用能力指令 |
| `aion-chat/codex_chat_profile.py` | 生成聊天专用模型目录副本 |
| `aion-chat/prompts/codex_companion_base.md` | 短的陪伴聊天基础指令，之前已添加 |
| `aion-chat/codex_app_server.py` | 原有传输层，继续直接发送 `localImage` |
| `aion-chat/test_codex_cli_minimal_mode.py` | 覆盖项、目录转换、隔离环境等检查 |
| `aion-chat/repair_codex.py`、`repair_runtime.py` | 独立维修入口，本次未修改 |
