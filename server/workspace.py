"""Workspace filesystem isolation for server-side tools and exports.

This is deliberately independent from the retired conversation-storage
package: workspaces remain an explicit, separate persistence boundary.
"""

from __future__ import annotations

import re
from pathlib import Path


_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class IsolatedWorkspaceResolver:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def __call__(self, workspace_id: str | None) -> Path:
        if not workspace_id:
            return self.root
        if not _WORKSPACE_ID.fullmatch(workspace_id):
            raise ValueError("invalid_workspace_id")
        child = (self.root / workspace_id).resolve()
        child.relative_to(self.root)
        child.mkdir(parents=True, exist_ok=True)
        return child


__all__ = ["IsolatedWorkspaceResolver"]
