from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response

from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
from litellm.types.llms.openrouter import OpenRouterDecisionsRequest

router: Final = APIRouter()


@router.post("/v1/decisions")
@router.post("/decisions")
@router.post("/alpha/decisions")
async def decisions(
    body: OpenRouterDecisionsRequest,
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> Response:
    from litellm.proxy.proxy_server import (
        general_settings,  # pyright: ignore[reportUnknownVariableType]  # proxy settings are dynamically initialized
        llm_router,
        proxy_config,
        proxy_logging_obj,
        select_data_generator,  # pyright: ignore[reportUnknownVariableType]  # shared streaming helper is untyped
        version,
    )

    processor: Final = ProxyBaseLLMRequestProcessing(
        data={  # mutable-ok: shared proxy processing injects logging and routing metadata
            "model": body.model,
            "method": "POST",
            "endpoint": "decisions",
            "_required_custom_llm_provider": "openrouter",
            "passthrough_on_no_deployment": False,
            "json": body.model_dump(mode="json", by_alias=True, exclude_unset=True),
            "user": body.user,
        }
    )
    try:
        return await processor.base_passthrough_process_llm_request(  # pyright: ignore[reportUnknownMemberType]  # existing shared processor accepts dynamic provider payloads
            request=request,
            fastapi_response=fastapi_response,
            user_api_key_dict=user_api_key_dict,
            proxy_logging_obj=proxy_logging_obj,
            llm_router=llm_router,
            general_settings=general_settings,
            proxy_config=proxy_config,
            select_data_generator=select_data_generator,
            version=version,
        )
    except Exception as exc:
        raise await processor._handle_llm_api_exception(  # pyright: ignore[reportPrivateUsage]  # shared proxy error translation has no public entry point
            e=exc,
            user_api_key_dict=user_api_key_dict,
            proxy_logging_obj=proxy_logging_obj,
            version=version,
        )
