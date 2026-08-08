"""OpenAI-compatible client: chat, embeddings, rerank.

Design rules, all of them deadline-driven:

  * stdlib only -- no dependency to install at 11am on submission day
  * every call is cached on disk by content hash, so a rerun costs nothing and a
    crash mid-run resumes instantly
  * every call has a retry with backoff, and a hard budget so a loop cannot eat
    the submission window
  * every failure degrades to None; the caller must have a deterministic fallback
  * the LLM is never asked to do arithmetic -- only classification and extraction
"""
import hashlib
import json
import time
import urllib.error
import urllib.request

from config import CACHE, LLMConfig

_calls = {"chat": 0, "embed": 0, "rerank": 0, "cached": 0, "failed": 0}


def stats():
    return dict(_calls)


def _cache_path(kind, payload):
    CACHE.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                       .encode("utf-8")).hexdigest()[:32]
    return CACHE / f"{kind}-{h}.json"


# Some gateways sit behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" agent with HTTP 403 error code 1010. A normal client
# User-Agent is required for the request to reach the API at all.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "halyk-covenant-agent/1.0 (+python-urllib)",
}


def _post(url, body, api_key, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={**_HEADERS, "Authorization": f"Bearer {api_key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def list_models():
    """Which models this key can actually reach. Useful before a timed run."""
    if not (LLMConfig.base_url and LLMConfig.api_key):
        return []
    req = urllib.request.Request(
        f"{LLMConfig.base_url}/models",
        headers={**_HEADERS, "Authorization": f"Bearer {LLMConfig.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return sorted(m["id"] for m in json.loads(r.read()).get("data", []))
    except Exception:                                          # noqa: BLE001
        return []


def _call(kind, url, body, *, tries=None):
    """Cached, retried POST. Returns parsed JSON or None."""
    cp = _cache_path(kind, {"url": url, "body": body})
    if cp.exists():
        _calls["cached"] += 1
        return json.loads(cp.read_text(encoding="utf-8"))
    if sum(v for k, v in _calls.items() if k in ("chat", "embed", "rerank")) \
            >= LLMConfig.budget_calls:
        _calls["failed"] += 1
        return None

    tries = tries or LLMConfig.max_retries
    delay = 1.5
    for attempt in range(tries):
        try:
            out = _post(url, body, LLMConfig.api_key, LLMConfig.timeout)
            cp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            _calls[kind] += 1
            return out
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            # 4xx other than rate-limit will not fix itself; stop early
            if e.code not in (408, 409, 429) and e.code < 500:
                print(f"  ! {kind} HTTP {e.code}: {detail}")
                break
            # Free tiers meter tokens per MINUTE, so exponential backoff from 1.5s
            # gives up long before the window resets. Honour Retry-After, and
            # floor the wait at a value that can actually outlast the window.
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = max(float(ra), delay)
                except (TypeError, ValueError):
                    delay = max(delay, 20.0)
            if attempt == tries - 1:
                print(f"  ! {kind} HTTP {e.code} after {tries} tries: {detail}")
        except Exception as e:                                  # noqa: BLE001
            if attempt == tries - 1:
                print(f"  ! {kind} failed after {tries} tries: {e}")
        time.sleep(delay)
        delay *= 2
    _calls["failed"] += 1
    return None


# --------------------------------------------------------------------------- chat
def chat_json(system, user, *, model=None, schema_hint=None, max_tokens=1200):
    """Ask for strict JSON back. Returns a dict, or None if unavailable/unparseable.

    We do not trust `response_format` to exist on every gateway, so the prompt
    also demands raw JSON and we strip code fences defensively.
    """
    if not LLMConfig.available():
        return None
    sys_msg = system
    if schema_hint:
        sys_msg += ("\n\nReturn RAW JSON only -- no prose, no markdown fence. "
                    "It must match this shape exactly:\n" + schema_hint)
    body = {
        "model": model or LLMConfig.model,
        "temperature": LLMConfig.temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": sys_msg},
                     {"role": "user", "content": user}],
    }
    out = _call("chat", f"{LLMConfig.base_url}/chat/completions", body)
    if not out:
        return None
    try:
        text = out["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        return None
    return _parse_json(text)


def _parse_json(text):
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # models sometimes wrap JSON in a sentence; salvage the outermost object
        for a, b in (("{", "}"), ("[", "]")):
            i, j = text.find(a), text.rfind(b)
            if i != -1 and j > i:
                try:
                    return json.loads(text[i:j + 1])
                except json.JSONDecodeError:
                    continue
    return None


# --------------------------------------------------------------------------- embeddings
def embed(texts, *, batch=32):
    """Returns a list of vectors, or None if embeddings are unavailable."""
    if not LLMConfig.embeddings_available() or not texts:
        return None
    vecs = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        out = _call("embed", f"{LLMConfig.base_url}/embeddings",
                    {"model": LLMConfig.embed_model, "input": chunk})
        if not out:
            return None
        try:
            vecs += [d["embedding"] for d in sorted(out["data"],
                                                    key=lambda d: d.get("index", 0))]
        except (KeyError, TypeError):
            return None
    return vecs


# --------------------------------------------------------------------------- rerank
def rerank(query, documents, *, top_n=8):
    """Returns [(index, score), ...] best-first, or None if unavailable."""
    if not LLMConfig.rerank_available() or not documents:
        return None
    url = LLMConfig.rerank_url or f"{LLMConfig.base_url}/rerank"
    out = _call("rerank", url, {"model": LLMConfig.rerank_model, "query": query,
                                "documents": documents, "top_n": top_n})
    if not out:
        return None
    rows = out.get("results") or out.get("data") or []
    try:
        return [(r["index"], float(r.get("relevance_score", r.get("score", 0))))
                for r in rows]
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- health
def preflight():
    """One cheap call to prove the endpoint works before the run commits to it."""
    if not LLMConfig.available():
        return False, LLMConfig.describe()
    # generous budget: reasoning models spend tokens thinking before they emit,
    # so a tight cap makes a healthy endpoint look broken
    r = chat_json("You are a JSON API.", 'Reply with {"ok":true}',
                  schema_hint='{"ok": true}', max_tokens=512)
    if r and r.get("ok") is True:
        extra = []
        if LLMConfig.embeddings_available():
            extra.append("embeddings OK" if embed(["ping"]) else "embeddings FAILED")
        if LLMConfig.rerank_available():
            extra.append("rerank OK" if rerank("a", ["b", "c"]) else "rerank FAILED")
        return True, f"{LLMConfig.describe()}" + (f"  [{', '.join(extra)}]" if extra else "")
    return False, f"chat call failed against {LLMConfig.base_url} (model {LLMConfig.model})"
