<#
.SYNOPSIS
  Nahraje firmware do čidla přes UPDI (Arduino Uno/Nano, USB-UART převodník nebo UPDI Friend).

.DESCRIPTION
  Nic nemusíte instalovat: skript si sám stáhne nástroj avrdude a hotový firmware
  z GitHubu (oboje s kontrolou SHA-256) a najde port programátoru.
  Zapojení a řešení problémů: docs/programovani_uzlu.md

.EXAMPLE
  .\nahraj.ps1 -Test       # jen ověří spojení s čipem (přečte signaturu)
  .\nahraj.ps1 -Fuses      # nový čip: fuses + firmware
  .\nahraj.ps1             # aktualizace firmwaru (klíče párování zůstanou)
  .\nahraj.ps1 -Port COM5  # port zadaný ručně (když je připojeno víc zařízení)
  .\nahraj.ps1 -Build      # firmware přeložit z této složky (potřebuje avr-gcc v PATH nebo C:\avr)
#>
param(
    [string]$Port = "",
    [switch]$Fuses,
    [switch]$Test,
    [switch]$Build,
    [int]$Baud = 115200,
    [string]$Programmer = "serialupdi",
    [string]$ToolsDir = "C:\avr"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location $PSScriptRoot

$Repo = "Svaty10/Retencni_Nadrz_V2"
$AvrdudeUrl = "https://github.com/avrdudes/avrdude/releases/download/v8.3/avrdude-v8.3-windows-x64.zip"
$AvrdudeSha = "8630c7d8bb9682a1685aaa9ad42550ae1131c9d13528f0d20975368ba615e9b0"
$Cache = Join-Path $env:LOCALAPPDATA "Retencka"

function Get-Sha256([string]$path) { (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() }

function Find-Tool([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @($ToolsDir, $Cache)) {
        $f = Get-ChildItem -Path $dir -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { return $f.FullName }
    }
    return $null
}

function Get-Avrdude {
    $exe = Find-Tool "avrdude"
    if ($exe) { return $exe }
    Write-Host "Stahuji avrdude 8.3 (github.com/avrdudes)…"
    New-Item -ItemType Directory -Force $Cache | Out-Null
    $zip = Join-Path $Cache "avrdude.zip"
    Invoke-WebRequest -UseBasicParsing $AvrdudeUrl -OutFile $zip
    if ((Get-Sha256 $zip) -ne $AvrdudeSha) { Remove-Item $zip; throw "Stažený avrdude nemá správný kontrolní součet – přerušeno." }
    Expand-Archive -Force $zip (Join-Path $Cache "avrdude")
    Remove-Item $zip
    $exe = Find-Tool "avrdude"
    if (-not $exe) { throw "avrdude se nepodařilo rozbalit." }
    return $exe
}

function Get-Port {
    if ($Port) { return $Port }
    # only USB serial devices (Arduino, CH340, CP2102, FTDI ...) – not built-in COM1 or Bluetooth ports
    $usb = @(Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%(COM%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.PNPDeviceID -like "USB*" -or $_.PNPDeviceID -like "FTDIBUS*" })
    if ($usb.Count -eq 1 -and $usb[0].Name -match "\((COM\d+)\)") {
        Write-Host "Programátor: $($usb[0].Name)"
        return $Matches[1]
    }
    if ($usb.Count -eq 0) { throw "Nenalezen žádný USB sériový port – připojte Arduino / převodník (příp. ovladač CH340)." }
    $list = ($usb | ForEach-Object { $_.Name }) -join "; "
    throw "Připojeno víc USB portů ($list) – zadejte jeden, např. .\nahraj.ps1 -Port COM5"
}

function Invoke-Avrdude([string[]]$extra) {
    $p = Get-Port                 # first: no download when no programmer is connected
    $avrdude = Get-Avrdude
    $avrArgs = @("-c", $Programmer, "-p", "t1624", "-P", $p, "-b", "$Baud") + $extra
    & $avrdude @avrArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Nahrání selhalo – zkontrolujte zapojení (TX přes odpor, RX přímo, GND, u Arduina RESET–GND), port a baterie v čidle."
    }
}

function Get-Firmware {
    New-Item -ItemType Directory -Force build | Out-Null
    $hex = "build\firmware.hex"
    if ($Build) {
        $gcc = Find-Tool "avr-gcc"
        if (-not $gcc) { throw "Pro -Build je potřeba avr-gcc (rozbalit do $ToolsDir, viz docs\programovani_uzlu.md)." }
        $bin = Split-Path $gcc
        $cflags = @("-mmcu=attiny1624", "-DF_CPU=3333333UL", "-Iinclude", "-Os", "-std=gnu11",
                    "-Wall", "-Wextra", "-Werror", "-Wno-array-bounds",
                    "-funsigned-char", "-funsigned-bitfields", "-fshort-enums",
                    "-ffunction-sections", "-fdata-sections", "-flto")
        $objs = @()
        foreach ($s in @("main", "power", "adc", "sht40", "cc1101", "crypto", "proto", "link")) {
            & $gcc @cflags -c "src\$s.c" -o "build\$s.o"
            if ($LASTEXITCODE -ne 0) { throw "Překlad src\$s.c selhal." }
            $objs += "build\$s.o"
        }
        & $gcc -mmcu=attiny1624 -Os -flto "-Wl,--gc-sections" @objs -o build\firmware.elf
        if ($LASTEXITCODE -ne 0) { throw "Linkování selhalo." }
        & (Join-Path $bin "avr-objcopy.exe") -O ihex -R .eeprom -R .fuse -R .lock -R .signature build\firmware.elf $hex
        & (Join-Path $bin "avr-size.exe") build\firmware.elf
        return $hex
    }
    Write-Host "Stahuji hotový firmware (GitHub $Repo, vydání uzel-latest)…"
    $base = "https://github.com/$Repo/releases/download/uzel-latest"
    Invoke-WebRequest -UseBasicParsing "$base/retencka-uzel.hex" -OutFile $hex
    $want = (Invoke-WebRequest -UseBasicParsing "$base/retencka-uzel.hex.sha256").Content
    if ($want -is [byte[]]) { $want = [Text.Encoding]::ASCII.GetString($want) }
    if ((Get-Sha256 $hex) -ne $want.Trim().ToLower()) { Remove-Item $hex; throw "Firmware nemá správný kontrolní součet – přerušeno." }
    return $hex
}

if ($Test) {
    Invoke-Avrdude @()
    Write-Host "Spojení OK (signatura ATtiny1624 = 1E 94 2A)." -ForegroundColor Green
    return
}

$hex = Get-Firmware
# fuse1 BODCFG 0x04 = BOD 1,8 V v běhu, ve spánku vypnutý
# fuse2 OSCCFG 0x02 = interní 20 MHz (firmware si dělí na 3,33 MHz)
# fuse5 SYSCFG0 0xC5 = PA0 zůstává UPDI, EEPROM (klíče párování) přežije přehrání – NEMĚNIT
$extra = @()
if ($Fuses) { $extra += @("-U", "fuse1:w:0x04:m", "-U", "fuse2:w:0x02:m", "-U", "fuse5:w:0xC5:m") }
$extra += @("-U", "flash:w:${hex}:i")
Invoke-Avrdude $extra
Write-Host "Hotovo. Čidlo se restartovalo a 10 minut nabízí párování (hub: Nastavení → Čidla)." -ForegroundColor Green
