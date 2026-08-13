"""``output_config.effort`` survives the ``/v1/messages`` bridge without ``thinking``.

The bridge used to translate ``output_config`` only from inside the ``thinking``
branch, so a caller that sent an effort tier and no ``thinking`` at all had its tier
dropped on the floor. Claude Code does exactly that on some turns
(claude-code#79798), and the tier is the only signal of how hard the caller wanted
the model to work, so it has to reach the provider layer that can act on it.
"""

import os
import sys

sys.path.insert(0, os.path.abspath("../../../../../.."))

from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter,
)

ARN = "arn:aws:bedrock:ap-southeast-1:354674817012:application-inference-profile/ymo9b3n37s88"


def _bridge(model: str, **extra) -> dict:
    request = {
        "model": model,
        "max_tokens": 32000,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }
    translated = LiteLLMAnthropicMessagesAdapter().translate_anthropic_to_openai(
        anthropic_message_request=request
    )
    return translated[0] if isinstance(translated, tuple) else translated


def test_effort_survives_when_thinking_is_absent() -> None:
    kwargs = _bridge(ARN, output_config={"effort": "xhigh"})

    assert kwargs["output_config"] == {"effort": "xhigh"}
    assert "thinking" not in kwargs


def test_effort_still_rides_along_with_thinking() -> None:
    """The original path keeps working — the refactor only widened it."""
    kwargs = _bridge(ARN, thinking={"type": "adaptive"}, output_config={"effort": "max"})

    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "max"}


def test_structured_output_format_is_not_mistaken_for_an_effort_tier() -> None:
    """``format`` is consumed into ``response_format``; only the effort subset rides on.

    Forwarding a bare ``{"format": ...}`` would send Bedrock a duplicate schema.
    """
    kwargs = _bridge(
        ARN,
        output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}},
    )

    assert "output_config" not in kwargs


def test_non_bedrock_target_never_receives_the_raw_param() -> None:
    """Other bridged providers reject ``output_config``, so it must stay Bedrock-only."""
    kwargs = _bridge("gpt-4o", output_config={"effort": "xhigh"})

    assert "output_config" not in kwargs
