# -*- coding: utf-8 -*-
"""Verificação e correção da ORDEM de leitura dentro de arquivos .cbz.

Celulares e leitores de manga ordenam por nome de arquivo: basta um nome
fora de sequência para quebrar a leitura. Este módulo:
  - checa se as imagens estão na ordem de leitura (ordem natural),
  - checa arquivos vazios / duplicados / entradas em subpasta,
  - corrige reescrevendo o CBZ com as entradas ordenadas (e sem pastas).
"""

import os
import re
import zipfile
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def natural_key(name):
    return [int(tok) if tok.isdigit() else tok.lower()
            for tok in re.split(r"(\d+)", str(name))]


def check_cbz(path):
    issues = []
    try:
        with zipfile.ZipFile(path, "r") as zin:
            infos = zin.infolist()
            names = [i.filename for i in infos]
    except Exception as exc:
        return {"file": str(path), "ok": False,
                "issues": [f"não é um CBZ válido: {exc}"]}

    if not names:
        issues.append("CBZ vazio")
        return {"file": str(path), "ok": False, "issues": issues}

    bad_dirs = [n for n in names if "/" in n or "\\" in n]
    if bad_dirs:
        issues.append(f"{len(bad_dirs)} imagem(ns) em subpasta(s)")

    dup = len(names) - len(set(names))
    if dup:
        issues.append(f"{dup} nome(s) duplicado(s)")

    non_img = [n for n in names
               if Path(n).suffix.lower() not in IMAGE_EXTS]
    if non_img:
        issues.append(f"{len(non_img)} arquivo(s) não são imagem")

    empty = sum(1 for i in infos if i.file_size == 0)
    if empty:
        issues.append(f"{empty} imagem(ns) vazia(s)")

    flat_names = [os.path.basename(n) for n in names]
    if flat_names != sorted(flat_names, key=natural_key):
        issues.append("páginas fora da ordem de leitura")

    return {"file": str(path), "ok": not issues, "issues": issues}


def fix_cbz(path):
    """Reescreve o CBZ achatando subpastas e ordenando naturalmente.

    Retorna True se corrigiu, False se já estava ok (ou não foi possível).
    """
    result = check_cbz(path)
    if result["ok"]:
        return False
    try:
        with zipfile.ZipFile(path, "r") as zin:
            entries = [(i.filename, zin.read(i)) for i in zin.infolist()]
    except Exception:
        return False

    # achata: só o nome do arquivo, descarta não-imagens
    images = [(os.path.basename(n), d) for n, d in entries
              if Path(n).suffix.lower() in IMAGE_EXTS]
    if not images:
        return False
    images.sort(key=lambda t: natural_key(t[0]))

    # remove duplicatas idênticas mantendo a primeira
    seen = set()
    dedup = []
    for name, data in images:
        key = (name, len(data))
        if key in seen:
            continue
        seen.add(key)
        dedup.append((name, data))

    tmp = str(path) + ".fixing"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zout:
        for name, data in dedup:
            zout.writestr(name, data)

    with zipfile.ZipFile(tmp, "r") as zin:
        if [i.filename for i in zin.infolist()] != [n for n, _ in dedup]:
            os.remove(tmp)
            return False

    os.replace(tmp, path)
    return True


def scan(root):
    root = Path(root)
    items = []
    if not root.is_dir():
        return {"root": str(root), "checked": 0, "ok": 0,
                "problems": [], "error": "pasta não encontrada"}
    for p in sorted(root.rglob("*.cbz"), key=natural_key):
        items.append(check_cbz(p))
    problems = [i for i in items if not i["ok"]]
    return {
        "root": str(root),
        "checked": len(items),
        "ok": len(items) - len(problems),
        "problems": problems,
    }


def fix_all(root):
    report = scan(root)
    fixed = 0
    failed = []
    for item in report["problems"]:
        try:
            if fix_cbz(item["file"]):
                fixed += 1
            else:
                failed.append({"file": item["file"], "error": "não foi possível corrigir"})
        except Exception as exc:
            failed.append({"file": item["file"], "error": str(exc)})
    return {"checked": report["checked"], "fixed": fixed, "failed": failed}
