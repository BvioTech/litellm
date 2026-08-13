"""Adaptive thinking survives a Bedrock application inference profile ARN.

Claude Code drives Opus 5 with ``thinking={"type": "adaptive"}`` plus
``output_config={"effort": ...}``. When the deployment points at an application
inference profile ARN, the model id is an opaque suffix, so every cost-map
capability probe reports "not an adaptive-thinking model". Before the fix,
``map_openai_params`` rewrote the caller's explicit adaptive request into a legacy
``{"type": "enabled", "budget_tokens": 2048}`` budget while ``output_config.effort``
was still forwarded verbatim: Bedrock received a contradictory pair and the
requested effort tier was silently discarded.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("../../../../.."))

from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig

ARN = "arn:aws:bedrock:ap-southeast-1:354674817012:application-inference-profile/ymo9b3n37s88"
ADAPTIVE_MODEL = "us.anthropic.claude-opus-5-20260101-v1:0"
LEGACY_MODEL = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"

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


def test_reasoning_effort_through_an_arn_keeps_the_legacy_budget():
    """Deliberately unchanged: an ARN may front a pre-4.6 model.

    On the ``reasoning_effort`` path the caller never asked for adaptive
    thinking, so litellm picks the representation. A fixed budget is accepted by
    every Claude generation; inferring adaptive from an opaque ARN would break
    profiles that front an older model. Documented in BVIO_BRANCH.md as a
    knowingly-retained gap, not an oversight.
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
