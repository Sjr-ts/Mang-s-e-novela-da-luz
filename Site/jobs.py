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
import upscale

LOG_FILE = Path(__file__).resolve().parent / "logs" / "baixador.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_log = logging.getLogger("baixador")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
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
    """Remove empty directories under <dest>/mangafire (left by gallery-dl)."""
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


class Job:
    def __init__(self, payload):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()

        self.url = payload["url"]
        self.title = payload.get("title") or ""
        self.cover = payload.get("cover") or ""
        self.dest = payload["dest"]
        self.lang = payload.get("lang") or "pt-br"
        self.resolution = payload.get("resolution") or "original"
        self.output_format = payload.get("format") or "cbz"
        self.keep_images = bool(payload.get("keep_images"))
        self.official_only = bool(payload.get("official_only"))

        items = []
        for it in payload.get("items", []):
            items.append({
                "kind": it["kind"],
                "id": int(it["id"]),
                "number": it.get("number"),
                "name": it.get("name") or "",
                "official": bool(it.get("official", True)),
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
        archive = os.path.join(dest, ".download-archive.txt")
        os.makedirs(dest, exist_ok=True)

        gd_config.setg("base-directory", dest)
        gd_config.setg("path-restrict", "windows")
        gd_config.setg("archive", archive)
        gd_config.setg("postprocessors", [])  # montamos nós mesmos o arquivo final

        log_handler = JobLogHandler(job)
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        total = 0
        page_counts = []
        for it in job.items:
            try:
                n = mangafire.item_pages(job.url, it)
            except Exception as exc:
                _log.warning("pages count failed for %s: %s", it, exc)
                n = 0
            page_counts.append(n)
            total += n
        with job.lock:
            job.pages_total = total

        try:
            for idx, item in enumerate(job.items):
                if job.cancel_event.is_set():
                    break
                label = _fmt_item(item)
                url = mangafire.item_url(job.url, item)
                with job.lock:
                    job.item_index = idx
                    job.item_label = label
                    job.item_pages_total = page_counts[idx]
                    job.item_pages_done = 0
                    job.item_started = time.time()
                    job.phase = "download"
                    job.phase_done = 0
                    job.phase_total = page_counts[idx]
                job.push_message(f"[{idx + 1}/{len(job.items)}] {label} — baixando...")

                manga_root = os.path.join(dest, "mangafire")
                item_start = time.time() - 2
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
                    itemdir = formats.find_item_folder(dest, item_start)
                except Exception as exc:
                    _log.exception("item folder detection failed %s", url)
                    job.push_message(f"{label}: ERRO localizando a pasta — {exc}")
                    with job.lock:
                        job.items_failed += 1
                        job.items_done += 1
                    continue
                if itemdir is None:
                    # tudo já baixado, mas o arquivo final pode não existir ainda
                    itemdir = formats.find_dir_for_item(dest, item, job.title)
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
