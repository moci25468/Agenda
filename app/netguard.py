"""出站地址安全校验：阻止 SSRF 打到本机 / 内网 / 云元数据地址。

用户可自由配置门户 URL、AI base_url、推送服务器等地址，服务端会主动请求它们。
若不校验，攻击者可借此访问 127.0.0.1、10.x、192.168.x 或 169.254.169.254
（云厂商元数据接口）等内部资源。这里做「协议 + 主机名 + 字面量 IP」三层拦截。
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

# 严格模式：额外封禁 RFC1918 私网与回环。默认放开私有网段，因为本项目把
# 「本地 Ollama / 自建 Bark / 校园内网门户」当作合法用法；但链路本地（169.254）
# 与云元数据主机任何时候都封禁，这是 SSRF 最常被利用的目标。
_STRICT_PRIVATE = os.environ.get("CAMPUS_BLOCK_PRIVATE_OUTBOUND") == "1"

_METADATA_HOSTS = {
    "metadata", "metadata.google.internal", "metadata.tencentyun.com",
}
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")


class UnsafeURLError(ValueError):
    """地址指向本机/内网/非法协议时抛出。"""


def _ip_blocked(ip) -> bool:
    # 链路本地（169.254.x.x / fe80::）、保留、组播、未指定：无任何合法用途，恒封
    if ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    if _STRICT_PRIVATE and (ip.is_private or ip.is_loopback):
        return True
    return False


def assert_safe_outbound_url(url: str, *, allow_http: bool = True) -> str:
    """校验出站地址，安全则原样返回，否则抛 UnsafeURLError。

    allow_http=False 时只允许 https（用于需要保密的凭证类请求）。
    """
    url = (url or "").strip()
    if not url:
        raise UnsafeURLError("地址为空")
    parts = urlsplit(url)
    schemes = ("http", "https") if allow_http else ("https",)
    if parts.scheme not in schemes:
        raise UnsafeURLError("仅支持 " + "/".join(schemes) + " 地址")
    host = (parts.hostname or "").lower().strip("[]")
    if not host:
        raise UnsafeURLError("地址缺少主机名")
    if host in _METADATA_HOSTS:
        raise UnsafeURLError("不允许访问云元数据地址")
    if _STRICT_PRIVATE and (
        host in _LOCAL_HOSTNAMES or host.endswith(_LOCAL_SUFFIXES)
    ):
        raise UnsafeURLError("不允许访问本机/内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url  # 普通域名，放行（DNS 解析到内网的极端场景由部署层限制）
    if _ip_blocked(ip):
        raise UnsafeURLError("不允许访问本机/内网地址")
    return url
