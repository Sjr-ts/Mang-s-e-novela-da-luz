# -*- coding: utf-8 -*-
"""Job manager: download queue with phases (download → upscale → convert),
progress snapshots for the UI and cooperative cancellation."""

import logging
import os
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
    """gallery-dl output that just counts pages (skip or success)."""

    def __init__(self, job):
        self.job = job

    def start(self, path):
        pass

    def skip(self, path):
        self._tick()

    def success(self, path):
        self._tick()

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


def _item_prefix(item):
    if item.get("prefix"):
        return item["prefix"]
    if item.get("kind") == "volume":
        try:
            return f"v{int(item.get('number') or 0):02d}"
        except (TypeError, ValueError):
            return "v"
    n = str(item.get("number") if item.get("number") is not None else "0")
    i, dot, f = n.partition(".")
    try:
        i = int(float(i))
    except ValueError:
        i = 0
    return f"c{i:03d}{dot + f if dot else ''}"


def _download_direct(job, item, url, manga_root, label):
    """Download de imagens direto (sites sem extractor gallery-dl, ex. bacakomik)."""
    adapter = sites_module.get(job.site)
    imgs = adapter.reader_images(url)
    if not imgs:
        raise RuntimeError("nenhuma imagem encontrada")
    title_dir = Path(manga_root) / _safe_dirname(job.title)
    prefix = _item_prefix(item)
    itemdir = title_dir / prefix
    itemdir.mkdir(parents=True, exist_ok=True)

    item_start = time.time()
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
        try:
            r = http().get(img, timeout=30, stream=True) if False else None
        except Exception:
            r = None
        import requests as _rq
        resp = _rq.get(img, headers={"User-Agent": UA}, timeout=30, stream=True)
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)
        with job.lock:
            job.item_pages_done += 1
            job.pages_done += 1
    return itemdir


def _existing_output(dest, item, site_folder="mangafire"):
    """True if a final archive for this item exists (chapter/volume prefix)."""
    root = Path(dest) / site_folder
    if not root.is_dir():
        return False
    prefix = _item_prefix(item)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in filenames:
            lower = name.lower()
            if (lower.endswith((".cbz", ".pdf", ".epub"))
                    and os.path.basename(name).startswith(prefix)):
                return True
    return False


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
        keys.append(str(item["number"]))
    if not keys:
        return 0
    try:
        import sqlite3
        total = 0
        with sqlite3.connect(str(archive), timeout=60) as conn:
            for key in keys:
                cur = conn.execute(
                    "DELETE FROM archive WHERE entry LIKE ? OR entry LIKE ?",
                    (f"%_{key}_%", f"{key}_%"))
                total += cur.rowcount
        return total
    except Exception as exc:
        _log.warning("nao consegui ajustar o archive %s: %s", archive, exc)
        return 0


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
                if j.status == "queued":
                    return j
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

        BUDGET = 10
        total = 0
        page_counts = []
        for idx, it in enumerate(job.items):
            if idx < BUDGET:
                n = _count(it)
            else:
                n = None
            page_counts.append(n)
            total += n or 0
        with job.lock:
            job.pages_total = total
        job.push_message("Páginas estimadas: %d (contagem completa sob demanda)." % total)

        try:
            for idx, item in enumerate(job.items):
                if job.cancel_event.is_set():
                    break
                label = _fmt_item(item)
                if page_counts[idx] is None:
                    n = _count(item)
                    page_counts[idx] = n
                    with job.lock:
                        job.pages_total += n
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
                is_custom = bool(adapter is not None and
                                 getattr(adapter, "custom_download", lambda: False)())

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
                        dj = gd_job.DownloadJob(url)
                        dj.out = CountingOutput(job)
                        status = dj.run()
                        if status:
                            job.push_message(f"{label}: concluído com avisos (código {status}).")
                        else:
                            job.push_message(f"{label}: download ok.")
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

                    try:
                        itemdir = formats.find_item_folder(dest, item_start, job.site_folder)
                    except Exception as exc:
                        _log.exception("item folder detection failed %s", url)
                        job.push_message(f"{label}: ERRO localizando a pasta — {exc}")
                        with job.lock:
                            job.items_failed += 1
                            job.items_done += 1
                        continue
                    if itemdir is None:
                        itemdir = formats.find_dir_for_item(dest, item, job.title, job.site_folder)
                        if itemdir is not None:
                            job.push_message(
                                f"{label}: páginas já baixadas — montando o arquivo a partir delas.")
                if itemdir is None:
                    _remove_empty_dirs(manga_root)
                    job.push_message(f"{label}: já estava tudo baixado.")
                    with job.lock:
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
        job.push_message(f"Concluído.{_failure_suffix(job)}")

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
