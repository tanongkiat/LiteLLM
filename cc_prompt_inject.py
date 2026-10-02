"""
LiteLLM custom callback: prepends the Claude Code system prompt for
claude-sub-auto requests.

Why: Anthropic only serves subscription (sk-ant-oat*) tokens to requests that
look like Claude Code — the CC system prompt must be the first system block,
otherwise the API answers with a cloaked 429 "Error" even though auth is valid
(verified empirically: ZCode-style system prompt -> 429; CC prompt first with
any other system content after it -> 200).

This lets IDEs that can only send base-URL + API-key (ZCode, Cursor, ...) use
a Claude subscription seat through the gateway: the OAuth token lives in the
model config (CLAUDE_OAUTH_TOKEN in .env), the prompt is injected here.

Injection depends on the call path (verified against LiteLLM main-stable):
  call_type "acompletion"          -> OpenAI-compat: prepend a role:"system"
                                      message (converted to the system field
                                      by LiteLLM's anthropic transformation)
  call_type "anthropic_messages"   -> Anthropic-native /v1/messages: prepend a
                                      text block to the TOP-LEVEL system field
                                      (role:"system" inside messages is a 400)

Registered in litellm_config.yaml:
  litellm_settings:
    callbacks: ["cc_prompt_inject.handler"]
"""
from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger

CC_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
TARGET_MODELS = ("claude-sub-auto",)


def _already_has_cc(text: Any) -> bool:
    return isinstance(text, str) and text.startswith("You are Claude Code")


class CCPromptInject(CustomLogger):
    async def async_pre_call_hook(
        self, user_api_key_dict, cache, data: dict, call_type: str, **kwargs: Any
    ) -> Optional[dict]:
        model = str(data.get("model") or "")
        if not any(m in model for m in TARGET_MODELS):
            return data

        if call_type == "acompletion":  # OpenAI-compatible path
            messages = data.get("messages")
            if not isinstance(messages, list) or not messages:
                return data
            first = messages[0] if isinstance(messages[0], dict) else {}
            if _already_has_cc(first.get("content")) and first.get("role") == "system":
                return data  # idempotent
            data["messages"] = [{"role": "system", "content": CC_PROMPT}, *messages]
            return data

        if call_type == "anthropic_messages":  # Anthropic-native path
            system = data.get("system")
            if system is None:
                data["system"] = [{"type": "text", "text": CC_PROMPT}]
            elif isinstance(system, str):
                data["system"] = [
                    {"type": "text", "text": CC_PROMPT},
                    {"type": "text", "text": system},
                ]
            elif isinstance(system, list):
                if system and isinstance(system[0], dict) and _already_has_cc(system[0].get("text")):
                    return data  # idempotent
                data["system"] = [{"type": "text", "text": CC_PROMPT}, *system]
            return data

        return data  # other call types: leave untouched


handler = CCPromptInject()
