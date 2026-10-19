#!/usr/bin/env python3
"""Подбор системы найма и slug для компаний → companies.yml.

Берёт seed_companies.txt и строки companies.yml с `ats: probe`, перебирает варианты slug по всем
поддерживаемым ATS и принимает кандидата, только если это похоже на нужную компанию, а не на тёзку:
в вакансиях встречаются слова домена из profile.yml или имя борды совпадает с названием.

  python discover.py                    весь seed + probe-строки yml; найденное записать в companies.yml
  python discover.py --dry-run          ничего не писать, только отчёт
  python discover.py --name "Foo Bar"   одна компания (без записи)
  python discover.py --from-boards      взять компании из вакансий на досках (boards.py), подходящих под фильтр, и подобрать им ATS
  python discover.py --harvest f.txt …  файлы со строками «ats,slug,Компания» (известные slug'и) — проверить и добавить
Результаты проверок кэшируются в .probe_cache.json на неделю (в т.ч. «такой борды нет»).
"""
import os, re, sys, time, argparse, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import yaml

from sources import ATS, Http, key, ats_from_url
from scoring import title_ok, in_domain
from boards import BOARDS
from probe import (BIG, REST, DATA, candidates, cached, verdict, mixed, run, load_cache, save_cache)

HERE   = os.path.dirname(os.path.abspath(__file__))
YML    = os.path.join(HERE, "companies.yml")
SEED   = os.path.join(HERE, "seed_companies.txt")
REPORT = os.path.join(DATA, "discover_report.md")


# ── ввод/вывод ────────────────────────────────────────────────────────────
def read_seed(path):
    group, out = "Прочее", []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("##"):
            group = line.lstrip("# ").strip()
        elif line and not line.startswith("#"):
            name, _, hints = line.partition("|")
            hints = [x.strip() for x in hints.split(",") if x.strip()]
            out.append(dict(name=name.strip(), group=group, hints=[x for x in hints if not x.startswith("!")],
                            flags=[x[1:] for x in hints if x.startswith("!")]))
    return out

def q(v):
    """Значение для yml: в кавычки, если там спецсимволы или YAML прочитает не строкой (slug «no» → False, «1e5» → число)."""
    v = str(v)
    try:
        plain = isinstance(yaml.safe_load(v), str)
    except yaml.YAMLError:
        plain = False
    return f'"{v}"' if not plain or re.search(r"""[,{}\[\]#:&*!|>'"%@`]|^\s|\s$""", v) else v

HEADER = """# Компании и их идентификатор в системе найма: name, slug, ats (greenhouse, ashby, lever, workable, …).
# Если система неизвестна — ставь `ats: probe`: сборщик определит её сам (или запусти discover.py).
# Секции «── … ──» собирает discover.py: записи сохраняются, комментарии внутри секций — нет."""

def emit(header, groups, tail=""):
    L = list(header) + ["companies:"]
    for title, items in groups:
        if not items:
            continue
        L += ["", f"# ── {title} ──"]
        for e in items:
            extra = "".join(f", {k}: {q(v)}" for k, v in e.items() if k not in ("name", "slug", "ats"))
            L.append(f"  - {{name: {q(e['name']) + ',':<26} slug: {q(e['slug']) + ',':<26} ats: {e['ats']}{extra}}}")
    return "\n".join(L) + "\n" + (("\n" + tail) if tail else "")


# ── выбор и разбор yml ────────────────────────────────────────────────────
def read_groups(raw):
    """В какой секции yml стоит запись: чтобы повторный запуск не перекладывал компании между секциями."""
    out, group = {}, None
    for line in raw.splitlines():
        m = re.match(r"# ── (.+) ──\s*$", line)
        if m:
            group = m.group(1)
        elif group and line.lstrip().startswith("- {"):
            try:
                out[key(yaml.safe_load(line.strip()[2:])["name"])] = group
            except Exception:
                pass
    return out

def pick(rs):
    """Из подтверждённых результатов — лучшая ATS (больше вакансий по домену) и все её подтверждённые slug'и:
    у компании бывает несколько борд (бывает две на одной ATS). Пустые борды-призраки берём, только если другого нет."""
    ok = [r for r in rs if r["status"] == "ok"]
    if not ok:
        return []
    best = max({r["ats"] for r in ok}, key=lambda a: (sum(r["hits"] for r in ok if r["ats"] == a),
                                                       sum(r["n"] for r in ok if r["ats"] == a)))
    picked = list({r["slug"].lower(): r for r in ok if r["ats"] == best}.values())      # slug'и ATS нечувствительны к регистру
    return [r for r in picked if r["n"]] or picked[:1]

def harvest(cache, paths, book, res, items=()):
    """Файлы «ats,slug,Компания» и готовые тройки с досок: slug уже известен, перебор не нужен — только проверка, что это та компания."""
    items = list(dict.fromkeys(tuple(i) for i in items))
    for path in paths or ():
        for line in open(path, encoding="utf-8"):
            p = [x.strip() for x in line.strip().split(",", 2)]
            if len(p) == 3 and p[0] in ATS and p[1] and p[2]:
                items.append(tuple(p))
    h = Http()
    def one(p):
        try:
            return p, cached(cache, h, p[0], p[1])
        except Exception:
            return p, None
    with ThreadPoolExecutor(8) as ex:
        for (ats, slug, name), f in ex.map(one, items):
            v = verdict(f, name, ats)
            gated = v != "ok" and mixed(f, ats)           # борда есть, но компания не из вашей области → с gate
            if v == "ok" or gated:
                book.setdefault(key(name), dict(name=name, group="Найдено на досках", hints=[], flags=[], fixed=None))
                res.setdefault(key(name), []).append(dict(ats=ats, slug=slug, status="ok", gate=gated, **f))
    print(f"    harvest: {len(items)} строк", file=sys.stderr)


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--name"); ap.add_argument("--from-boards", action="store_true")
    ap.add_argument("--harvest", nargs="+", metavar="FILE"); ap.add_argument("--depth", type=int, default=6, help="сколько вариантов slug пробовать в больших ATS")
    a = ap.parse_args()

    raw = open(YML, encoding="utf-8").read() if os.path.exists(YML) else ""
    header = HEADER.splitlines()
    # всё, что в yml после секции companies (например, boards:), сохраняем как есть
    m = re.search(r"(?m)^(?:#[^\n]*\n)*(?!companies:)[A-Za-z_]+:", raw)
    tail = raw[m.start():] if m else ""
    cur = (yaml.safe_load(raw) or {}).get("companies", []) if raw else []
    old_groups, cache = read_groups(raw), load_cache()

    # единый список: ключ → запись; записи yml с конкретной ATS не перепроверяем (хотите перепроверить — поставьте ats: probe)
    book = {}
    for s in (read_seed(SEED) if os.path.exists(SEED) else []):
        book.setdefault(key(s["name"]), dict(name=s["name"], group=s["group"], hints=s["hints"], flags=s.get("flags", []), fixed=None))
    for c in cur:
        e = book.setdefault(key(c["name"]), dict(name=c["name"], group=old_groups.get(key(c["name"]), "Добавлено вручную"),
                                                 hints=[], flags=[], fixed=None))
        e["name"] = c["name"]                       # написание из yml главнее seed
        e.setdefault("orig", []).append(dict(c))
        if c.get("ats") not in (None, "probe"):
            e["fixed"] = (e["fixed"] or []) + [dict(c)]
        else:
            e["hints"] = [c["slug"], *e["hints"]]
    hv = []                                         # (ats, slug, компания), найденные прямо на досках
    if a.from_boards:                               # компании из вакансий на досках: ATS и slug берём из подсказки/ссылки, иначе подбираем по имени
        h, cfgb = Http(), (yaml.safe_load(raw) or {}).get("boards") or {}
        for bname, (fn, _gate) in BOARDS.items():
            bc = cfgb.get(bname) or {}
            if not bc.get("enabled", True):
                continue
            try:
                found = 0
                for j in fn(h, bc):
                    c = (j.get("company") or "").strip()
                    hint = j.get("ats") or next((r for u in [j.get("url"), *(j.get("links") or [])] if (r := ats_from_url(u))), None)
                    if hint and hint[0] in ATS and c:
                        hv.append((hint[0], hint[1], c))
                    elif c and title_ok(j["title"]) and in_domain(j["title"], j.get("body"), c, " ".join(j.get("tags") or [])):
                        if key(c) not in book:
                            found += 1
                        book.setdefault(key(c), dict(name=c, group="Найдено на досках", hints=[], flags=[], fixed=None))
                print(f"    доска {bname}: новых компаний {found}", file=sys.stderr)
            except Exception as e:
                print(f"    доска {bname}: {type(e).__name__}: {e}", file=sys.stderr)
    if a.name:
        book = {key(a.name): dict(name=a.name, group="", hints=[], flags=[], fixed=None)}

    todo = [(k, e["name"], candidates(e["name"], e["hints"])) for k, e in book.items() if not e["fixed"]]
    print(f"к проверке: {len(todo)} компаний; зафиксировано в yml: {sum(1 for e in book.values() if e['fixed'])}", file=sys.stderr)
    t0 = time.time()
    res = run(cache, todo, BIG, a.depth)
    left = [(i, n, c) for i, n, c in todo if not any(r["status"] == "ok" for r in res[i])]
    print(f"большие ATS: {len(todo) - len(left)} найдено за {time.time()-t0:.0f}с; остальные ATS для {len(left)} компаний", file=sys.stderr)
    for i, rs in run(cache, left, REST, 3).items():
        res[i] += rs
    if a.harvest or hv:
        harvest(cache, a.harvest, book, res, hv)
    save_cache(cache)

    final, groups, rep = {}, {}, {"ok": [], "maybe": [], "none": []}
    for k, e in book.items():
        if e["fixed"]:
            final[k] = e["fixed"]
        else:
            picks = pick(res.get(k, []))
            if picks:
                final[k] = [dict(name=e["name"], slug=r["slug"], ats=r["ats"], **({"gate": True} if "gate" in e["flags"] or r.get("gate") else {})) for r in picks]
                rep["ok"] += [(e["name"], r) for r in picks]
            else:
                maybe = sorted(res.get(k, []), key=lambda r: (-r["hits"], -r["n"]))
                rep["maybe" if maybe else "none"].append((e["name"], maybe[0]) if maybe else e["name"])
    # одна борда — одна запись: одна борда подходит нескольким названиям (продукты одной компании), иначе вакансии лежали бы в БД в 3–4 копиях.
    # Выигрывает название из исходного yml пользователя, затем более короткое.
    seen_board = {}
    for k, ents in final.items():
        for ent in ents:
            b = (ent["ats"], str(ent["slug"]).lower())
            rank = (0 if book[k].get("orig") else 1, len(ent["name"]))
            if b not in seen_board or rank < seen_board[b][0]:
                seen_board[b] = (rank, k, ent)
    keep = {id(v[2]) for v in seen_board.values()}
    before = set(final)
    final = {k: [ent for ent in ents if id(ent) in keep] for k, ents in final.items()}
    final = {k: ents for k, ents in final.items() if ents}
    merged = before - set(final)                   # их борда отдана другому названию — это не «не определено»
    for k, e in book.items():
        if k in final:
            groups.setdefault(e["group"], []).extend(final[k])
    for k, e in book.items():                      # строки пользователя, которые не определились, не теряем
        if k not in final and k not in merged and e.get("orig"):
            groups.setdefault("Не определено (проверить вручную)", []).extend(e["orig"])
    order = [g for g in dict.fromkeys([s["group"] for s in read_seed(SEED)] if os.path.exists(SEED) else [])]
    ordered = [(g, groups.pop(g)) for g in order if g in groups] + list(groups.items())

    lines = [f"# discover {dt.date.today()} — найдено {len(rep['ok'])}, на проверку {len(rep['maybe'])}, не найдено {len(rep['none'])}", ""]
    lines += ["## Найдено", "| компания | ATS | slug | вакансий | по домену | борда |", "|---|---|---|---|---|---|"]
    lines += [f"| {n} | {r['ats']} | {r['slug']} | {r['n']} | {r['hits']} | {r['board']} |" for n, r in sorted(rep["ok"], key=lambda x: x[0].lower())]
    lines += ["", "## На ручную проверку (борда есть, но доказательств мало)", "| компания | ATS | slug | вакансий | по домену | борда | первые заголовки |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {n} | {r['ats']} | {r['slug']} | {r['n']} | {r['hits']} | {r['board']} | {'; '.join(r['titles'])[:80]} |" for n, r in sorted(rep["maybe"], key=lambda x: x[0].lower())]
    lines += ["", "## Не найдено ни в одной поддерживаемой ATS (свой сайт, Workday, Notion и т.п.)", ", ".join(sorted(rep["none"], key=str.lower))]
    print("\n".join(lines))
    if not (a.dry_run or a.name):
        out = emit(header, ordered, tail)
        want = sum(len(items) for _, items in ordered)
        try:                                        # самопроверка: записанное должно читаться и содержать все записи
            back = yaml.safe_load(out)
            assert len(back["companies"]) == want and isinstance(back.get("boards", {}), dict)
        except Exception as e:
            sys.exit(f"сгенерированный yml не прошёл проверку ({type(e).__name__}: {e}) — companies.yml НЕ тронут")
        os.makedirs(DATA, exist_ok=True)
        open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        try:
            open(YML, "w", encoding="utf-8").write(out)
            print(f"\nзаписано: {YML} ({want} записей), {REPORT}", file=sys.stderr)
        except OSError as e:                        # например, в контейнере companies.yml не смонтирован: 10+ минут перебора не теряем
            alt = os.path.join(DATA, "companies.generated.yml")
            open(alt, "w", encoding="utf-8").write(out)
            sys.exit(f"companies.yml недоступен для записи ({e}); результат ({want} записей) сохранён в {alt}, отчёт — {REPORT}")

if __name__ == "__main__":
    main()
