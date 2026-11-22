@echo off
rem Dvojklik = vycte vysledek diagnostiky z cidla (EEPROM pres UPDI) a vypise, co zkontrolovat.
rem Navod: docs\diagnostika_uzlu.md. Dalsi volby: napr. -Port COM5
chcp 65001 >nul
title Vysledek diagnostiky
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -Cti %*
echo.
pause
