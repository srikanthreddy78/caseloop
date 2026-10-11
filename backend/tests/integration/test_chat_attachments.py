"""Widget photo uploads, attachment downloads for widget and business, and the SSE stream."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
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
