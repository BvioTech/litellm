import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Final
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import litellm
from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy import proxy_server
from litellm.proxy._types import LiteLLMRoutes, LitellmUserRoles, UserAPIKeyAuth, hash_token


@pytest.fixture
def upstream() -> Iterator[tuple[str, Queue, Queue]]:
    requests: Final = Queue()
    responses: Final = Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body: Final = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.put((self.path, dict(self.headers), body))
            status, payload = responses.get(timeout=5)
            encoded: Final = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server: Final = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread: Final = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1", requests, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def gateway(monkeypatch, upstream):
    base, _, _ = upstream
    router: Final = litellm.Router(
        model_list=[
            {
                "model_name": "jev-latest",
                "litellm_params": {
                    "model": "openrouter/~typesafe/jev-latest",
                    "api_key": "test-upstream-key",
                    "api_base": base,
                },
                "model_info": {"mode": "decisions"},
            }
        ],
        num_retries=0,
    )
    monkeypatch.setattr(proxy_server, "llm_router", router)
    monkeypatch.setattr(proxy_server, "master_key", "sk-test-gateway-key")
    monkeypatch.setattr(proxy_server, "general_settings", {})
    monkeypatch.setattr(proxy_server, "user_model", None)
    yield router
    router.discard()


def request_body() -> dict:
    return {
        "model": "jev-latest",
        "state": {"ticket": "Checkout fails", "nested": [None, 1, True]},
        "questions": {
            "is_bug": {
                "type": "noul",
                "instructions": "Is this a defect?",
                "criteria": {"true": "Broken", "false": "Working"},
            },
            "team": {
                "type": "choice",
                "instructions": ["Choose a team"],
                "criteria": {"payments": None, "account": {"description": "Login"}},
            },
            "urgency": {"type": "score", "instructions": {"question": "How urgent?"}, "criteria": ["Low", "High"]},
        },
        "provider": {"order": ["TypeSafe"]},
        "user": "caller",
        "session_id": "decision-session",
        "trace": {"name": "triage"},
    }


@pytest.mark.parametrize("path", ("/decisions", "/v1/decisions", "/alpha/decisions"))
@pytest.mark.asyncio
async def test_decisions_gateway_forwards_native_protocol(gateway, upstream, path: str):
    _, requests, responses = upstream
    payload: Final = {
        "id": "gen-dec-test",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": {
            "is_bug": {"type": "noul", "noul": 0.96},
            "team": {
                "type": "choice",
                "choice": "payments",
                "probabilities": {"payments": 0.8, "account": 0.2},
                "confidence": 0.6,
            },
            "urgency": {"type": "score", "score": 0.8, "legend": {"0": "Low", "1": "High"}},
        },
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }
    responses.put((200, payload))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
    ) as client:
        result: Final = await client.post(
            path, json=request_body(), headers={"Authorization": "Bearer sk-test-gateway-key"}
        )
    assert result.status_code == 200, result.text
    assert result.json() == payload
    upstream_path, headers, body = requests.get_nowait()
    assert upstream_path == "/api/alpha/decisions"
    assert httpx.Headers(headers)["authorization"] == "Bearer test-upstream-key"
    assert body == {**request_body(), "model": "~typesafe/jev-latest"}
    assert path in LiteLLMRoutes.openai_routes.value


@pytest.mark.parametrize("status", (400, 429, 503))
@pytest.mark.asyncio
async def test_decisions_upstream_errors(gateway, upstream, status: int):
    _, requests, responses = upstream
    responses.put((status, {"error": {"code": status, "message": "decision failure"}}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
    ) as client:
        result: Final = await client.post(
            "/v1/decisions", json=request_body(), headers={"Authorization": "Bearer sk-test-gateway-key"}
        )
    assert result.status_code == status, result.text
    assert "decision failure" in result.text
    assert requests.qsize() == 1


@pytest.mark.asyncio
async def test_decisions_requires_gateway_auth(gateway, upstream):
    _, requests, _ = upstream
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
    ) as client:
        result: Final = await client.post("/v1/decisions", json=request_body())
    assert result.status_code in (401, 403), result.text
    assert requests.empty()


@pytest.mark.parametrize("allowed", (True, False))
@pytest.mark.asyncio
async def test_decisions_virtual_key_permissions_and_spend(gateway, upstream, monkeypatch, allowed: bool):
    _, requests, responses = upstream
    events: Final = asyncio.Queue()

    class Recorder(CustomLogger):
        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            if kwargs.get("litellm_params", {}).get("litellm_metadata", {}).get("user_api_key") == hash_token(
                "sk-jev-virtual"
            ):
                await events.put(kwargs)

    monkeypatch.setattr(litellm, "callbacks", [Recorder()])
    cache: Final = DualCache()
    token: Final = hash_token("sk-jev-virtual")
    cache.set_cache(
        token,
        UserAPIKeyAuth(
            token=token,
            api_key=token,
            models=["jev-latest"] if allowed else ["other-model"],
            user_role=LitellmUserRoles.INTERNAL_USER,
            user_id="jev-test-user",
        ),
    )
    monkeypatch.setattr(proxy_server, "user_api_key_cache", cache)
    database: Final = MagicMock()
    database.get_data = AsyncMock(return_value=None)
    database.db.litellm_endusertable.find_many = AsyncMock(return_value=[])
    database.db.litellm_modelaccessgroup.find_many = AsyncMock(return_value=[])
    monkeypatch.setattr(proxy_server, "prisma_client", database)
    responses.put(
        (
            200,
            {
                "answers": {"is_bug": {"type": "noul", "noul": 0.96}},
                "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
            },
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
    ) as client:
        result: Final = await client.post(
            "/v1/decisions", json=request_body(), headers={"Authorization": "Bearer sk-jev-virtual"}
        )
    if not allowed:
        assert result.status_code in (401, 403), result.text
        assert requests.empty()
        return
    assert result.status_code == 200, result.text
    event: Final = await asyncio.wait_for(events.get(), timeout=5)
    assert event["response_cost"] == pytest.approx(0.000019992)
    metadata: Final = event["litellm_params"]["litellm_metadata"]
    assert metadata["user_api_key"] == token
    assert metadata["user_api_key_user_id"] == "jev-test-user"
    assert event["standard_logging_object"]["end_user"] == "caller"


@pytest.mark.asyncio
async def test_decisions_rejects_unconfigured_model(gateway, upstream):
    _, requests, _ = upstream
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
    ) as client:
        result: Final = await client.post(
            "/v1/decisions",
            json={**request_body(), "model": "unknown-model"},
            headers={"Authorization": "Bearer sk-test-gateway-key"},
        )
    assert result.status_code == 400, result.text
    assert requests.empty()


@pytest.mark.parametrize("fallback", (False, True))
@pytest.mark.asyncio
async def test_decisions_never_sends_to_another_provider(upstream, monkeypatch, gateway, fallback: bool):
    base, requests, responses = upstream
    model_list: Final = (
        [
            {
                "model_name": "jev-latest",
                "litellm_params": {"model": "openrouter/typesafe/jev-1.13", "api_key": "test-key", "api_base": base},
            }
        ]
        if fallback
        else []
    ) + [
        {
            "model_name": "wrong-provider" if fallback else "jev-latest",
            "litellm_params": {"model": "hosted_vllm/test-model", "api_key": "wrong-provider-key", "api_base": base},
        }
    ]
    router: Final = litellm.Router(
        model_list=model_list, num_retries=0, fallbacks=[{"jev-latest": ["wrong-provider"]}] if fallback else []
    )
    monkeypatch.setattr(proxy_server, "llm_router", router)
    if fallback:
        responses.put((503, {"error": {"code": 503, "message": "temporarily unavailable"}}))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
        ) as client:
            result: Final = await client.post(
                "/v1/decisions", json=request_body(), headers={"Authorization": "Bearer sk-test-gateway-key"}
            )
        assert result.status_code in (400, 503), result.text
        assert requests.qsize() == (1 if fallback else 0)
        if not fallback:
            assert "requires provider openrouter" in result.text
    finally:
        router.discard()


@pytest.mark.asyncio
async def test_decisions_falls_back_to_another_openrouter_deployment(upstream, monkeypatch, gateway):
    base, requests, responses = upstream
    router: Final = litellm.Router(
        model_list=[
            {
                "model_name": "jev-latest",
                "litellm_params": {
                    "model": "openrouter/~typesafe/jev-latest",
                    "api_key": "primary-key",
                    "api_base": base,
                },
            },
            {
                "model_name": "jev-fallback",
                "litellm_params": {
                    "model": "openrouter/typesafe/jev-1.13",
                    "api_key": "fallback-key",
                    "api_base": base,
                },
            },
        ],
        num_retries=0,
        fallbacks=[{"jev-latest": ["jev-fallback"]}],
    )
    monkeypatch.setattr(proxy_server, "llm_router", router)
    responses.put((503, {"error": {"code": 503, "message": "temporarily unavailable"}}))
    payload: Final = {
        "answers": {"is_bug": {"type": "noul", "noul": 0.96}},
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }
    responses.put((200, payload))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
        ) as client:
            result: Final = await client.post(
                "/v1/decisions", json=request_body(), headers={"Authorization": "Bearer sk-test-gateway-key"}
            )
        assert result.status_code == 200, result.text
        assert result.json() == payload
        first_path, first_headers, first_body = requests.get_nowait()
        last_path, last_headers, last_body = requests.get_nowait()
        assert first_path == last_path == "/api/alpha/decisions"
        assert httpx.Headers(first_headers)["authorization"] == "Bearer primary-key"
        assert httpx.Headers(last_headers)["authorization"] == "Bearer fallback-key"
        assert first_body["model"] == "~typesafe/jev-latest"
        assert last_body["model"] == "typesafe/jev-1.13"
        assert last_body["questions"] == first_body["questions"] == request_body()["questions"]
    finally:
        router.discard()


@pytest.mark.asyncio
async def test_decisions_cooldown_does_not_use_environment_credentials(upstream, monkeypatch, gateway):
    base, requests, _ = upstream
    model: Final = "openrouter/typesafe/jev-1.13"
    router: Final = litellm.Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {"model": model, "api_key": "deployment-key", "api_base": base},
                "model_info": {"id": "jev-cooling"},
            }
        ],
        num_retries=0,
    )
    router.cooldown_cache.add_deployment_to_cooldown(
        model_id="jev-cooling",
        original_exception=Exception("cooling"),
        exception_status=429,
        cooldown_time=60,
    )
    assert router.cooldown_cache.cache.get_cache("deployment:jev-cooling:cooldown") is not None
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-key")
    monkeypatch.setenv("OPENROUTER_API_BASE", base)
    monkeypatch.setattr(proxy_server, "llm_router", router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
        ) as client:
            result: Final = await client.post(
                "/v1/decisions",
                json={**request_body(), "model": model},
                headers={"Authorization": "Bearer sk-test-gateway-key"},
            )
        assert result.status_code != 200, result.text
        assert requests.empty()
    finally:
        router.discard()


@pytest.mark.asyncio
async def test_decisions_alias_can_equal_endpoint_name(upstream, monkeypatch, gateway):
    base, requests, responses = upstream
    router: Final = litellm.Router(
        model_list=[
            {
                "model_name": "decisions",
                "litellm_params": {"model": "openrouter/typesafe/jev-1.13", "api_key": "test-key", "api_base": base},
            }
        ],
        num_retries=0,
    )
    monkeypatch.setattr(proxy_server, "llm_router", router)
    responses.put(
        (
            200,
            {
                "answers": {"is_bug": {"type": "noul", "noul": 0.96}},
                "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
            },
        )
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
        ) as client:
            result: Final = await client.post(
                "/v1/decisions",
                json={**request_body(), "model": "decisions"},
                headers={"Authorization": "Bearer sk-test-gateway-key"},
            )
        assert result.status_code == 200, result.text
        assert requests.get_nowait()[0] == "/api/alpha/decisions"
    finally:
        router.discard()


@pytest.mark.asyncio
async def test_decisions_health_check_uses_native_questions(upstream):
    base, requests, responses = upstream
    responses.put(
        (
            200,
            {
                "answers": {"healthy": {"type": "noul", "noul": 0.99}},
                "usage": {"input_tokens": 30, "output_tokens": 3, "cost": 0.00000126},
            },
        )
    )
    result: Final = await litellm.ahealth_check(
        model_params={"model": "openrouter/typesafe/jev-1.13", "api_key": "test-key", "api_base": base},
        mode="decisions",
    )
    assert "error" not in result, result
    path, _, body = requests.get_nowait()
    assert path == "/api/alpha/decisions"
    assert body["questions"]["healthy"]["type"] == "noul"
    assert "messages" not in body
    assert "max_tokens" not in body


@pytest.mark.parametrize(
    ("extra", "status"),
    (
        ({"stream": True}, 422),
        ({"api_base": "https://untrusted.example"}, 401),
        ({"questions": {}}, 422),
        ({"questions": {"test": {"type": "text", "instructions": "Write a reply"}}}, 422),
    ),
)
@pytest.mark.asyncio
async def test_decisions_rejects_invalid_input(gateway, upstream, extra: dict, status: int):
    _, requests, _ = upstream
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app), base_url="http://gateway"
    ) as client:
        result: Final = await client.post(
            "/v1/decisions", json={**request_body(), **extra}, headers={"Authorization": "Bearer sk-test-gateway-key"}
        )
    assert result.status_code == status, result.text
    assert requests.empty()
