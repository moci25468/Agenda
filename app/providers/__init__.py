from .base import TimetableProvider
from .mock import MockProvider
from .zhengfang import ZhengfangProvider
from .httpjson import HttpJsonProvider, PortalProvider
from ..config import resolve_course_source, zhengfang_configured


def get_provider(cfg: dict, kind: str | None = None) -> TimetableProvider:
    """取课表数据源。kind 省略时按 provider.primary 自动解析（门户优先）。"""
    if kind is None:
        kind = resolve_course_source(cfg)
    if kind == "zhengfang" and zhengfang_configured(cfg):
        return ZhengfangProvider(cfg)
    if kind == "portal":
        return PortalProvider(cfg)
    if kind == "httpjson":
        return HttpJsonProvider(cfg)
    if kind == "zhengfang":
        # 选了正方但未配置：回退 mock，避免同步直接崩
        return MockProvider()
    return MockProvider()
