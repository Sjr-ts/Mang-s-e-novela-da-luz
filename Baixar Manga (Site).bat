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
rem --- se ja esta rodando, so abre o navegador ---
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/ping' -TimeoutSec 2 -UseBasicParsing; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %errorlevel%==0 (
  start "" "http://127.0.0.1:8765/"
  exit /b 0
)
rem --- inicia o servidor em segundo plano, sem janela preta ---
start "" /min pythonw "Site\app.py"
rem --- espera a porta responder (max 15s) e abre o navegador ---
powershell -NoProfile -Command "$ok=$false; for ($i=0; $i -lt 30; $i++) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/ping' -TimeoutSec 1 -UseBasicParsing; if ($r.StatusCode -eq 200) { $ok=$true; break } } catch { Start-Sleep -Milliseconds 500 } }; if (-not $ok) { exit 1 }; Start-Process 'http://127.0.0.1:8765/'"
if %errorlevel% neq 0 (
  echo.
  echo O site NAO conseguiu iniciar. Veja o log em Site\logs\baixador.log
  echo (ou rode: python Site\app.py  para ver o erro diretamente)
  pause
)
exit /b 0
