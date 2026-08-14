# Blue Lake Agent

## Project Overview

一个单进程、workspace 隔离的 Agent 对话代码库。前端是 React/Vite，后端是 FastAPI/Uvicorn；核心执行层是与 HTTP、SQLite 解耦的 Python Agent Runtime，通过 Tool Registry、Markdown Skills 和 OpenAI-compatible LLM adapter 运行。

当前默认/root Skill 是 `wenqu`。它是 Agent 上下文中的 Markdown 指令包，负责指导模型组织备课训练和文件状态；它不是独立后端服务，也不是 Python workflow engine。

主要技术：Python 3.11+、FastAPI、SQLite、OpenAI Python SDK、React 18、TypeScript、Vite、Vitest；文档导出使用 `python-docx` 与 WeasyPrint。

## Architecture

先读 [docs/architecture.mmd](docs/architecture.mmd)。这是面向 Coding Agent 的架构压缩表达、源码导航和事实入口；修改架构相关代码时，应先以它定位模块，再回到对应源码验证。PNG/SVG 仅是同一源图的人工视觉预览： [SVG](docs/architecture.svg) · [PNG](docs/architecture.png)。

![项目架构预览](docs/architecture.png)

阅读图时把握四个边界：

- `web/` 只通过 cookie 携带凭据的 HTTP 与 SSE 调用 `/api`；它不直接访问 Agent、SQLite 或 workspace 文件。
- `server/api/` 负责身份/workspace 边界、HTTP 校验、SSE 传输和 API 服务编排；`server/agent/` 不导入 FastAPI 或 storage 实现。
- `AgentCore` 依赖 ConversationStore、LLM client resolver 和 workspace resolver 等端口。Tool Runtime 与 Skill Runtime 都在同一 Agent 进程内。
- `wenqu` 和 `wenqu-*` 都是 Skill Runtime 扫描的 Markdown 文件。`wenqu` 为默认/root 上下文；阶段 Skills 由现有 `load_skill` 机制动态注入会话，绝不是独立服务。

## Core Runtime Flow

1. `web/src/App.tsx` 首先调用 `GET /api/bootstrap` 获取/刷新 HttpOnly `workspace_id` cookie；随后通过 `web/src/api/client.ts` 管理 session、设置和 Skill 目录。
2. `POST /api/chat` 在 `server/api/chat.py` 验证 session 与 workspace，预热该 workspace 的 LLM 配置，使用 `RunCoordinator` 防止同一 `(workspace, session)` 并发执行，然后经 `AgentAdapter` 调用 `AgentCore.stream()`。
3. `AgentCore` 注入默认 Skill、UI 选中的 Skill 和消息中的 `@skill` 提及（去重且持久化为会话上下文），保存用户消息，并由 `ContextManager` 在超出预算时生成/持久化上下文摘要。
4. 主模型经 `LLMResolverAdapter` 按 workspace 选出 `OpenAICompatClient`，以流式 Chat Completions 接收文本、reasoning 和 tool-call delta。`ToolRegistry` 校验并执行工具；`load_skill`/`remove_skill` 变更后续会话上下文，文件工具限定在当前 workspace 内。
5. `AgentCore` 持久化 assistant/tool 消息并产生 `AgentEvent`；`chat.py` 编码为 SSE，前端增量更新文本、reasoning、工具轨迹和 Skill 通知。首条消息的标题生成在终止事件送达后尽力执行，并使用 summary model 与 2 秒上限。

`wenqu` 有一个明确的代码级特例：当它作为默认 Skill 注入时，`server/agent/core.py` 追加 `conversation_id` 和 `workspace_data_root: "wenqu/sessions"`。之后的训练路由、阶段切换和落盘由 `SKILL.md` 指令引导模型调用现有文件工具完成；服务端没有对应的 Wenqu Python 状态机。

## Codebase Map

| 区域 | 先看哪里 | 负责内容 / 何时修改 |
| --- | --- | --- |
| 前端壳与对话状态 | `web/src/App.tsx`, `web/src/components/` | 修改会话 UI、流式事件展示、Skill 选择、设置入口或下载交互。 |
| 前端网络层 | `web/src/api/client.ts`, `web/src/api/sse.ts`, `web/src/types.ts` | 修改 REST/SSE 协议、cookie 凭据、事件解析或浏览器类型。 |
| 应用组合与配置 | `server/main.py`, `server/config.py`, `server/llm_resolver.py` | 修改依赖装配、环境覆盖、静态 SPA、LLM 解析/池化或启动安全策略。 |
| FastAPI API | `server/api/chat.py`, `services.py`, `sessions.py`, `files.py`, `meta.py`, `user_config.py`, `middleware/auth.py` | 修改 HTTP 端点、SSE、Agent transport adapter、workspace 身份、会话/文件/配置 API。 |
| Agent Core | `server/agent/core.py`, `context.py`, `models.py`, `ports.py` | 修改 turn loop、取消、上下文压缩、事件模型或端口契约；保持该目录与 FastAPI/storage 实现解耦。 |
| Tool Runtime | `server/agent/registry.py`, `server/agent/tools/` | 新增工具、修改 JSON Schema/超时/结果截断，或调整 workspace 受限文件操作、导出工具。 |
| Skill Runtime | `server/agent/skills.py`, `server/agent/tools/skill_tools.py`, `skills/` | 修改 Skill 扫描、注入、`load_skill`/`remove_skill` 或 Markdown 指令包。 |
| Wenqu Skills | `skills/wenqu/SKILL.md`; `skills/wenqu-{intake,cocreate,draft,rehearsal,iterate,compare}/SKILL.md`; `skills/wenqu-student/SKILL.md` | `wenqu` 定义 root 路由、运行时所有权和 `wenqu/sessions` 协议；阶段包按需动态加载，`wenqu-student` 仅由 rehearsal 指令在试讲中加载。 |
| SQLite 与适配器 | `server/storage/sqlite.py`, `agent_adapter.py`, `user_configs.py`, `cache.py` | 修改持久化 schema、会话消息/Skill 激活、加密 LLM 配置或可选缓存；SQLite 始终是事实源。 |
| Workspace 与导出 | `server/storage/workspace.py`, `server/services/document_exporter.py`, `server/services/exporters/` | 修改 workspace 路径隔离、下载 manifest 或 Markdown → md/txt/docx/pdf 转换。 |
| 测试、文档和部署 | `tests/unit/`, `tests/integration/`, `docs/`, `deploy/DEPLOY.md` | 为边界行为补测试；架构修改同步更新 `.mmd` 和预览；生产部署细节见部署文档。 |

## State / Storage / Configuration

| 状态 | 事实源与位置 | 生命周期 / 边界 |
| --- | --- | --- |
| workspace identity | `workspace_id` HttpOnly cookie；无 cookie 时 `GET /api/bootstrap` 创建；非浏览器客户端可使用受校验的 `X-Workspace-ID` | 一个 workspace 可有多个 session。cookie 在有效时优先于 header；前端 `localStorage` 只保留非敏感 UI 镜像，不是服务端身份事实源。 |
| 会话与 Agent context | SQLite 的 `workspaces`、`sessions`、`messages`、`session_skills` | `sessions` 是代码/API 中的 conversation。消息、tool calls/results、已注入 Skill 和上下文摘要都在 SQLite；删除 session 级联删除这些记录。 |
| LLM 用户配置 | SQLite `user_configs`，由 `UserConfigRepository` 写入 | 每 workspace 一份。API key 以 `AGENT_SECRET_KEY` 对应的 Fernet key 加密，API 只返回掩码；解析后的 main 配置不完整时 chat 返回 412。summary 缺省时回退到 main。 |
| 运行中状态与缓存 | `RunCoordinator`、`AgentCore._active`、LLM resolver cache/client pool、`SideCache` | 都在进程内，重启丢失。Redis 仅是可选 side cache，不能当作持久化或一致性依据。 |
| 一般 workspace 文件 | `<workspace.root>/<workspace_id>/`，由 `IsolatedWorkspaceResolver` 懒创建 | `read/write/edit/find/grep/ls` 均使用受限路径解析；不能越出当前 workspace。开发默认 `workspace.root: .`，生产应通过 `AGENT_WORKSPACE_ROOT` 放到可写数据目录。 |
| 导出文件 | `<workspace>/.agent-exports/` | `DocumentExporter` 保存随机 opaque file ID、文件和 JSON manifest；`GET /api/files/{file_id}` 仅在当前 workspace 内解析。 |
| Wenqu training state | `<workspace>/wenqu/sessions/<training_id>/` | `current.md`、阶段记录和 lesson v1/v2 是训练状态事实源。其 `owner_conversation_id` 使用上面的 session ID；删除应用 session 不会删除 training 文件。 |
| 服务与安全配置 | `config.yaml` 加 `AGENT_*` 环境变量 | `AGENT_CONFIG` 决定配置文件；`AGENT_SQLITE_PATH`、`AGENT_WORKSPACE_ROOT`、`AGENT_STATIC_DIR` 是主要运行时路径。当前 `llm.require_user_config: true` 时缺失或非法 `AGENT_SECRET_KEY` 会在启动时失败；`AGENT_COOKIE_SECURE` 控制身份 cookie。前端可选 `VITE_API_BASE_URL` 改变 API 基址。 |
| 可信 Skill 目录查找 | `server/main.py:_resolve_skills_directory()` | 当前组合代码依次查找 `<config dir>/skills`、`<workspace root>/skills`、仓库 `skills/`。虽然 `AGENT_SKILLS_ROOT` 会被解析到配置对象，当前查找函数并未把它作为输入。 |

不要混淆两类状态：Wenqu 的 `current.md` 等 training 文件由模型按 Skill 指令通过工具读写，独立于普通 conversation persistence；后者由 AgentStoreAdapter 映射到 SQLite。只有默认 `wenqu` 注入获得的 `conversation_id`/`workspace_data_root` 把它们关联起来。

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

`start.sh` 会为本地环境创建或复用 `.agent_secret_key`，并设定 `AGENT_COOKIE_SECURE=false`；前端开发服务器通过 `web/vite.config.ts` 将 `/api` 代理到 `127.0.0.1:8000`。当前默认配置要求每个 workspace 在 Settings 中提供可用的 main LLM 配置。

验证命令：

```bash
# repository root
.venv/bin/python -m pytest

# frontend
cd web
npm test
npm run build
```

生产部署、Nginx、systemd、SQLite/workspace 外置和 PDF smoke test 见 [deploy/DEPLOY.md](deploy/DEPLOY.md)。

处理架构敏感改动时，顺序是：先读 [docs/architecture.mmd](docs/architecture.mmd)，再检查图中指向的实现与测试，最后更新并验证 `.mmd` 及预览。不要从 PNG/SVG 反推架构；它们只用于辅助查看。
