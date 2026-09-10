"""
Test that the Router retry loop correctly handles non-retryable errors.

Verifies that:
1. Non-retryable errors (e.g., 400 ContextWindowExceeded) inside the retry loop
   break out immediately instead of being swallowed.
2. original_exception is updated to the latest error, not stuck on the first.
3. Retryable errors (e.g., 429 RateLimitError) still retry normally.

Regression tests for https://github.com/BerriAI/litellm/issues/21343
"""

import json
import struct
import zlib
from collections.abc import Mapping
from typing import Final
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web

import litellm
from litellm import Router
from litellm.router_utils.cooldown_handlers import _async_get_cooldown_deployments

BEDROCK_ACCOUNT_DENIED_MESSAGE: Final = "Access to Anthropic models is not allowed for this account."
BEDROCK_UNSUPPORTED_THINKING_MESSAGE: Final = "thinking.type.enabled is not supported for this model"
HEALTHY_RESPONSE: Final = "healthy-response"


def _bedrock_stream_event(event_type: str, payload: Mapping[str, object]) -> bytes:
    headers: Final = b"".join(
        bytes((len(name),)) + name.encode() + b"\x07" + struct.pack(">H", len(value)) + value.encode()
        for name, value in (
            (":message-type", "event"),
            (":event-type", event_type),
            (":content-type", "application/json"),
        )
    )
    data: Final = json.dumps(payload).encode()
    prelude: Final = struct.pack(">II", len(headers) + len(data) + 16, len(headers))
    message: Final = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + data
    return message + struct.pack(">I", zlib.crc32(message))


def _bedrock_success_stream() -> bytes:
    return b"".join(
        (
            _bedrock_stream_event("messageStart", {"role": "assistant"}),
            _bedrock_stream_event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": HEALTHY_RESPONSE}}),
            _bedrock_stream_event("contentBlockStop", {"contentBlockIndex": 0}),
            _bedrock_stream_event("messageStop", {"stopReason": "end_turn"}),
            _bedrock_stream_event("metadata", {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}),
        )
    )


def _retry_succeeds(account_denied: bool, healthy_peer: bool, num_retries: int) -> bool:
    return account_denied and healthy_peer and num_retries > 0


def _expected_cooldowns(account_denied: bool, healthy_peer: bool, num_retries: int) -> list[str]:
    if not account_denied:
        return []
    if _retry_succeeds(account_denied, healthy_peer, num_retries):
        return ["denied-0", "denied-1"]
    if num_retries == 0:
        return ["denied-0"]
    return ["denied-0", "denied-1", "denied-2"]


def _expected_requests(account_denied: bool, healthy_peer: bool, num_retries: int) -> list[str]:
    if account_denied and num_retries > 0:
        return ["denied-0", "denied-1", "healthy" if healthy_peer else "denied-2"]
    return ["denied-0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["chat_completions", "anthropic_messages"])
@pytest.mark.parametrize("account_denied", [True, False])
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("num_retries,healthy_peer", [(0, True), (2, True), (5, False)])
async def test_bedrock_account_denial_retries_healthy_deployment(
    entrypoint: str, account_denied: bool, stream: bool, num_retries: int, healthy_peer: bool, unused_tcp_port: int
) -> None:
    received: Final = Mock()

    async def respond(request: web.Request) -> web.Response:
        received(request.path)
        if "denied-" in request.path:
            message: Final = BEDROCK_ACCOUNT_DENIED_MESSAGE if account_denied else BEDROCK_UNSUPPORTED_THINKING_MESSAGE
            return web.json_response(
                {"message": message},
                status=400,
            )
        if request.path.endswith("converse-stream"):
            return web.Response(
                body=_bedrock_success_stream(),
                content_type="application/vnd.amazon.eventstream",
            )
        return web.json_response(
            {
                "output": {"message": {"role": "assistant", "content": [{"text": HEALTHY_RESPONSE}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            },
        )

    app: Final = web.Application()
    app.router.add_post("/{path:.*}", respond)
    runner: Final = web.AppRunner(app)
    await runner.setup()
    site: Final = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    bedrock_model_prefix: Final = "bedrock/arn:aws:bedrock:us-east-2:000000000000:application-inference-profile/"
    third_deployment: Final = "healthy" if healthy_peer else "denied-2"
    router: Final = Router(
        model_list=[
            {
                "model_name": "claude-opus-5",
                "litellm_params": {
                    "model": f"{bedrock_model_prefix}{deployment}",
                    "aws_region_name": "us-east-2",
                    "aws_access_key_id": "offline-access-key",
                    "aws_secret_access_key": "offline-secret-key",
                    "api_base": f"http://127.0.0.1:{unused_tcp_port}",
                    "order": order,
                },
                "model_info": {"id": deployment, "base_model": "us.anthropic.claude-opus-5"},
            }
            for order, deployment in enumerate(("denied-0", "denied-1", third_deployment))
        ],
        num_retries=num_retries,
        allowed_fails=100,
        cooldown_time=300,
    )
    try:
        call: Final = router.acompletion if entrypoint == "chat_completions" else router.anthropic_messages
        request: Final = call(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=64,
            stream=stream,
            disable_fallbacks=True,
        )
        if _retry_succeeds(account_denied, healthy_peer, num_retries):
            response: Final = await request
            if stream:
                chunks: Final = [chunk async for chunk in response]
                if entrypoint == "chat_completions":
                    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == HEALTHY_RESPONSE
                else:
                    events: Final = b"".join(chunks)
                    assert events.count(HEALTHY_RESPONSE.encode()) == 1
                    assert events.count(b"event: message_stop") == 1
            elif entrypoint == "chat_completions":
                assert response.choices[0].message.content == HEALTHY_RESPONSE
            else:
                assert response["content"][0]["text"] == HEALTHY_RESPONSE
            assert sorted(await _async_get_cooldown_deployments(router, None)) == _expected_cooldowns(
                account_denied, healthy_peer, num_retries
            )
        else:
            with pytest.raises(
                litellm.BadRequestError,
                match=BEDROCK_ACCOUNT_DENIED_MESSAGE if account_denied else BEDROCK_UNSUPPORTED_THINKING_MESSAGE,
            ):
                await request
            assert sorted(await _async_get_cooldown_deployments(router, None)) == _expected_cooldowns(
                account_denied, healthy_peer, num_retries
            )
        assert [mock_call.args[0].rsplit("/", 2)[1] for mock_call in received.call_args_list] == _expected_requests(
            account_denied, healthy_peer, num_retries
        )
    finally:
        router.reset()
        await runner.cleanup()


def _make_rate_limit_error(message="Rate limited"):
    """Create a RateLimitError for testing."""
    return litellm.RateLimitError(
        message=message,
        llm_provider="bedrock",
        model="anthropic.claude-v2",
    )


def _make_context_window_error(message="prompt is too long: 1205821 tokens > 200000"):
    """Create a ContextWindowExceededError for testing."""
    return litellm.ContextWindowExceededError(
        message=message,
        llm_provider="vertex_ai",
        model="claude-3-opus",
    )


def _make_bad_request_error(message="Invalid request"):
    """Create a BadRequestError for testing."""
    return litellm.BadRequestError(
        message=message,
        llm_provider="openai",
        model="gpt-4",
    )


def _make_not_found_error(message="Model not found"):
    """Create a NotFoundError for testing."""
    return litellm.NotFoundError(
        message=message,
        llm_provider="openai",
        model="gpt-99",
    )


def _create_router(num_retries=2):
    """Create a Router with two deployments for testing."""
    return Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "openai/gpt-4",
                    "api_key": "fake-key-1",
                },
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "openai/gpt-4",
                    "api_key": "fake-key-2",
                },
            },
        ],
        num_retries=num_retries,
    )


def _base_kwargs():
    """Return kwargs required by async_function_with_retries."""
    return {
        "model": "test-model",
        "messages": [{"role": "user", "content": "test"}],
        "original_function": AsyncMock(),
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_non_retryable_error_in_retry_loop_raises_immediately():
    """
    When a non-retryable error (400 ContextWindowExceeded) occurs inside the
    retry loop, the router should raise it immediately instead of swallowing it
    and raising the original error.

    Scenario: First call -> 429, Retry -> 400 (non-retryable)
    Expected: ContextWindowExceededError is raised, NOT RateLimitError
    """
    router = _create_router(num_retries=2)

    rate_limit_error = _make_rate_limit_error()
    context_window_error = _make_context_window_error()

    call_count = 0

    async def mock_make_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise rate_limit_error
        else:
            raise context_window_error

    with (
        patch.object(router, "make_call", side_effect=mock_make_call),
        patch.object(
            router,
            "_async_get_healthy_deployments",
            return_value=(["d1", "d2"], ["d1", "d2"]),
        ),
        patch.object(router, "_time_to_sleep_before_retry", return_value=0),
        patch.object(router, "log_retry", side_effect=lambda kwargs, e: kwargs),
    ):
        with pytest.raises(litellm.ContextWindowExceededError):
            await router.async_function_with_retries(
                num_retries=2,
                **_base_kwargs(),
            )


@pytest.mark.asyncio
async def test_bad_request_error_in_retry_loop_raises_immediately():
    """
    A generic 400 BadRequestError inside the retry loop should also break out
    immediately since 400 is not retryable.
    """
    router = _create_router(num_retries=2)

    rate_limit_error = _make_rate_limit_error()
    bad_request_error = _make_bad_request_error()

    call_count = 0

    async def mock_make_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise rate_limit_error
        else:
            raise bad_request_error

    with (
        patch.object(router, "make_call", side_effect=mock_make_call),
        patch.object(
            router,
            "_async_get_healthy_deployments",
            return_value=(["d1", "d2"], ["d1", "d2"]),
        ),
        patch.object(router, "_time_to_sleep_before_retry", return_value=0),
        patch.object(router, "log_retry", side_effect=lambda kwargs, e: kwargs),
    ):
        with pytest.raises(litellm.BadRequestError):
            await router.async_function_with_retries(
                num_retries=2,
                **_base_kwargs(),
            )


@pytest.mark.asyncio
async def test_original_exception_updated_to_latest_error():
    """
    When all retries are exhausted with retryable errors, the LAST error
    should be raised, not the first one.
    """
    router = _create_router(num_retries=2)

    call_count = 0

    async def mock_make_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_rate_limit_error(f"Rate limit attempt {call_count}")

    with (
        patch.object(router, "make_call", side_effect=mock_make_call),
        patch.object(
            router,
            "_async_get_healthy_deployments",
            return_value=(["d1", "d2"], ["d1", "d2"]),
        ),
        patch.object(router, "_time_to_sleep_before_retry", return_value=0),
        patch.object(router, "log_retry", side_effect=lambda kwargs, e: kwargs),
    ):
        with pytest.raises(litellm.RateLimitError) as exc_info:
            await router.async_function_with_retries(
                num_retries=2,
                **_base_kwargs(),
            )
        # Should be the LAST error, not the first
        assert "Rate limit attempt 3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_retryable_errors_still_retry_normally():
    """
    Retryable errors (429 RateLimitError) should still be retried the
    configured number of times before raising.
    """
    router = _create_router(num_retries=3)

    call_count = 0

    async def mock_make_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_rate_limit_error(f"Rate limit attempt {call_count}")

    with (
        patch.object(router, "make_call", side_effect=mock_make_call),
        patch.object(
            router,
            "_async_get_healthy_deployments",
            return_value=(["d1", "d2"], ["d1", "d2"]),
        ),
        patch.object(router, "_time_to_sleep_before_retry", return_value=0),
        patch.object(router, "log_retry", side_effect=lambda kwargs, e: kwargs),
    ):
        with pytest.raises(litellm.RateLimitError):
            await router.async_function_with_retries(
                num_retries=3,
                **_base_kwargs(),
            )

        # Initial call + 3 retries = 4 total calls
        assert call_count == 4


@pytest.mark.asyncio
async def test_not_found_error_in_retry_loop_raises_immediately():
    """
    A 404 NotFoundError inside the retry loop should break out immediately.
    """
    router = _create_router(num_retries=2)

    rate_limit_error = _make_rate_limit_error()
    not_found_error = _make_not_found_error()

    call_count = 0

    async def mock_make_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise rate_limit_error
        else:
            raise not_found_error

    with (
        patch.object(router, "make_call", side_effect=mock_make_call),
        patch.object(
            router,
            "_async_get_healthy_deployments",
            return_value=(["d1", "d2"], ["d1", "d2"]),
        ),
        patch.object(router, "_time_to_sleep_before_retry", return_value=0),
        patch.object(router, "log_retry", side_effect=lambda kwargs, e: kwargs),
    ):
        with pytest.raises(litellm.NotFoundError):
            await router.async_function_with_retries(
                num_retries=2,
                **_base_kwargs(),
            )

        # Only 2 calls: initial + first retry that hits non-retryable
        assert call_count == 2
