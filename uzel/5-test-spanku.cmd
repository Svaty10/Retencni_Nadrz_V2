@echo off
rem Dvojklik = nahraje testovaci firmware, ktery jen spi (mereni klidoveho proudu). Navod: docs\programovani_uzlu.md
rem Zpet na normalni firmware: 3-aktualizace-firmwaru.cmd. Dalsi volby: napr. -Port COM5
chcp 65001 >nul
title Test spanku: cidlo jen spi
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -TestSpanku %*
echo.
pause
