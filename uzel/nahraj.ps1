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
  .\nahraj.ps1 -PripravArduino   # z Arduina Uno/Nano udělá programátor jtag2updi (pin 6)

  Programátor se pozná sám: Arduino/převodník na pinech 0/1 (SerialUPDI), nebo Arduino
  s jtag2updi (pin 6). Ručně: -Programmer serialupdi | jtag2updi.
#>
param(
    [string]$Port = "",
    [switch]$Fuses,
    [switch]$Test,
    [switch]$Build,
    [switch]$PripravArduino,
    [int]$Baud = 115200,
    [string]$Programmer = "auto",
    [string]$ToolsDir = "C:\avr"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# any error: one clear red message instead of a PowerShell stack trace
trap {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
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
        # Windows PowerShell 5.1: -Recurse on a missing folder searches the whole parent drive
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
        $f = Get-ChildItem -LiteralPath $dir -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
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

function Test-Echo([string]$p) {
    # SerialUPDI: TX and RX meet on the UPDI wire, so every byte sent must come back
    # even without the sensor. No echo = the programmer side is wired wrong.
    $sp = New-Object System.IO.Ports.SerialPort $p, $Baud, ([System.IO.Ports.Parity]::Even), 8, ([System.IO.Ports.StopBits]::Two)
    $sp.ReadTimeout = 500
    try {
        $sp.Open()
        Start-Sleep -Milliseconds 300            # some boards reset when the port opens
        $sp.DiscardInBuffer()
        $out = [byte[]](0x55, 0xAA, 0x0F)
        $sp.Write($out, 0, $out.Length)
        $got = New-Object byte[] 3
        $n = 0
        try { while ($n -lt 3) { $n += $sp.Read($got, $n, 3 - $n) } } catch [System.TimeoutException] { }
        return ($n -eq 3 -and $got[0] -eq 0x55 -and $got[1] -eq 0xAA -and $got[2] -eq 0x0F)
    } catch [System.UnauthorizedAccessException] {
        throw "Port $p je obsazený jiným programem – zavřete Arduino IDE / sériový monitor."
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
    }
}

function Invoke-Avrdude([string[]]$extra) {
    Write-Host "1/3 Hledám programátor…"
    $p = Get-Port                 # first: no download when no programmer is connected
    Write-Host "2/3 Kontroluji zapojení programátoru…"
    $prog = $Programmer
    if ($prog -eq "auto") {
        # SerialUPDI (pins 0/1) echoes every byte; an Arduino with jtag2updi does not
        if (Test-Echo $p) { $prog = "serialupdi"; Write-Host "    Arduino/převodník na pinech 0/1 (SerialUPDI)." }
        else { $prog = "jtag2updi"; Write-Host "    Zkouším Arduino s jtag2updi (pin 6)…" }
    } elseif ($prog -eq "serialupdi" -and -not (Test-Echo $p)) {
        throw ("Signál se nevrací. Zkuste odpojit drát UPDI od čidla a test spustit znovu:`n" +
               "  • pak 'Arduino OK' -> linku drží čidlo: nemá napájení (baterie / 3,3 V z Uno)`n" +
               "    nebo je drát na jiném pinu než UPDI (pin 10 ATtiny1624),`n" +
               "  • pořád nic -> zapojení Arduina: drát RESET – GND, D0 (přes odpor) a D1 spojené`n" +
               "    v jednom bodě. Klony s CH340 to často neumí – použijte 0-priprav-arduino.cmd.")
    }
    Write-Host "3/3 Komunikuji s čipem v čidle…"
    $avrdude = Get-Avrdude
    $avrArgs = @("-c", $prog, "-p", "t1624", "-P", $p, "-b", "$Baud") + $extra
    # show avrdude output and keep it for the diagnosis (stderr must not stop the script)
    $ErrorActionPreference = "Continue"
    $out = & $avrdude @avrArgs 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        if ($line -and $line -notmatch "^System\.Management\.Automation") { Write-Host $line; $line }
    }
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rc -eq 0) { return }
    $text = $out -join "`n"
    # jtag2updi answered (RSP_... status) = programmer fine, only the target is silent
    if ($prog -eq "jtag2updi" -and $text -notmatch "RSP_|FW version") {
        throw ("Arduino neodpovídá ani jako převodník (piny 0/1), ani jako programátor jtag2updi (pin 6).`n" +
               "  • klon Uno/Nano s CH340: jednou spusťte 0-priprav-arduino.cmd (bez drátů na Arduinu),`n" +
               "    pak zapojte pin 6 přes 4,7 kΩ na UPDI,`n" +
               "  • nebo zapojení přes piny 0/1: drát RESET – GND, D0 přes odpor a D1 spojené na UPDI.")
    }
    if ($prog -eq "jtag2updi") {
        throw ("Arduino (jtag2updi) je v pořádku, ale čip v čidle neodpovídá:`n" +
               "  • drát z pinu 6 Arduina přes 4,7 kΩ na UPDI (pin 10 ATtiny1624), GND na GND,`n" +
               "  • napájení čidla (baterie, nebo 3,3 V z Arduina Uno – ne obojí),`n" +
               "  • drát RESET – GND na Arduinu NESMÍ být zapojený (jtag2updi by neběželo).")
    }
    throw ("Čip v čidle neodpovídá – Arduino je zapojené dobře, chyba je na straně ČIDLA:`n" +
           "  • napájení čidla (baterie, nebo 3,3 V z Arduina Uno – ne obojí),`n" +
           "  • UPDI drát opravdu na UPDI (pin 10 ATtiny1624) a GND na GND,`n" +
           "  • zkuste pomaleji: .\nahraj.ps1 -Test -Baud 57600")
}

function Install-Jtag2updi {
    Write-Host "Nahrávám do Arduina program jtag2updi (z Arduina bude programátor na pinu 6)…"
    Write-Host "Na Arduinu nesmí být zapojené žádné dráty (hlavně ne RESET – GND)."
    $p = Get-Port
    $avrdude = Get-Avrdude
    $hex = "tools\jtag2updi\jtag2updi-uno.hex"
    foreach ($b in @(115200, 57600)) {               # Optiboot (Uno) / old bootloader (Nano)
        & $avrdude -c arduino -p m328p -P $p -b $b -U "flash:w:${hex}:i"
        if ($LASTEXITCODE -eq 0) {
            Write-Host ""
            Write-Host "Hotovo – Arduino je teď programátor jtag2updi." -ForegroundColor Green
            Write-Host "Zapojení: pin 6 → 4,7 kΩ → UPDI čidla, GND → GND, napájení čidla; RESET–GND NEzapojovat."
            return
        }
    }
    throw "Nahrání do Arduina selhalo – je to Uno/Nano (ATmega328P)? Zavřete Arduino IDE a odpojte dráty z Arduina."
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

if ($PripravArduino) {
    Install-Jtag2updi
    return
}

if ($Test) {
    Invoke-Avrdude @()
    Write-Host "Spojení OK (signatura ATtiny1624 = 1E 94 2A)." -ForegroundColor Green
    return
}

$Port = Get-Port               # programmer first: no firmware download without one
$hex = Get-Firmware
# fuse1 BODCFG 0x04 = BOD 1,8 V v běhu, ve spánku vypnutý
# fuse2 OSCCFG 0x02 = interní 20 MHz (firmware si dělí na 3,33 MHz)
# fuse5 SYSCFG0 0xC5 = PA0 zůstává UPDI, EEPROM (klíče párování) přežije přehrání – NEMĚNIT
$extra = @()
if ($Fuses) { $extra += @("-U", "fuse1:w:0x04:m", "-U", "fuse2:w:0x02:m", "-U", "fuse5:w:0xC5:m") }
$extra += @("-U", "flash:w:${hex}:i")
Invoke-Avrdude $extra
Write-Host "Hotovo. Čidlo se restartovalo a 10 minut nabízí párování (hub: Nastavení → Čidla)." -ForegroundColor Green
