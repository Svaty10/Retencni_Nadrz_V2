@echo off
rem Dvojklik = smaze parovani v cidle (klic v EEPROM); firmware zustava. Navod: docs\programovani_uzlu.md
rem Dalsi volby lze pripsat za nazev souboru, napr. -Port COM5
chcp 65001 >nul
title Smazat parovani cidla
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nahraj.ps1" -SmazatParovani %*
echo.
pause
