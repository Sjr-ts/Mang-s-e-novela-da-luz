# -*- coding: utf-8 -*-
"""Item finalization: build CBZ / PDF / EPUB from a page folder.

Pipeline per item (see jobs.py):
  1) gallery-dl downloads the pages into <dest>/mangafire/<manga>/<chapter>/
  2) optional resolution upgrade rewrites the images in-place
  3) build the requested archive (cbz | pdf | epub) — or keep the folder

If some pages were already in .download-archive.txt (resume after interruption),
those pages are NOT re-downloaded; they are recovered from the previous archive
(.cbz) of the same item and merged in natural order (001, 002, ...).
"""

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from PIL import Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def natural_key(name):
    return [int(tok) if tok.isdigit() else tok.lower()
            for tok in re.split(r"(\d+)", str(name))]


def image_names(path):
    return sorted(
        (p for p in Path(path).iterdir()
         if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda p: natural_key(p.name),
    )


def find_item_folder(dest, item_start, root_name="mangafire"):
    """Detect the item folder that was touched by the current download.

    gallery-dl recreates/fills the chapter folder on every run, so the folder
    containing pages and with mtime >= item_start belongs to this item.
    """
    root = Path(dest) / root_name
    if not root.is_dir():
        return None
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        p = Path(dirpath)
        images = [f for f in filenames
                  if Path(f).suffix.lower() in IMAGE_EXTS]
        if not images:
            continue
        try:
            # a pasta do capítulo fica exatamente em mangafire/<manga>/<capítulo>
            if len(p.parts) < len(root.parts) + 2:
                continue
            if os.path.getmtime(p) >= item_start - 1:
                candidates.append(p)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: len(p.parts))


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


def find_dir_for_item(dest, item, job_title="", root_name="mangafire"):
    """Locate the EXISTING page folder for an item whose pages are already on
    disk (needed when everything was archive-skipped and the final file is
    missing). Returns the folder, or None if ambiguous/not found."""
    prefix = _item_prefix(item)
    root = Path(dest) / root_name
    if not root.is_dir():
        return None
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        p = Path(dirpath)
        if not p.name.startswith(prefix):
            continue
        if any(Path(f).suffix.lower() in IMAGE_EXTS for f in filenames):
            candidates.append(p)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1 and job_title:
        # desempata pelo nome da pasta do mangá (mesma regra do gallery-dl)
        norm = job_title
        for bad, good in [("<", "_"), (">", "_"), (":", "_"), ('"', "_"),
                          ("/", "_"), ("\\", "_"), ("|", "_"), ("?", "_"),
                          ("*", "_"), ("  ", " "), (".", "_")]:
            norm = norm.replace(bad, good)
        candidates = [c for c in candidates
                      if c.parent.name.replace(".", "_") == norm] or list(candidates)
        if len(candidates) == 1:
            return candidates[0]
    return None


def collect_pages(folder, old_archives):
    """Yield (display_name, image_bytes) in natural read order.

    Pages in `folder` win over pages recovered from `old_archives`
    with the same name; recovered pages fill the gaps.
    """
    folder = Path(folder)
    folder_imgs = {p.name: p for p in image_names(folder)}
    recovered = {}

    for old in old_archives:
        try:
            with zipfile.ZipFile(old, "r") as zin:
                for info in zin.infolist():
                    name = os.path.basename(info.filename)
                    if name not in folder_imgs and name not in recovered:
                        if Path(name).suffix.lower() in IMAGE_EXTS:
                            recovered[name] = (info.filename, zin.read(info))
        except (zipfile.BadZipFile, OSError):
            continue

    merged = dict(recovered)
    for name, p in folder_imgs.items():
        merged[name] = (name, p.read_bytes())

    for name in sorted(merged, key=natural_key):
        yield merged[name]


def _old_archives(parent_dir, base_name):
    parent = Path(parent_dir)
    out = []
    if not parent.is_dir():
        return out
    for ext in (".cbz", ".pdf", ".epub"):
        candidate = parent / (base_name + ext)
        if candidate.is_file():
            out.append(candidate)
    return out


def _encode_jpeg(img, quality=95):
    img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _safe_replace(tmp_path, final_path):
    os.replace(tmp_path, final_path)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def build_cbz(folder, out_path, archives, progress=None):
    tmp = out_path + ".building"
    pages = list(collect_pages(folder, archives))
    total = len(pages)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zout:
        for i, (name, data) in enumerate(pages, 1):
            zout.writestr(name, data)
            if progress:
                progress(i, total, "cbz")
    _check_cbz(tmp, total)
    _safe_replace(tmp, out_path)
    return total


def build_pdf(folder, out_path, archives, progress=None):
    pages = list(collect_pages(folder, archives))
    if not pages:
        raise ValueError("Nenhuma página encontrada para montar o PDF.")
    tmp = out_path + ".building"
    images = []
    total = len(pages)
    try:
        for i, (_name, data) in enumerate(pages, 1):
            images.append(Image.open(io.BytesIO(data)))
            if progress:
                progress(i, total, "pdf")
        first, rest = images[0], images[1:]
        first.convert("RGB").save(
            tmp, "PDF", save_all=True, append_images=[im.convert("RGB") for im in rest],
            resolution=100.0, quality=95)
    finally:
        for im in images:
            im.close()
    _safe_replace(tmp, out_path)
    return total


def build_epub(folder, out_path, archives, title, progress=None):
    pages = list(collect_pages(folder, archives))
    if not pages:
        raise ValueError("Nenhuma página encontrada para montar o EPUB.")
    tmp = out_path + ".building"
    total = len(pages)
    lang = "pt-BR"
    base = (title or "manga").strip() or "manga"
    safe_title = base.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_id = "".join(c for c in base if c.isascii() and c.isalnum()) or "manga"

    manifest_items = []
    spine_items = []
    for i, (name, _data) in enumerate(pages, 1):
        safe_name = f"p{i:04d}{Path(name).suffix.lower()}"
        manifest_items.append((f"p{i:04d}", safe_name, _media_type(name)))
        spine_items.append(f"p{i:04d}")

    opf = [
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bid">{safe_id}</dc:identifier>\n'
        f'    <dc:title>{safe_title}</dc:title>\n'
        '    <dc:language>pt-BR</dc:language>\n'
        '    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>\n'
        f'    <meta name="generator" content="Baixador de Manga"/>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n',
    ]
    for item_id, href, mtype in manifest_items:
        opf.append(f'    <item id="{item_id}" href="images/{href}" media-type="{mtype}"/>\n')
    opf.append('  </manifest>\n  <spine>\n')
    for item_id, _href, _mtype in manifest_items:
        opf.append(f'    <itemref idref="{item_id}"/>\n')
    opf.append('  </spine>\n</package>\n')

    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="pt-BR">\n'
        '<head><meta charset="utf-8"/><title>' + safe_title + '</title></head>\n'
        '<body><nav epub:type="toc"><h1>' + safe_title + '</h1><ol>'
    )
    for i, (name, _data) in enumerate(pages, 1):
        ext = Path(name).suffix.lower()
        nav += f'<li><a href="images/p{i:04d}{ext}">Página {i}</a></li>\n'
    nav += '</ol></nav></body></html>\n'

    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n'
    )

    with zipfile.ZipFile(tmp, "w") as zout:
        zout.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zout.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        zout.writestr("OEBPS/content.opf", "".join(opf), zipfile.ZIP_DEFLATED)
        zout.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        for i, (name, data) in enumerate(pages, 1):
            mtype = _media_type(name)
            ext = Path(name).suffix.lower()
            if mtype in ("image/jpeg",):
                zout.writestr(f"OEBPS/images/p{i:04d}{ext}", data, zipfile.ZIP_STORED)
            else:
                zout.writestr(f"OEBPS/images/p{i:04d}{ext}", data, zipfile.ZIP_DEFLATED)
            if progress:
                progress(i, total, "epub")
    _check_epub(tmp)
    _safe_replace(tmp, out_path)
    return total


def _media_type(name):
    ext = Path(name).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")


def _check_cbz(path, expected_pages):
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        if len(names) != expected_pages:
            raise ValueError(f"CBZ montado com {len(names)} páginas, esperava {expected_pages}")
        if any(info.file_size == 0 for info in zin.infolist()):
            raise ValueError("CBZ contém arquivo vazio")


def _check_epub(path):
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        if not names or names[0] != "mimetype":
            raise ValueError("EPUB inválido: 'mimetype' deve ser o primeiro item")
        if "OEBPS/content.opf" not in names or "META-INF/container.xml" not in names:
            raise ValueError("EPUB inválido: estrutura incompleta")
