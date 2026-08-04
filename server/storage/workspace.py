"""Filesystem layout adapter that isolates each workspace under its own dir.

The agent core owns the workspace isolation *contract* (the
``WorkspaceResolver`` port); this adapter owns the filesystem policy: each
workspace gets a private directory ``<root>/<workspace_id>/`` created lazily on
first access. Returning distinct roots is what makes ``read_file`` and any
other file-touching tool workspace-confined — storage rows are already
filtered by ``workspace_id``; without a per-workspace root every workspace
would share the same files.
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class IsolatedWorkspaceResolver:
    """Resolve a workspace to a private, lazily-created directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def __call__(self, workspace_id: str | None) -> Path:
        if not workspace_id:
            return self.root
        if not _WORKSPACE_ID.fullmatch(workspace_id):
            raise ValueError(f"Invalid workspace_id: {workspace_id!r}")
        child = (self.root / workspace_id).resolve()
        # Defense-in-depth against path traversal even when the id is validated
        # upstream: the resolved path must stay inside the shared root.
        child.relative_to(self.root)
        child.mkdir(parents=True, exist_ok=True)
        return child


__all__ = ["IsolatedWorkspaceResolver"]