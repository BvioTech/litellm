"""
Bedrock account-level Anthropic denial: the deployment is blocked, not cooled down.

Bedrock reports the denial as a 400 ValidationException (Error 002) whose body is
`{"message": "Access to Anthropic models is not allowed for this account."}`. Clearing it
needs an AWS-side change, so one match pauses the deployment via `model_info.blocked` and
alerts through FEISHU_WEBHOOK_URL.
"""

import json
import sys
from typing import Final
from unittest.mock import patch

import httpx
import pytest

import litellm
from litellm.router_utils.cooldown_handlers import _async_get_cooldown_deployments
from litellm.router_utils.denied_account_block import (
    block_denied_bedrock_deployment,
    is_bedrock_account_access_denied,
    persist_blocked_deployment,
    upstream_error_message,
)

DENIED: Final = "Access to Anthropic models is not allowed for this account."
UNRELATED: Final = "thinking.type.enabled is not supported for this model"
ARN: Final = "bedrock/arn:aws:bedrock:us-east-2:111111111111:application-inference-profile/{name}"
ALERT: Final = "litellm.router_utils.denied_account_block.schedule_feishu_text_alert"
PERSIST: Final = "litellm.router_utils.denied_account_block._schedule_blocked_persistence"


def _exception(
    body: str,
    provider: str = "bedrock",
    status: int = 400,
    suffix: str = ". Received Model Group=claude-opus-5",
) -> litellm.BadRequestError | litellm.PermissionDeniedError:
    """A litellm exception shaped exactly as `exception_type` builds it for Bedrock."""
    error_class: Final = litellm.BadRequestError if status == 400 else litellm.PermissionDeniedError
    return error_class(
        message=f"BedrockException - {body}{suffix}",
        model="us.anthropic.claude-opus-5",
        llm_provider=provider,
        response=httpx.Response(status, request=httpx.Request("POST", "https://bedrock.example/converse")),
    )


def _router(*deployment_ids: str, **kwargs) -> litellm.Router:
    return litellm.Router(
        model_list=[
            {
                "model_name": "claude-opus-5",
                "litellm_params": {"model": ARN.format(name=deployment_id)},
                "model_info": {"id": deployment_id},
            }
            for deployment_id in deployment_ids
        ],
        **kwargs,
    )


@pytest.mark.parametrize(
    "body,expected",
    [
        # The non-streaming Bedrock path: `err.response.text`, plain JSON.
        (json.dumps({"message": DENIED}), True),
        # The streaming path raises `BedrockError(message=str(response.read()))`, so the
        # body is a bytes *repr*. Regression: parsing this as JSON fails outright.
        (str(json.dumps({"message": DENIED}).encode()), True),
        # Bedrock appends remediation hints to the same sentence.
        (json.dumps({"message": f"{DENIED} For additional access options, contact AWS Sales."}), True),
        # AWS is inconsistent about this key's case across services.
        (json.dumps({"Message": DENIED}), True),
        (json.dumps({"__type": "AccessDeniedException", "message": DENIED}), True),
        # A body that is not JSON at all.
        (DENIED, True),
        (" access TO Anthropic  models is not allowed for this account ", True),
        (json.dumps({"message": UNRELATED}), False),
        # Regression: a request whose own prompt echoes the sentence back inside an
        # ordinary validation error must not pause the deployment.
        (json.dumps({"message": f"Invalid input: the prompt says {DENIED}"}), False),
        (json.dumps({"error": {"message": DENIED}}), False),
        (json.dumps({"message": None}), False),
        (json.dumps([DENIED]), False),
    ],
)
def test_detects_only_the_account_denial(body: str, expected: bool) -> None:
    assert is_bedrock_account_access_denied(_exception(body)) is expected


@pytest.mark.parametrize("status", [400, 403])
def test_both_status_codes_bedrock_reports_are_detected(status: int) -> None:
    assert is_bedrock_account_access_denied(_exception(json.dumps({"message": DENIED}), status=status)) is True


@pytest.mark.parametrize("provider", ["openai", "anthropic", "vertex_ai"])
def test_other_providers_are_ignored(provider: str) -> None:
    assert is_bedrock_account_access_denied(_exception(json.dumps({"message": DENIED}), provider=provider)) is False


def test_non_litellm_exceptions_are_ignored() -> None:
    assert is_bedrock_account_access_denied(RuntimeError(DENIED)) is False
    assert is_bedrock_account_access_denied(None) is False


def test_upstream_message_survives_the_appended_model_group_suffix() -> None:
    """The router appends ". Received Model Group=..." after the body; it must not be
    swallowed into the bytes literal nor break the decode."""
    assert upstream_error_message(_exception(str(json.dumps({"message": DENIED}).encode()))) == DENIED


@pytest.mark.asyncio
async def test_denial_blocks_the_deployment_and_takes_it_out_of_routing() -> None:
    router: Final = _router("denied", "healthy")
    try:
        with patch(ALERT) as alert, patch(PERSIST) as persist:
            assert block_denied_bedrock_deployment(
                router=router,
                deployment_id="denied",
                exception=_exception(json.dumps({"message": DENIED})),
                model_group="claude-opus-5",
            )
        blocked: Final = {
            d["model_info"]["id"]: d["model_info"].get("blocked")
            for d in router.get_model_list(model_name="claude-opus-5") or []
        }
        assert blocked == {"denied": True, "healthy": None}
        healthy, all_deployments = await router._async_get_healthy_deployments(  # pyright: ignore[reportPrivateUsage]
            model="claude-opus-5", parent_otel_span=None
        )
        assert [d["model_info"]["id"] for d in healthy] == ["healthy"]
        # Blocking must not shrink the candidate list the retry guard counts.
        assert sorted(d["model_info"]["id"] for d in all_deployments) == ["denied", "healthy"]
        assert await _async_get_cooldown_deployments(router, None) == []
        persist.assert_called_once_with("denied")
        assert alert.call_count == 1
    finally:
        router.reset()


@pytest.mark.asyncio
async def test_only_the_failed_deployment_is_blocked() -> None:
    """A sibling on the same AWS account keeps serving until a request of its own is denied."""
    router: Final = _router("denied", "sibling-same-account")
    try:
        with patch(ALERT), patch(PERSIST):
            block_denied_bedrock_deployment(
                router=router,
                deployment_id="denied",
                exception=_exception(json.dumps({"message": DENIED})),
            )
        healthy, _ = await router._async_get_healthy_deployments(  # pyright: ignore[reportPrivateUsage]
            model="claude-opus-5", parent_otel_span=None
        )
        assert [d["model_info"]["id"] for d in healthy] == ["sibling-same-account"]
    finally:
        router.reset()


def test_blocking_is_idempotent_and_alerts_once() -> None:
    router: Final = _router("denied", "healthy")
    error: Final = _exception(json.dumps({"message": DENIED}))
    try:
        with patch(ALERT) as alert, patch(PERSIST) as persist:
            assert block_denied_bedrock_deployment(router=router, deployment_id="denied", exception=error) is True
            assert block_denied_bedrock_deployment(router=router, deployment_id="denied", exception=error) is False
        assert alert.call_count == 1
        assert persist.call_count == 1
    finally:
        router.reset()


def test_an_unrelated_error_does_not_block() -> None:
    router: Final = _router("denied", "healthy")
    try:
        with patch(ALERT) as alert, patch(PERSIST):
            assert (
                block_denied_bedrock_deployment(
                    router=router,
                    deployment_id="denied",
                    exception=_exception(json.dumps({"message": UNRELATED})),
                )
                is False
            )
        assert alert.call_count == 0
        deployments: Final = router.get_model_list(model_name="claude-opus-5") or []
        assert all(d["model_info"].get("blocked") is not True for d in deployments)
    finally:
        router.reset()


def test_an_unknown_deployment_id_is_a_noop() -> None:
    router: Final = _router("denied")
    try:
        with patch(ALERT) as alert, patch(PERSIST):
            assert (
                block_denied_bedrock_deployment(
                    router=router,
                    deployment_id="not-in-this-router",
                    exception=_exception(json.dumps({"message": DENIED})),
                )
                is False
            )
        assert alert.call_count == 0
    finally:
        router.reset()


def test_blocking_ignores_cooldown_settings() -> None:
    """Blocking is not a cooldown, so `disable_cooldowns` does not opt out of it."""
    router: Final = _router("denied", "healthy", disable_cooldowns=True)
    try:
        with patch(ALERT), patch(PERSIST):
            assert block_denied_bedrock_deployment(
                router=router,
                deployment_id="denied",
                exception=_exception(json.dumps({"message": DENIED})),
            )
    finally:
        router.reset()


def test_the_alert_carries_the_arn_but_never_request_credentials() -> None:
    router: Final = _router("denied")
    # str(exception) on a real failure can carry the signed request URL; only the message
    # Bedrock itself reported may reach the Feishu group.
    error: Final = _exception(
        json.dumps({"message": DENIED}),
        suffix=". Received Model Group=claude-opus-5 X-Amz-Credential=AKIAIOSFODNN7EXAMPLE/20260916/us-east-2",
    )
    try:
        with patch(ALERT) as alert, patch(PERSIST):
            block_denied_bedrock_deployment(
                router=router, deployment_id="denied", exception=error, model_group="claude-opus-5"
            )
        text: Final = alert.call_args.args[0]
        assert "arn:aws:bedrock:us-east-2:111111111111" in text
        assert DENIED in text
        assert "claude-opus-5" in text
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        assert "X-Amz-Credential" not in text
    finally:
        router.reset()


@pytest.mark.asyncio
async def test_blocked_persistence_is_a_noop_outside_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # The proxy module is only imported when the proxy runs; the SDK must not need it.
    monkeypatch.delitem(sys.modules, "litellm.proxy.proxy_server", raising=False)
    assert await persist_blocked_deployment("denied") is False
