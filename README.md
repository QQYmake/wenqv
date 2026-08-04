# Blue Lake Agent

Chinese README: [readme_zh.md](readme_zh.md)

A personal, workspace-isolated Agent chat application with a FastAPI backend, a React/Vite frontend, SQLite persistence, an optional Redis side cache, and file-based Skills.

The project deliberately stays a **single process**: FastAPI owns HTTP/SSE delivery and composes an HTTP-independent Agent Core. SQLite is the source of truth. Redis is optional and only caches dynamic reads for 24 hours.

See the [architecture notes and exported diagram](docs/architecture.md) for the dependency rules, request lifecycle, persistence model, and failure boundaries. The quick Mermaid source is [`docs/architecture.mmd`](docs/architecture.mmd).

## Quick start

Prerequisites: Python 3.11+, Node.js 20.19+ (or 22.12+), and an OpenAI-compatible chat-completions endpoint with streaming tool calls.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cd web
npm install
cd ..
```

Set `AGENT_MAIN_API_KEY` (and, when needed, `AGENT_MAIN_BASE_URL` / `AGENT_MAIN_MODEL`), then start both development servers:

```powershell
# terminal 1, repository root
uvicorn server.main:app --reload --port 8000

# terminal 2
cd web
npm run dev
```

Open <http://127.0.0.1:5173>. The Vite development server proxies `/api` to FastAPI.

For a production-style local run, build the SPA first. FastAPI serves `web/dist` when that directory exists:

```powershell
cd web
npm run build
cd ..
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

## Configuration

Edit [`config.yaml`](config.yaml). `llm.summary` is optional; summary and title tasks automatically reuse `llm.main` when it is absent. Environment variables override secrets and common deployment settings, so API keys do not need to be committed.

Skills are Markdown files under `skills/` with YAML frontmatter:

```markdown
---
name: concise_plan
description: Turn a broad goal into a small executable plan.
---

Your skill instructions go here.
```

A user can load one through `@concise_plan`, the UI selector, or the Agent's `load_skill` meta-tool. Injection is persisted as a marked conversation message and deduplicated per session.

## Verification

```powershell
python -m pytest
cd web
npm test -- --run
npm run build
```

The tests use fake LLM and repository adapters; no API key, Redis server, or network call is required.

The bundled Chinese webfont is LXGW WenKai Lite. Its local license and source notes are kept under `web/public/fonts/`, so the UI does not depend on a font CDN.

## Safety boundary

`read_file` resolves paths inside the configured workspace root and rejects traversal outside it. This is an application-level guard, not an operating-system sandbox. Keep the server private or add authentication and network controls before exposing it to an untrusted network.
