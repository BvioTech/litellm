from collections.abc import Sequence
from copy import deepcopy
from itertools import permutations
from typing import Final

import pytest

from litellm import Router
from litellm.proxy.common_utils.model_listing_utils import _model_info_sort_order, sort_model_listing_entries


def _ranked_router(values: Sequence[tuple[str, object]]) -> Router:
    return Router(
        model_list=[
            {
                "model_name": name,
                "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-fake"},
                "model_info": {"id": f"deployment-{index}", "sort_order": value},
            }
            for index, (name, value) in enumerate(values)
        ]
    )


def test_model_listing_sort_is_numeric_and_keeps_invalid_values_last() -> None:
    values: Final = (
        ("missing", None),
        ("ten", 10),
        ("two", 2),
        ("zero", 0),
        ("fraction", 0.5),
        ("negative", -1),
        ("text", "2"),
        ("boolean", True),
        ("invalid-object", {"nested": 1}),
        ("invalid-list", [1]),
    )
    router: Final = _ranked_router(values)
    entries: Final = tuple((name, name) for name, _ in values)
    result: Final = sort_model_listing_entries(entries, router)
    expected: Final = (
        "negative",
        "zero",
        "fraction",
        "two",
        "ten",
        "boolean",
        "invalid-list",
        "invalid-object",
        "missing",
        "text",
    )
    assert result == tuple((name, name) for name in expected)


@pytest.mark.parametrize("reverse_deployments", [False, True])
def test_model_listing_sort_is_repeatable_across_input_and_deployment_order_without_mutation(
    reverse_deployments: bool,
) -> None:
    values: Final = (("zeta", 20), ("zeta", 1), ("middle", 2), ("alpha", 1), ("unknown", None))
    router: Final = _ranked_router(tuple(reversed(values)) if reverse_deployments else values)
    before: Final = deepcopy(router.get_model_list())
    entries: Final = (("unknown", "unknown"), ("zeta", "zeta"), ("middle", "middle"), ("alpha", "alpha"))
    for input_entries in permutations(entries):
        result: Final = sort_model_listing_entries(input_entries, router)
        assert result == (("alpha", "alpha"), ("zeta", "zeta"), ("middle", "middle"), ("unknown", "unknown"))
        assert sort_model_listing_entries(result, router) == result
    assert router.get_model_list() == before


def test_model_listing_sort_missing_configuration_falls_back_to_model_id() -> None:
    router: Final = _ranked_router((("zeta", None), ("alpha", None)))
    entries: Final = (("zeta", "zeta"), ("alpha", "alpha"), ("wildcard-model", "unconfigured"))
    assert sort_model_listing_entries(entries, router) == (
        ("alpha", "alpha"),
        ("wildcard-model", "unconfigured"),
        ("zeta", "zeta"),
    )
    assert sort_model_listing_entries(entries, None) == (
        ("alpha", "alpha"),
        ("wildcard-model", "unconfigured"),
        ("zeta", "zeta"),
    )


def test_model_listing_sort_preserves_internal_lookup_ids() -> None:
    router: Final = _ranked_router((("internal-first", 2), ("internal-second", 1)))
    entries: Final = (("public-first", "internal-first"), ("public-second", "internal-second"))
    assert sort_model_listing_entries(entries, router) == tuple(reversed(entries))


@pytest.mark.parametrize(
    "model_info",
    [
        None,
        [],
        {},
        {"sort_order": None},
        {"sort_order": True},
        {"sort_order": "1"},
        {"sort_order": float("nan")},
        {"sort_order": float("inf")},
        {"sort_order": -float("inf")},
    ],
)
def test_model_listing_invalid_sort_configuration_uses_fallback(model_info: object) -> None:
    assert _model_info_sort_order(model_info) is None
