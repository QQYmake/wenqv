# Blue Lake Agent

English README: [README.md](README.md)

Blue Lake Agent 是一个面向个人使用的 Agent 聊天应用，支持流式工具调用、按 workspace 划分的会话数据、持久化会话，以及基于文件的 Skills。项目由 FastAPI 后端、React/Vite SPA、SQLite 持久化、尽力而为的旁路缓存和兼容 OpenAI 的模型端点组成。

每位访客都会获得一个稳定的浏览器 Cookie 身份，每个 workspace 都可以在设置页保存自己独立加密的 LLM 配置（main + summary 双角色），无需共享 API key。

前端构建完成后，FastAPI 会同时托管 SPA 和 API，形成一个生产部署单元。开发阶段则有意让 Vite 与 FastAPI 作为两个进程运行。项目面向可信、私有环境；Cookie 身份是数据隔离边界，并非真正的身份认证。

## 功能

- **流式 Agent 执行：** 可中断的 ReAct 循环通过类型化 SSE 事件持续返回模型输出、工具调用、结果、错误和完成状态。
- **有界执行：** 限制总轮次、连续工具失败次数、工具超时和结果大小；上下文压缩失败时会回退到确定性截断。
- **持久化会话：** 支持创建、恢复、重命名和删除会话，并保留消息、执行轨迹、已加载 Skills 和根据首条消息生成的标题。
- **稳定的访客身份：** `GET /api/bootstrap` 签发并复用 HttpOnly 的 `workspace_id` Cookie，刷新页面后已保存的配置依然可见；`AuthMiddleware` 将其注入为 `X-Workspace-ID`，并保护私有 API 路径（无身份返回 401）。
- **每 workspace 加密的 LLM 配置：** 设置页以 Fernet（`AGENT_SECRET_KEY`）加密存储 `main` / `summary` 两个角色的 base_url、api_key、model；保存前可分别测试每个角色的连接。
- **文件式 Skills：** 可以通过 UI 选择器、`@mention`，或 `load_skill` / `remove_skill` 工具加载可信 Markdown 指令。注入记录会持久化，并在每个会话内去重。
- **内置工具：** `calculator`、限制在 workspace 内的 `read_file`、`load_skill` 和 `remove_skill`。
- **Editorial Lakehouse 界面：** 响应式欢迎页与聊天界面、GFM Markdown、代码高亮、可折叠执行轨迹、明暗主题、本地 LXGW WenKai Lite 字体，以及带 reduced-motion fallback 的 Three.js 湖面（懒加载）。
- **可靠存储：** SQLite 是事实数据源。`SideCache` 始终包含内存 TTL 缓存，并可把 Redis 作为尽力而为的主缓存；默认 TTL 为 24 小时。

## 架构

![Blue Lake Agent architecture](docs/architecture.svg)

| 层 | 职责 |
| --- | --- |
| React + Vite SPA | 展示组件、`App.tsx` 运行时状态、REST 请求、基于 `fetch` 的 SSE 解析、主题与湖面视觉 |
| FastAPI 传输与应用服务 | 会话/元数据路由、聊天流、协作式中断、运行协调、身份中间件与依赖访问 |
| 与 HTTP 无关的 Agent Core | ReAct 编排、上下文策略、Skill 语义、工具校验/执行与出站端口 |
| 出站适配器 | `AgentStoreAdapter`、`SQLiteStore`、旁路缓存、OpenAI-compatible clients、Skill 文件和 workspace 文件 |

核心采用**六边形架构边界**：API 路由在会话和元数据操作中直接使用 `SQLiteStore`，而 `AgentCore` 的持久化路径是 `ConversationStore → AgentStoreAdapter → SQLiteStore`。

依赖规则、请求生命周期、持久化、取消和有意保留的限制见[架构说明](docs/architecture.md)。架构图的规范源文件是 [`docs/architecture.mmd`](docs/architecture.mmd)，仓库同时提交了 [SVG](docs/architecture.svg) 与 [PNG](docs/architecture.png) 导出文件。

### 组合、身份与每用户配置

- **懒加载组合根。** `server/main.py` 没有模块级 `app` 绑定；`uvicorn server.main:app` 通过模块级 `__getattr__` 在首次访问时才构建应用。当 `llm.require_user_config=true`（默认配置）时，缺少或非法的 `AGENT_SECRET_KEY` 会在启动时快速失败，而不是在首次加密时才 500。
- **Cookie 身份。** `GET /api/bootstrap` 为每位访客签发 `workspace_id`（UUID，HttpOnly + SameSite=Lax）Cookie——若请求已带合法身份则直接复用，保证刷新、多标签页之间身份稳定。`AuthMiddleware` 从 Cookie（或显式 `X-Workspace-ID` 头）提取身份并注入给下游 workspace 依赖。公开端点（`/api/bootstrap`、`/api/health`、`OPTIONS` 预检、`/api/user/config/test` 以及 SPA 本身）在身份建立前也可访问，其余 API 无身份一律 401。
- **加密的每 workspace LLM 配置。** `/api/user/config` 读取时返回掩码值、写入时用 Fernet（密钥来自 `AGENT_SECRET_KEY` / `.agent_secret_key`）加密存入每 workspace 的 `user_configs` 表。`PUT` 会使 resolver 缓存失效；`POST /api/user/config/test` 对 `main` 与 `summary` 角色各自发一次极小请求独立探测，提交的 key 覆盖已存 key、留空则回退已存/默认值——因此只配 summary 也能单独测试。
- **每 workspace 客户端池。** `LLMResolverAdapter` 把用户配置与 `llm.*` 默认值合并，每次请求先 `warm` 解析一次（一次 DB 读），并按 `(base_url, api_key, model)` 以 LRU 上限池化 `OpenAICompatClient`。Agent Core 只看到同步的 `ClientResolver` 端口。
- **双模型角色。** `main` 负责对话；`summary` 负责摘要、上下文压缩与标题生成，未配置时回退复用 `main`。
- **SPA 回退 + JSON 404。** `SPAStaticFiles` 托管 Vite 产物并为客户端路由回退到 `index.html`，但 `/api/*` 未命中路径一律返回 JSON 404——绝不会返回会让前端解析失败的 HTML 外壳。

## 快速开始

前置要求：

- Windows 10/11（自带启动脚本 `start.bat`）、Python 3.11+、Node.js 20.19+ 或 22.12+
- 一个兼容 OpenAI Chat Completions、支持 streamed tool calls 的端点
- 仅在需要可选共享旁路缓存时准备 Redis

### 1. 安装

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

cd web
npm ci
cd ..
```

### 2. 推荐：一键启动后端（`start.bat`）

Windows 下推荐用 `start.bat` 启动后端，它会：

- 固定使用 `.venv\Scripts\python.exe`，缺少依赖时自动按 `requirements.txt` 安装；
- 已存在 `.agent_secret_key` 则复用，否则自动生成并持久化——用户 API key 重启后依然可读；
- 设置 `AGENT_COOKIE_SECURE=false`（本机 loopback http 下，Secure Cookie 会被更严格的浏览器丢弃）；
- 启动 `uvicorn server.main:app --reload --port 8000`。

```powershell
start.bat
```

### 3. 运行前端（开发模式）

```powershell
cd web
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 会把 `/api` 代理到 8000 端口的 FastAPI。

### 4. 在界面中配置模型

打开设置页，至少填写 **main** 角色（base_url、api_key、model）；**summary** 角色可选，缺省复用 main。点击**测试**——`/api/user/config/test` 会对每个已配置角色独立探测——然后**保存**。API key 在磁盘上是加密存储的；刷新页面后配置依然存在。

### 手动启动后端（`start.bat` 之外的选择）

```powershell
# 生成一次合法的 Fernet key，或自行导出 AGENT_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

$env:AGENT_SECRET_KEY = "粘贴上面生成的 32 字节 urlsafe base64 key"
uvicorn server.main:app --reload --port 8000
```

> 使用随仓库提供的 `config.yaml`（`llm.require_user_config=true`）时，缺失或非法的 `AGENT_SECRET_KEY` 会让服务在启动时故意快速失败。

### 接近生产环境的本地运行

先构建 SPA。只要 `web/dist` 存在，FastAPI 就会直接托管它：

```powershell
cd web
npm run build
cd ..
uvicorn server.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。在完成[安全与部署](#安全与部署)列出的控制措施之前，请保持 loopback 地址绑定。

## 配置

[`config.yaml`](config.yaml) 提供默认值，环境变量会覆盖 YAML 配置。相对数据库、静态资源和 workspace 路径都基于所选配置文件的位置解析。

| 范围 | 环境变量覆盖项 |
| --- | --- |
| 配置文件 | `AGENT_CONFIG` |
| 密钥 / 身份 | `AGENT_SECRET_KEY`（加密用户 API key 的 Fernet 密钥）、`AGENT_REQUIRE_USER_CONFIG`、`AGENT_COOKIE_SECURE` |
| 主模型 / 摘要模型 | 使用 `AGENT_MAIN_` 或 `AGENT_SUMMARY_` 前缀，再接 `API_KEY`、`BASE_URL`、`MODEL`、`MAX_TOKENS`、`TIMEOUT_S` 或 `TEMPERATURE` |
| Agent / context | `AGENT_MAX_TURNS`、`AGENT_MAX_TOOL_RETRIES`、`AGENT_TOOL_TIMEOUT_S`、`AGENT_TOOL_RESULT_MAX_CHARS`、`AGENT_TOKEN_BUDGET`、`AGENT_SUMMARY_TRIGGER_RATIO`、`AGENT_PRESERVE_RECENT_MESSAGES` |
| 存储 / 缓存 | `AGENT_SQLITE_PATH`、`REDIS_URL`、`AGENT_CACHE_TTL_S` |
| 服务端 | `AGENT_HOST`、`AGENT_PORT`、`AGENT_CORS_ORIGINS`、`AGENT_STATIC_DIR` |
| Workspace | `AGENT_WORKSPACE_ID`、`AGENT_WORKSPACE_NAME`、`AGENT_WORKSPACE_ROOT` |
| 前端 API origin | `VITE_API_BASE_URL` |

`llm.summary` 是可选配置。没有单独的 summary provider 时，摘要、上下文压缩和标题任务会复用 `llm.main`。当 `llm.require_user_config` 为 true 时，`llm.main` 块只是兜底：每位访客需要在设置页配置自己的 API key。

## Skills

Skills 是 `<workspace.root>/skills` 下的 Markdown 文件。使用默认配置时，对应仓库中的 [`skills/`](skills/) 目录。

```markdown
---
name: concise_plan
description: Turn a broad goal into a small executable plan.
---

Your trusted Skill instructions go here.
```

Skill 可以通过 `@concise_plan`、UI 选择器或 Agent 的 `load_skill` meta-tool 加载。Skill 文件属于模型指令，在开放给 Agent 之前应视为可信输入并完成审查。

## 项目结构

```text
server/
  agent/       # 与框架无关的 Agent 策略、端口、上下文、工具和 Skills
    tools/     # calculator、read_file、load_skill / remove_skill
  api/         # FastAPI 路由、SSE framing、适配器与运行协调
    middleware/auth.py   # Cookie 身份 -> X-Workspace-ID，401 守卫
    user_config.py       # 掩码读取 / 加密写入 / 按角色连接测试
  storage/     # SQLite repository、user_configs（Fernet）、旁路缓存、workspace 隔离
  config.py    # YAML 加载与显式环境变量覆盖
  llm_resolver.py       # 合并用户配置 + 默认值的每 workspace 客户端池
  main.py      # 懒加载组合根与生产 SPA 托管
web/src/
  api/         # REST client 与增量 SSE parser
  components/  # chat、Markdown、trace、sidebar、welcome、composer、settings
  scene/       # 装饰性的 Three.js 湖面（懒加载）
  theme/       # Editorial Lakehouse 视觉系统
skills/        # 可信 Markdown Skill registry
tests/         # 单元 + 集成测试（fake LLM / repo 适配器）
docs/          # 架构源文件、导出图与设计说明
start.bat      # Windows 一键开发启动脚本（后端）
```

## 验证

```powershell
python -m pytest

cd web
npm test
npm run build
```

自动化测试使用 fake LLM 和 repository adapters，不需要 API key、Redis server 或网络访问。

## 安全与部署

- 项目没有真正的身份认证、授权、TLS 或 rate limiting。CORS 是浏览器策略，不是访问控制。`workspace_id` Cookie 是数据隔离边界（每个 workspace 拥有独立的会话、文件与加密配置），而不是安全边界。
- `AGENT_SECRET_KEY` 用于加密用户 API key。它永远不会被提交；`start.bat` 生成的 `.agent_secret_key` 已被 git 忽略。丢失密钥将导致已存 key 无法解密。
- `X-Workspace-ID` 只限定数据库操作范围，不构成身份边界。当前 resolver 会把所有 workspace ID 映射到同一个配置的文件系统根目录。
- `read_file` 会拒绝 `AGENT_WORKSPACE_ROOT` 之外的路径并限制文件大小，但这只是应用层防护，不是操作系统 sandbox。
- 工具读取的文件内容可能被发送到配置的远程模型。请把 secrets 放在 workspace root 之外。
- Skills 可以改变模型行为，必须作为可信输入处理。
- 任何公共部署都应先增加受保护的 reverse proxy、TLS、真正的身份认证、请求大小/速率限制，以及更强的进程与文件系统隔离。生产环境请设置 `AGENT_COOKIE_SECURE=true`（Cookie 仅在 HTTPS 下发送）。

内置字体文件及其 license/source 说明仍位于 [`web/public/fonts/`](web/public/fonts/)；UI 不依赖字体 CDN。
