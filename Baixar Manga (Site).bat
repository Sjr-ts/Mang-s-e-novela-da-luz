@echo off
setlocal
chcp 65001 >nul 2>&1
title Baixador de Manga (Site)
cd /d "%~dp0"
if not exist "Site\app.py" (
  echo ERRO: Site\app.py nao encontrado nesta pasta.
  pause
  exit /b 1
)
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/ping' -TimeoutSec 2 -UseBasicParsing; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %errorlevel%==0 (
  echo O servidor ja esta rodando - abrindo o navegador...
  start "" "http://127.0.0.1:8765/"
  exit /b 0
)
echo.
echo   Baixador de Manga - servidor local
echo   Endereco: http://127.0.0.1:8765
echo   (feche esta janela para desligar o programa)
echo.
python "Site\app.py"
echo.
echo O servidor parou.
pause
