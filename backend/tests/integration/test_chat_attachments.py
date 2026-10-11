"""Widget photo uploads, attachment downloads for widget and business, and the SSE stream."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from app.api import chat, deps
from app.container import Services
from app.main import create_app
from httpx import AsyncClient

from tests.integration.test_chat_flow import run_jobs

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


async def start_chat(client: AsyncClient, business: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(
        "/v1/chat/sessions",
        json={"email": "pat@customer-mail.co", "name": "Pat", "message": "Lamp arrived broken"},
        headers={"X-Widget-Key": business["widget_public_key"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_upload_photos_and_download_them(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    sess = await start_chat(auth_client, business)
    token, case_id = sess["token"], sess["case_id"]
    await run_jobs(auth_client)  # agent asks for photos; case now awaiting_customer

    resp = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[
            ("files", ("crack.png", PNG, "image/png")),
            ("files", ("../../receipt one.PDF", PDF, "application/pdf")),
        ],
        data={"caption": "Here is the damage and the receipt"},
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    sent = out["messages"][-1]
    assert sent["direction"] == "inbound"
    assert sent["text"] == "Here is the damage and the receipt"
    assert [a["index"] for a in sent["attachments"]] == [0, 1]
    assert sent["attachments"][0] == {
        "index": 0,
        "filename": "crack.png",
        "content_type": "image/png",
        "size": len(PNG),
    }
    assert sent["attachments"][1]["filename"] == "receipt_one.PDF"
    assert "storage_key" not in sent["attachments"][0]
    assert out["state"] == "gathering"

    # The widget fetches its own thumbnails through the session token.
    img = await auth_client.get(f"/v1/chat/sessions/{token}/attachments/{sent['id']}/0")
    assert img.status_code == 200, img.text
    assert img.headers["content-type"] == "image/png"
    assert img.content == PNG
    pdf = await auth_client.get(f"/v1/chat/sessions/{token}/attachments/{sent['id']}/1")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content == PDF
    missing = await auth_client.get(f"/v1/chat/sessions/{token}/attachments/{sent['id']}/2")
    assert missing.status_code == 404
    wrong_token = await auth_client.get(f"/v1/chat/sessions/nope/attachments/{sent['id']}/0")
    assert wrong_token.status_code == 404

    # The agent runs again and now has the photos it asked for.
    await run_jobs(auth_client)
    case = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
    photos = next(r for r in case["requirements"] if r["key"] == "photos")
    assert photos["status"] == "provided"

    # The business sees them on the timeline, addressed by a case-wide index.
    timeline = (await auth_client.get(f"/v1/cases/{case_id}/timeline")).json()["data"]
    with_files = [t for t in timeline if t["kind"] == "message" and t.get("attachments")]
    assert len(with_files) == 1
    assert with_files[0]["id"] == sent["id"]
    assert [a["index"] for a in with_files[0]["attachments"]] == [0, 1]
    assert all("storage_key" not in a for a in with_files[0]["attachments"])

    dl = await auth_client.get(f"/v1/cases/{case_id}/attachments/0")
    assert dl.status_code == 200 and dl.headers["content-type"] == "image/png"
    assert dl.content == PNG
    dl2 = await auth_client.get(f"/v1/cases/{case_id}/attachments/1")
    assert dl2.status_code == 200 and dl2.content == PDF
    assert (await auth_client.get(f"/v1/cases/{case_id}/attachments/2")).status_code == 404

    # Another business cannot reach them.
    other = await auth_client.post(
        "/dev/businesses",
        json={
            "name": "Other Co",
            "slug": "other",
            "inbound_email": "support@other.caseloop.dev",
            "escalation_email": "ops@other.co",
        },
    )
    assert other.status_code == 201
    foreign = await auth_client.get(
        f"/v1/cases/{case_id}/attachments/0",
        headers={"Authorization": f"Bearer {other.json()['data']['api_key']}"},
    )
    assert foreign.status_code == 404


async def test_upload_without_caption_and_limits(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    sess = await start_chat(auth_client, business)
    token = sess["token"]

    resp = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[("files", (f"photo{i}.png", PNG, "image/png")) for i in range(2)],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["messages"][-1]["text"] == "Sent 2 photos"

    one = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[("files", ("photo.png", PNG, "image/png"))],
        data={"caption": "   "},
    )
    assert one.json()["messages"][-1]["text"] == "Sent a photo"

    too_many = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[("files", (f"p{i}.png", PNG, "image/png")) for i in range(6)],
    )
    assert too_many.status_code == 422
    wrong_type = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
    )
    assert wrong_type.status_code == 415
    too_big = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[("files", ("huge.png", PNG + b"\0" * (8 * 1024 * 1024), "image/png"))],
    )
    assert too_big.status_code == 413
    none = await auth_client.post(f"/v1/chat/sessions/{token}/attachments", data={"caption": "x"})
    assert none.status_code == 422
    gone = await auth_client.post(
        "/v1/chat/sessions/nope/attachments", files=[("files", ("p.png", PNG, "image/png"))]
    )
    assert gone.status_code == 404

    # Rejected uploads leave nothing behind.
    msgs = (await auth_client.get(f"/v1/chat/sessions/{token}/messages")).json()["messages"]
    assert sum(len(m["attachments"]) for m in msgs) == 3


class _QueueStream(httpx.AsyncByteStream):
    def __init__(
        self, queue: asyncio.Queue[bytes | None], task: asyncio.Task[None], gone: asyncio.Event
    ) -> None:
        self.queue, self.task, self.gone = queue, task, gone

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        self.gone.set()
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except TimeoutError:
            self.task.cancel()
            raise AssertionError("stream did not close after the client disconnected") from None


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """Like httpx.ASGITransport, but hands chunks over as they are sent instead of buffering."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        gone = asyncio.Event()
        started: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        body_sent = False
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                started.set_result(message)
            elif message["type"] == "http.response.body":
                await queue.put(message.get("body", b""))
                if not message.get("more_body", False):
                    await queue.put(None)

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                if not started.done():
                    started.set_exception(exc)
                raise
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        start = await started
        return httpx.Response(
            status_code=start["status"],
            headers=start.get("headers", []),
            stream=_QueueStream(queue, task, gone),
            request=request,
        )


async def read_event(lines: AsyncIterator[str], timeout: float = 5) -> tuple[str, Any]:
    """Next SSE event as (name, parsed data); comment lines are skipped."""

    async def _next() -> tuple[str, Any]:
        name, data = "message", ""
        async for line in lines:
            if line.startswith(":"):
                continue
            if line == "":
                if data:
                    return name, json.loads(data)
                continue
            field, _, value = line.partition(":")
            if field == "event":
                name = value.strip()
            elif field == "data":
                data += value.strip()
        raise AssertionError("stream ended before an event arrived")

    return await asyncio.wait_for(_next(), timeout=timeout)


async def test_stream_sends_state_then_new_messages(
    auth_client: AsyncClient, business: dict[str, Any], services: Services
) -> None:
    sess = await start_chat(auth_client, business)
    token = sess["token"]
    app = create_app(services)
    app.state.services = services

    async with (
        AsyncClient(transport=StreamingASGITransport(app), base_url="http://test") as sc,
        sc.stream("GET", f"/v1/chat/sessions/{token}/stream") as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = resp.aiter_lines()
        assert await read_event(lines) == ("state", {"state": "new"})
        name, first = await read_event(lines)
        assert name == "message" and first["direction"] == "inbound"
        assert first["attachments"] == []

        # The agent replies while the stream is open: state change and the reply both arrive.
        await run_jobs(auth_client)
        got: list[tuple[str, Any]] = []
        while len(got) < 2:
            got.append(await read_event(lines))
        assert ("state", {"state": "awaiting_customer"}) in got
        reply = next(d for n, d in got if n == "message")
        assert reply["direction"] == "outbound" and reply["sender"] == "agent"
    # Leaving the block disconnects; _QueueStream.aclose asserts the server loop stopped.

    async with AsyncClient(transport=StreamingASGITransport(app), base_url="http://test") as sc:
        gone = await sc.get("/v1/chat/sessions/nope/stream")
        assert gone.status_code == 404

        # `after` skips what the widget already has.
        async with sc.stream(
            "GET", f"/v1/chat/sessions/{token}/stream", params={"after": first["at"]}
        ) as resp:
            lines = resp.aiter_lines()
            assert await read_event(lines) == ("state", {"state": "awaiting_customer"})
            name, only = await read_event(lines)
            assert name == "message" and only["direction"] == "outbound"


GIF = b"GIF89a" + b"\0" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 20
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\0" * 20
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\0" * 20


async def test_downloads_are_served_inert(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    sess = await start_chat(auth_client, business)
    token, case_id = sess["token"], sess["case_id"]
    resp = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        files=[("files", ("crack.png", PNG, "image/png"))],
    )
    assert resp.status_code == 201, resp.text
    message_id = resp.json()["messages"][-1]["id"]
    for path in (
        f"/v1/chat/sessions/{token}/attachments/{message_id}/0",
        f"/v1/cases/{case_id}/attachments/0",
    ):
        dl = await auth_client.get(path)
        assert dl.status_code == 200, dl.text
        assert dl.headers["x-content-type-options"] == "nosniff"
        assert dl.headers["content-security-policy"] == "sandbox"
        assert dl.headers["cache-control"] == "private, max-age=3600"


async def test_declared_type_must_match_the_bytes(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    token = (await start_chat(auth_client, business))["token"]
    url = f"/v1/chat/sessions/{token}/attachments"

    async def upload(name: str, data: bytes, ctype: str) -> httpx.Response:
        return await auth_client.post(url, files=[("files", (name, data, ctype))])

    for name, data, ctype in [
        ("a.gif", GIF, "image/gif"),
        ("a.jpg", JPEG, "image/jpeg"),
        ("a.webp", WEBP, "image/webp"),
        ("a.pdf", PDF, "application/pdf"),
        ("IMG_1.HEIC", HEIC, "image/heic"),
        ("a.heif", HEIC, "image/heif"),
    ]:
        resp = await upload(name, data, ctype)
        assert resp.status_code == 201, (name, resp.text)

    rejected = [
        ("script.png", b"<script>alert(1)</script>", "image/png"),
        ("gif-as-png.png", GIF, "image/png"),
        ("png-as-pdf.pdf", PNG, "application/pdf"),
        ("html.jpg", b"<!doctype html>", "image/jpeg"),
        ("riff-wave.webp", b"RIFF\x10\x00\x00\x00WAVEfmt ", "image/webp"),
        ("empty.png", b"", "image/png"),
        # HEIC is trusted by the extension the client sent, so that has to be right.
        ("photo.jpg", HEIC, "image/heic"),
        ("photo", HEIC, "image/heif"),
    ]
    for name, data, ctype in rejected:
        resp = await upload(name, data, ctype)
        assert resp.status_code == 415, (name, resp.status_code, resp.text)
        assert resp.json()["error"]["code"] == "unsupported_media_type"
    # A bad file in a batch rejects the batch and leaves nothing in the case.
    mixed = await auth_client.post(
        url,
        files=[("files", ("ok.png", PNG, "image/png")), ("files", ("bad.png", GIF, "image/png"))],
    )
    assert mixed.status_code == 415
    msgs = (await auth_client.get(f"/v1/chat/sessions/{token}/messages")).json()["messages"]
    assert sum(len(m["attachments"]) for m in msgs) == 6


async def test_oversized_content_length_is_refused_before_reading(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    token = (await start_chat(auth_client, business))["token"]
    resp = await auth_client.post(
        f"/v1/chat/sessions/{token}/attachments",
        content=b"not read",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(46 * 1024 * 1024),
            "Origin": "https://shop.example",
        },
    )
    assert resp.status_code == 413, resp.text
    assert resp.json() == {"error": {"code": "too_large", "message": "upload is over 45 MB"}}
    # The widget runs cross-origin, so the early refusal still carries CORS headers.
    assert resp.headers["access-control-allow-origin"] == "*"
    # Only the upload route is guarded; the session token is not even looked up first.
    other = await auth_client.post(
        f"/v1/chat/sessions/{token}/messages",
        json={"text": "hi"},
        headers={"Content-Length": str(46 * 1024 * 1024)},
    )
    assert other.status_code != 413
    nope = await auth_client.post(
        "/v1/chat/sessions/nope/attachments",
        content=b"",
        headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "999999999"},
    )
    assert nope.status_code == 413


async def test_per_case_file_and_byte_caps(
    auth_client: AsyncClient,
    business: dict[str, Any],
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = await start_chat(auth_client, business)
    token, case_id = sess["token"], sess["case_id"]
    url = f"/v1/chat/sessions/{token}/attachments"

    async def upload(name: str, data: bytes = PNG, n: int = 1) -> int:
        resp = await auth_client.post(
            url, files=[("files", (f"{name}{i}.png", data, "image/png")) for i in range(n)]
        )
        return resp.status_code

    monkeypatch.setattr(chat, "MAX_CASE_FILES", 6)
    assert await upload("p", n=5) == 201
    two_more = await auth_client.post(
        url, files=[("files", (f"q{i}.png", PNG, "image/png")) for i in range(2)]
    )
    assert two_more.status_code == 422
    assert "6 files in total; 5 already sent" in two_more.json()["error"]["message"]
    assert await upload("r") == 201
    assert await upload("s") == 422

    # Bytes: the budget is what the case has not used yet, checked while the upload streams in.
    monkeypatch.setattr(chat, "MAX_CASE_FILES", 100)
    big = PNG + b"\0" * 40_000
    # Room for two big files and one more small one, but not a third big one.
    monkeypatch.setattr(chat, "MAX_CASE_BYTES", 6 * len(PNG) + 2 * len(big) + len(PNG) + 10)
    assert await upload("b1", big) == 201
    assert await upload("b2", big) == 201
    third = await auth_client.post(url, files=[("files", ("b3.png", big, "image/png"))])
    assert third.status_code == 413, third.text
    assert "attachment limit" in third.json()["error"]["message"]
    # Rejected bytes are gone again; a small file still fits in what is left.
    assert await upload("t") == 201
    msgs = (await auth_client.get(f"/v1/chat/sessions/{token}/messages")).json()["messages"]
    assert sum(len(m["attachments"]) for m in msgs) == 9
    folder = services.settings.data_dir / "attachments" / case_id
    assert len([p for p in folder.iterdir() if p.is_file()]) == 9


async def _drain(lines: AsyncIterator[str]) -> list[str]:
    return [line async for line in lines]


async def test_stream_closes_after_the_time_cap_and_frees_its_slot(
    auth_client: AsyncClient,
    business: dict[str, Any],
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "STREAM_MAX_SECONDS", 1.5)
    token = (await start_chat(auth_client, business))["token"]
    app = create_app(services)
    app.state.services = services
    async with (
        AsyncClient(transport=StreamingASGITransport(app), base_url="http://test") as sc,
        sc.stream("GET", f"/v1/chat/sessions/{token}/stream") as resp,
    ):
        assert resp.status_code == 200
        lines = resp.aiter_lines()
        # The browser is told how long to wait before reconnecting, before any event.
        assert await asyncio.wait_for(anext(lines), 5) == "retry: 3000"
        assert await read_event(lines) == ("state", {"state": "new"})
        assert deps.stream_slots.count("token", token) == 1
        assert deps.stream_slots.count("ip", "127.0.0.1") == 1
        name, _ = await read_event(lines)
        assert name == "message"
        rest = await asyncio.wait_for(_drain(lines), timeout=6)  # ends on its own, no error
        assert not any(line.startswith("event:") for line in rest)
    assert deps.stream_slots.count("token", token) == 0
    assert deps.stream_slots.count("ip", "127.0.0.1") == 0


async def test_concurrent_streams_are_capped_per_chat_and_per_ip(
    auth_client: AsyncClient,
    business: dict[str, Any],
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = (await start_chat(auth_client, business))["token"]
    app = create_app(services)
    app.state.services = services
    assert deps.stream_slots.caps == {"token": 3, "ip": 20}
    async with AsyncClient(transport=StreamingASGITransport(app), base_url="http://test") as sc:
        async with contextlib.AsyncExitStack() as stack:
            for _ in range(3):
                resp = await stack.enter_async_context(
                    sc.stream("GET", f"/v1/chat/sessions/{token}/stream")
                )
                assert resp.status_code == 200
                await read_event(resp.aiter_lines())
            fourth = await sc.get(f"/v1/chat/sessions/{token}/stream")
            assert fourth.status_code == 429, fourth.text
            assert fourth.headers["retry-after"] == "5"
            assert "too many open streams" in fourth.json()["error"]["message"]
            # Nothing was acquired for the refused one.
            assert deps.stream_slots.count("token", token) == 3
            assert deps.stream_slots.count("ip", "127.0.0.1") == 3
        # All three closed: a new stream is welcome again.
        assert deps.stream_slots.count("token", token) == 0
        async with sc.stream("GET", f"/v1/chat/sessions/{token}/stream") as resp:
            assert resp.status_code == 200
            await read_event(resp.aiter_lines())

        # The per-IP cap counts streams across chats.
        monkeypatch.setitem(deps.stream_slots.caps, "ip", 1)
        other = (
            await sc.post(
                "/v1/chat/sessions",
                json={"email": "sam@customer-mail.co", "message": "Hello"},
                headers={"X-Widget-Key": business["widget_public_key"]},
            )
        ).json()["token"]
        async with sc.stream("GET", f"/v1/chat/sessions/{token}/stream") as resp:
            await read_event(resp.aiter_lines())
            blocked = await sc.get(f"/v1/chat/sessions/{other}/stream")
            assert blocked.status_code == 429
        async with sc.stream("GET", f"/v1/chat/sessions/{other}/stream") as resp:
            assert resp.status_code == 200
