@echo off
title IoT Vision - 1 TESTE (video gravado)
call "%~dp0_preparar_ambiente.bat" || exit /b 1
"%PY%" executar_teste.py %*
