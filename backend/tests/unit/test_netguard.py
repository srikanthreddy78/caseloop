from __future__ import annotations

import pytest
from app.netguard import UnsafeUrlError, assert_public_url


def test_public_https_passes() -> None:
    assert_public_url("https://shop.example/api/orders/{order_number}")
    assert_public_url("https://93.184.216.34/track")


@pytest.mark.parametrize(
    "url",
    [
        "http://shop.example/api",  # plain http never leaves the box
        "https://127.0.0.1/",
        "https://10.0.0.1/orders",
        "https://192.168.1.20:8080/",
        "https://172.16.5.5/",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://[::1]/",
        "https://[fd00::1]/",
        "https://[::ffff:10.0.0.1]/",
        "https://0.0.0.0/",
        "https://localhost/",
        "https://api.localhost/",
        "ftp://shop.example/",
        "https:///nohost",
        "https://{order_number}.example/",  # placeholder in the host
    ],
)
def test_private_and_non_https_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url(url)


def test_localhost_over_http_only_when_allowed() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url("http://localhost:8000/orders/{order_number}")
    assert_public_url("http://localhost:8000/orders/{order_number}", allow_localhost=True)
    assert_public_url("https://localhost/orders", allow_localhost=True)
    # The allowance is for localhost only, never for other private space.
    with pytest.raises(UnsafeUrlError):
        assert_public_url("http://10.0.0.1/orders", allow_localhost=True)
    with pytest.raises(UnsafeUrlError):
        assert_public_url("http://127.0.0.1/orders", allow_localhost=True)


def test_hostname_resolving_to_private_space_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.netguard.resolve_host", lambda host: ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(UnsafeUrlError, match="non-public"):
        assert_public_url("https://rebind.example/")


def test_unresolvable_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(host: str) -> list[str]:
        raise OSError("nxdomain")

    monkeypatch.setattr("app.netguard.resolve_host", boom)
    with pytest.raises(UnsafeUrlError, match="does not resolve"):
        assert_public_url("https://nowhere.example/")
