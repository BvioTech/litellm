import json
from copy import deepcopy
from typing import Final

import httpx
import pytest

import litellm
from litellm.cost_calculator import get_response_cost_from_hidden_params
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.llms.openrouter.decisions.transformation import OpenRouterDecisionsConfig


@pytest.mark.parametrize(
    ("base", "expected"),
    (
        (None, "https://openrouter.ai/api/alpha/decisions"),
        ("https://proxy.example/api/v1/", "https://proxy.example/api/alpha/decisions"),
        ("https://proxy.example/api/alpha", "https://proxy.example/api/alpha/decisions"),
        ("https://proxy.example/custom", "https://proxy.example/custom/decisions"),
    ),
)
def test_decisions_url(base: str | None, expected: str):
    config: Final = OpenRouterDecisionsConfig()
    url, _ = config.get_complete_url(base, "test-upstream-key", "typesafe/jev-1.13", "decisions", None, {})
    assert str(url) == expected


@pytest.mark.parametrize("async_call", (False, True))
@pytest.mark.asyncio
async def test_passthrough_preserves_credentials_and_caller_body(async_call: bool):
    body: Final = {
        "model": "caller-alias",
        "state": {"ticket": "Checkout fails"},
        "questions": {"is_bug": {"type": "noul", "instructions": "Is this a defect?"}},
    }
    original: Final = deepcopy(body)

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://proxy.example/api/alpha/decisions"
        assert request.headers["authorization"] == "Bearer deployment-key"
        assert json.loads(request.content) == {**original, "model": "typesafe/jev-1.13"}
        return httpx.Response(
            200,
            json={
                "answers": {"is_bug": {"type": "noul", "noul": 0.98}},
                "usage": {"input_tokens": 10, "output_tokens": 3, "cost": 0.0001},
            },
        )

    if async_call:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            handler: Final = AsyncHTTPHandler()
            await handler.client.aclose()
            handler.client = client
            response: Final = await litellm.allm_passthrough_route(
                model="openrouter/typesafe/jev-1.13",
                endpoint="decisions",
                method="POST",
                api_key="deployment-key",
                api_base="https://proxy.example/api/v1",
                json=body,
                client=handler,
            )
            assert isinstance(response, httpx.Response)
            assert response.json()["answers"]["is_bug"]["noul"] == 0.98
    else:
        with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
            handler: Final = HTTPHandler()
            handler.client.close()
            handler.client = client
            response: Final = litellm.llm_passthrough_route(
                model="openrouter/typesafe/jev-1.13",
                endpoint="decisions",
                method="POST",
                api_key="deployment-key",
                api_base="https://proxy.example/api/v1",
                json=body,
                client=handler,
            )
            assert isinstance(response, httpx.Response)
            assert response.json()["answers"]["is_bug"]["noul"] == 0.98
    assert body == original


@pytest.mark.parametrize("cost", (0.000019992, 0.0, None))
def test_decisions_logging_preserves_tokens_and_upstream_cost(cost: float | None):
    result: Final = OpenRouterDecisionsConfig().logging_non_streaming_response(
        model="typesafe/jev-1.13",
        custom_llm_provider="openrouter",
        httpx_response=httpx.Response(
            200,
            json={
                "id": "gen-dec-test",
                "answers": {"is_bug": {"type": "noul", "noul": 0.96}},
                "usage": {"input_tokens": 476, "output_tokens": 70, "cost": cost},
            },
        ),
        request_data={},
        logging_obj=Logging(
            model="typesafe/jev-1.13",
            messages=[],
            stream=False,
            call_type="allm_passthrough_route",
            start_time=0,
            litellm_call_id="test",
            function_id="test",
        ),
        endpoint="decisions",
    )
    assert result.usage.prompt_tokens == 476
    assert result.usage.completion_tokens == 70
    assert json.loads(result.choices[0].message.content)["is_bug"]["noul"] == 0.96
    assert get_response_cost_from_hidden_params(result._hidden_params) == cost
