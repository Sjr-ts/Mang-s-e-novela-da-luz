# Baixador de Manga — MangaFire → CBZ, PDF e EPUB (PT-BR)

Programa **local** para baixar capítulos e volumes de mangás do **MangaFire**,
com interface de site profissional, barra de progresso real e leitura garantida
mesmo no celular. Tudo no seu PC — nada é enviado para a nuvem.

## Recursos

- **Site local** (127.0.0.1) com interface escura profissional — abra com um duplo clique
- **Buscar qualquer mangá** do MangaFire pelo link (aceita também link de capítulo/volume)
- **Tela do mangá**: capa, autores, sinopse, gêneros — e contagem real de capítulos/volumes
- **Capítulos e volumes** com seleção individual, "todos", ou faixa (`de`–`até`)
- **Idiomas**: pt-br, en, es, es-la, fr (e qualquer idioma que exista no site)
- **Edição**: tudo disponível ou somente oficiais licenciadas
- **Formato de saída**: **CBZ** (ZIP — Kindle/Tachiyomi/Komga) · **PDF** · **EPUB** ·
  **imagens soltas**
- **Melhoria de imagem** opcional, sempre proporcional (sem esticar o desenho):
  - Lanczos 1.6× (≈1000 px) · Lanczos 2.0× · largura exata 1000 px
  - **Waifu2x** (binário oficial): modelo *arte* (anti-JPEG) e *cunet* (máximo)
- **Retomada automática**: parar no meio nunca perde o que já foi baixado
  (arquivo `.download-archive.txt`; as páginas já salvas não são baixadas de novo)
- **Barra de progresso de verdade** por fase: download · melhoria · conversão
  (páginas, itens, %, tempo restante e cancelamento seguro)
- **Verificador de ordem de leitura**: checa todo o `.cbz` da biblioteca e corrige
  automaticamente páginas fora de ordem (001 → 002 → … no celular)
- **Biblioteca** embutida: mostra tudo que está na pasta de downloads e abre no Explorer

## Requisitos

- Windows 10/11, Python 3.10+ e [gallery-dl](https://github.com/mikf/gallery-dl)
- Opcional para IA (Waifu2x): GPU com Vulkan (Intel/AMD/NVIDIA)

```powershell
pip install gallery-dl flask pillow
```

Para a melhoria com IA, baixe o pacote oficial do
[waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan/releases)
e extraia a pasta em `tools/waifu2x/` (veja `tools/README.txt` após instalar).
Sem ele, todas as funções exceto Waifu2x funcionam normalmente.

## Como usar

**Modo site (recomendado)** — duplo clique em **`Baixar Manga (Site).bat`**.
O navegador abre sozinho em `http://127.0.0.1:8765`:

1. Cole o link do mangá e clique **Buscar**
2. Selecione capítulos/volumes (todos, alguns ou uma faixa)
3. Escolha "Idioma", "Edição", "Resolução" e **Formato**
4. **Baixar** — acompanhe o progresso, cancele quando quiser
5. Aba **Biblioteca** para ver o que já foi baixado

**Modo simples** — `Baixar Manga.bat` → responde 3 perguntas no console.
**Modo linha de comando** — `.\Baixador de Manga.ps1 -Link "..." -De 1 -Ate 50`
(veja `LEIA-ME.txt` para todos os parâmetros)

## Qualidade das imagens — nota honesta

O MangaFire serve **uma única qualidade** por página (é a mesma que o site exibe).
Em português, todas são versões de scanlation (~625×904 px). A única edição com
resolução maior é a **oficial licenciada**, disponível em inglês
(*idioma = English · edição = somente oficiais*): páginas de ~1000×1600 px.
Nenhuma opção do programa consegue "criar" qualidade que a fonte não tem.

## Estrutura

```
Site/app.py          site local (Flask, 127.0.0.1:8765)
Site/mangafire.py    leitura da API do MangaFire (gallery-dl)
Site/jobs.py         fila, progresso e cancelamento
Site/formats.py      montagem de CBZ / PDF / EPUB (+ merge de retomada)
Site/upscale.py      melhoria de imagem (Lanczos / Waifu2x)
Site/verify.py       verificador/corretor da ordem de leitura (CBZ)
Site/static/         interface (HTML/CSS/JS)
Baixador de Manga.ps1 / Baixar Manga*.bat   atalhos e CLI
```

## Legal / uso

Ferramenta de **uso pessoal**. O conteúdo pertence aos respectivos detentores de
direitos. Não distribua os arquivos baixados. Não use para burlar paywalls de
serviços oficiais.

## Créditos

- [gallery-dl](https://github.com/mikf/gallery-dl) (GPL-2.0) — motor de download
- [waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan) (MIT) — melhoria com IA
- MangaFire — fonte dos metadados/imagens

## Licença

GPL-2.0-or-later (compatível com o gallery-dl).
