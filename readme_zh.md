# Blue Lake Agent

[English README](README.md) · [架构源文件](docs/architecture.mmd) · [部署指南](deploy/DEPLOY.md)

Blue Lake Agent 是一个可自托管、单进程的工具调用型 LLM 聊天应用。它将 React/Vite 单页应用、FastAPI 后端、与框架无关的 ReAct Agent Core、SQLite 持久化、文件式 Skills 与 OpenAI 兼容模型端点组合在一起。

它面向可信的私有环境：每个浏览器拥有稳定的 workspace 身份，每个 workspace 可以保存各自加密的模型配置。它不是可直接暴露到公网的多租户认证系统。

## 已实现能力

- **每轮思考强度。** 输入框左下角可选 `low`、`medium`、`high`、`max`，浏览器记住上次选择，并应用到该轮 ReAct 的全部 `main` 调用。
- **流式 Agent 对话。** 可取消的 ReAct 循环会持续发送文本、可用的思考摘要、工具调用、工具结果、错误与完成状态。
- **持久化会话。** 可创建、恢复、重命名和删除会话；消息、工具执行轨迹与已加载 Skills 保存在 SQLite 中。
- **按 workspace 隔离的状态。** `workspace_id` HttpOnly Cookie 让浏览器刷新后仍能访问同一组会话和已保存的模型配置。
- **加密的模型设置。** 设置面板保存 `main` 和可选 `summary` 两个角色的配置，并用 Fernet 加密 API key；浏览器只能得到掩码后的 key。
- **Skills 与工具。** 可信 Markdown Skills 可在 UI 中选择、通过 `@skill-name` 提及，或由工具管理。内置工具为 `calculator`、限定 workspace 的 `read_file`、`load_skill` 与 `remove_skill`。
- **有界执行。** Agent 轮数、连续工具失败次数、工具超时、工具输出大小和上下文大小都可配置。
- **低干扰聊天界面。** 助手回合只固定显示一行默认折叠的“思考中”；兼容端点返回纯文本摘要时可点击展开，工具轨迹仍单独可见。
- **可靠的本地存储。** SQLite 是事实数据源。Redis 仅作为可选的尽力而为旁路缓存；内存 TTL 缓存始终可用。

## 总体架构

![Blue Lake Agent 架构图](docs/architecture.png)

上图由 [`docs/architecture.mmd`](docs/architecture.mmd) 生成。代码分为五个边界：

| 边界 | 主要位置 | 职责 |
| --- | --- | --- |
| SPA | `web/src` | UI 状态、REST 请求、基于 `fetch()` 的 SSE 解析、设置与视觉层 |
| API / 组合层 | `server/main.py`、`server/api` | FastAPI 路由、workspace 中间件、SSE 传输、会话 CRUD、运行协调 |
| Agent Core | `server/agent` | 与 HTTP 无关的 ReAct 循环、上下文准备、Skills、工具与类型化事件 |
| 适配器 | `server/storage`、`server/llm_resolver.py`、`server/agent/llm.py` | SQLite、缓存、加密用户配置、workspace 文件、OpenAI 兼容客户端 |
| 外部状态 | `data/`、`skills/`、模型端点、可选 Redis | 持久化数据、可信指令、模型服务与可选缓存 |

最重要的依赖规则是：**Agent Core 只依赖端口（ports），不依赖 FastAPI、SQLite 或 OpenAI SDK。** `server/main.py` 作为组合根，为它注入真实适配器。

### 一次请求如何流动

1. SPA 调用 `GET /api/bootstrap`；服务端创建或复用 HttpOnly `workspace_id` Cookie。
2. SPA 加载会话、Skills、公开运行限制和掩码后的用户配置。此后，`AuthMiddleware` 将私有 API 请求限定在当前 workspace。
3. 消息连同 `reasoning_effort` 发往 `POST /api/chat`；API 只接受 `low`、`medium`、`high`、`max`，随后打开 SSE 流。
4. `AgentCore` 将所选强度原样应用到该次运行的每个 `main` 调用；独立的 `summary` 角色不继承它。
5. 回答、尽力提取的纯文本思考摘要和工具事件以类型化 SSE 返回。回答与思考元数据按同一 `request_id` 持久化，刷新后仍合并为一个助手回合。

### 上下文与模型角色

- `main` 是对话必需角色，负责流式回答和工具调用。
- `summary` 是可选角色；配置完整时用于上下文压缩和首条消息标题。它不可用或失败时，对话仍可继续：标题回退为首条提示词，上下文压缩回退为确定性裁剪。
- `reasoning_effort` 作为 Chat Completions 顶层参数发送，不做模型特定映射，也不静默降档；模型不支持某个档位时沿用现有可读错误链路。
- Chat Completions 没有官方思考摘要契约。适配器只转发兼容网关在 `reasoning_content`、`reasoning` 或 `thinking` 中提供的直接纯文本，并忽略结构化或不透明内容。
- `ContextManager` 会估算中英文混合 token，保留近期消息与 Skill 注入，并把一条 assistant 工具调用和其工具回复视为不可拆分的一组。

### Skills 与工具安全

Skills 是 `<workspace root>/skills` 下的 Markdown 文件；默认配置下即仓库的 [`skills/`](skills/) 目录。每个文件必须带有 front matter：

```markdown
---
name: concise_plan
description: Turn a broad goal into a small executable plan.
---

在这里写入可信的模型指令。
```

已加载的 Skill 会按会话持久化并去重。`read_file` 只能解析 workspace root 内的路径并限制文件大小；这是应用层保护，不是操作系统级 sandbox。

## 仓库导览

```text
server/
  api/                 FastAPI 路由、请求 schema、中间件、服务适配器
  agent/               ReAct Core、ports、上下文管理、Skills、工具、LLM 适配器
  storage/             SQLite、缓存、加密配置、workspace 解析器
  main.py              延迟组合根与可选 SPA 托管
web/
  src/                 React 应用、API client/SSE parser、组件、视觉场景
  public/fonts/        内置 LXGW WenKai Lite 字体及其许可说明
skills/                可信 Markdown Skill 目录
tests/                 Python 单元/集成测试
deploy/                Ubuntu + nginx + systemd 部署模板与指南
docs/architecture.mmd  Mermaid 架构图规范源文件
```

## 快速开始（Windows 开发）

### 前置条件

- Python 3.11+
- 与 Vite 8 兼容的当前 Node.js 版本
- 一个兼容 OpenAI Chat Completions 且支持 streamed tool calls 的模型端点
- 只有需要可选共享旁路缓存时才需要 Redis

### 1. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

cd web
npm ci
cd ..
```

### 2. 启动后端

```powershell
start.bat
```

首次运行且未设置 `AGENT_SECRET_KEY` 时，`start.bat` 会创建含合法 Fernet key 的本地 `.agent_secret_key`。它还会为本机 loopback HTTP 设置 `AGENT_COOKIE_SECURE=false`，并在 `http://127.0.0.1:8000` 启动 FastAPI。

### 3. 启动前端

```powershell
cd web
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 会把 `/api` 代理到 8000 端口上的 FastAPI。

### 4. 配置模型

打开 **设置**，填写 `main` 的 `base_url`、`api_key` 与 `model`。先点击**测试**，再**保存**。`summary` 可以另行配置，但开始对话不依赖它。

## 配置

[`config.yaml`](config.yaml) 提供默认值。所有相对路径相对于选定配置文件解析；环境变量优先级更高。

| 范围 | 主要配置 |
| --- | --- |
| 配置文件 | `AGENT_CONFIG` |
| 密钥与身份 | `AGENT_SECRET_KEY`、`AGENT_REQUIRE_USER_CONFIG`、`AGENT_COOKIE_SECURE` |
| 主 / 摘要模型 | `AGENT_MAIN_*`、`AGENT_SUMMARY_*`（`API_KEY`、`BASE_URL`、`MODEL`、`MAX_TOKENS`、`TIMEOUT_S`、`TEMPERATURE`） |
| Agent / context 限制 | `AGENT_MAX_TURNS`、`AGENT_MAX_TOOL_RETRIES`、`AGENT_TOOL_TIMEOUT_S`、`AGENT_TOOL_RESULT_MAX_CHARS`、`AGENT_TOKEN_BUDGET`、`AGENT_SUMMARY_TRIGGER_RATIO`、`AGENT_PRESERVE_RECENT_MESSAGES` |
| 存储与缓存 | `AGENT_SQLITE_PATH`、`REDIS_URL`、`AGENT_CACHE_TTL_S` |
| 服务端与 SPA | `AGENT_HOST`、`AGENT_PORT`、`AGENT_CORS_ORIGINS`、`AGENT_STATIC_DIR` |
| Workspace | `AGENT_WORKSPACE_ID`、`AGENT_WORKSPACE_NAME`、`AGENT_WORKSPACE_ROOT` |
| 前端 API origin | `VITE_API_BASE_URL` |

当前提交的 `config.yaml` 设置了 `llm.require_user_config: true`。在该模式下，服务端会在缺少合法 `AGENT_SECRET_KEY` 时刻意拒绝启动，避免已经保存的 API key 在不知情时变得无法恢复。

## API 概览

| Endpoint | 用途 |
| --- | --- |
| `GET /api/bootstrap` | 创建或复用浏览器 workspace 身份 |
| `GET /api/sessions`、`POST /api/sessions` | 列出和创建当前 workspace 的会话 |
| `PATCH` / `DELETE /api/sessions/{id}` | 重命名或删除会话 |
| `GET /api/sessions/{id}/messages` | 恢复已存消息历史 |
| `GET /api/skills`、`GET /api/config` | 获取 Skills 与浏览器安全的运行时信息 |
| `GET` / `PUT /api/user/config` | 读取掩码设置、写入加密模型设置 |
| `POST /api/user/config/test` | 不保存地探测提交的模型角色 |
| `POST /api/chat` | 开始 SSE 对话；body 含 `reasoning_effort`（四档，默认 `medium`） |
| `POST /api/chat/abort` | 请求协作式取消当前运行 |
| `GET /api/health` | 健康检查 |

当 `web/dist` 存在时，FastAPI 可以托管构建后的 SPA，并为前端路由回退到 `index.html`。不存在的 `/api/*` 路由仍返回 JSON 404，不会误返回 SPA 的 HTML 外壳。

## 验证

```powershell
python -m pytest

cd web
npm test
npm run build
```

测试使用 fake LLM 和 adapters，不需要 API key、在线模型端点或 Redis。

## 部署与安全边界

若部署到 Ubuntu + nginx + systemd，请从 [`deploy/DEPLOY.md`](deploy/DEPLOY.md) 开始。应先构建 SPA，固定保存 `AGENT_SECRET_KEY`，让后端只监听 loopback，并由 nginx 终止 TLS。

在将项目暴露给可信私有环境之外的用户前，应补充真实认证与授权、TLS、请求大小与速率限制，以及更强的进程/文件系统隔离。尤其需要注意：

- `workspace_id` Cookie 与 `X-Workspace-ID` 只做数据作用域划分，**不构成认证或授权**。
- `AGENT_SECRET_KEY` 用于加密保存的 API key。轮换或丢失它，会使旧的加密 key 无法读取。
- `read_file` 读取的文件和可信 Skill 的内容可能被发送到配置的远程模型端点。应把 secrets 放在 workspace root 外，并在启用前审查 Skills。
- CORS 是浏览器策略，不是访问控制。HTTPS 部署必须设置 `AGENT_COOKIE_SECURE=true`。
