"""Per-user LLM configuration endpoints (masked read, encrypted write, test)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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
    """Validate the submitted config by issuing one tiny completion request.

    The submitted body is used as-is (not persisted). A non-empty ``api_key``
    overrides any stored key; an empty one falls back to the stored/default key.
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
    if not (main["base_url"] and main["api_key"] and main["model"]):
        raise HTTPException(status_code=422, detail="API 配置不完整")

    from server.agent.models import ChatMessage

    # Probe each role independently with its own client so a broken summary
    # provider is reported even when main is healthy (and vice versa).
    results: dict[str, Any] = {}
    for role, fields in (("main", main), ("summary", summary)):
        if not (fields["base_url"] and fields["api_key"] and fields["model"]):
            results[role] = {"ok": False, "detail": "API 配置不完整"}
            continue
        try:
            client = resolver.build_client(
                base_url=fields["base_url"],
                api_key=fields["api_key"],
                model=fields["model"],
            )
        except Exception as exc:  # build error (bad config)
            results[role] = {"ok": False, "detail": str(exc)}
            continue
        try:
            response = await client.complete(
                [ChatMessage(role="user", content="ping")], tools=None, max_tokens=1
            )
            results[role] = {"ok": True, "detail": (response.content or "")[:80]}
        except Exception as exc:
            results[role] = {"ok": False, "detail": str(exc) or exc.__class__.__name__}

    ok = all(result["ok"] for result in results.values())
    detail = "；".join(
        f"{role}: {result['detail']}" for role, result in results.items() if not result["ok"]
    ) or (results.get("main", {}).get("detail") or "ok")
    return {"ok": ok, "detail": detail, "roles": results}


def _overlay(base_url: str, api_key: str, model: str, body: ProviderConfigBody) -> dict[str, str]:
    return {
        "base_url": body.base_url.strip() or base_url,
        "api_key": body.api_key or api_key,
        "model": body.model.strip() or model,
    }


__all__ = ["router"]