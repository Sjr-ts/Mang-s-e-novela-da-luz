@echo off
setlocal
chcp 65001 >nul 2>&1
title Atualizar GitHub - Baixador de Manga
cd /d "%~dp0"

echo.
echo   Enviando as alteracoes para o repositorio...
echo   (https://github.com/Sjr-ts/Mang-s-e-novela-da-luz)
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if %errorlevel% neq 0 (
  echo ERRO: esta pasta nao e um repositorio git.
  pause
  exit /b 1
)

git add -A
git diff --cached --quiet
if %errorlevel%==0 (
  echo Nenhuma alteracao para enviar.
  pause
  exit /b 0
)

set /p MSG=Descreva a alteracao (ou Enter para padrao): 
if "%MSG%"=="" set MSG=Atualizacao do Baixador de Manga
git commit -m "%MSG%"
echo.
git push origin main
echo.
if %errorlevel%==0 (
  echo Enviado com sucesso!
) else (
  echo Falha no envio. Verifique sua conexao e as credenciais do GitHub.
)
pause
