@echo off
rem Dvojklik = spusti nahraj.ps1 (navod: docs\programovani_uzlu.md).
rem Dalsi volby lze pripsat za nazev souboru, napr. -Port COM5
chcp 65001 >nul
title Novy cip: nastaveni + firmware
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -Fuses %*
echo.
pause
