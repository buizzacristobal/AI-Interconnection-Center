"""Calls AI providers so the hub can run "hosted" agents itself.

Two wire formats cover every provider in PRESETS:
  - anthropic: Anthropic Messages API (raw HTTP, the project is stdlib-only)
  - openai:    OpenAI-compatible /chat/completions (OpenAI, Gemini, DeepSeek,
               OpenRouter, Moonshot/Kimi, Qwen/DashScope, Ollama, custom)

Keys come from .aic/config.json, else from the preset's environment variable.
Keys are only ever sent to the provider's own base URL.
"""

import json
import os
import urllib.error
import urllib.request

ANTHROPIC_VERSION = "2026-10-21"
# server-side refusal fallback ("default" routing) for the current top models
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
ANTHROPIC_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

PRESETS = [
    {"id": "anthropic", "label": "Anthropic (Claude)", "kind": "anthropic",
     "base_url": "https://api.anthropic.com", "env": "ANTHROPIC_API_KEY",
     "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]},
    {"id": "openai", "label": "OpenAI", "kind": "openai",
     "base_url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY", "models": []},
    {"id": "gemini", "label": "Google Gemini", "kind": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "env": "GEMINI_API_KEY", "models": []},
    {"id": "deepseek", "label": "DeepSeek", "kind": "openai",
     "base_url": "https://api.deepseek.com", "env": "DEEPSEEK_API_KEY", "models": ["deepseek-chat", "deepseek-reasoner"]},
    {"id": "openrouter", "label": "OpenRouter (many models)", "kind": "openai",
     "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY", "models": []},
    {"id": "moonshot", "label": "Moonshot (Kimi)", "kind": "openai",
     "base_url": "https://api.moonshot.ai/v1", "env": "MOONSHOT_API_KEY", "models": []},
    {"id": "qwen", "label": "Qwen (DashScope)", "kind": "openai",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "env": "DASHSCOPE_API_KEY", "models": []},
    {"id": "ollama", "label": "Ollama (local, no key)", "kind": "openai",
     "base_url": "http://127.0.0.1:11434/v1", "env": "", "keyless": True, "models": []},
    {"id": "custom", "label": "Custom (OpenAI-compatible)", "kind": "openai",
     "base_url": "", "env": "", "models": []},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}


class LLMError(Exception):
    pass


def resolve(provider_id, cfg_providers):
    """Merged provider settings: preset + stored overrides + env key."""
    preset = PRESET_BY_ID.get(provider_id)
    if not preset:
        raise LLMError("Unknown provider '%s'." % provider_id)
    stored = cfg_providers.get(provider_id, {})
    key = stored.get("api_key") or (os.environ.get(preset["env"], "") if preset["env"] else "")
    base = (stored.get("base_url") or preset["base_url"]).rstrip("/")
    if not base:
        raise LLMError("Provider '%s' needs a base URL (Settings > AI providers)." % preset["label"])
    if not key and not preset.get("keyless"):
        hint = " or set %s" % preset["env"] if preset["env"] else ""
        raise LLMError("No API key for %s. Add one in Settings > AI providers%s." % (preset["label"], hint))
    return {"id": provider_id, "kind": preset["kind"], "base_url": base, "api_key": key, "label": preset["label"]}


def key_source(provider_id, cfg_providers):
    preset = PRESET_BY_ID.get(provider_id, {})
    if cfg_providers.get(provider_id, {}).get("api_key"):
        return "saved"
    if preset.get("env") and os.environ.get(preset["env"]):
        return "env:" + preset["env"]
    if preset.get("keyless"):
        return "keyless"
    return ""


def _post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err or raw)
        except ValueError:
            msg = raw[:300]
        raise LLMError("%s HTTP %d: %s" % (url.split("/")[2], e.code, msg))
    except urllib.error.URLError as e:
        raise LLMError("Could not reach %s: %s" % (url.split("/")[2], e.reason))
    except (TimeoutError, OSError) as e:
        raise LLMError("Request to %s failed: %s" % (url.split("/")[2], e))
    except ValueError:
        raise LLMError("Provider returned a non-JSON response.")


def complete(prov, model, system, user_text, timeout=300, max_tokens=16000):
    """One stateless call. Returns the reply text."""
    if prov["kind"] == "anthropic":
        headers = {"x-api-key": prov["api_key"], "anthropic-version": ANTHROPIC_VERSION}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user_text}]}
        if prov["base_url"] == "https://api.anthropic.com" and model in ANTHROPIC_FALLBACK_MODELS:
            headers["anthropic-beta"] = ANTHROPIC_FALLBACK_BETA
            body["fallbacks"] = "default"
        data = _post(prov["base_url"] + "/v1/messages", headers, body, timeout)
        if data.get("stop_reason") == "refusal":
            cat = (data.get("stop_details") or {}).get("category") or "unspecified"
            raise LLMError("The model declined this request (refusal: %s)." % cat)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    else:
        headers = {"Authorization": "Bearer " + prov["api_key"]} if prov["api_key"] else {}
        body = {"model": model, "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user_text}]}
        data = _post(prov["base_url"] + "/chat/completions", headers, body, timeout)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected response shape from %s." % prov["label"])
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = content or ""
    text = text.strip()
    if not text:
        raise LLMError("%s returned an empty reply." % prov["label"])
    return text
