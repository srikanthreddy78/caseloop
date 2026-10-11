from __future__ import annotations

from typing import Any

from httpx import AsyncClient

SESSION = {"email": "pat@customer-mail.co", "message": "Hello from the widget"}


async def _start(client: AsyncClient, key: str, origin: str | None) -> int:
    headers = {"X-Widget-Key": key}
    if origin is not None:
        headers["Origin"] = origin
    resp = await client.post("/v1/chat/sessions", json=SESSION, headers=headers)
    return resp.status_code


async def test_allowed_origins_gate_session_creation(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    key = business["widget_public_key"]
    assert business["allowed_origins"] == []
    # No allow-list: any origin, or none at all, may open a chat.
    assert await _start(auth_client, key, "https://anything.example") == 201

    patched = await auth_client.patch(
        "/v1/businesses/me",
        json={"allowed_origins": ["https://Shop.Example.com/", " https://shop.example.com", ""]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["allowed_origins"] == ["https://shop.example.com"]
    me = await auth_client.get("/v1/businesses/me")
    assert me.json()["data"]["allowed_origins"] == ["https://shop.example.com"]

    assert await _start(auth_client, key, "https://shop.example.com") == 201
    assert await _start(auth_client, key, "HTTPS://SHOP.EXAMPLE.COM") == 201
    assert await _start(auth_client, key, None) == 201  # non-browser callers carry no Origin

    resp = await auth_client.post(
        "/v1/chat/sessions",
        json=SESSION,
        headers={"X-Widget-Key": key, "Origin": "https://evil.example"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == {
        "code": "forbidden",
        "message": "origin not allowed for this widget",
    }

    # Clearing the list opens the widget up again.
    cleared = await auth_client.patch("/v1/businesses/me", json={"allowed_origins": []})
    assert cleared.json()["data"]["allowed_origins"] == []
    assert await _start(auth_client, key, "https://evil.example") == 201


async def test_allowed_origins_must_be_origins(auth_client: AsyncClient) -> None:
    for bad in ["shop.example.com", "https://shop.example.com/widget", "ftp://x.example"]:
        resp = await auth_client.patch("/v1/businesses/me", json={"allowed_origins": [bad]})
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation_error"
    # Other fields are untouched by the settings-backed one.
    resp = await auth_client.patch(
        "/v1/businesses/me",
        json={"allowed_origins": ["http://localhost:5173"], "max_followups": 1},
    )
    data = resp.json()["data"]
    assert data["allowed_origins"] == ["http://localhost:5173"] and data["max_followups"] == 1
