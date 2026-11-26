"""A small interface to chat models, so the coach doesn't depend on one provider.

The coach needs two things from a model: calling tools (to look things up), and writing
JSON that matches a schema (the report's words). `LLMClient` is that; each provider is one
class behind it, picked by the COACH_LLM setting ("provider:model"). Ollama runs open
models locally, for free; a paid provider can be added the same way.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# A chat message, in the shape both Ollama and most APIs use:
# {"role": "system" | "user" | "assistant" | "tool", "content": "...", ...}
Message = dict[str, Any]


class LLMError(Exception):
    """The model couldn't be reached, or answered with something unusable."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Tool:
    """A function the model may call: its name, what it does, and its arguments' schema."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: list[ToolCall]
    message: Message  # the reply as it goes back into the conversation


class LLMClient(Protocol):
    name: str  # e.g. "ollama:qwen3:8b", for the report to say who wrote it

    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Reply:
        """One turn: the model's reply to the conversation so far. With `tools`, it may
        ask for tool calls instead of answering; with `schema`, its text is JSON matching
        it."""
        ...


class OllamaClient:
    """A model served by a local Ollama (https://ollama.com), through its chat API."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, model: str) -> None:
        self.http = http
        self.url = base_url.rstrip("/") + "/api/chat"
        self.model = model
        self.name = f"ollama:{model}"

    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Thinking models (qwen3) reason before answering; on a laptop's CPU that
            # multiplies the time, and the coach's job is writing, not puzzles.
            "think": False,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if schema is not None:
            body["format"] = schema
        try:
            response = await self.http.post(self.url, json=body)
            response.raise_for_status()
            # Read it all here: an answer in any other shape is the model's failure too.
            message: Message = response.json()["message"]
            calls = [
                ToolCall(c["function"]["name"], c["function"].get("arguments") or {})
                for c in message.get("tool_calls") or []
            ]
            text = message.get("content") or ""
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise LLMError(f"{self.name}: {e!r}") from e
        return Reply(text=text, tool_calls=calls, message=message)


def client_from_setting(setting: str, http: httpx.AsyncClient, ollama_url: str) -> LLMClient | None:
    """The client COACH_LLM names ("ollama:qwen3:8b"), or None when it's empty."""
    if not setting.strip():
        return None
    provider, _, model = setting.strip().partition(":")
    if provider == "ollama" and model:
        return OllamaClient(http, ollama_url, model)
    raise ValueError(
        f"COACH_LLM: unknown provider or no model in {setting!r} (try ollama:qwen3:8b)"
    )


def tool_result(call: ToolCall, content: str) -> Message:
    """A tool's answer, as the message that goes back to the model."""
    return {"role": "tool", "tool_name": call.name, "content": content}
