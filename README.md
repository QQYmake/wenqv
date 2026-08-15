# Blue Lake Agent

## Project Overview

一个单进程、workspace 隔离、**浏览器本地隐私模式**的 Agent 对话代码库。前端是 React/Vite，后端是 FastAPI/Uvicorn；核心执行层是与 HTTP、持久化解耦的 Python Agent Runtime，通过 Tool Registry、Markdown Skills 和 OpenAI-compatible LLM adapter 运行。

关键隐私边界：**聊天历史、会话列表、provider API key 全部只存在于浏览器本地**（IndexedDB + Web Crypto AES-GCM），服务端不启动 SQLite、Redis、Fernet、身份 cookie，也不保存任何对话或凭据。每次 `POST /api/chat` 由浏览器携带完整运行时上下文（消息 + 已注入 Skill）与解密后的 provider 配置，服务端为单次请求构建内存态 Runtime，流结束前把权威快照作为 `conversation_state` 事件回传给浏览器原子落盘。

当前默认/root Skill 是 `wenqu`（问渠）。它是 Agent 上下文中的 Markdown 指令包，负责指导模型完成高中数学/英语备课实训与文件状态组织；它不是独立后端服务，也不是 Python workflow engine。运行时应用名称为 “Agent Lake”，package 名为 `blue-lake-agent`。

主要技术：Python 3.11+、FastAPI、OpenAI Python SDK、React 18、TypeScript、Vite、Vitest；文档导出使用 `python-docx` 与 WeasyPrint。

## Architecture

先读 [docs/architecture.mmd](docs/architecture.mmd)。这是面向 Coding Agent 的架构压缩表达、源码导航和事实入口；修改架构相关代码时，应先以它定位模块，再回到对应源码验证。PNG/SVG 仅是同一源图的人工视觉预览： [SVG](docs/architecture.svg) · [PNG](docs/architecture.png)。

![项目架构预览](docs/architecture.png)

阅读图时把握四个边界：

- `web/` 只通过带匿名 `X-Workspace-ID` 头的 HTTP 与 SSE 调用 `/api`；它是对话与凭据的**唯一事实源**，不直接访问 Agent、SQLite 或 workspace 文件。
- `server/api/` 负责匿名 workspace 头校验、Pydantic 传输边界、SSE 传输和请求作用域 Runtime 的编排；`server/agent/` 不导入 FastAPI 或任何 storage 实现。
- `AgentCore` 依赖 ConversationStore、LLMClientProvider 和 WorkspaceResolver 等端口。每个 chat 请求由 `server/request_runtime.py` 构建一次性的内存 store + provider clients + AgentCore，请求结束即释放。
- `wenqu` 和 `wenqu-*` 都是 Skill Runtime 扫描的 Markdown 文件。`wenqu` 为默认/root 上下文；阶段 Skills 由现有 `load_skill` 机制动态注入会话，绝不是独立服务。

## Core Runtime Flow

1. 浏览器在 IndexedDB 中生成/加载匿名 `workspace_id`（UUID），每次请求经 `web/src/api/client.ts` 放入 `X-Workspace-ID` 头；`web/src/storage/local.ts` 拥有会话、消息、runtime context 和加密 provider 配置的读写。
2. `POST /api/chat` 在 `server/api/chat.py` 经 `AuthMiddleware` 校验匿名头，再由 `schemas.py` 校验请求体（`session_id`、`message`、`runtime_context`、`provider_config`、`skills`、`reasoning_effort`，含 4 MiB/5 MiB 体积上限）。`RunCoordinator` 防止同一 `(workspace, session)` 并发执行，随后 `RequestRuntimeFactory.create()` 构建一次性 `RequestRuntime`。
3. `RequestRuntimeFactory` 用浏览器传来的 `runtime_context` 给 `InMemoryConversationStore` 做种子——Skill 注入文本绝不接受浏览器原文，而是按受信 Skill 目录重新渲染——再构建请求作用域的 `RequestProviderClients` 并装配 `AgentCore`。
4. `AgentCore` 注入默认 Skill、UI 选中的 Skill 和消息中的 `@skill` 提及（去重且写入内存会话上下文），保存用户消息，并由 `ContextManager` 在超出预算时生成摘要/截断（summary 角色缺省时回退 main）。
5. 主模型经请求作用域的 `OpenAICompatClient` 以流式 Chat Completions 接收文本、reasoning 和 tool-call delta。`ToolRegistry` 校验并执行工具（超时与结果截断）；`load_skill`/`remove_skill` 变更内存会话上下文，文件工具限定在当前 workspace 内。
6. `AgentCore` 把 assistant/tool 消息写入内存 store 并产生 `AgentEvent`；`chat.py` 编码为 SSE，前端增量更新文本、reasoning、工具轨迹和 Skill 通知。终止 `done` 事件之前，服务端发送 `conversation_state` 事件携带权威快照，浏览器据此原子写回 IndexedDB。首条消息的标题由前端本地生成（`fallbackTitle`）。

`wenqu` 有一个明确的代码级特例：当它作为默认 Skill 注入时，`server/agent/core.py` 追加 `conversation_id` 和 `workspace_data_root: "wenqu/sessions"`。之后的训练路由、阶段切换和落盘由 `SKILL.md` 指令引导模型调用现有文件工具完成；服务端没有对应的 Wenqu Python 状态机。

## Codebase Map

| 区域 | 先看哪里 | 负责内容 / 何时修改 |
| --- | --- | --- |
| 前端壳与对话状态 | `web/src/App.tsx`, `web/src/components/` | 修改会话 UI、流式事件展示、Skill 选择、设置入口或下载交互。 |
| 前端网络层 | `web/src/api/client.ts`, `web/src/api/sse.ts`, `web/src/api/download.ts`, `web/src/types.ts` | 修改 REST/SSE 协议、`X-Workspace-ID` 头、事件解析或浏览器类型。 |
| 浏览器本地持久化 | `web/src/storage/local.ts` | 修改 IndexedDB/Web Crypto 存储：会话、消息、runtime context 与 AES-GCM 加密的 provider 配置；这是对话与凭据的唯一事实源。 |
| 应用组合与配置 | `server/main.py`, `server/config.py`, `server/workspace.py` | 修改依赖装配、环境覆盖、静态 SPA、Skill 目录解析、workspace 路径隔离或启动安全策略（拒绝旧持久化变量）。 |
| 请求作用域运行时 | `server/request_runtime.py` | 修改每次 chat 请求的一次性 `InMemoryConversationStore` + `RequestProviderClients` + `AgentCore` 构建、受信状态播种与释放。 |
| FastAPI API | `server/api/router.py`, `chat.py`, `services.py`, `files.py`, `meta.py`, `provider.py`, `schemas.py`, `middleware/auth.py` | 修改 HTTP 端点、SSE 编码、Agent transport adapter、匿名 workspace 校验、Pydantic 传输边界、导出下载、provider 探测/模型发现。 |
| Agent Core | `server/agent/core.py`, `context.py`, `models.py`, `ports.py`, `memory.py` | 修改 turn loop、取消、上下文压缩、事件模型、端口契约或内存存储适配；保持该目录与 FastAPI/storage 实现解耦。 |
| Tool Runtime | `server/agent/registry.py`, `server/agent/tools/` | 新增工具、修改 JSON Schema/超时/结果截断，或调整 workspace 受限文件操作、导出工具。 |
| Skill Runtime | `server/agent/skills.py`, `server/agent/tools/skill_tools.py`, `skills/` | 修改 Skill 扫描、注入、`load_skill`/`remove_skill` 或 Markdown 指令包。 |
| Wenqu Skills | `skills/wenqu/SKILL.md`; `skills/wenqu-{intake,cocreate,draft,rehearsal,iterate}/SKILL.md`; `skills/wenqu-student/SKILL.md` | `wenqu` 定义 root 路由、运行时所有权和 `wenqu/sessions` 协议；阶段包按需动态加载，`wenqu-student` 仅由 rehearsal 指令在试讲中加载。 |
| LLM adapter | `server/agent/llm.py` | 修改 `OpenAICompatClient` 的流式/非流式请求、vision fallback、reasoning 提取或 summary 回退。 |
| Workspace 与导出 | `server/services/document_exporter.py`, `server/services/exporters/` | 修改 Markdown → md/txt/docx/pdf 转换、opaque file ID、`.agent-exports/` manifest 或下载解析。 |
| 测试、文档和部署 | `tests/unit/`, `docs/`, `deploy/DEPLOY.md` | 为边界行为补测试；架构修改同步更新 `.mmd` 和预览；生产部署细节见部署文档。 |

迁移遗留的旧服务端持久化代码仍保留在仓库但**不再接线**：`server/storage/`（SQLite/cache/agent_adapter/user_configs/encryption/workspace）、`server/llm_resolver.py`、`server/api/sessions.py`、`server/api/user_config.py` 均未被 `server/main.py` 或 `server/api/router.py` 引用。`server/config.py::_reject_legacy_persistence` 会对旧的 `llm`/`storage` 配置段和 `AGENT_SECRET_KEY`、`AGENT_SQLITE_PATH`、`AGENT_COOKIE_SECURE`、`AGENT_MAIN_*`、`REDIS_URL` 等旧环境变量启动即报错，不要沿用它们。

## State / Storage / Configuration

| 状态 | 事实源与位置 | 生命周期 / 边界 |
| --- | --- | --- |
| 对话历史与会话 | 浏览器 IndexedDB `blue-lake-local`（`sessions`/`timelines`/`runtime`），由 `web/src/storage/local.ts` 读写 | 唯一事实源。服务端不保存；每请求由浏览器把 `runtime_context` 交给服务端，流结束前服务端返回 `conversation_state` 快照供浏览器原子写回。 |
| Provider 凭据 | 浏览器 IndexedDB + Web Crypto AES-GCM（`provider_keys`/`provider_config`），`loadProviderConfig`/`saveProviderConfig` | API key 仅在浏览器解密后随请求体发给服务端；服务端进程内存中只存活于单次请求，不落盘、不写日志。summary 为空时回退 main。 |
| workspace 身份 | 浏览器本地生成匿名 UUID，经 `X-Workspace-ID` 请求头发送；`server/api/middleware/auth.py` 校验格式 | 匿名隔离边界，不是公网身份认证。服务端无 cookie、无数据库行。 |
| 运行中状态 | `RunCoordinator`（进程内 active run 表）、`AgentCore._active`、`RequestRuntime` | 均为进程内存且请求作用域，请求结束即释放；生产 `--workers 1` 是硬性要求。 |
| 一般 workspace 文件 | `<workspace.root>/<workspace_id>/`，由 `server/workspace.py::IsolatedWorkspaceResolver` 懒创建 | `read/write/edit/find/grep/ls` 均使用受限路径解析，不能越出当前 workspace。开发默认 `workspace.root: .`，生产通过 `AGENT_WORKSPACE_ROOT` 放到可写数据目录。 |
| 导出文件 | `<workspace>/.agent-exports/<opaque-id>.{md,txt,docx,pdf}` + manifest | `DocumentExporter` 生成 opaque file ID；`GET /api/files/{file_id}` 仅在当前 workspace 内解析。 |
| Wenqu training state | `<workspace>/wenqu/sessions/<training_id>/` | `current.md`、阶段记录和 lesson v1/v2 是训练状态事实源；其 `owner_conversation_id` 关联浏览器会话 ID，删除应用会话不会删除 training 文件。 |
| 服务与安全配置 | `config.yaml` 加 `AGENT_*` 环境变量（仅非 secret 运行参数） | `AGENT_CONFIG` 决定配置文件；`AGENT_HOST/PORT/STATIC_DIR/WORKSPACE_ROOT/SKILLS_ROOT/CORS_ORIGINS/DEFAULT_SKILLS` 等是主要运行参数。旧持久化变量会被拒绝。前端可选 `VITE_API_BASE_URL` 改变 API 基址。 |
| 可信 Skill 目录查找 | `server/main.py:_resolve_skills_directory()` | 依次查找 `workspace.skills_root`（`AGENT_SKILLS_ROOT`）、config 同级 `skills/`、仓库 `skills/`；Skill 文本必须留在可写 workspace 之外。 |

不要混淆两类状态：Wenqu 的 `current.md` 等 training 文件由模型按 Skill 指令通过工具读写，独立于普通 conversation 历史；后者只存在于浏览器 IndexedDB，服务端仅在单次请求内以 `InMemoryConversationStore` 临时持有。只有默认 `wenqu` 注入获得的 `conversation_id`/`workspace_data_root` 把它们关联起来。

## Development / Agent Guide

首次本地准备：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

cd web
npm ci
```

本地开发使用两个终端：

```bash
# terminal 1, repository root
./start.sh

# terminal 2
cd web
npm run dev
```

`start.sh` 显式使用 `.venv`，无需任何服务端 secret 或数据库。前端开发服务器通过 `web/vite.config.ts` 将 `/api` 代理到 `127.0.0.1:8000`。每个浏览器需要在 Settings 中各自填写可用的 main（可选 summary）provider 配置，凭据仅保存在当前浏览器本地。

验证命令：

```bash
# repository root
.venv/bin/python -m pytest

# frontend
cd web
npm test
npm run build
```

生产部署、Nginx、systemd、workspace 外置和 PDF smoke test 见 [deploy/DEPLOY.md](deploy/DEPLOY.md)。

处理架构敏感改动时，顺序是：先读 [docs/architecture.mmd](docs/architecture.mmd)，再检查图中指向的实现与测试，最后更新并验证 `.mmd` 及预览。不要从 PNG/SVG 反推架构；它们只用于辅助查看。
