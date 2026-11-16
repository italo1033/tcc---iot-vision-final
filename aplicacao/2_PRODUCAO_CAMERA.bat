@echo off
title IoT Vision - 2 PRODUCAO (camera ao vivo)
call "%~dp0_preparar_ambiente.bat" || exit /b 1
"%PY%" executar_producao.py
