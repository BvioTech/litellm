"""
Diagnostics for the "empty turn" failure mode on `/v1/messages`.

Anthropic clients render only `text` and `tool_use` blocks, and Bedrock Converse
returns reasoning as a signature with no plaintext. A turn blocked by a guardrail
or one whose reasoning ate the whole budget therefore reaches the client as a
blank reply with no error on either side. Two upstream-absent behaviours close
that blind spot:

* `content_filter` maps to Anthropic's own `refusal` instead of falling into the
  `end_turn` fallback, so a blocked turn is distinguishable from a completed one.
* A stream with no client-visible content block logs exactly one warning carrying
  the upstream `stop_reason` and `output_tokens`.
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

_LOGGER_PATH = "litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator.verbose_logger"
# Both are echoed back by the warning, so the assertions can pin them.
_MODEL = "claude-opus-5"
_OUTPUT_TOKENS = 1427


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


def _signature_only_chunk() -> ModelResponseStream:
    """Bedrock Converse shape: reasoning arrives as a signature with no plaintext."""
    delta = Delta(reasoning_content="", thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "sig-x"}])
    return ModelResponseStream(choices=[StreamingChoices(index=0, delta=delta, finish_reason=None)])


def _finish_chunk(finish_reason: str = "stop") -> ModelResponseStream:
    return ModelResponseStream(
        choices=[StreamingChoices(index=0, delta=Delta(), finish_reason=finish_reason)],
        usage=Usage(prompt_tokens=5, completion_tokens=_OUTPUT_TOKENS, total_tokens=5 + _OUTPUT_TOKENS),
    )


def _text_chunk(text: str) -> ModelResponseStream:
    return ModelResponseStream(choices=[StreamingChoices(index=0, delta=Delta(content=text), finish_reason=None)])


def _warnings_about_empty_turn(mock_logger) -> list:
    return [call for call in mock_logger.warning.call_args_list if "no client-visible" in str(call)]


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
    record the refusal as a success. The surrounding cases pin the mappings our
    new branch sits among, so inserting it cannot shadow them or the fallback.
    """
    adapter = LiteLLMAnthropicMessagesAdapter()
    assert adapter._translate_openai_finish_reason_to_anthropic(openai_finish_reason) == expected


def test_no_visible_output_warns_with_upstream_context() -> None:
    """A stream ending with only a thinking block must leave a diagnosable trace."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_stream_of(_signature_only_chunk(), _finish_chunk()), model=_MODEL
    )
    with patch(_LOGGER_PATH) as mock_logger:
        _collect_async(wrapper)

    warnings = _warnings_about_empty_turn(mock_logger)
    assert len(warnings) == 1, "expected exactly one empty-turn warning"
    assert wrapper.emitted_visible_delta is False

    # The upstream context must be carried so an operator can tell a refusal
    # apart from reasoning that consumed the whole budget.
    assert _MODEL in warnings[0].args
    assert _OUTPUT_TOKENS in warnings[0].args


def test_refusal_stream_warns_and_reports_refusal() -> None:
    """A guardrail-blocked turn surfaces both `refusal` and the warning."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_stream_of(_signature_only_chunk(), _finish_chunk("content_filter")), model=_MODEL
    )
    with patch(_LOGGER_PATH) as mock_logger:
        sse = _collect_async(wrapper)

    assert '"refusal"' in sse
    assert len(_warnings_about_empty_turn(mock_logger)) == 1


def test_visible_output_does_not_warn() -> None:
    """A normal stream carrying text must not emit the empty-turn warning."""
    wrapper = AnthropicStreamWrapper(completion_stream=_stream_of(_text_chunk("Hello."), _finish_chunk()), model=_MODEL)
    with patch(_LOGGER_PATH) as mock_logger:
        sse = _collect_async(wrapper)

    assert "Hello." in sse
    assert _warnings_about_empty_turn(mock_logger) == []
    assert wrapper.emitted_visible_delta is True


def test_warning_fires_once_per_stream() -> None:
    """The `warned_no_visible_output` guard is what keeps the warning single.

    `_augment_message_delta_usage` is reachable from several flush paths
    (hold-and-merge, end-of-stream drain, the `StopIteration` handler). Driving it
    twice is the only way to reach the guard: a stream hitting two flush paths must
    still log one line, or the warning stops being a per-request signal.
    """
    wrapper = AnthropicStreamWrapper(completion_stream=_stream_of(), model=_MODEL)
    message_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": _OUTPUT_TOKENS},
    }
    with patch(_LOGGER_PATH) as mock_logger:
        wrapper._augment_message_delta_usage(message_delta)
        wrapper._augment_message_delta_usage(message_delta)

    assert len(_warnings_about_empty_turn(mock_logger)) == 1
    assert wrapper.warned_no_visible_output is True
