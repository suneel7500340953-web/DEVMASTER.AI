"""Provider-independent LLM layer (stdlib only). Keys come from environment variables, never from files."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    provider: str


def _request(method, url, headers, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except ValueError:
            data = {"error": {"message": body[:300]}}
        return e.code, dict(e.headers), data
    except urllib.error.URLError as e:
        raise LLMError(f"Network error: {e.reason}") from None


def http_post(url, headers, payload):
    return _request("POST", url, headers, payload)


def http_get(url, headers):
    return _request("GET", url, headers)


def _err_msg(data) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    return str(err.get("message") if isinstance(err, dict) else (err or data))[:300]


def _post_retry(post, sleep, url, headers, payload, retries=2):
    for attempt in range(retries + 1):
        status, hdrs, data = post(url, headers, payload)
        if status == 200:
            return data
        if status in (429, 500, 502, 503) and attempt < retries:
            ra = next((v for k, v in hdrs.items() if k.lower() == "retry-after"), None)
            try:
                wait = min(float(ra), 20)
            except (TypeError, ValueError):
                wait = 2 * (attempt + 1)
            sleep(wait)
            continue
        raise LLMError(f"{status}: {_err_msg(data)}")


class OpenAICompatible:
    """OpenAI, Groq, OpenRouter, Ollama, vLLM, LM Studio: anything with /chat/completions."""

    def __init__(self, name, base_url, api_key=None, token_param="max_tokens",
                 send_temperature=True, post=http_post, get=http_get, sleep=time.sleep):
        self.name, self.base_url, self._key = name, base_url.rstrip("/"), api_key
        self.token_param, self.send_temperature = token_param, send_temperature
        self._post, self._get, self._sleep = post, get, sleep

    def __repr__(self):
        return f"OpenAICompatible({self.name!r}, {self.base_url!r})"  # never includes the key

    def _headers(self):
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def complete(self, messages, model, max_tokens=4096, temperature=0.2) -> LLMResponse:
        payload = {"model": model, "messages": messages, self.token_param: max_tokens}
        if self.send_temperature:
            payload["temperature"] = temperature
        data = _post_retry(self._post, self._sleep, f"{self.base_url}/chat/completions", self._headers(), payload)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected response from provider") from None
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        u = data.get("usage") or {}
        return LLMResponse(text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), model, self.name)

    def list_models(self) -> list[str]:
        status, _, data = self._get(f"{self.base_url}/models", self._headers())
        if status != 200:
            raise LLMError(f"{status}: {_err_msg(data)}")
        return sorted(m["id"] for m in data.get("data", []))


class Anthropic:
    name = "anthropic"

    def __init__(self, api_key, base_url="https://api.anthropic.com/v1", post=http_post, sleep=time.sleep):
        self._key, self.base_url, self._post, self._sleep = api_key, base_url.rstrip("/"), post, sleep

    def __repr__(self):
        return f"Anthropic({self.base_url!r})"

    def complete(self, messages, model, max_tokens=4096, temperature=0.2) -> LLMResponse:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                   "messages": [m for m in messages if m["role"] != "system"]}
        if system:
            payload["system"] = system
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01"}
        data = _post_retry(self._post, self._sleep, f"{self.base_url}/messages", headers, payload)
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError):
            raise LLMError("Unexpected response from provider") from None
        u = data.get("usage") or {}
        return LLMResponse(text.strip(), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), model, self.name)


DEFAULTS = {
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
               "token_param": "max_completion_tokens", "send_temperature": False},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "ollama": {"kind": "openai", "base_url": "http://localhost:11434/v1", "key_env": None},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY"},
}


def build_registry(env=None, config_path=None) -> dict:
    """Providers that are usable now. Optional JSON config adds/overrides providers:
    {"providers": {"myllm": {"kind": "openai", "base_url": "...", "key_env": "MY_KEY"}}}"""
    env = os.environ if env is None else env
    specs = {k: dict(v) for k, v in DEFAULTS.items()}
    if config_path and Path(config_path).exists():
        for k, v in json.loads(Path(config_path).read_text()).get("providers", {}).items():
            specs[k] = {**specs.get(k, {}), **v}
    out = {}
    for name, s in specs.items():
        key_env = s.get("key_env")
        key = env.get(key_env) if key_env else None
        if key_env and not key:
            continue
        if s.get("kind") == "anthropic":
            out[name] = Anthropic(key, s.get("base_url", DEFAULTS["anthropic"]["base_url"]))
        else:
            out[name] = OpenAICompatible(name, s["base_url"], key, s.get("token_param", "max_tokens"),
                                         s.get("send_temperature", True))
    return out
