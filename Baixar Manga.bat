@echo off
setlocal
chcp 65001 >nul 2>&1
title Baixador de Manga
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Baixador de Manga.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" (
  echo.
  pause
)
exit /b %RC%
