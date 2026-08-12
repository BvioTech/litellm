"""
Diagnostics for the "empty turn" failure mode on `/v1/messages`.

Anthropic clients render only `text` and `tool_use` blocks. A stream that ends
carrying just a `thinking` block therefore shows up as a blank reply with no
error on either side — Bedrock Converse returns a reasoning signature with no
plaintext, so a turn whose answer was blocked by a guardrail or never started
(reasoning consumed the whole budget) is indistinguishable from success.

Two upstream-absent behaviours are covered here:

* `content_filter` maps to Anthropic's own `refusal` rather than falling into
  the `end_turn` fallback, so clients can tell a blocked turn from a completed
  one and stop retrying a request that can never succeed.
* A stream with no client-visible content block logs exactly one warning
  carrying the upstream `stop_reason` and `output_tokens`, which is the only way
  to tell a refusal apart from reasoning that ate the whole budget.
"""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (
    AnthropicStreamWrapper,
)
from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter,
)
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

_LOGGER_PATH = (
    "litellm.llms.anthropic.experimental_pass_through.adapters"
    ".streaming_iterator.verbose_logger"
)


def _collect_async(wrapper: AnthropicStreamWrapper) -> str:
    async def _run() -> str:
        out = []
        async for raw in wrapper.async_anthropic_sse_wrapper():
            out.append(raw.decode() if isinstance(raw, bytes) else raw)
        return "".join(out)

    return asyncio.run(_run())


def _stream_of(*chunks: ModelResponseStream) -> "AsyncIterator[ModelResponseStream]":
    async def _aiter() -> "AsyncIterator[ModelResponseStream]":
        for chunk in chunks:
            yield chunk

    return _aiter()


def _signature_only_chunk(signature: str = "sig-x") -> ModelResponseStream:
    """Bedrock Converse shape: reasoning arrives as a signature with no plaintext."""
    return ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(
                    reasoning_content="",
                    thinking_blocks=[
                        {"type": "thinking", "thinking": "", "signature": signature}
                    ],
                ),
                finish_reason=None,
            )
        ],
    )


def _finish_chunk(
    finish_reason: str = "stop", completion_tokens: int = 1427
) -> ModelResponseStream:
    return ModelResponseStream(
        choices=[StreamingChoices(index=0, delta=Delta(), finish_reason=finish_reason)],
        usage=Usage(
            prompt_tokens=5,
            completion_tokens=completion_tokens,
            total_tokens=5 + completion_tokens,
        ),
    )


def _text_chunk(text: str) -> ModelResponseStream:
    return ModelResponseStream(
        choices=[StreamingChoices(index=0, delta=Delta(content=text), finish_reason=None)],
    )


def _warnings_about_empty_turn(mock_logger) -> list:
    return [
        call
        for call in mock_logger.warning.call_args_list
        if "no client-visible" in str(call)
    ]


@pytest.mark.parametrize(
    "openai_finish_reason, expected",
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("content_filter", "refusal"),
        ("some_future_reason", "end_turn"),
    ],
)
def test_finish_reason_mapping(openai_finish_reason: str, expected: str) -> None:
    """`content_filter` must map to `refusal`, not the `end_turn` fallback.

    Reporting a guardrail-blocked turn as `end_turn` makes it indistinguishable
    from a turn the model finished naturally once the content is empty: clients
    retry a request that can never succeed (Claude Code injects
    "[no visible output]" and burns a second call), and provider health counters
    record the refusal as a success.
    """
    adapter = LiteLLMAnthropicMessagesAdapter()
    assert (
        adapter._translate_openai_finish_reason_to_anthropic(openai_finish_reason)
        == expected
    )


def test_no_visible_output_warns_with_upstream_context() -> None:
    """A stream ending with only a thinking block must leave a diagnosable trace."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_stream_of(_signature_only_chunk(), _finish_chunk()),
        model="claude-opus-5",
    )
    with patch(_LOGGER_PATH) as mock_logger:
        _collect_async(wrapper)

    warnings = _warnings_about_empty_turn(mock_logger)
    assert len(warnings) == 1, "expected exactly one empty-turn warning"
    assert wrapper.emitted_visible_delta is False

    # The upstream context must be carried so an operator can tell a refusal
    # apart from reasoning that consumed the whole budget.
    args = warnings[0].args
    assert "claude-opus-5" in args
    assert 1427 in args


def test_refusal_stream_warns_and_reports_refusal() -> None:
    """A guardrail-blocked turn surfaces both `refusal` and the warning."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_stream_of(
            _signature_only_chunk(), _finish_chunk(finish_reason="content_filter")
        ),
        model="claude-opus-5",
    )
    with patch(_LOGGER_PATH) as mock_logger:
        sse = _collect_async(wrapper)

    assert '"refusal"' in sse
    assert len(_warnings_about_empty_turn(mock_logger)) == 1


def test_visible_output_does_not_warn() -> None:
    """A normal stream carrying text must not emit the empty-turn warning."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_stream_of(_text_chunk("Hello."), _finish_chunk()),
        model="claude-opus-5",
    )
    with patch(_LOGGER_PATH) as mock_logger:
        sse = _collect_async(wrapper)

    assert "Hello." in sse
    assert _warnings_about_empty_turn(mock_logger) == []
    assert wrapper.emitted_visible_delta is True


def test_warning_fires_once_per_stream() -> None:
    """Repeated `message_delta` flush paths must not multiply the warning."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_stream_of(
            _signature_only_chunk(), _signature_only_chunk("sig-y"), _finish_chunk()
        ),
        model="claude-opus-5",
    )
    with patch(_LOGGER_PATH) as mock_logger:
        _collect_async(wrapper)

    assert len(_warnings_about_empty_turn(mock_logger)) == 1
    assert wrapper.warned_no_visible_output is True
