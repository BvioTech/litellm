import json
from collections.abc import Mapping, Sequence
from typing import Final

import httpx

import litellm
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.llms.base_llm.passthrough.transformation import BasePassthroughConfig
from litellm.llms.openrouter.common_utils import OpenRouterException
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import AllMessageValues
from litellm.types.llms.openrouter import OpenRouterDecisionsResponse
from litellm.types.utils import Choices, Message, ModelResponse, Usage


class OpenRouterDecisionsConfig(BasePassthroughConfig):
    @staticmethod
    def get_api_key(api_key: str | None = None) -> str | None:
        return api_key or litellm.openrouter_key or get_secret_str("OPENROUTER_API_KEY") or get_secret_str("OR_API_KEY")

    @staticmethod
    def get_api_base(api_base: str | None = None) -> str:
        base: Final = (api_base or get_secret_str("OPENROUTER_API_BASE") or "https://openrouter.ai/api/alpha").rstrip(
            "/"
        )
        return f"{base[:-3]}/alpha" if base.endswith("/v1") else base

    def get_models(
        self, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:  # mutable-ok: BaseLLMModelInfo requires a list
        return list(("typesafe/jev-1.13", "~typesafe/jev-latest"))  # mutable-ok: BaseLLMModelInfo return contract

    @staticmethod
    def get_base_model(model: str) -> str:
        return model

    def validate_environment(
        self,
        headers: Mapping[str, str],
        model: str,
        messages: Sequence[AllMessageValues],
        optional_params: Mapping[str, object],
        litellm_params: Mapping[str, object],
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict[str, str]:  # mutable-ok: passthrough transport augments the returned headers
        key: Final = self.get_api_key(api_key)
        if key is None:
            raise OpenRouterException(status_code=401, message="OPENROUTER_API_KEY is required for Decisions requests")
        return {  # mutable-ok: transport augments these authentication headers
            **headers,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def is_streaming_request(self, endpoint: str, request_data: Mapping[str, object]) -> bool:
        return False

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        endpoint: str,
        request_query_params: Mapping[str, str] | None,
        litellm_params: Mapping[str, object],
    ) -> tuple[httpx.URL, str]:
        if endpoint.strip("/") != "decisions":
            raise OpenRouterException(status_code=400, message="OpenRouter passthrough supports the decisions endpoint")
        base: Final = self.get_api_base(api_base)
        return httpx.URL(f"{base}/decisions", params=request_query_params), base

    def logging_non_streaming_response(
        self,
        model: str,
        custom_llm_provider: str,
        httpx_response: httpx.Response,
        request_data: Mapping[str, object],
        logging_obj: Logging,
        endpoint: str,
    ) -> ModelResponse:
        payload: Final = OpenRouterDecisionsResponse.model_validate_json(httpx_response.content)
        result: Final = ModelResponse(
            id=payload.id,
            model=model,
            choices=[  # mutable-ok: ModelResponse accepts only lists of Choices
                Choices(message=Message(role="assistant", content=json.dumps(payload.answers)), finish_reason="stop"),
            ],
            usage=Usage(
                prompt_tokens=payload.usage.input_tokens,
                completion_tokens=payload.usage.output_tokens,
                total_tokens=payload.usage.input_tokens + payload.usage.output_tokens,
            ),
        )
        if payload.usage.cost is not None:
            result.set_provider_response_headers(httpx.Headers((("x-litellm-response-cost", str(payload.usage.cost)),)))
        return result
