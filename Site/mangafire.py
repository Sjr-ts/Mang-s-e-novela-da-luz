# -*- coding: utf-8 -*-
"""MangaFire API helpers (in-process via gallery-dl's VRF implementation).

Only READ requests against the public MangaFire API are used here.
"""

import re

from gallery_dl import text as _gtext
from gallery_dl.extractor.mangafire import (
    MangafireMangaExtractor,
    MangafireChapterExtractor,
)

_CHAPTER = re.compile(r"^(https?://[^/]+/title/[^/?#]+)/(chapter|volume)/(\d+)", re.I)
_MANGA = re.compile(r"^(https?://[^/]+)(/title/[^/?#]+)", re.I)


class MangaFireError(Exception):
    """User-facing error (bad link, manga not found, ...)."""


def normalize_url(url):
    u = (url or "").strip().rstrip("/")
    m = _CHAPTER.match(u)
    if m:
        return m.group(1)
    m = _MANGA.match(u)
    if m:
        return m.group(1) + m.group(2)
    raise MangaFireError(
        "Link inválido. Cole o endereço da página do mangá, "
        "ex.: https://mangafire.to/title/053-fairy-taill")


def _hid(base):
    ex = MangafireMangaExtractor.from_url(base)
    ex.initialize()
    return ex, ex.groups[0]


def get_manga(url):
    base = normalize_url(url)
    ex, hid = _hid(base)
    try:
        data = ex.request_api(f"/titles/{hid}").get("data", {})
    except Exception as exc:
        raise MangaFireError(f"Não consegui ler este mangá: {exc}") from exc

    poster = data.get("poster") or {}
    cover = poster.get("large") or poster.get("medium") or poster.get("small") or poster.get("or") or ""

    def titles(seq):
        out = []
        for item in seq or []:
            if isinstance(item, dict):
                t = item.get("title") or item.get("name")
                if t:
                    out.append(str(t))
            elif item:
                out.append(str(item))
        return out

    syn = _gtext.unescape(_gtext.remove_html(data.get("synopsisHtml") or "")).strip()

    return {
        "id": hid,
        "title": data.get("title") or "Sem título",
        "url": base,
        "alt_titles": [str(t) for t in (data.get("altTitles") or [])][:6],
        "type": data.get("type"),
        "status": data.get("status"),
        "year": data.get("year"),
        "languages": list(data.get("languages") or []),
        "hasVolumes": bool(data.get("hasVolumes")),
        "cover": cover,
        "description": syn,
        "authors": titles(data.get("authors")),
        "artists": titles(data.get("artists")),
        "tags": titles(data.get("genres")),
        "themes": titles(data.get("themes")),
        "demographics": titles(data.get("demographics")),
    }


def list_chapters(url, lang):
    base = normalize_url(url)
    ex, hid = _hid(base)
    try:
        data = ex.request_api(f"/titles/{hid}/chapters", {
            "language": lang, "limit": "200", "order": "desc",
            "page": "1", "sort": "number",
        })
    except Exception as exc:
        raise MangaFireError(f"Falha ao listar capítulos: {exc}") from exc

    out = []
    page = 1
    while True:
        for it in data.get("items", []):
            ch_type = str(it.get("type") or "")
            out.append({
                "kind": "chapter",
                "id": it.get("id"),
                "number": it.get("number"),
                "name": it.get("name") or "",
                "type": ch_type,
                "official": ch_type.lower() == "official",
                "date": it.get("createdAt"),
            })
        meta = data.get("meta") or {}
        if not meta.get("hasNext"):
            break
        page += 1
        if page > 20:
            break
        try:
            data = ex.request_api(f"/titles/{hid}/chapters", {
                "language": lang, "limit": "200", "order": "desc",
                "page": str(page), "sort": "number",
            })
        except Exception as exc:
            raise MangaFireError(f"Falha ao listar capítulos (página {page}): {exc}") from exc
    out.sort(key=lambda c: (c["number"] if c["number"] is not None else -1))
    return out


def list_volumes(url, lang):
    base = normalize_url(url)
    ex, hid = _hid(base)
    try:
        data = ex.request_api(f"/titles/{hid}/volumes", {
            "language": lang, "limit": "200", "order": "desc",
            "page": "1", "sort": "number",
        })
    except Exception as exc:
        raise MangaFireError(f"Falha ao listar volumes: {exc}") from exc

    out = []
    page = 1
    while True:
        for it in data.get("items", []):
            if it.get("language") != lang:
                continue
            out.append({
                "kind": "volume",
                "id": it.get("id"),
                "number": it.get("number"),
                "name": it.get("name") or "",
                "chapterCount": it.get("chapterCount"),
                "language": it.get("language"),
                "official": True,
            })
        meta = data.get("meta") or {}
        if not meta.get("hasNext") or not data.get("items"):
            break
        page += 1
        if page > 10:
            break
        try:
            data = ex.request_api(f"/titles/{hid}/volumes", {
                "language": lang, "limit": "200", "order": "desc",
                "page": str(page), "sort": "number",
            })
        except Exception as exc:
            raise MangaFireError(f"Falha ao listar volumes (página {page}): {exc}") from exc
    out.sort(key=lambda v: (v["number"] if v["number"] is not None else -1))
    return out


def item_url(base, item):
    kind = "volume" if item.get("kind") == "volume" else "chapter"
    return f"{base}/{kind}/{int(item['id'])}"


def item_pages(url, item):
    base = normalize_url(url)
    ex = MangafireChapterExtractor.from_url(item_url(base, item))
    ex.initialize()
    ex.metadata(None)
    return len(getattr(ex, "pages", []) or [])
