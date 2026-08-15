"""Ephemeral Provider discovery and connectivity checks."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.agent.models import ChatMessage
from server.request_runtime import RequestProviderClients

from .dependencies import get_workspace_id
from .schemas import ModelDiscoveryRequest, ProviderTestRequest


router = APIRouter(prefix="/provider", tags=["provider"])


@router.post("/test")
async def test_provider(
    body: ProviderTestRequest,
    _: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Probe supplied credentials without saving or logging them."""

    clients = RequestProviderClients(body.provider_config.model_dump())
    roles = ["main"]
    if body.provider_config.summary.model.strip():
        roles.append("summary")
    results: dict[str, dict[str, bool]] = {}
    try:
        for role in roles:
            try:
                await clients.get_client(role).complete(
                    [ChatMessage(role="user", content="ping")], tools=None, max_tokens=1
                )
                results[role] = {"ok": True}
            except Exception:
                results[role] = {"ok": False}
    finally:
        await clients.close()
    return {"ok": all(result["ok"] for result in results.values()), "roles": results}


@router.post("/models")
async def list_models(
    body: ModelDiscoveryRequest,
    _: str = Depends(get_workspace_id),
) -> dict[str, list[str]]:
    """List models from credentials provided in this one request only."""

    client: Any | None = None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=body.base_url, api_key=body.api_key, timeout=30)
        response = await client.models.list()
        return {"models": _model_ids(response)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="provider_request_failed") from None
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass


def _model_ids(response: Any) -> list[str]:
    values: Any = response.get("data", []) if isinstance(response, Mapping) else getattr(response, "data", [])
    result: list[str] = []
    for item in values if isinstance(values, (list, tuple)) else ():
        value = item.get("id") if isinstance(item, Mapping) else getattr(item, "id", None)
        model = str(value).strip() if value is not None else ""
        if model and model not in result:
            result.append(model)
    return result


__all__ = ["router"]
