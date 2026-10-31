"""Messages HTTP contracts with in-memory backend transports (no NPU required)."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from examples.disaggregated_prefill_v1 import load_balance_proxy_server_example as proxy

KV_PARAMS = {
    "do_remote_prefill": True,
    "do_remote_decode": False,
    "remote_engine_id": "prefill-engine",
    "remote_request_id": "prefill-request",
    "remote_block_ids": [1, 2],
    "remote_host": "prefill",
    "remote_port": 5000,
}
MESSAGE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "你好"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 5, "output_tokens": 2},
}


class BackendStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


@asynccontextmanager
async def messages_client(monkeypatch, *, prefill=None, decode=None, chunks=None, p_status=200, d_status=200):
    scheduler = proxy.SharedProxyScheduler([("prefill", 8001)], [("decode", 8002)])
    runtime = proxy.WorkerRuntime(scheduler)
    monkeypatch.setattr(proxy, "runtime", runtime)
    monkeypatch.setattr(proxy, "global_args", argparse.Namespace(max_retries=1, retry_delay=0))
    captured = []

    async def backend(request):
        captured.append((request, json.loads(request.content)))
        if request.url.host == "prefill":
            body = {**MESSAGE, "kv_transfer_params": KV_PARAMS} if prefill is None else prefill
            return httpx.Response(p_status, json=body)
        if chunks is not None:
            return httpx.Response(d_status, stream=BackendStream(chunks))
        return httpx.Response(d_status, json=MESSAGE if decode is None else decode)

    for role, host, port in [(proxy.ServerRole.PREFILL, "prefill", 8001), (proxy.ServerRole.DECODE, "decode", 8002)]:
        runtime._clients[role][proxy.server_key(host, port)] = httpx.AsyncClient(
            base_url=f"http://{host}:{port}/v1", transport=httpx.MockTransport(backend)
        )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy") as client:
            yield client, captured, scheduler
    finally:
        await runtime.close()


def assert_load_released(scheduler):
    assert scheduler.request_num == 0
    assert all(server.active_tokens == 0 for server in scheduler.decoders.values())
    assert all(server.active_kv_cache == 0 for server in scheduler.prefillers.values())


def request_body(stream=False):
    return {
        "model": "test-model",
        "system": "Be helpful.",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}],
        "max_tokens": 32,
        "stream": stream,
        "tools": [{"name": "weather", "description": "Weather", "input_schema": {"type": "object"}}],
    }


@pytest.mark.parametrize("with_usage", [True, False])
def test_messages_json_pd_and_headers(monkeypatch, with_usage):
    async def run():
        output = dict(MESSAGE)
        if not with_usage:
            output.pop("usage")
        async with messages_client(monkeypatch, decode=output) as (client, captured, scheduler):
            body = request_body()
            headers = {
                "x-api-key": "test-key",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "test-feature",
                "X-Request-Id": "client-request",
            }
            response = await client.post("/v1/messages", json=body, headers=headers)
            assert response.status_code == 200
            assert response.json() == output  # No OpenAI usage fields injected.
            assert [req.url.path for req, _ in captured] == ["/v1/messages", "/v1/messages"]
            p_body, d_body = [payload for _, payload in captured]
            assert p_body["max_tokens"] == 1 and p_body["stream"] is False
            assert "min_tokens" not in p_body
            assert p_body["kv_transfer_params"]["do_remote_decode"] is True
            assert d_body == {**body, "request_id": "client-request", "kv_transfer_params": KV_PARAMS}
            for req, payload in captured:
                for name, value in headers.items():
                    assert req.headers[name] == value
                assert payload["system"] == body["system"]
                assert payload["messages"] == body["messages"]
                assert payload["tools"] == body["tools"]
            assert_load_released(scheduler)

    asyncio.run(run())


def test_messages_transport_failure_is_visible_and_releases_load(monkeypatch):
    async def run():
        chunks = [b'event: ping\ndata: {"type":"ping"}\n\n', httpx.ReadError("decode disconnected")]
        async with messages_client(monkeypatch, chunks=chunks) as (client, captured, scheduler):
            # Starlette may wrap a stream failure in an ExceptionGroup.
            with pytest.raises(Exception, match="decode disconnected|unhandled errors"):
                await client.post("/v1/messages", json=request_body(stream=True))
            assert len(captured) == 2  # Never replay after bytes have been delivered.
            assert_load_released(scheduler)

    asyncio.run(run())


def test_messages_disconnect_releases_load(monkeypatch):
    async def run():
        chunks = [b'event: ping\ndata: {"type":"ping"}\n\n'] * 3
        async with messages_client(monkeypatch, chunks=chunks) as (_, captured, scheduler):
            disconnected = asyncio.Event()
            body_read = False

            async def receive():
                nonlocal body_read
                if not body_read:
                    body_read = True
                    return {"type": "http.request", "body": json.dumps(request_body(stream=True)).encode()}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    disconnected.set()
                    await asyncio.Event().wait()  # Client leaves while the first bytes are being sent.

            scope = {
                "type": "http",
                "asgi": {"spec_version": "2.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/messages",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
            }
            await asyncio.wait_for(proxy.app(scope, receive, send), timeout=1)
            assert len(captured) == 2
            assert_load_released(scheduler)

    asyncio.run(run())


def test_messages_sse_preserves_arbitrary_chunk_boundaries(monkeypatch):
    events = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_test"}}\n\n'
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"tool_1","name":"weather","input":{}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"你好"}}\n\n'
        'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    ).encode()

    async def run():
        # One-byte chunks split both SSE fields and multibyte UTF-8 characters.
        async with messages_client(monkeypatch, chunks=[events[i : i + 1] for i in range(len(events))]) as context:
            client, captured, scheduler = context
            response = await client.post("/v1/messages", json=request_body(stream=True))
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.content == events
            assert len(captured) == 2
            assert_load_released(scheduler)

    asyncio.run(run())


@pytest.mark.parametrize("stage,status", [("prefill", 400), ("prefill", 404), ("decode", 400), ("decode", 503)])
def test_messages_upstream_errors(monkeypatch, stage, status):
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "backend failure"}}

    async def run():
        options = (
            {"prefill": error, "p_status": status} if stage == "prefill" else {"decode": error, "d_status": status}
        )
        async with messages_client(monkeypatch, **options) as (client, captured, scheduler):
            response = await client.post("/v1/messages", json=request_body())
            assert response.status_code == status
            assert response.json() == error
            assert len(captured) == (1 if stage == "prefill" else 2)
            assert_load_released(scheduler)

    asyncio.run(run())


@pytest.mark.parametrize("params", [None, {}])
def test_messages_missing_kv_does_not_run_decode(monkeypatch, params):
    async def run():
        async with messages_client(monkeypatch, prefill={**MESSAGE, "kv_transfer_params": params}) as context:
            client, captured, scheduler = context
            response = await client.post("/v1/messages", json=request_body())
            assert response.status_code == 502
            assert "kv_transfer_params" in response.json()["error"]["message"]
            assert len(captured) == 1
            assert_load_released(scheduler)

    asyncio.run(run())
