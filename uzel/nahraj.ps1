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
  .\nahraj.ps1 -TestSpanku       # testovací firmware, který jen spí (měření klidového proudu)
  .\nahraj.ps1 -Diagnostika      # DIAGNOSTICKÝ firmware: test rádia, SHT40 a sondy (..\docs\diagnostika_uzlu.md)
  .\nahraj.ps1 -Cti              # vyčte a vysvětlí výsledek diagnostiky z čidla

  Programátor se pozná sám: Arduino/převodník na pinech 0/1 (SerialUPDI), nebo Arduino
  s jtag2updi (pin 6). Ručně: -Programmer serialupdi | jtag2updi.
#>
param(
    [string]$Port = "",
    [switch]$Fuses,
    [switch]$Test,
    [switch]$Build,
    [switch]$PripravArduino,
    [switch]$SmazatParovani,
    [switch]$TestSpanku,
    [switch]$Diagnostika,
    [switch]$Cti,
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

if ($SmazatParovani) {
    # EEPROM = klíč párování a čítač rámců (obě kopie); flash ani fuses se nemění.
    # Nutné jen u čidla, jehož hub klíč ztratil (čidlo odebrané z hubu, nový hub bez zálohy).
    $ff = (@("0xff") * 256) -join ","
    Invoke-Avrdude @("-U", "eeprom:w:${ff}:m")
    Write-Host "Párování smazáno. Vyndejte a vložte baterii (nebo odpojte a připojte napájení) –" -ForegroundColor Green
    Write-Host "čidlo pak 10 minut nabízí párování (hub: Nastavení → Čidla → Čekat na čidlo)." -ForegroundColor Green
    return
}

# fuse1 BODCFG 0x04 = BOD 1,8 V v běhu, ve spánku vypnutý
# fuse2 OSCCFG 0x02 = interní 20 MHz (firmware si dělí na 3,33 MHz)
# fuse5 SYSCFG0 0xC5 = PA0 zůstává UPDI, EEPROM (klíče párování) přežije přehrání – NEMĚNIT
$FuseArgs = @("-U", "fuse1:w:0x04:m", "-U", "fuse2:w:0x02:m", "-U", "fuse5:w:0xC5:m")

if ($TestSpanku) {
    # Hotový hex je v repozitáři (make spanek); fuses kvůli BOD, který by ve spánku bral ~20 uA.
    Invoke-Avrdude ($FuseArgs + @("-U", "flash:w:tools\test_spanku.hex:i"))
    Write-Host "Hotovo. Čidlo teď jen spí: nic neměří a nevysílá, párování zůstalo." -ForegroundColor Green
    Write-Host "Před měřením ODPOJTE převodník (UPDI i GND). Zpět: 3-aktualizace-firmwaru.cmd" -ForegroundColor Green
    return
}

# ---------------------------------------------------------------- diagnostika
# Záznam diagnostického firmwaru (tools\diag.c) v EEPROM od 0xC0:
#   magic "DIAG" u32 | cycles u16 | heard_total u16 | heard_best i8 | pkt[25] | lines | lines2 | clk_edges | crc16
# pkt = testovací paket DIAG (0x07), stejný jako čidlo vysílá (nikdo ho nezpracovává).
function Get-Crc16([byte[]]$d, [int]$off, [int]$n) {
    $crc = 0xFFFF
    for ($i = 0; $i -lt $n; $i++) {
        $crc = $crc -bxor ([int]$d[$off + $i] -shl 8)
        for ($b = 0; $b -lt 8; $b++) {
            if ($crc -band 0x8000) { $crc = (($crc -shl 1) -bxor 0x1021) -band 0xFFFF } else { $crc = ($crc -shl 1) -band 0xFFFF }
        }
    }
    return $crc
}

function Show-Diagnostika([byte[]]$ee) {
    $i8 = { param($v) if ($v -ge 128) { $v - 256 } else { $v } }
    $u16 = { param($o) [int]$ee[$o] + 256 * [int]$ee[$o + 1] }
    $ano = { param($ok) if ($ok) { "ANO" } else { "NE" } }
    $base = 0xC0
    $magic = [System.Text.Encoding]::ASCII.GetString($ee, $base, 4)
    # klíč párování normálního firmwaru: 2 kopie po 59 B, magic "RTK1" + příznak spárováno
    $key = $false
    foreach ($a in @(0, 59)) {
        if ([System.Text.Encoding]::ASCII.GetString($ee, $a, 4) -eq "RTK1" -and $ee[$a + 4] -eq 1) { $key = $true }
    }
    Write-Host ""
    if ($magic -ne "DIAG") {
        Write-Host "V čidle není záznam diagnostiky." -ForegroundColor Yellow
        Write-Host "  • nahrajte diagnostický firmware (7-diagnostika.cmd) a nechte čidlo aspoň 30 s běžet,"
        Write-Host "  • když už nahraný je a záznam pořád chybí, čip se po startu hned restartuje (napájení!)."
        Write-Host ("Párovací klíč v EEPROM: " + $(if ($key) { "ANO (čidlo je spárované)" } else { "NE" }))
        return
    }
    $n = 39
    $crc = Get-Crc16 $ee $base ($n - 2)
    if ($crc -ne (& $u16 ($base + $n - 2))) {
        Write-Host "Záznam diagnostiky je poškozený (čtení během zápisu?) – zkuste vyčíst znovu." -ForegroundColor Yellow
        return
    }
    $cycles = & $u16 ($base + 4)
    $heardTotal = & $u16 ($base + 6)
    $heardBest = & $i8 $ee[$base + 8]
    $p = $base + 9
    $id = "{0:X4}" -f (& $u16 ($p + 1))
    $rst = $ee[$p + 5]
    $vbat = & $u16 ($p + 6)
    $t = & $u16 ($p + 8); if ($t -ge 32768) { $t -= 65536 }
    $hum = $ee[$p + 10]
    $probe = & $u16 ($p + 11)
    $part = $ee[$p + 13]; $ver = $ee[$p + 14]; $f = $ee[$p + 15]
    $marc = $ee[$p + 16]; $fscal = $ee[$p + 17]
    $heard = $ee[$p + 18]; $bad = $ee[$p + 19]
    $best = & $i8 $ee[$p + 20]; $floor = & $i8 $ee[$p + 21]
    $ltype = $ee[$p + 22]; $lid = "{0:X4}" -f (& $u16 ($p + 23))
    $so = ($f -band 0x01) -ne 0; $spi = ($f -band 0x02) -ne 0; $pll = ($f -band 0x04) -ne 0
    $gdo = ($f -band 0x08) -ne 0; $txok = ($f -band 0x10) -ne 0
    $clip = ($f -band 0x20) -ne 0; $sht = ($f -band 0x80) -ne 0
    $why = @()
    if ($rst -band 0x01) { $why += "zapnutí napájení" }
    if ($rst -band 0x02) { $why += "PODPĚTÍ (brown-out)" }
    if ($rst -band 0x08) { $why += "watchdog" }
    if ($rst -band 0x10) { $why += "software" }
    if ($rst -band 0x20) { $why += "programátor (UPDI)" }
    if ($rst -band 0x04) { $why += "pin RESET" }
    $types = @{ 0x03 = "data retenčky"; 0x04 = "párování retenčky"; 0x05 = "hub retenčky"; 0x06 = "retenčka";
                0x07 = "test jiného čidla retenčky"; 0x13 = "data anemometru"; 0x14 = "párování anemometru";
                0x17 = "test anemometru" }
    $tname = if ($types.ContainsKey([int]$ltype)) { $types[[int]$ltype] } else { "neznámý" }

    Write-Host ("Diagnostika čidla – poslední záznam (cyklus $cycles, běží asi {0} min)" -f [math]::Round($cycles * 6.5 / 60)) -ForegroundColor Cyan
    Write-Host ("  ID čidla:               $id")
    Write-Host ("  Poslední restart:       " + $(if ($why) { $why -join ", " } else { "–" }))
    Write-Host ("  Baterie:                " + $(if ($vbat -eq 0xFFFF) { "chyba měření" } else { "{0:N3} V" -f ($vbat / 1000.0) }))
    Write-Host ("  Párovací klíč v EEPROM: " + $(if ($key) { "ANO – hub musí mít čidlo spárované" } else { "NE – čidlo s normálním firmwarem nabízí párování" }))
    Write-Host ("  SHT40 (teplota, vlhk.): " + $(if ($sht) { "{0:N1} °C, $hum %" -f ($t / 100.0) } else { "NEODPOVÍDÁ" }))
    Write-Host ("  Sonda (napětí):         " + $(if ($probe -eq 0xFFFF) { "chyba měření" } else { "{0:N3} V" -f ($probe / 10000.0) }) + $(if ($clip) { "  (na plném rozsahu ADC)" } else { "" }))

    $l = $ee[$base + 34]; $l2 = $ee[$base + 35]; $edges = $ee[$base + 36]
    $idle = ($l -band 0x01) -ne 0
    $bMosi = ($l -band 0x02) -ne 0; $bSck = ($l -band 0x04) -ne 0
    $soLow = ($l -band 0x08) -ne 0; $soRel = ($l -band 0x10) -ne 0
    $clk = ($l -band 0x20) -ne 0
    $wr = (($l -band 0x40) -ne 0) -and (($l -band 0x80) -ne 0)
    $soDrv = (($l2 -band 0x01) -ne 0) -and (($l2 -band 0x02) -ne 0)
    Write-Host "Test linek SPI (bez čtení dat přes MISO)" -ForegroundColor Cyan
    Write-Host ("  MISO volná (CSn=1, pull-up):     " + $(if ($idle) { "ANO" } else { "NE – trvale v nule" }))
    if ($idle) {
        Write-Host ("  MISO spojená s MOSI / SCLK:      " + (& $ano $bMosi) + " / " + (& $ano $bSck))
        Write-Host ("  Rádio stáhne SO po CSn=0:        " + (& $ano $soLow))
        Write-Host ("  ... a pustí ho po CSn=1:         " + (& $ano $soRel))
    }
    Write-Host ("  Hodiny na GDO0 po resetu rádia:  " + (& $ano $clk) + "  ($edges změn)")
    Write-Host ("  Rádio přijme zápis (GDO0 0 a 1): " + (& $ano $wr))
    Write-Host ("  Rádio ovládá pin SO (0 a 1):     " + (& $ano $soDrv))
    Write-Host "Rádio CC1101" -ForegroundColor Cyan
    Write-Host ("  Odpovídá (SO, krystal): " + (& $ano $so))
    Write-Host ("  PARTNUM / VERSION:      0x{0:X2} / 0x{1:X2}" -f $part, $ver)
    Write-Host ("  Zápis a čtení registru: " + (& $ano $spi))
    Write-Host ("  PLL zamčená:            " + (& $ano $pll) + ("  (MARCSTATE 0x{0:X2}, FSCAL1 0x{1:X2})" -f $marc, $fscal))
    Write-Host ("  Vysílání dokončeno:     " + (& $ano $txok))
    Write-Host ("  GDO0 při vysílání:      " + (& $ano $gdo))
    Write-Host "Příjem na čidle (2 s poslechu v každém cyklu)" -ForegroundColor Cyan
    Write-Host ("  Šum kanálu:             " + $(if ($floor -eq -128) { "–" } else { "$floor dBm" }))
    Write-Host ("  Zachyceno od startu:    $heardTotal paketů" + $(if ($heardTotal) { ", nejsilnější $heardBest dBm" } else { "" }))
    Write-Host ("  V posledním cyklu:      $heard (chybných $bad)" + $(if ($heard) { ", poslední: $tname (0x{0:X2}) od $lid" -f $ltype } else { "" }))

    $tips = @()
    $lineFault = $true
    if (-not $idle -and $wr) {
        $tips += ("VERDIKT: rádio příkazy přijímá (GDO0 poslouchá zápisy), ale linka SO/MISO je trvale v nule,`n" +
                  "     i když rádio vybrané není. Vadný výstup SO čipu CC1101 – vyměňte modul." +
                  $(if (-not $soDrv) { "`n     (Ani na povel rádio pin SO nezvedne – potvrzuje to.)" } else { "" }))
    } elseif (-not $idle) {
        $tips += ("VERDIKT: linka SO/MISO je trvale v nule a rádio nepřijímá žádné příkazy (GDO0 na zápis nereaguje).`n" +
                  $(if ($clk) { "     Čip je napájený a krystal běží (hodiny na GDO0), ale SPI rozhraní čipu je mrtvé.`n" } else { "" }) +
                  "     Vadný čip CC1101 – vyměňte modul (nebo jej zkuste na funkční desce).")
    } elseif ($bMosi -or $bSck) {
        $tips += "VERDIKT: MISO je spojená s " + $(if ($bMosi) { "MOSI" } else { "SCLK" }) + " – můstek mezi spoji (na desce nebo uvnitř modulu)."
    } elseif (-not $soLow) {
        $tips += ("VERDIKT: rádio po CSn=0 nestáhne SO – " +
                  $(if ($clk) { "krystal přitom běží, nereaguje na CSn: vadný čip nebo spoj CSn na modulu." } else { "bez hodin na GDO0: čip nemá napájení/krystal, nebo je mrtvý." }))
    } elseif (-not $soRel) {
        $tips += "VERDIKT: rádio po CSn=1 nepustí SO (drží ho v nule) – vadný čip CC1101, vyměňte modul."
    } elseif (-not $wr) {
        $tips += "VERDIKT: linky jsou v pořádku, ale rádio zápisy nepřijímá (GDO0 nereaguje) – SI/MOSI nebo SCLK na modulu, případně vadný čip."
    } else {
        $lineFault = $false
    }
    if ($lineFault) {
        $tips += "Ostatní údaje rádia (PARTNUM, PLL, vysílání, šum) jsou kvůli tomu bez významu."
    } elseif (-not $spi -and $part -eq 0x00 -and $ver -eq 0x00) {
        $tips += ("Linky SPI i ovládání rádia prošly, přesto se při čtení registrů vrací jen nuly:`n" +
                  "     rádio nevydává data na SO – vadný čip CC1101, vyměňte modul.")
    } else {
        if (-not $spi) { $tips += "Rádio odpovídá, ale zápis/čtení registru nesedí: spoje SI/MOSI (PA1, pin 11) a SCLK (PA3, pin 13)." }
        if ($part -ne 0x00 -or ($ver -ne 0x14 -and $ver -ne 0x04)) { $tips += "Neobvyklé PARTNUM/VERSION – čip nejspíš není CC1101 nebo SPI čte nesmysly." }
        if (-not $pll) { $tips += "Syntezátor se nezamkl: krystal 26 MHz (a jeho kondenzátory), napájení rádia." }
        if (-not $txok) { $tips += "Vysílání se nedokončilo: rádio zůstalo ve vysílání – napájení při zátěži (~30 mA), PLL." }
        if ($txok -and -not $gdo) { $tips += "GDO0 (PB2, pin 7) se při vysílání nehnul: přerušený spoj GDO0 – čidlo pak NEPOZNÁ odpověď hubu (párování nefunguje)." }
        if ($floor -gt -80) { $tips += "Vysoký šum na kanálu ($floor dBm): rušení v okolí nebo zvlněné napájení rádia." }
        if ($heardTotal -eq 0 -and $cycles -ge 50) {
            $tips += ("Za ~5 min poslechu čidlo nezachytilo žádný paket. Pokud v dosahu vysílá jiné čidlo (anemometr`n" +
                      "     každou minutu, ostatní čidla retenčky každých 5 min), nefunguje příjem: anténa, krystal, spoj GDO0 (PB2).")
        } elseif ($heardTotal -eq 0) {
            $tips += "Zatím nic nezachyceno – nechte diagnostiku běžet aspoň 5 minut a vyčtěte znovu."
        }
        if ($heardTotal -gt 0 -and $heardBest -lt -95) { $tips += "Ostatní čidla slyší jen slabě (nejvýš $heardBest dBm) – anténa nebo dosah." }
    }
    if (-not $sht) { $tips += "SHT40 neodpovídá: spoje SDA (PB1, pin 8) a SCL (PB0, pin 9), napájení senzoru, pull-up rezistory I2C." }
    if ($probe -eq 0xFFFF) { $tips += "Měření sondy selhalo (převodník ADC nedokončil měření)." }
    elseif ($clip) { $tips += "Sonda je na plném rozsahu ADC: zkrat signálu sondy na 5 V, vadná sonda, nebo propojený konektor." }
    if ($rst -band 0x02) { $tips += "Restart kvůli podpětí: baterie nebo napájení z programátoru nestačí na vysílání nebo na 5 V pro sondu." }
    if ($vbat -ne 0xFFFF -and $vbat -lt 3000) { $tips += "Napětí pod 3,0 V – normální firmware by se vypnul." }
    Write-Host ""
    if ($tips) {
        Write-Host "Co zkontrolovat:" -ForegroundColor Yellow
        $tips | ForEach-Object { Write-Host "  • $_" }
    } else {
        Write-Host "Rádio, SHT40 i sonda v pořádku – čidlo vysílá i přijímá." -ForegroundColor Green
    }
    Write-Host "  Napětí sondy porovnejte s výškou hladiny (odpojená sonda ≈ 0 V)."
    Write-Host ""
    Write-Host "Zpět na normální firmware: 3-aktualizace-firmwaru.cmd (párování zůstane)."
}

if ($Cti) {
    New-Item -ItemType Directory -Force build | Out-Null
    $bin = Join-Path (Get-Location) "build\eeprom-diag.bin"
    if (Test-Path $bin) { Remove-Item $bin }
    Invoke-Avrdude @("-U", "eeprom:r:${bin}:r")
    Show-Diagnostika ([System.IO.File]::ReadAllBytes($bin))
    return
}

if ($Diagnostika) {
    # Hotový hex je v repozitáři (make diag); fuses stejné jako u normálního firmwaru (EESAVE: klíče zůstanou).
    Invoke-Avrdude ($FuseArgs + @("-U", "flash:w:tools\diag.hex:i"))
    Write-Host ""
    Write-Host "Diagnostický firmware běží: každých ~6 s otestuje rádio, SHT40 a sondu." -ForegroundColor Green
    Write-Host "  • nechte čidlo běžet aspoň 5 minut (poslouchá ostatní čidla v dosahu),"
    Write-Host "  • výsledek z čidla: 8-precist-diagnostiku.cmd (vyčtení čidlo restartuje, test začne znovu),"
    Write-Host "  • zpět: 3-aktualizace-firmwaru.cmd – párování v čidle zůstává."
    return
}

$Port = Get-Port               # programmer first: no firmware download without one
$hex = Get-Firmware
$extra = @()
if ($Fuses) { $extra += $FuseArgs }
$extra += @("-U", "flash:w:${hex}:i")
Invoke-Avrdude $extra
Write-Host "Hotovo. Čidlo se restartovalo a 10 minut nabízí párování (hub: Nastavení → Čidla)." -ForegroundColor Green
