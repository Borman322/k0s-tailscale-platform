"""Фильтр и оценка вакансий по профилю из profile.yml. Чистые функции: ни сети, ни БД."""
import os, re
import yaml

PROFILE = os.environ.get("PROFILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.yml")
_p = yaml.safe_load(open(PROFILE, encoding="utf-8")) or {}

MIN_SCORE = int(_p.get("min_score", 20))

def _word(w):
    # слово целиком (с -s/-es): «helm» не ловится в «overwhelmed», «aws» — в «laws»
    return re.compile(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?:s|es)?(?![a-z0-9])")

def _any(parts):
    return re.compile("|".join(f"(?:{x})" for x in parts), re.I) if parts else None

_kw = _p.get("keywords") or {}
PATTERNS = [(k, _word(str(k)), int(w)) for k, w in {**(_kw.get("strong") or {}), **(_kw.get("normal") or {})}.items()]

TITLE_OK = _any((_p.get("title") or {}).get("include"))
TITLE_NO = _any((_p.get("title") or {}).get("exclude"))

_dom = [str(d) for d in _p.get("domain") or []]
DOMAIN = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(d.lower()) for d in _dom) + r")(?:s|es)?(?![a-z0-9])", re.I) if _dom else None

# ── регион / формат: пометки в blockers, вакансию они не выкидывают ─────────
US_ONLY = re.compile(r"(authorized to work in the (united states|us)|us[- ]only|"
                     r"must (be|reside) (located )?in the united states|u\.s\. citizen)", re.I)
EU_AUTH = re.compile(r"(eu work authorization|right to work in the (eu|european union)|"
                     r"must (be|reside) (located )?in the (eu|european union|eea))", re.I)
LOC_US  = re.compile(r"\b(usa|u\.s\.a?\.?|us|united states|north america)\b", re.I)
LOC_EU  = re.compile(r"\b(europe|emea|eu|european union|uk|united kingdom|dach)\b", re.I)
WORLD   = re.compile(r"\b(worldwide|anywhere|global(ly)?|international)\b", re.I)
ONSITE  = re.compile(r"\b(on-?site|hybrid)\b", re.I)
REMOTE  = re.compile(r"\b(remote|anywhere|worldwide|globally|distributed)\b", re.I)

def title_ok(title):
    return bool(not TITLE_OK or TITLE_OK.search(title)) and not (TITLE_NO and TITLE_NO.search(title))

def in_domain(*parts):
    """Без домена в профиле подходит всё."""
    return DOMAIN is None or bool(DOMAIN.search(" ".join(p for p in parts if p)))

def score_job(title, body, location):
    text = f"{title} {body} {location}".lower()
    matched, score = [], 0
    for kw, rx, w in PATTERNS:
        if rx.search(text):
            score += w
            matched.append(kw)
    if TITLE_NO and TITLE_NO.search(title):  score -= 60
    if TITLE_OK and TITLE_OK.search(title):  score += 15

    blockers = []
    # «Remote - US» — всё равно только для США
    if US_ONLY.search(body) or (LOC_US.search(location) and not WORLD.search(location)):
        blockers.append("us-only")
    if EU_AUTH.search(body) or (LOC_EU.search(location) and not WORLD.search(location)):
        blockers.append("eu-auth")
    if ONSITE.search(location) and not REMOTE.search(location):
        blockers.append("onsite")

    remote = bool(REMOTE.search(location) or REMOTE.search(title))
    lock = "us" if "us-only" in blockers else ("eu" if "eu-auth" in blockers else
           ("none" if remote else "unknown"))
    return score, sorted(set(matched)), sorted(set(blockers)), remote, lock
