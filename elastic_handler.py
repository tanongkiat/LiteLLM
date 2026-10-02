"""
LiteLLM custom provider for Elastic inference endpoints (incl. EIS).

Auth is resolved in this order:
  1. the api_key passed from config.yaml (litellm_params.api_key)
  2. EC_APIKEY from the environment
  3. ES_USER + ES_PASS from the environment (basic auth)

An API key must be Elastic's *encoded* form -- the `encoded` field returned by
POST /_security/api_key, i.e. base64("<id>:<api_key>"). It is sent as
`Authorization: ApiKey <key>`.

Env vars (or set api_base / api_key in config.yaml):
  ELASTICSEARCH_URL  e.g. https://<cluster>.es.<region>.aws.found.io
  EC_APIKEY          encoded Elastic API key
  ES_USER, ES_PASS   your Elastic login (fallback)

Model name in LiteLLM:  elastic/<inference_id>
"""
import json
import os
from typing import AsyncIterator, Iterator

import httpx
import litellm
from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.llms.openai import ChatCompletionToolCallChunk
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage

DEFAULT_URL = ""  # no default: ELASTICSEARCH_URL must be set explicitly
TIMEOUT = httpx.Timeout(300.0, connect=15.0)

# Elastic rejects these OpenAI params with HTTP 400, so they are never
# forwarded even when a client sends them: parallel_tool_calls, response_format.
PASSTHROUGH_PARAMS = (
    "temperature", "top_p", "max_completion_tokens", "stop", "tools", "tool_choice",
)


def _timeout(timeout=None) -> httpx.Timeout:
    """Honour the per-request timeout LiteLLM passes down."""
    if timeout is None:
        return TIMEOUT
    if isinstance(timeout, httpx.Timeout):
        return timeout
    return httpx.Timeout(float(timeout), connect=min(15.0, float(timeout)))


def _settings(model: str, api_base=None, api_key=None, optional_params=None):
    """Return (url, basic_auth_tuple_or_None, headers) for one inference call."""
    inference_id = model.split("/", 1)[1] if model.startswith("elastic/") else model
    base = (api_base or os.getenv("ELASTICSEARCH_URL") or DEFAULT_URL).rstrip("/")
    if not base:
        raise ValueError("Set ELASTICSEARCH_URL environment variable, or api_base in config.yaml")

    key = api_key or os.getenv("EC_APIKEY")
    if key:
        auth, headers = None, {"Authorization": f"ApiKey {key.strip()}"}
    else:
        user = os.getenv("ES_USER")
        pwd = os.getenv("ES_PASS")
        if not user or not pwd:
            raise ValueError(
                "Set an API key (api_key in config.yaml or EC_APIKEY), "
                "or ES_USER and ES_PASS environment variables"
            )
        auth, headers = (user, pwd), {}

    url = f"{base}/_inference/chat_completion/{inference_id}/_stream"
    return url, auth, headers


def _body(messages, optional_params):
    body = {"messages": messages}
    op = optional_params or {}
    for k in PASSTHROUGH_PARAMS:
        if op.get(k) is not None:
            body[k] = op[k]
    if op.get("max_tokens") is not None and "max_completion_tokens" not in body:
        body["max_completion_tokens"] = op["max_tokens"]
    body.update(op.get("extra_body") or {})  # escape hatch for Elastic-specific params
    return body


def _parse_line(line: str):
    """Parse one SSE line into an event dict, or None to skip the line.

    Event keys: done, text, tool_calls, finish_reason, usage.
    """
    line = line.strip()
    if not line.startswith("data:"):
        if line.startswith("{") and '"error"' in line:
            raise CustomLLMError(status_code=500, message=f"Elastic error: {line}")
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return {"done": True}
    obj = json.loads(data)
    if "error" in obj:
        # errors can arrive mid-stream; keep Elastic's status so LiteLLM maps it
        raise CustomLLMError(status_code=obj.get("status") or 500,
                             message=f"Elastic error: {data}")
    obj = obj.get("chat_completion", obj)  # older versions wrap the chunk
    choices = obj.get("choices") or [{}]
    ch = choices[0]
    delta = ch.get("delta") or {}
    return {
        "done": False,
        "text": delta.get("content") or "",
        "tool_calls": delta.get("tool_calls") or [],
        "finish_reason": ch.get("finish_reason"),
        "usage": obj.get("usage"),
    }


def _tool_use(tc: dict) -> ChatCompletionToolCallChunk:
    """Map one Elastic tool_call delta to LiteLLM's tool-call chunk shape.

    Elastic sends id + function.name in the first fragment, then
    function.arguments in pieces; `type` is null on the later fragments.
    """
    fn = tc.get("function") or {}
    return {
        "id": tc.get("id"),
        "type": "function",
        "function": {"name": fn.get("name"), "arguments": fn.get("arguments") or ""},
        "index": tc.get("index") or 0,
    }


def _chunk(text, finish_reason=None, done=False, tool_use=None, usage=None) -> GenericStreamingChunk:
    return {
        "text": text,
        "is_finished": done,
        "finish_reason": finish_reason or ("stop" if done else ""),
        "index": 0,
        "tool_use": tool_use,
        "usage": usage,
    }


def _usage(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "prompt_tokens": raw.get("prompt_tokens") or 0,
        "completion_tokens": raw.get("completion_tokens") or 0,
        "total_tokens": raw.get("total_tokens") or 0,
    }


def _raise_for_status(r: httpx.Response, body: bytes):
    """Surface Elastic's HTTP status so LiteLLM maps it to the right exception.

    LiteLLM turns status_code into RateLimitError / AuthenticationError /
    BadRequestError etc., which is what makes retries and fallbacks work.
    """
    if r.status_code >= 400:
        raise CustomLLMError(
            status_code=r.status_code,
            message=f"Elastic HTTP {r.status_code}: {body.decode(errors='replace')[:1000]}",
        )


def _collect(chunks):
    """Fold streamed chunks into (text, tool_calls, finish_reason, usage).

    Tool-call fragments are accumulated per index: the id and name arrive once,
    the JSON arguments arrive in pieces that must be concatenated in order.
    """
    text = ""
    tools: dict = {}
    finish_reason = None
    usage = None
    for c in chunks:
        text += c.get("text") or ""
        tu = c.get("tool_use")
        if tu:
            slot = tools.setdefault(tu["index"], {"id": None, "name": None, "arguments": ""})
            if tu.get("id"):
                slot["id"] = tu["id"]
            fn = tu.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            slot["arguments"] += fn.get("arguments") or ""
        if c.get("usage"):
            usage = c["usage"]
        if c.get("finish_reason"):
            finish_reason = c["finish_reason"]
    return text, tools, finish_reason or "stop", usage


def _response(model: str, text: str, tools: dict | None = None,
              finish_reason: str = "stop", usage: dict | None = None) -> ModelResponse:
    # content is None only alongside tool_calls (OpenAI convention); plain
    # text responses keep "" rather than becoming null
    message: dict = {"role": "assistant", "content": (text or None) if tools else text}
    if tools:
        message["tool_calls"] = [
            {
                "id": t["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": t["name"] or "", "arguments": t["arguments"] or "{}"},
            }
            for i, t in sorted(tools.items())
        ]
    resp = ModelResponse(
        model=model,
        choices=[{"index": 0, "finish_reason": finish_reason, "message": message}],
    )
    if usage:
        setattr(resp, "usage", Usage(**usage))
    return resp


class ElasticLLM(CustomLLM):
    # ---------- streaming ----------
    def streaming(self, model, messages, api_base=None, api_key=None, optional_params=None,
                  headers=None, timeout=None, **kw) -> Iterator[GenericStreamingChunk]:
        url, auth, hdrs = _settings(model, api_base, api_key, optional_params)
        hdrs.update(headers or {})
        body = _body(messages, optional_params)
        try:
            with httpx.Client(timeout=_timeout(timeout)) as c:
                with c.stream("POST", url, auth=auth, headers=hdrs, json=body) as r:
                    if r.status_code >= 400:
                        _raise_for_status(r, r.read())
                    finish_reason = None
                    usage = None
                    for line in r.iter_lines():
                        ev = _parse_line(line)
                        if ev is None:
                            continue
                        if ev["done"]:
                            break
                        if ev["finish_reason"]:
                            finish_reason = ev["finish_reason"]
                        if ev["usage"]:
                            usage = _usage(ev["usage"])
                        if ev["text"]:
                            yield _chunk(ev["text"])
                        for tc in ev["tool_calls"]:
                            yield _chunk("", tool_use=_tool_use(tc))
                    yield _chunk("", finish_reason or "stop", True, usage=usage)
        except httpx.TimeoutException as e:
            raise CustomLLMError(status_code=408, message=f"Elastic request timed out: {e}") from e
        except httpx.RequestError as e:
            raise CustomLLMError(status_code=503, message=f"Elastic connection error: {e}") from e

    async def astreaming(self, model, messages, api_base=None, api_key=None, optional_params=None,
                         headers=None, timeout=None, **kw) -> AsyncIterator[GenericStreamingChunk]:
        url, auth, hdrs = _settings(model, api_base, api_key, optional_params)
        hdrs.update(headers or {})
        body = _body(messages, optional_params)
        try:
            async with httpx.AsyncClient(timeout=_timeout(timeout)) as c:
                async with c.stream("POST", url, auth=auth, headers=hdrs, json=body) as r:
                    if r.status_code >= 400:
                        _raise_for_status(r, await r.aread())
                    finish_reason = None
                    usage = None
                    async for line in r.aiter_lines():
                        ev = _parse_line(line)
                        if ev is None:
                            continue
                        if ev["done"]:
                            break
                        if ev["finish_reason"]:
                            finish_reason = ev["finish_reason"]
                        if ev["usage"]:
                            usage = _usage(ev["usage"])
                        if ev["text"]:
                            yield _chunk(ev["text"])
                        for tc in ev["tool_calls"]:
                            yield _chunk("", tool_use=_tool_use(tc))
                    yield _chunk("", finish_reason or "stop", True, usage=usage)
        except httpx.TimeoutException as e:
            raise CustomLLMError(status_code=408, message=f"Elastic request timed out: {e}") from e
        except httpx.RequestError as e:
            raise CustomLLMError(status_code=503, message=f"Elastic connection error: {e}") from e

    # ---------- non-streaming (Elastic only streams, so collect it) ----------
    def completion(self, model, messages, api_base=None, api_key=None, optional_params=None,
                   headers=None, timeout=None, **kw) -> ModelResponse:
        chunks = self.streaming(model, messages, api_base=api_base, api_key=api_key,
                                optional_params=optional_params, headers=headers, timeout=timeout)
        return _response(model, *_collect(chunks))

    async def acompletion(self, model, messages, api_base=None, api_key=None, optional_params=None,
                          headers=None, timeout=None, **kw) -> ModelResponse:
        chunks = [c async for c in self.astreaming(
            model, messages, api_base=api_base, api_key=api_key,
            optional_params=optional_params, headers=headers, timeout=timeout)]
        return _response(model, *_collect(chunks))


elastic_llm = ElasticLLM()
