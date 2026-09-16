"""
Bedrock account-level Anthropic denial: pause the deployment instead of cooling it down.

Bedrock reports the denial as a 400 ``ValidationException`` (and, in some account setups, a
403 ``AccessDeniedException``) whose body is ``{"message": "Access to Anthropic models is
not allowed for this account."}``. Clearing it needs an AWS-side change, so a TTL cooldown
just re-selects the deployment seconds later and fails again.

On a match the deployment is marked ``blocked``, the flag every routing entry point already
filters on (``Router._filter_blocked_deployments``), and an alert goes to
``FEISHU_WEBHOOK_URL``. The marker is also written to ``LiteLLM_ProxyModelTable.blocked``
under the proxy, which makes it survive a reconcile, a restart, and reach peer pods.

Scope is one deployment per match: a sibling on the same AWS account is paused when a
request of its own hits the denial. Grouping by the ARN's account id was considered and
dropped -- it only works for ARN models, and the extra failure it saves is one request per
model.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, cast  # noqa: TID251  # untyped router rows, raw_decode, sys.modules

import litellm
from litellm._logging import verbose_router_logger
from litellm.router_utils.cooldown_handlers import is_advisor_orchestration_failure
from litellm.router_utils.feishu_alerting import schedule_feishu_text_alert

if TYPE_CHECKING:
    from litellm.router import Router

# Matched as a prefix, not anywhere in the message: Bedrock appends remediation hints to
# the same sentence ("... For additional access options, contact AWS Sales at ..."), so
# whole-string equality misses the denial, while a free search would also fire on a
# request whose own prompt is echoed back inside an ordinary validation error.
_ACCOUNT_ACCESS_DENIED: Final = re.compile(
    r"Access\s+to\s+Anthropic\s+models\s+is\s+not\s+allowed\s+for\s+this\s+account",
    re.IGNORECASE,
)

# The streaming Bedrock path raises `BedrockError(message=str(response.read()))`, so the
# body arrives as a bytes *repr* (`b'{"message": ...}'`) rather than JSON. The trailing
# quote is matched non-greedily so a suffix the router appends later
# (". Received Model Group=...") stays outside the literal handed to literal_eval.
_BYTES_REPR: Final = re.compile(r"""^b(?P<quote>['"]).*?(?<!\\)(?P=quote)""", re.DOTALL)

_MAX_ALERT_ERROR_CHARS: Final = 400


def _decoded_bytes_repr(body: str) -> str:
    """The bytes literal at the start of ``body``, decoded; ``body`` unchanged otherwise."""
    match: Final = _BYTES_REPR.match(body)
    if match is None:
        return body
    try:
        literal: Final[object] = cast(object, ast.literal_eval(body[: match.end()]))  # cast-ok: literal_eval is untyped
    except (ValueError, SyntaxError):
        return body
    return literal.decode("utf-8", "replace") if isinstance(literal, bytes) else body


def _attribute(source: object, name: str) -> object:
    """One attribute off an untyped object (a litellm exception, the proxy module)."""
    return cast(object, getattr(source, name, None))  # cast-ok: getattr on an untyped object


def upstream_error_message(exception: BaseException) -> str | None:
    """The message Bedrock itself reported, or None when the body carries no top-level one.

    LiteLLM wraps provider bodies as ``BedrockException - <body>``. The body is JSON on the
    non-streaming path and a bytes repr of that JSON on the streaming one; a body that is
    neither is returned as-is so plain-text errors stay readable in alerts.
    """
    raw: Final = _attribute(exception, "message")
    if not isinstance(raw, str):
        return None
    body: Final = _decoded_bytes_repr(raw.partition(" - ")[2].lstrip())
    if not body:
        return None
    try:
        payload: Final[object] = cast(object, json.JSONDecoder().raw_decode(body)[0])  # cast-ok: raw_decode -> Any
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, Mapping):
        return None
    fields: Final = cast(Mapping[str, object], payload)  # cast-ok: Mapping narrowing leaves values unknown
    # AWS is inconsistent about the case of this key across services and SDK versions.
    return next(
        (value for key, value in fields.items() if key.lower() == "message" and isinstance(value, str)),
        None,
    )


def is_bedrock_account_access_denied(exception: BaseException | None) -> bool:
    """Whether ``exception`` is Bedrock's account-level Anthropic denial."""
    if not isinstance(exception, (litellm.BadRequestError, litellm.PermissionDeniedError)):
        return False
    if _attribute(exception, "llm_provider") != "bedrock" or is_advisor_orchestration_failure(exception):
        return False
    message: Final = upstream_error_message(exception)
    return message is not None and _ACCOUNT_ACCESS_DENIED.match(message.strip()) is not None


def _deployment_row(router: Router, deployment_id: str) -> Mapping[str, object] | None:
    """The live ``model_list`` entry for ``deployment_id``, or None when it is unknown."""
    return cast(  # cast-ok: Router.get_model_info is typed as a bare dict
        "Mapping[str, object] | None",
        router.get_model_info(id=deployment_id),  # pyright: ignore[reportUnknownMemberType]  # bare dict
    )


def _mark_blocked(row: Mapping[str, object]) -> bool:
    """Set ``blocked`` on the row's ``model_info``; False when it was already set.

    Writes into the live ``model_list`` entry on purpose: ``model_info.blocked`` is what
    every routing filter reads, so the pause takes effect on the next selection.
    """
    model_info: Final[object] = row.get("model_info")
    if not isinstance(model_info, dict):
        return False
    if model_info.get("blocked") is True:  # pyright: ignore[reportUnknownMemberType]  # bare-dict router row
        return False
    model_info["blocked"] = True  # pyright: ignore[reportUnknownMemberType]  # bare-dict router row
    return True


def _deployment_model(row: Mapping[str, object]) -> str | None:
    """The configured model string (an ARN for inference profiles), for the alert."""
    litellm_params: Final[object] = row.get("litellm_params")
    if not isinstance(litellm_params, Mapping):
        return None
    params: Final = cast(Mapping[str, object], litellm_params)  # cast-ok: Mapping narrowing leaves values unknown
    model: Final[object] = params.get("model")
    return model if isinstance(model, str) else None


def _truncated(error: str | None) -> str:
    if error is None:
        return "unavailable"
    if len(error) <= _MAX_ALERT_ERROR_CHARS:
        return error
    return error[: _MAX_ALERT_ERROR_CHARS - 3] + "..."


def _alert_text(deployment_id: str, model: str | None, model_group: str | None, error: str | None) -> str:
    lines: Final = (
        "LiteLLM: Bedrock account denied Anthropic access - deployment blocked",
        f"Deployment: {deployment_id}",
        f"Model: {model or 'unknown'}",
        f"Model group: {model_group or 'unknown'}",
        f"Error: {_truncated(error)}",
    )
    return "\n".join(lines)


def block_denied_bedrock_deployment(
    router: Router,
    deployment_id: str,
    exception: BaseException,
    model_group: str | None = None,
) -> bool:
    """Mark the denied deployment ``blocked`` and alert once. Returns whether it changed.

    Idempotent: the retries of one request, and the fallback path's own hook, all see the
    flag already set and neither re-alert nor re-write the DB.
    """
    if not is_bedrock_account_access_denied(exception):
        return False
    row: Final = _deployment_row(router=router, deployment_id=deployment_id)
    if row is None or not _mark_blocked(row):
        return False

    # Model-group info caches the group's deployments, so a stale entry would keep
    # reporting the blocked deployment as part of the group.
    router._invalidate_model_group_info_cache()  # pyright: ignore[reportPrivateUsage]  # Router-owned cache, invalidated as delete_deployment does

    verbose_router_logger.warning(
        "Bedrock account access denied - blocked deployment %s (model group %s)",
        deployment_id,
        model_group,
    )
    # Only the message Bedrock reported, never str(exception): the latter carries the
    # request context, which can include signed-URL credentials.
    schedule_feishu_text_alert(
        _alert_text(
            deployment_id=deployment_id,
            model=_deployment_model(row),
            model_group=model_group,
            error=upstream_error_message(exception),
        )
    )
    _schedule_blocked_persistence(deployment_id)
    return True


async def persist_blocked_deployment(deployment_id: str) -> bool:
    """Best-effort ``blocked=true`` in the proxy DB. Returns whether the row was written.

    Only DB-stored models have a row; a config.yaml deployment is blocked in this process
    only and comes back on the next config reload. No-op outside the proxy.
    """
    # sys.modules rather than an import: the SDK must not pull the proxy's fastapi
    # dependency chain in, as litellm/passthrough/timeout_utils.py does for the same reason.
    proxy_module: Final = cast(object, sys.modules.get("litellm.proxy.proxy_server"))  # cast-ok: untyped lookup
    if proxy_module is None:
        return False
    prisma_client: Final = _attribute(proxy_module, "prisma_client")
    if prisma_client is None or _attribute(proxy_module, "store_model_in_db") is not True:
        return False

    from litellm.proxy.utils import PrismaClient
    from litellm.repositories.model_repository import ModelRepository

    prisma: Final = cast("PrismaClient", prisma_client)  # cast-ok: value read off the proxy module
    try:
        # ModelRepository.table is wrapped for config sync, so this update publishes the
        # change to peer pods on its own; no explicit broadcast needed here.
        await ModelRepository(prisma).table.update(
            where={"model_id": deployment_id},  # mutable-ok: prisma takes a plain filter payload
            data={"blocked": True},  # mutable-ok: prisma takes a plain update payload
        )
    except Exception as e:  # noqa: BLE001  # persistence is best effort; the in-memory flag already applies
        verbose_router_logger.warning("Could not mark deployment %s blocked in the DB: %s", deployment_id, e)
        return False
    return True


def _schedule_blocked_persistence(deployment_id: str) -> None:
    try:
        loop: Final = asyncio.get_running_loop()
    except RuntimeError:
        # The sync router path has no loop. The proxy is fully async, so this only skips
        # the DB write for SDK callers, which have no proxy DB to write to anyway.
        return
    loop.create_task(persist_blocked_deployment(deployment_id))
