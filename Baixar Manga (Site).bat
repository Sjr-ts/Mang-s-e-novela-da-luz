@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Baixador de Manga (Site)
cd /d "%~dp0"

if not exist "Site\app.py" (
  echo.
  echo  ERRO: Site\app.py nao encontrado nesta pasta.
  echo  Certifique-se de extrair a pasta completa do projeto.
  pause
  exit /b 1
)

rem ============================================================
rem 1) Se o servidor ja estiver rodando, apenas abre o navegador
rem ============================================================
echo Verificando se o Baixador ja esta aberto...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/ping' -TimeoutSec 2 -UseBasicParsing; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %errorlevel%==0 (
  echo  Servidor ja esta rodando. Abrindo o navegador...
  start "" "http://127.0.0.1:8765/"
  exit /b 0
)

rem ============================================================
rem 2) Escolhe o Python: integrado -> sistema -> instala
rem ============================================================
set "PYEXE="
set "HAS_RUNTIME=0"
if exist "runtime\python\python.exe" set "HAS_RUNTIME=1"

if "!HAS_RUNTIME!"=="1" (
  echo Verificando Python integrado...
  "runtime\python\python.exe" -c "import flask, gallery_dl, PIL, requests, certifi" >nul 2>&1
  if !errorlevel! == 0 (
    set "PYEXE=runtime\python\python.exe"
  ) else (
    echo  Python integrado existe, mas faltam dependencias. Reinstalando...
    goto :DO_INSTALL
  )
) else (
  echo Python integrado nao encontrado.
  goto :DO_INSTALL
)

if defined PYEXE goto :HAVE_PYTHON

:DO_INSTALL
if not exist "Instalar Python Integrado.ps1" (
  echo.
  echo  ERRO: Instalar Python Integrado.ps1 nao encontrado.
  echo  Baixe o projeto completo novamente.
  pause
  exit /b 1
)
echo.
echo  Instalando Python integrado (pode levar alguns minutos)...
echo  (Download oficial de python.org, verificado por SHA256)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "Instalar Python Integrado.ps1"
if not exist "runtime\python\python.exe" (
  echo.
  echo  ERRO: a instalacao do Python integrado falhou.
  echo  Veja as mensagens acima. Tente rodar manualmente:
  echo     powershell -ExecutionPolicy Bypass -File "Instalar Python Integrado.ps1"
  pause
  exit /b 1
)
"runtime\python\python.exe" -c "import flask, gallery_dl, PIL, requests, certifi" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  ERRO: dependencias nao foram instaladas corretamente.
  pause
  exit /b 1
)
set "PYEXE=runtime\python\python.exe"
echo.
echo  Python integrado instalado com sucesso!

:TRY_SYSTEM_PYTHON
if defined PYEXE goto :HAVE_PYTHON
where python >nul 2>&1
if errorlevel 1 goto :NO_PYTHON
python -c "import flask, gallery_dl, PIL, requests, certifi" >nul 2>&1
if errorlevel 1 goto :NO_PYTHON
set "PYEXE=python"
goto :HAVE_PYTHON

:NO_PYTHON
echo.
echo  ERRO: nao foi possivel obter um Python funcional.
echo  Instale o Python manualmente ou rode Instalar Python Integrado.ps1
pause
exit /b 1

:HAVE_PYTHON
echo.
echo Iniciando o servidor...
set "PYSRV=%PYEXE%"
if exist "runtime\python\pythonw.exe" set "PYSRV=runtime\python\pythonw.exe"
start "" /min "%PYSRV%" "Site\app.py"

echo Aguardando o site ficar pronto (max 30s)...
set "WAITED=0"
:WAIT_LOOP
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/ping' -TimeoutSec 1 -UseBasicParsing; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %errorlevel%==0 goto :OPENED
set /a WAITED+=1
if !WAITED! geq 60 goto :TIMEOUT
timeout /t 1 /nobreak >nul
goto :WAIT_LOOP

:OPENED
start "" "http://127.0.0.1:8765/"
echo.
echo  Baixador iniciado. O navegador foi aberto.
echo  (Para desligar, use o botao "Desligar" na pagina.)
exit /b 0

:TIMEOUT
echo.
echo  O site NAO conseguiu iniciar. Veja o log em Site\logs\baixador.log
echo  (ou rode: %PYEXE% Site\app.py  para ver o erro)
pause
exit /b 1
