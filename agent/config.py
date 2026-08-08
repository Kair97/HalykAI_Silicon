"""Agent configuration. Every provider detail comes from the environment, so the
agent runs unchanged against Alem AI Plus, OpenAI, or nothing at all.

Copy .env.example to .env and fill it in. Nothing here has a secret baked in.
"""
import os
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HALYK_ROOT", AGENT_DIR.parent))
# The dataset directory is an explicit input, not a fixed folder name: on
# submission day the private archive may live anywhere. `--archive` sets this.
# Every module must resolve it the same way or one of them will look for a key
# that is not there.
ARCHIVE = Path(os.environ.get("HALYK_ARCHIVE", ROOT / "agentic-bank-public"))
WORK = Path(os.environ.get("AGENT_WORK", AGENT_DIR / "work"))
CACHE = WORK / "cache"


def _load_dotenv():
    """Minimal .env reader -- avoids a dependency for six lines of work."""
    for p in (AGENT_DIR / ".env", ROOT / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


class LLMConfig:
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LLM_API_KEY", "")
    # primary reasoning model, and a second one used only to cross-check
    # low-confidence verdicts (disagreement => escalate, never silently average)
    model = os.environ.get("LLM_MODEL", "")
    model_b = os.environ.get("LLM_MODEL_B", "")
    embed_model = os.environ.get("EMBED_MODEL", "")
    rerank_model = os.environ.get("RERANK_MODEL", "")
    rerank_url = os.environ.get("RERANK_URL", "")
    timeout = float(os.environ.get("LLM_TIMEOUT", "90"))
    max_retries = int(os.environ.get("LLM_MAX_RETRIES", "4"))
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
    # hard ceiling so a runaway loop cannot eat the submission window
    budget_calls = int(os.environ.get("LLM_BUDGET_CALLS", "600"))

    @classmethod
    def available(cls):
        return bool(cls.base_url and cls.api_key and cls.model)

    @classmethod
    def embeddings_available(cls):
        return bool(cls.base_url and cls.api_key and cls.embed_model)

    @classmethod
    def rerank_available(cls):
        return bool(cls.api_key and (cls.rerank_url or cls.rerank_model))

    @classmethod
    def describe(cls):
        if not cls.available():
            return ("LLM DISABLED (no LLM_BASE_URL / LLM_API_KEY / LLM_MODEL) -- "
                    "the agent will run in deterministic-only mode")
        bits = [f"chat={cls.model}"]
        if cls.model_b:
            bits.append(f"cross-check={cls.model_b}")
        if cls.embed_model:
            bits.append(f"embed={cls.embed_model}")
        if cls.rerank_model:
            bits.append(f"rerank={cls.rerank_model}")
        return f"LLM enabled @ {cls.base_url}  [{', '.join(bits)}]"


class Trace:
    """Langfuse is optional; absence must never break a run."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("LANGFUSE_HOST", "")

    @classmethod
    def available(cls):
        return bool(cls.public_key and cls.secret_key and cls.host)


SUBMISSION = {
    "team": os.environ.get("TEAM_NAME", "KOZ"),
    "contact_email": os.environ.get("CONTACT_EMAIL", "orynbekkairo@gmail.com"),
    "model": os.environ.get("SUBMISSION_MODEL_LABEL", "") or (LLMConfig.model or "rule-based"),
}
