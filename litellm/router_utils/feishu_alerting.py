"""
Feishu (Lark) webhook alerting for router-level deployment bans.

``FEISHU_WEBHOOK_URL`` is read at call time, so the proxy can supply it through the
environment without re-importing this module.

Delivery is best-effort by design: an alert must never change the outcome of the
request that triggered it, and a missing or failing webhook is only logged.
"""

import asyncio
import os
import threading
from collections.abc import Mapping
from typing import Final, cast  # noqa: TID251  # httpx response.json() is untyped

import httpx
from typing_extensions import ReadOnly, TypedDict

from litellm._logging import verbose_router_logger

FEISHU_WEBHOOK_URL_ENV_VAR: Final = "FEISHU_WEBHOOK_URL"
_FEISHU_TIMEOUT_SECONDS: Final = 15.0


class _FeishuTextContent(TypedDict):
    text: ReadOnly[str]


class _FeishuTextPayload(TypedDict):
    msg_type: ReadOnly[str]
    content: ReadOnly[_FeishuTextContent]


async def send_feishu_text_alert(text: str) -> bool:
    """Post plain text to the configured Feishu webhook.

    Returns True only when the webhook accepted the message. Never raises.
    """
    webhook: Final = os.getenv(FEISHU_WEBHOOK_URL_ENV_VAR)
    if not webhook:
        verbose_router_logger.debug("%s is not set; skipping Feishu alert", FEISHU_WEBHOOK_URL_ENV_VAR)
        return False

    payload: Final[_FeishuTextPayload] = {"msg_type": "text", "content": {"text": text}}
    try:
        async with httpx.AsyncClient(timeout=_FEISHU_TIMEOUT_SECONDS) as client:
            response: Final = await client.post(webhook, json=payload)
            response.raise_for_status()
            body: Final[object] = cast(object, response.json())  # cast-ok: httpx response.json() is untyped
    except Exception as e:  # noqa: BLE001  # alerting must never break the request
        verbose_router_logger.warning("Feishu alert delivery failed: %s", e)
        return False

    if not isinstance(body, Mapping):
        verbose_router_logger.warning("Feishu returned a non-object response: %s", body)
        return False
    parsed: Final = cast(Mapping[str, object], body)  # cast-ok: isinstance narrowing keeps the value type unknown
    code: Final[object] = parsed.get("code", parsed.get("StatusCode"))
    if code != 0:
        verbose_router_logger.warning("Feishu rejected the alert: %s", parsed)
        return False
    return True


def schedule_feishu_text_alert(text: str) -> None:
    """Fire-and-forget alert from sync code (the router failure callback).

    Uses the running event loop when there is one. Without a loop - for example the
    sync router path or a threaded failure handler - the HTTP call runs in a daemon
    thread so the caller is never blocked.
    """
    try:
        loop: Final = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(
            target=lambda: asyncio.run(send_feishu_text_alert(text)),
            daemon=True,
        ).start()
        return
    loop.create_task(send_feishu_text_alert(text))
