"""LLM provider — returns an LLM instance based on config (claude_cli or local/Ollama)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Generator, List, Optional, Protocol

from src.config.config_loader import LLMConfig

logger = logging.getLogger(__name__)

_MIN_CONTEXT_TOKENS = 64_000
_CHARS_PER_TOKEN = 3.5
_PROMPT_BUDGET_RATIO = 0.60

_CLAUDE_MODEL_CONTEXT: dict = {
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
}


# ---------------------------------------------------------------------------
# Corporate OAuth CLI auth helper
# ---------------------------------------------------------------------------

def _get_claude_auth() -> tuple[str | None, str | None]:
    """Return (api_key, auth_token) for the Anthropic client.

    Resolution order:
    1. ANTHROPIC_API_KEY env var  → (api_key, None)
    2. ~/.claude/settings.json apiKeyHelper script → (None, oauth_token)
    3. Neither found → (None, None) — SDK will raise a clear error
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key, None

    # Corporate auth: call apiKeyHelper to get a short-lived OAuth token
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            helper = settings.get("apiKeyHelper", "")
            if helper and Path(helper).exists():
                result = subprocess.run(
                    [helper], capture_output=True, text=True, timeout=15
                )
                token = result.stdout.strip()
                if token and result.returncode == 0:
                    logger.debug("Obtained auth token via apiKeyHelper")
                    return None, token
                else:
                    logger.warning("apiKeyHelper failed: %s", result.stderr.strip())
        except Exception as exc:
            logger.warning("Could not run apiKeyHelper: %s", exc)

    return None, None


# ---------------------------------------------------------------------------
# Protocol (interface)
# ---------------------------------------------------------------------------

class LLMInterface(Protocol):
    """Minimal interface expected from any LLM instance."""

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Send a prompt and return the full response text."""
        ...

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        """Send a prompt and yield response text chunks."""
        ...


# ---------------------------------------------------------------------------
# Claude (Anthropic SDK) implementation
# ---------------------------------------------------------------------------

class ClaudeProvider:
    """LLM provider backed by the Anthropic Python SDK.

    Auth resolution order:
    1. ANTHROPIC_API_KEY env var (standard Anthropic API key)
    2. ~/.claude/settings.json apiKeyHelper script (corporate OAuth token)
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            import anthropic  # type: ignore
        except ImportError:
            raise ImportError("Install anthropic SDK: pip install anthropic")

        api_key, auth_token = _get_claude_auth()

        if api_key:
            self._client = anthropic.Anthropic(api_key=api_key)
            logger.debug("ClaudeProvider: using ANTHROPIC_API_KEY")
        elif auth_token:
            self._client = anthropic.Anthropic(auth_token=auth_token)
            logger.debug("ClaudeProvider: using apiKeyHelper OAuth token")
        else:
            raise ValueError(
                "No Anthropic credentials found. Set ANTHROPIC_API_KEY in .env "
                "or ensure ~/.claude/settings.json has a working apiKeyHelper."
            )

    @property
    def context_window(self) -> int:
        try:
            info = self._client.models.retrieve(self.config.model)
            return getattr(info, "context_window", 0) or _CLAUDE_MODEL_CONTEXT.get(self.config.model, 200_000)
        except Exception:
            return _CLAUDE_MODEL_CONTEXT.get(self.config.model, 200_000)

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Return the full model response."""
        try:
            return self._complete(prompt, system)
        except Exception as exc:
            if "auth" in str(exc).lower() or "401" in str(exc):
                logger.info("Auth token expired — refreshing and retrying")
                self._refresh_client()
                return self._complete(prompt, system)
            raise

    def _complete(self, prompt: str, system: Optional[str] = None) -> str:
        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            messages=messages,
            temperature=self.config.temperature,
        )
        if system:
            kwargs["system"] = system
        logger.debug("Claude complete: model=%s, prompt_len=%d, temperature=%.2f", self.config.model, len(prompt), self.config.temperature)
        response = self._client.messages.create(**kwargs)
        if not response.content:
            raise RuntimeError(f"Claude API returned empty content (stop_reason={response.stop_reason})")
        return response.content[0].text

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        """Yield streamed response chunks."""
        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            messages=messages,
            temperature=self.config.temperature,
        )
        if system:
            kwargs["system"] = system

        logger.debug("Claude stream: model=%s, temperature=%.2f", self.config.model, self.config.temperature)
        with self._client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text

    def complete_with_tools(
        self,
        messages: list,
        system: Optional[str] = None,
        tools: Optional[list] = None,
        tool_executor=None,
        max_iterations: int = 8,
    ) -> str:
        """Run an agentic tool-use loop and return the final text reply.

        Accepts tools in either **OpenAI function-calling format** (portable,
        used by all providers) or Anthropic format.  OpenAI-format dicts are
        converted internally before the Anthropic API call.

        Args:
            messages:       Initial messages list (``[{"role":"user","content":...}]``).
            system:         System prompt string.
            tools:          Tool definitions — OpenAI or Anthropic format.
            tool_executor:  Callable ``(tool_name: str, tool_input: dict) -> str``.
            max_iterations: Safety cap on tool-call rounds (default 8).

        Returns:
            Final text response from the model.
        """
        if not tools or tool_executor is None:
            return self.complete(messages[-1]["content"] if messages else "", system)

        anthropic_tools = self._to_anthropic_tools(tools)

        kwargs: dict = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            tools=anthropic_tools,
            messages=list(messages),
            temperature=self.config.temperature,
        )
        if system:
            kwargs["system"] = system

        for iteration in range(max_iterations):
            logger.debug(
                "ClaudeProvider.complete_with_tools: iter=%d messages=%d",
                iteration, len(kwargs["messages"]),
            )
            response = self._client.messages.create(**kwargs)

            if response.stop_reason != "tool_use":
                if not response.content:
                    raise RuntimeError(f"Claude API returned empty content (stop_reason={response.stop_reason})")
                texts = [
                    blk.text for blk in response.content
                    if hasattr(blk, "text")
                ]
                return "\n".join(texts)

            kwargs["messages"].append({"role": "assistant", "content": response.content})

            tool_results = []
            for blk in response.content:
                if blk.type != "tool_use":
                    continue
                logger.info(
                    "[complete_with_tools] calling tool '%s' input=%s",
                    blk.name, str(blk.input)[:200],
                )
                result_text = tool_executor(blk.name, blk.input)
                logger.debug("[complete_with_tools] tool '%s' → %d chars", blk.name, len(result_text))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": blk.id,
                    "content": result_text,
                })

            kwargs["messages"].append({"role": "user", "content": tool_results})

        logger.warning("[complete_with_tools] max_iterations=%d reached, requesting conclusion", max_iterations)
        kwargs["messages"].append({
            "role": "user",
            "content": "Please provide your final answer based on the information gathered so far.",
        })
        kwargs.pop("tools", None)
        response = self._client.messages.create(**kwargs)
        if not response.content:
            raise RuntimeError(f"Claude API returned empty content (stop_reason={response.stop_reason})")
        texts = [blk.text for blk in response.content if hasattr(blk, "text")]
        return "\n".join(texts)

    @staticmethod
    def _to_anthropic_tools(tools: list) -> list:
        """Convert OpenAI-format tool defs to Anthropic format.

        Handles both::

            # OpenAI format (portable)
            {"type": "function", "function": {"name": ..., "parameters": {...}}}

            # Anthropic format (pass-through)
            {"name": ..., "input_schema": {...}}
        """
        result = []
        for t in (tools or []):
            if "input_schema" in t:
                result.append(t)
                continue
            fn = t.get("function", t)
            result.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def _refresh_client(self) -> None:
        """Re-fetch the OAuth token and rebuild the Anthropic client."""
        import anthropic  # type: ignore
        _, auth_token = _get_claude_auth()
        if auth_token:
            self._client = anthropic.Anthropic(auth_token=auth_token)
            logger.debug("ClaudeProvider: token refreshed")


# ---------------------------------------------------------------------------
# Ollama (local) implementation
# ---------------------------------------------------------------------------

class OllamaProvider:
    """LLM provider backed by a local Ollama server."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            import ollama  # type: ignore
            self._client = ollama
        except ImportError:
            raise ImportError("Install ollama: pip install ollama")

    @property
    def context_window(self) -> int:
        try:
            info = self._client.show(self.config.local_model)
            params = info.get("model_info", {}) or info.get("details", {})
            for key in params:
                if "context" in key.lower():
                    val = params[key]
                    return int(val) if isinstance(val, (int, float)) else 0
        except Exception:
            pass
        return 0

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Return the full model response from Ollama."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.debug("Ollama complete: model=%s", self.config.local_model)
        response = self._client.chat(
            model=self.config.local_model,
            messages=messages,
            options={"temperature": self.config.temperature},
        )
        return response["message"]["content"]

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        """Yield streamed chunks from Ollama."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.debug("Ollama stream: model=%s", self.config.local_model)
        for chunk in self._client.chat(
            model=self.config.local_model,
            messages=messages,
            stream=True,
            options={"temperature": self.config.temperature},
        ):
            yield chunk["message"]["content"]

    def complete_with_tools(
        self,
        messages: list,
        system: Optional[str] = None,
        tools: Optional[list] = None,
        tool_executor=None,
        max_iterations: int = 8,
    ) -> str:
        """Run an agentic tool-use loop via Ollama's native tool-calling API.

        Ollama accepts OpenAI-format tool definitions directly (Llama 3.1+,
        Mistral, Qwen2.5, Gemma3, and other models that support function calling).
        """
        if not tools or tool_executor is None:
            last_msg = messages[-1]["content"] if messages else ""
            return self.complete(last_msg, system)

        msg_list: List[dict] = []
        if system:
            msg_list.append({"role": "system", "content": system})
        msg_list.extend(messages)

        for iteration in range(max_iterations):
            logger.debug("OllamaProvider.complete_with_tools: iter=%d", iteration)
            response = self._client.chat(
                model=self.config.local_model,
                messages=msg_list,
                tools=tools,
                options={"temperature": self.config.temperature},
            )
            msg = response["message"]
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                return msg.get("content", "")

            # Append assistant turn (may have content + tool_calls)
            msg_list.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn = tc["function"]
                logger.info("[OllamaProvider] calling tool '%s'", fn["name"])
                result = tool_executor(fn["name"], fn["arguments"])
                msg_list.append({"role": "tool", "content": result})

        logger.warning("[OllamaProvider.complete_with_tools] max_iterations reached")
        msg_list.append({"role": "user", "content": "Please provide your final answer."})
        response = self._client.chat(
            model=self.config.local_model,
            messages=msg_list,
            options={"temperature": self.config.temperature},
        )
        return response["message"].get("content", "")


# ---------------------------------------------------------------------------
# LM Studio (OpenAI-compatible local server) implementation
# ---------------------------------------------------------------------------

class LMStudioProvider:
    """LLM provider backed by LM Studio's OpenAI-compatible REST API.

    LM Studio exposes a local server at http://localhost:1234/v1 that speaks
    the OpenAI chat-completions protocol.  Any model loaded in LM Studio
    (e.g. Qwen3-5.9B) can be called using the ``openai`` Python SDK by
    pointing it at the local base URL.

    Setup:
        1. Open LM Studio → load the Qwen3-5.9B (or any other) model.
        2. Start the local server: LM Studio → Local Server → Start Server.
        3. Set ``provider: lm_studio`` in config.yaml.

    The ``api_key`` value is not validated by LM Studio but the openai SDK
    requires it to be non-empty; "lm-studio" is the conventional dummy value.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            import openai  # type: ignore
        except ImportError:
            raise ImportError(
                "Install openai SDK: pip install openai\n"
                "(LM Studio uses the OpenAI-compatible API)"
            )
        self._client = openai.OpenAI(
            base_url=config.lm_studio_url,
            api_key="lm-studio",          # LM Studio ignores the key value
            timeout=getattr(config, "lm_studio_timeout", 3600),
        )
        logger.debug(
            "LMStudioProvider: url=%s model=%s",
            config.lm_studio_url, config.lm_studio_model,
        )

    @property
    def context_window(self) -> int:
        try:
            models = self._client.models.list()
            for m in models.data:
                if m.id == self.config.lm_studio_model:
                    return getattr(m, "context_window", 0) or getattr(m, "context_length", 0) or 0
        except Exception:
            pass
        return 0

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Return the full model response from LM Studio."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.debug("LMStudio complete: model=%s", self.config.lm_studio_model)
        response = self._client.chat.completions.create(
            model=self.config.lm_studio_model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        return response.choices[0].message.content or ""

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        """Yield streamed response chunks from LM Studio."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.debug("LMStudio stream: model=%s", self.config.lm_studio_model)
        for chunk in self._client.chat.completions.create(
            model=self.config.lm_studio_model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        ):
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def complete_with_tools(
        self,
        messages: list,
        system: Optional[str] = None,
        tools: Optional[list] = None,
        tool_executor=None,
        max_iterations: int = 8,
    ) -> str:
        """Run an agentic tool-use loop via LM Studio's OpenAI-compatible API.

        LM Studio's server speaks the OpenAI chat-completions protocol, so
        OpenAI-format tool definitions work without any conversion.
        """
        if not tools or tool_executor is None:
            last_msg = messages[-1]["content"] if messages else ""
            return self.complete(last_msg, system)

        msg_list: List[dict] = []
        if system:
            msg_list.append({"role": "system", "content": system})
        msg_list.extend(messages)

        for iteration in range(max_iterations):
            logger.debug("LMStudioProvider.complete_with_tools: iter=%d", iteration)
            response = self._client.chat.completions.create(
                model=self.config.lm_studio_model,
                messages=msg_list,
                tools=tools,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
            msg = response.choices[0].message
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                return msg.content or ""

            # Append assistant turn with tool_calls for context
            msg_list.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                logger.info("[LMStudioProvider] calling tool '%s'", tc.function.name)
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                result = tool_executor(tc.function.name, args)
                msg_list.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        logger.warning("[LMStudioProvider.complete_with_tools] max_iterations reached")
        msg_list.append({"role": "user", "content": "Please provide your final answer."})
        response = self._client.chat.completions.create(
            model=self.config.lm_studio_model,
            messages=msg_list,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        return response.choices[0].message.content or "" or ""


# ---------------------------------------------------------------------------
# Google Gemini implementation
# ---------------------------------------------------------------------------


class GeminiProvider:
    """LLM provider backed by the Google Gemini API.

    Supports Gemini 1.5 / 2.0 models via ``google-generativeai`` SDK.
    Gemma models running *locally via Ollama* should use ``OllamaProvider``
    instead (Ollama speaks the same OpenAI-compat function-calling protocol).

    Setup:
        pip install google-generativeai
        Set ``GEMINI_API_KEY`` env var or ``config.llm.gemini_api_key``.

    Config example::

        llm:
          provider: gemini
          gemini_model: gemini-1.5-flash   # or gemini-2.0-flash, gemini-1.5-pro
          gemini_api_key: ""               # or set GEMINI_API_KEY env var
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError:
            raise ImportError(
                "Install google-generativeai: pip install google-generativeai"
            )

        api_key = (getattr(config, "gemini_api_key", "") or "").strip() or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError(
                "Gemini API key not found. Set GEMINI_API_KEY env var or "
                "config.llm.gemini_api_key."
            )
        genai.configure(api_key=api_key)
        self._genai = genai
        self._model_name = getattr(config, "gemini_model", "gemini-1.5-flash")
        logger.debug("GeminiProvider: model=%s", self._model_name)

    @property
    def context_window(self) -> int:
        try:
            model_info = self._genai.get_model(f"models/{self._model_name}")
            return getattr(model_info, "input_token_limit", 0) or 0
        except Exception:
            pass
        return 0

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Return the full model response from Gemini."""
        model = self._genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system,
            generation_config={"temperature": self.config.temperature},
        )
        logger.debug("Gemini complete: model=%s, temperature=%.2f", self._model_name, self.config.temperature)
        response = model.generate_content(prompt)
        return response.text

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        """Yield streamed response chunks from Gemini."""
        model = self._genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system,
            generation_config={"temperature": self.config.temperature},
        )
        logger.debug("Gemini stream: model=%s, temperature=%.2f", self._model_name, self.config.temperature)
        for chunk in model.generate_content(prompt, stream=True):
            try:
                if chunk.text:
                    yield chunk.text
            except Exception:
                pass

    def complete_with_tools(
        self,
        messages: list,
        system: Optional[str] = None,
        tools: Optional[list] = None,
        tool_executor=None,
        max_iterations: int = 8,
    ) -> str:
        """Run an agentic tool-use loop via Gemini's function-calling API.

        Converts OpenAI-format tool definitions to Gemini ``FunctionDeclaration``
        objects internally.  The loop follows the Gemini function-calling
        protocol: send → receive FunctionCall parts → execute → send
        FunctionResponse → repeat until a text-only response is returned.
        """
        if not tools or tool_executor is None:
            last_msg = messages[-1]["content"] if messages else ""
            return self.complete(last_msg, system)

        gemini_tools = self._openai_to_gemini_tools(tools)
        model = self._genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system,
            tools=gemini_tools,
            generation_config={"temperature": self.config.temperature},
        )

        history, last_user_content = _gemini_split_messages(messages)
        chat = model.start_chat(history=history)
        response = chat.send_message(last_user_content)

        for iteration in range(max_iterations):
            fc_parts = _gemini_function_call_parts(response)
            if not fc_parts:
                return _gemini_extract_text(response)

            tool_responses = []
            for part in fc_parts:
                fc = part.function_call
                logger.info("[GeminiProvider] calling tool '%s'", fc.name)
                result = tool_executor(fc.name, dict(fc.args))
                tool_responses.append(
                    self._genai.protos.Part(
                        function_response=self._genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"content": result},
                        )
                    )
                )

            response = chat.send_message(tool_responses)

        logger.warning("[GeminiProvider.complete_with_tools] max_iterations reached")
        return _gemini_extract_text(response)

    def _openai_to_gemini_tools(self, openai_tools: list) -> list:
        """Convert OpenAI-format tool defs to a Gemini Tool list."""
        fn_decls = []
        for t in (openai_tools or []):
            fn = t.get("function", t)
            params = fn.get("parameters", {"type": "object", "properties": {}})
            fn_decl = self._genai.protos.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters=self._json_schema_to_gemini(params),
            )
            fn_decls.append(fn_decl)
        return [self._genai.protos.Tool(function_declarations=fn_decls)]

    def _json_schema_to_gemini(self, schema: dict):
        """Recursively convert a JSON Schema dict to a Gemini protos.Schema."""
        _type_map = {
            "string":  self._genai.protos.Type.STRING,
            "integer": self._genai.protos.Type.INTEGER,
            "number":  self._genai.protos.Type.NUMBER,
            "boolean": self._genai.protos.Type.BOOLEAN,
            "array":   self._genai.protos.Type.ARRAY,
            "object":  self._genai.protos.Type.OBJECT,
        }
        schema_type = _type_map.get(
            schema.get("type", "string"), self._genai.protos.Type.STRING
        )
        props = {
            k: self._json_schema_to_gemini(v)
            for k, v in schema.get("properties", {}).items()
        }
        items = self._json_schema_to_gemini(schema["items"]) if "items" in schema else None
        return self._genai.protos.Schema(
            type=schema_type,
            description=schema.get("description", ""),
            properties=props,
            required=schema.get("required", []),
            items=items,
        )


# ---------------------------------------------------------------------------
# Gemini helper functions (module-level — avoid class self-reference issues)
# ---------------------------------------------------------------------------

def _gemini_split_messages(messages: list) -> tuple:
    """Split messages into (gemini_history, last_user_content).

    Gemini's chat API takes prior turns in ``history`` (role "user"/"model")
    and the final user message via ``send_message()``.
    """
    if not messages:
        return [], ""
    history = []
    for m in messages[:-1]:
        role = "user" if m.get("role") == "user" else "model"
        content = m.get("content", "")
        if isinstance(content, str) and content:
            history.append({"role": role, "parts": [content]})
    last = messages[-1].get("content", "")
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last if p.get("type") == "text")
    return history, last or ""


def _gemini_function_call_parts(response) -> list:
    """Return response parts that contain a FunctionCall, safely."""
    parts = []
    try:
        for p in response.parts:
            if hasattr(p, "function_call") and p.function_call.name:
                parts.append(p)
    except Exception:
        pass
    return parts


def _gemini_extract_text(response) -> str:
    """Safely extract text from a Gemini response."""
    try:
        return response.text
    except Exception:
        pass
    parts = []
    try:
        for p in response.parts:
            if hasattr(p, "text") and p.text:
                parts.append(p.text)
    except Exception:
        pass
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claude subprocess implementation
# ---------------------------------------------------------------------------


class ClaudeSubprocessProvider:
    """LLM provider that calls the local ``claude`` CLI via subprocess.

    Useful when you have the Claude CLI installed with corporate auth
    but do not have a direct Anthropic API key.  Prompts are piped to
    ``claude --print`` and the text response is read from stdout.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        # Verify the claude binary is reachable
        result = subprocess.run(
            ["which", "claude"], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                "claude CLI not found on PATH. "
                "Install it or set provider to 'claude_cli' / 'local'."
            )
        self._claude_bin = result.stdout.strip()
        logger.debug("ClaudeSubprocessProvider: using binary at %s", self._claude_bin)

    @property
    def context_window(self) -> int:
        return _CLAUDE_MODEL_CONTEXT.get(self.config.model, 200_000)

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Run ``claude --print`` and return the full response."""
        return "".join(self.stream(prompt, system))

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        """Run ``claude --print`` and yield the response."""
        # Embed system prompt directly in the message — avoids relying on
        # --append-system-prompt which may not be supported on all CLI versions.
        if system:
            full_prompt = f"<system>\n{system}\n</system>\n\n{prompt}"
        else:
            full_prompt = prompt

        cmd = [self._claude_bin, "--print", "--output-format", "text"]
        if self.config.temperature and self.config.temperature > 0:
            cmd.extend(["-t", str(self.config.temperature)])

        logger.debug(
            "ClaudeSubprocess: prompt_len=%d", len(full_prompt)
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(input=full_prompt, timeout=self.config.subprocess_timeout)

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited with code {proc.returncode}.\n"
                f"stderr: {stderr.strip()}"
            )

        yield stdout


# ---------------------------------------------------------------------------
# LLM call logger — transparent wrapper around any provider
# ---------------------------------------------------------------------------


class LoggingLLMProvider:
    """Wraps any LLM provider and records every call via LLMCallLogger.

    ``LLMCallLogger`` writes three output files derived from ``log_path``:

    * ``<stem>.jsonl`` — one JSON record per call (backward compat)
    * ``<stem>.txt``   — human-readable text with call delimiters
    * ``<stem>.html``  — dark-terminal HTML with collapsible panes (live auto-refresh)

    In the run's ``llm.log`` each call also emits two readable lines::

        ==> call #N | provider | system_len=N prompt_len=N
        <== call #N | Xs | response_len=N
    """

    def __init__(self, provider, log_path: str) -> None:
        from src.llm.call_logger import LLMCallLogger
        self._provider    = provider
        self._call_logger = LLMCallLogger(log_path)
        self._call_id     = 0
        logger.info(
            "LLM call logging → %s (provider: %s)",
            log_path, type(provider).__name__,
        )

    def set_next_label(self, label: str) -> None:
        """Set a descriptive label for the next logged call (shown in HTML pane header)."""
        self._call_logger.next_call_label = label

    @property
    def context_window(self) -> int:
        return getattr(self._provider, "context_window", 0)

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        self._call_id += 1
        call_id = self._call_id
        provider_name = type(self._provider).__name__
        import time
        t0 = time.monotonic()

        logger.info(
            "==> call #%d | %s | system_len=%d prompt_len=%d",
            call_id, provider_name,
            len(system) if system else 0, len(prompt),
        )

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                response = self._provider.complete(prompt, system=system)
                duration = time.monotonic() - t0
                self._call_logger.log(
                    call_id=call_id, provider=provider_name,
                    prompt=prompt, system=system, response=response,
                    duration_s=duration, success=True, error=None,
                )
                logger.info(
                    "<== call #%d | %.1fs | response_len=%d",
                    call_id, duration, len(response),
                )
                return response
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "<== call #%d attempt 1 failed after %.1fs: %s — retrying in 2s …",
                        call_id, time.monotonic() - t0, exc,
                    )
                    time.sleep(2)

        duration = time.monotonic() - t0
        self._call_logger.log(
            call_id=call_id, provider=provider_name,
            prompt=prompt, system=system, response=None,
            duration_s=duration, success=False, error=str(last_exc),
        )
        logger.error("<== call #%d FAILED after %.1fs: %s", call_id, duration, last_exc)
        raise last_exc

    def stream(self, prompt: str, system: Optional[str] = None) -> Generator[str, None, None]:
        self._call_id += 1
        call_id = self._call_id
        provider_name = type(self._provider).__name__
        import time
        t0 = time.monotonic()
        chunks: list[str] = []

        logger.info(
            "==> stream #%d | %s | system_len=%d prompt_len=%d",
            call_id, provider_name,
            len(system) if system else 0, len(prompt),
        )

        try:
            for chunk in self._provider.stream(prompt, system=system):
                chunks.append(chunk)
                yield chunk
            duration = time.monotonic() - t0
            full_response = "".join(chunks)
            self._call_logger.log(
                call_id=call_id, provider=provider_name,
                prompt=prompt, system=system, response=full_response,
                duration_s=duration, success=True, error=None,
            )
            logger.info(
                "<== stream #%d | %.1fs | response_len=%d",
                call_id, duration, len(full_response),
            )
        except Exception as exc:
            duration = time.monotonic() - t0
            self._call_logger.log(
                call_id=call_id, provider=provider_name,
                prompt=prompt, system=system,
                response="".join(chunks) or None,
                duration_s=duration, success=False, error=str(exc),
            )
            logger.error("<== stream #%d FAILED: %s", call_id, exc)
            raise

    def complete_with_tools(
        self,
        messages: list,
        system: Optional[str] = None,
        tools: Optional[list] = None,
        tool_executor=None,
        max_iterations: int = 8,
    ) -> str:
        """Proxy complete_with_tools to the underlying provider, with logging."""
        self._call_id += 1
        call_id = self._call_id
        provider_name = type(self._provider).__name__
        import time
        t0 = time.monotonic()

        logger.info(
            "==> tool_use #%d | %s | messages=%d tools=%d",
            call_id, provider_name, len(messages), len(tools or []),
        )
        try:
            reply = self._provider.complete_with_tools(
                messages, system, tools, tool_executor, max_iterations
            )
            duration = time.monotonic() - t0
            logger.info(
                "<== tool_use #%d | %.1fs | reply_len=%d",
                call_id, duration, len(reply),
            )
            return reply
        except Exception as exc:
            duration = time.monotonic() - t0
            logger.error("<== tool_use #%d FAILED after %.1fs: %s", call_id, duration, exc)
            raise


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_llm(
    config: LLMConfig,
    log_dir: Optional[str] = None,
) -> "LoggingLLMProvider | ClaudeProvider | OllamaProvider | ClaudeSubprocessProvider":
    """Return an LLM instance based on the config provider setting.

    Args:
        config:  LLMConfig with ``provider`` set to one of the supported values.
        log_dir: Optional per-run log directory.  When provided, llm_calls.* are
                 written to ``<log_dir>/llm_calls.jsonl`` (and .txt / .html) so
                 parallel pipeline runs each get their own isolated call log.
                 Falls back to ``config.call_log_path`` when omitted.

    Returns:
        An LLM provider, wrapped in LoggingLLMProvider when a log path is set.

    Raises:
        ValueError: If the provider is not recognised.
    """
    if config.provider == "claude_cli":
        logger.info("Using Claude provider (model: %s)", config.model)
        provider = ClaudeProvider(config)
    elif config.provider == "local":
        logger.info("Using Ollama provider (model: %s)", config.local_model)
        provider = OllamaProvider(config)
    elif config.provider == "claude_subprocess":
        logger.info("Using Claude subprocess provider (claude --print)")
        provider = ClaudeSubprocessProvider(config)
    elif config.provider == "lm_studio":
        logger.info("Using LM Studio provider (url=%s model=%s)", config.lm_studio_url, config.lm_studio_model)
        provider = LMStudioProvider(config)
    elif config.provider == "gemini":
        gemini_model = getattr(config, "gemini_model", "gemini-1.5-flash")
        logger.info("Using Gemini provider (model: %s)", gemini_model)
        provider = GeminiProvider(config)
    else:
        raise ValueError(
            f"Unknown LLM provider: '{config.provider}'. "
            "Valid options: 'claude_cli', 'local', 'claude_subprocess', 'lm_studio', 'gemini'"
        )

    # Resolve the call-log path: per-run dir takes priority over global config.
    if log_dir:
        from pathlib import Path as _Path
        effective_log_path = str(_Path(log_dir) / "llm_calls.jsonl")
    elif config.call_log_path:
        effective_log_path = config.call_log_path
    else:
        effective_log_path = ""

    if effective_log_path:
        provider = LoggingLLMProvider(provider, effective_log_path)

    ctx = getattr(provider, "context_window", 0) or 0
    if ctx > 0:
        logger.info("LLM context window: %d tokens", ctx)
        if ctx < _MIN_CONTEXT_TOKENS:
            raise RuntimeError(
                f"Model context window is {ctx:,} tokens — this pipeline requires "
                f"at least {_MIN_CONTEXT_TOKENS:,} tokens. Use a larger model or "
                f"set llm.max_prompt_chars manually to override this check."
            )
        auto_chars = int(ctx * _CHARS_PER_TOKEN * _PROMPT_BUDGET_RATIO)
        existing = getattr(config, "max_prompt_chars", 0) or 0
        if existing == 0 or auto_chars < existing:
            config.max_prompt_chars = auto_chars
            logger.info(
                "Auto-set max_prompt_chars=%d (from %d token context × %.0f%% budget)",
                auto_chars, ctx, _PROMPT_BUDGET_RATIO * 100,
            )
    elif getattr(config, "max_prompt_chars", 0) == 0:
        config.max_prompt_chars = 80_000
        logger.info("Context window unknown — using default max_prompt_chars=%d", 80_000)

    return provider
