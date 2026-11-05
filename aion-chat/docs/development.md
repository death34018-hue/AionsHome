# 服务结构与开发入口

## 从一个页面找到实现

1. 从[小家首页](../static/home.html)的 `APPS` 表查用户看到的名字和 URL。例如「小厨房」对应 `/kitchen`，「陪伴阅读」对应 `/reading`。
2. 在[主服务](../main.py)搜索 URL 或 `include_router`，找到页面和 API 路由。有些功能在自己的路由模块注册，找不到时再查 [routes/](../routes/) 里的 `@router.get` 或 `APIRouter`。
3. 查看该功能对应的页面、业务模块和少量相关测试。例如书架从 `static/reading.html` 进入 `routes/book.py`；小厨房的接口前缀是 `/api/kitchen`，小音乐盒查 `/api/compositions`。娱乐室的大厅与 MCP 探索分别使用 `/playground` 和 `/playground/explore`。

**找聊天里没有桌面图标的功能**，可以先按界面文字查[主私聊菜单](../static/chat.html)或[聊天室菜单](../static/chatroom.html)。消息旁的按钮和手势由同名 JS 生成。伴侣能主动做什么、需要打开哪个开关，从[工具能力清单](../capabilities.py)按中文名称查，再看对应聊天路由如何处理；有些能力还要求连接设备或配置外部服务。

## 服务与数据

| 要查什么 | 入口 |
| --- | --- |
| HTTP 页面、静态文件与启动 | [main.py](../main.py) 注册 FastAPI 页面、静态目录和路由，默认端口为 8080；[static/](../static/) 放浏览器和 WebView 共用的页面。 |
| 聊天模型、提示词与多端同步 | [config.py](../config.py) 定义内置模型和数据目录，[ai_providers.py](../ai_providers.py) 对接服务商，[context_builder.py](../context_builder.py) 组装上下文，[ws.py](../ws.py) 负责 WebSocket 通知。 |
| 两个聊天入口 | [routes/chat.py](../routes/chat.py) 处理主聊天，[routes/chatroom.py](../routes/chatroom.py) 处理群聊及聊天室私聊。改两边共用的卡片或语音表现时，分别从这两个入口核对。 |
| 原生能力 | [Android 工程](../../AionApp/) 负责通知、语音、本机传感器和 WebView 桥。摄像头、手环、红外等功能的准备条件见[设备说明](devices.md)。 |

核心 SQLite 数据库位于 `aion-chat/data/chat.db`。服务配置、上传文件、相册、歌曲和语音珍藏大多在 `aion-chat/data/` 下。聊天室附件另有 `Connor-Codex/uploads/`；独立部署的访客会客室也有自己的数据库，备份范围按实际启用的服务选择。[会客室备份说明](../../AionsHome-Visitor-Lounge/README.md#备份)

## 更新文档

改动用户能做的事，更新[项目入口](../README.md)索引指向的那一篇。新增入口时，在索引里用界面上的名字加一行；服务商支持的模型清单等经常变化的细节，直接指向代码或设置页。查以前为什么这样实现、旧版接口是什么，按主题查[历史档案](history/README.md)。
