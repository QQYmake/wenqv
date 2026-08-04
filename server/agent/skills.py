"""Filesystem skill catalogue and context-injection helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import ChatMessage
from .ports import ConversationStore


_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_-]{0,63})\b")


class SkillError(ValueError):
    pass


class SkillNotFoundError(SkillError):
    pass


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    description: str
    content: str
    path: Path

    def public_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True, slots=True)
class SkillInjectionResult:
    name: str
    loaded: bool
    already_loaded: bool = False
    message: ChatMessage | None = None


class SkillManager:
    """Scans Markdown skills and constructs explicitly marked messages."""

    def __init__(self, directory: str | Path, *, scan_on_init: bool = True) -> None:
        self.directory = Path(directory)
        self._skills: dict[str, SkillDefinition] = {}
        if scan_on_init:
            self.scan()

    def scan(self) -> tuple[SkillDefinition, ...]:
        found: dict[str, SkillDefinition] = {}
        if not self.directory.exists():
            self._skills = found
            return ()
        for path in sorted(self.directory.glob("*.md")):
            definition = _parse_skill(path)
            if definition.name in found:
                other = found[definition.name].path
                raise SkillError(
                    f"Duplicate skill name '{definition.name}' in {other} and {path}"
                )
            found[definition.name] = definition
        self._skills = found
        return self.list()

    def list(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills.values())

    def catalog(self) -> list[dict[str, str]]:
        return [skill.public_dict() for skill in self.list()]

    def get(self, name: str) -> SkillDefinition:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise SkillNotFoundError(f"Unknown skill: {name}") from exc

    def extract_mentions(self, text: str) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for match in _MENTION.finditer(text):
            name = match.group(1)
            if name in self._skills and name not in seen:
                seen.add(name)
                result.append(name)
        return tuple(result)

    def render(self, name: str) -> str:
        skill = self.get(name)
        return (
            f'<skill_context name="{skill.name}">\n'
            f"Description: {skill.description}\n\n"
            f"{skill.content.rstrip()}\n"
            "</skill_context>"
        )

    def build_injection(
        self,
        name: str,
        *,
        role: str = "user",
        tool_call_id: str | None = None,
    ) -> ChatMessage:
        self.get(name)
        if role not in ("user", "tool"):
            raise ValueError("Skill injection role must be 'user' or 'tool'")
        return ChatMessage(
            role=role,  # type: ignore[arg-type]
            content=self.render(name),
            tool_call_id=tool_call_id,
            name="load_skill" if role == "tool" else None,
            metadata={"kind": "skill_injection", "skill_name": name},
        )

    async def inject_selected(
        self,
        store: ConversationStore,
        session_id: str,
        names: Iterable[str],
    ) -> tuple[SkillInjectionResult, ...]:
        results: list[SkillInjectionResult] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            message = self.build_injection(name)
            loaded = await store.inject_skill(session_id, name, message)
            results.append(
                SkillInjectionResult(
                    name=name,
                    loaded=loaded,
                    already_loaded=not loaded,
                    message=message if loaded else None,
                )
            )
        return tuple(results)


def _parse_skill(path: Path) -> SkillDefinition:
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"Skill file must start with frontmatter: {path}")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise SkillError(f"Skill frontmatter is not closed: {path}") from exc
    metadata = _parse_frontmatter(lines[1:end], path)
    name = metadata.get("name", "").strip()
    description = metadata.get("description", "").strip()
    if not _SKILL_NAME.fullmatch(name):
        raise SkillError(f"Invalid or missing skill name in {path}")
    if not description:
        raise SkillError(f"Missing skill description in {path}")
    content = "\n".join(lines[end + 1 :]).strip()
    if not content:
        raise SkillError(f"Skill content is empty: {path}")
    return SkillDefinition(name=name, description=description, content=content, path=path)


def _parse_frontmatter(lines: Sequence[str], path: Path) -> dict[str, str]:
    """Parse the scalar frontmatter fields required by this project.

    Keeping this parser deliberately small avoids coupling the core to PyYAML.
    Quoted scalar values and ``description: |`` blocks are supported.
    """

    result: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillError(f"Invalid frontmatter line in {path}: {line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value in ("|", ">"):
            block: list[str] = []
            while index < len(lines) and (
                not lines[index].strip() or lines[index][:1].isspace()
            ):
                block.append(lines[index].strip())
                index += 1
            value = ("\n" if value == "|" else " ").join(block).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result

