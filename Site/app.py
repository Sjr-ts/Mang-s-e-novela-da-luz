# -*- coding: utf-8 -*-
"""Baixador de Manga - site local profissional.

Servidor Flask em 127.0.0.1:<porta> que serve a interface e conversa com a
API do MangaFire + o motor de download (gallery-dl) por baixo.
"""

import base64
import json
import re
import logging
import os
import ssl
import sys
import threading
import time
from pathlib import Path

# ----------------------------------------------------------------------------
# Bundle CA = certifi + stores do Windows (ROOT/CA) -> corrige TLS com ESET MITM
# ----------------------------------------------------------------------------
def _build_ca_bundle():
    try:
        import certifi
        parts = [open(certifi.where(), "rb").read()]
        seen = set()
        for store in ("ROOT", "CA"):
            try:
                entries = ssl.enum_certificates(store)
            except Exception:
                continue
            for entry in entries:
                cert = None
                for item in entry:
                    if isinstance(item, (bytes, str)) and (
                            (isinstance(item, bytes) and item.startswith(b"\\x30"))
                            or (isinstance(item, str) and "CERTIFICATE" in item)):
                        cert = item
                        break
                if cert is None:
                    continue
                if isinstance(cert, str):
                    if "BEGIN CERTIFICATE" not in cert:
                        continue
                    pem = cert
                else:
                    pem = "-----BEGIN CERTIFICATE-----\\n" + base64.encodebytes(cert).decode() + "-----END CERTIFICATE-----\\n"
                h = hash(pem)
                if h in seen:
                    continue
                seen.add(h)
                parts.append(pem.encode())
        bundle = b"".join(parts)
        path = os.path.join(os.environ.get("TEMP", "."), "bm-ca-bundle.pem")
        with open(path, "wb") as fh:
            fh.write(bundle)
        return path
    except Exception:
        return None

_CA = _build_ca_bundle()
if _CA and os.path.exists(_CA):
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA)
    os.environ.setdefault("CURL_CA_BUNDLE", _CA)

from flask import Flask, Response, jsonify, render_template, request

import sites as sites_module
import jobs as jobs_module
import upscale
import verify as verify_module

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
DEFAULT_DEST = str(REPO_DIR / "Mangas")

log = logging.getLogger("baixador.app")

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))


# --------------------------------------------------------------------------
# páginas
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", default_dest=DEFAULT_DEST)


# --------------------------------------------------------------------------
# API: informação do mangá
# --------------------------------------------------------------------------
@app.get("/api/info")
def api_info():
    url = (request.args.get("url") or "").strip()
    lang = (request.args.get("lang") or "pt-br").strip()
    site = request.args.get("site") or sites_module.detect(url)
    adapter = sites_module.get(site)
    if adapter is None:
        return jsonify({"ok": False, "error": "Site não suportado."}), 400
    if not getattr(adapter, "available", True):
        return jsonify({"ok": False, "error": "%s está indisponível no momento." % adapter.label}), 400
    try:
        info = adapter.get_info(url)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    url = info.get("url") or url
    langs = info.get("languages") or adapter.langs
    if lang not in langs:
        lang = adapter.lang if adapter.lang in langs else (langs[0] if langs else adapter.lang)

    try:
        chapters_raw = adapter.list_chapters(url, lang)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    volumes_raw = []
    if info.get("hasVolumes") or site == "mangafire":
        try:
            volumes_raw = adapter.list_volumes(url, lang)
        except Exception:
            volumes_raw = []

    # normaliza capítulos para o formato esperado pela UI
    if site in ("webtoons", "naver"):
        def _prefix(n):
            try:
                return "e%03d" % int(float(n))
            except (TypeError, ValueError):
                return "e" + str(n)
    else:
        def _prefix(n):
            return None  # usa lógica padrão c/v

    def _prefix_v(n):
        try:
            return "v%02d" % int(float(n))
        except (TypeError, ValueError):
            return None

    chapters = []
    for c in chapters_raw:
        chapters.append({
            "kind": "chapter",
            "id": c.get("id") if c.get("id") is not None else c.get("url"),
            "number": c.get("number"),
            "name": c.get("label") or c.get("name", ""),
            "url": c.get("url"),
            "official": bool(c.get("official", adapter.official)),
            "prefix": _prefix(c.get("number")),
        })
    volumes = []
    for v in volumes_raw:
        volumes.append({
            "kind": "volume",
            "id": v.get("id") if v.get("id") is not None else v.get("url"),
            "number": v.get("number"),
            "name": v.get("label") or v.get("name", ""),
            "url": v.get("url"),
            "official": True,
            "prefix": _prefix_v(v.get("number")),
        })

    manga = {
        "title": info.get("title", "Sem título"),
        "url": url,
        "cover": info.get("cover", ""),
        "description": info.get("description", ""),
        "authors": info.get("authors", []),
        "artists": info.get("artists", []),
        "tags": info.get("genres", []),
        "languages": langs,
        "hasVolumes": bool(info.get("hasVolumes")),
        "site": site,
        "site_label": adapter.label,
        "official_filter": bool(site in ("mangafire", "weebcentral")),
    }

    return jsonify({
        "ok": True,
        "site": site,
        "site_label": adapter.label,
        "manga": manga,
        "lang": lang,
        "chapters": chapters,
        "volumes": volumes,
        "stats": {
            "chapters": len(chapters),
            "chapters_official": sum(1 for c in chapters if c["official"]),
            "volumes": len(volumes),
        },
        "languages": [{"code": l, "label": adapter.lang_labels.get(l, l)} for l in langs],
        "official_filter": manga["official_filter"],
    })


@app.get("/api/chapters")
def api_chapters():
    url = (request.args.get("url") or "").strip()
    lang = (request.args.get("lang") or "pt-br").strip()
    site = request.args.get("site") or sites_module.detect(url)
    adapter = sites_module.get(site)
    if adapter is None:
        return jsonify({"ok": False, "error": "Site não suportado."}), 400
    try:
        info = adapter.get_info(url)
        url = info.get("url") or url
        chapters_raw = adapter.list_chapters(url, lang)
        volumes_raw = adapter.list_volumes(url, lang) if (info.get("hasVolumes") or site == "mangafire") else []
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    def _ep_prefix(n):
        if site not in ("webtoons", "naver") or n is None:
            return None
        try:
            return "e%03d" % int(float(n))
        except (TypeError, ValueError):
            return None
    chapters = [{"kind": "chapter", "id": c.get("id") or c.get("url"), "number": c.get("number"),
                 "name": c.get("label") or c.get("name", ""), "url": c.get("url"),
                 "official": bool(c.get("official", adapter.official)),
                 "prefix": _ep_prefix(c.get("number"))}
                for c in chapters_raw]
    volumes = [{"kind": "volume", "id": v.get("id") or v.get("url"), "number": v.get("number"),
                "name": v.get("label") or v.get("name", ""), "url": v.get("url"),
                "official": True} for v in volumes_raw]
    return jsonify({"ok": True, "site": site, "lang": lang, "chapters": chapters, "volumes": volumes})


# --------------------------------------------------------------------------
# API: download / fila / eventos
# --------------------------------------------------------------------------
@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    site = data.get("site") or sites_module.detect(url)
    dest = (data.get("dest") or "").strip() or DEFAULT_DEST
    lang = (data.get("lang") or "pt-br").strip()
    resolution = data.get("resolution") or "original"
    output_format = data.get("format") or "cbz"
    keep_images = bool(data.get("keep_images"))
    official_only = bool(data.get("official_only"))
    force = bool(data.get("force"))
    items = data.get("items") or []
    title = data.get("title") or ""
    cover = data.get("cover") or ""

    if resolution not in upscale.MODES:
        return jsonify({"ok": False, "error": "Resolução inválida."}), 400
    if output_format not in ("cbz", "pdf", "epub", "images"):
        return jsonify({"ok": False, "error": "Formato inválido."}), 400
    if resolution.startswith("waifu2x") and resolution not in upscale.ai_available():
        return jsonify({"ok": False,
                        "error": "Waifu2x não instalado nesta máquina "
                                 "(pasta tools/waifu2x ausente ou incompleta)."}), 400
    if not url:
        return jsonify({"ok": False, "error": "Falta o link do mangá."}), 400
    try:
        items = [dict(it) for it in items]
        for it in items:
            if it.get("kind") not in ("chapter", "volume"):
                raise ValueError("tipo de item inválido")
            if site == "mangafire":
                if it.get("id") is None and it.get("url"):
                    # recupera id a partir da url do capítulo/volume
                    m = re.search(r"/(?:chapter|volume)/(\d+)", it["url"])
                    if m:
                        it["id"] = int(m.group(1))
                int(it["id"])
            else:
                if not it.get("url"):
                    raise ValueError("item sem url")
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("selecao invalida: %s | site=%s | items=%r", exc, site, items[:2])
        return jsonify({"ok": False, "error": "Seleção inválida: %s" % exc}), 400

    dest = os.path.abspath(dest)
    if not dest or dest == os.path.abspath("/"):
        return jsonify({"ok": False, "error": "Pasta de destino inválida."}), 400

    try:
        job = jobs_module.manager.add_job({
            "url": url, "site": site, "dest": dest, "lang": lang,
            "resolution": resolution, "format": output_format,
            "keep_images": keep_images, "official_only": official_only,
            "force": force, "items": items, "title": title, "cover": cover,
        })
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Não consegui usar a pasta: {exc}"}), 400

    return jsonify({"ok": True, "job": job.to_dict()})


@app.get("/api/jobs")
def api_jobs():
    return jsonify({"ok": True, "jobs": jobs_module.manager.snapshot()})


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id):
    ok = jobs_module.manager.cancel(job_id)
    if not ok:
        return jsonify({"ok": False, "error": "Job não encontrado."}), 404
    return jsonify({"ok": True})


@app.get("/api/events")
def api_events():
    def stream():
        last_sig = None
        idle = 0
        while True:
            snap = jobs_module.manager.snapshot()
            sig = json.dumps(snap, sort_keys=True, default=str)
            if sig != last_sig:
                yield f"data: {sig}\n\n"
                last_sig = sig
                idle = 0
            else:
                idle += 1
                if idle >= 75:      # ~30s sem novidades: keep-alive
                    yield ": keep-alive\n\n"
                    idle = 0
            time.sleep(0.4)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# API: capa (proxy) e biblioteca
# --------------------------------------------------------------------------
@app.get("/api/cover")
def api_cover():
    url = request.args.get("u", "")
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = [
        "mangafire.to", "mfcdn.nl", "mfcdn1.xyz", "mfcdn2.xyz",
        "uploads.mangadex.org",
        "temp.compsci88.com", "official.lowee.us",
        "hiperdex.com", "hiper.to",
        "webtoon-phinf.pstatic.net", "swebtoon-phinf.pstatic.net",
        "thumbnail.naver.net", "pstatic.net", "comic.naver.com",
        "bacakomik.my", "i0.wp.com", "i1.wp.com", "i2.wp.com",
        "mpark.to", "mangapark.io", "mangapark.net",
    ]
    allowed = any(host == h or host.endswith("." + h) or host.endswith(h) for h in allowed_hosts)
    if not allowed:
        return "host não permitido", 403

    import requests
    try:
        r = requests.get(url, timeout=15, stream=True)
        r.raise_for_status()
        from flask import stream_with_context
        return Response(stream_with_context(r.iter_content(chunk_size=8192)),
                        content_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return "capa indisponível", 502


ARCHIVE_EXTS = (".cbz", ".pdf", ".epub")


def _count_archives(root):
    counts = {"cbz": 0, "pdf": 0, "epub": 0}
    newest = 0.0
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in (".cbz", ".pdf", ".epub"):
                    counts[ext[1:]] += 1
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                    except OSError:
                        pass
    except OSError:
        pass
    return counts, newest


@app.get("/api/library")
def api_library():
    dest = os.path.abspath(request.args.get("dest") or DEFAULT_DEST)
    items = []
    for site_key, adapter in sites_module.ADAPTERS.items():
        site_root = os.path.join(dest, adapter.folder)
        if not os.path.isdir(site_root):
            continue
        for name in sorted(os.listdir(site_root)):
            full = os.path.join(site_root, name)
            if not os.path.isdir(full):
                continue
            counts, newest = _count_archives(full)
            size = 0
            try:
                for dirpath, _dirs, files in os.walk(full):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in ARCHIVE_EXTS:
                            size += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
            items.append({
                "name": name, "path": full, "site": site_key,
                "site_label": adapter.label,
                "cbz": counts["cbz"], "pdf": counts["pdf"], "epub": counts["epub"],
                "archives": counts["cbz"] + counts["pdf"] + counts["epub"],
                "size": size, "newest": newest,
            })
    items.sort(key=lambda x: x["newest"], reverse=True)
    total_cbz = sum(i["cbz"] for i in items)
    total_pdf = sum(i["pdf"] for i in items)
    total_epub = sum(i["epub"] for i in items)
    total_size = sum(i["size"] for i in items)
    return jsonify({"ok": True, "dest": dest, "items": items,
                    "stats": {"manga": len(items), "cbz": total_cbz,
                              "pdf": total_pdf, "epub": total_epub,
                              "archives": total_cbz + total_pdf + total_epub,
                              "size": total_size}})


@app.post("/api/open-folder")
def api_open_folder():
    data = request.get_json(silent=True) or {}
    path = os.path.abspath((data.get("path") or DEFAULT_DEST).strip())
    dest_root = os.path.abspath(data.get("root") or DEFAULT_DEST)
    # segurança: só abre pastas dentro do diretório de downloads
    try:
        common = os.path.commonpath([path, dest_root])
    except ValueError:
        return jsonify({"ok": False, "error": "Caminho inválido."}), 400
    if common != dest_root or not os.path.isdir(path):
        return jsonify({"ok": False, "error": "Pasta fora do diretório de downloads."}), 400
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - explorer local, caminho validado
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    return jsonify({"ok": True})


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    site = (request.args.get("site") or "").strip()
    lang = (request.args.get("lang") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "Falta o termo de busca."}), 400
    targets = [site] if site and site in sites_module.ADAPTERS else list(sites_module.ADAPTERS.keys())
    results = []
    for sk in targets:
        adapter = sites_module.get(sk)
        if adapter is None or not getattr(adapter, "available", True):
            continue
        try:
            hits = adapter.search(q, lang or adapter.lang)
        except Exception:
            hits = []
        for h in hits:
            results.append({
                "title": h.get("title", ""),
                "url": h.get("url", ""),
                "cover": h.get("cover"),
                "site": sk,
                "site_label": adapter.label,
            })
    return jsonify({"ok": True, "query": q, "results": results})


@app.get("/api/system")
def api_system():
    ai = upscale.ai_available()
    return jsonify({
        "ok": True,
        "formats": ["cbz", "pdf", "epub", "images"],
        "resolutions": list(upscale.MODES),
        "ai": {"available": bool(ai), "models": ai},
        "version": "3.0.0",
        "sites": [{"key": k, "label": a.label, "available": bool(getattr(a, "available", True)),
                   "langs": a.langs, "lang_labels": a.lang_labels,
                   "official": a.official}
                  for k, a in sites_module.ADAPTERS.items()],
    })


@app.post("/api/verify")
def api_verify():
    data = request.get_json(silent=True) or {}
    root = os.path.abspath((data.get("root") or DEFAULT_DEST).strip())
    if not os.path.isdir(root):
        return jsonify({"ok": False, "error": "Pasta não encontrada."}), 400
    report = verify_module.scan(root)
    return jsonify({"ok": True, **report})


@app.post("/api/fix")
def api_fix():
    data = request.get_json(silent=True) or {}
    root = os.path.abspath((data.get("root") or DEFAULT_DEST).strip())
    if not os.path.isdir(root):
        return jsonify({"ok": False, "error": "Pasta não encontrada."}), 400
    result = verify_module.fix_all(root)
    return jsonify({"ok": True, **result})


@app.get("/api/ping")
def api_ping():
    return jsonify({"ok": True, "time": time.time()})


@app.post("/api/shutdown")
def api_shutdown():
    """Desliga o servidor local (usado pelo botão Desligar do site)."""
    def _die():
        time.sleep(0.4)          # dá tempo de a resposta chegar ao navegador
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("BAIXADOR_PORT", "8765"))
    # 127.0.0.1 apenas — ninguém de fora acessa esta máquina
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
