"""Architecture guard: server.agent must stay framework- and storage-free."""

from __future__ import annotations

from pathlib import Path

import server.agent
import server.agent.ports


def test_agent_module_has_no_storage_or_fastapi_import() -> None:
    agent_dir = Path(server.agent.__file__).parent
    forbidden_substrings = ("server.storage", "fastapi", "starlette", "import server.api")
    offenders: list[str] = []
    for path in sorted(agent_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        # Skip comments lines so explanatory docstrings mentioning the words
        # do not trigger a false positive; only real import lines count.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not (
                stripped.startswith("import ")
                or stripped.startswith("from ")
            ):
                continue
            if stripped.startswith("from .") or stripped.startswith("from server.agent"):
                continue
            for forbidden in forbidden_substrings:
                if forbidden in stripped:
                    offenders.append(f"{path}: {stripped}")
    assert not offenders, f"server.agent imports forbidden modules: {offenders}"


def test_client_resolver_port_is_defined() -> None:
    assert hasattr(server.agent.ports, "ClientResolver")