"""Knowledge base: a lookup tool the model calls during the call.

The brief asks for answers "by looking it up during the call, not by having
the whole document in the prompt". `"knowledgeBase": "clinic-faq"` in the call
config names `knowledge/clinic-faq.md`. The document is split at its `## `
headings into sections; `lookup_knowledge(query)` returns the best-matching
sections by keyword overlap (section titles count more than body text).

It runs in-process and takes about a millisecond, so no filler line is played
for it. What the lookup adds to a reply is the second model call that reads
the result; REPORT.md measures that on "knowledge" turns.

A keyword match is enough for a one-page FAQ. For a large document I'd swap
`search` for embeddings; the tool's contract (query in, passages out) stays.
"""

import re
import time
from functools import lru_cache
from pathlib import Path

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.calls.store import CallRecord
from app.config import ROOT

KNOWLEDGE_DIR = ROOT / "knowledge"
KNOWLEDGE_TOOL = "lookup_knowledge"
MAX_SECTIONS = 2

KNOWLEDGE_RULES = f"""
For any question about the business itself (what it does, opening hours,
address, parking, prices, insurance, payment, cancellations, emergencies),
call {KNOWLEDGE_TOOL} first and answer only from what it returns, in one or
two short sentences. If it returns nothing relevant, say you don't have that
information and offer to help with something else. Never guess a price or a time.
""".strip()

_STOP = set("""a an and are as at be by can do does for from have how i in is it
me my of on or our the to we what when where which who will with you your""".split())

# Everyday words callers use, mapped to words the document uses.
_SYNONYMS = {
    "cost": "price", "costs": "price", "charge": "price", "charges": "price", "fee": "price",
    "fees": "price", "much": "price", "rate": "price", "expensive": "price",
    "open": "hours", "opening": "hours", "close": "hours", "closing": "hours", "timing": "hours",
    "timings": "hours", "time": "hours", "sunday": "hours", "holiday": "hours",
    "where": "address", "location": "address", "located": "address", "directions": "address",
    "car": "parking", "park": "parking", "bike": "parking", "vehicle": "parking",
    "pay": "payment", "upi": "payment", "card": "payment", "cash": "payment",
    "claim": "insurance", "cashless": "insurance", "policy": "insurance",
    "cancel": "cancellations", "reschedule": "cancellations",
    "pain": "emergencies", "urgent": "emergencies", "emergency": "emergencies", "swelling": "emergencies",
    "services": "about", "treatments": "about", "offer": "about", "clinic": "about",
}


def _terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for w in words:
        if w in _STOP:
            continue
        out.append(w)
        if w in _SYNONYMS:
            out.append(_SYNONYMS[w])
        if len(w) > 3 and w.endswith("s"):
            out.append(w[:-1])
    return out


def knowledge_path(name: str) -> Path | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name or ""):
        return None
    path = KNOWLEDGE_DIR / f"{name}.md"
    return path if path.exists() else None


@lru_cache(maxsize=16)
def load_sections(name: str) -> tuple[tuple[str, str], ...]:
    path = knowledge_path(name)
    if path is None:
        return ()
    sections, title, body = [], None, []
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            if title:
                sections.append((title, "\n".join(body).strip()))
            title, body = line[3:].strip(), []
        elif title:
            body.append(line)
    if title:
        sections.append((title, "\n".join(body).strip()))
    return tuple(sections)


def search(name: str, query: str, k: int = MAX_SECTIONS) -> list[dict]:
    query_terms = set(_terms(query))
    sections = load_sections(name)
    if not query_terms and sections:
        # Only filler words ("what do you do?"): a general question, so the
        # document's first section (its overview) is the answer.
        title, body = sections[0]
        return [{"section": title, "text": body}]
    scored = []
    for title, body in sections:
        title_terms, body_terms = _terms(title), _terms(body)
        score = sum(3 * title_terms.count(t) + body_terms.count(t) for t in query_terms)
        if score:
            scored.append((score, title, body))
    scored.sort(key=lambda s: -s[0])
    return [{"section": title, "text": body} for _, title, body in scored[:k]]


def build_knowledge_tool(record: CallRecord, name: str) -> FunctionSchema:
    async def lookup(params: FunctionCallParams):
        query = str((params.arguments or {}).get("query") or "").strip()
        started = time.perf_counter()
        passages = search(name, query)
        ms = round((time.perf_counter() - started) * 1000, 2)
        result = {"found": bool(passages), "passages": passages}
        record.tools.append({"name": KNOWLEDGE_TOOL, "arguments": {"query": query},
                             "result": {"found": result["found"],
                                        "sections": [p["section"] for p in passages]},
                             "ok": True, "ms": ms, "at": time.time()})
        logger.info(f"[{record.id}] {KNOWLEDGE_TOOL}({query!r}) -> "
                    f"{[p['section'] for p in passages]} in {ms} ms")
        await params.result_callback(result)

    return FunctionSchema(
        name=KNOWLEDGE_TOOL,
        description="Look up facts about the business (what it does, hours, address, parking, "
                    "prices, insurance, payment, cancellations, emergencies) in its document.",
        properties={"query": {"type": "string",
                              "description": "A few keywords for what the caller asked, "
                                             "e.g. 'opening hours', 'price of cleaning', 'parking'"}},
        required=["query"],
        handler=lookup,
    )
