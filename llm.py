"""Robust, switchable tailoring backend.
Supports TWO API styles:
  - "openai"    : OpenAI-compatible /chat/completions (OpenAI, Gemini, Groq, local Ollama)
  - "anthropic" : Anthropic native /v1/messages (Claude) — different endpoint/headers/format
Features: provider presets, retries+backoff, JSON parsing, usage/cost capture."""
import os, time, json, re, requests

_HERE = os.path.dirname(__file__)
SECRETS = os.path.join(_HERE, "secrets.yaml")

class LLMError(Exception): pass

# Approximate prices (USD per 1M tokens) for LOCAL ESTIMATION only — edit to current rates.
# Actual token counts come from the API response; the daily cap uses these rates.
# Each preset declares its "api" style so the right request format is used.
# Providers keyed by SERVICE NAME. Each lists its endpoint, API style, the models available
# under it (with per-1M-token price estimates you can edit), and a key hint.
PROVIDERS = {
 "Anthropic": {"api":"anthropic", "base_url":"https://api.anthropic.com/v1", "key_hint":"sk-ant-...",
    "models":{"claude-haiku-4-5-20251001":{"in":1.0,"out":5.0},
              "claude-sonnet-4-6":{"in":3.0,"out":15.0},
              "claude-opus-4-8":{"in":15.0,"out":75.0}}},
 "OpenAI": {"api":"openai", "base_url":"https://api.openai.com/v1", "key_hint":"sk-...",
    "models":{"gpt-4o-mini":{"in":0.15,"out":0.60},
              "gpt-4o":{"in":2.50,"out":10.0},
              "gpt-4.1-mini":{"in":0.40,"out":1.60}}},
 "Google Gemini": {"api":"openai", "base_url":"https://generativelanguage.googleapis.com/v1beta/openai",
    "key_hint":"AIza...",
    "models":{"gemini-2.0-flash":{"in":0.10,"out":0.40},
              "gemini-1.5-pro":{"in":1.25,"out":5.0}}},
 "Groq": {"api":"openai", "base_url":"https://api.groq.com/openai/v1", "key_hint":"gsk_...",
    "models":{"llama-3.1-8b-instant":{"in":0.05,"out":0.08},
              "llama-3.3-70b-versatile":{"in":0.59,"out":0.79}}},
 "Local Ollama (free)": {"api":"openai", "base_url":"http://localhost:11434/v1", "key_hint":"(no key needed)",
    "models":{"llama3.1:8b":{"in":0.0,"out":0.0}, "qwen2.5:7b":{"in":0.0,"out":0.0}}},
 "Custom": {"api":"openai", "base_url":"", "key_hint":"", "models":{}},
}

def provider_price(provider, model):
    """Return {'in':..,'out':..} per 1M tokens for a provider+model (0 if unknown/custom)."""
    p = PROVIDERS.get(provider, {})
    return p.get("models", {}).get(model, {"in": 0.0, "out": 0.0})

def get_api_key():
    import fileio
    try:
        s = fileio.read_yaml(SECRETS, default={}) or {}
        if s.get("api_key"): return str(s["api_key"]).strip()
    except Exception: pass
    return os.environ.get("OPENAI_API_KEY", "").strip()

def save_api_key(key):
    import fileio
    try:
        s = fileio.read_yaml(SECRETS, default={}) or {}
    except Exception:
        s = {}
    s["api_key"] = str(key or "").strip()
    fileio.write_yaml_atomic(SECRETS, s)

# ---------------- OpenAI-compatible path ----------------
def _chat_openai(prompt, base_url, model, key, max_tokens, timeout, retries, json_mode):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key: headers["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    if json_mode: payload["response_format"] = {"type": "json_object"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 429:
                last = "rate limited (429)"; time.sleep((2**attempt)*3); continue
            if r.status_code == 400 and "response_format" in payload:
                # Some OpenAI-compatible servers reject response_format; drop it
                # ONCE and retry. A second 400 is a real error and must surface.
                payload.pop("response_format", None); continue
            if 400 <= r.status_code < 500:
                # Non-retryable client error: retrying can't help — surface the
                # server's own explanation instead of a generic "failed after N".
                raise LLMError(f"API error {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            d = r.json()
            text = d["choices"][0]["message"]["content"]
            u = d.get("usage", {}) or {}
            return text, {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}
        except requests.exceptions.RequestException as e:
            last = str(e)[:200]; time.sleep(2**attempt)
        except (KeyError, ValueError) as e:
            raise LLMError(f"Unexpected API response: {e}")
    raise LLMError(f"API failed after {retries} attempts: {last}")

# ---------------- Anthropic native path ----------------
def _chat_anthropic(prompt, base_url, model, key, max_tokens, timeout, retries):
    # base_url like https://api.anthropic.com/v1  ->  /messages
    url = base_url.rstrip("/")
    url = url + "/messages" if url.endswith("/v1") else url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 429:
                last = "rate limited (429)"; time.sleep((2**attempt)*3); continue
            if r.status_code == 401:
                raise LLMError("401 Unauthorized — Anthropic rejected the API key. Check it is "
                    "correct (starts sk-ant-), active, and has credit. Re-paste and Save it in Settings.")
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise LLMError(f"Anthropic API error {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            u = d.get("usage", {}) or {}
            return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}
        except requests.exceptions.RequestException as e:
            last = str(e)[:200]; time.sleep(2**attempt)
        except (KeyError, ValueError) as e:
            raise LLMError(f"Unexpected Anthropic response: {e}")
    raise LLMError(f"API failed after {retries} attempts: {last}")

def chat(prompt, base_url, model, api="openai", api_key=None, max_tokens=1600, timeout=120, retries=3, json_mode=False):
    """Returns (text, usage). Routes to the correct API style.
    api_key (if given) is used directly; otherwise the saved key is read. This lets
    "Test connection" use the just-entered key even before it is saved."""
    key = (api_key or get_api_key() or "").strip()
    is_local = "localhost" in (base_url or "") or "127.0.0.1" in (base_url or "")
    if not key and not is_local:
        raise LLMError("No API key configured. Paste your key in Settings and Save (or Test) it.")
    if api == "anthropic":
        return _chat_anthropic(prompt, base_url, model, key, max_tokens, timeout, retries)
    return _chat_openai(prompt, base_url, model, key, max_tokens, timeout, retries, json_mode)

def _parse_json(text):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m: t = m.group(1)
    try:
        return json.loads(t)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b > a: return json.loads(t[a:b+1])
        raise LLMError("Model did not return valid JSON")

def chat_json(prompt, base_url, model, api="openai", api_key=None, **kw):
    text, usage = chat(prompt, base_url, model, api=api, api_key=api_key, json_mode=True, **kw)
    return _parse_json(text), usage

def est_cost(usage, price):
    return (usage.get("in",0)/1e6)*price.get("in",0.0) + (usage.get("out",0)/1e6)*price.get("out",0.0)

def health(base_url, model, api="openai", api_key=None):
    try:
        txt, _ = chat("Reply with the single word: OK", base_url, model, api=api,
                      api_key=api_key, max_tokens=5, timeout=30, retries=2)
        return True, txt.strip()
    except Exception as e:
        return False, str(e)[:200]
