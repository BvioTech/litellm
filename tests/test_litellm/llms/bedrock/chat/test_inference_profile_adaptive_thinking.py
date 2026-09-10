"""Thinking reaches Bedrock in the shape required by the model behind an ARN.

An application inference profile ARN is an opaque id, so every cost-map capability
probe reports "not an adaptive-thinking model" and the capability-blind gates
synthesize a legacy ``thinking={"type": "enabled", "budget_tokens": N}`` budget.
Bedrock rejects that outright on these models: ``"thinking.type.enabled" is not
supported for this model. Use "thinking.type.adaptive" and "output_config.effort"``.
The mirror-image failure is ``output_config.effort`` above ``high`` arriving with
thinking off, which Bedrock also rejects.

``model_info.base_model`` is the only id that resolves the real capability, and
``get_optional_params`` is the one layer that sees it, so the repair lands there,
after ``map_openai_params`` has already done its capability-blind rewriting.
"""

import os
import sys
from typing import Final

import pytest

sys.path.insert(0, os.path.abspath("../../../../.."))

import litellm
from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig

ARN = "arn:aws:bedrock:ap-southeast-1:354674817012:application-inference-profile/ymo9b3n37s88"
ADAPTIVE_MODEL = "us.anthropic.claude-opus-5-20260101-v1:0"
LEGACY_MODEL = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
# What the deployment's `model_info.base_model` points at in production.
BASE_MODEL = "global.anthropic.claude-opus-5"

ADAPTIVE_REQUEST = {
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},
    "max_tokens": 32000,
}


def _map(model: str, non_default_params: dict) -> dict:
    return AmazonConverseConfig().map_openai_params(
        non_default_params=dict(non_default_params),
        optional_params={},
        model=model,
        drop_params=True,
    )


def _wire(base_model: str | None = BASE_MODEL, **kwargs) -> dict:
    """Everything a request crosses: get_optional_params, then the Converse body."""
    optional_params = litellm.utils.get_optional_params(
        model=ARN,
        custom_llm_provider="bedrock",
        drop_params=True,
        max_tokens=32000,
        base_model=base_model,
        **kwargs,
    )
    body = AmazonConverseConfig().transform_request(
        model=ARN,
        messages=[{"role": "user", "content": "hi"}],
        optional_params=dict(optional_params),
        litellm_params={},
        headers={},
    )
    return body["additionalModelRequestFields"]


@pytest.mark.parametrize(
    "budget_tokens, expected_effort",
    [
        (16384, "xhigh"),
        (16000, "xhigh"),
        (8192, "xhigh"),
        (4096, "high"),
        (2048, "medium"),
        (1024, "low"),
    ],
)
def test_legacy_budget_is_translated_to_adaptive(budget_tokens: int, expected_effort: str) -> None:
    """A caller sending the legacy budget shape must not reach Bedrock with it.

    This is the production 400: an SDK client that picks `thinking.type=enabled`
    (because its own capability table says the model is budget-only) gets a hard
    rejection from Bedrock. The budget carries the caller's intended depth, so map
    it onto the effort tier rather than dropping it.
    """
    fields = _wire(thinking={"type": "enabled", "budget_tokens": budget_tokens})

    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"]["effort"] == expected_effort


def test_caller_effort_wins_over_the_budget_derived_tier() -> None:
    """An explicit tier is intent; a budget is only a proxy for it."""
    fields = _wire(
        thinking={"type": "enabled", "budget_tokens": 1024},
        output_config={"effort": "max"},
    )

    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"]["effort"] == "max"


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_high_effort_with_thinking_off_enables_adaptive(effort: str) -> None:
    """Bedrock rejects effort above `high` while thinking is off.

    Claude Code sends exactly this pair (claude-code#79798) — the effort tier is
    the more specific signal, so honour it by turning thinking on instead of
    failing the request or silently downgrading the tier.
    """
    fields = _wire(thinking={"type": "disabled"}, output_config={"effort": effort})

    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"]["effort"] == effort


def test_high_effort_without_any_thinking_param_enables_adaptive() -> None:
    """Same repair when the caller omits `thinking` entirely rather than disabling it."""
    fields = _wire(output_config={"effort": "xhigh"})

    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"]["effort"] == "xhigh"


def test_disabled_thinking_is_left_alone_below_the_ceiling() -> None:
    """`high` and below are legal with thinking off, so the caller's choice stands.

    Pins the boundary: the repair above must not become a blanket "always think".
    """
    fields = _wire(thinking={"type": "disabled"}, output_config={"effort": "high"})

    assert fields["thinking"] == {"type": "disabled"}
    assert fields["output_config"]["effort"] == "high"


def test_reasoning_effort_reaches_bedrock_as_adaptive() -> None:
    """The OpenAI-style entry point lands on the same shape.

    `_handle_reasoning_effort_parameter` is capability-blind too, so it synthesizes
    a legacy budget for an ARN; the repair has to cover this path as well.
    """
    fields = _wire(reasoning_effort="high")

    assert fields["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert fields["output_config"]["effort"] == "high"


def test_reasoning_effort_budget_respects_max_completion_tokens() -> None:
    params: Final = litellm.utils.get_optional_params(
        model=ARN,
        custom_llm_provider="bedrock",
        base_model=LEGACY_MODEL,
        reasoning_effort="high",
        max_tokens=32000,
        max_completion_tokens=2048,
    )

    assert params["maxTokens"] == 2048
    assert params["thinking"] == {"type": "enabled", "budget_tokens": 2047}


def test_claude_46_keeps_an_explicit_legacy_budget() -> None:
    fields: Final = _wire(
        base_model="us.anthropic.claude-opus-4-6-v1",
        thinking={"type": "enabled", "budget_tokens": 16000},
    )

    assert fields["thinking"] == {"type": "enabled", "budget_tokens": 16000}


@pytest.mark.parametrize("model", [ARN, "us.anthropic.claude-opus-4-6-v1"])
def test_claude_46_base_model_preserves_bedrock_effort_normalization(model: str) -> None:
    params: Final = litellm.utils.get_optional_params(
        model=model,
        custom_llm_provider="bedrock",
        base_model="us.anthropic.claude-opus-4-6-v1",
        reasoning_effort="xhigh",
        max_tokens=32000,
    )

    assert params["thinking"]["type"] == "adaptive"
    assert params["output_config"]["effort"] == "max"


def test_claude_46_native_effort_is_normalized_behind_an_arn() -> None:
    output_config: Final = {"effort": "xhigh"}
    fields: Final = _wire(
        base_model="us.anthropic.claude-opus-4-6-v1",
        thinking={"type": "adaptive"},
        output_config=output_config,
    )

    assert fields["output_config"]["effort"] == "max"
    assert output_config == {"effort": "xhigh"}


@pytest.mark.parametrize(
    "model, expected_param, expected_value",
    [
        ("converse/openai.gpt-oss-120b-1:0", "reasoning_effort", "high"),
        (
            "converse/amazon.nova-2-lite-v1:0",
            "reasoningConfig",
            {"type": "enabled", "maxReasoningEffort": "high"},
        ),
    ],
)
def test_non_claude_base_model_keeps_provider_reasoning(
    model: str, expected_param: str, expected_value: str | dict[str, str]
) -> None:
    params: Final = litellm.utils.get_optional_params(
        model=model,
        custom_llm_provider="bedrock",
        base_model=model.removeprefix("converse/"),
        reasoning_effort="high",
        max_tokens=32000,
    )

    assert params[expected_param] == expected_value
    assert "thinking" not in params
    assert "output_config" not in params


@pytest.mark.parametrize(
    "base_model",
    [
        "global.anthropic.claude-opus-5",
        "global.anthropic.claude-opus-4-7",
        "global.anthropic.claude-opus-4-6-v1",
        "global.anthropic.claude-sonnet-5",
        "global.anthropic.claude-sonnet-4-6",
    ],
)
def test_reasoning_effort_max_survives_application_profile_bridge(base_model: str) -> None:
    """Every adaptive model that advertises max must keep that tier behind an opaque ARN."""
    fields = _wire(base_model=base_model, reasoning_effort="max")

    assert fields["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert fields["output_config"]["effort"] == "max"


def test_without_base_model_the_legacy_shape_survives() -> None:
    """Documents the config dependency, so a silent regression is impossible.

    Nothing but `model_info.base_model` can resolve an ARN's capabilities. Drop it
    from the deployment and the legacy budget goes to Bedrock unrepaired, which is
    the 400 this module exists to prevent.
    """
    fields = _wire(base_model=None, thinking={"type": "enabled", "budget_tokens": 16000})

    assert fields["thinking"] == {"type": "enabled", "budget_tokens": 16000}


@pytest.mark.parametrize("model", [ARN, f"bedrock/converse/{ARN}", ADAPTIVE_MODEL])
def test_adaptive_thinking_forwarded_verbatim(model):
    """An ARN behaves exactly like a resolvable adaptive model id."""
    mapped = _map(model, ADAPTIVE_REQUEST)

    assert mapped["thinking"] == {"type": "adaptive"}
    assert mapped["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_every_effort_tier_survives_the_arn(effort):
    """The effort tier is the payload — it must not collapse to a fixed budget."""
    mapped = _map(ARN, {**ADAPTIVE_REQUEST, "output_config": {"effort": effort}})

    assert mapped["thinking"] == {"type": "adaptive"}
    assert mapped["output_config"]["effort"] == effort


def test_resolvable_non_adaptive_model_still_downgrades():
    """The downgrade is still correct when the model id *is* resolvable.

    Only the ARN — where no capability can be resolved — is exempt. A real
    pre-4.6 model id must keep being rewritten to a legacy budget, otherwise
    Bedrock rejects the request.
    """
    mapped = _map(LEGACY_MODEL, ADAPTIVE_REQUEST)

    assert mapped["thinking"]["type"] == "enabled"
    assert "budget_tokens" in mapped["thinking"]


def test_legacy_thinking_through_an_arn_is_untouched():
    """A caller that asks for a fixed budget still gets that exact budget."""
    mapped = _map(ARN, {"thinking": {"type": "enabled", "budget_tokens": 16000}, "max_tokens": 32000})

    assert mapped["thinking"] == {"type": "enabled", "budget_tokens": 16000}


def test_reasoning_effort_gate_is_capability_blind_on_its_own():
    """``map_openai_params`` alone cannot fix the ``reasoning_effort`` path.

    Pins *why* the repair has to live in ``get_optional_params``: this layer only
    ever sees ``litellm_params.model``, so for an ARN it synthesizes a legacy budget
    and no ``output_config`` at all. Paired with
    ``test_reasoning_effort_reaches_bedrock_as_adaptive``, which shows the same
    input coming out correct once ``base_model`` is in scope.
    """
    mapped = _map(ARN, {"reasoning_effort": "high", "max_tokens": 32000})

    assert mapped["thinking"]["type"] == "enabled"
    assert "output_config" not in mapped


def test_request_body_carries_both_fields():
    """End-to-end: both fields reach additionalModelRequestFields."""
    config = AmazonConverseConfig()
    optional_params = _map(ARN, ADAPTIVE_REQUEST)

    body = config.transform_request(
        model=ARN,
        messages=[{"role": "user", "content": "hi"}],
        optional_params=optional_params,
        litellm_params={},
        headers={},
    )

    assert body["additionalModelRequestFields"]["thinking"] == {"type": "adaptive"}
    assert body["additionalModelRequestFields"]["output_config"] == {"effort": "high"}
