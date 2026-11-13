<#
.SYNOPSIS
    Baixador de Manga - baixa mangas do MangaFire (pt-br por padrao),
    um capitulo por vez, ja no formato CBZ.

.DESCRIPTION
    Usa a ferramenta gallery-dl (ja instalada) apenas para LER as paginas
    e as APIs publicas do MangaFire e salvar as imagens no seu PC.
    Cada capitulo baixado vira um arquivo .cbz (um ZIP so com as imagens),
    que Kindle, Tachiyomi, Komga e leitores de manga abrem direto.

    Sem argumentos, o programa faz algumas perguntas (modo facil).
    Com argumentos, roda direto (modo avancado).

.PARAMETER Link
    Endereco do manga no MangaFire.
    Exemplo: https://mangafire.to/title/3kr-rave-master

.PARAMETER De
    Capitulo inicial. 0 = desde o primeiro. Exemplo: -De 1

.PARAMETER Ate
    Capitulo final. 0 = ate o ultimo. Exemplo: -Ate 100

.PARAMETER Lang
    Idioma dos capitulos. Padrao: pt-br (outros: en, es, ...)

.PARAMETER Destino
    Pasta onde salvar. Padrao: (pasta do programa)\Mangas

.PARAMETER ManterImagens
    Nao apaga os JPGs depois de criar o CBZ.

.PARAMETER Simular
    Nao baixa nada; apenas mostra o que seria feito.

.PARAMETER SoVolumes
    Baixa VOLUMES (compilados) em vez de capitulos avulsos.
    Lista os volumes do idioma em -Lang direto na API do MangaFire e
    baixa cada um como um CBZ (ex.: v01.cbz).
    Com -De/-Ate, limita aos numeros de volume (ex.: -De 1 -Ate 3).

.EXAMPLE
    .\Baixador de Manga.ps1 -Link "https://mangafire.to/title/053-fairy-taill" -SoVolumes

.EXAMPLE
    .\Baixador de Manga.ps1 -Link "https://mangafire.to/title/053-fairy-taill" -SoVolumes -De 1 -Ate 10

.EXAMPLE
    .\Baixador de Manga.ps1 -Link "https://mangafire.to/title/3kr-rave-master"

.EXAMPLE
    .\Baixador de Manga.ps1 -Link "https://mangafire.to/title/3kr-rave-master" -De 1 -Ate 100

.EXAMPLE
    .\Baixador de Manga.ps1 -Link "https://mangafire.to/title/3kr-rave-master" -De 5 -Ate 5

.EXAMPLE
    .\Baixador de Manga.ps1 -Link "https://mangafire.to/title/3kr-rave-master" -Lang en -Destino "D:\Mangas"
#>
[CmdletBinding()]
param(
    [string]$Link,
    [int]$De = 0,
    [int]$Ate = 0,
    [string]$Lang = 'pt-br',
    [string]$Destino,
    [switch]$ManterImagens,
    [switch]$Simular,
    [switch]$SoVolumes
)

# o gallery-dl e o python escrevem avisos no stderr; nao deixar o PowerShell tratar como erro
$ErrorActionPreference = 'Continue'
$env:PYTHONWARNINGS    = 'ignore:urllib3'
$ToolDir    = $PSScriptRoot
$ConfigFile = Join-Path $ToolDir 'mangafire.json'
$VolumeList = Join-Path $ToolDir 'listar-volumes.py'

function Write-Step   ($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok     ($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Aviso  ($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Erro   ($msg) { Write-Host "[X] $msg" -ForegroundColor Red }

function Convert-Faixa([string]$texto) {
    $t = $texto.Trim().ToLowerInvariant()
    if ($t -eq '' -or $t -in @('todos', 'tudo', 'all', '*')) { return @(0, 0) }
    $t = $t -replace 'até', '-' -replace 'ate', '-' -replace ' a ', '-'
    if ($t -match '^(\d+)$') { return @([int]$Matches[1], [int]$Matches[1]) }
    if ($t -match '^(\d*)\s*-\s*(\d*)$') {
        $a = if ($Matches[1]) { [int]$Matches[1] } else { 0 }
        $b = if ($Matches[2]) { [int]$Matches[2] } else { 0 }
        if ($a -gt 0 -and $b -gt 0 -and $a -gt $b) {
            throw "O capitulo inicial ($a) nao pode ser maior que o final ($b)."
        }
        return @($a, $b)
    }
    throw "Formato invalido: '$texto'. Use algo como 1-100, 50-, -100, 27 ou Enter para todos."
}

Write-Host ''
Write-Host '  BAIXADOR DE MANGA  (MangaFire - pt-br - CBZ)' -ForegroundColor Magenta
Write-Host ''

# --- localizar o Python (runtime integrado tem prioridade) ---------------
function Get-ProjectPython {
    $toolDir = $ToolDir
    $candidates = @()
    $rt = Join-Path $toolDir 'runtime\python\python.exe'
    if (Test-Path $rt) { $candidates += $rt }
    $sysPy = Get-Command python -ErrorAction SilentlyContinue
    if ($sysPy) { $candidates += $sysPy.Source }
    $sysPy3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($sysPy3) { $candidates += $sysPy3.Source }
    foreach ($c in $candidates) {
        & $c -c "import gallery_dl, flask" *> $null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

# --- localizar o gallery-dl (ou o modulo Python) -------------------------
$runner     = $null
$runnerArgs = @()
$gd = Get-Command gallery-dl -ErrorAction SilentlyContinue
if ($gd) {
    $runner = $gd.Source
} else {
    $py = Get-ProjectPython
    if ($py) {
        & $py -m gallery_dl --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $runner     = $py
            $runnerArgs = @('-m', 'gallery_dl')
        }
    }
    if (-not $runner) {
        # tenta instalar o python integrado
        $inst = Join-Path $ToolDir 'Instalar Python Integrado.ps1'
        if (Test-Path $inst) {
            Write-Host 'Python/gallery-dl nao encontrados; instalando Python integrado...' -ForegroundColor Yellow
            & powershell -NoProfile -ExecutionPolicy Bypass -File $inst
            $py = Get-ProjectPython
            if ($py) {
                & $py -m gallery_dl --version *> $null
                if ($LASTEXITCODE -eq 0) {
                    $runner     = $py
                    $runnerArgs = @('-m', 'gallery_dl')
                }
            }
        }
    }
    if (-not $runner) {
        Write-Erro 'gallery-dl nao encontrado.'
        Write-Host 'Instale com:  pip install gallery-dl  (ou rode Instalar Python Integrado.ps1)'
        exit 1
    }
}
if (-not (Test-Path $ConfigFile)) {
    Write-Erro "Arquivo mangafire.json nao encontrado em: $ToolDir"
    exit 1
}

# --- modo interativo (sem argumentos) ------------------------------------
$Interactive = -not $Link
if ($Interactive) {
    $Link = Read-Host 'Cole o link do manga (ex.: https://mangafire.to/title/3kr-rave-master)'
    $volIn = Read-Host 'Baixar VOLUMES (compilados) em vez de capitulos? (s/N)'
    if ($volIn -match '^\s*[sSyY]') { $SoVolumes = $true }
    $pergunta = if ($SoVolumes) {
        'Quais volumes? (ex.: 1-10  |  5-  |  -20  |  3  |  Enter = todos)'
    } else {
        'Quais capitulos? (ex.: 1-100  |  50-  |  -100  |  27  |  Enter = todos)'
    }
    $faixa = Read-Host $pergunta
    try {
        $par = Convert-Faixa $faixa
        $De  = $par[0]
        $Ate = $par[1]
    } catch {
        Write-Erro $_.Exception.Message
        exit 1
    }
    $langIn = Read-Host 'Idioma [pt-br]'
    if ($langIn.Trim()) { $Lang = $langIn.Trim() }
    $manter = Read-Host 'Manter as imagens alem do CBZ? (s/N)'
    if ($manter -match '^\s*[sSyY]') { $ManterImagens = $true }
}

# --- validacoes -----------------------------------------------------------
$Link = $Link.Trim()
if (-not $Link) { Write-Erro 'Link vazio.'; exit 1 }
if ($Link -notmatch '^https?://') { $Link = 'https://' + $Link }
if ($De -lt 0 -or $Ate -lt 0) { Write-Erro '-De e -Ate nao podem ser negativos.'; exit 1 }
if ($De -gt 0 -and $Ate -gt 0 -and $De -gt $Ate) {
    Write-Erro "-De ($De) nao pode ser maior que -Ate ($Ate)."
    exit 1
}

if (-not $Destino) { $Destino = Join-Path $ToolDir 'Mangas' }
New-Item -ItemType Directory -Force -Path $Destino | Out-Null
$ArchiveFile = Join-Path $Destino '.download-archive.txt'

# --- modo volumes: listar URLs dos volumes na API --------------------------
$listFile = $null
if ($SoVolumes) {
    if (-not (Test-Path $VolumeList)) {
        Write-Erro "Arquivo listar-volumes.py nao encontrado em: $ToolDir"
        exit 1
    }
    $pyExe = Get-ProjectPython
    if (-not $pyExe) {
        Write-Erro 'python nao encontrado (necessario para listar os volumes).'
        exit 1
    }
    $listFile = Join-Path $Destino '.volumes-urls.txt'
    Write-Step 'Consultando volumes na API do MangaFire...'
    & $pyExe $VolumeList '--url', $Link, '--lang', $Lang, '--de', "$De", '--ate', "$Ate", '--saida', $listFile
    $nvol = 0
    if (Test-Path $listFile) {
        $nvol = @(Get-Content $listFile | Where-Object { $_.Trim() }).Count
    }
    if ($nvol -eq 0) {
        Write-Erro "Nenhum volume '$Lang' encontrado para este manga (ou a faixa nao tem resultado)."
        Write-Host 'Dica: rode com -Simular ou confira o idioma no site (nem todo manga tem volumes em pt-br).'
        exit 1
    }
    Write-Host "  $nvol volume(s) na fila de download."
}

# --- montar o comando ------------------------------------------------------
$gdArgs = @($runnerArgs)
$gdArgs += '--config-json', $ConfigFile
$gdArgs += '-o', "extractor.mangafire.lang=$Lang"
$gdArgs += '--restrict-filenames', 'windows'
$gdArgs += '--chunk-size', '1M'
$gdArgs += '--download-archive', $ArchiveFile
$gdArgs += '--cbz'
if ($ManterImagens) { $gdArgs += '-O', 'keep-files=true' }
if ($SoVolumes) {
    # volumes ja filtrados por idioma/faixa em listar-volumes.py; --child-filter nao se aplica a URLs de volume
    $gdArgs += '-i', $listFile
} else {
    $filtro = @()
    if ($De  -gt 0) { $filtro += "chapter >= $De" }
    if ($Ate -gt 0) { $filtro += "chapter <= $Ate" }
    if ($filtro.Count -gt 0) { $gdArgs += '--child-filter', ($filtro -join ' and ') }
    $gdArgs += $Link
}
if ($Simular) { $gdArgs += '-s' }
$gdArgs += '-d', $Destino

# --- resumo do que sera feito -----------------------------------------------
$txtDe    = if ($De  -gt 0) { "$De" }  else { 'inicio' }
$txtAte   = if ($Ate -gt 0) { "$Ate" } else { 'fim' }
$txtTipo  = if ($SoVolumes) { 'Volumes' } else { 'Capitulos' }
Write-Step 'Iniciando...'
Write-Host "  Link:      $Link"
Write-Host "  ${txtTipo}:  $txtDe ate $txtAte"
Write-Host "  Idioma:    $Lang"
Write-Host "  Destino:   $Destino"
Write-Host ''

# --- executar -----------------------------------------------------------------
& $runner @gdArgs
$code = $LASTEXITCODE
Write-Host ''

if ($code -eq 0) {
    if ($Simular) {
        Write-Ok 'Simulacao concluida (nada foi baixado).'
        Write-Host "    Destino: $Destino"
    } else {
        Write-Ok 'Terminou! Os arquivos CBZ estao em:'
        Write-Host "    $Destino"
        $cbzs = Get-ChildItem -Path $Destino -Filter '*.cbz' -Recurse -ErrorAction SilentlyContinue
        if ($cbzs) {
            $totalMB = ($cbzs | Measure-Object -Property Length -Sum).Sum / 1MB
            Write-Host ("    {0} arquivo(s) CBZ  -  {1:N1} MB no total" -f $cbzs.Count, $totalMB)
        }
    }
} else {
    Write-Aviso "gallery-dl terminou com codigo $code (falha total ou parcial)."
    Write-Host 'Rode o mesmo comando de novo: as paginas ja baixadas sao puladas'
    Write-Host "(controle em: $ArchiveFile)"
}

if ($Interactive) {
    Write-Host ''
    Read-Host 'Pressione Enter para fechar'
}
exit $code
