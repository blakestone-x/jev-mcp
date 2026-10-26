"""MCP tool definitions for typed judgments over a closed answer set."""

from __future__ import annotations

import json
import logging
import math
import os
import random
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import TypeAlias, cast

import msgspec
from mcp.server import MCPServer
from pydantic import BaseModel, ValidationError
from typesafe_sdk import (
    Choice,
    Noul,
    NoulCriteria,
    Score,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeError,
    TypeSafeRateLimitError,
)

from . import questions
from .client import ConfigurationError, JevClient, SDKQuestion, get_client
from .models import (
    AskChoiceQuestion,
    AskInput,
    AskNoulQuestion,
    AskQuestion,
    AskQuestionsArgument,
    AskScoreQuestion,
    CheckInput,
    ChoiceAnswerData,
    ClassifyInput,
    HealthData,
    HealthInput,
    JSONContent,
    JSONValue,
    LabelDescription,
    MatchInput,
    NoulAnswerData,
    PropositionDescription,
    ScoreAnswerData,
    ScoreInput,
    ScreenInput,
    StateValue,
    SystemOneData,
    as_json_content,
    as_json_value,
)

logging.basicConfig(level=logging.WARNING)
logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)
logger = logging.getLogger("jev_mcp")

JSONObject: TypeAlias = dict[str, JSONValue]
ToolStateArgument: TypeAlias = str | dict[str, object] | list[object] | int | float | bool | None
PayloadBuilder: TypeAlias = Callable[[SystemOneData], JSONValue]
EnsemblePayloadBuilder: TypeAlias = Callable[[SystemOneData, SystemOneData], JSONValue]

# MCP 2.2 calls the server class MCPServer; this local name keeps the import
# stable for code that imported the server class from this module.
FastMCP = MCPServer

mcp = FastMCP(
    name="jev",
    version="0.2.1",
    description="Typed judgments over a closed answer set.",
    log_level="WARNING",
)

DEFAULT_PRICE_PER_MTOK = 0.042
DEFAULT_MAX_REQUEST_CHARS = 120_000
DEFAULT_MATCH_MAX_CANDIDATES = 2_000
DEFAULT_MATCH_DEADLINE_SECONDS = 45.0
OUT_OF_OPTIONS_ERROR = "TypeSafe returned an answer outside the submitted options"


class InvalidToolInputError(ValueError):
    """A safe, body-level input error with no caller value in its message."""

    def __init__(self, message: str, *, loc: tuple[str, ...]) -> None:
        super().__init__(message)
        self.message = message
        self.loc = loc


class InvalidToolResponseError(ValueError):
    """A safe error for a provider answer outside the submitted answer set."""

    def __init__(self) -> None:
        super().__init__(OUT_OF_OPTIONS_ERROR)


class MatchDeadlineExceeded(TimeoutError):
    """Raised when a match request reaches its configured wall-clock deadline."""

    def __init__(self, windows_completed: int, windows_total: int) -> None:
        super().__init__("match deadline exceeded")
        self.windows_completed = windows_completed
        self.windows_total = windows_total


def _bounded_loc(parts: object) -> list[JSONValue]:
    """Keep caller-controlled validation path components bounded."""

    if isinstance(parts, tuple):
        values = cast(tuple[object, ...], parts)
        return [str(part)[:64] for part in values]
    if isinstance(parts, list):
        values = cast(list[object], parts)
        return [str(part)[:64] for part in values]
    return [str(parts)[:64]]


def _validation_details(error: ValidationError) -> list[JSONValue]:
    """Keep validation details bounded; loc may contain caller-supplied keys."""

    details: list[JSONValue] = []
    for issue in error.errors()[:10]:
        raw_loc = issue["loc"]
        raw_message = str(issue["msg"])[:160]
        detail: JSONObject = {
            "loc": _bounded_loc(raw_loc),
            "msg": raw_message,
        }
        details.append(detail)
    return details


def _failure(error: Exception) -> JSONObject:
    """Return a short, state-free failure envelope and a safe warning log."""

    if isinstance(error, InvalidToolInputError):
        detail: JSONObject = {"loc": _bounded_loc(error.loc), "msg": error.message}
        result: JSONObject = {
            "ok": False,
            "status": 400,
            "error": "invalid tool input",
            "details": [detail],
        }
    elif isinstance(error, InvalidToolResponseError):
        result = {"ok": False, "status": 502, "error": OUT_OF_OPTIONS_ERROR}
    elif isinstance(error, MatchDeadlineExceeded):
        result = {
            "ok": False,
            "status": 504,
            "error": "match deadline exceeded",
            "windows_completed": error.windows_completed,
            "windows_total": error.windows_total,
        }
    elif isinstance(error, ValidationError):
        result: JSONObject = {
            "ok": False,
            "status": 400,
            "error": "invalid tool input",
            "details": _validation_details(error),
        }
    elif isinstance(error, ConfigurationError):
        result = {"ok": False, "status": 401, "error": str(error)}
    elif isinstance(error, TypeSafeAPITimeoutError):
        result = {"ok": False, "status": 504, "error": "TypeSafe request timed out"}
    elif isinstance(error, TypeSafeAPIConnectionError):
        result = {"ok": False, "status": 503, "error": "TypeSafe service unavailable"}
    elif isinstance(error, TypeSafeAPIResponseValidationError):
        result = {"ok": False, "status": 502, "error": "TypeSafe API response invalid"}
    elif isinstance(error, TypeSafeRateLimitError):
        result = {"ok": False, "status": 429, "error": "TypeSafe API request failed"}
        if error.retry_after_ms is not None:
            result["retry_after_ms"] = error.retry_after_ms
    elif isinstance(error, TypeSafeAPIError):
        result = {"ok": False, "status": error.status, "error": "TypeSafe API request failed"}
    elif isinstance(error, TypeSafeError):
        result = {"ok": False, "status": 500, "error": "TypeSafe client error"}
    else:
        result = {"ok": False, "status": 500, "error": "Jev tool failed"}
    logger.warning("Jev tool failed (%s)", type(error).__name__)
    return result


def _response_data(response: SystemOneResponse) -> SystemOneData:
    """Convert the SDK's frozen response into a typed JSON boundary."""

    builtins: object = msgspec.to_builtins(response, str_keys=True, enc_hook=_sdk_enc_hook)
    return SystemOneData.model_validate(builtins)


def _sdk_enc_hook(value: object) -> object:
    """Convert SDK Pydantic models using their wire serializer."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    raise TypeError(f"unsupported SDK value type: {type(value).__name__}")


def _latency_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)


def _price_per_mtok() -> float:
    raw_price = os.environ.get("JEV_MCP_PRICE_PER_MTOK", "").strip()
    if not raw_price:
        return DEFAULT_PRICE_PER_MTOK
    try:
        price = float(raw_price)
    except ValueError:
        return DEFAULT_PRICE_PER_MTOK
    return price if math.isfinite(price) and price >= 0 else DEFAULT_PRICE_PER_MTOK


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _nonnegative_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _max_request_chars() -> int:
    for name in ("JEV_MCP_MAX_REQUEST_CHARS", "JEV_MCP_MAX_STATE_CHARS"):
        if os.environ.get(name, "").strip():
            return _positive_int_env(name, DEFAULT_MAX_REQUEST_CHARS)
    return DEFAULT_MAX_REQUEST_CHARS


def _match_max_candidates() -> int:
    return _positive_int_env("JEV_MCP_MATCH_MAX_CANDIDATES", DEFAULT_MATCH_MAX_CANDIDATES)


def _match_deadline_seconds() -> float:
    return _nonnegative_float_env("JEV_MCP_MATCH_DEADLINE_S", DEFAULT_MATCH_DEADLINE_SECONDS)


def _serialized_char_count(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _request_char_count(
    state: StateValue,
    questions_to_ask: Mapping[str, SDKQuestion],
) -> int:
    built_questions: object = msgspec.to_builtins(
        questions_to_ask,
        str_keys=True,
        enc_hook=_sdk_enc_hook,
    )
    return _serialized_char_count({"state": state, "questions": built_questions})


def _validate_request_size(
    state: StateValue,
    questions_to_ask: Mapping[str, SDKQuestion],
    *,
    loc: tuple[str, ...] = ("request",),
) -> None:
    limit = _max_request_chars()
    if _request_char_count(state, questions_to_ask) > limit:
        raise InvalidToolInputError(
            f"request exceeds JEV_MCP_MAX_REQUEST_CHARS limit of {limit} characters",
            loc=loc,
        )


def _validate_match_total_size(request: MatchInput) -> None:
    limit = _max_request_chars()
    if _serialized_char_count(request.model_dump(mode="json")) > limit:
        raise InvalidToolInputError(
            f"request exceeds JEV_MCP_MAX_REQUEST_CHARS limit of {limit} characters",
            loc=("request",),
        )


def _check_match_deadline(deadline: float, *, windows_completed: int, windows_total: int) -> None:
    if perf_counter() >= deadline:
        raise MatchDeadlineExceeded(windows_completed, windows_total)


def _success(
    data: SystemOneData,
    answers: JSONValue,
    started: float,
    *,
    additional_data: tuple[SystemOneData, ...] = (),
) -> JSONObject:
    """Build a success envelope; the default is the published early-access input price and may change."""

    all_data = (data, *additional_data)
    input_tokens = sum(item.usage.input_tokens or 0 for item in all_data)
    output_tokens = sum(item.usage.output_tokens or 0 for item in all_data)
    usage: JSONObject = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "est_cost_usd": input_tokens * _price_per_mtok() / 1_000_000,
    }
    return {
        "ok": True,
        "model": data.model,
        "answers": answers,
        "usage": usage,
        "latency_ms": _latency_ms(started),
    }


def _run_evaluation(
    *,
    state: StateValue,
    questions_to_ask: Mapping[str, SDKQuestion],
    build_payload: PayloadBuilder,
    started: float,
) -> JSONObject:
    try:
        _validate_request_size(state, questions_to_ask)
        response = get_client().evaluate(state=state, questions=questions_to_ask)
        data = _response_data(response)
        return _success(data, build_payload(data), started)
    except Exception as error:
        return _failure(error)


def _run_ensemble_evaluation(
    *,
    state: StateValue,
    first_questions: Mapping[str, SDKQuestion],
    second_questions: Mapping[str, SDKQuestion],
    build_payload: EnsemblePayloadBuilder,
    started: float,
) -> JSONObject:
    try:
        _validate_request_size(state, first_questions)
        _validate_request_size(state, second_questions)
        client = get_client()
        first_data = _response_data(client.evaluate(state=state, questions=first_questions))
        second_data = _response_data(client.evaluate(state=state, questions=second_questions))
        return _success(
            first_data,
            build_payload(first_data, second_data),
            started,
            additional_data=(second_data,),
        )
    except Exception as error:
        return _failure(error)


def _answer(data: SystemOneData, question_id: str) -> dict[str, JSONValue]:
    raw = data.answers.get(question_id)
    if not isinstance(raw, dict):
        raise ValueError(f"missing answer {question_id}")
    return raw


def _choice_answer(data: SystemOneData, question_id: str) -> ChoiceAnswerData:
    return ChoiceAnswerData.model_validate(_answer(data, question_id))


def _score_answer(data: SystemOneData, question_id: str) -> ScoreAnswerData:
    return ScoreAnswerData.model_validate(_answer(data, question_id))


def _noul_answer(data: SystemOneData, question_id: str) -> NoulAnswerData:
    return NoulAnswerData.model_validate(_answer(data, question_id))


def _validate_choice_answer(choice: ChoiceAnswerData, allowed: Collection[str]) -> None:
    if choice.choice not in allowed:
        raise InvalidToolResponseError()


def _validate_score_answer(score: ScoreAnswerData, levels: Collection[str]) -> None:
    allowed = {str(index) for index, _ in enumerate(levels)}
    if not set(score.legend).issubset(allowed):
        raise InvalidToolResponseError()


def _validate_ask_answers(data: SystemOneData, asked_questions: Mapping[str, AskQuestion]) -> None:
    for question_id, question in asked_questions.items():
        if isinstance(question, AskChoiceQuestion):
            _validate_choice_answer(_choice_answer(data, question_id), question.criteria)


def _label_criteria(labels: Mapping[str, str | LabelDescription]) -> dict[str, JSONContent | None]:
    criteria: dict[str, JSONContent | None] = {}
    for label, description in labels.items():
        if isinstance(description, LabelDescription):
            criteria[label] = as_json_content(description.model_dump(mode="json"))
        else:
            criteria[label] = description
    return criteria


def _proposition_question(value: str | PropositionDescription) -> Noul:
    if isinstance(value, PropositionDescription):
        return Noul(
            instructions=value.statement,
            criteria={"true": value.true, "false": value.false},
        )
    return Noul(instructions=value)


def _sdk_noul(question: AskNoulQuestion) -> Noul:
    criteria: NoulCriteria | None = None
    if question.criteria is not None:
        criteria = {}
        if "true" in question.criteria:
            criteria["true"] = question.criteria["true"]
        if "false" in question.criteria:
            criteria["false"] = question.criteria["false"]
    return Noul(instructions=question.instructions, criteria=criteria)


def _sdk_question(question: AskQuestion) -> SDKQuestion:
    if isinstance(question, AskChoiceQuestion):
        return Choice(instructions=question.instructions, criteria=question.criteria)
    if isinstance(question, AskScoreQuestion):
        return Score(instructions=question.instructions, criteria=question.criteria)
    return _sdk_noul(question)


def _sdk_questions(raw_questions: Mapping[str, AskQuestion]) -> dict[str, SDKQuestion]:
    return {name: _sdk_question(question) for name, question in raw_questions.items()}


def _static_score(criteria: list[str], instructions: str) -> Score:
    return Score(
        instructions=instructions,
        criteria=[as_json_content(level) for level in criteria],
    )


def _static_noul(instructions: str, criteria: Mapping[str, object]) -> Noul:
    noul_criteria: NoulCriteria = {}
    for name, description in criteria.items():
        if name == "true":
            noul_criteria["true"] = as_json_content(description)
        elif name == "false":
            noul_criteria["false"] = as_json_content(description)
    return Noul(
        instructions=instructions,
        criteria=noul_criteria,
    )


def _static_noul_definition(definition: object) -> Noul:
    parsed = as_json_value(definition)
    if not isinstance(parsed, dict):
        raise ValueError("static Noul definition must be an object")
    instructions = parsed.get("instructions")
    criteria = parsed.get("criteria")
    if not isinstance(instructions, str) or not isinstance(criteria, dict):
        raise ValueError("static Noul definition is incomplete")
    return _static_noul(instructions, criteria)


def _screen_questions() -> dict[str, SDKQuestion]:
    result: dict[str, SDKQuestion] = {
        name: _static_noul_definition(definition) for name, definition in questions.SCREEN_NOULS.items()
    }
    result["risk"] = _static_score(questions.SCREEN_RISK_LEVELS, questions.SCREEN_RISK_INSTRUCTIONS)
    return result


def _choice_payload(data: ChoiceAnswerData, *, band: str | None = None) -> JSONObject:
    payload: JSONObject = {
        "choice": data.choice,
        "confidence": data.confidence,
        "probabilities": as_json_value(data.probabilities),
    }
    if band is not None:
        payload["band"] = band
    return payload


def _score_payload(data: ScoreAnswerData, *, normalized: float | None = None) -> JSONObject:
    payload: JSONObject = {
        "score": data.score,
        "confidence": data.confidence,
        "probabilities": as_json_value(data.probabilities),
        "legend": as_json_value(data.legend),
    }
    if normalized is not None:
        payload["normalized"] = normalized
    return payload


def _noul_values(data: SystemOneData, question_ids: list[str]) -> dict[str, float]:
    return {question_id: _noul_answer(data, question_id).noul for question_id in question_ids}


def _state_object(value: Mapping[str, object]) -> StateValue:
    parsed = as_json_value(dict(value))
    if isinstance(parsed, (str, dict, list)):
        return parsed
    raise ValueError("state must be a string, object, or array")


def _classify_band(confidence: float) -> str:
    if confidence >= questions.CLASSIFY_BAND_THRESHOLDS["act"]:
        return "act"
    if confidence >= questions.CLASSIFY_BAND_THRESHOLDS["verify"]:
        return "verify"
    return "review"


def _shuffled_mapping(criteria: Mapping[str, JSONContent | None]) -> dict[str, JSONContent | None]:
    items = list(criteria.items())
    if len(items) > 1:
        random.Random(len(items)).shuffle(items)
        if [key for key, _ in items] == list(criteria):
            items.reverse()
    return dict(items)


def _match_choice_question(
    candidates: Mapping[str, str],
    instruction: str | None,
) -> Choice:
    criteria: dict[str, JSONContent | None] = {
        candidate_id: candidate_text for candidate_id, candidate_text in candidates.items()
    }
    criteria["none"] = questions.MATCH_NONE_DESCRIPTION
    return Choice(
        instructions=instruction or questions.MATCH_DEFAULT_INSTRUCTIONS,
        criteria=criteria,
    )


def _match_questions(
    candidates: Mapping[str, str],
    instruction: str | None,
) -> dict[str, SDKQuestion]:
    return {
        "choice": _match_choice_question(candidates, instruction),
        "exists": _static_noul(
            questions.MATCH_EXISTS_INSTRUCTIONS,
            questions.MATCH_EXISTS_CRITERIA,
        ),
    }


@dataclass(frozen=True)
class WindowMatch:
    candidates: dict[str, str]
    choice: ChoiceAnswerData
    exists: float


def _selected_candidate(choice: ChoiceAnswerData, candidates: Mapping[str, str]) -> str | None:
    if choice.choice == "none":
        return None
    if choice.choice not in candidates:
        raise InvalidToolResponseError()
    return choice.choice


def _top3(choice: ChoiceAnswerData, candidates: Mapping[str, str]) -> list[JSONValue]:
    ranked = sorted(
        (
            (candidate_id, probability)
            for candidate_id, probability in choice.probabilities.items()
            if candidate_id != "none" and candidate_id in candidates
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return [
        {"id": candidate_id, "probability": probability}
        for candidate_id, probability in ranked[:3]
    ]


def _evaluate_match_window(
    client: JevClient,
    query: str,
    candidates: dict[str, str],
    instruction: str | None,
) -> tuple[SystemOneData, WindowMatch]:
    state = _state_object({"query": query, "candidates": candidates})
    questions_to_ask = _match_questions(candidates, instruction)
    _validate_request_size(state, questions_to_ask, loc=("request", "window"))
    data = _response_data(client.evaluate(state=state, questions=questions_to_ask))
    choice = _choice_answer(data, "choice")
    exists = _noul_answer(data, "exists").noul
    _selected_candidate(choice, candidates)
    return data, WindowMatch(candidates=candidates, choice=choice, exists=exists)


def _final_match_choice(
    client: JevClient,
    query: str,
    candidates: dict[str, str],
    instruction: str | None,
    responses: list[SystemOneData],
    *,
    deadline: float | None = None,
    windows_completed: int = 0,
    windows_total: int = 0,
) -> tuple[ChoiceAnswerData | None, dict[str, str]]:
    def check_deadline() -> None:
        if deadline is not None:
            _check_match_deadline(
                deadline,
                windows_completed=windows_completed,
                windows_total=windows_total,
            )

    current = candidates
    while len(current) > questions.CHOICE_MAX_OPTIONS - 1:
        reduced: dict[str, str] = {}
        items = list(current.items())
        for start in range(0, len(items), questions.CHOICE_MAX_OPTIONS - 1):
            group = dict(items[start : start + questions.CHOICE_MAX_OPTIONS - 1])
            check_deadline()
            state = _state_object({"query": query, "candidates": group})
            questions_to_ask = {"choice": _match_choice_question(group, instruction)}
            _validate_request_size(state, questions_to_ask, loc=("request", "window"))
            data = _response_data(
                client.evaluate(
                    state=state,
                    questions=questions_to_ask,
                )
            )
            responses.append(data)
            check_deadline()
            choice = _choice_answer(data, "choice")
            selected = _selected_candidate(choice, group)
            if selected is not None:
                reduced[selected] = group[selected]
        if not reduced:
            return None, current
        current = reduced

    check_deadline()
    state = _state_object({"query": query, "candidates": current})
    questions_to_ask = {"choice": _match_choice_question(current, instruction)}
    _validate_request_size(state, questions_to_ask, loc=("request", "window"))
    data = _response_data(
        client.evaluate(
            state=state,
            questions=questions_to_ask,
        )
    )
    responses.append(data)
    check_deadline()
    choice = _choice_answer(data, "choice")
    _selected_candidate(choice, current)
    return choice, current


@mcp.tool()
def jev_ask(state: ToolStateArgument, questions: AskQuestionsArgument) -> JSONObject:
    """Use Jev for typed judgments over supplied state; it is not for text generation or numeric extraction.

    Confidence is distribution concentration, not correctness.
    The default est_cost_usd uses the published early-access input price and may change.
    """

    started = perf_counter()
    try:
        request = AskInput.model_validate({"state": state, "questions": questions})
        sdk_questions = _sdk_questions(request.questions)
    except Exception as error:
        return _failure(error)

    def build_payload(data: SystemOneData) -> JSONValue:
        _validate_ask_answers(data, request.questions)
        return data.answers

    return _run_evaluation(
        state=request.state,
        questions_to_ask=sdk_questions,
        build_payload=build_payload,
        started=started,
    )


@mcp.tool()
def jev_classify(
    state: ToolStateArgument,
    labels: Mapping[str, object],
    question: str | None = None,
    add_other: bool = True,
    ensemble: bool = False,
) -> JSONObject:
    """Use Jev to select one label from a closed list; it is not for text generation or numeric extraction.

    Confidence is distribution concentration, not correctness. With order-ensemble enabled, order flips
    concentrate at low confidence, so disagreement is a second, cheap reason to route to a person.
    The default est_cost_usd uses the published early-access input price and may change.
    """

    started = perf_counter()
    try:
        request = ClassifyInput.model_validate(
            {
                "state": state,
                "labels": labels,
                "question": question,
                "add_other": add_other,
                "ensemble": ensemble,
            }
        )
        criteria = _label_criteria(request.labels)
        if request.add_other and "other" not in request.labels:
            criteria["other"] = questions.CLASSIFY_OTHER_DESCRIPTION
        first_questions = {
            "choice": Choice(
                instructions=request.question or questions.CLASSIFY_DEFAULT_INSTRUCTIONS,
                criteria=criteria,
            )
        }
    except Exception as error:
        return _failure(error)

    if not request.ensemble:
        allowed_choices = set(criteria)

        def build_payload(data: SystemOneData) -> JSONValue:
            choice = _choice_answer(data, "choice")
            _validate_choice_answer(choice, allowed_choices)
            return _choice_payload(choice, band=_classify_band(choice.confidence))

        return _run_evaluation(
            state=request.state,
            questions_to_ask=first_questions,
            build_payload=build_payload,
            started=started,
        )

    second_questions = {
        "choice": Choice(
            instructions=request.question or questions.CLASSIFY_DEFAULT_INSTRUCTIONS,
            criteria=_shuffled_mapping(criteria),
        )
    }

    allowed_choices = set(criteria)

    def build_ensemble_payload(first_data: SystemOneData, second_data: SystemOneData) -> JSONValue:
        primary = _choice_answer(first_data, "choice")
        alternate = _choice_answer(second_data, "choice")
        _validate_choice_answer(primary, allowed_choices)
        _validate_choice_answer(alternate, allowed_choices)
        agreement = primary.choice == alternate.choice
        payload = _choice_payload(primary, band=_classify_band(primary.confidence) if agreement else "review")
        payload["agreement"] = agreement
        payload["alternate_choice"] = alternate.choice
        return payload

    return _run_ensemble_evaluation(
        state=request.state,
        first_questions=first_questions,
        second_questions=second_questions,
        build_payload=build_ensemble_payload,
        started=started,
    )


@mcp.tool()
def jev_score(state: ToolStateArgument, levels: list[str], question: str) -> JSONObject:
    """Use Jev to place state on an ordered situation rubric; it is not for text generation or numeric extraction.

    Confidence is distribution concentration, not correctness.
    The default est_cost_usd uses the published early-access input price and may change.
    """

    started = perf_counter()
    try:
        request = ScoreInput.model_validate({"state": state, "levels": levels, "question": question})
        sdk_questions = {
            "score": Score(
                instructions=request.question,
                criteria=[as_json_content(level) for level in request.levels],
            )
        }
    except Exception as error:
        return _failure(error)

    def build_payload(data: SystemOneData) -> JSONValue:
        score = _score_answer(data, "score")
        _validate_score_answer(score, request.levels)
        return _score_payload(score, normalized=score.score / (len(request.levels) - 1))

    return _run_evaluation(
        state=request.state,
        questions_to_ask=sdk_questions,
        build_payload=build_payload,
        started=started,
    )


@mcp.tool()
def jev_check(
    state: ToolStateArgument,
    propositions: Mapping[str, object],
) -> JSONObject:
    """Use Jev for independent yes/no checks over state; it is not for text generation or numeric extraction.

    Confidence is distribution concentration; Noul probability is evidence strength.
    The default est_cost_usd uses the published early-access input price and may change.
    """

    started = perf_counter()
    try:
        request = CheckInput.model_validate({"state": state, "propositions": propositions})
        sdk_questions = {
            proposition_id: _proposition_question(proposition)
            for proposition_id, proposition in request.propositions.items()
        }
    except Exception as error:
        return _failure(error)

    def build_payload(data: SystemOneData) -> JSONValue:
        probabilities = _noul_values(data, list(request.propositions))
        flags = [
            proposition_id
            for proposition_id, probability in probabilities.items()
            if probability >= questions.CHECK_FLAG_THRESHOLD
        ]
        uncertain = [
            proposition_id
            for proposition_id, probability in probabilities.items()
            if questions.CHECK_UNCERTAIN_MIN <= probability < questions.CHECK_UNCERTAIN_MAX
        ]
        return as_json_value(
            {
                "probabilities": probabilities,
                "flags": flags,
                "uncertain": uncertain,
            }
        )

    return _run_evaluation(
        state=request.state,
        questions_to_ask=sdk_questions,
        build_payload=build_payload,
        started=started,
    )


@mcp.tool()
def jev_match(
    query: str,
    candidates: Mapping[str, object],
    question: str | None = None,
    window: int = 200,
) -> JSONObject:
    """Find the best candidate with an explicit abstention signal; it is not text generation.

    Candidates are evaluated in windows, and ``exists`` is the maximum of the
    per-window Noul probabilities. More windows can therefore inflate ``exists``;
    inspect ``exists_by_window`` and ``windows`` before using the result. The
    default est_cost_usd uses the published early-access input price and may change.
    """

    started = perf_counter()
    try:
        request = MatchInput.model_validate(
            {
                "query": query,
                "candidates": candidates,
                "question": question,
                "window": window,
            }
        )
    except Exception as error:
        return _failure(error)

    try:
        _validate_match_total_size(request)
        max_candidates = _match_max_candidates()
        if len(request.candidates) > max_candidates:
            raise InvalidToolInputError(
                f"candidates exceeds JEV_MCP_MATCH_MAX_CANDIDATES limit of {max_candidates} entries",
                loc=("candidates",),
            )
        client = get_client()
        items: list[tuple[str, str]] = list(request.candidates.items())
        chunk_size = min(request.window, questions.MATCH_MAX_WINDOW)
        chunks: list[list[tuple[str, str]]] = [
            items[start : start + chunk_size] for start in range(0, len(items), chunk_size)
        ]

        deadline = started + _match_deadline_seconds()
        responses: list[SystemOneData] = []
        window_matches: list[WindowMatch] = []
        for chunk in chunks:
            _check_match_deadline(
                deadline,
                windows_completed=len(window_matches),
                windows_total=len(chunks),
            )
            data, match = _evaluate_match_window(client, request.query, dict(chunk), request.question)
            responses.append(data)
            window_matches.append(match)
            _check_match_deadline(
                deadline,
                windows_completed=len(window_matches),
                windows_total=len(chunks),
            )

        source_match = max(window_matches, key=lambda match: match.exists)
        finalist_candidates: dict[str, str] = {}
        for match in window_matches:
            selected = _selected_candidate(match.choice, match.candidates)
            if selected is not None:
                finalist_candidates[selected] = match.candidates[selected]

        if len(window_matches) > 1 and finalist_candidates:
            final_choice, final_candidates = _final_match_choice(
                client,
                request.query,
                finalist_candidates,
                request.question,
                responses,
                deadline=deadline,
                windows_completed=len(window_matches),
                windows_total=len(chunks),
            )
            if final_choice is None:
                selected = None
                confidence = None
                top3 = []
                abstain_probability = 1.0
            else:
                selected = _selected_candidate(final_choice, final_candidates)
                confidence = final_choice.confidence if selected is not None else None
                top3 = _top3(final_choice, final_candidates)
                abstain_probability = final_choice.probabilities.get("none", 0.0)
        else:
            final_choice = source_match.choice
            selected = _selected_candidate(final_choice, source_match.candidates)
            confidence = final_choice.confidence if selected is not None else None
            top3 = _top3(final_choice, source_match.candidates)
            abstain_probability = final_choice.probabilities.get("none", 0.0)

        exists = max(match.exists for match in window_matches)
        if final_choice is None:
            band = "none"
        elif selected is not None and confidence is not None and exists >= 0.70 and confidence >= 0.50:
            band = "match"
        elif exists >= 0.30:
            band = "review"
        else:
            band = "none"

        answers = as_json_value(
            {
                "best_id": selected,
                "best_text": request.candidates.get(selected) if selected is not None else None,
                "confidence": confidence,
                "abstain_probability": abstain_probability,
                "exists": exists,
                "windows": len(window_matches),
                "exists_by_window": [match.exists for match in window_matches],
                "top3": top3,
                "band": band,
            }
        )
        return _success(responses[0], answers, started, additional_data=tuple(responses[1:]))
    except Exception as error:
        return _failure(error)


@mcp.tool()
def jev_screen(text: ToolStateArgument, source: str | None = None) -> JSONObject:
    """Use Jev to screen untrusted text; it is a filter, not a security boundary.

    Confidence is distribution concentration, not correctness.
    The default est_cost_usd uses the published early-access input price and may change.
    """

    started = perf_counter()
    try:
        request = ScreenInput.model_validate({"text": text, "source": source})
        state: StateValue = {"text": request.text}
        if request.source is not None:
            state["source"] = request.source
        sdk_questions = _screen_questions()
    except Exception as error:
        return _failure(error)

    def build_payload(data: SystemOneData) -> JSONValue:
        noul_ids = list(questions.SCREEN_NOULS)
        nouls = _noul_values(data, noul_ids)
        risk = _score_answer(data, "risk")
        if risk.score >= questions.SCREEN_VERDICT_THRESHOLDS["block_risk"]:
            verdict = "block"
        elif (
            risk.score >= questions.SCREEN_VERDICT_THRESHOLDS["review_risk"]
            or any(probability >= questions.SCREEN_VERDICT_THRESHOLDS["review_noul"] for probability in nouls.values())
        ):
            verdict = "review"
        else:
            verdict = "pass"
        reasons = {
            question_id: probability
            for question_id, probability in nouls.items()
            if probability >= questions.SCREEN_VERDICT_THRESHOLDS["review_noul"]
        }
        return as_json_value(
            {
                "verdict": verdict,
                "risk": risk.score,
                "risk_confidence": risk.confidence,
                "reasons": reasons,
            }
        )

    return _run_evaluation(
        state=state,
        questions_to_ask=sdk_questions,
        build_payload=build_payload,
        started=started,
    )


@mcp.tool()
def jev_health() -> JSONObject:
    """Use Jev health to inspect the configured model and the account's available models and release dates.

    Health checks report zero estimated cost; the published early-access input price may change for other tools.
    """

    started = perf_counter()
    try:
        HealthInput()
        client = get_client()
        response = client.list_models()
        builtins: object = msgspec.to_builtins(response, str_keys=True, enc_hook=_sdk_enc_hook)
        health = HealthData.model_validate(builtins)
        if not health.models:
            raise ValueError("TypeSafe service returned no models")
        model_data = [as_json_value(model.model_dump(mode="json")) for model in health.models]
        return {
            "ok": True,
            "model": client.model,
            "answers": {
                "models": model_data,
                "round_trip_latency_ms": _latency_ms(started),
            },
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "est_cost_usd": 0.0,
            },
            "latency_ms": _latency_ms(started),
        }
    except Exception as error:
        return _failure(error)


__all__ = [
    "FastMCP",
    "jev_ask",
    "jev_check",
    "jev_classify",
    "jev_health",
    "jev_match",
    "jev_score",
    "jev_screen",
    "mcp",
]
