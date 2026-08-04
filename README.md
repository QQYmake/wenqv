# Blue Lake Agent

Chinese README: [readme_zh.md](readme_zh.md)

Blue Lake Agent is a personal Agent chat application with streamed tool use, workspace-scoped conversation data, persistent sessions, and file-based Skills. It combines a FastAPI backend, a React/Vite SPA, SQLite persistence, a best-effort side cache, and an OpenAI-compatible model endpoint.

After the frontend is built, FastAPI serves the SPA and API as one production deployment unit. Development intentionally runs Vite and FastAPI as two processes. The project is designed for trusted, private use; it does not include authentication.

## Features

- **Streaming Agent runs:** a cancellable ReAct loop emits typed SSE events for model output, tool calls, results, errors, and completion.
- **Bounded execution:** total turns, consecutive tool failures, tool timeouts, and tool-result sizes are constrained; context compression falls back to deterministic truncation.
- **Persistent sessions:** create, restore, rename, and delete conversations; retain messages, execution traces, loaded Skills, and generated first-message titles.
- **File-based Skills:** load trusted Markdown instructions from the selector, an `@mention`, or the `load_skill` / `remove_skill` tools. Injection is persisted and deduplicated per session.
- **Built-in tools:** `calculator`, workspace-confined `read_file`, `load_skill`, and `remove_skill`.
- **Editorial Lakehouse UI:** responsive welcome and chat surfaces, GFM Markdown, highlighted code, collapsible execution traces, light/dark themes, a local LXGW WenKai Lite font, and a Three.js lake with reduced-motion fallback.
- **Durable storage:** SQLite is authoritative. `SideCache` always has an in-memory TTL cache and may use Redis as a best-effort primary; the default TTL is 24 hours.

## Architecture

![Blue Lake Agent architecture](docs/architecture.svg)

| Layer | Responsibility |
| --- | --- |
| React + Vite SPA | Presentation components, `App.tsx` runtime state, REST requests, `fetch`-based SSE parsing, theme, and lake presentation |
| FastAPI transport and services | Session/meta routes, chat streaming, cooperative abort, run coordination, and dependency access |
| HTTP-independent Agent Core | ReAct orchestration, context policy, Skill semantics, tool validation/execution, and outbound ports |
| Outbound adapters | `AgentStoreAdapter`, `SQLiteStore`, side cache, OpenAI-compatible clients, Skill files, and workspace files |

The core follows a **hexagonal boundary**: API routes use `SQLiteStore` directly for session and metadata operations, while `AgentCore` persists through `ConversationStore → AgentStoreAdapter → SQLiteStore`.

See [the architecture notes](docs/architecture.md) for dependency rules, lifecycle, persistence, cancellation, and deliberate limits. The canonical diagram source is [`docs/architecture.mmd`](docs/architecture.mmd), with committed [SVG](docs/architecture.svg) and [PNG](docs/architecture.png) exports.

## Quick start

Prerequisites:

- Python 3.11+
- Node.js 20.19+ or 22.12+
- An OpenAI-compatible Chat Completions endpoint that supports streamed tool calls
- Redis only if you want the optional shared side cache

### 1. Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

cd web
npm ci
cd ..
```

### 2. Configure the model

Set environment variables in the shell that will start FastAPI:

```powershell
$env:AGENT_MAIN_API_KEY = "replace-with-your-key"

# Optional provider overrides
$env:AGENT_MAIN_BASE_URL = "https://api.openai.com/v1"
$env:AGENT_MAIN_MODEL = "gpt-4.1-mini"
```

[`.env.example`](.env.example) is a reference only. The application does not automatically load a `.env` file. You may instead edit [`config.yaml`](config.yaml), but do not commit secrets.

### 3. Run in development

```powershell
# terminal 1, repository root
uvicorn server.main:app --reload --port 8000

# terminal 2
cd web
npm run dev
```

Open <http://127.0.0.1:5173>. Vite proxies `/api` to FastAPI.

### Production-style local run

Build the SPA first. FastAPI serves `web/dist` when it exists:

```powershell
cd web
npm run build
cd ..
uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Keep the loopback bind unless you have added the deployment controls listed under [Safety and deployment](#safety-and-deployment).

## Configuration

[`config.yaml`](config.yaml) provides defaults. Environment variables override YAML values. Relative database, static, and workspace paths are resolved from the selected configuration file.

| Area | Environment overrides |
| --- | --- |
| Config file | `AGENT_CONFIG` |
| Main / summary provider | Prefix `AGENT_MAIN_` or `AGENT_SUMMARY_`, then `API_KEY`, `BASE_URL`, `MODEL`, `MAX_TOKENS`, `TIMEOUT_S`, or `TEMPERATURE` |
| Agent / context | `AGENT_MAX_TURNS`, `AGENT_MAX_TOOL_RETRIES`, `AGENT_TOOL_TIMEOUT_S`, `AGENT_TOOL_RESULT_MAX_CHARS`, `AGENT_TOKEN_BUDGET`, `AGENT_SUMMARY_TRIGGER_RATIO`, `AGENT_PRESERVE_RECENT_MESSAGES` |
| Storage / cache | `AGENT_SQLITE_PATH`, `REDIS_URL`, `AGENT_CACHE_TTL_S` |
| Server | `AGENT_HOST`, `AGENT_PORT`, `AGENT_CORS_ORIGINS`, `AGENT_STATIC_DIR` |
| Workspace | `AGENT_WORKSPACE_ID`, `AGENT_WORKSPACE_NAME`, `AGENT_WORKSPACE_ROOT` |
| Frontend API origin | `VITE_API_BASE_URL` |

`llm.summary` is optional. Summary, compression, and title tasks reuse `llm.main` when a dedicated summary provider is absent.

## Skills

Skills are Markdown files under `<workspace.root>/skills`. With the default configuration, that is the repository [`skills/`](skills/) directory.

```markdown
---
name: concise_plan
description: Turn a broad goal into a small executable plan.
---

Your trusted Skill instructions go here.
```

A Skill can be loaded through `@concise_plan`, the UI selector, or the Agent's `load_skill` meta-tool. Treat Skill files as trusted model instructions; review them before making them available to the Agent.

## Project structure

```text
server/
  agent/       # framework-independent Agent policy, ports, context, tools, Skills
  api/         # FastAPI routes, SSE framing, adapters, run coordination
  storage/     # SQLite repository, AgentStoreAdapter, memory/Redis side cache
  config.py    # YAML loading and explicit environment overrides
  main.py      # composition root and production SPA hosting
web/src/
  api/         # REST client and incremental SSE parser
  components/  # chat, Markdown, trace, sidebar, welcome, composer
  scene/       # decorative Three.js lake
  theme/       # Editorial Lakehouse visual system
skills/        # trusted Markdown Skill registry
docs/          # architecture source, exports, and design notes
```

## Verification

```powershell
python -m pytest

cd web
npm test
npm run build
```

The automated tests use fake LLM and repository adapters; they do not require an API key, Redis server, or network access.

## Safety and deployment

- There is no authentication, authorization, TLS, or rate limiting. CORS is browser policy, not access control.
- `X-Workspace-ID` scopes database operations; it is not an identity boundary. The current resolver maps workspace IDs to one configured filesystem root.
- `read_file` rejects paths outside `AGENT_WORKSPACE_ROOT` and limits file size, but this is an application guard rather than an operating-system sandbox.
- Tool-read file contents may be sent to the configured remote model. Keep secrets outside the workspace root.
- Skills can change model behavior and must be treated as trusted input.
- Before any public deployment, add a protected reverse proxy, TLS, authentication, request-size/rate limits, and stronger process/filesystem isolation.

The bundled font files and their license/source notes remain under [`web/public/fonts/`](web/public/fonts/); the UI does not depend on a font CDN.
