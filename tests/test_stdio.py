"""Regression tests for validation and registration at the MCP stdio boundary."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TOOLS = {"jev_ask", "jev_classify", "jev_score", "jev_check", "jev_match", "jev_screen", "jev_health"}


def _as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object")
    return cast(dict[str, object], value)


def _as_object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return cast(list[object], value)


def _server_parameters(environment: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "jev_mcp"],
        cwd=str(REPO_ROOT),
        env=environment,
    )


def _text_result(result: CallToolResult) -> str:
    text_blocks = [block.text for block in result.content if isinstance(block, TextContent)]
    assert len(text_blocks) == 1
    return text_blocks[0]


async def _assert_invalid_inputs_are_safe() -> None:
    environment = dict(os.environ)
    environment["TYPESAFE_API_KEY"] = "dummy"
    cases: list[tuple[str, dict[str, object], tuple[str, ...]]] = [
        (
            "jev_ask",
            {
                "state": "900000001",
                "questions": {
                    "choice": {
                        "type": "choice",
                        "criteria": {str(index): "CHOICE_INPUT_SENTINEL_256" for index in range(256)},
                    }
                },
            },
            ("900000001", "CHOICE_INPUT_SENTINEL_256"),
        ),
        (
            "jev_ask",
            {
                "state": 900000002,
                "questions": {
                    "choice": {"type": "choice", "criteria": {"yes": "yes"}},
                },
            },
            ("900000002",),
        ),
        (
            "jev_classify",
            {
                "state": "CLASSIFY_STATE_INPUT_SENTINEL",
                "labels": {str(index): "label" for index in range(256)},
            },
            ("CLASSIFY_STATE_INPUT_SENTINEL",),
        ),
        (
            "jev_classify",
            {"state": 900000003, "labels": {"one": "one"}},
            ("900000003",),
        ),
        (
            "jev_classify",
            {"state": "state", "labels": {"one": 900000004}},
            ("900000004",),
        ),
        (
            "jev_score",
            {
                "state": "SCORE_STATE_INPUT_SENTINEL",
                "levels": ["only"],
                "question": "SCORE_QUESTION_INPUT_SENTINEL",
            },
            ("SCORE_STATE_INPUT_SENTINEL", "SCORE_QUESTION_INPUT_SENTINEL"),
        ),
        (
            "jev_score",
            {"state": 900000005, "levels": ["low", "high"], "question": "question"},
            ("900000005",),
        ),
        (
            "jev_check",
            {"state": 900000006, "propositions": {"flags": "flag"}},
            ("900000006",),
        ),
        (
            "jev_check",
            {"state": "state", "propositions": {"flags": 900000007}},
            ("900000007",),
        ),
        (
            "jev_match",
            {"query": "query", "candidates": {"candidate": 900000009}},
            ("900000009",),
        ),
        (
            "jev_screen",
            {"text": 900000008},
            ("900000008",),
        ),
    ]

    async with stdio_client(_server_parameters(environment)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for name, arguments, forbidden_values in cases:
                result = await session.call_tool(name, arguments)
                assert isinstance(result, CallToolResult)
                assert result.is_error is False
                envelope = _as_object(json.loads(_text_result(result)))
                assert envelope["ok"] is False
                assert envelope["status"] == 400
                assert envelope["error"] == "invalid tool input"
                details = envelope["details"]
                assert isinstance(details, list)
                assert set(_as_object(cast(object, details[0]))) == {"loc", "msg"}
                response_text = _text_result(result)
                for forbidden_value in forbidden_values:
                    assert forbidden_value not in response_text


async def _assert_missing_key_and_tool_list() -> None:
    environment = dict(os.environ)
    environment.pop("TYPESAFE_API_KEY", None)

    async with stdio_client(_server_parameters(environment)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == PUBLIC_TOOLS

            result = await session.call_tool(
                "jev_classify",
                {"state": "state", "labels": {"one": "one"}},
            )
            assert isinstance(result, CallToolResult)
            assert result.is_error is False
            assert _as_object(json.loads(_text_result(result))) == {
                "ok": False,
                "status": 401,
                "error": "TYPESAFE_API_KEY is not set",
            }
            assert "TYPESAFE_API_KEY is not set" in json.loads(_text_result(result))["error"]


async def _assert_ask_schema_publishes_question_union() -> None:
    environment = dict(os.environ)
    environment.pop("TYPESAFE_API_KEY", None)

    async with stdio_client(_server_parameters(environment)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            ask_tool = next(tool for tool in listed.tools if tool.name == "jev_ask")
            properties = _as_object(ask_tool.input_schema["properties"])
            questions_schema = _as_object(properties["questions"])
            question_schema = _as_object(questions_schema["additionalProperties"])
            assert question_schema["discriminator"] == {"propertyName": "type"}
            variants = _as_object_list(question_schema["oneOf"])
            assert {_as_object(variant)["title"] for variant in variants} == {
                "AskChoiceQuestion",
                "AskNoulQuestion",
                "AskScoreQuestion",
            }


def test_invalid_tool_inputs_through_stdio_use_safe_envelopes() -> None:
    asyncio.run(_assert_invalid_inputs_are_safe())


def test_missing_key_still_lists_tools_and_returns_401() -> None:
    asyncio.run(_assert_missing_key_and_tool_list())


def test_ask_schema_publishes_discriminated_question_shapes() -> None:
    asyncio.run(_assert_ask_schema_publishes_question_union())
