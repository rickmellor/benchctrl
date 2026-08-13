"""Minimal OpenAI-compatible chat client, stdlib only.

Talks to whatever is listening on localhost — ollama on the board, or a
stub in tests. ``urllib`` rather than ``httpx`` because the agent must
install with nothing but ``pyserial``; the board has no pip and the MCP
stack's dependencies ship compiled wheels for the wrong architecture.

Sized for the hardware it runs on. At roughly 5.3 tok/s prompt processing
and 3.3 tok/s generation, a 400-token prompt costs about 75 s before the
first output token appears and 120 output tokens another 36 s — call it two
minutes per turn. Every default here follows from that arithmetic.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("benchctrl.agent.llm.client")

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"

#: Deliberately coarse: this is not a chat interface, it is a slow advisor.
DEFAULT_TIMEOUT_S = 180.0


class LLMUnavailable(RuntimeError):
    """No inference backend is reachable.

    Not an error worth failing a run over — the model is advisory, and a run
    with no annotations is still a complete run.
    """


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    elapsed_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated: bool = False

    @property
    def tokens_per_s(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        return self.completion_tokens / self.elapsed_s


class LLMClient:
    """A very small OpenAI-compatible chat client."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        api_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key

    # --- availability ---------------------------------------------------

    def available(self, timeout_s: float = 2.0) -> bool:
        """Whether a backend is reachable, cheaply."""
        try:
            self._get("/models", timeout_s=timeout_s)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("llm backend not available: %r", exc)
            return False

    def models(self) -> list[str]:
        try:
            payload = self._get("/models")
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"could not list models: {exc}") from exc
        return [m.get("id", "") for m in payload.get("data", [])]

    # --- completion -----------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        *,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 120,
        temperature: float = 0.2,
        timeout_s: Optional[float] = None,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        started = time.monotonic()
        try:
            payload = self._post("/chat/completions", body, timeout_s=timeout_s)
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"inference backend unreachable: {exc}") from exc
        elapsed = time.monotonic() - started

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = payload.get("usage") or {}

        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    log.warning("model emitted unparseable tool arguments: %r", args)
                    args = {}
            calls.append(ToolCall(name=name, arguments=args or {}))

        completion = Completion(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            elapsed_s=elapsed,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            truncated=choice.get("finish_reason") == "length",
        )
        log.info(
            "llm turn: %.1fs, %d prompt + %d output tokens (%.1f tok/s)%s",
            completion.elapsed_s,
            completion.prompt_tokens,
            completion.completion_tokens,
            completion.tokens_per_s,
            " [truncated]" if completion.truncated else "",
        )
        return completion

    # --- transport ------------------------------------------------------

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str, *, timeout_s: Optional[float] = None) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers=self._headers(), method="GET"
        )
        with urllib.request.urlopen(
            request, timeout=timeout_s or self.timeout_s
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, body: dict, *, timeout_s: Optional[float] = None) -> dict:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=self._headers(), method="POST"
        )
        with urllib.request.urlopen(
            request, timeout=timeout_s or self.timeout_s
        ) as response:
            return json.loads(response.read().decode("utf-8"))


def estimate_tokens(text: str) -> int:
    """Rough token count — about four characters per token.

    Good enough for a budget guard. Being 20 % wrong about a 400-token cap
    changes a two-minute turn by twenty seconds; being wrong about whether
    there is a cap at all is what actually hurts.
    """
    return max(1, len(text) // 4)
