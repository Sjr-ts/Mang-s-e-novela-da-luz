# -*- coding: utf-8 -*-
"""Job manager: download queue with phases (download → upscale → convert),
progress snapshots for the UI and cooperative cancellation."""

import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

from gallery_dl import config as gd_config
from gallery_dl import job as gd_job

import formats
import mangafire
import sites as sites_module
import upscale

LOG_FILE = Path(__file__).resolve().parent / "logs" / "baixador.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_log = logging.getLogger("baixador")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
FORMATS = ("cbz", "pdf", "epub", "images")


class JobCancel(BaseException):
    """Abort requested from the download/upscale callbacks."""


def _fmt_item(item):
    num = item.get("number") or 0
    extra = f"V{num:02d}" if item["kind"] == "volume" else f"cap.{num:g}"
    name = (item.get("name") or "").strip()
    return f"{extra} {name}".strip()


class CountingOutput():
    """gallery-dl output that counts pages and records WHERE they went.

    gallery-dl passes the exact file path to skip()/success(), so we know the
    real item folder without guessing by mtime (which used to pick the wrong
    folder and break the "already downloaded" detection).
    """

    def __init__(self, job):
        self.job = job
        self.dirs = []          # pastas de imagem tocadas por este item
        self._seen = set()

    def start(self, path):
        self._record(path)

    def skip(self, path):
        self._record(path)
        self._tick()

    def success(self, path):
        self._record(path)
        self._tick()

    def _record(self, path):
        try:
            d = os.path.dirname(os.path.abspath(str(path)))
        except Exception:
            return
        if d and d not in self._seen:
            self._seen.add(d)
            self.dirs.append(d)

    def progress(self, bytes_total, bytes_downloaded, bytes_per_second):
        pass

    def _tick(self):
        job = self.job
        with job.lock:
            job.item_pages_done += 1
            job.pages_done += 1
        if job.cancel_event.is_set():
            raise JobCancel()


class JobLogHandler(logging.Handler):
    def __init__(self, job):
        super().__init__(level=logging.WARNING)
        self.job = job

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.job.push_message(f"{record.levelname}: {msg}")


def _remove_empty_dirs(root):
    """Remove empty directories under <root> (left by gallery-dl)."""
    root = Path(root)
    if not root.is_dir():
        return []
    removed = []
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False,
                                                  onerror=lambda _e: None):
        p = Path(dirpath)
        try:
            p.rmdir()  # só some se estiver vazia
            removed.append(str(p))
        except OSError:
            pass
    return removed


def _safe_dirname(name):
    bad = '<>:"/\\\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in (name or ""))
    out = re.sub(r"\s+", " ", out).strip()
    return out or "manga"


def _item_prefix(item, site_folder=""):
    """Nome-base do arquivo/pasta deste item (ex.: c001, e012, v03)."""
    if item.get("prefix"):
        return item["prefix"]
    if item.get("kind") == "volume":
        try:
            return f"v{int(item.get('number') or 0):02d}"
        except (TypeError, ValueError):
            return "v"
    raw = item.get("number")
    try:
        if float(raw).is_integer():
            n = str(int(raw))
        else:
            n = str(raw)
    except (TypeError, ValueError):
        n = str(raw) if raw is not None else "0"
    i, dot, f = n.partition(".")
    try:
        i = int(float(i))
    except ValueError:
        i = 0
    # webtoons/naver gravam os episodios como e001, e002, ...
    letter = "e" if site_folder in ("webtoons", "naver-webtoon") else "c"
    return f"{letter}{i:03d}{dot + f if dot else ''}"


def _norm_title(name):
    """Normaliza titulo/pasta para comparar 'Zodiac Girls' com 'zodiac-girls'."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _item_key(item, title=""):
    num = item.get("number")
    try:
        num_s = ("%g" % float(num)) if num is not None else "?"
    except (TypeError, ValueError):
        num_s = str(num)
    # titulo faz parte da chave: cap.1 de UMA obra nunca vale por outra
    return "%s|%s:%s" % (_norm_title(title), item.get("kind") or "chapter",
                         num_s)


# --------------------------------------------------------------------------
# Manifesto de itens concluidos (fonte da verdade para "ja baixado")
# --------------------------------------------------------------------------
_MANIFEST_NAME = ".baixador-done.json"


def _manifest_path(dest, site_folder):
    return Path(dest) / site_folder / _MANIFEST_NAME


def _manifest_load(dest, site_folder):
    try:
        data = json.loads(_manifest_path(dest, site_folder)
                          .read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _manifest_save(dest, site_folder, data):
    p = _manifest_path(dest, site_folder)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
    except (OSError, ValueError):
        pass


def _manifest_get(dest, site_folder, item, title=""):
    """Caminho final do item se ele EXISTIR no disco; senao None."""
    data = _manifest_load(dest, site_folder)
    ent = data.get(_item_key(item, title))
    if not ent or not ent.get("path"):
        return None
    p = Path(dest) / ent["path"]
    try:
        if ent.get("kind") == "dir":
            ok = p.is_dir() and any(
                f.is_file() and f.suffix.lower() in IMAGE_EXTS
                for f in p.iterdir())
        else:
            ok = p.is_file() and p.stat().st_size > 0
    except OSError:
        ok = False
    if not ok:
        # arquivo foi apagado/movido: esquece a entrada
        data.pop(_item_key(item, title), None)
        _manifest_save(dest, site_folder, data)
        return None
    return p


def _manifest_put(dest, site_folder, item, path, title=""):
    try:
        rel = os.path.relpath(str(path), str(dest)).replace("\\", "/")
    except ValueError:
        rel = str(path)
    data = _manifest_load(dest, site_folder)
    key = _item_key(item, title)
    if data.get(key, {}).get("path") == rel:
        return
    data[key] = {"path": rel,
                 "kind": "dir" if os.path.isdir(str(path)) else "file",
                 "ts": int(time.time())}
    _manifest_save(dest, site_folder, data)


def _manifest_drop(dest, site_folder, item, title=""):
    data = _manifest_load(dest, site_folder)
    if _item_key(item, title) in data:
        data.pop(_item_key(item, title), None)
        _manifest_save(dest, site_folder, data)


def _title_folder(root, title):
    """Pasta dentro de <root> que pertence a esta obra (escopo por titulo)."""
    try:
        entries = [e for e in os.listdir(root)
                   if os.path.isdir(os.path.join(root, e))]
    except OSError:
        return None
    if not entries:
        return None
    want = _norm_title(title)
    if not want:
        # titulo sem letras/digitos (ex.: coreano): so confia se ha UMA obra
        return Path(root) / entries[0] if len(entries) == 1 else None
    for e in entries:
        if _norm_title(e) == want:
            return Path(root) / e
    # tolerancia: uma pasta contem a outra (ex.: titulo x titulo — extra)
    cands = []
    for e in entries:
        n = _norm_title(e)
        if len(want) >= 4 and len(n) >= 4 and (want in n or n in want):
            cands.append(e)
    if len(cands) == 1:
        return Path(root) / cands[0]
    # Se o titulo NAO da para normalizar (ex.: coreano) e so ha UMA obra na
    # pasta, nao ha risco de escolher a errada. Para titulo normalizavel, a
    # falta de correspondencia significa que a obra ainda nao foi baixada.
    return None


def _name_matches_item(name, item, prefix):
    """O nome de arquivo/pasta corresponde EXATAMENTE a este item?

    Regra 1: prefixo com fronteira (c001, c001_Titulo, e012, v03).
    Regra 2: o primeiro token codifica o numero (e001, v01, c001.5, 001).
    Evita o bug classico: 'c001' de OUTRA obra ser lido como 'ja baixado'.
    """
    stem = os.path.splitext(str(name))[0]
    if prefix and (stem == prefix or stem.startswith(prefix + "_")
                   or stem.startswith(prefix + "-")
                   or ("." in prefix and stem.startswith(prefix + "."))):
        return True
    token = re.split(r"[_\s]", stem, 1)[0]
    m = re.match(r"^[A-Za-z]*?(\d+)(?:\.(\d+))?$", token)
    if not m:
        return False
    num = item.get("number")
    if num is None:
        return False
    try:
        got = float(m.group(1)) + (
            float("0." + m.group(2)) if m.group(2) else 0.0)
        want = float(num)
    except (TypeError, ValueError):
        return False
    return abs(got - want) < 1e-6


def _existing_output(dest, item, site_folder="mangafire", title="",
                     output_format="cbz"):
    """True se o item final (CBZ/PDF/EPUB) ja existe no disco.

    Procura SOMENTE na pasta desta obra e com casamento exato do nome —
    nunca em outras pastas (era assim que capitulo de outro mangá era
    pulado por engano).
    """
    if _manifest_get(dest, site_folder, item, title) is not None:
        return True
    if output_format == "images":
        # pasta de imagens so conta se o manifesto disser que terminou
        return False
    root = Path(dest) / site_folder
    if not root.is_dir():
        return False
    folder = _title_folder(root, title)
    if folder is None:
        return False
    prefix = _item_prefix(item, site_folder)
    for dirpath, _dirnames, filenames in os.walk(folder,
                                                  onerror=lambda _e: None):
        for name in filenames:
            lower = name.lower()
            if lower.endswith((".cbz", ".pdf", ".epub")) and \
                    _name_matches_item(name, item, prefix):
                _manifest_put(dest, site_folder, item,
                              os.path.join(dirpath, name), title)
                return True
    return False


def _download_direct(job, item, url, manga_root, label):
    """Download de imagens direto (sites sem extractor gallery-dl, ex. bacakomik)."""
    adapter = sites_module.get(job.site)
    imgs = adapter.reader_images(url)
    if not imgs:
        raise RuntimeError("nenhuma imagem encontrada")
    title_dir = Path(manga_root) / _safe_dirname(job.title)
    prefix = _item_prefix(item, job.site_folder)
    itemdir = title_dir / prefix
    itemdir.mkdir(parents=True, exist_ok=True)

    session = sites_module.http()
    session.headers.update({"User-Agent": UA,
                            "Referer": url.rsplit("/", 1)[0] + "/"})
    total = len(imgs)
    for i, img in enumerate(imgs, 1):
        if job.cancel_event.is_set():
            raise JobCancel()
        ext = os.path.splitext(img.split("?")[0])[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        dest_path = itemdir / f"{i:03d}{ext}"
        if dest_path.is_file() and dest_path.stat().st_size > 0:
            # já baixada — apenas tick
            with job.lock:
                job.item_pages_done += 1
                job.pages_done += 1
            continue
        last_err = None
        for attempt in (1, 2):
            try:
                resp = session.get(img, timeout=40, stream=True)
                resp.raise_for_status()
                tmp = Path(str(dest_path) + ".part")
                size = 0
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
                            size += len(chunk)
                if size <= 0:
                    raise IOError("resposta vazia")
                os.replace(str(tmp), str(dest_path))
                last_err = None
                break
            except Exception as exc:      # tenta de novo uma vez
                last_err = exc
                time.sleep(0.6 * attempt)
        if last_err is not None:
            try:
                dest_path.unlink()
            except OSError:
                pass
            raise RuntimeError(f"falha na imagem {i}/{total}: {last_err}")
        with job.lock:
            job.item_pages_done += 1
            job.pages_done += 1
    return itemdir


def _strip_item_from_archive(dest, item, site_folder="mangafire", archive_name=None):
    """Remove this item's rows from the download archive. Returns rows removed."""
    if archive_name is None:
        archive_name = ".download-archive.txt" if site_folder == "mangafire" else ".download-archive-%s.txt" % site_folder
    archive = Path(dest) / archive_name
    if not archive.is_file():
        return 0
    keys = []
    if item.get("id") is not None:
        keys.append(str(item["id"]))
    if item.get("number") is not None:
        try:
            keys.append("%g" % float(item["number"]))
        except (TypeError, ValueError):
            keys.append(str(item["number"]))
    patterns = []
    for key in keys:
        if key:
            patterns.append("%%_%s_%%" % key)     # ..._<key>_...
            patterns.append("%s_%%" % key)        # <key>_...
    url = (item.get("url") or "").strip()
    if url:
        patterns.append("%%" + url + "%%")        # entrada = url
    if not patterns:
        return 0
    try:
        import sqlite3
        total = 0
        with sqlite3.connect(str(archive), timeout=60) as conn:
            for pat in patterns:
                cur = conn.execute(
                    "DELETE FROM archive WHERE entry LIKE ?", (pat,))
                total += cur.rowcount
        return total
    except Exception as exc:
        _log.warning("nao consegui ajustar o archive %s: %s", archive, exc)
        return 0


def _folder_from_report(job, desc_dest, dirs, item, item_start):
    """Pasta de paginas deste item, a partir dos caminhos do gallery-dl."""
    prefix = _item_prefix(item, job.site_folder)
    real = []
    for d in dirs:
        p = Path(d)
        try:
            if p.is_dir() and any(c.is_file() and c.suffix.lower() in IMAGE_EXTS
                                  for c in p.iterdir()):
                real.append(p)
        except OSError:
            continue
    if len(real) == 1:
        return real[0]
    if len(real) > 1:
        match = [p for p in real if _name_matches_item(p.name, item, prefix)]
        if match:
            return max(match, key=lambda p: len(p.parts))
        return max(real, key=lambda p: len(p.parts))
    # nada foi gravado desta vez: procura paginas de uma tentativa anterior
    try:
        p = formats.find_item_folder(desc_dest, item_start,
                                     job.site_folder, job.title)
    except Exception:
        _log.exception("item folder detection failed")
        p = None
    if p is None:
        p = formats.find_dir_for_item(desc_dest, item, job.title,
                                      job.site_folder)
    return p


class Job:
    def __init__(self, payload):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()

        self.url = payload["url"]
        self.site = payload.get("site") or sites_module.detect(self.url)
        self.title = payload.get("title") or ""
        self.cover = payload.get("cover") or ""
        self.dest = payload["dest"]
        self.lang = payload.get("lang") or "pt-br"
        self.resolution = payload.get("resolution") or "original"
        self.output_format = payload.get("format") or "cbz"
        self.keep_images = bool(payload.get("keep_images"))
        self.official_only = bool(payload.get("official_only"))
        self.force = bool(payload.get("force"))
        adapter = sites_module.get(self.site)
        self.site_folder = getattr(adapter, "folder", "mangafire") if adapter else "mangafire"

        items = []
        for it in payload.get("items", []):
            raw_id = it.get("id")
            try:
                raw_id = int(raw_id)
            except (ValueError, TypeError):
                pass
            items.append({
                "kind": it["kind"],
                "id": raw_id,
                "number": it.get("number"),
                "name": it.get("name") or "",
                "official": bool(it.get("official", True)),
                "url": it.get("url") or "",
                "prefix": it.get("prefix") or "",
            })
        if self.official_only:
            items = [it for it in items
                     if it["kind"] == "volume" or it["official"]]
        self.items = items

        self.status = "queued"          # queued | running | done | canceled | error
        self.error = ""
        self.messages = deque(maxlen=200)
        self.items_done = 0
        self.items_failed = 0
        self.item_index = -1
        self.item_label = ""
        self.item_pages_total = 0
        self.item_pages_done = 0
        self.phase = ""                 # "" | download | upscale | convert
        self.phase_done = 0
        self.phase_total = 0
        self.pages_done = 0
        self.pages_total = 0
        self.created = time.time()
        self.started = None
        self.item_started = None
        self.finished = None

    # -- helpers ----------------------------------------------------------
    def push_message(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            self.messages.append(line)
        _log.info("%s %s", self.id, msg)

    def to_dict(self):
        with self.lock:
            elapsed = (time.time() - self.started) if self.started else 0
            remaining_pages = max(0, self.pages_total - self.pages_done)
            rate = (self.pages_done / elapsed) if elapsed > 0 and self.pages_done else 0
            eta = (remaining_pages / rate) if rate > 0 else None
            if self.item_started and self.item_pages_done:
                cur_rate = self.item_pages_done / max(1e-6, time.time() - self.item_started)
                cur_eta = ((self.item_pages_total - self.item_pages_done) / cur_rate) if cur_rate > 0 else None
            else:
                cur_eta = None
            return {
                "id": self.id,
                "status": self.status,
                "error": self.error,
                "title": self.title,
                "cover": self.cover,
                "lang": self.lang,
                "resolution": self.resolution,
                "format": self.output_format,
                "items_total": len(self.items),
                "items_done": self.items_done,
                "items_failed": self.items_failed,
                "item_index": self.item_index,
                "item_label": self.item_label,
                "item_pages_total": self.item_pages_total,
                "item_pages_done": self.item_pages_done,
                "phase": self.phase,
                "phase_done": self.phase_done,
                "phase_total": self.phase_total,
                "pages_total": self.pages_total,
                "pages_done": self.pages_done,
                "elapsed": elapsed,
                "eta": eta,
                "item_eta": cur_eta,
                "messages": list(self.messages),
                "created": self.created,
                "started": self.started,
                "finished": self.finished,
                "dest": self.dest,
            }


class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = []
        self.worker = threading.Thread(target=self._worker, daemon=True,
                                       name="job-worker")
        self.worker.start()

    # -- API ---------------------------------------------------------------
    def add_job(self, payload):
        dest = os.path.abspath(payload["dest"])
        os.makedirs(dest, exist_ok=True)
        fmt = payload.get("format") or "cbz"
        if fmt not in FORMATS:
            raise ValueError("Formato inválido.")
        job = Job({**payload, "dest": dest, "format": fmt})
        if not job.items:
            raise ValueError("Nenhum item selecionado para baixar.")
        with self.lock:
            self.jobs.append(job)
        job.push_message(f"Na fila: {len(job.items)} item(ns) de "
                         f"{job.title or job.url}")
        return job

    def snapshot(self):
        with self.lock:
            return [j.to_dict() for j in self.jobs]

    def get(self, job_id):
        with self.lock:
            for j in self.jobs:
                if j.id == job_id:
                    return j
        return None

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job:
            return False
        job.cancel_event.set()
        job.push_message("Cancelamento solicitado...")
        return True

    # -- worker ------------------------------------------------------------
    def _next_queued(self):
        with self.lock:
            for j in self.jobs:
                if j.status == "queued" and not j.cancel_event.is_set():
                    return j
            # jobs cancelados na fila: marca como canceled para nao ficar eterno
            for j in self.jobs:
                if j.status == "queued" and j.cancel_event.is_set():
                    j.status = "canceled"
                    j.finished = time.time()
        return None

    def _worker(self):
        while True:
            job = self._next_queued()
            if job is None:
                time.sleep(0.4)
                continue
            try:
                self._run_job(job)
            except Exception:
                _log.exception("job %s failed", job.id)
                with job.lock:
                    job.status = "error"
                    job.error = traceback.format_exc(limit=3)
                    job.phase = ""
                job.push_message("ERRO inesperado (veja o log).")

    def _run_job(self, job):
        with job.lock:
            job.status = "running"
            job.started = time.time()
        job.push_message(f"Iniciando: {job.title or job.url} "
                         f"({job.lang}) -> {job.dest}")

        dest = job.dest
        if job.site == "mangafire":
            archive_name = ".download-archive.txt"
        else:
            archive_name = ".download-archive-%s.txt" % job.site
        archive = os.path.join(dest, archive_name)
        os.makedirs(dest, exist_ok=True)

        gd_config.setg("base-directory", dest)
        gd_config.setg("path-restrict", "windows")
        gd_config.setg("archive", archive)
        gd_config.setg("postprocessors", [])  # montamos nós mesmos o arquivo final

        # diretorios por site (per-episode para webtoons/naver)
        try:
            gd_config.set(("extractor", "webtoons"), "directory",
                          ["{category}", "{comic}", "e{episode_no:>03}"])
            gd_config.set(("extractor", "webtoons"), "bgm", False)
            gd_config.set(("extractor", "webtoons"), "sleep-request", 0)
            gd_config.set(("extractor", "naver-webtoon"), "directory",
                          ["{category}", "{comic}", "e{episode:>03}"])
            gd_config.set(("extractor", "naver-webtoon"), "sleep-request", 0)
            gd_config.set(("extractor", "mangadex"), "directory",
                          ["{category}", "{manga}", "c{chapter:>03}{chapter_minor}{title:?: //}"])
            gd_config.set(("extractor", "mangadex"), "sleep-request", 0)
            gd_config.set(("extractor", "weebcentral"), "sleep-request", 0)
            gd_config.set(("extractor", "hiperdex"), "sleep-request", 0)
        except Exception as exc:
            _log.warning("config override failed: %s", exc)

        log_handler = JobLogHandler(job)
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        adapter = sites_module.get(job.site)
        is_mf = (job.site == "mangafire")

        def _count(it):
            try:
                if is_mf:
                    # mangafire conta via item_url+id
                    try:
                        return mangafire.item_pages(job.url, it)
                    except Exception:
                        return 0
                if adapter is not None and getattr(adapter, "custom_download", False):
                    return adapter.item_pages(it.get("url") or "")
                if adapter is not None:
                    return adapter.item_pages(it.get("url") or "")
                return 0
            except Exception as exc:
                _log.warning("pages count failed for %s: %s", it, exc)
                return 0

        def _gd_download(url, label, item, item_start):
            """Baixa um item com o gallery-dl e devolve a pasta de paginas.

            Usa os caminhos EXATOS que o gallery-dl reporta (CountingOutput)
            em vez de adivinhar a pasta pela data de modificacao — era assim
            que a pasta errada era escolhida e o item acabava com erro.
            """
            from gallery_dl import extractor as _gd_extr
            extr = _gd_extr.find(url)
            if extr is None:
                raise RuntimeError("nenhum extractor para " + url)
            out = CountingOutput(job)
            dj = gd_job.DownloadJob(extr)
            dj.out = out
            status = dj.run()
            if status:
                job.push_message(f"{label}: concluído com avisos (código {status}).")
            else:
                job.push_message(f"{label}: download ok.")
            return _folder_from_report(job, dest, out.dirs, item, item_start)

        BUDGET = 10  # (referencia) contagem de paginas roda em 2o plano
        page_counts = []
        job._already_done = []
        job._archive_purged = False
        for idx, it in enumerate(job.items):
            if (not job.force) and _existing_output(
                    dest, it, job.site_folder, job.title, job.output_format):
                # CBZ/PDF/EPUB final ja existe: nao rebaixa
                job._already_done.append(idx)
                page_counts.append(0)
                continue
            page_counts.append(None)
        with job.lock:
            job.pages_total = 0
        if job._already_done:
            job.push_message("%d item(ns) ja estavam baixados (arquivo final existente) - pulando."
                             % len(job._already_done))

        # contagem de paginas em 2o plano: ETA real sem atrasar o inicio
        def _count_worker():
            for idx, it in enumerate(job.items):
                if job.cancel_event.is_set():
                    return
                if idx in job._already_done:
                    continue
                with job.lock:
                    known = page_counts[idx]
                if known is not None:
                    continue
                n = _count(it)
                with job.lock:
                    if page_counts[idx] is None:
                        page_counts[idx] = n
                        job.pages_total += n or 0
                time.sleep(0.15)
        pending = len(job.items) - len(job._already_done)
        if pending:
            threading.Thread(target=_count_worker, daemon=True,
                             name="page-count").start()
            job.push_message("Contando paginas de %d item(ns)..." % pending)
        else:
            job.push_message("Páginas estimadas: 0 (nada a baixar).")

        try:
            for idx, item in enumerate(job.items):
                if job.cancel_event.is_set():
                    break
                label = _fmt_item(item)
                if idx in getattr(job, "_already_done", []):
                    job.push_message(f"[{idx + 1}/{len(job.items)}] {label} — já baixado (CBZ existente), pulando.")
                    with job.lock:
                        job.items_done += 1
                        job.phase = ""
                    continue
                if job.force:
                    # forca re-download: remove o arquivo final e o registro
                    pref = _item_prefix(item, job.site_folder)
                    root = Path(dest) / job.site_folder
                    for ext in (".cbz", ".pdf", ".epub"):
                        for p in (root.rglob(pref + "*" + ext)
                                  if root.is_dir() else []):
                            try:
                                p.unlink()
                            except OSError:
                                pass
                    if job.output_format == "images":
                        for p in (list(root.rglob(pref)) if root.is_dir() else []):
                            if p.is_dir():
                                shutil.rmtree(p, ignore_errors=True)
                    _manifest_drop(dest, job.site_folder, item, job.title)
                    _strip_item_from_archive(dest, item, job.site_folder)
                if page_counts[idx] is None:
                    n = _count(item)
                    with job.lock:
                        if page_counts[idx] is None:
                            page_counts[idx] = n
                            job.pages_total += n or 0
                if job.site == "mangafire":
                    url = mangafire.item_url(job.url, item)
                else:
                    url = item.get("url") or ""
                with job.lock:
                    job.item_index = idx
                    job.item_label = label
                    job.item_pages_total = page_counts[idx] or 0
                    job.item_pages_done = 0
                    job.item_started = time.time()
                    job.phase = "download"
                    job.phase_done = 0
                    job.phase_total = page_counts[idx] or 0
                job.push_message(f"[{idx + 1}/{len(job.items)}] {label} — baixando...")

                manga_root = os.path.join(dest, job.site_folder)
                item_start = time.time() - 2
                _cd = getattr(adapter, "custom_download", False)
                if callable(_cd):
                    _cd = _cd()
                is_custom = bool(adapter is not None and _cd)

                if is_custom:
                    # download direto (bacakomik e outros sem extractor gallery-dl)
                    try:
                        itemdir = _download_direct(job, item, url, manga_root, label)
                        if itemdir is None:
                            raise RuntimeError("sem pasta")
                    except JobCancel:
                        _remove_empty_dirs(manga_root)
                        job.push_message(f"{label}: cancelado.")
                        with job.lock:
                            job.status = "canceled"
                            job.items_done = len(job.items)
                            job.item_index = len(job.items)
                        break
                    except Exception as exc:
                        _log.exception("download failed %s", url)
                        job.push_message(f"{label}: ERRO no download — {exc}")
                        with job.lock:
                            job.items_failed += 1
                            job.items_done += 1
                        continue
                else:
                    try:
                        itemdir = _gd_download(url, label, item, item_start)
                        if itemdir is None and not job._archive_purged:
                            # O controle do gallery-dl dizia "ja baixado", mas
                            # nao existe nada no disco (arquivo apagado/movido).
                            # Limpa o registro e tenta de novo: auto-correcao
                            # em vez do aviso "rebaixe manualmente".
                            job._archive_purged = True
                            job.push_message(
                                f"{label}: controle antigo sem arquivos — rebaixando.")
                            try:
                                os.remove(archive)
                            except OSError:
                                pass
                            itemdir = _gd_download(url, label, item, item_start)
                    except JobCancel:
                        _remove_empty_dirs(manga_root)
                        job.push_message(f"{label}: cancelado.")
                        with job.lock:
                            job.status = "canceled"
                            job.items_done = len(job.items)
                            job.item_index = len(job.items)
                        break
                    except Exception as exc:
                        _log.exception("download failed %s", url)
                        job.push_message(f"{label}: ERRO no download — {exc}")
                        with job.lock:
                            job.items_failed += 1
                            job.items_done += 1
                        continue
                if itemdir is None:
                    _remove_empty_dirs(manga_root)
                    job.push_message(
                        f"{label}: AVISO - nao consegui localizar as paginas baixadas; rode de novo.")
                    with job.lock:
                        job.items_failed += 1
                        job.items_done += 1
                        job.phase = ""
                    continue

                # ---- melhoria de imagem (opcional) ----
                if job.resolution != "original" and not job.cancel_event.is_set():
                    try:
                        images = formats.image_names(itemdir)
                    except Exception as exc:
                        _log.exception("image list failed %s", itemdir)
                        job.push_message(f"{label}: ERRO listando as páginas — {exc}")
                        with job.lock:
                            job.items_failed += 1
                            job.items_done += 1
                        continue
                    with job.lock:
                        job.phase = "upscale"
                        job.phase_done = 0
                        job.phase_total = len(images)
                    job.push_message(f"{label}: melhorando imagens ({job.resolution}, {len(images)} pág.)...")
                    try:
                        upscale.upscale_folder(
                            itemdir, job.resolution, dest,
                            progress=self._phase_progress(job),
                            cancel_event=job.cancel_event)
                        job.push_message(f"{label}: imagens melhoradas.")
                    except upscale.UpscaleCancel:
                        job.push_message(f"{label}: melhoria cancelada (imagens mantidas em {itemdir}).")
                        with job.lock:
                            job.status = "canceled"
                            job.item_index = len(job.items)
                        break
                    except Exception as exc:
                        _log.exception("upscale failed %s", itemdir)
                        job.push_message(f"{label}: melhoria FALHOU — {exc}")

                if job.cancel_event.is_set():
                    job.push_message(f"{label}: cancelamento após download (imagens mantidas).")
                    with job.lock:
                        job.status = "canceled"
                        job.item_index = len(job.items)
                    break

                # ---- montar o formato final ----
                if job.output_format == "images":
                    job.push_message(f"{label}: imagens em {itemdir}")
                    _manifest_put(dest, job.site_folder, item, itemdir,
                                  job.title)
                else:
                    try:
                        images = formats.image_names(itemdir)
                    except Exception as exc:
                        _log.exception("image list failed %s", itemdir)
                        job.push_message(f"{label}: ERRO listando as páginas — {exc}")
                        with job.lock:
                            job.items_failed += 1
                        continue
                    out_path = Path(str(itemdir) + "." + job.output_format)
                    old_archives = [Path(str(itemdir) + e) for e in
                                    (".cbz", ".pdf", ".epub")]
                    old_archives = [p for p in old_archives if p.is_file()]
                    _remove_intermediate(out_path, old_archives)
                    with job.lock:
                        job.phase = "convert"
                        job.phase_done = 0
                        job.phase_total = len(images)
                    job.push_message(f"{label}: montando {out_path.name}...")
                    try:
                        self._build(job, itemdir, out_path, old_archives,
                                    label, title=item.get("name") or itemdir.name)
                        if not job.keep_images:
                            shutil.rmtree(itemdir, ignore_errors=True)
                        job.push_message(f"{label}: {out_path.name} pronto.")
                        _manifest_put(dest, job.site_folder, item, out_path,
                                      job.title)
                    except Exception as exc:
                        _log.exception("build failed %s", itemdir)
                        job.push_message(f"{label}: FALHA ao montar o arquivo — {exc}"
                                         f" (imagens mantidas em {itemdir})")
                        with job.lock:
                            job.items_failed += 1

                _remove_empty_dirs(manga_root)
                with job.lock:
                    job.items_done += 1
                    job.phase = ""
        finally:
            root_logger.removeHandler(log_handler)
            with job.lock:
                if job.status == "running":
                    job.status = "canceled" if job.cancel_event.is_set() else "done"
                job.finished = time.time()
                job.item_label = ""
                job.item_index = len(job.items)
                job.phase = ""
        done_new = job.items_done - len(getattr(job, "_already_done", []))
        total_items = len(job.items)
        if job.status != "canceled" and job.items_failed >= total_items \
                and total_items > 0:
            # nada saiu: mostra como FALHA, nao como "concluido"
            with job.lock:
                job.status = "error"
                if not job.error:
                    job.error = ("%d de %d item(ns) falharam — veja os detalhes."
                                 % (job.items_failed, total_items))
            job.push_message("Falha: nenhum item foi baixado (%d com erro — veja o log)."
                             % job.items_failed)
        elif done_new <= 0 and not job.items_failed:
            job.push_message("Nada a fazer: todos os itens ja estavam baixados." + _failure_suffix(job))
        else:
            job.push_message("Concluido: %d item(ns) processado(s).%s" % (done_new, _failure_suffix(job)))

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _phase_progress(job):
        def _cb(done, total, _phase):
            with job.lock:
                job.phase_done = done
                job.phase_total = total
        return _cb

    def _build(self, job, itemdir, out_path, old_archives, label, title):
        progress = self._phase_progress(job)
        fmt = job.output_format
        if fmt == "cbz":
            formats.build_cbz(itemdir, str(out_path), old_archives, progress)
        elif fmt == "pdf":
            formats.build_pdf(itemdir, str(out_path), old_archives, progress)
        elif fmt == "epub":
            formats.build_epub(itemdir, str(out_path), old_archives, title, progress)


def _failure_suffix(job):
    if job.items_failed:
        return f" ({job.items_failed} item(ns) com erro — veja o log)."
    return ""


def _remove_intermediate(out_path, old_archives):
    """Remove temporários .building órfãos de uma tentativa anterior."""
    tmp = Path(str(out_path) + ".building")
    if tmp.is_file():
        try:
            tmp.unlink()
        except OSError:
            pass


manager = JobManager()
