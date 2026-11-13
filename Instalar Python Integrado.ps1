<#
.SYNOPSIS
    Instala um Python embeddable (oficial, python.org) dentro da pasta
    'runtime\' do projeto, para que o Baixador funcione em maquinas sem
    Python instalado.

.DESCRIPTION
    - Baixa python-3.13.13-embed-amd64.zip de python.org (HTTPS).
    - Verifica o SHA256 contra o valor oficial esperado.
    - Extrai em runtime\python\.
    - Habilita 'import site' (necessario para o site-packages do pip).
    - Gera um ca-bundle.pem a partir do armazenamento de certificados do
      Windows (ROOT/CA) — contorna MITM do ESET/antivirus.
    - Baixa get-pip.py de bootstrap.pypa.io e instala pip.
    - Instala as dependencias de requirements.txt.

    Nao usa executaveis de terceiros; tudo vem de python.org / pypa.io.
#>

param(
    [string]$ProjectDir = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = "Stop"

$pyVer       = "3.13.13"
$pyZip       = "python-$pyVer-embed-amd64.zip"
$pyUrl       = "https://www.python.org/ftp/python/$pyVer/$pyZip"
$expectedSha = "8766A8775746235E23CF5AEE5027AB1060BB981D93110577ADCF3508AA0CBD55"

$runtimeDir  = Join-Path $ProjectDir "runtime"
$pyDir       = Join-Path $runtimeDir "python"
$pyExe       = Join-Path $pyDir "python.exe"
$reqFile     = Join-Path $ProjectDir "requirements.txt"

if (Test-Path $pyExe) {
    # verifica se as dependencias estao OK
    & $pyExe -c "import flask, gallery_dl, PIL, requests, certifi" *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Python integrado ja esta instalado e funcional." -ForegroundColor Green
        exit 0
    }
    Write-Host "Python integrado existe mas faltam dependencias; reinstalando pip/deps..." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

$tmpZip = Join-Path $env:TEMP "bm-python-$pyVer.zip"
Write-Host "Baixando Python $pyVer (embeddable) de python.org..."
Invoke-WebRequest -Uri $pyUrl -OutFile $tmpZip -UseBasicParsing -TimeoutSec 300

$hash = (Get-FileHash $tmpZip -Algorithm SHA256).Hash
if ($hash -ne $expectedSha) {
    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
    throw "SHA256 do Python nao confere. Esperado: $expectedSha  Obtido: $hash"
}
Write-Host "SHA256 verificado." -ForegroundColor Green

if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
Write-Host "Extraindo..."
Expand-Archive -Path $tmpZip -DestinationPath $pyDir -Force
Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue

# habilita 'import site' no ._pth e adiciona a pasta Site/ ao sys.path
$pthFile = Join-Path $pyDir "python313._pth"
$pthLines = @("python313.zip", ".", "..\..\Site", "", "# Uncomment to run site.main() automatically", "import site")
Set-Content -Path $pthFile -Value $pthLines -Encoding ASCII

# gera ca-bundle.pem a partir do armazenamento do Windows (ROOT + CA)
$caBundle = Join-Path $runtimeDir "ca-bundle.pem"
$caPy = Join-Path $pyDir "_build_ca.py"
$caCode = @'
import ssl, os, base64
parts = []
seen = set()
for store in ("ROOT", "CA"):
    try:
        entries = ssl.enum_certificates(store)
    except Exception:
        continue
    for entry in entries:
        try:
            cert = entry[0]
            enc = entry[1]
        except Exception:
            continue
        if enc != "x509_asn":
            continue
        if isinstance(cert, str):
            cert = cert.encode()
        if cert in seen:
            continue
        seen.add(cert)
        b64 = base64.b64encode(cert).decode("ascii")
        pem = "-----BEGIN CERTIFICATE-----\n"
        for i in range(0, len(b64), 64):
            pem += b64[i:i+64] + "\n"
        pem += "-----END CERTIFICATE-----\n"
        parts.append(pem.encode("ascii"))
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ca-bundle.pem")
out = os.path.normpath(out)
with open(out, "wb") as fh:
    fh.write(b"".join(parts))
print("CA bundle:", out, len(parts), "certs")
'@
Set-Content -Path $caPy -Value $caCode -Encoding UTF8
Write-Host "Gerando ca-bundle.pem a partir dos certificados do Windows..."
& $pyExe $caPy
Remove-Item $caPy -Force -ErrorAction SilentlyContinue
if (-not (Test-Path $caBundle)) {
    throw "Falha ao gerar ca-bundle.pem"
}

# variaveis de ambiente para pip (contorna MITM do antivirus)
$env:SSL_CERT_FILE      = $caBundle
$env:REQUESTS_CA_BUNDLE = $caBundle
$env:PIP_CERT           = $caBundle
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

# baixa get-pip.py de bootstrap.pypa.io (oficial)
$getPip = Join-Path $pyDir "get-pip.py"
Write-Host "Baixando get-pip.py (bootstrap.pypa.io)..."
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing -TimeoutSec 120

Write-Host "Instalando pip..."
& $pyExe $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar pip (codigo $LASTEXITCODE)" }
Remove-Item $getPip -Force -ErrorAction SilentlyContinue

# instala dependencias
Write-Host "Instalando dependencias (Flask, gallery-dl, Pillow, ...)"
$target = Join-Path $pyDir "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $target | Out-Null
& $pyExe -m pip install --target $target --no-warn-script-location -r $reqFile
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar dependencias (codigo $LASTEXITCODE)" }

# verifica
& $pyExe -c "import flask, gallery_dl, PIL, requests, certifi" *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Dependencias instaladas mas import falhou."
}
Write-Host "Python integrado instalado com sucesso em: $pyDir" -ForegroundColor Green
exit 0
