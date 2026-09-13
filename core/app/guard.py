"""Zero-cost guardrails that run before any model call.

1. Jailbreak / off-task phrase block — the classic "ignore your instructions", role-play and
   "write me a poem" requests are refused without spending retrieval or LLM budget.
2. The retrieval gate (chat.py) then refuses clear misses by dense score. Anything in the
   ambiguous band goes to the LLM, whose system prompt is the correctness guard (rule 3).
"""
from __future__ import annotations

import re

_PATTERNS = [
    # English
    r"\bignore (all |your |the |previous |prior )*(instructions|rules|prompt)",
    r"\b(system prompt|developer message|jailbreak|DAN mode)\b",
    r"\b(pretend|act|role[- ]?play) (you are|to be|as)\b",
    r"\bwrite (me )?(a |an )?(poem|song|story|rap|joke|essay)\b",
    r"\btell me a (joke|story)\b",
    # French
    r"\bignore[sz]? (tes|les|toutes les) (instructions|règles|consignes)",
    r"\b(écris|ecris|raconte)[- ]?(moi )?(un|une) (poème|poeme|chanson|histoire|blague)\b",
    r"\bfais semblant\b|\bjoue le rôle\b",
    # Egyptian Arabic / MSA
    r"(انسى|انسي|تجاهل|إتجاهل)\s+(التعليمات|القواعد|الأوامر|الاوامر)",
    r"(اكتبلي|اكتب لي|اكتب)\s+(قصيدة|أغنية|اغنية|قصة|نكتة)",
    r"(احكيلي|احكي لي|قولي|قول لي)\s+(نكتة|قصة)",
    r"(اتصرف|مثّل|مثل)\s+(كأنك|إنك|انك)",
]
_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)


def is_blocked(message: str) -> bool:
    return bool(_RE.search(message))
