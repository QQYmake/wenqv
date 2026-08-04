# Blue Lake Agent

English README: [README.md](README.md)

Blue Lake Agent 是一个个人使用、按 workspace 隔离的 Agent 聊天应用。它由 FastAPI 后端、React/Vite 前端、SQLite 持久化、可选 Redis 旁路缓存，以及基于文件的 Skills 组成。

项目有意保持为**单进程**：FastAPI 负责 HTTP/SSE 传输，并组装一个不依赖 HTTP 的 Agent Core。SQLite 是事实数据源。Redis 是可选组件，只缓存动态读取结果，TTL 为 24 小时。

架构依赖规则、请求生命周期、持久化模型和失败边界见 [architecture notes and exported diagram](docs/architecture.md)。快速阅读用的 Mermaid 源码见 [`docs/architecture.mmd`](docs/architecture.mmd)。

## 快速开始

前置要求：Python 3.11+、Node.js 20.19+（或 22.12+），以及一个兼容 OpenAI chat-completions 的端点，并支持 streaming tool calls。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cd web
npm install
cd ..
```

设置 `AGENT_MAIN_API_KEY`。必要时也设置 `AGENT_MAIN_BASE_URL` / `AGENT_MAIN_MODEL`。然后启动两个开发服务器：

```powershell
# terminal 1, repository root
uvicorn server.main:app --reload --port 8000

# terminal 2
cd web
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 开发服务器会把 `/api` 代理到 FastAPI。

如果要做更接近生产环境的本地运行，先构建 SPA。只要 `web/dist` 存在，FastAPI 就会直接托管它：

```powershell
cd web
npm run build
cd ..
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

## 配置

编辑 [`config.yaml`](config.yaml)。`llm.summary` 是可选配置；如果缺省，摘要和标题生成任务会自动复用 `llm.main`。环境变量会覆盖 secrets 和常见部署配置，所以 API key 不需要提交到仓库。

Skill 是 `skills/` 目录下带 YAML frontmatter 的 Markdown 文件：

```markdown
---
name: concise_plan
description: Turn a broad goal into a small executable plan.
---

Your skill instructions go here.
```

用户可以通过 `@concise_plan`、UI 选择器，或 Agent 的 `load_skill` meta-tool 加载 Skill。Skill 注入会作为带标记的对话消息持久化，并在同一会话内去重。

## 验证

```powershell
python -m pytest
cd web
npm test -- --run
npm run build
```

测试使用 fake LLM 和 repository adapters；不需要 API key、Redis server 或网络调用。

内置中文网页字体是 LXGW WenKai Lite。它的本地 license 和来源说明保存在 `web/public/fonts/`，所以 UI 不依赖字体 CDN。

## 安全边界

`read_file` 会把路径解析到配置的 workspace root 内，并拒绝访问 root 之外的路径。这是应用层保护，不是操作系统级 sandbox。把服务暴露到不可信网络之前，应保持私有访问，或增加认证、网络控制、TLS、请求大小/速率限制，以及更强的操作系统级文件/工具隔离。
