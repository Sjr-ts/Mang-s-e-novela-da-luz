# -*- coding: utf-8 -*-
"""Adaptadores multi-site do Baixador de Manga.

Contrato de cada adaptador:
    key, label, folder, official, langs, lang_labels, lang
    search(query, lang) -> [{title, url, cover}]
    get_info(url)       -> {title, cover, description, authors, genres, volumes, chapters}
    list_chapters(url, lang) -> [{label, url, number}]
    list_volumes(url, lang) -> [{label, url}]
    item_pages(url)     -> int
"""
import base64
import html as htmllib
import io
import json
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_CA = os.path.join(os.environ.get("TEMP", "."), "bm-ca-bundle.pem")
if os.path.exists(_CA):
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA)
    os.environ.setdefault("CURL_CA_BUNDLE", _CA)

import requests
import mangafire as mf

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_session = None


def http():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
    return _session


def _get(url, timeout=25, **kw):
    return http().get(url, timeout=timeout, **kw)


def _strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def _abs(base, href):
    if not href:
        return None
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    m = re.match(r"(https?://[^/]+)", base)
    return (m.group(1) if m else "") + href


def _first(pattern, text, default=None, flags=re.S):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else default


def _norm_num(s):
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None


class BaseAdapter:
    key = ""
    label = ""
    folder = ""
    official = False
    langs = ["en"]
    lang_labels = {"en": "Inglês"}
    lang = "en"
    available = True
    cover_hosts = []

    def search(self, query, lang=None):
        return []

    def get_info(self, url):
        raise NotImplementedError

    def list_chapters(self, url, lang=None):
        raise NotImplementedError

    def list_volumes(self, url, lang=None):
        return []

    def item_pages(self, url):
        raise NotImplementedError

    def reader_images(self, url):
        return []

    def custom_download(self):
        return False


# ============================================================
# MangaFire (adaptador sobre o módulo existente)
# ============================================================
class MangaFireAdapter(BaseAdapter):
    key = "mangafire"
    label = "MangaFire"
    folder = "mangafire"
    official = True
    langs = ["pt-br", "en", "es", "es-la", "fr", "it", "tr", "id", "ru"]
    lang_labels = {"pt-br": "Português (BR)", "en": "Inglês", "es": "Espanhol",
                   "es-la": "Espanhol (LA)", "fr": "Francês", "it": "Italiano",
                   "tr": "Turco", "id": "Indonésio", "ru": "Russo"}
    lang = "pt-br"
    cover_hosts = ["mangafire.to", "mfcdn.nl", "mfcdn1.xyz", "mfcdn2.xyz"]

    def normalize_url(self, url):
        return mf.normalize_url(url)

    def search(self, query, lang="pt-br"):
        return []

    def get_info(self, url):
        base = mf.normalize_url(url)
        manga = mf.get_manga(base)
        langs = list(manga.get("languages") or [])
        return {
            "title": manga.get("title", "Sem título"),
            "cover": manga.get("cover", ""),
            "description": manga.get("description", ""),
            "authors": manga.get("authors", []) + manga.get("artists", []),
            "genres": manga.get("tags", []),
            "volumes": [],
            "chapters": [],
            "url": base,
            "languages": langs,
            "hasVolumes": bool(manga.get("hasVolumes")),
            "official": True,
        }

    def list_chapters(self, url, lang="pt-br"):
        base = mf.normalize_url(url)
        chapters = mf.list_chapters(base, lang)
        out = []
        for c in chapters:
            num = c.get("number")
            name = c.get("name") or ""
            ch_url = mf.item_url(base, c)
            out.append({
                "id": c.get("id"),
                "label": ("Capítulo %s" % num) + (" — " + name if name else ""),
                "url": ch_url,
                "number": num,
                "official": bool(c.get("official", True)),
            })
        return out

    def list_volumes(self, url, lang="pt-br"):
        base = mf.normalize_url(url)
        if not mf.get_manga(base).get("hasVolumes"):
            return []
        volumes = mf.list_volumes(base, lang)
        out = []
        for v in volumes:
            out.append({
                "id": v.get("id"),
                "label": ("Volume %s" % v.get("number")) + (" — " + v.get("name", "") if v.get("name") else ""),
                "url": mf.item_url(base, v),
                "number": v.get("number"),
            })
        return out

    def item_pages(self, url):
        return 0  # mangafire conta via item_url+id no jobs.py

    def item_pages_with_item(self, url, item):
        return mf.item_pages(url, item)


# ============================================================
# MangaDex
# ============================================================
class MangaDexAdapter(BaseAdapter):
    key = "mangadex"
    label = "MangaDex"
    folder = "mangadex"
    official = False
    langs = ["pt-br", "en", "es-la", "id", "ru"]
    lang_labels = {"pt-br": "Português (BR)", "en": "Inglês", "es-la": "Espanhol (LA)",
                   "id": "Indonésio", "ru": "Russo"}
    lang = "en"
    cover_hosts = ["uploads.mangadex.org"]

    BASE = "https://api.mangadex.org"

    def _api(self):
        s = http()
        s.headers.update({"User-Agent": "gallery-dl/1.30.0", "Accept": "application/json"})
        return s

    def search(self, query, lang="en"):
        s = self._api()
        try:
            r = s.get(self.BASE + "/manga",
                      params={"title": query, "limit": 12,
                              "includes[]": ["cover_art", "author"],
                              "contentRating[]": ["safe", "suggestive"],
                              "order[relevance]": "desc"}, timeout=25)
            r.raise_for_status()
        finally:
            s.headers.update({"User-Agent": UA,
                              "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        out = []
        for m in r.json().get("data", []):
            a = m.get("attributes", {})
            title = a.get("title", {}).get("en") or next(iter(a.get("title", {}).values()), m["id"])
            cover = None
            for rel in m.get("relationships", []):
                if rel.get("type") == "cover_art" and rel.get("attributes"):
                    cover = "https://uploads.mangadex.org/data/%s/%s" % (m["id"], rel["attributes"]["fileName"])
            out.append({"title": title, "cover": cover,
                        "url": "https://mangadex.org/title/%s" % m["id"]})
        return out

    def get_info(self, url):
        mid = url.rstrip("/").split("/")[-1].split("?")[0]
        s = self._api()
        try:
            r = s.get(self.BASE + "/manga/" + mid, params={"includes[]": ["cover_art", "author", "artist"]},
                      timeout=25)
            r.raise_for_status()
            m = r.json()["data"]
            a = m["attributes"]
            cover = None
            authors = []
            for rel in m.get("relationships", []):
                if rel.get("type") == "cover_art" and rel.get("attributes"):
                    cover = "https://uploads.mangadex.org/data/%s/%s" % (mid, rel["attributes"]["fileName"])
                if rel.get("type") in ("author", "artist") and rel.get("attributes"):
                    authors.append(rel["attributes"].get("name", ""))
            title = a.get("title", {}).get("en") or next(iter(a.get("title", {}).values()), mid)
            desc = _strip_tags(a.get("description", {}).get("en")
                               or next(iter(a.get("description", {}).values()), ""))
            tags = [t["attributes"]["name"]["en"] for t in a.get("tags", [])
                    if t.get("attributes")]
            avail = a.get("availableTranslatedLanguages") or []
        finally:
            s.headers.update({"User-Agent": UA,
                              "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        return {
            "title": title, "cover": cover, "description": desc[:600],
            "authors": [x for x in authors if x][:4], "genres": tags[:8],
            "volumes": [], "chapters": [],
            "url": "https://mangadex.org/title/%s" % mid,
            "languages": avail[:12] or ["en"],
            "hasVolumes": False, "official": False,
        }

    def feed_chapters(self, url, lang="en"):
        mid = url.rstrip("/").split("/")[-1].split("?")[0]
        s = self._api()
        try:
            out, offset = [], 0
            while True:
                r = s.get(self.BASE + "/manga/%s/feed" % mid,
                          params={"limit": 500, "offset": offset, "translatedLanguage[]": [lang],
                                  "order[chapter]": "asc", "includes[]": ["scanlation_group"]},
                          timeout=30)
                if not r.ok:
                    break
                data = r.json().get("data", [])
                for ch in data:
                    ca = ch.get("attributes", {})
                    num = ca.get("chapter") or "?"
                    title = ca.get("title") or ""
                    label = f"Capítulo {num}" + (f" — {title}" if title else "")
                    out.append({"label": label,
                                "url": "https://mangadex.org/chapter/" + ch["id"],
                                "number": _norm_num(num)})
                offset += len(data)
                if not r.json().get("hasMore") or not data:
                    break
            return out
        finally:
            s.headers.update({"User-Agent": UA,
                              "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})

    def list_chapters(self, url, lang="en"):
        return self.feed_chapters(url, lang)

    def item_pages(self, url):
        cid = url.rstrip("/").split("/")[-1].split("?")[0]
        s = self._api()
        try:
            r = s.get(self.BASE + "/at-home/server/" + cid, timeout=25)
            r.raise_for_status()
            j = r.json()
            return len(j["chapter"]["pages"])
        finally:
            s.headers.update({"User-Agent": UA,
                              "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})


# ============================================================
# WeebCentral
# ============================================================
class WeebCentralAdapter(BaseAdapter):
    key = "weebcentral"
    label = "Weeb Central"
    folder = "weebcentral"
    official = True
    langs = ["en"]
    lang_labels = {"en": "Inglês"}
    lang = "en"
    cover_hosts = ["temp.compsci88.com", "official.lowee.us"]

    BASE = "https://weebcentral.com"

    def search(self, query, lang="en"):
        r = _get(self.BASE + "/search/data", params={"text": query},
                 headers={"HX-Request": "true", "HX-Target": "series-list"})
        out = []
        if r.ok:
            for sid in dict.fromkeys(re.findall(r"/series/(\w+)", r.text)):
                m = re.search(r'/series/%s".{0,600}?alt="([^"]+)"' % sid, r.text, re.S)
                if not m:
                    m = re.search(r'/series/%s".{0,600}?title="([^"]+)"' % sid, r.text, re.S)
                out.append({"title": _strip_tags(m.group(1)) if m else sid,
                            "url": "%s/series/%s" % (self.BASE, sid), "cover": None})
                if len(out) >= 12:
                    break
        return out

    def get_info(self, url):
        referer = url.rstrip("/")
        r = _get(referer)
        r.raise_for_status()
        t = r.text
        title = _first(r"<title>(.*?) \| Weeb Central", t) or _first(r"<title>(.*?)</title>", t)
        cover = _first(r'property="og:image"\s+content="([^"]+)"', t)
        desc = _first(r"<strong>Description:?</strong>(.*?)</(?:p|div)>", t, "")
        official = ">Yes" in (t or "")
        return {
            "title": _strip_tags(title).replace("| Weeb Central", "").strip(),
            "cover": cover, "description": _strip_tags(desc)[:600],
            "authors": [], "genres": [], "volumes": [], "chapters": [],
            "url": referer, "languages": ["en"], "hasVolumes": False,
            "official": official,
        }

    def list_chapters(self, url, lang="en"):
        referer = url.rstrip("/")
        cl = _get(referer + "/full-chapter-list",
                  headers={"HX-Request": "true", "HX-Target": "chapter-list",
                           "HX-Current-URL": referer, "Referer": referer}).text
        out = []
        for m in re.finditer(r'href="(/chapters/(\w+))"(.*?)</a>', cl, re.S):
            inner = m.group(3)
            num_m = re.search(r"(?:Chapter|Episode|Ch\.?)\s*([\d.]+)", inner, re.I)
            if not num_m:
                num_m = re.search(r">\s*([\d.]+)\s*<", inner)
            num = _norm_num(num_m.group(1)) if num_m else None
            span_m = re.search(r"<span[^>]*>([^<]+)</span>", inner)
            lbl = _strip_tags(span_m.group(1)) if span_m else None
            if not lbl:
                lbl = "Capítulo %s" % (num if num is not None else m.group(2))
            out.append({"label": lbl, "url": self.BASE + m.group(1),
                        "number": num})
        return out

    def item_pages(self, url):
        referer = url.rstrip("/").split("/chapters/")[0]
        r = _get(url + "/images",
                 params={"is_prev": "False", "current_page": "1", "reading_style": "long_strip"},
                 headers={"HX-Request": "true", "HX-Current-URL": url, "Referer": referer})
        r.raise_for_status()
        srcs = re.findall(r'<img src="([^"]+)"', r.text)
        seen, out = set(), []
        for s in srcs:
            if s.startswith("http") and s not in seen:
                seen.add(s)
                out.append(s)
        return len(out)


# ============================================================
# Hiperdex
# ============================================================
class HiperdexAdapter(BaseAdapter):
    key = "hiperdex"
    label = "Hiperdex"
    folder = "hiperdex"
    official = False
    langs = ["en"]
    lang_labels = {"en": "Inglês"}
    lang = "en"
    cover_hosts = ["hiperdex.com", "hiper.to"]

    BASE = "https://hiperdex.com"
    AUTH = {"x-cfg-auth": "yceqt7qgu004"}

    def _warmup(self):
        try:
            if "__st" not in (getattr(http(), "cookies", {}) or {}):
                _get(self.BASE + "/", timeout=15)
        except Exception:
            pass

    def _trpc(self, endpoint, params):
        self._warmup()
        r = _get(self.BASE + "/api/trpc/" + endpoint,
                 params={"input": json.dumps({"json": params})},
                 headers=self.AUTH, timeout=25)
        r.raise_for_status()
        return r.json()["result"]["data"]["json"]

    def search(self, query, lang="en"):
        q = query.lower()
        try:
            r = _get(self.BASE + "/sitemap.xml", timeout=25)
            subs = re.findall(r"<loc>([^<]+sitemap-series[^<]*)</loc>", r.text)
        except Exception:
            subs = []
        urls = []
        for sm in subs:
            try:
                r2 = _get(sm, timeout=40)
                urls += re.findall(r"<loc>(https://hiperdex\.com/manga/[^<]+)</loc>", r2.text)
            except Exception:
                continue
            if len(urls) > 5000:
                break
        out = []
        for u in urls:
            slug = u.rstrip("/").split("/manga/")[1]
            if q in slug.replace("-", " "):
                out.append({"title": slug.replace("-", " ").title(), "url": u, "cover": None})
            if len(out) >= 12:
                break
        return out

    def get_info(self, url):
        slug = url.rstrip("/").split("/manga/")[1].split("?")[0]
        md = self._trpc("series.bySlugWithGenres", {"slug": slug})
        chs = self._trpc("series.chapters", {"seriesId": md["id"]})
        chapters = []
        for c in chs:
            num = c.get("number")
            lbl = "Capítulo %s" % num
            if c.get("title"):
                lbl += " — " + c["title"]
            chapters.append({"label": lbl,
                             "url": "%s/manga/%s/%s/" % (self.BASE, slug, num),
                             "number": _norm_num(num)})
        return {
            "title": md.get("title") or slug,
            "cover": md.get("coverUrl"),
            "description": _strip_tags(md.get("summary") or md.get("description") or "")[:600],
            "authors": md.get("authors") or [], "genres": md.get("genres") or [],
            "volumes": [], "chapters": chapters,
            "url": url.split("?")[0].rstrip("/") + "/",
            "languages": ["en"], "hasVolumes": False, "official": False,
        }

    def list_chapters(self, url, lang="en"):
        info = self.get_info(url)
        return info["chapters"]

    def item_pages(self, url):
        m = re.match(r"https://hiperdex\.com/manga/([^/]+)/(\d+)", url)
        if not m:
            raise ValueError("URL de capítulo Hiperdex inválida: " + url)
        pages = self._trpc("reader.chapterPages",
                           {"seriesSlug": m.group(1), "chapterNumber": float(m.group(2))})
        return len(pages)


# ============================================================
# LINE Webtoon (webtoons.com)
# ============================================================
class WebtoonsAdapter(BaseAdapter):
    key = "webtoons"
    label = "LINE Webtoon"
    folder = "webtoons"
    official = True
    langs = ["en", "es", "fr", "de", "th", "id", "pt-br"]
    lang_labels = {"en": "Inglês", "es": "Espanhol", "fr": "Francês", "de": "Alemão",
                   "th": "Tailandês", "id": "Indonésio", "pt-br": "Português (BR)"}
    lang = "en"
    cover_hosts = ["webtoon-phinf.pstatic.net", "swebtoon-phinf.pstatic.net"]

    BASE = "https://www.webtoons.com"

    def _lang_from_url(self, url):
        m = re.match(r"https?://(?:www\.)?webtoons\.com/([a-z]{2})/", url)
        return m.group(1) if m else "en"

    def search(self, query, lang="en"):
        return []

    def _list_url(self, url):
        m = re.search(r"title_no=(\d+)", url)
        if not m:
            raise ValueError("URL do Webtoon precisa de title_no=: " + url)
        tid = m.group(1)
        if "/list" in url:
            return url.split("#")[0], tid
        # viewer URL: .../<lang>/<genre>/<comic>/<slug>/viewer?title_no=N
        path = url.split("?")[0].rstrip("/")
        parts = path.split("/")
        # drop trailing slug segment if followed by viewer
        if parts and parts[-1] == "viewer":
            parts = parts[:-1]
        # parts now end with .../<comic> or .../<comic>/<slug>; take up to comic
        # We want lang/genre/comic base. Re-derive from a known pattern.
        # base typically has at least lang/genre/comic (3 segments after host)
        if len(parts) >= 4:
            base_path = "/".join(parts[3:6])  # lang/genre/comic
        else:
            base_path = "/".join(parts[3:])
        return "%s/%s/list?title_no=%s" % (self.BASE, base_path, tid), tid

    def get_info(self, url):
        list_url, tid = self._list_url(url)
        r = _get(list_url)
        r.raise_for_status()
        t = r.text
        title = _first(r"<h1[^>]*>(.*?)</h1>", t) or _first(r"<title>(.*?)</title>", t)
        title = _strip_tags(title).replace(" | LINE WEBTOON", "").replace("| WEBTOON", "")
        cover = _first(r'property="og:image"\s+content="([^"]+)"', t)
        desc = _first(r'<p class="detail_body"[^>]*>(.*?)</p>', t, "")
        eps = {}
        for m in re.finditer(r'href="([^"]*viewer\?title_no=%s&episode_no=(\d+)[^"]*)"' % tid, t):
            href, no = m.group(1), int(m.group(2))
            if href.startswith("/"):
                href = self.BASE + href
            eps.setdefault(no, href)
        return {
            "title": title.strip(), "cover": cover,
            "description": _strip_tags(desc)[:600],
            "authors": [], "genres": [],
            "volumes": [],
            "chapters": [{"label": "Episódio %d" % no, "url": eps[no], "number": no}
                         for no in sorted(eps)],
            "url": list_url, "title_no": tid,
            "languages": [self._lang_from_url(url)], "hasVolumes": False,
            "official": True,
        }

    def list_chapters(self, url, lang="en"):
        list_url, tid = self._list_url(url)
        r = _get(list_url)
        r.raise_for_status()
        t = r.text
        chapters = {}
        for m in re.finditer(r'href="([^"]*viewer\?title_no=%s&episode_no=(\d+)[^"]*)"' % tid, t):
            href, no = m.group(1), int(m.group(2))
            if href.startswith("/"):
                href = self.BASE + href
            chapters.setdefault(no, {"label": "Episódio %d" % no, "url": href, "number": no})
        # paginação
        page = 1
        while True:
            page += 1
            r2 = _get(list_url, params={"page": str(page)})
            if not r2.ok:
                break
            added = 0
            for m in re.finditer(r'href="([^"]*viewer\?title_no=%s&episode_no=(\d+)[^"]*)"' % tid, r2.text):
                href, no = m.group(1), int(m.group(2))
                if href.startswith("/"):
                    href = self.BASE + href
                if no not in chapters:
                    chapters[no] = {"label": "Episódio %d" % no, "url": href, "number": no}
                    added += 1
            if added == 0 or page > 60:
                break
        return [chapters[no] for no in sorted(chapters)]

    def item_pages(self, url):
        r = _get(url, allow_redirects=True)
        r.raise_for_status()
        return r.text.count('class="_images" data-url="')


# ============================================================
# Naver Webtoon
# ============================================================
class NaverWebtoonAdapter(BaseAdapter):
    key = "naver"
    label = "Naver Webtoon"
    folder = "naver-webtoon"
    official = True
    langs = ["ko"]
    lang_labels = {"ko": "Coreano"}
    lang = "ko"
    cover_hosts = ["thumbnail.naver.net", "pstatic.net", "comic.naver.com"]

    BASE = "https://m.comic.naver.com"
    DESKTOP = "https://comic.naver.com"

    def search(self, query, lang="ko"):
        return []

    def _title_id(self, url):
        m = re.search(r"titleId=(\d+)", url)
        if not m:
            raise ValueError("URL do Naver precisa de titleId=: " + url)
        return m.group(1)

    def get_info(self, url):
        tid = self._title_id(url)
        list_url = self.DESKTOP + "/webtoon/list?titleId=" + tid
        r = _get(self.DESKTOP + "/webtoon/detail?titleId=" + tid + "&no=1")
        r.raise_for_status()
        t = r.text
        title = _first(r'titleName:\s*"([^"]+)"', t)
        if not title:
            title = _first(r'<h1[^>]*>(.*?)</h1>', t) or _first(r"<title>(.*?)</title>", t)
            title = _strip_tags(title).replace(" : 네이버웹툰", "").replace(" | 네이버 웹툰", "")
            title = re.sub(r"\s+\d+화.*$", "", title).strip()
        cover = _first(r'property="og:image"\s+content="([^"]+)"', t)
        chapters = self.list_chapters(url, "ko")
        return {
            "title": title, "cover": cover, "description": "", "authors": [], "genres": [],
            "volumes": [], "chapters": chapters[:11],
            "url": list_url, "titleId": tid, "languages": ["ko"],
            "hasVolumes": False, "official": True,
        }

    def list_chapters(self, url, lang="ko"):
        tid = self._title_id(url)
        chapters = []
        page = 1
        detail_path = "webtoon"
        while page <= 50:
            r2 = _get(self.DESKTOP + "/api/article/list",
                      params={"titleId": tid, "page": str(page), "sort": "ASC"},
                      headers={"Accept": "application/json", "Referer": self.DESKTOP + "/webtoon/list?titleId=" + tid})
            if not r2.ok:
                break
            try:
                j = r2.json()
            except Exception:
                break
            if page == 1:
                path = (j.get("webtoonLevelCode") or "WEBTOON").lower().replace("_c", "C", 1)
                if path:
                    detail_path = path
            eps = j.get("articleList") or []
            for e in eps:
                no = e.get("no")
                name = e.get("title") or ("Episódio %s" % no)
                chapters.append({
                    "label": _strip_tags(name),
                    "url": "%s/%s/detail?titleId=%s&no=%s" % (self.DESKTOP, detail_path, tid, no),
                    "number": _norm_num(no),
                })
            next_page = (j.get("pageInfo") or {}).get("nextPage")
            if not eps or not next_page or next_page == page:
                break
            page = next_page
        return chapters

    def item_pages(self, url):
        r = _get(url, allow_redirects=True)
        r.raise_for_status()
        t = r.text
        n = t.count('class="_images" data-url="')
        if not n:
            n = len(re.findall(r'data-url="(https?://[^"]+\.(?:jpg|png|webp)[^"]*)"', t))
        return n


# ============================================================
# BacaKomik (sucessor do Komikcast)
# ============================================================
class BacaKomikAdapter(BaseAdapter):
    key = "bacakomik"
    label = "Baca Komik"
    folder = "bacakomik"
    official = False
    langs = ["id"]
    lang_labels = {"id": "Indonésio"}
    lang = "id"
    cover_hosts = ["bacakomik.my", "i0.wp.com", "i1.wp.com", "i2.wp.com"]
    custom_download = True

    BASE = "https://bacakomik.my"

    def search(self, query, lang="id"):
        r = _get(self.BASE + "/wp-json/wp/v2/search",
                 params={"search": query, "per_page": 12, "subtype": "post"})
        out = []
        if r.ok:
            for hit in r.json():
                u = hit.get("url") or ""
                if "/komik/" not in u:
                    continue
                out.append({"title": _strip_tags(hit.get("title", "")),
                            "url": u, "cover": None})
        if not out:
            r = _get(self.BASE + "/", params={"s": query})
            if r.ok:
                seen, uniq = set(), []
                for m in re.finditer(
                        r'<a[^>]+href="(https://bacakomik\.my/komik/[^"]+)"[^>]*[^>]*title="([^"]+)"', r.text):
                    if m.group(1) not in seen:
                        seen.add(m.group(1))
                        uniq.append({"title": _strip_tags(m.group(2)), "url": m.group(1), "cover": None})
                out = uniq[:12]
        return out

    def get_info(self, url):
        r = _get(url)
        r.raise_for_status()
        t = r.text
        title = _first(r'<h1[^>]*class="[^"]*entry-title[^"]*"[^>]*>(.*?)</h1>', t) \
            or _first(r"<title>(.*?)</title>", t) or url
        cover = _first(
            r'<div[^>]*class="[^"]*(?:thumb|poster|cover)[^"]*"[^>]*>\s*'
            r'<img[^>]+(?:src|data-src)="([^"]+)"', t)
        if not cover:
            cover = _first(r'property="og:image"\s+content="([^"]+)"', t)
        desc = _first(
            r'<div[^>]*class="[^"]*(?:entry-content|synopsis|description)[^"]*"[^>]*>'
            r"(.*?)</div>", t, "")
        chapters = self.list_chapters(url, "id")
        return {
            "title": _strip_tags(title).replace(" - BacaKomik", "").strip(),
            "cover": cover,
            "description": _strip_tags(desc)[:600],
            "authors": [], "genres": [], "volumes": [],
            "chapters": chapters,
            "url": url, "languages": ["id"], "hasVolumes": False,
            "official": False,
        }

    def list_chapters(self, url, lang="id"):
        r = _get(url)
        r.raise_for_status()
        t = r.text
        slug = url.rstrip("/").split("/")[-1]
        found = {}
        for m in re.finditer(
                r'<a[^>]+href="(https://bacakomik\.my/([^"/]+)-chapter-([^"/]+)/?)"',
                t):
            u, ch_slug, num = m.group(1), m.group(2), m.group(3)
            if ch_slug != slug:
                continue
            found.setdefault(u, f"Capítulo {num}")
        out = []
        for u, lbl in found.items():
            out.append({"label": lbl, "url": u, "number": _norm_num(lbl.split()[-1])})
        return out

    def item_pages(self, url):
        return len(self.reader_images(url))

    def reader_images(self, url):
        r = _get(url)
        r.raise_for_status()
        t = r.text
        pos = t.find('id="episode"')
        if pos < 0:
            pos = t.find('class="chapter postbody"')
        blk = t[pos:pos + 200000] if pos >= 0 else t
        imgs = re.findall(r"this\.src='(https?://[^']+)'", t, re.I)
        out, seen = [], set()
        for i in imgs:
            base = i.split("?")[0]
            if re.search(r"(?:ikon|icon|avatar|logo|banner|favicon)", base, re.I):
                continue
            if ("amatipolanyatirudesainnya" not in base and "bukansiapasiapa" not in base
                    and "wp-content/uploads" not in base):
                continue
            if base in seen:
                continue
            seen.add(base)
            out.append(i)
        if not out:
            for i in re.findall(r'(https?://(?:amatipolanyatirudesainnya|bukansiapasiapa)\.lol/[^\s"<>]+)', t, re.I):
                base = i.split("?")[0]
                if base not in seen:
                    seen.add(base)
                    out.append(i)
        return out


# ============================================================
# MangaPark (bloqueado por parklogic anti-bot — stub)
# ============================================================
class MangaParkAdapter(BaseAdapter):
    key = "mangapark"
    label = "MangaPark"
    folder = "mangapark"
    official = False
    langs = ["en"]
    lang_labels = {"en": "Inglês"}
    lang = "en"
    available = False
    cover_hosts = ["mpark.to", "mangapark.io"]

    def get_info(self, url):
        raise RuntimeError("MangaPark está bloqueado por proteção anti-bot (parklogic). "
                          "Use outro site ou cole o link de um capítulo.")

    def list_chapters(self, url, lang="en"):
        raise RuntimeError("MangaPark está bloqueado por proteção anti-bot (parklogic).")

    def item_pages(self, url):
        raise RuntimeError("MangaPark está bloqueado por proteção anti-bot (parklogic).")

    def search(self, query, lang="en"):
        return []


# ============================================================
# registro
# ============================================================
ADAPTERS = {}


def register(adapter):
    ADAPTERS[adapter.key] = adapter
    return adapter


def get(key):
    return ADAPTERS.get(key)


def detect(url):
    u = (url or "").lower()
    if "mangafire.to" in u or "mangafire." in u:
        return "mangafire"
    if "mangadex.org" in u or "mangadex.cc" in u:
        return "mangadex"
    if "weebcentral.com" in u:
        return "weebcentral"
    if "hiperdex.com" in u or "hiperto" in u:
        return "hiperdex"
    if "webtoons.com" in u:
        return "webtoons"
    if "comic.naver.com" in u or "m.comic.naver.com" in u:
        return "naver"
    if "bacakomik.my" in u:
        return "bacakomik"
    if "mpark.to" in u or "mangapark.io" in u or "mangapark.net" in u:
        return "mangapark"
    # fallback: tenta pelo adapter que reconhece a URL
    return "mangafire"


def _init():
    register(MangaFireAdapter())
    register(MangaDexAdapter())
    register(WeebCentralAdapter())
    register(HiperdexAdapter())
    register(WebtoonsAdapter())
    register(NaverWebtoonAdapter())
    register(BacaKomikAdapter())
    register(MangaParkAdapter())


_init()
