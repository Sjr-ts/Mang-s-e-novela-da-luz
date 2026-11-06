# -*- coding: utf-8 -*-
"""Image enhancement for downloaded page folders.

Two engines, all with per-page progress callbacks and cancellation:

  Lanczos (Pillow)   - original / s1.6 / s2.0 / width-1000
  Waifu2x (ncnn)     - external binary, models upconv_7_anime_style_art_rgb
                       (anti-JPEG, fast) and models-cunet (máximo, lento)

Waifu2x is only used when the binary is installed under tools/waifu2x
(or BAIXADOR_WAIFU2X points to the .exe). Results are cached in
<dest>/.upscaled.json so resumed downloads never re-upscale the same file.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from PIL import Image

_log = logging.getLogger("baixador.upscale")

MODES = ("original", "lanczos16", "lanczos2", "w1000", "waifu2x-art", "waifu2x-cunet")
LANCZOS = {"lanczos16": ("factor", 1.6), "lanczos2": ("factor", 2.0), "w1000": ("width", 1000)}

WAIFU2X_MODELS = {
    "waifu2x-art": "models-upconv_7_anime_style_art_rgb",
    "waifu2x-cunet": "models-cunet",
}

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WAIFU2X_DIR = REPO_DIR / "tools" / "waifu2x" / "waifu2x-ncnn-vulkan-20250915-windows"


class UpscaleCancel(Exception):
    pass


def waifu2x_exe():
    env = os.environ.get("BAIXADOR_WAIFU2X")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_WAIFU2X_DIR / "waifu2x-ncnn-vulkan.exe")
    for c in candidates:
        if c.is_file():
            return c
    return None


def ai_available():
    exe = waifu2x_exe()
    if exe is None:
        return {}
    base = exe.parent
    available = {}
    for mode, model in WAIFU2X_MODELS.items():
        if (base / model).is_dir():
            available[mode] = True
    return available


def _registry_path(dest):
    return Path(dest) / ".upscaled.json"


def _load_registry(dest):
    try:
        return json.loads(_registry_path(dest).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_registry(dest, registry):
    try:
        _registry_path(dest).write_text(json.dumps(registry), encoding="utf-8")
    except OSError:
        pass


def _file_key(path):
    st = os.stat(path)
    return f"{int(st.st_mtime)}:{st.st_size}"


def _natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _iter_images(folder):
    folder = Path(folder)
    return sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")),
        key=lambda p: _natural_key(p.name),
    )


def upscale_folder(folder, mode, dest_root, progress=None, cancel_event=None):
    """Upscale all images in `folder` in-place.

    progress(done, total, "upscale") called per page; cancel_event aborts early.
    Returns (processed, skipped_cached).
    """
    if mode == "original":
        return 0, 0
    if mode not in MODES:
        raise ValueError(f"Resolução desconhecida: {mode}")

    folder = Path(folder)
    images = _iter_images(folder)
    registry = _load_registry(dest_root)
    total = len(images)
    done = skipped = 0

    if mode in LANCZOS:
        _kind, _value = LANCZOS[mode]

        def _resize(img):
            w, h = img.size
            if _kind == "width":
                nw = int(_value)
                nh = max(1, round(h * nw / w))
            else:
                nw, nh = int(round(w * _value)), int(round(h * _value))
            return nw, nh

        fn = lambda p: _lanczos_image(p, _resize)
    else:
        exe = waifu2x_exe()
        if exe is None:
            raise RuntimeError(
                "Waifu2x não instalado (tools/waifu2x). "
                "Baixe o pacote oficial e extraia em tools/waifu2x.")
        models = exe.parent / WAIFU2X_MODELS[mode]
        if not models.is_dir():
            raise RuntimeError(f"Modelo não encontrado: {models}")
        fn = lambda path: _waifu2x_image(exe, models, path)

    for img in images:
        if cancel_event is not None and cancel_event.is_set():
            raise UpscaleCancel()
        key = f"{img}::{mode}"
        fprint = _file_key(img)
        if registry.get(key) == fprint:
            skipped += 1
            done += 1
            if progress:
                progress(done, total, "upscale")
            continue
        fn(img)
        registry[key] = _file_key(img)
        done += 1
        if progress:
            progress(done, total, "upscale")

    _save_registry(dest_root, registry)
    return done, skipped


def _lanczos_image(path, target_size):
    img = Image.open(path)
    img.load()
    w, h = img.size
    nw, nh = target_size(img)
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(path, "JPEG", quality=94, optimize=True)
    img.close()


def _waifu2x_image(exe, models, path):
    tmp = path.with_suffix(path.suffix + ".w2x.tmp.jpg")
    cmd = [str(exe), "-i", str(path), "-o", str(tmp),
           "-s", "2", "-n", "1", "-f", "jpg", "-m", str(models)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=600)
    if proc.returncode != 0 or not tmp.is_file():
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        out = proc.stdout.decode("utf-8", "replace")[-400:] if proc.stdout else ""
        raise RuntimeError(f"Waifu2x falhou em {path.name}: {out}")
    os.replace(tmp, path)
