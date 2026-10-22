<#
.SYNOPSIS
  Přeloží firmware čidla a nahraje ho přes UPDI (SerialUPDI: USB-UART adaptér nebo Arduino).

.DESCRIPTION
  Nepotřebuje make. Nástroje hledá v PATH nebo ve složce -ToolsDir (výchozí C:\avr):
    avr-gcc  – https://github.com/ZakKemble/avr-gcc-build/releases (avr-gcc-*-x64-windows.zip)
    avrdude  – https://github.com/avrdudes/avrdude/releases (avrdude-v*-windows-x64.zip)
  Postup a zapojení: docs/programovani_uzlu.md

.EXAMPLE
  .\nahraj.ps1 -Port COM5 -Test     # jen ověří spojení s čipem (přečte signaturu)
  .\nahraj.ps1 -Port COM5 -Fuses    # nový čip: fuses + firmware
  .\nahraj.ps1 -Port COM5           # aktualizace firmwaru
  .\nahraj.ps1 -BuildOnly           # jen přeložit (build\firmware.hex)
#>
param(
    [string]$Port = "",
    [switch]$Fuses,
    [switch]$Test,
    [switch]$BuildOnly,
    [int]$Baud = 115200,
    [string]$Programmer = "serialupdi",
    [string]$ToolsDir = "C:\avr"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Tool([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $f = Get-ChildItem -Path $ToolsDir -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($f) { return $f.FullName }
    throw "Nenalezen $name – rozbalte ho do $ToolsDir (viz docs\programovani_uzlu.md)."
}

function Invoke-Avrdude([string[]]$extra) {
    if (-not $Port) { throw "Zadejte port, např. -Port COM5 (Správce zařízení → Porty (COM a LPT))." }
    $avrdude = Find-Tool "avrdude"
    $avrArgs = @("-c", $Programmer, "-p", "t1624", "-P", $Port, "-b", "$Baud") + $extra
    & $avrdude @avrArgs
    if ($LASTEXITCODE -ne 0) {
        throw "avrdude selhal – zkontrolujte zapojení (TX přes odpor, RX přímo, GND), port a napájení čidla."
    }
}

if ($Test) {
    Invoke-Avrdude @()
    Write-Host "Spojení OK (signatura ATtiny1624 = 1E 94 2A)." -ForegroundColor Green
    return
}

# ------------------------------------------------------------------ build
$gcc = Find-Tool "avr-gcc"
$bin = Split-Path $gcc
New-Item -ItemType Directory -Force build | Out-Null
$cflags = @("-mmcu=attiny1624", "-DF_CPU=3333333UL", "-Iinclude", "-Os", "-std=gnu11",
            "-Wall", "-Wextra", "-Werror", "-Wno-array-bounds",
            "-funsigned-char", "-funsigned-bitfields", "-fshort-enums",
            "-ffunction-sections", "-fdata-sections", "-flto")
$objs = @()
foreach ($s in @("main", "power", "adc", "sht40", "cc1101", "crypto", "proto", "link")) {
    $o = "build\$s.o"
    & $gcc @cflags -c "src\$s.c" -o $o
    if ($LASTEXITCODE -ne 0) { throw "Překlad src\$s.c selhal." }
    $objs += $o
}
& $gcc -mmcu=attiny1624 -Os -flto "-Wl,--gc-sections" @objs -o build\firmware.elf
if ($LASTEXITCODE -ne 0) { throw "Linkování selhalo." }
& (Join-Path $bin "avr-objcopy.exe") -O ihex -R .eeprom -R .fuse -R .lock -R .signature build\firmware.elf build\firmware.hex
if ($LASTEXITCODE -ne 0) { throw "Vytvoření HEX selhalo." }
& (Join-Path $bin "avr-size.exe") build\firmware.elf
Write-Host "Firmware: build\firmware.hex" -ForegroundColor Green
if ($BuildOnly) { return }

# ------------------------------------------------------------------ flash
# fuse1 BODCFG 0x04 = BOD 1,8 V v běhu, ve spánku vypnutý
# fuse2 OSCCFG 0x02 = interní 20 MHz (firmware si dělí na 3,33 MHz)
# fuse5 SYSCFG0 0xC5 = PA0 zůstává UPDI, EEPROM (klíče párování) přežije přehrání
$extra = @()
if ($Fuses) { $extra += @("-U", "fuse1:w:0x04:m", "-U", "fuse2:w:0x02:m", "-U", "fuse5:w:0xC5:m") }
$extra += @("-U", "flash:w:build\firmware.hex:i")
Invoke-Avrdude $extra
Write-Host "Hotovo. Čidlo se restartovalo a 10 minut nabízí párování (hub: Nastavení → Čidla)." -ForegroundColor Green
