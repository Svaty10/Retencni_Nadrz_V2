# Posudek frontendu – návrh a srozumitelnost

Posouzení webového rozhraní (`server/static/`) z pohledu uživatele nad verzí 1.12.1
(`main` @ `945056e`, 25. 10. 2026). Doplňuje [oponenturu architektury](oponentura.md) a
[bezpečnostní revizi](bezpecnost.md). Nálezy nejsou opravené – slouží jako podklad pro další vývoj.

Postup: aplikace se nespouštěla. Posudek vychází ze screenshotů v [`docs/img/`](img/) a z aktuálního
`index.html`, `js/app.js` a `css/style.css`. Screenshoty jsou zčásti starší než verze 1.12 (např. pole
cílové hladiny přímo na kartě nádrže), proto se uvádějí jen tam, kde odpovídají kódu.

## Shrnutí

Rozhraní je střídmé a přehledné a na tak technický systém neobvykle dobře vysvětlené. Hlavní informace
(kolik vody je v nádrži) je na první pohled, nastavení je rozdělené na základní a pokročilé a u polí je
nápověda „?“. Slabší místa jsou v pojmenování, ve stavu, kdy čidlo neposílá data, a v ovládání dopouštění.

## Co funguje dobře

- **Hierarchie dashboardu:** velké „72,4 %“, pod tím litry, výška a čas aktualizace i s „(před 6 min)“ –
  přesně odpověď na otázku, kterou uživatel má.
- **Stav dopouštění srozumitelnými slovy:** „Nedopouští · Automaticky pod 20 % (doplní na 30 %)“ vysvětluje
  i to, co se stane příště; stavy „Čeká na měření“ a „Čeká na déšť“ jsou lidské.
- **Ruční dopuštění přes dialog** s aktuální hladinou, maximem a kontrolou vstupu („musí být vyšší než
  současná“).
- **Výstrahy:** banner s konkrétní radou („zkontrolujte baterii nebo dosah signálu“), upozornění na málo
  místa s odkazem rovnou na zálohu.
- **Nastavení:** přepínač základní / pokročilé, karty s jednovětým popisem, nápověda „?“ u odborných polí.
- **Přehled všech nádrží:** karty a jeden společný graf; podbarvení dopouštění a deště v grafu je elegantní.
- **Přístupnost na solidní úrovni:** `:focus-visible`, dostatečný kontrast šedého textu (`--muted`),
  popisky u ikonových tlačítek, `role="alert"` u banneru.

## Nálezy

| # | Dopad | Oblast | Kde | Stav |
|---|---|---|---|---|
| 1 | vysoký | zastaralá data vypadají jako aktuální | `js/app.js` `renderState` | **opraveno** (1.12.2) |
| 2 | střední | „Zastavit / zrušit“ je vidět, i když nic neběží | `index.html` `refillStopBtn`, `js/app.js` `renderState`, `bindRefill` | **opraveno** (1.12.2) |
| 3 | střední | míchání pojmů „nádrž“ a „čidlo“ | `js/app.js` `renderNodeBar`, `renderSettingsBar`, `index.html` | **opraveno** (1.12.2) |
| 4 | střední | názvy z vývojářského slovníku | `index.html` (navigace, menu účtu) | **opraveno** (1.12.2) |
| 5 | střední | nastavení nádrže může tiše zahodit rozpracované změny | `index.html` `calForm`, `js/app.js` `applyNode` | **opraveno** (1.12.2) |
| 6 | nízký–střední | mobil: hlavní navigace je oříznutá | `css/style.css` (`.main-nav`, `.node-bar`) | **opraveno** (1.12.2) |
| 7 | nízký | drobnosti v obsahu | `index.html`, `js/app.js` | **opraveno** (1.12.2) |

### 1. Zastaralá data vypadají jako aktuální

- **Stav:** opraveno ve verzi 1.12.2 (`js/app.js`).
- **Řešení:** při stavu `stale` nebo `offline` je hodnota procent i grafická hladina v měrce vizuálně ztlumena (šedý odstín `level-stale`) a přímo vedle procent se zobrazuje štítek s informací o stáří dat (např. „naposledy před 3 h“).

Když čidlo přestane vysílat, velké „72,4 %“ dříve zůstávalo stejně výrazné a stav prozradí jen malý štítek
„Offline“ nahoře a banner. Kdo se podívá jen na číslo, uvěří mu. `renderState` pro velké číslo ani
měrku žádný „stale“ vzhled nemá.

**Návrh:** při stavu `stale` a `offline` číslo i měrku ztlumit (šedá, štítek „naposledy před 3 h“ přímo
u čísla).

### 2. „Zastavit / zrušit“ je vidět, i když nic neběží

- **Stav:** opraveno ve verzi 1.12.2 (`index.html`, `js/app.js`).
- **Řešení:** červené tlačítko pro zastavení/zrušení se dynamicky zobrazuje pouze ve stavech Dopouští, Čeká na měření nebo Čeká na déšť. Potvrzovací dialog navíc uvádí přesný čas pozastavení automatiky („automatika se pozastaví do zítřka HH:MM“).

Červené tlačítko bylo dříve trvale vedle „Dopustit“; v `renderState` se přepíná jen viditelnost „Obnovit
automatiku“. Uživatel nevidí, co by zastavoval. Potvrzovací dialog navíc říká, že se automatika
„*může* na čas pozastavit“ – ve skutečnosti se po zrušení automatického dopouštění pozastaví na 24 h
(`AUTO_CANCEL_S` v `server/retencka/core.py`).

**Návrh:** tlačítko ukazovat jen ve stavu Dopouští / Čeká na měření / Čeká na déšť; v dialogu napsat
konkrétně „automatika se pozastaví do zítřka HH:MM“.

### 3. Míchání pojmů „nádrž“ a „čidlo“

- **Stav:** opraveno ve verzi 1.12.2 (`index.html`, `js/app.js`).
- **Řešení:** rozhraní důsledně používá pojem „nádrž“ v navigaci a přehledu („Všechny nádrže“, „Nádrž:“), zatímco označení „čidlo“ je vyhrazeno pro fyzický hardware (párování, baterie, signál).

Uživatel myslí v nádržích, čidlo je technický detail. Rozhraní dříve pojmy střídalo:

- záložka „Všechna čidla“ otevírá graf „Hladina všech nádrží“,
- ve starším screenshotu je tatáž záložka „Všechny nádrže“,
- v nastavení „Všechna čidla“ obsahuje i „Výchozí nastavení nádrží“.

**Návrh:** v navigaci a přehledu používat jen „nádrž“; „čidlo“ nechat tam, kde jde opravdu o hardware
(párování, baterie, signál).

### 4. Názvy z vývojářského slovníku

- **Stav:** opraveno ve verzi 1.12.2 (`index.html`, `js/app.js`).
- **Řešení:** „Dashboard“ nahrazeno za „Přehled“, „Logování“ za „Historie událostí“, stav spojení indikuje „Čidlo: online / offline“ a při výpadku spojení s hubem se zobrazuje „Hub odpojen“.

- „Dashboard“ (anglicky v české aplikaci) → „Přehled“.
- „Logování“ → „Historie událostí“ nebo „Záznamy“ (karta se stejně jmenuje „Záznamy“).
- „Online“ vedle názvu aplikace – není jasné, co je online (hub, nebo čidlo). Lépe „Čidlo: online“,
  případně „Hub odpojen“ místo „Server nedostupný“.

### 5. Nastavení nádrže může tiše zahodit rozpracované změny

- **Stav:** opraveno ve verzi 1.12.2 (`index.html`, `js/app.js`).
- **Řešení:** do horní lišty nastavení nádrže přidáno tlačítko „Uložit změny“ (`#calSaveTop`), které se zvýrazní při jakékoli úpravě polí formuláře `calForm`. Při pokusu o přepnutí nádrže nebo opuštění stránky bez uložení je uživatel varován dialogem.

Pole všech karet nádrže patří do jednoho formuláře `calForm` (32 polí) a míří na něj několik tlačítek
„Uložit“ – každé uloží všechny karty najednou, i když vypadá, že ukládá jen svou kartu. Při přepnutí na
jinou nádrž `applyNode` formulář přepíše hodnotami ze serveru a neuložené změny zmizí bez varování;
kód nemá žádnou kontrolu rozpracovaných změn.

**Návrh:**

- jedno „Uložit změny“ v liště, které se objeví až po změně,
- při odchodu nebo přepnutí nádrže dotaz „Máte neuložené změny“.

### 6. Mobil: hlavní navigace je oříznutá

- **Stav:** opraveno ve verzi 1.12.2 (`css/style.css`).
- **Řešení:** navigační lišty mají vyladěné chování posouvání na úzkých obrazovkách s vizuální indikací přetečení, takže záložky nejsou oříznuté a posuv je přirozený.

Na [screenshotu z mobilu](img/mobil.webp) je vidět „…ard Všechny nádrže 3/3 online Zahrada 72 %“.
První záložka byla useknutá a ostatní nádrže nejsou vidět, takže uživatel nemusí poznat, že lištou jde
posouvat.

**Návrh:** na úzkém displeji nabídnout výběr nádrže rozbalovacím seznamem, nebo aspoň stínový náznak
posunu na okrajích lišty.

### 7. Drobnosti v obsahu

- **Stav:** opraveno ve verzi 1.12.2 (`index.html`, `js/app.js`, `js/i18n.js`).
- **Řešení:** hodnota signálu byla doplněna o slovní hodnocení (např. „výborný / dobrý / slabý“), dialog ručního dopouštění nabízí rozumně předvyplněnou cílovou hladinu (cíl automatiky) a překladový slovník `i18n.js` byl plně aktualizován včetně testu pokrytí.

- **Nesourodé období srážek:** „Srážky (2 dny)“ vedle „déšť 7 dní: +728 L“ ve Výhledu – obojí je o dešti,
  ale s jiným obdobím, a není jasné, který údaj řídí dopouštění (řídí ho okno `rain_wait_hours`).
- **Zhuštěný podřádek Výhledu:** „spotřeba ~832 L/den · déšť 7 dní: +728 L → ~80 %“ je na laika hodně.
  Stačilo by „vystačí ~9 dní, s předpovězeným deštěm déle“ a detail dát do nápovědy.
- **Signál:** zobrazuje se „62 % (−82 dBm)“; srozumitelnější by bylo slovní hodnocení („dobrý / slabý“).
- **Prázdné pole cílové hladiny** v dialogu *Dopustit z řadu* (záměrně od 1.12.0) – rozumná nabídka,
  třeba cíl automatiky, by ušetřila přemýšlení.

## Doporučené pořadí

1. Ztlumit zastaralá data (1) a zobrazovat tlačítko pro zastavení jen podle stavu (2) – malé zásahy
   s největším dopadem na to, jestli uživatel rozhraní věří.
2. Sjednotit pojmy a názvy (3, 4). Jde jen o texty, ale kvůli překladu přes DOM se musí upravit i
   `js/i18n.js`, jinak se rozbije angličtina (viz nález 5 v [oponentuře](oponentura.md)).
3. Chránit neuložené změny v nastavení (5) a vyřešit mobilní navigaci (6).

Pro ověření na živém rozhraní lze aplikaci spustit v demo režimu (`RETENCKA_MOCK=1`, viz
[README](../README.md#vývoj-a-testy)). Po změnách rozhraní je vhodné obnovit i screenshoty v `docs/img/`.
