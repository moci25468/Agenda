from .base import TimetableProvider
from .mock import MockProvider
from .zhengfang import ZhengfangProvider
from .httpjson import HttpJsonProvider, PortalProvider


def get_provider(cfg: dict) -> TimetableProvider:
    ptype = cfg.get("provider", {}).get("type", "mock")
    if ptype == "zhengfang":
        return ZhengfangProvider(cfg)
    if ptype == "httpjson":
        return HttpJsonProvider(cfg)
    if ptype == "portal":
        return PortalProvider(cfg)
    return MockProvider()