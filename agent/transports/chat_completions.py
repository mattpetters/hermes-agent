"""OpenAI Chat Completions transport.

Handles the default api_mode ('chat_completions') used by ~16 OpenAI-compatible
providers (OpenRouter, Nous, NVIDIA, Qwen, Ollama, DeepSeek, xAI, Kimi, etc.).

Messages and tools are already in OpenAI format — convert_messages and
convert_tools are near-identity.  The complexity lives in build_kwargs
which has provider-specific conditionals for max_tokens defaults,
reasoning configuration, temperature handling, and extra_body assembly.
"""

import copy
from typing import Any

from agent.moonshot_schema import is_moonshot_model, sanitize_moonshot_tools
from agent.prompt_builder import DEVELOPER_ROLE_MODELS
from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall, Usage


class ChatCompletionsTransport(ProviderTransport):
    """Transport for api_mode='chat_completions'.

    The default path for OpenAI-compatible providers.
    """

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs
    ) -> list[dict[str, Any]]:
        """Messages are already in OpenAI format — sanitize Codex leaks only.

        Strips Codex Responses API fields (``codex_reasoning_items`` /
        ``codex_message_items`` on the message, ``call_id``/``response_item_id``
        on tool_calls) that strict chat-completions providers reject with 400/422.
        """
        needs_sanitize = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if "codex_reasoning_items" in msg or "codex_message_items" in msg:
                needs_sanitize = True
                break
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and (
                        "call_id" in tc or "response_item_id" in tc
                    ):
                        needs_sanitize = True
                        break
                if needs_sanitize:
                    break

        if not needs_sanitize:
            return messages

        sanitized = copy.deepcopy(messages)
        for msg in sanitized:
            if not isinstance(msg, dict):
                continue
            msg.pop("codex_reasoning_items", None)
            msg.pop("codex_message_items", None)
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tc.pop("call_id", None)
                        tc.pop("response_item_id", None)
        return sanitized

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools are already in OpenAI format — identity."""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """Build chat.completions.create() kwargs.

        This is the most complex transport method — it handles ~16 providers
        via params rather than subclasses.

        params:
            timeout: float — API call timeout
            max_tokens: int | None — user-configured max tokens
            ephemeral_max_output_tokens: int | None — one-shot override (error recovery)
            max_tokens_param_fn: callable — returns {max_tokens: N} or {max_completion_tokens: N}
            reasoning_config: dict | None
            request_overrides: dict | None
            session_id: str | None
            qwen_session_metadata: dict | None — {sessionId, promptId} precomputed
            model_lower: str — lowercase model name for pattern matching
            # Provider detection flags (all optional, default False)
            is_openrouter: bool
            is_nous: bool
            is_qwen_portal: bool
            is_github_models: bool
            is_kimi: bool
            is_custom_provider: bool
            ollama_num_ctx: int | None
            # Provider routing
            provider_preferences: dict | None
            # Qwen-specific
            qwen_prepare_fn: callable | None — runs AFTER codex sanitization
            qwen_prepare_inplace_fn: callable | None — in-place variant for deepcopied lists
            # Temperature
            fixed_temperature: Any — from _fixed_temperature_for_model()
            omit_temperature: bool
            # Reasoning
            supports_reasoning: bool
            github_reasoning_extra: dict | None
            # Claude on OpenRouter/Nous max output
            anthropic_max_output: int | None
            # Extra
            extra_body_additions: dict | None — pre-built extra_body entries
        """
        # Codex sanitization: drop reasoning_items / call_id / response_item_id
        sanitized = self.convert_messages(messages)

        # ── Provider profile: single-path when present ──────────
        _profile = params.get("provider_profile")
        if _profile:
            return self._build_kwargs_from_profile(
                _profile, model, sanitized, tools, params
            )

        # ── Legacy flag-based path (no profile) ─────────────────
        # deepcopied, reuse that copy via the in-place variant to avoid a
        # second deepcopy.
        is_qwen = params.get("is_qwen_portal", False)
        if is_qwen:
            qwen_prep = params.get("qwen_prepare_fn")
            qwen_prep_inplace = params.get("qwen_prepare_inplace_fn")
            if sanitized is messages:
                if qwen_prep is not None:
                    sanitized = qwen_prep(sanitized)
            else:
                # Already deepcopied — transform in place
                if qwen_prep_inplace is not None:
                    qwen_prep_inplace(sanitized)
                elif qwen_prep is not None:
                    sanitized = qwen_prep(sanitized)

        # Developer role swap for GPT-5/Codex models
        model_lower = params.get("model_lower", (model or "").lower())
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # Temperature
        fixed_temp = params.get("fixed_temperature")
        omit_temp = params.get("omit_temperature", False)
        if omit_temp:
            api_kwargs.pop("temperature", None)
        elif fixed_temp is not None:
            api_kwargs["temperature"] = fixed_temp

        # Qwen metadata (caller precomputes {sessionId, promptId})
        qwen_meta = params.get("qwen_session_metadata")
        if qwen_meta and is_qwen:
            api_kwargs["metadata"] = qwen_meta

        # Tools
        if tools:
            # Moonshot/Kimi uses a stricter flavored JSON Schema.  Rewriting
            # tool parameters here keeps aggregator routes (Nous, OpenRouter,
            # etc.) compatible, in addition to direct moonshot.ai endpoints.
            if is_moonshot_model(model):
                tools = sanitize_moonshot_tools(tools)
            api_kwargs["tools"] = tools

        # max_tokens resolution — priority: ephemeral > user > provider default
        max_tokens_fn = params.get("max_tokens_param_fn")
        ephemeral = params.get("ephemeral_max_output_tokens")
        max_tokens = params.get("max_tokens")
        anthropic_max_out = params.get("anthropic_max_output")
        is_kimi = params.get("is_kimi", False)
        reasoning_config = params.get("reasoning_config")

        if ephemeral is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(ephemeral))
        elif max_tokens is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(max_tokens))
        elif is_qwen and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(65536))
        elif is_kimi and max_tokens_fn:
            # Kimi/Moonshot: 32000 matches Kimi CLI's default
            api_kwargs.update(max_tokens_fn(32000))
        elif anthropic_max_out is not None:
            api_kwargs["max_tokens"] = anthropic_max_out

        # Kimi: top-level reasoning_effort (unless thinking disabled)
        if is_kimi:
            _kimi_thinking_off = bool(
                reasoning_config
                and isinstance(reasoning_config, dict)
                and reasoning_config.get("enabled") is False
            )
            if not _kimi_thinking_off:
                _kimi_effort = "medium"
                if reasoning_config and isinstance(reasoning_config, dict):
                    _e = (reasoning_config.get("effort") or "").strip().lower()
                    if _e in ("low", "medium", "high"):
                        _kimi_effort = _e
                api_kwargs["reasoning_effort"] = _kimi_effort

        # extra_body assembly
        extra_body: dict[str, Any] = {}

        is_openrouter = params.get("is_openrouter", False)
        is_nous = params.get("is_nous", False)
        is_github_models = params.get("is_github_models", False)

        provider_prefs = params.get("provider_preferences")
        if provider_prefs and is_openrouter:
            extra_body["provider"] = provider_prefs

        # Kimi extra_body.thinking
        if is_kimi:
            _kimi_thinking_enabled = True
            if reasoning_config and isinstance(reasoning_config, dict):
                if reasoning_config.get("enabled") is False:
                    _kimi_thinking_enabled = False
            extra_body["thinking"] = {
                "type": "enabled" if _kimi_thinking_enabled else "disabled",
            }

        # Reasoning
        if params.get("supports_reasoning", False):
            if is_github_models:
                gh_reasoning = params.get("github_reasoning_extra")
                if gh_reasoning is not None:
                    extra_body["reasoning"] = gh_reasoning
            else:
                if reasoning_config is not None:
                    rc = dict(reasoning_config)
                    if is_nous and rc.get("enabled") is False:
                        pass  # omit for Nous when disabled
                    else:
                        extra_body["reasoning"] = rc
                else:
                    extra_body["reasoning"] = {"enabled": True, "effort": "medium"}

        if is_nous:
            extra_body["tags"] = ["product=hermes-agent"]

        # Ollama num_ctx
        ollama_ctx = params.get("ollama_num_ctx")
        if ollama_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_ctx
            extra_body["options"] = options

        # Ollama/custom think=false
        if params.get("is_custom_provider", False):
            if reasoning_config and isinstance(reasoning_config, dict):
                _effort = (reasoning_config.get("effort") or "").strip().lower()
                _enabled = reasoning_config.get("enabled", True)
                if _effort == "none" or _enabled is False:
                    extra_body["think"] = False

        if is_qwen:
            extra_body["vl_high_resolution_images"] = True

        # Merge any pre-built extra_body additions
        additions = params.get("extra_body_additions")
        if additions:
            extra_body.update(additions)

        if extra_body:
            api_kwargs["extra_body"] = extra_body

        # Request overrides last (service_tier etc.)
        overrides = params.get("request_overrides")
        if overrides:
            api_kwargs.update(overrides)

        return api_kwargs

    def _build_kwargs_from_profile(self, profile, model, sanitized, tools, params):
        """Build API kwargs using a ProviderProfile — single path, no legacy flags.

        This method replaces the entire flag-based kwargs assembly when a
        provider_profile is passed. Every quirk comes from the profile object.
        """
        from providers.base import OMIT_TEMPERATURE

        # Message preprocessing
        sanitized = profile.prepare_messages(sanitized)

        # Developer role swap — model-name-based, applies to all providers
        _model_lower = (model or "").lower()
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in _model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        # Temperature
        if profile.fixed_temperature is OMIT_TEMPERATURE:
            pass  # Don't include temperature at all
        elif profile.fixed_temperature is not None:
            api_kwargs["temperature"] = profile.fixed_temperature
        else:
            # Use caller's temperature if provided
            temp = params.get("temperature")
            if temp is not None:
                api_kwargs["temperature"] = temp

        # Timeout
        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # Tools
        if tools:
            api_kwargs["tools"] = tools

        # max_tokens resolution — priority: ephemeral > user > profile default
        max_tokens_fn = params.get("max_tokens_param_fn")
        ephemeral = params.get("ephemeral_max_output_tokens")
        user_max = params.get("max_tokens")
        anthropic_max = params.get("anthropic_max_output")

        if ephemeral is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(ephemeral))
        elif user_max is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(user_max))
        elif profile.default_max_tokens and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(profile.default_max_tokens))
        elif anthropic_max is not None:
            api_kwargs["max_tokens"] = anthropic_max

        # Provider-specific api_kwargs extras (reasoning_effort, metadata, etc.)
        reasoning_config = params.get("reasoning_config")
        extra_body_from_profile, top_level_from_profile = (
            profile.build_api_kwargs_extras(
                reasoning_config=reasoning_config,
                supports_reasoning=params.get("supports_reasoning", False),
                qwen_session_metadata=params.get("qwen_session_metadata"),
            )
        )
        api_kwargs.update(top_level_from_profile)

        # extra_body assembly
        extra_body: dict[str, Any] = {}

        # Profile's extra_body (tags, provider prefs, vl_high_resolution, etc.)
        profile_body = profile.build_extra_body(
            session_id=params.get("session_id"),
            provider_preferences=params.get("provider_preferences"),
        )
        if profile_body:
            extra_body.update(profile_body)

        # Profile's reasoning/thinking extra_body entries
        if extra_body_from_profile:
            extra_body.update(extra_body_from_profile)

        # Merge any pre-built extra_body additions from the caller
        additions = params.get("extra_body_additions")
        if additions:
            extra_body.update(additions)

        # Request overrides (user config)
        overrides = params.get("request_overrides")
        if overrides:
            for k, v in overrides.items():
                if k == "extra_body" and isinstance(v, dict):
                    extra_body.update(v)
                else:
                    api_kwargs[k] = v

        if extra_body:
            api_kwargs["extra_body"] = extra_body

        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize OpenAI ChatCompletion to NormalizedResponse.

        For chat_completions, this is near-identity — the response is already
        in OpenAI format.  extra_content on tool_calls (Gemini thought_signature)
        is preserved via ToolCall.provider_data.  reasoning_details (OpenRouter
        unified format) and reasoning_content (DeepSeek/Moonshot) are also
        preserved for downstream replay.
        """
        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                # Preserve provider-specific extras on the tool call.
                # Gemini 3 thinking models attach extra_content with
                # thought_signature — without replay on the next turn the API
                # rejects the request with 400.
                tc_provider_data: dict[str, Any] = {}
                extra = getattr(tc, "extra_content", None)
                if extra is None and hasattr(tc, "model_extra"):
                    extra = (tc.model_extra or {}).get("extra_content")
                if extra is not None:
                    if hasattr(extra, "model_dump"):
                        try:
                            extra = extra.model_dump()
                        except Exception:
                            pass
                    tc_provider_data["extra_content"] = extra
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                        provider_data=tc_provider_data or None,
                    )
                )

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        # Preserve reasoning fields separately.  DeepSeek/Moonshot use
        # ``reasoning_content``; others use ``reasoning``.  Downstream code
        # (_extract_reasoning, thinking-prefill retry) reads both distinctly,
        # so keep them apart in provider_data rather than merging.
        reasoning = getattr(msg, "reasoning", None)
        reasoning_content = getattr(msg, "reasoning_content", None)

        provider_data: dict[str, Any] = {}
        if reasoning_content:
            provider_data["reasoning_content"] = reasoning_content
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        return NormalizedResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        if not response.choices:
            return False
        return True

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Extract OpenRouter/OpenAI cache stats from prompt_tokens_details."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            return None
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None


# Auto-register on import
from agent.transports import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)
