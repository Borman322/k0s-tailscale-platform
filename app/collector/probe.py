"""Определение системы найма по slug и проверка «та ли это компания». Общее для discover.py и collector.py."""
import os, sys, json, time, itertools, difflib
from concurrent.futures import ThreadPoolExecutor

from sources import ATS, Http, NoBoard, key, words, SUFFIX
from scoring import in_domain, DOMAIN

HERE  = os.path.dirname(os.path.abspath(__file__))
DATA  = os.environ.get("VACANCIES_DATA", HERE)          # изменяемое состояние (кэш проб, отчёт discover); в контейнере это том /data
CACHE = os.path.join(DATA, ".probe_cache.json")
TTL   = 7 * 86400

BIG  = ["greenhouse", "ashby", "lever"]                 # здесь живёт большинство компаний
# Personio и Workable в массовый перебор по именам не берём: у Workable лимит 429 и пауза в секунду на запрос (перебор
# шёл бы десятки минут). Их slug'и приходят с досок (discover.py --from-boards), адаптеры работают для явных записей и в resolve().
REST = [a for a in ATS if a not in BIG and a not in ("personio", "workable")]


def load_cache():
    try:
        return json.load(open(CACHE))
    except (OSError, ValueError):
        return {}

def save_cache(cache):
    """Кэш — ускорение, а не данные: если писать некуда (read-only ФС, не смонтирован том), прогон не должен падать."""
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE)
    except OSError as e:
        print(f"  кэш проб не сохранён ({e}) — следующий прогон будет медленнее", file=sys.stderr)


# ── кандидаты и сопоставление имён ────────────────────────────────────────
def candidates(name, hints=()):
    w = words(name); core = [x for x in w if x not in SUFFIX] or w
    out = [*hints, "".join(w), "-".join(w), "".join(core), "-".join(core), "".join(core) + "labs", "".join(core) + "hq"]
    return list(dict.fromkeys(c for c in out if c))[:8]

def domain_slugs(name):
    """У части компаний на Ashby slug — домен целиком: jobs.ashbyhq.com/example.com."""
    core = "".join([x for x in words(name) if x not in SUFFIX] or words(name))
    return [core + t for t in (".com", ".io", ".xyz", ".org")] if core else []

def name_match(board, name):
    a, b = key(board or ""), key(name)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)) or difflib.SequenceMatcher(None, a, b).ratio() >= .85


# ── проверка борды ────────────────────────────────────────────────────────
def facts(h, ats, slug):
    """Что лежит на борде: число вакансий, сколько из них по домену (profile.yml), имя борды. None — такой борды нет."""
    seen = [0]
    def want(_t):
        seen[0] += 1
        return seen[0] <= 4         # у ATS без текста в списке описания тянем для первых 4: хватает на оценку
    try:
        jobs = list(itertools.islice(ATS[ats](h, slug, want), 400))
    except NoBoard:
        return None
    board = next((j["company"] for j in jobs if j.get("company")), "")
    if not board and ats == "greenhouse" and jobs:
        try:
            board = h.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}").json().get("name", "")
        except Exception:
            pass
    return dict(n=len(jobs), hits=sum(in_domain(j["title"], j["body"], j.get("company")) for j in jobs),
                board=board, titles=[j["title"] for j in jobs[:3]])

def cached(cache, h, ats, slug):
    k = f"{ats}:{slug}"
    c = cache.get(k)
    if c and time.time() - c["ts"] < TTL:
        return c["f"]
    f = facts(h, ats, slug)             # сетевые ошибки пробрасываем — в кэш их не пишем
    cache[k] = {"ts": time.time(), "f": f}
    return f

NOBODY = {"breezy", "bamboohr", "rippling", "smartrecruiters"}   # в списке нет текста вакансий: facts() подтягивает его только для первых 4

def evidence(f, ats=None):
    """(сколько вакансий с текстом, доля из них по домену)."""
    nt = min(f["n"], 4) if ats in NOBODY else f["n"]
    return nt, (f["hits"] / nt if nt else 0.0)

def verdict(f, name, ats=None):
    """ok — принимаем; maybe — на ручную проверку; None — не то.
    Одного «нашлось 3 вакансии по домену» мало: на борде из 400 вакансий это шум — нужна доля. Имя борды тоже мало:
    короткие слова-названия носят многие тёзки. Без домена в profile.yml доказательство одно — имя борды."""
    if not f:
        return None
    n, hits = f["n"], f["hits"]
    nt, ratio = evidence(f, ats)
    nm = name_match(f["board"], name)
    if n == 0:
        return "ok" if nm else None                       # борда-призрак с тем же именем: пусть лежит, вдруг откроют вакансии
    if DOMAIN is None:                                    # профиль без домена: вакансии ничего не доказывают
        return "ok" if nm else "maybe"
    if (nt >= 3 and hits >= 2 and ratio >= .25) or (0 < nt < 3 and hits >= 1):
        return "ok"
    return "maybe" if ratio >= .08 or (nm and hits >= 1) else None

def mixed(f, ats=None):
    """Компания не из вашей области, но подходящие вакансии у неё есть: берём с gate — только вакансии со словами домена."""
    nt, ratio = evidence(f, ats) if f else (0, 0)
    return bool(f) and f["hits"] >= 3 and ratio >= .05


def resolve(h, cache, slug, name):
    """ATS для slug среди тех, где борда проходит проверку «домен / имя совпало».
    Одного «есть вакансии» мало: тот же slug в другой системе найма может принадлежать совсем другой компании.
    None — нигде нет; если какую-то ATS проверить не удалось (сеть, 429), это ошибка, а не «нет»."""
    err = None
    for ats in ATS:
        try:
            f = cached(cache, h, ats, slug)
        except Exception as e:
            err = err or e
            continue
        if verdict(f, name, ats) == "ok":
            return ats
    if err:
        raise err
    return None


# ── массовый перебор ──────────────────────────────────────────────────────
def probe_company(cache, h, ats, name, cands):
    """Первый подошедший slug в одной ATS; иначе лучший «maybe»."""
    best = None
    for slug in cands:
        try:
            f = cached(cache, h, ats, slug)
        except Exception:
            continue
        v = verdict(f, name, ats)
        if v == "ok":
            return dict(ats=ats, slug=slug, status="ok", **f)
        if v and not best:
            best = dict(ats=ats, slug=slug, status=v, **f)
    return best

def run(cache, todo, ats_list, depth):
    """todo: [(idx, name, cands)] → {idx: [результаты]}. По потоку на каждую ATS: у медленных (Workable, Personio)
    пауза между запросами длиннее, и они не должны тормозить Greenhouse/Ashby/Lever."""
    h, out, done = Http(), {i: [] for i, _, _ in todo}, [0]
    def one(ats, item):
        i, name, cands = item
        cs = cands[:depth]
        if ats == "ashby":
            cs = cs + [c for c in domain_slugs(name) if c not in cs]
        r = probe_company(cache, h, ats, name, cs)
        done[0] += 1
        if done[0] % 200 == 0:
            print(f"    … {done[0]} проверок", file=sys.stderr, flush=True)
        return i, r
    def lane(ats):
        with ThreadPoolExecutor(8 if ats in BIG else 2) as ex:
            return [x for x in ex.map(lambda it: one(ats, it), todo) if x[1]]
    with ThreadPoolExecutor(len(ats_list)) as ex:
        for res in ex.map(lane, ats_list):
            for i, r in res:
                out[i].append(r)
    return out
