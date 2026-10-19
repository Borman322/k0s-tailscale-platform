"""Источники вакансий: HTTP-слой и адаптеры систем найма (ATS).

Адаптер — генератор fn(h, slug, want) → dict(external_id, title, url, location, body, posted_at).
want(title) — стоит ли тащить полное описание: у части ATS список приходит без текста,
а за каждым описанием нужен отдельный запрос, поэтому грузим только то, что прошло фильтр по заголовку.
Нет такой компании в этой ATS → NoBoard. Борда есть, но пустая → генератор просто ничего не отдаст.
"""
import re, time, html, threading, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import requests

UA  = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
TMO = (6, 25)   # connect, read


class NoBoard(Exception):
    """Такой компании в этой системе найма нет."""


# ── утилиты ───────────────────────────────────────────────────────────────
def text(s):
    """HTML (в т.ч. дважды экранированный, как у Greenhouse) → чистый текст."""
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def iso(v):
    """Эпоха (с/мс), ISO или RFC-2822 → ISO-строка либо None. Битая дата не должна ронять пачку INSERT."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            v = float(v)
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc).isoformat()
        s = re.sub(r"\s*UTC$", "+00:00", str(v).strip())     # Recruitee: «2026-10-05 14:49:28 UTC»
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return parsedate_to_datetime(s).isoformat()
    except Exception:
        return None

def loc_join(*parts, mode=""):
    """Локации без дублей + пометка формата работы, чтобы remote/hybrid ловились теми же регулярками."""
    s = " / ".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
    if not s or not mode or mode.lower() in s.lower():
        return s or mode
    return f"{s} ({mode})"

SUFFIX = {"labs", "lab", "foundation", "finance", "protocol", "network", "technologies", "technology", "digital",
          "group", "capital", "inc", "dao", "hq", "ltd", "limited", "org", "io", "xyz", "co", "com", "corp",
          "systems", "association", "company", "holdings", "global", "gmbh", "ag"}

def words(name):
    return re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))

def key(name):
    """Ключ сравнения компаний: без регистра, знаков и суффиксов вроде Labs/Foundation."""
    w = [x for x in words(name) if x not in SUFFIX]
    return "".join(w or words(name))

def kids(el):
    """Дочерние XML-элементы по имени без namespace."""
    return {k.tag.rsplit("}", 1)[-1]: k for k in el}


# ссылка на вакансию → (ATS, slug): так ловим компании прямо из объявлений на досках
_HOSTS = {"boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse", "boards.eu.greenhouse.io": "greenhouse",
          "job-boards.eu.greenhouse.io": "greenhouse", "jobs.lever.co": "lever", "jobs.eu.lever.co": "lever",
          "jobs.ashbyhq.com": "ashby", "apply.workable.com": "workable", "jobs.smartrecruiters.com": "smartrecruiters",
          "careers.smartrecruiters.com": "smartrecruiters", "ats.rippling.com": "rippling", "jobs.gem.com": "gem"}
_SUBDOMAIN = {"recruitee.com": "recruitee", "jobs.personio.de": "personio", "jobs.personio.com": "personio",
              "teamtailor.com": "teamtailor", "bamboohr.com": "bamboohr", "breezy.hr": "breezy"}

def ats_from_url(url):
    try:
        u = urlsplit(url or "")
    except ValueError:
        return None
    host, parts = (u.hostname or "").lower(), [x for x in u.path.split("/") if x]
    if host in _HOSTS:
        first = parts[0] if parts else ""
        return (_HOSTS[host], first) if first and first not in ("embed", "api", "v1", "j", "o") else None
    for suffix, name in _SUBDOMAIN.items():
        sub = host[:-len(suffix) - 1] if host.endswith("." + suffix) else ""
        if sub and sub not in ("www", "app", "api", "careers", "jobs"):
            return (name, sub)
    return None


# ── HTTP ──────────────────────────────────────────────────────────────────
class Http:
    """requests с вежливостью: пауза между запросами к одной системе найма, ретраи на 429/5xx.
    Сессия на поток — requests.Session между потоками не шарим."""
    GAP = {"workable.com": 1.0, "personio.de": .5, "personio.com": .5, "teamtailor.com": .3,
           "bamboohr.com": .3, "smartrecruiters.com": .15,
           # доски: на них идёт 1–8 запросов за прогон, лимиты у части публикуются и строгие
           "himalayas.app": 1.5, "remoteok.com": 1.5, "jobicy.com": 1.5, "arbeitnow.com": 1.0}

    def __init__(self):
        self.tl, self.lock, self.next = threading.local(), threading.Lock(), {}

    def _session(self):
        if not hasattr(self.tl, "s"):
            self.tl.s = requests.Session()
            self.tl.s.headers.update(UA)
        return self.tl.s

    def _wait(self, url):
        dom = ".".join((urlsplit(url).hostname or "").split(".")[-2:])   # recruitee.com, а не acme.recruitee.com
        with self.lock:
            t = max(self.next.get(dom, 0), time.monotonic())
            self.next[dom] = t + self.GAP.get(dom, .08)
        if t > time.monotonic():
            time.sleep(t - time.monotonic())

    def request(self, method, url, **kw):
        kw.setdefault("timeout", TMO)
        for i in range(3):
            self._wait(url)
            try:
                r = self._session().request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if i == 2:
                    raise
                time.sleep(1.5 * (i + 1))
                continue
            if r.status_code in (429, 502, 503, 504) and i < 2:
                try:    wait = float(r.headers.get("Retry-After"))
                except (TypeError, ValueError): wait = 3 * (i + 1)
                time.sleep(min(wait, 20))
                continue
            return r

    def get(self, url, **kw):  return self.request("GET", url, **kw)
    def post(self, url, **kw): return self.request("POST", url, **kw)


def _json(h, url, **kw):
    """JSON или NoBoard: 404 и «200 с HTML-заглушкой» (так отвечают BambooHR/Personio на чужой slug)."""
    r = h.get(url, **kw)
    if r.status_code in (404, 410):
        raise NoBoard(url)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise NoBoard(f"{url}: ответ не JSON")


# ── ATS ───────────────────────────────────────────────────────────────────
def greenhouse(h, slug, want=None):
    for j in _json(h, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                   params={"content": "true"}).get("jobs", []):
        yield dict(external_id=str(j["id"]), title=j["title"], url=j.get("absolute_url", ""),
                   location=(j.get("location") or {}).get("name", ""),
                   body=text(j.get("content")), posted_at=iso(j.get("updated_at")))

def lever(h, slug, want=None):
    data, found = [], False
    for host in ("api.lever.co", "api.eu.lever.co"):      # компании из EU-региона живут на втором хосте
        try:
            d = _json(h, f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        except NoBoard:
            continue
        found = True
        if d:                                            # на глобальном хосте может быть пустая борда-призрак
            data = d
            break
    if not found:
        raise NoBoard(slug)
    for j in data:
        cat = j.get("categories") or {}
        mode = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site"}.get(j.get("workplaceType"), "")
        # требования у Lever лежат в lists, а не в description — без них терялась половина стека
        body = " ".join([j.get("descriptionPlain") or text(j.get("description"))]
                        + [f'{l.get("text", "")} {text(l.get("content"))}' for l in j.get("lists") or []]
                        + [j.get("additionalPlain") or text(j.get("additional"))])
        yield dict(external_id=j["id"], title=j.get("text", ""), url=j.get("hostedUrl", ""),
                   location=loc_join(cat.get("location"), *(cat.get("allLocations") or []), mode=mode),
                   body=body, posted_at=iso(j.get("createdAt")))

def ashby(h, slug, want=None):
    for j in _json(h, f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
        if j.get("isListed") is False:
            continue
        mode = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        yield dict(external_id=j["id"], title=j.get("title", ""), url=j.get("jobUrl", ""),
                   location=loc_join(j.get("location"), *[s.get("location") for s in j.get("secondaryLocations") or []],
                                     mode=mode),
                   body=j.get("descriptionPlain") or text(j.get("descriptionHtml")),
                   posted_at=iso(j.get("publishedAt")))

def workable(h, slug, want=None):
    d = _json(h, f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    for j in d.get("jobs", []):
        yield dict(external_id=j.get("shortcode") or j.get("code"), title=j.get("title", ""), company=d.get("name"),
                   url=j.get("url") or j.get("shortlink", ""),
                   location=loc_join(", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])),
                                     mode="Remote" if j.get("telecommuting") else ""),
                   body=text(" ".join(filter(None, [j.get("description"), j.get("requirements"), j.get("benefits")]))),
                   posted_at=iso(j.get("published_on") or j.get("created_at")))

def smartrecruiters(h, slug, want=None):
    off, total = 0, 1
    while off < total:
        d = _json(h, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                  params={"limit": 100, "offset": off})
        total, page = d.get("totalFound", 0), d.get("content", [])
        if not total and not off:
            raise NoBoard(slug)        # на чужой slug отвечает 200 и totalFound=0 — пустую борду от несуществующей не отличить
        for j in page:
            l = j.get("location") or {}
            body = ""
            if want and want(j.get("name", "")):      # список без описаний — тянем только нужные
                try:
                    secs = (_json(h, j["ref"]).get("jobAd") or {}).get("sections") or {}
                    body = text(" ".join(s.get("text", "") for s in secs.values()))
                except Exception:
                    pass
            yield dict(external_id=j["id"], title=j.get("name", ""), company=(j.get("company") or {}).get("name"),
                       url=f'https://jobs.smartrecruiters.com/{(j.get("company") or {}).get("identifier", slug)}/{j["id"]}',
                       location=loc_join(", ".join(filter(None, [l.get("city"), l.get("region"), (l.get("country") or "").upper()])),
                                         mode="Remote" if l.get("remote") else "Hybrid" if l.get("hybrid") else ""),
                       body=body, posted_at=iso(j.get("releasedDate")))
        if not page:
            break
        off += len(page)

def recruitee(h, slug, want=None):
    for j in _json(h, f"https://{slug}.recruitee.com/api/offers/").get("offers", []):
        yield dict(external_id=str(j["id"]), title=j.get("title", ""), url=j.get("careers_url", ""),
                   company=j.get("company_name"),
                   location=loc_join(", ".join(filter(None, [j.get("city"), j.get("country")])) or j.get("location"),   # location у них бывает просто «Remote job»
                                     mode="Remote" if j.get("remote") else "Hybrid" if j.get("hybrid") else ""),
                   body=text(f'{j.get("description") or ""} {j.get("requirements") or ""}'),
                   posted_at=iso(j.get("published_at") or j.get("created_at")))

def personio(h, slug, want=None):
    for dom in ("personio.de", "personio.com"):
        r = h.get(f"https://{slug}.jobs.{dom}/xml", params={"language": "en"}, stream=True, allow_redirects=False)
        loc = r.headers.get("location", "")
        if r.status_code in (301, 302, 307, 308) and ".jobs.personio." in loc:      # тенант живёт на соседнем домене
            r.close()
            r = h.get(loc, stream=True)
        if r.status_code == 200 and "xml" in r.headers.get("content-type", ""):
            break
        r.close()       # чужой slug: 307 на главную personio.com (1,6 МБ) либо 200 с HTML — тело не качаем
    else:
        raise NoBoard(slug)
    for p in ET.fromstring(r.content).iter("position"):
        g = lambda t: (p.findtext(t) or "").strip()
        body = text(" ".join(f'{d.findtext("name") or ""} {d.findtext("value") or ""}'
                             for d in p.findall("jobDescriptions/jobDescription")))
        yield dict(external_id=g("id"), title=g("name"), url=f"https://{slug}.jobs.{dom}/job/{g('id')}", company=g("subcompany"),
                   location=loc_join(g("office"), *[(o.text or "") for o in p.findall("additionalOffices/office")]),
                   body=body, posted_at=iso(g("createdAt")))

def teamtailor(h, slug, want=None):
    r = h.get(f"https://{slug}.teamtailor.com/jobs.rss")
    if r.status_code != 200 or b"<rss" not in r.content[:500]:
        raise NoBoard(slug)
    root = ET.fromstring(r.content)
    for it in root.iter("item"):
        k = kids(it)
        t = lambda n: (k[n].text or "").strip() if n in k else ""
        locs = []
        for l in (k["locations"] if "locations" in k else []):
            lk = kids(l)
            locs.append(loc_join(*[(lk[n].text if n in lk else "") for n in ("name", "city", "country")]))
        yield dict(external_id=t("guid"), title=t("title"), url=t("link"), company=root.findtext("channel/title"),
                   location=loc_join(*locs, mode={"full": "Remote", "fully": "Remote", "hybrid": "Hybrid"}.get(t("remoteStatus").lower(), "")),
                   body=text(t("description")), posted_at=iso(t("pubDate")))

def bamboohr(h, slug, want=None):
    r = h.get(f"https://{slug}.bamboohr.com/careers/list", headers={"Accept": "application/json"})
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise NoBoard(slug)
    for j in r.json().get("result", []):
        l, a = j.get("location") or {}, j.get("atsLocation") or {}
        body = ""
        if want and want(j.get("jobOpeningName", "")):
            try:
                body = text(_json(h, f"https://{slug}.bamboohr.com/careers/{j['id']}/detail")["result"]["jobOpening"]["description"])
            except Exception:
                pass
        yield dict(external_id=str(j["id"]), title=j.get("jobOpeningName", ""),
                   url=f"https://{slug}.bamboohr.com/careers/{j['id']}",
                   location=loc_join(", ".join(filter(None, [l.get("city") or a.get("city"), l.get("state") or a.get("state"), a.get("country")])),
                                     mode="Remote" if j.get("isRemote") or str(j.get("locationType")) == "1"
                                     else "Hybrid" if str(j.get("locationType")) == "2" else ""),
                   body=body, posted_at=None)

def rippling(h, slug, want=None):
    jobs = {}
    for j in _json(h, f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"):
        jobs.setdefault(j["uuid"], (j, []))[1].append((j.get("workLocation") or {}).get("label", ""))   # uuid повторяется на каждую локацию
    for uid, (j, locs) in jobs.items():
        body, posted, comp = "", None, None
        if want and want(j.get("name", "")):
            try:
                d = _json(h, f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uid}")
                body, posted, comp = text(" ".join((d.get("description") or {}).values())), iso(d.get("createdOn")), d.get("companyName")
            except Exception:
                pass
        yield dict(external_id=uid, title=j.get("name", ""), url=j.get("url", ""), company=comp,
                   location=loc_join(*locs), body=body, posted_at=posted)

def gem(h, slug, want=None):
    for j in _json(h, f"https://api.gem.com/job_board/v0/{slug}/job_posts/"):
        mode = {"remote": "Remote", "hybrid": "Hybrid", "in_office": "On-site"}.get(j.get("location_type"), "")
        yield dict(external_id=str(j["id"]), title=j.get("title", ""), url=j.get("absolute_url", ""),
                   location=loc_join((j.get("location") or {}).get("name"), mode=mode),
                   body=j.get("content_plain") or text(j.get("content")),
                   posted_at=iso(j.get("first_published_at") or j.get("created_at")))

def breezy(h, slug, want=None):
    for j in _json(h, f"https://{slug}.breezy.hr/json"):     # описаний в списке нет — оценка по заголовку и отделу
        l = j.get("location") or {}
        yield dict(external_id=j["id"], title=j.get("name", ""), url=j.get("url", ""),
                   company=(j.get("company") or {}).get("name"),
                   location=loc_join(l.get("name"), mode="Remote" if l.get("is_remote") else ""),
                   body=j.get("department") or "", posted_at=iso(j.get("published_date")))


# порядок = порядок опроса при определении системы по slug (сначала самые частые)
ATS = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever, "workable": workable,
       "personio": personio, "smartrecruiters": smartrecruiters, "recruitee": recruitee,
       "teamtailor": teamtailor, "gem": gem, "rippling": rippling, "bamboohr": bamboohr, "breezy": breezy}
