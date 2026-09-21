from collections.abc import Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from typing_extensions import TypedDict


class OpenRouterErrorMessage(TypedDict):
    message: str
    code: int
    metadata: dict


DecisionsInput: TypeAlias = str | Mapping[str, JsonValue] | tuple[JsonValue, ...]


class DecisionsNoulCriteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    yes: DecisionsInput = Field(alias="true")
    no: DecisionsInput = Field(alias="false")


class DecisionsNoulQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["noul"]
    instructions: DecisionsInput
    criteria: DecisionsNoulCriteria | None = None


class DecisionsChoiceQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["choice"]
    instructions: DecisionsInput
    criteria: Mapping[str, DecisionsInput | None]


class DecisionsScoreQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["score"]
    instructions: DecisionsInput
    criteria: tuple[DecisionsInput, ...]


DecisionsQuestion: TypeAlias = Annotated[
    DecisionsNoulQuestion | DecisionsChoiceQuestion | DecisionsScoreQuestion,
    Field(discriminator="type"),
]


class OpenRouterDecisionsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    state: DecisionsInput
    questions: Mapping[str, DecisionsQuestion] = Field(min_length=1)
    user: str | None = None
    provider: Mapping[str, JsonValue] | None = None
    session_id: str | None = None
    trace: Mapping[str, JsonValue] | None = None


class OpenRouterDecisionsUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class OpenRouterDecisionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str | None = None
    answers: Mapping[str, JsonValue]
    usage: OpenRouterDecisionsUsage
