"""HTTP middleware adapters (API-layer concerns)."""

from .auth import AuthMiddleware

__all__ = ["AuthMiddleware"]
