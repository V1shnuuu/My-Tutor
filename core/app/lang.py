"""Per-message language detection: Arabic script → 'ar' (answered in Egyptian dialect),
Arabizi (Arabic in Latin letters) → 'ar' with arabizi=True, otherwise English vs French.
Microsecond-cheap; runs on every message."""
from __future__ import annotations

import re
from dataclasses import dataclass

ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿ]")
LATIN_RE = re.compile(r"[A-Za-zÀ-ÿ]")
ARABIZI_DIGIT_RE = re.compile(r"(?i)\b[a-z]*[23579][a-z]+\b|\b[a-z]+[23579][a-z]*\b")

# Egyptian Arabizi function words / very common tokens (Latin script).
ARABIZI_LEX = {
    "ana", "enta", "enti", "ento", "e7na", "ehna", "howa", "heya", "homa", "eh", "eih", "ezay", "ezzay",
    "leh", "leeh", "fen", "feen", "emta", "kam", "keda", "kda", "mesh", "msh", "mish", "3ayez", "3ayza",
    "3awez", "3awza", "3ashan", "3shan", "lazem", "momken", "mumken", "ma3lesh", "ma3lish", "tamam",
    "tmam", "aywa", "ah", "la2", "la", "yani", "ya3ni", "y3ni", "bas", "bs", "wala", "walla", "delwa2ty",
    "dlw2ty", "bokra", "embareh", "el", "al", "fel", "fi", "fe", "3ala", "3la", "ma3", "m3", "men", "mn",
    "3an", "howa", "hya", "dah", "da", "di", "dol", "kol", "kul", "7aga", "7agat", "shwaya", "shwya",
    "kteer", "ktir", "awi", "awy", "gedan", "gdn", "tab", "tayeb", "tyb", "khalas", "5alas", "yalla",
    "ezayak", "ezayek", "3amel", "3amla", "3andak", "3andi", "3ndi", "3ndk", "mafish", "mfish", "fih",
    "feeh", "ba2a", "b2a", "lesa", "lessa", "abl", "ba3d", "b3d", "2abl", "2oli", "2olly", "2ol", "2al",
    "esh", "ish", "ymken", "yemken", "ely", "elly", "illi", "sho", "shu", "we", "w", "ya", "wallahi",
    "mohem", "mhm", "sa7", "sa7ee7", "ghalat", "3'alat", "fahem", "fahma", "fhmt", "fehemt", "mafhemtsh",
    "mafhmtsh", "e7ki", "eshra7", "eshra7ly", "esra7", "ezaay", "ezai", "lih", "lyh", "3lshan",
}

EN_LEX = {
    "the", "is", "are", "what", "how", "why", "when", "where", "which", "does", "do", "can", "explain",
    "and", "of", "to", "in", "a", "an", "this", "that", "with", "for", "it", "be", "was", "were", "about",
    "difference", "between", "mean", "means", "example", "please", "you", "i", "we", "they", "not",
    "lecture", "video", "question", "answer", "again", "more", "tell", "me", "show", "step", "steps",
}
FR_LEX = {
    "le", "la", "les", "est", "sont", "quoi", "que", "qu", "quel", "quelle", "quels", "quelles",
    "comment", "pourquoi", "quand", "où", "ou", "peux", "peut", "explique", "expliquer", "et", "de",
    "des", "du", "dans", "un", "une", "ce", "cette", "ces", "avec", "pour", "il", "elle", "ils", "elles",
    "je", "nous", "vous", "tu", "pas", "ne", "différence", "entre", "veut", "dire", "exemple", "cours",
    "vidéo", "question", "réponse", "encore", "plus", "moi", "montre", "étape", "étapes", "c'est", "s'il",
    "vous", "plaît", "merci", "bonjour", "qu'est", "est-ce",
}


@dataclass
class Detected:
    lang: str            # ar | en | fr
    arabizi: bool = False
    confidence: float = 1.0


def detect(text: str, previous: str | None = None) -> Detected:
    t = text.strip()
    if not t:
        return Detected(previous or "en", confidence=0.0)
    arabic = len(ARABIC_RE.findall(t))
    latin = len(LATIN_RE.findall(t))
    letters = arabic + latin
    if letters == 0:
        return Detected(previous or "en", confidence=0.0)
    if arabic / letters > 0.3:
        return Detected("ar")

    tokens = re.findall(r"(?i)[a-zà-ÿ0-9']+", t.lower())
    if not tokens:
        return Detected(previous or "en", confidence=0.0)

    # Arabizi: digit-inside-word pattern or lexicon hits
    digit_words = len(ARABIZI_DIGIT_RE.findall(t))
    arz_hits = sum(1 for w in tokens if w in ARABIZI_LEX)
    en_hits = sum(1 for w in tokens if w in EN_LEX)
    fr_hits = sum(1 for w in tokens if w in FR_LEX)
    if digit_words >= 1 or (arz_hits >= 2 and arz_hits > max(en_hits, fr_hits)):
        return Detected("ar", arabizi=True, confidence=0.8)
    if arz_hits >= 1 and en_hits == 0 and fr_hits == 0 and len(tokens) <= 3:
        return Detected("ar", arabizi=True, confidence=0.6)

    # French accents are a strong signal
    if re.search(r"[éèêàâçùûôîœ]", t.lower()) and fr_hits >= en_hits:
        return Detected("fr", confidence=0.9)
    if fr_hits > en_hits:
        return Detected("fr", confidence=min(1.0, 0.5 + 0.1 * fr_hits))
    if en_hits > fr_hits:
        return Detected("en", confidence=min(1.0, 0.5 + 0.1 * en_hits))

    # Tie / no lexicon hits: statistical fallback for longer text, else previous language
    if len(tokens) >= 4:
        try:
            from langdetect import DetectorFactory, detect_langs

            DetectorFactory.seed = 0
            best = detect_langs(t)[0]
            if best.lang in ("en", "fr"):
                return Detected(best.lang, confidence=float(best.prob))
        except Exception:
            pass
    return Detected(previous if previous in ("en", "fr") else "en", confidence=0.4)


# Rule-based Arabizi → Arabic-script transliteration used only to widen retrieval
# (the LLM sees the original text). Lossy by design.
_ARZ_MAP = [
    ("sh", "ش"), ("ch", "ش"), ("th", "ث"), ("kh", "خ"), ("gh", "غ"), ("3'", "غ"), ("7'", "خ"),
    ("2", "ء"), ("3", "ع"), ("5", "خ"), ("6", "ط"), ("7", "ح"), ("8", "ق"), ("9", "ص"),
    ("a", "ا"), ("b", "ب"), ("c", "ك"), ("d", "د"), ("e", "ي"), ("f", "ف"), ("g", "ج"), ("h", "ه"),
    ("i", "ي"), ("j", "ج"), ("k", "ك"), ("l", "ل"), ("m", "م"), ("n", "ن"), ("o", "و"), ("p", "ب"),
    ("q", "ق"), ("r", "ر"), ("s", "س"), ("t", "ت"), ("u", "و"), ("v", "ف"), ("w", "و"), ("x", "كس"),
    ("y", "ي"), ("z", "ز"),
]


def transliterate_arabizi(text: str) -> str:
    out = []
    for word in text.lower().split():
        i, buf = 0, []
        while i < len(word):
            for src, dst in _ARZ_MAP:
                if word.startswith(src, i):
                    buf.append(dst)
                    i += len(src)
                    break
            else:
                buf.append(word[i])
                i += 1
        out.append("".join(buf))
    return " ".join(out)
