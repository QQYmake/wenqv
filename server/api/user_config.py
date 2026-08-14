"""Per-user LLM configuration endpoints (masked read, encrypted write, test)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - the dependency is required in production
    AsyncOpenAI = None  # type: ignore[assignment,misc]

from .dependencies import get_services, get_workspace_id
from .services import APIServices


router = APIRouter(prefix="/user/config", tags=["user-config"])


class ProviderConfigBody(BaseModel):
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)


class UserConfigBody(BaseModel):
    main: ProviderConfigBody = Field(default_factory=ProviderConfigBody)
    summary: ProviderConfigBody = Field(default_factory=ProviderConfigBody)


class ModelDiscoveryBody(BaseModel):
    role: Literal["main", "summary"]
    base_url: str = ""
    api_key: str = ""


@router.get("")
async def get_user_config(
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict[str, Any]:
    repo = services.user_config_repo
    if repo is None:
        raise HTTPException(status_code=503, detail="User config storage unavailable")
    return await repo.get_masked(workspace_id)


@router.put("")
async def put_user_config(
    body: UserConfigBody,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict[str, Any]:
    repo = services.user_config_repo
    if repo is None:
        raise HTTPException(status_code=503, detail="User config storage unavailable")
    await repo.upsert(
        workspace_id,
        main=body.main.model_dump(),
        summary=body.summary.model_dump(),
    )
    # Invalidate the resolver cache so the next chat re-reads the new config.
    if services.client_resolver is not None and hasattr(services.client_resolver, "warm"):
        await services.client_resolver.warm(workspace_id)
    return await repo.get_masked(workspace_id)


@router.post("/test")
async def test_user_config(
    body: UserConfigBody,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict[str, Any]:
    """Validate the submitted config by issuing one tiny completion request
    for every role that ends up complete (submitted or previously saved).

    Main and summary are probed independently, so a summary-only config can be
    tested without filling the main model first. The submitted body is used
    as-is (not persisted); a non-empty ``api_key`` overrides any stored key,
    and an empty one falls back to the stored/default key.
    """

    resolver = services.client_resolver
    repo = services.user_config_repo
    if resolver is None or repo is None:
        raise HTTPException(status_code=503, detail="Resolver unavailable")

    # Build a merged config: start from the resolved (user+default) config,
    # then overlay the non-empty submitted fields.
    resolved = await repo.get_resolved(workspace_id, services.config.llm)
    main = _overlay(resolved.main_base_url, resolved.main_api_key, resolved.main_model, body.main)
    summary = _overlay(
        resolved.summary_base_url,
        resolved.summary_api_key,
        resolved.summary_model,
        body.summary,
    )

    probes: dict[str, dict[str, Any]] = {}
    if _complete(main):
        probes["main"] = await _probe(resolver, main)
    if _complete(summary):
        probes["summary"] = await _probe(resolver, summary)
    if not probes:
        raise HTTPException(
            status_code=422,
            detail="请至少填写一个完整的模型配置（base_url、api_key、model）",
        )

    failed = [f"{role}: {probe['detail']}" for role, probe in probes.items() if not probe["ok"]]
    ok = not failed
    notes = []
    if not _complete(main):
        notes.append("主模型未配置，配置完成后才能开始对话")
    if not _complete(summary):
        notes.append("摘要模型未配置（可选，将复用主模型）")
    if ok:
        detail = "连接成功"
    else:
        detail = "；".join(failed)
    if notes:
        detail = f"{detail}（{'；'.join(notes)}）"
    return {"ok": ok, "detail": detail, "roles": probes}


@router.post("/models")
async def list_models(
    body: ModelDiscoveryBody,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict[str, list[str]]:
    """Discover model IDs from the provider configured for one role.

    Discovery deliberately does not require a model value. The resolved
    workspace config supplies the saved/default provider fields, while the
    current request may override the URL and key without persisting anything.
    """

    repo = services.user_config_repo
    if repo is None:
        raise HTTPException(status_code=503, detail="用户配置存储不可用")

    try:
        resolved = await repo.get_resolved(workspace_id, services.config.llm)
        base_url, api_key = _resolved_provider(resolved, body.role)
    except Exception:
        # Do not expose repository/encryption errors or their arguments.
        raise HTTPException(status_code=503, detail="用户配置存储不可用") from None

    base_url = body.base_url.strip() or base_url
    api_key = body.api_key or api_key
    if len(base_url) > 512 or len(api_key) > 512:
        raise HTTPException(status_code=422, detail="模型发现配置无效")
    if not base_url or not api_key:
        raise HTTPException(status_code=422, detail="无法解析完整的 base_url 和 api_key")

    try:
        models = await _discover_models(
            base_url=base_url,
            api_key=api_key,
            timeout=_model_discovery_timeout(services.config, body.role),
        )
    except Exception as exc:
        if _is_timeout_error(exc):
            raise HTTPException(status_code=504, detail="模型列表请求超时") from None
        raise HTTPException(status_code=502, detail="无法从 Provider 获取模型列表") from None
    return {"models": models}


def _complete(role: dict[str, str]) -> bool:
    return bool(role["base_url"] and role["api_key"] and role["model"])


def _resolved_provider(resolved: Any, role: Literal["main", "summary"]) -> tuple[str, str]:
    if role == "main":
        return str(resolved.main_base_url or "").strip(), str(resolved.main_api_key or "")
    return str(resolved.summary_base_url or "").strip(), str(resolved.summary_api_key or "")


async def _discover_models(*, base_url: str, api_key: str, timeout: float) -> list[str]:
    if AsyncOpenAI is None:  # pragma: no cover - dependency installation issue
        raise RuntimeError("OpenAI SDK unavailable")

    client = None
    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        response = await client.models.list()
        return _normalize_model_ids(response)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # A close failure must not turn a successful discovery into a
                # provider error, and must never be sent to the browser.
                pass


def _normalize_model_ids(response: Any) -> list[str]:
    items: Any
    if isinstance(response, Mapping):
        items = response.get("data", [])
    else:
        items = getattr(response, "data", response)
    if isinstance(items, Mapping):
        items = items.get("data", [])
    if not isinstance(items, (list, tuple)):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, Mapping):
            value = item.get("id")
        else:
            value = getattr(item, "id", None)
        model_id = str(value).strip() if value is not None else ""
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append(model_id)
    return result


def _model_discovery_timeout(config: Any, role: Literal["main", "summary"]) -> float:
    try:
        provider = config.llm.for_role(role)
        timeout = float(getattr(provider, "timeout_s", 30.0))
        return max(timeout, 1.0)
    except Exception:
        return 30.0


def _is_timeout_error(exc: BaseException) -> bool:
    timeout_names = {"APITimeoutError", "ConnectTimeout", "ReadTimeout", "TimeoutException"}
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, asyncio.TimeoutError)):
            return True
        if any(cls.__name__ in timeout_names for cls in type(current).__mro__):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _probe(resolver: Any, role: dict[str, str]) -> dict[str, Any]:
    from server.agent.models import ChatMessage

    # Probe each role independently with its own client so a broken summary
    # provider is reported even when main is healthy (and vice versa), and a
    # summary-only config can be tested without configuring the main model.
    try:
        client = resolver.build_client(
            base_url=role["base_url"],
            api_key=role["api_key"],
            model=role["model"],
        )
    except Exception as exc:  # build error (bad config)
        return {"ok": False, "detail": str(exc) or exc.__class__.__name__}
    try:
        response = await client.complete(
            [ChatMessage(role="user", content="ping")], tools=None, max_tokens=1
        )
        return {"ok": True, "detail": (response.content or "")[:80]}
    except Exception as exc:
        return {"ok": False, "detail": str(exc) or exc.__class__.__name__}


def _overlay(base_url: str, api_key: str, model: str, body: ProviderConfigBody) -> dict[str, str]:
    return {
        "base_url": body.base_url.strip() or base_url,
        "api_key": body.api_key or api_key,
        "model": body.model.strip() or model,
    }


__all__ = ["router"]
