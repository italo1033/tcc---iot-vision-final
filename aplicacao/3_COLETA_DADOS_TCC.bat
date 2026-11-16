@echo off
title IoT Vision - 3 COLETA DE DADOS DO TCC
call "%~dp0_preparar_ambiente.bat" || exit /b 1
"%PY%" coletar_dados_tcc.py
