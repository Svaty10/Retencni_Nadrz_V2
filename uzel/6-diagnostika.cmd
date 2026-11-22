@echo off
rem Dvojklik = nahraje diagnosticky firmware (radio, baterie, SHT40, sonda). Navod: docs\diagnostika_uzlu.md
rem Vysledek vycte 7-precist-diagnostiku.cmd. Zpet: 3-aktualizace-firmwaru.cmd (parovani zustane).
chcp 65001 >nul
title Diagnostika cidla
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -Diagnostika %*
echo.
pause
