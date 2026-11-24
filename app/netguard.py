"""出站地址校验与固定解析结果的 HTTP 传输层。"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
from urllib.parse import urlsplit

import certifi
import httpcore
import httpx

_STRICT_PRIVATE = os.getenv("CAMPUS_BLOCK_PRIVATE_OUTBOUND") == "1"
_METADATA_HOSTS = {
    "metadata", "metadata.google.internal", "metadata.tencentyun.com",
}
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")


class UnsafeURLError(ValueError):
    pass


def _normalized_host(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UnsafeURLError("地址主机名无效") from None


def _host_blocked(host: str) -> bool:
    if host in _METADATA_HOSTS or host in _LOCAL_HOSTNAMES:
        return True
    return _STRICT_PRIVATE and (host.endswith(".localhost") or host.endswith(_LOCAL_SUFFIXES))


def _ip_blocked(ip) -> bool:
    if getattr(ip, "ipv4_mapped", None) is not None:
        return _ip_blocked(ip.ipv4_mapped)
    if ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified or ip.is_loopback:
        return True
    return _STRICT_PRIVATE and (ip.is_private or ip.is_loopback)


def assert_safe_outbound_url(url: str, *, allow_http: bool = True) -> str:
    url = (url or "").strip()
    if not url:
        raise UnsafeURLError("地址为空")
    try:
        parts = urlsplit(url)
        host = _normalized_host(parts.hostname or "")
        _ = parts.port
    except ValueError:
        raise UnsafeURLError("地址格式无效") from None
    schemes = ("http", "https") if allow_http else ("https",)
    if parts.scheme not in schemes:
        raise UnsafeURLError("仅支持 " + "/".join(schemes) + " 地址")
    if not host:
        raise UnsafeURLError("地址缺少主机名")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("地址不能包含用户名或密码")
    if _host_blocked(host):
        raise UnsafeURLError("不允许访问本机/内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if _ip_blocked(ip):
        raise UnsafeURLError("不允许访问本机/内网地址")
    return url


class _SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self._delegate = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        host = _normalized_host(host)
        if _host_blocked(host):
            raise UnsafeURLError("不允许访问本机/内网地址")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM)
        else:
            if _ip_blocked(literal):
                raise UnsafeURLError("不允许访问本机/内网地址")
            infos = [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET,
                      socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(literal), port))]
        if not infos:
            raise OSError("地址没有可用的解析结果")
        addresses = []
        for family, socktype, proto, _, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if _ip_blocked(ip):
                raise UnsafeURLError("不允许访问本机/内网地址")
            addresses.append((family, socktype, proto, str(ip), sockaddr[1]))
        last_error = None
        for _, _, _, address, resolved_port in addresses:
            try:
                return await self._delegate.connect_tcp(
                    address, resolved_port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options)
            except Exception as exc:
                last_error = exc
        raise last_error

    async def connect_unix_socket(self, *args, **kwargs):
        raise UnsafeURLError("不允许通过 Unix socket 访问出站地址")

    async def sleep(self, seconds):
        await self._delegate.sleep(seconds)


class SafeAsyncHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, limits=None, verify=True):
        context = ssl.create_default_context(cafile=certifi.where())
        if verify is False:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context, network_backend=_SafeNetworkBackend(),
            max_connections=limits.max_connections if limits else 100,
            max_keepalive_connections=limits.max_keepalive_connections if limits else 20,
            keepalive_expiry=limits.keepalive_expiry if limits else 5.0,
        )

    async def handle_async_request(self, request):
        assert_safe_outbound_url(str(request.url))
        core_request = httpcore.Request(
            method=request.method, url=str(request.url), headers=request.headers.raw,
            content=await request.aread(), extensions=request.extensions)
        response = await self._pool.handle_async_request(core_request)
        content = await response.aread()
        extensions = {"http_version": response.extensions.get("http_version", b"HTTP/1.1")}
        return httpx.Response(
            response.status_code, headers=response.headers, content=content,
            extensions=extensions, request=request)

    async def aclose(self):
        await self._pool.aclose()


async def check_outbound_request(request) -> None:
    assert_safe_outbound_url(str(request.url))
