"""Typed input and response-boundary models for the Jev MCP server."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, WithJsonSchema, field_validator, model_validator
from typing_extensions import TypeAliasType

from .questions import CHOICE_MAX_OPTIONS, MATCH_MAX_WINDOW, MATCH_MIN_WINDOW, SCORE_MAX_LEVELS, SCORE_MIN_LEVELS

JSONPrimitive: TypeAlias = str | int | float | bool | None
if TYPE_CHECKING:
    JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
else:
    JSONValue = TypeAliasType("JSONValue", JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"])
if TYPE_CHECKING:
    JSONSchemaValue: TypeAlias = (
        str | int | float | bool | None | list["JSONSchemaValue"] | dict[str, "JSONSchemaValue"]
    )
else:
    JSONSchemaValue = TypeAliasType(
        "JSONSchemaValue",
        str | int | float | bool | None | list["JSONSchemaValue"] | dict[str, "JSONSchemaValue"],
    )
JSONContent: TypeAlias = str | list["JSONValue"] | dict[str, "JSONValue"]
StateValue: TypeAlias = str | dict[str, JSONValue] | list[JSONValue]


class _StrictModel(BaseModel):
    """Reject unknown fields and implicit scalar coercion at tool boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True)


class LabelDescription(_StrictModel):
    """Structured label guidance for a Choice question."""

    what: str
    not_for: str
    examples: list[str]


LabelValue: TypeAlias = str | LabelDescription


class PropositionDescription(_StrictModel):
    """Structured true/false guidance for a Noul question."""

    statement: str
    true: str
    false: str


PropositionValue: TypeAlias = str | PropositionDescription


class AskNoulQuestion(_StrictModel):
    type: Literal["noul"]
    instructions: JSONContent | None = None
    criteria: dict[str, JSONContent | None] | None = None

    @field_validator("criteria")
    @classmethod
    def validate_criteria_keys(
        cls, value: dict[str, JSONContent | None] | None
    ) -> dict[str, JSONContent | None] | None:
        if value is not None and not set(value).issubset({"true", "false"}):
            raise ValueError("noul criteria may contain only true and false")
        return value


class AskChoiceQuestion(_StrictModel):
    type: Literal["choice"]
    instructions: JSONContent | None = None
    criteria: dict[str, JSONContent | None]

    @field_validator("criteria")
    @classmethod
    def validate_criteria(cls, value: dict[str, JSONContent | None]) -> dict[str, JSONContent | None]:
        if not value:
            raise ValueError("choice criteria must not be empty")
        if len(value) > CHOICE_MAX_OPTIONS:
            raise ValueError(f"choice criteria may contain at most {CHOICE_MAX_OPTIONS} entries")
        return value


class AskScoreQuestion(_StrictModel):
    type: Literal["score"]
    instructions: JSONContent | None = None
    criteria: list[JSONContent]

    @field_validator("criteria")
    @classmethod
    def validate_criteria(cls, value: list[JSONContent]) -> list[JSONContent]:
        if not SCORE_MIN_LEVELS <= len(value) <= SCORE_MAX_LEVELS:
            raise ValueError(f"score criteria must contain {SCORE_MIN_LEVELS} to {SCORE_MAX_LEVELS} levels")
        return value


AskQuestion: TypeAlias = AskNoulQuestion | AskChoiceQuestion | AskScoreQuestion
AskQuestionField: TypeAlias = Annotated[AskQuestion, Field(discriminator="type")]


def _ask_questions_json_schema() -> dict[str, JSONSchemaValue]:
    """Publish the question union while keeping the runtime argument type permissive."""

    schema_adapter: TypeAdapter[JSONSchemaValue] = TypeAdapter(JSONSchemaValue)
    raw_schema: JSONSchemaValue = schema_adapter.validate_python(
        TypeAdapter(dict[str, AskQuestionField]).json_schema()
    )
    if not isinstance(raw_schema, dict):
        raise TypeError("question schema must be an object")
    raw_definitions = raw_schema.get("$defs", {})
    definitions: dict[str, JSONSchemaValue] = raw_definitions if isinstance(raw_definitions, dict) else {}

    def expand(value: JSONSchemaValue) -> JSONSchemaValue:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                definition_name = reference.rsplit("/", 1)[-1]
                if definition_name == "JSONValue":
                    return {}
                definition = definitions.get(definition_name)
                if definition is not None:
                    return expand(definition)
            expanded: dict[str, JSONSchemaValue] = {}
            for key, item in value.items():
                if key == "$defs":
                    continue
                if key == "discriminator" and isinstance(item, dict):
                    property_name = item.get("propertyName", "type")
                    expanded[key] = {
                        "propertyName": property_name if isinstance(property_name, str) else "type",
                    }
                else:
                    expanded[key] = expand(item)
            return {key: item for key, item in expanded.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    expanded_schema = expand(raw_schema)
    if not isinstance(expanded_schema, dict):
        raise TypeError("question schema must be an object")
    return expanded_schema


AskQuestionsArgument: TypeAlias = Annotated[
    Mapping[str, object],
    WithJsonSchema(_ask_questions_json_schema()),
]


class AskInput(_StrictModel):
    state: StateValue
    questions: dict[str, AskQuestionField]

    @field_validator("questions")
    @classmethod
    def validate_questions(cls, value: dict[str, AskQuestionField]) -> dict[str, AskQuestionField]:
        if not value:
            raise ValueError("at least one question is required")
        if any(not name.strip() for name in value):
            raise ValueError("question names must not be empty")
        return value


class ClassifyInput(_StrictModel):
    state: StateValue
    labels: dict[str, LabelValue]
    question: str | None = None
    add_other: bool = True
    ensemble: bool = False

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, LabelValue]) -> dict[str, LabelValue]:
        if not value:
            raise ValueError("at least one label is required")
        if any(not label.strip() for label in value):
            raise ValueError("label names must not be empty")
        return value

    @model_validator(mode="after")
    def validate_choice_size(self) -> ClassifyInput:
        other_count = 1 if self.add_other and "other" not in self.labels else 0
        if len(self.labels) + other_count > CHOICE_MAX_OPTIONS:
            raise ValueError(f"labels plus other may contain at most {CHOICE_MAX_OPTIONS} entries")
        return self


class ScoreInput(_StrictModel):
    state: StateValue
    levels: list[str]
    question: str

    @field_validator("levels")
    @classmethod
    def validate_levels(cls, value: list[str]) -> list[str]:
        if not SCORE_MIN_LEVELS <= len(value) <= SCORE_MAX_LEVELS:
            raise ValueError(f"levels must contain {SCORE_MIN_LEVELS} to {SCORE_MAX_LEVELS} entries")
        return value


class CheckInput(_StrictModel):
    state: StateValue
    propositions: dict[str, PropositionValue]

    @field_validator("propositions")
    @classmethod
    def validate_propositions(cls, value: dict[str, PropositionValue]) -> dict[str, PropositionValue]:
        if not value:
            raise ValueError("at least one proposition is required")
        if any(not proposition_id.strip() for proposition_id in value):
            raise ValueError("proposition ids must not be empty")
        return value


class MatchInput(_StrictModel):
    query: str
    candidates: dict[str, str]
    question: str | None = None
    window: int = 200

    @field_validator("candidates")
    @classmethod
    def validate_candidates(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("at least one candidate is required")
        if any(not candidate_id.strip() for candidate_id in value):
            raise ValueError("candidate ids must not be empty")
        if "none" in value:
            raise ValueError("candidate id 'none' is reserved")
        return value

    @field_validator("window")
    @classmethod
    def validate_window(cls, value: int) -> int:
        if not MATCH_MIN_WINDOW <= value <= MATCH_MAX_WINDOW:
            raise ValueError(f"window must be between {MATCH_MIN_WINDOW} and {MATCH_MAX_WINDOW}")
        return value


class ScreenInput(_StrictModel):
    text: str
    source: str | None = None


class HealthInput(_StrictModel):
    """Empty model retained so the health tool has an explicit input boundary."""


class UsageData(_StrictModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    input_tokens: int | None = None
    output_tokens: int | None = None


class SystemOneData(_StrictModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    model: str
    usage: UsageData
    answers: dict[str, JSONValue]


class ChoiceAnswerData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    choice: str
    confidence: float
    probabilities: dict[str, float]


class ScoreAnswerData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    score: float
    confidence: float
    legend: dict[str, JSONValue]
    probabilities: dict[str, float]


class NoulAnswerData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    noul: float


class HealthModelData(_StrictModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str
    description: str
    release_date: str


class HealthData(_StrictModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    models: tuple[HealthModelData, ...]


JSON_VALUE_ADAPTER: TypeAdapter[JSONValue] = TypeAdapter(JSONValue)
JSON_CONTENT_ADAPTER: TypeAdapter[JSONContent] = TypeAdapter(JSONContent)


def as_json_value(value: object) -> JSONValue:
    """Validate an SDK/msgspec/Pydantic value as JSON-compatible data."""

    return JSON_VALUE_ADAPTER.validate_python(value)


def as_json_content(value: object) -> JSONContent:
    """Validate a question criterion as TypeSafe JSON content."""

    return JSON_CONTENT_ADAPTER.validate_python(value)
