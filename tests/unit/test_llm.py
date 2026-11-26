"""Unit tests for osu_coach.llm: the Ollama client's requests and how it reads replies,
its errors, and the COACH_LLM setting. Ollama is faked with respx; nothing real is
called. tests/integration/test_coach_llm_live.py runs a real local model.
"""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from osu_coach.llm import (
    LLMError,
    OllamaClient,
    Tool,
    ToolCall,
    client_from_setting,
    tool_result,
)

BASE = "http://ollama.test:11434"
CHAT = f"{BASE}/api/chat"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def ollama() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True) as router:
        yield router


def reply(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"model": "qwen3:8b", "message": message, "done": True})


def sent(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


MESSAGES = [{"role": "user", "content": "Coach peppy."}]
TOOL = Tool("get_skill_profile", "The player's skills.")


# ---------- Requests ----------


async def test_a_plain_request(http: httpx.AsyncClient, ollama: respx.MockRouter) -> None:
    route = ollama.post(CHAT).mock(return_value=reply("hi"))

    await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES)

    # No streaming (one answer), no thinking (slow on a CPU), and nothing else unasked.
    assert sent(route) == {
        "model": "qwen3:8b",
        "messages": MESSAGES,
        "stream": False,
        "think": False,
    }


async def test_tools_are_sent_as_functions(
    http: httpx.AsyncClient, ollama: respx.MockRouter
) -> None:
    route = ollama.post(CHAT).mock(return_value=reply())
    with_args = Tool(
        "search", "Find maps.", {"type": "object", "properties": {"skill": {"type": "string"}}}
    )

    await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES, tools=[TOOL, with_args])

    no_args = {"type": "object", "properties": {}}  # a tool without arguments still has a schema
    assert sent(route)["tools"] == [
        {"type": "function", "function": {"name": "get_skill_profile",
                                          "description": "The player's skills.",
                                          "parameters": no_args}},
        {"type": "function", "function": {"name": "search", "description": "Find maps.",
                                          "parameters": with_args.parameters}},
    ]  # fmt: skip


async def test_a_schema_is_sent_as_the_format(
    http: httpx.AsyncClient, ollama: respx.MockRouter
) -> None:
    route = ollama.post(CHAT).mock(return_value=reply("{}"))
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}

    await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES, schema=schema)

    assert sent(route)["format"] == schema
    assert "tools" not in sent(route)


@pytest.mark.parametrize("base", [BASE, BASE + "/"], ids=["no-slash", "trailing-slash"])
async def test_the_url(http: httpx.AsyncClient, ollama: respx.MockRouter, base: str) -> None:
    route = ollama.post(CHAT).mock(return_value=reply())

    await OllamaClient(http, base, "qwen3:8b").chat(MESSAGES)

    assert route.called  # .../api/chat, with no double slash


def test_its_name_says_provider_and_model() -> None:
    assert OllamaClient(httpx.AsyncClient(), BASE, "qwen3:8b").name == "ollama:qwen3:8b"


# ---------- Replies ----------


async def test_a_text_reply(http: httpx.AsyncClient, ollama: respx.MockRouter) -> None:
    ollama.post(CHAT).mock(return_value=reply("Hello"))

    r = await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES)

    assert (r.text, r.tool_calls) == ("Hello", [])
    assert r.message == {"role": "assistant", "content": "Hello"}  # as it goes back in


async def test_tool_calls(http: httpx.AsyncClient, ollama: respx.MockRouter) -> None:
    calls = [
        {
            "id": "c1",
            "function": {"index": 0, "name": "get_skill_profile", "arguments": {"osu_id": 2}},
        },
        {"id": "c2", "function": {"index": 1, "name": "search_beatmaps"}},  # edge: no arguments
    ]
    ollama.post(CHAT).mock(return_value=reply(tool_calls=calls))

    r = await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES, tools=[TOOL])

    assert r.tool_calls == [
        ToolCall("get_skill_profile", {"osu_id": 2}),
        ToolCall("search_beatmaps", {}),
    ]
    assert r.text == ""
    assert r.message["tool_calls"] == calls  # kept for the conversation


async def test_no_content_is_empty_text(http: httpx.AsyncClient, ollama: respx.MockRouter) -> None:
    # Edge: a reply with tool calls can leave out "content" entirely.
    ollama.post(CHAT).mock(
        return_value=httpx.Response(200, json={"message": {"role": "assistant"}})
    )

    r = await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES)

    assert r.text == ""


# ---------- Errors ----------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": "model 'nope' not found"}),  # model not pulled
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"done": True}),  # no message
        httpx.Response(
            200, json={"message": {"tool_calls": [{"id": "x"}]}}
        ),  # a call with no function
        httpx.Response(200, json={"message": "text"}),  # a message that isn't an object
    ],
    ids=["404", "500", "not-json", "no-message", "broken-tool-call", "message-not-object"],
)
async def test_unusable_answers_are_llm_errors(http: httpx.AsyncClient, ollama: respx.MockRouter,
                                               response: httpx.Response) -> None:  # fmt: skip
    ollama.post(CHAT).mock(return_value=response)

    with pytest.raises(LLMError, match="ollama:qwen3:8b"):
        await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES)


async def test_no_server_is_an_llm_error(http: httpx.AsyncClient, ollama: respx.MockRouter) -> None:
    ollama.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(LLMError):
        await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES)


async def test_a_timeout_is_an_llm_error(http: httpx.AsyncClient, ollama: respx.MockRouter) -> None:
    ollama.post(CHAT).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(LLMError):
        await OllamaClient(http, BASE, "qwen3:8b").chat(MESSAGES)


# ---------- The setting and tool results ----------


@pytest.mark.parametrize(
    ("setting", "model"),
    [
        ("ollama:qwen3:8b", "qwen3:8b"),  # the model's own ":" is kept
        ("  ollama:bonsai-2:9b  ", "bonsai-2:9b"),  # edge: spaces around it
        ("ollama:llama3", "llama3"),
    ],
)
def test_an_ollama_setting(setting: str, model: str) -> None:
    client = client_from_setting(setting, httpx.AsyncClient(), BASE)

    assert isinstance(client, OllamaClient)
    assert (client.model, client.url) == (model, CHAT)


@pytest.mark.parametrize("setting", ["", "   "], ids=["empty", "blank"])
def test_no_setting_no_model(setting: str) -> None:
    assert client_from_setting(setting, httpx.AsyncClient(), BASE) is None


@pytest.mark.parametrize("setting", ["ollama:", "ollama", "openai:gpt-5", "qwen3:8b", "nonsense"])
def test_a_bad_setting_is_refused(setting: str) -> None:
    with pytest.raises(ValueError, match="COACH_LLM"):
        client_from_setting(setting, httpx.AsyncClient(), BASE)


def test_a_tool_result_message() -> None:
    assert tool_result(ToolCall("get_skill_profile", {}), '{"aim": 50}') == {
        "role": "tool",
        "tool_name": "get_skill_profile",
        "content": '{"aim": 50}',
    }
