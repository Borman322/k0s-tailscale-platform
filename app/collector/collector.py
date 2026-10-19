#!/usr/bin/env python3
"""Сборщик вакансий. Основной путь — напрямую в API систем найма (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Recruitee, Personio, Teamtailor, BambooHR, Rippling, Gem, Breezy). Доски-агрегаторы
(раздел boards в companies.yml) — дополнение: ловят то, чего нет в списке компаний.

  python collector.py                         обход всех источников → Postgres (DB_DSN)
  python collector.py --dry-run               без БД: печатает лучшие находки, ничего не пишет
  python collector.py --only gitlab,datadog   только эти компании/доски (по имени, без регистра)
  python collector.py --json out.json         дополнительно сохранить находки в файл
Список компаний пополняет discover.py, схема БД — schema.sql.
"""
import os, re, sys, json, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import yaml

from sources import ATS, Http, key
from scoring import score_job, title_ok, in_domain, MIN_SCORE
import probe
try:
    from boards import BOARDS          # {имя: (функция, нужен_ли_фильтр_по_домену)}
except ImportError:
    BOARDS = {}

DB   = os.environ.get("DB_DSN", "postgresql://jobs:jobs@db:5432/jobs")
HERE = os.path.dirname(os.path.abspath(__file__))

# ── хранилище ─────────────────────────────────────────────────────────────
UPSERT = """
    INSERT INTO jobs (source,company,external_id,title,url,location,
                      remote,region_lock,score,matched,blockers,posted_at)
    VALUES %s
    ON CONFLICT (source,company,external_id) DO UPDATE
      SET last_seen=now(), is_open=TRUE, score=EXCLUDED.score,
          title=EXCLUDED.title, location=EXCLUDED.location,
          matched=EXCLUDED.matched, blockers=EXCLUDED.blockers
    RETURNING id, (xmax = 0) AS inserted
"""

class Pg:
    def __init__(self, dsn):
        import psycopg2                              # только здесь: для --dry-run драйвер не нужен
        from psycopg2.extras import execute_values
        self.ev = execute_values
        self.conn = psycopg2.connect(dsn); self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.seen = []

    def save(self, source, company, rows, fetched, err):
        new = 0
        if rows:
            res = self.ev(self.cur, UPSERT, rows, fetch=True)   # без fetch=True fetchall() отдаёт лишь последнюю страницу (100 строк)
            self.seen += [r[0] for r in res]
            fresh = [r[0] for r in res if r[1]]
            new = len(fresh)
            if fresh:
                self.ev(self.cur, "INSERT INTO pipeline(job_id) VALUES %s ON CONFLICT DO NOTHING", [(i,) for i in fresh])
        self.cur.execute("INSERT INTO runs(source,company,fetched,inserted,error) VALUES(%s,%s,%s,%s,%s)",
                         (source, company, fetched, len(rows), err))
        return new

    def close_missing(self, companies, boards):
        """Закрываем только то, что относится к успешно опрошенным источникам. Раньше закрывалось всё, чего не было
        в выдаче, — один сбой сети по компании «закрывал» все её вакансии. Компания может сменить ATS,
        поэтому для компаний область — имя компании по всем ATS, а не пара (ATS, компания)."""
        cond, args = [], [self.seen]
        if companies:
            cond.append("(company = ANY(%s) AND source = ANY(%s))"); args += [list(companies), list(ATS)]
        if boards:
            cond.append("source = ANY(%s)"); args.append(list(boards))
        if not cond:
            return 0
        self.cur.execute(f"UPDATE jobs SET is_open=FALSE WHERE is_open AND id <> ALL(%s) AND ({' OR '.join(cond)})", args)
        return self.cur.rowcount

    def close(self):
        self.cur.close(); self.conn.close()

class Dry:
    """Без БД: копим находки в памяти."""
    def __init__(self): self.rows = []
    def save(self, source, company, rows, fetched, err):
        self.rows += rows
        return len(rows)
    def close_missing(self, companies, boards): return 0
    def close(self): pass

COLS = ("source", "company", "external_id", "title", "url", "location", "remote", "region_lock", "score", "matched", "blockers", "posted_at")


# ── сбор ──────────────────────────────────────────────────────────────────
def fetch(h, cache, kind, item):
    """Одна компания или одна доска → {source, company, jobs, gate, err}. Исключения не выпускаем: сбой одного источника не рушит остальные."""
    src = item.get("ats") if kind == "company" else item["name"]
    try:
        if kind == "company":
            name, slug, src = item["name"], item["slug"], item.get("ats", "probe")
            if src == "probe":
                src = probe.resolve(h, cache, slug, name)
                if not src:
                    return dict(source="probe", company=name, jobs=[], gate=False, missing=True,
                                err="не найдена ни в одной системе найма")
            if src not in ATS:
                return dict(source=src, company=name, jobs=[], gate=False, missing=True, err=f"неизвестная ATS «{src}»")
            return dict(source=src, company=name, jobs=list(ATS[src](h, slug, title_ok)), gate=bool(item.get("gate")), err=None)
        fn, gate = BOARDS[item["name"]]
        return dict(source=src, company=src, jobs=list(fn(h, item)), board=True, gate=item.get("gate", gate), err=None)
    except Exception as e:
        return dict(source=src or "?", company=item["name"], jobs=[], gate=False, err=f"{type(e).__name__}: {e}"[:300])

def build_rows(res, known, min_score, dup=None):
    """Фильтр по заголовку → (домен для широких досок) → оценка. Строки в порядке колонок jobs.
    dup — {(компания, заголовок): источник} уже принятых вакансий: одна и та же вакансия приходит с нескольких досок."""
    rows, seen, twins = [], set(), {}
    dup = {} if dup is None else dup
    for j in res["jobs"]:
        company = (j.get("company") if res.get("board") else None) or res["company"]
        if res.get("board") and key(company) in known:
            continue                                  # у компании есть прямая борда — она полнее и свежее
        pair = (key(company), re.sub(r"\W+", " ", (j.get("title") or "").lower()).strip())
        if res.get("board") and dup.get(pair, res["source"]) != res["source"]:
            continue                                  # уже взяли эту вакансию с другого источника
        title, body, loc = j["title"], j.get("body") or "", j.get("location") or ""
        if not j.get("external_id") or not title_ok(title):
            continue
        if res["gate"] and not in_domain(title, body, company, " ".join(j.get("tags") or [])):
            continue                                  # широкая доска: без слов домена из profile.yml — не наша область
        sc, matched, blockers, remote, lock = score_job(title, body, loc)
        if sc < min_score:
            continue
        nul = lambda v: v.replace("\x00", "")          # Postgres не принимает NUL в text — упала бы вся пачка INSERT
        row = (res["source"], nul(company), nul(str(j["external_id"])), nul(title), nul(j.get("url") or ""), nul(loc),
               remote, lock, sc, matched, blockers, j.get("posted_at"))
        if row[:3] in seen:                           # дубль в одной выдаче рушит ON CONFLICT ... DO UPDATE
            continue
        seen.add(row[:3])
        # одна вакансия, размноженная по городам (бывает по 10+ копий): оставляем одну строку, считаем локации
        twin = (key(company), title.lower(), hash(body[:3000]), lock, tuple(blockers))
        if twin in twins:
            twins[twin][1] += 1
            continue
        twins[twin] = [len(rows), 1]
        rows.append(row)
        dup.setdefault(pair, res["source"])
    for i, n in twins.values():
        if n > 1:
            r = rows[i]
            rows[i] = r[:5] + (f"{r[5]} +{n - 1} локаций",) + r[6:]
    return rows


# ── основной проход ───────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="имена компаний/досок через запятую")
    ap.add_argument("--json", help="сохранить находки в файл")
    ap.add_argument("--min-score", type=int, default=MIN_SCORE)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--top", type=int, default=40, help="сколько строк печатать в --dry-run")
    a = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(HERE, "companies.yml"), encoding="utf-8")) or {}
    companies = [dict(c, name=str(c["name"]), slug=str(c["slug"])) for c in cfg.get("companies") or []]
    boards = [dict(name=k, **(v or {})) for k, v in (cfg.get("boards") or {}).items()
              if (v or {}).get("enabled", True) and k in BOARDS]
    skipped = [k for k in (cfg.get("boards") or {}) if k not in BOARDS]
    if skipped:
        print(f"  доски без адаптера пропущены: {', '.join(skipped)}", file=sys.stderr)
    if a.only:
        want = {x.strip().lower() for x in a.only.split(",")}
        companies = [c for c in companies if c["name"].lower() in want or c["slug"].lower() in want]
        boards = [b for b in boards if b["name"].lower() in want]
        if not companies and not boards:
            sys.exit(f"--only {a.only}: ничего не найдено в companies.yml")

    h, cache = Http(), probe.load_cache()
    store = Dry() if a.dry_run else Pg(DB)
    jobs = [("company", c) for c in companies] + [("board", b) for b in boards]
    ok_companies, ok_boards, total_new, total = set(), set(), 0, len(jobs)
    covered, late, stats, dup = set(), [], {}, {}    # covered — компании, чью прямую борду прочитали и там есть вакансии

    def handle(n, res):
        nonlocal total_new
        rows = build_rows(res, covered, a.min_score, dup)
        fetched = len(res["jobs"])
        if res["err"]:
            print(f"  [{n:>3}/{total}] {res['company']:24s} ОШИБКА {res['err']}", flush=True)
            store.save("probe" if res.get("missing") else res["source"], res["company"], [], 0, res["err"])
            return
        total_new += store.save(res["source"], res["company"], rows, fetched, None)
        (ok_boards if res.get("board") else ok_companies).add(res["source"] if res.get("board") else res["company"])
        stats[(res["source"], res["company"])] = (fetched, len(rows))
        print(f"  [{n:>3}/{total}] {res['company']:24s} {res['source']:16s} вакансий {fetched:>4}  подошло {len(rows):>3}", flush=True)

    with ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(fetch, h, cache, k, it) for k, it in jobs]
        for n, f in enumerate(as_completed(futs), 1):
            res = f.result()
            if res.get("board"):
                late.append((n, res))             # доски — после компаний: им нужно знать, что уже покрыто прямой бордой
                continue
            if not res["err"] and res["jobs"]:
                covered.add(key(res["company"]))
            handle(n, res)
    for n, res in late:
        handle(n, res)

    closed = store.close_missing(ok_companies, ok_boards)
    probe.save_cache(cache)

    if a.dry_run:
        rows = sorted(store.rows, key=lambda r: -r[8])
        print(f"\nподошло вакансий: {len(rows)} из {sum(f for f, _ in stats.values())} просмотренных; топ-{a.top}:")
        for r in rows[:a.top]:
            print(f"  {r[8]:>3} {r[7]:7s} {'R' if r[6] else '-'} {r[1][:20]:20s} {r[3][:56]:56s} {r[5][:26]:26s} "
                  f"{','.join(r[10])[:22]:22s} {r[4]}")
        if a.json:
            json.dump([dict(zip(COLS, r)) for r in rows], open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    else:
        print(f"\nновых вакансий: {total_new}; закрыто: {closed}", flush=True)
        if a.json:
            print("--json работает только с --dry-run", file=sys.stderr)
    store.close()

if __name__ == "__main__":
    main()
