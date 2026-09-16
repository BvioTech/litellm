"""Delivery contract of the Feishu webhook used for deployment-ban alerts."""

from typing import Final
from unittest.mock import patch

import pytest

from litellm.router_utils.feishu_alerting import send_feishu_text_alert

WEBHOOK: Final = "https://open.feishu.cn/open-apis/bot/v2/hook/example"


class _FakeResponse:
    def __init__(self, body: object, status_error: bool = False) -> None:
        self._body: Final = body
        self._status_error: Final = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise RuntimeError("503 Service Unavailable")

    def json(self) -> object:
        return self._body


class _FakeClient:
    calls: Final[list[tuple[str, dict]]] = []

    def __init__(self, response: _FakeResponse, **_kwargs: object) -> None:
        self._response: Final = response

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, url: str, json: dict) -> _FakeResponse:
        _FakeClient.calls.append((url, json))
        return self._response


def _client_factory(response: _FakeResponse):
    return lambda **kwargs: _FakeClient(response=response, **kwargs)


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    _FakeClient.calls = []


@pytest.mark.asyncio
async def test_missing_webhook_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    assert await send_feishu_text_alert("hello") is False
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_accepted_alert_posts_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", WEBHOOK)
    with patch(
        "litellm.router_utils.feishu_alerting.httpx.AsyncClient",
        _client_factory(_FakeResponse({"code": 0})),
    ):
        assert await send_feishu_text_alert("line one\nline two") is True
    assert _FakeClient.calls == [(WEBHOOK, {"msg_type": "text", "content": {"text": "line one\nline two"}})]


@pytest.mark.asyncio
async def test_rejected_alert_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", WEBHOOK)
    with patch(
        "litellm.router_utils.feishu_alerting.httpx.AsyncClient",
        _client_factory(_FakeResponse({"code": 19001, "msg": "param invalid"})),
    ):
        assert await send_feishu_text_alert("hello") is False


@pytest.mark.asyncio
async def test_transport_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", WEBHOOK)
    with patch(
        "litellm.router_utils.feishu_alerting.httpx.AsyncClient",
        _client_factory(_FakeResponse({}, status_error=True)),
    ):
        assert await send_feishu_text_alert("hello") is False
