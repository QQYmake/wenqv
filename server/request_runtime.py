"""Request-scoped Agent runtime construction.

The objects in this module are intentionally short lived: each chat request
gets a fresh in-memory conversation store and provider clients, then exports a
canonical context snapshot for the browser before all references are released.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from server.agent.context import ContextConfig, ContextManager
from server.agent.core import AgentConfig, AgentCore
from server.agent.llm import LLMConfig, OpenAICompatClient
from server.agent.memory import InMemoryConversationStore
from server.agent.models import ChatMessage
from server.agent.registry import ToolRegistry
from server.agent.skills import SkillManager


class RequestProviderClients:
    """Creates no pooled or process-wide provider client.

    ``OpenAICompatClient`` creates its SDK object lazily; this provider owns it
    for exactly one request and clears the small role cache on close.
    """

    def __init__(self, provider_config: Mapping[str, Any]) -> None:
        self._config = {
            "main": _provider_mapping(provider_config.get("main")),
            "summary": _provider_mapping(provider_config.get("summary")),
        }
        if not _complete(self._config["main"]):
            raise ValueError("provider_config_invalid")
        if self._config["summary"] and not _complete(self._config["summary"]):
            raise ValueError("provider_config_invalid")
        self._clients: dict[str, OpenAICompatClient] = {}

    def get_client(self, role: str, workspace_id: str | None = None) -> OpenAICompatClient:
        del workspace_id
        selected = "summary" if role == "summary" and self._config["summary"] else "main"
        client = self._clients.get(selected)
        if client is None:
            client = OpenAICompatClient(LLMConfig.from_mapping(self._config[selected]))
            self._clients[selected] = client
        return client

    async def close(self) -> None:
        clients, self._clients = tuple(self._clients.values()), {}
        self._config = {}
        for client in clients:
            try:
                await client.close()
            except Exception:
                # Closing an HTTP connection must never expose a provider error
                # or prevent the run coordinator from releasing the request.
                pass


@dataclass(slots=True)
class RequestRuntime:
    session_id: str
    store: InMemoryConversationStore
    agent: AgentCore
    clients: RequestProviderClients

    async def snapshot(self) -> dict[str, Any]:
        messages = await self.store.list_messages(self.session_id)
        skills = await self.store.list_session_skills(self.session_id)
        return {
            "messages": [message.to_dict() for message in messages],
            "active_skills": sorted(skills),
        }

    async def close(self) -> None:
        await self.clients.close()


class RequestRuntimeFactory:
    """Builds one isolated AgentCore from trusted server-side assets."""

    def __init__(
        self,
        *,
        skills: SkillManager,
        tools: ToolRegistry,
        agent_config: AgentConfig,
        context_config: ContextConfig,
        workspace_root: str,
        workspace_resolver: Any,
    ) -> None:
        self.skills = skills
        self.tools = tools
        self.agent_config = agent_config
        self.context_config = context_config
        self.workspace_root = workspace_root
        self.workspace_resolver = workspace_resolver

    async def create(
        self,
        *,
        session_id: str,
        runtime_context: Mapping[str, Any],
        provider_config: Mapping[str, Any],
    ) -> RequestRuntime:
        active_skills = _validated_skill_names(
            runtime_context.get("active_skills", ()), self.skills
        )
        selected_messages = _trusted_messages(
            runtime_context.get("messages", ()), active_skills, self.skills
        )
        store = InMemoryConversationStore()
        await store.seed(session_id, selected_messages, active_skills)
        clients = RequestProviderClients(provider_config)
        agent = AgentCore(
            store=store,
            clients=clients,
            skills=self.skills,
            tools=self.tools,
            config=self.agent_config,
            context_manager=ContextManager(clients, self.context_config),
            workspace_root=self.workspace_root,
            workspace_resolver=self.workspace_resolver,
        )
        return RequestRuntime(session_id=session_id, store=store, agent=agent, clients=clients)


def _provider_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    # Explicitly copy only known transport fields. Unknown values never cross
    # from the browser into the provider SDK's extra request body.
    result = {
        "base_url": str(value.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(value.get("api_key") or ""),
        "model": str(value.get("model") or "").strip(),
    }
    if not any(result.values()):
        return {}
    for name in ("max_tokens", "temperature", "timeout_s"):
        if value.get(name) is not None:
            result[name] = value[name]
    return result


def _complete(provider: Mapping[str, Any]) -> bool:
    return bool(provider.get("base_url") and provider.get("api_key") and provider.get("model"))


def _validated_skill_names(value: Any, skills: SkillManager) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("runtime_context_invalid")
    result: list[str] = []
    for raw in value:
        name = str(raw).strip()
        if not name or name in result:
            continue
        try:
            skills.get(name)
        except Exception:
            raise ValueError("skill_invalid") from None
        result.append(name)
    return tuple(result)


def _trusted_messages(
    value: Any,
    active_skills: Sequence[str],
    skills: SkillManager,
) -> list[ChatMessage]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("runtime_context_invalid")
    messages: list[ChatMessage] = []
    injected: set[str] = set()
    active = set(active_skills)
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("runtime_context_invalid")
        metadata = raw.get("metadata")
        kind = metadata.get("kind") if isinstance(metadata, Mapping) else None
        if kind == "skill_injection":
            name = str(metadata.get("skill_name") or "")
            # Never accept instruction text from a browser.  At most it can
            # tell us where a previously trusted injection belongs in the tool
            # protocol; the actual text is freshly rendered from packaged Skill.
            if name in active and name not in injected:
                role = str(raw.get("role") or "user")
                if role not in {"user", "tool"}:
                    role = "user"
                messages.append(
                    skills.build_injection(
                        name,
                        role=role,
                        tool_call_id=(
                            str(raw.get("tool_call_id"))
                            if raw.get("tool_call_id") is not None
                            else None
                        ),
                    )
                )
                injected.add(name)
            continue
        try:
            message = ChatMessage.from_mapping(raw)
        except Exception:
            raise ValueError("runtime_context_invalid") from None
        # The Pydantic transport schema validates this too; retain a defensive
        # role check at the trust boundary before a message reaches the model.
        if message.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("runtime_context_invalid")
        messages.append(message)

    for name in active_skills:
        if name not in injected:
            messages.insert(0, skills.build_injection(name))
    return messages


__all__ = ["RequestProviderClients", "RequestRuntime", "RequestRuntimeFactory"]
