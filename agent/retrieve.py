"""RAG over the pre-indexed passages.

Retrieval is scoped before it is scored: a query for borrower P1 only ever ranks
P1's own passages. Scoping does more for precision than any embedding model,
because the corpus contains ~150 decoy documents that name real borrowers.

Three tiers, each degrading safely into the next:
  1. embeddings (cosine) + reranker   -- best precision
  2. embeddings (cosine)              -- if no reranker
  3. lexical BM25-lite                -- if no API at all; always available

No vector database: 1158 passages is a 4 MB float array. A network round-trip to
Milvus would cost more than the entire scan, and would be one more service that
can be down during a three-hour submission window.
"""
import json
import math
import re
from collections import Counter

import llm
from config import WORK

_STATE = {"chunks": None, "vecs": None, "df": None, "N": 0}
_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9\-]+")


def _tok(s):
    return [w.lower() for w in _TOKEN.findall(s)]


def load(build_vectors=True):
    if _STATE["chunks"] is not None:
        return _STATE["chunks"]
    chunks = json.loads((WORK / "chunks.json").read_text(encoding="utf-8"))
    _STATE["chunks"] = chunks
    _STATE["N"] = len(chunks)

    df = Counter()
    for c in chunks:
        c["_tokens"] = _tok(c["text"])
        df.update(set(c["_tokens"]))
    _STATE["df"] = df

    cache = WORK / "vectors.json"
    if build_vectors and llm.LLMConfigAvailable():
        if cache.exists():
            _STATE["vecs"] = json.loads(cache.read_text(encoding="utf-8"))
        else:
            vecs = llm.embed([c["text"] for c in chunks])
            if vecs:
                _STATE["vecs"] = vecs
                cache.write_text(json.dumps(vecs), encoding="utf-8")
    return chunks


def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return s / (na * nb)


def _bm25(query_tokens, chunk, avg_len, k1=1.5, b=0.75):
    df, N = _STATE["df"], _STATE["N"]
    tf = Counter(chunk["_tokens"])
    dl = len(chunk["_tokens"]) or 1
    score = 0.0
    for q in query_tokens:
        if q not in tf:
            continue
        idf = math.log(1 + (N - df[q] + 0.5) / (df[q] + 0.5))
        score += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * dl / avg_len))
    return score


def search(query, *, scenario_id=None, doc_types=None, k=6):
    """Scoped retrieval. Returns [{score, source, doc_type, text}, ...]."""
    chunks = load()
    pool = [c for c in chunks
            if (scenario_id is None or c["scenario_id"] == scenario_id)
            and (doc_types is None or c["doc_type"] in doc_types)]
    if not pool:
        return []

    vecs = _STATE["vecs"]
    ranked = None
    if vecs:
        qv = llm.embed([query])
        if qv:
            idx = {id(c): i for i, c in enumerate(chunks)}
            ranked = sorted(pool, key=lambda c: -_cos(qv[0], vecs[idx[id(c)]]))
    if ranked is None:
        qt = _tok(query)
        avg = sum(len(c["_tokens"]) for c in pool) / len(pool)
        ranked = sorted(pool, key=lambda c: -_bm25(qt, c, avg))

    top = ranked[: max(k * 4, 20)]
    rr = llm.rerank(query, [c["text"] for c in top], top_n=k)
    if rr:
        top = [top[i] for i, _s in rr if i < len(top)]
    return [{"source": c["file"], "doc_type": c["doc_type"],
             "scenario_id": c["scenario_id"], "text": c["text"]} for c in top[:k]]


def context_for(scenario_id, query, *, k=6, doc_types=None):
    """Retrieved passages formatted for a prompt, each labelled with its source
    so the model's answer can be traced back to a file."""
    hits = search(query, scenario_id=scenario_id, doc_types=doc_types, k=k)
    return "\n\n".join(
        f"[{i+1}] source={h['source']} type={h['doc_type']}\n{h['text']}"
        for i, h in enumerate(hits)), hits


# small indirection so retrieve.py has no import-time dependency on config flags
def _available():
    from config import LLMConfig
    return LLMConfig.embeddings_available()


llm.LLMConfigAvailable = _available
