# Přehled, tvary nádrží, přepad, posuvné grafy – návrh

Datum: 2026-11-18 · stav: ke schválení

## Proč

Deska V2 i aplikace fungují na dvou nádržích. Při běžném používání vyšlo najevo:

- přehled všech nádrží vypadá prázdně (mapa hubu),
- z detailu nádrže se nedá vrátit zpět na přehled,
- v historii událostí často chybí název čidla,
- tvary nádrží nepokrývají běžné typy retenčních nádrží,
- nádrž se plní jen po přepad, takže procenta ukazují špatnou plnost,
- grafy nejdou posouvat v čase,
- záloh je zbytečně mnoho a seznam všech záloh nikdo nepotřebuje.

## Cíle

1. **Přehled bez mapy hubu.** Na přehledu „Všechny nádrže“ se odstraní mapa hubu (`renderHubMap`, `#hubMap` a její CSS). Zůstanou karty nádrží, které se roztáhnou přes celou šířku (mřížka, víc karet v řadě podle šířky okna).
2. **Návrat z detailu.** Uzel „Spojení s hubem“ (`#fnHub`) na detailu nádrže je tlačítko. Kliknutí přepne na Všechny nádrže, stejně jako pilulka „Všechny nádrže“. Ovládat jde i klávesnicí.
3. **Jména v historii událostí.** Každý audit záznam, který se týká čidla, píše „Název (ID)“, např. „Retencka-V1 (BF1C)“. Týká se `web.py` (párování, nastavení, kalibrace, přepočet) i `core.py` (spárováno, odpárování). Když čidlo ještě nemá název (párování), zůstane jen ID. Staré záznamy se nepřepisují.
4. **Nové tvary nádrže** (`tank_shape` v `settings.py`, výpočet v `model._volume_and_max_l`):
   - `cyl_h_dished` – ležatý válec s klenutými dny: průměr D, délka válcové části L, hloubka dna a (jedno dno; polokoule = D/2). Objem = segment válce × L + dvě dna jako elipsoid s poloosami R, R, a. Zaplněný díl takového elipsoidu = (a/R) · π h²(3R − h)/3.
   - `sphere` – koule: průměr D, V = π h²(3R − h)/3.
   - `oval_h` – ležatý ovál (eliptický průřez): šířka W, výška H, délka L. Plocha segmentu = (W/H) × plocha kruhového segmentu s r = H/2.
   - `cyl_v_cone` – stojatý válec s kuželovým dnem: průměr D, výška kužele hc, celková výška.
   - `table` – vlastní tabulka výška [cm] → objem [L] od výrobce, lineární interpolace. Minimálně 2 body, rostoucí výška i objem.
   - Stávající `area`, `cyl_h`, `cyl_v`, `rect` zůstanou beze změny.
5. **Obrázky tvarů.** U výběru tvaru v nastavení nádrže je schematický obrázek (inline SVG, barvy podle motivu) vybraného tvaru. Popisky rozměrů odpovídají polím formuláře. Je v něm vyznačená výška přepadu.
6. **Přepad** – nové volitelné pole `overflow_cm`: výška přepadu nade dnem nádrže.
   - Geometrie se počítá z celého tvaru. Plná (100 %) je ale nádrž naplněná do výšky přepadu.
   - Procenta = V(hladina) / V(přepad), tedy objem k objemu, ne výška k výšce. U ležatého válce a koule je v tom velký rozdíl.
   - Max. objem (karty, „z 9 991 L“, Home Assistant, MQTT) = V(přepad).
   - Prázdné pole = přepad na plné výšce tvaru (dnešní chování).
   - Validace: 0 < přepad ≤ výška tvaru.
7. **Posuvné grafy (jako SwitchBot).** Týká se jen čárových (spojitých) grafů, ne sloupcových:
   - Uprostřed grafu je pevná svislá čára a nad ní bublina s hodnotou a časem bodu pod čárou.
   - Tažením myší nebo prstem se data posouvají pod čárou. Délku okna dál určuje výběr rozsahu 24 h / 7 d / …; zoom gesty nebude.
   - Načte se delší úsek dat, než je vidět. U kraje se dotáhnou starší data. Do budoucnosti se posunout nedá.
   - Vlastní kód nad Chart.js, žádná nová knihovna.
8. **Zálohy.**
   - V nastavení se ukazuje jen poslední záloha (datum, velikost) a počet uchovávaných.
   - `BACKUP_KEEP` se sníží ze 7 na **3**. Zálohy chrání proti vadné aktualizaci a poškozené databázi. Tři noci dávají rezervu i pro chybu, která se projeví až po dni.

## Nedělá se

- skupiny nádrží,
- přesun „Čidla v dosahu“ – zůstává, kde je,
- zoom prsty / kolečkem,
- přepis starých záznamů historie.

## Omezení

- CSP bez inline stylů: pozice a výšky se nastavují přes CSSOM (jako dnes).
- Nikde zatím nenasazeno, zpětná kompatibilita konfigurace je jen „nice-to-have“. Nové pole `overflow_cm` má výchozí hodnotu prázdnou a staré konfigurace fungují beze změny.
- Defaulty nastavení se kopírují do čidla (`test_node_defaults`), nová pole musí projít stejnou cestou.
- Texty UI česky a doplnit anglické překlady v `i18n.js`, pokud tam daná sekce je.

## Postupy (zvažované)

**Posuvné grafy:**
- **A – vlastní tažení nad Chart.js (vybráno).** Posouvá se `scales.x.min/max` nad předem načtenými daty. Středová čára je malý plugin. Bez nových závislostí, chování přesně podle SwitchBotu.
- B – chartjs-plugin-zoom + Hammer.js. Dvě nové knihovny ve vendor/, pevnou středovou čáru by stejně bylo potřeba dopsat a chování je spíš jako posun mapy. Zamítnuto.

**Tvary:** vzorce pro typické tvary + tabulka výška → objem pro vše ostatní. Varianta „jen tabulka“ by uživatele nutila hledat tabulku i u obyčejného válce.

## Volby nechané na implementaci (moje rozhodnutí, ke kontrole)

- Hladina nad přepadem: procenta se ořežou na 100 %, objem se taky nepočítá nad V(přepad). Voda přepadem odteče a krátké překmity jsou šum.
- Rozsah dotahování dat při posunu: vždy jedno další okno dané délky.
- Výchozí hloubka dna u `cyl_h_dished` = D/4 (běžné torisférické dno), uživatel ji může přepsat.
- Obrázky tvarů jako jeden SVG symbol na tvar v `index.html` (`<symbol>` jako ikony).
- Testy: výpočty objemu všech tvarů a přepadu v `test_model.py`, validace v `test_server.py`, zálohy v `test_backup.py`.
