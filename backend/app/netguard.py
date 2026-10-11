"""Keep outbound HTTP (order lookups, webhooks, Slack) pointed at the public internet.

Business-supplied URLs reach the network from our process, so an address inside our own network
(loopback, RFC 1918, link-local, the cloud metadata endpoint) must never be fetched.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

LOCAL_NAMES = ("localhost",)


class UnsafeUrlError(ValueError):
    """The URL is not an https address on the public internet."""


def resolve_host(host: str) -> list[str]:
    """Every address the host currently resolves to. Replaced in tests."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def _is_local_name(host: str) -> bool:
    return host in LOCAL_NAMES or host.endswith(".localhost")


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_url(url: str, *, allow_localhost: bool = False) -> None:
    """Raise UnsafeUrlError unless ``url`` is https to a host that resolves only to public addresses.

    ``allow_localhost`` lets ``http://localhost`` through for development; everything else on a
    private, loopback, link-local, or metadata address is refused in every environment.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").strip().strip(".").lower()
    if not host:
        raise UnsafeUrlError("URL has no host")
    if "{" in host or "}" in host:
        raise UnsafeUrlError("URL host must not contain a placeholder")
    local = _is_local_name(host)
    if parts.scheme == "http":
        if not (allow_localhost and local):
            raise UnsafeUrlError("URL must use https")
    elif parts.scheme != "https":
        raise UnsafeUrlError("URL must use https")
    if local:
        if allow_localhost:
            return
        raise UnsafeUrlError("URL must not point at localhost")
    try:
        literal: str | None = str(ipaddress.ip_address(host))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        try:
            addresses = resolve_host(host)
        except OSError as exc:
            raise UnsafeUrlError(f"URL host {host!r} does not resolve") from exc
        if not addresses:
            raise UnsafeUrlError(f"URL host {host!r} does not resolve")
    for address in addresses:
        if not _is_public(address):
            raise UnsafeUrlError(f"URL host {host!r} resolves to a non-public address")
