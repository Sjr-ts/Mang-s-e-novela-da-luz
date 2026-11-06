# -*- coding: utf-8 -*-
"""Lista as URLs de volumes de um manga do MangaFire.

Uso:
    python listar-volumes.py --url URL_DO_MANGA [--lang pt-br]
                             [--de N] [--ate N] [--saida ARQUIVO]

Somente LEITURA: usa a API publica do MangaFire (a mesma do site).
Imprime no stdout as URLs dos volumes do idioma indicado, uma por linha,
da de numero menor para a maior. Se --saida for informado, grava as URLs
no arquivo em vez de imprimir.
"""

import argparse
import sys
import re


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="URL do manga, ex.: https://mangafire.to/title/053-fairy-taill")
    ap.add_argument("--lang", default="pt-br")
    ap.add_argument("--de", type=int, default=0, help="volume inicial (0 = desde o primeiro)")
    ap.add_argument("--ate", type=int, default=0, help="volume final (0 = ate o ultimo)")
    ap.add_argument("--saida", default="", help="arquivo onde gravar as URLs (padrao: stdout)")
    args = ap.parse_args()

    m = re.match(r"^(https?://[^/]+)(/title/[^/]+)", args.url.strip())
    if not m:
        sys.exit("ERRO: URL invalida. Use algo como https://mangafire.to/title/000-nome-do-manga")
    base = m.group(1) + m.group(2)

    from gallery_dl.extractor.mangafire import MangafireMangaExtractor

    ex = MangafireMangaExtractor.from_url(base)
    ex.initialize()
    manga_id = ex.groups[0]

    achados = []
    page = 1
    while True:
        data = ex.request_api(f"/titles/{manga_id}/volumes", {
            "language": args.lang,
            "limit": "200",
            "order": "desc",
            "page": str(page),
            "sort": "number",
        })
        itens = data.get("items", [])
        for it in itens:
            if it.get("language") != args.lang:
                continue
            numero = it.get("number", 0) or 0
            if args.de and numero < args.de:
                continue
            if args.ate and numero > args.ate:
                continue
            vid = it.get("id")
            if vid:
                achados.append((numero, int(vid)))
        has_next = bool((data.get("meta") or {}).get("hasNext"))
        if not has_next or not itens:
            break
        page += 1
        if page > 10:
            break

    achados.sort(key=lambda t: (t[0], t[1]))
    linhas = [f"{base}/volume/{vid}" for _, vid in achados]

    if args.saida:
        with open(args.saida, "w", encoding="utf-8") as fp:
            fp.write("\n".join(linhas) + ("\n" if linhas else ""))
        print(f"Volumes {args.lang}: " + (", ".join(str(n) for n, _ in achados) or "nenhum encontrado"))
    else:
        print("\n".join(linhas))


if __name__ == "__main__":
    main()
