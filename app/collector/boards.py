"""Доски-агрегаторы: широкий охват и поиск компаний, которых ещё нет в companies.yml.

Адаптер: fn(h, cfg) → dict(external_id, title, company, url, location, body, posted_at, tags).
cfg — строка раздела boards из companies.yml (tags, pages, months…).
Условия площадок соблюдаем: url ведёт на оригинал, запросов за прогон минимум (1–3 на доску).
В BOARDS второй элемент — нужен ли фильтр по домену из profile.yml: на общих досках большинство вакансий не из вашей области.
"""
import re, html
import xml.etree.ElementTree as ET
from sources import text, iso, loc_join, kids


# ── общие доски удалёнки ──────────────────────────────────────────────────
def remoteok(h, cfg):
    """Лента 100 последних вакансий; тег фильтрует на стороне сервера. В условиях — ссылка на их страницу вакансии: url её и хранит."""
    seen = set()
    for tag in cfg.get("tags", ["devops", "sysadmin"]):
        d = h.get("https://remoteok.com/api", params={"tag": tag}).json()
        for j in d[1:]:                                         # d[0] — юридическое уведомление
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            yield dict(external_id=str(j["id"]), title=text(j.get("position")), company=j.get("company"),
                       url=j.get("url") or j.get("apply_url", ""), location=loc_join(j.get("location"), mode="Remote"),
                       body=text(j.get("description")), posted_at=iso(j.get("date")), tags=j.get("tags") or [])

def jobicy(h, cfg):
    """Условия Jobicy: указывать источник и вести на их URL вакансии — url так и хранится; новый проход не чаще раза в час.
    industry=admin — их «DevOps & Infrastructure»; окно ленты — 7 дней."""
    seen = set()
    for q in cfg.get("queries", [{"industry": "admin"}, {"industry": "engineering", "tag": "devops"}]):
        d = h.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 100, **q}).json()
        for j in d.get("jobs") or []:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            yield dict(external_id=str(j["id"]), title=text(j.get("jobTitle")), company=text(j.get("companyName")),
                       url=j.get("url", ""), location=loc_join(j.get("jobGeo"), mode="Remote"),
                       body=text(j.get("jobDescription") or j.get("jobExcerpt")), posted_at=iso(j.get("pubDate")),
                       tags=[*(q.get("tag") and [q["tag"]] or []), *(j.get("jobIndustry") or [])])

def arbeitnow(h, cfg):
    """Немецкая доска, англоязычные вакансии."""
    seen = set()
    for page in range(1, cfg.get("pages", 3) + 1):
        d = h.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page}).json()
        for j in d.get("data") or []:
            if j["slug"] in seen:
                continue
            seen.add(j["slug"])
            yield dict(external_id=j["slug"], title=text(j.get("title")), company=j.get("company_name"),
                       url=j.get("url", ""), location=loc_join(j.get("location"), mode="Remote" if j.get("remote") else ""),
                       body=text(j.get("description")), posted_at=iso(j.get("created_at")), tags=j.get("tags") or [])
        if not (d.get("links") or {}).get("next"):
            break

def weworkremotely(h, cfg):
    """RSS по категориям. Заголовок «Компания: Должность»."""
    for feed in cfg.get("feeds", ["remote-devops-sysadmin-jobs"]):
        r = h.get(f"https://weworkremotely.com/categories/{feed}.rss")
        if r.status_code != 200:
            continue
        for it in ET.fromstring(r.content).iter("item"):
            k = kids(it)
            t = lambda n: (k[n].text or "").strip() if n in k else ""
            company, _, title = t("title").partition(": ")
            yield dict(external_id=t("guid") or t("link"), title=text(title or company), company=text(company) if title else "",
                       url=t("link"), location=loc_join(t("region"), t("country"), mode="Remote"),
                       body=text(t("description")), posted_at=iso(t("pubDate")), tags=[t("category"), t("skills")])


def himalayas(h, cfg):
    """Himalayas: поиск по ключевым словам на стороне сервера (лента целиком слишком большая, чтобы её обходить).
    locationRestrictions — страны, где может жить кандидат: пустой список = весь мир.
    По условиям API — ссылка на himalayas.app: applicationLink так и хранится."""
    seen = set()
    for q in cfg.get("queries", ["devops engineer", "site reliability engineer", "platform engineer"]):
        for page in range(1, cfg.get("pages", 2) + 1):
            d = h.get("https://himalayas.app/jobs/api/search", params={"q": q, "page": page}).json()
            for j in d.get("jobs") or []:
                if j["guid"] in seen:
                    continue
                seen.add(j["guid"])
                restr = j.get("locationRestrictions") or []
                loc = ", ".join(restr[:5]) + ("…" if len(restr) > 5 else "") if restr else "Worldwide"
                yield dict(external_id=j["guid"], title=text(j.get("title")), company=j.get("companyName"),
                           url=j.get("applicationLink") or j["guid"], location=loc_join(loc, mode="Remote"),
                           body=text(j.get("description") or j.get("excerpt")),
                           posted_at=iso(j.get("pubDate")), tags=[*(j.get("categories") or []), q])
            if len(d.get("jobs") or []) < 20:
                break


# ── Hacker News: «Ask HN: Who is hiring?» ─────────────────────────────────
def hn_hiring(h, cfg):
    """Ветки «Who is hiring?» за последние месяцы: каждый комментарий верхнего уровня — вакансия.
    Принятый формат первой строки: «Компания | Должность | Локация | REMOTE | …»."""
    hits = h.get("https://hn.algolia.com/api/v1/search_by_date",
                 params={"tags": "story,author_whoishiring", "hitsPerPage": 12}).json()["hits"]
    threads = [t for t in hits if "who is hiring" in (t.get("title") or "").lower()][:cfg.get("months", 2)]
    for t in threads:
        tid, page = t["objectID"], 0
        while True:
            d = h.get("https://hn.algolia.com/api/v1/search",
                      params={"tags": f"comment,story_{tid}", "hitsPerPage": 1000, "page": page}).json()
            for c in d["hits"]:
                if str(c.get("parent_id")) != str(tid) or not c.get("comment_text"):
                    continue                                    # ответы на вакансии нам не нужны
                head = text(re.split(r"<p>", c["comment_text"], maxsplit=1)[0])
                parts = [p.strip() for p in head.split("|")]
                title = parts[1] if len(parts) > 2 else head[:140]
                yield dict(external_id=c["objectID"], title=title[:200], company=re.sub(r"\s*(?:\(|,| [-—–] ).*", "", parts[0])[:60] if len(parts) > 2 else "",
                           url=f"https://news.ycombinator.com/item?id={c['objectID']}",
                           location=loc_join(*parts[2:5]) if len(parts) > 2 else head[:80],
                           body=text(c["comment_text"]), posted_at=iso(c.get("created_at")), tags=[],
                           links=re.findall(r'href="([^"]+)"', html.unescape(c["comment_text"])))
            page += 1
            if page >= d.get("nbPages", 1):
                break


BOARDS = {                       # имя в companies.yml → (адаптер, нужен ли фильтр по домену из profile.yml)
    "remoteok": (remoteok, True),
    "jobicy": (jobicy, True),
    "arbeitnow": (arbeitnow, True),
    "weworkremotely": (weworkremotely, True),
    "hn_hiring": (hn_hiring, True),
    "himalayas": (himalayas, True),
}
