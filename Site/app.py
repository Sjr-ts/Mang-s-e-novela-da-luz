# -*- coding: utf-8 -*-
"""Baixador de Manga - site local profissional.

Servidor Flask em 127.0.0.1:<porta> que serve a interface e conversa com a
API do MangaFire + o motor de download (gallery-dl) por baixo.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import mangafire
from mangafire import MangaFireError
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
    url = request.args.get("url", "")
    lang = request.args.get("lang", "pt-br")
    try:
        manga = mangafire.get_manga(url)
    except MangaFireError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    lang = lang if lang in manga["languages"] else (
        "pt-br" if "pt-br" in manga["languages"] else manga["languages"][0]
        if manga["languages"] else "en")

    try:
        chapters = mangafire.list_chapters(url, lang)
    except MangaFireError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    volumes = []
    if manga["hasVolumes"]:
        try:
            volumes = mangafire.list_volumes(url, lang)
        except MangaFireError:
            volumes = []

    return jsonify({
        "ok": True,
        "manga": manga,
        "lang": lang,
        "chapters": chapters,
        "volumes": volumes,
        "stats": {
            "chapters": len(chapters),
            "chapters_official": sum(1 for c in chapters if c["official"]),
            "volumes": len(volumes),
        },
    })


@app.get("/api/chapters")
def api_chapters():
    url = request.args.get("url", "")
    lang = request.args.get("lang", "pt-br")
    try:
        chapters = mangafire.list_chapters(url, lang)
        volumes = mangafire.list_volumes(url, lang) if mangafire.get_manga(url)["hasVolumes"] else []
    except MangaFireError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "lang": lang,
                    "chapters": chapters, "volumes": volumes})


# --------------------------------------------------------------------------
# API: download / fila / eventos
# --------------------------------------------------------------------------
@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    dest = (data.get("dest") or "").strip() or DEFAULT_DEST
    lang = (data.get("lang") or "pt-br").strip()
    resolution = data.get("resolution") or "original"
    output_format = data.get("format") or "cbz"
    keep_images = bool(data.get("keep_images"))
    official_only = bool(data.get("official_only"))
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
            int(it["id"])
    except (ValueError, TypeError, KeyError):
        return jsonify({"ok": False, "error": "Seleção inválida."}), 400

    dest = os.path.abspath(dest)
    if not dest or dest == os.path.abspath("/"):
        return jsonify({"ok": False, "error": "Pasta de destino inválida."}), 400

    try:
        job = jobs_module.manager.add_job({
            "url": url, "dest": dest, "lang": lang,
            "resolution": resolution, "format": output_format,
            "keep_images": keep_images, "official_only": official_only,
            "items": items, "title": title, "cover": cover,
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
    allowed = host.endswith("mangafire.to") or host.endswith("mfcdn.nl") \
        or host.endswith("mfcdn1.xyz") or host.endswith("mfcdn2.xyz")
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
    mf_root = os.path.join(dest, "mangafire")
    if os.path.isdir(mf_root):
        for name in sorted(os.listdir(mf_root)):
            full = os.path.join(mf_root, name)
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
                "name": name, "path": full,
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


@app.get("/api/system")
def api_system():
    ai = upscale.ai_available()
    return jsonify({
        "ok": True,
        "formats": ["cbz", "pdf", "epub", "images"],
        "resolutions": list(upscale.MODES),
        "ai": {"available": bool(ai), "models": ai},
        "version": "2.0.0",
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
