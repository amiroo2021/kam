"""WebTrade2: independent read-only manual trading terminal surface."""

from .app import create_app
from .config import WebTrade2Config
from .service import WebTrade2Service

__all__ = ["WebTrade2Config", "WebTrade2Service", "create_app"]
