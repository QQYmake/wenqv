"""In-memory adapters useful for tests, local demos, and fallback wiring."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from typing import Sequence

from .models import ChatMessage


class InMemoryConversationStore:
    """A concurrency-safe implementation of the conversation persistence port."""

    def __init__(self) -> None:
        self._messages: dict[str, list[ChatMessage]] = defaultdict(list)
        self._skills: dict[str, set[str]] = defaultdict(set)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def list_messages(self, session_id: str) -> Sequence[ChatMessage]:
        async with self._locks[session_id]:
            return tuple(self._messages[session_id])

    async def add_message(self, session_id: str, message: ChatMessage) -> None:
        async with self._locks[session_id]:
            self._messages[session_id].append(message)

    async def replace_messages(
        self, session_id: str, messages: Sequence[ChatMessage]
    ) -> None:
        async with self._locks[session_id]:
            self._messages[session_id] = list(messages)

    async def list_session_skills(self, session_id: str) -> set[str]:
        async with self._locks[session_id]:
            return set(self._skills[session_id])

    async def inject_skill(
        self, session_id: str, skill_name: str, message: ChatMessage
    ) -> bool:
        async with self._locks[session_id]:
            if skill_name in self._skills[session_id]:
                return False
            self._skills[session_id].add(skill_name)
            self._messages[session_id].append(message)
            return True

    async def remove_session_skill(self, session_id: str, skill_name: str) -> bool:
        async with self._locks[session_id]:
            if skill_name not in self._skills[session_id]:
                return False
            self._skills[session_id].remove(skill_name)
            retained: list[ChatMessage] = []
            for message in self._messages[session_id]:
                if message.metadata.get("kind") != "skill_injection" or message.metadata.get(
                    "skill_name"
                ) != skill_name:
                    retained.append(message)
                    continue
                if message.role == "tool":
                    # Keep a neutral tool response so strict providers still see a
                    # response for the preceding assistant tool call.
                    retained.append(
                        replace(
                            message,
                            content=json.dumps(
                                {"status": "removed", "name": skill_name},
                                ensure_ascii=False,
                            ),
                            metadata={
                                **dict(message.metadata),
                                "kind": "skill_removed",
                                "redacted": True,
                            },
                        )
                    )
            self._messages[session_id] = retained
            return True

