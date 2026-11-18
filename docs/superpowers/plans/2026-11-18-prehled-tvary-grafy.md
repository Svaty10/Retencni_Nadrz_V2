# Přehled, tvary nádrží, přepad, posuvné grafy – implementační plán

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nové tvary nádrží s přepadem a obrázky, jména čidel v historii, přehled bez mapy hubu s návratem z detailu, posuvné čárové grafy se středovou čárou a 3 zálohy místo 7.

**Architecture:** Výpočet objemu zůstává v čisté funkci `model._volume_and_max_l`. Přepad se řeší jednou funkcí `full_level_cm(s)`, kterou použijí `compute_level`, `level_from_mm` i `max_volume_l`. Frontend (vanilla JS + Chart.js, CSP bez inline stylů) dostane jeden plugin pro tažení čárových grafů.

**Tech Stack:** Python 3 (aiohttp, sqlite, unittest), vanilla JS, Chart.js (vendor), HTML/CSS.

**Spec:** `docs/superpowers/specs/2026-11-18-prehled-tvary-grafy-design.md`

## Global Constraints

- Testy: `cd server && python -m unittest discover -s tests -v` – všechny musí projít po každém tasku.
- Pracovní strom obsahuje cizí rozpracované změny (firmware `uzel/`, `pcb/`, `CHANGELOG.md`, `docs/programovani_uzlu.md`). Commitovat **jen soubory daného tasku** (`git add <cesty>`), nikdy `git add -A`.
- CSP bez inline stylů: pozice/výšky jen přes CSSOM (`el.style.x = …` v JS), žádné `style=""` v HTML ani v innerHTML.
- Texty UI česky; kde má sekce anglický překlad v `static/js/i18n.js`, doplnit EN.
- Žádné nové knihovny (ani do `static/vendor/`).
- Staré konfigurace musí fungovat beze změny (nový klíč `overflow_cm` = 0 → chování jako dnes).
- Ověření UI: `preview_start {name: "retencka-demo"}` (port 8093, `RETENCKA_MOCK=1`); demo data `RETENCKA_DATA=<dir> python tools/seed_demo.py 30` a znovu `… 30 A1C3 --name "Garáž"`.

## Review Focus

1. **Přepad u ležatého válce:** D = 200, přepad 180 → při hladině 180 cm je 100 %, při hladině 100 cm je V(100)/V(180) (≈ 53 %, ne 50 % a ne 55,6 %). Test v Task 1.
2. **Přepad prázdný (0) u staré konfigurace:** výsledky `compute_level` i `max_volume_l` jsou bit po bitu stejné jako před změnou pro `area`, `cyl_h`, `cyl_v`, `rect`. Test v Task 1.
3. **Hladina nad přepadem** (ucpaný přepad, 190 cm při přepadu 180): 100 %, objem = V(přepad), žádný pád ani > 100 %. Test v Task 1.
4. **Tabulka s nesmyslnými body** (jeden bod, klesající objem, výška 0 dvakrát): odmítne se srozumitelnou českou chybou, uloží se nic. Test v Task 2.
5. **Graf s mezerou v datech nebo bez dat** (čidlo offline 2 dny, nová nádrž): tažení nespadne, bublina ukáže „bez dat“. Ruční ověření v Task 7.

---

### Task 1: Výpočet objemu – nové tvary, tabulka, přepad

**Files:**
- Modify: `server/retencka/model.py` (`tank_height_cm`, `_volume_and_max_l`, `compute_level`, `level_from_mm`, `max_volume_l`, `deadband_l`)
- Test: `server/tests/test_model.py`

**Interfaces:**
- Produces:
  - Nové `tank_shape` hodnoty: `cyl_h_dished`, `sphere`, `oval_h`, `cyl_v_cone`, `table`.
  - Nové klíče konfigurace (čte je model, schéma přidá Task 2): `tank_dome_cm` (hloubka jednoho dna), `tank_cone_cm` (výška kužele), `tank_table` (`[[výška_cm, objem_l], …]` seřazeno podle výšky), `overflow_cm` (0 = bez přepadu).
  - Rozměry podle tvaru:
    | tvar | pole |
    |---|---|
    | `cyl_h_dished` | `tank_diameter_cm` D, `tank_length_cm` L (válcová část), `tank_dome_cm` a |
    | `sphere` | `tank_diameter_cm` |
    | `oval_h` | `tank_width_cm` W, `max_depth_cm` H, `tank_length_cm` L |
    | `cyl_v_cone` | `tank_diameter_cm`, `max_depth_cm` (celková výška), `tank_cone_cm` |
    | `table` | `tank_table` |
  - `tank_height_cm(s) -> float`: výška tvaru (D pro `cyl_h`, `cyl_h_dished`, `sphere`; `max_depth_cm` pro `oval_h`, `cyl_v_cone`, ostatní; poslední výška tabulky pro `table`).
  - `full_level_cm(s) -> float`: `min(overflow_cm, tank_height_cm(s))`, když `overflow_cm > 0`, jinak `tank_height_cm(s)`.
  - `volume_l(level_cm: float, s: dict) -> float`: objem při hladině (ořezané na 0…tank_height).
  - `max_volume_l(s) -> float`: `volume_l(full_level_cm(s), s)`.
  - `Level.percent` = `volume_l(level) / max_volume_l * 100`, hladina ořezaná na `full_level_cm`.

- [ ] **Step 1: Napsat padající testy** do `test_model.py` (třída `TankShapesTest`):

```python
def cfg(**kw):
    base = {"tank_shape": "area", "cal_points": [[0, 0], [1000, 100]], "sensor_offset_cm": 0.0,
            "probe_accuracy_pct": 0.5, "probe_range_cm": 200.0, "overflow_cm": 0.0}
    base.update(kw); return base

def test_sphere_half_full(self):
    s = cfg(tank_shape="sphere", tank_diameter_cm=200.0)
    self.assertAlmostEqual(model.max_volume_l(s), 4 / 3 * math.pi * 1.0 ** 3 * 1000, delta=1)
    self.assertAlmostEqual(model.level_from_mm(1000, s).percent, 50.0, places=3)

def test_dished_hemispherical_equals_cylinder_plus_sphere(self):
    s = cfg(tank_shape="cyl_h_dished", tank_diameter_cm=200.0, tank_length_cm=300.0, tank_dome_cm=100.0)
    self.assertAlmostEqual(model.max_volume_l(s), (math.pi * 3.0 + 4 / 3 * math.pi) * 1000, delta=1)

def test_oval_is_scaled_cylinder(self):
    s = cfg(tank_shape="oval_h", tank_width_cm=200.0, max_depth_cm=100.0, tank_length_cm=100.0)
    self.assertAlmostEqual(model.max_volume_l(s), math.pi * 1.0 * 0.5 * 1.0 * 1000, delta=1)
    self.assertAlmostEqual(model.level_from_mm(500, s).percent, 50.0, places=3)

def test_cone_bottom(self):
    s = cfg(tank_shape="cyl_v_cone", tank_diameter_cm=100.0, max_depth_cm=200.0, tank_cone_cm=60.0)
    r = 0.5; cone = math.pi * r * r * 0.6 / 3; cyl = math.pi * r * r * 1.4
    self.assertAlmostEqual(model.max_volume_l(s), (cone + cyl) * 1000, delta=1)
    self.assertAlmostEqual(model.volume_l(30.0, s), math.pi * (r / 2) ** 2 * 0.3 / 3 * 1000, delta=0.5)

def test_table_interpolates(self):
    s = cfg(tank_shape="table", tank_table=[[0, 0], [100, 1000], [200, 3000]])
    self.assertEqual(model.tank_height_cm(s), 200)
    self.assertAlmostEqual(model.volume_l(150.0, s), 2000.0)

def test_overflow_horizontal_cylinder(self):
    s = cfg(tank_shape="cyl_h", tank_diameter_cm=200.0, tank_length_cm=100.0, overflow_cm=180.0)
    full = model.volume_l(180.0, s)
    self.assertAlmostEqual(model.max_volume_l(s), full)
    self.assertAlmostEqual(model.level_from_mm(1800, s).percent, 100.0)
    self.assertAlmostEqual(model.level_from_mm(1000, s).percent, model.volume_l(100.0, s) / full * 100)

def test_above_overflow_clamped(self):
    s = cfg(tank_shape="cyl_h", tank_diameter_cm=200.0, tank_length_cm=100.0, overflow_cm=180.0)
    lv = model.level_from_mm(1900, s)
    self.assertAlmostEqual(lv.percent, 100.0)
    self.assertAlmostEqual(lv.volume_l, model.max_volume_l(s))

def test_no_overflow_unchanged(self):
    # hodnoty spočítané PŘED změnou (spustit na původním kódu a vložit čísla)
    for s, mm, pct, vol in OLD_RESULTS: ...
```

`OLD_RESULTS`: před implementací spustit na původním `model.py` pro `area`(1,5 m², 150 cm), `cyl_h`(200×300), `cyl_v`(150, 200), `rect`(200×100×150) při 0, 370, 1000, 1500 mm a vložit výsledky `(percent, volume_l)` jako konstanty.

- [ ] **Step 2: Spustit** `python -m unittest tests.test_model -v` → nové testy FAIL (`AttributeError: volume_l` / špatná čísla); `test_no_overflow_unchanged` PASS.

- [ ] **Step 3: Implementovat** v `model.py`:
  - Rozdělit `_volume_and_max_l` na `volume_l(level_cm, s)` (dispatch podle tvaru). Starou funkci odstranit, volající přepsat.
  - `cyl_h_dished`: `segment_cyl(h) * L + (a / R) * π h² (3R − h) / 3` (cm³ → L: / 1000).
  - `oval_h`: `(W / H) * segment_kruhu(r = H/2, h) * L`.
  - `cyl_v_cone`: pro `h ≤ hc` kužel s poloměrem `R·h/hc` (`π (R h/hc)² h / 3`); nad tím plný kužel + válec.
  - `table`: lineární interpolace, mimo rozsah ořez na krajní body.
  - `compute_level` / `level_from_mm`: ořez hladiny na `full_level_cm(s)`, pak `percent = volume_l(level) / max_volume_l(s) * 100`.
  - `deadband_l`: obecně `deadband_mm/10 × max_area_cm2 / 1000`, kde `max_area` = max přes h v krocích 1 cm z `volume_l(h+1) − volume_l(h)`. Starý tvarový `if`-řetěz odstranit. Pro existující tvary výsledek do 1 % shodný (kontrola v testu: `cyl_v` 150 cm → beze změny).

- [ ] **Step 4: Spustit** celou sadu `python -m unittest discover -s tests -v` → PASS.

- [ ] **Step 5: Commit** `git add server/retencka/model.py server/tests/test_model.py` · `git commit -m "Tank model: dished/sphere/oval/cone/table shapes and overflow height"`

---

### Task 2: Schéma a validace nových polí

**Files:**
- Modify: `server/retencka/settings.py` (`NODE_SCHEMA`, `coerce`, `Nodes.update` za `_check_refill(eff)`)
- Modify: `server/retencka/web.py:38-50` (`RECALC_KEYS`, popisky pro audit)
- Test: `server/tests/test_server.py` (nebo `test_node_defaults.py`, kde se už testuje `Nodes.update`)

**Interfaces:**
- Consumes: klíče a tvary z Task 1.
- Produces:
  - `NODE_SCHEMA`:
    - `tank_shape` choice rozšířit o `cyl_h_dished,sphere,oval_h,cyl_v_cone,table`,
    - `tank_dome_cm` (0.0, float, 0, 1000),
    - `tank_cone_cm` (0.0, float, 0, 2000),
    - `overflow_cm` (0.0, float, 0, 2000),
    - `tank_table` ([], typ `"table"`).
  - Nové klíče **nejsou** v `INHERITABLE`.
  - `_check_tank(cfg: dict) -> None` – vyhazuje `SettingsError`.

- [ ] **Step 1: Testy:**
  - `test_table_rejects_single_point` – `coerce(NODE_SCHEMA, "tank_table", [[0, 0]])` → `SettingsError` „Tabulka objemu potřebuje aspoň 2 body (nejvýše 30).“
  - `test_table_rejects_falling_volume` – `[[0, 0], [100, 500], [150, 400]]` → `SettingsError` „Výška i objem v tabulce musí s každým řádkem růst.“
  - `test_table_sorted` – `[[100, 1000], [0, 0]]` → `[[0.0, 0.0], [100.0, 1000.0]]`.
  - `test_overflow_above_height_rejected` – `Nodes.update` s `cyl_h`, D = 200, `overflow_cm` = 210 → `SettingsError` „Přepad nemůže být výš než nádrž (200 cm).“; uloženo nic.
  - `test_dome_too_deep_rejected` – D = 200, dome = 120 → „Hloubka dna může být nejvýše polovina průměru (100 cm).“
  - `test_cone_too_tall_rejected` – cone ≥ max_depth → „Kužel musí být nižší než celá nádrž.“
  - `test_old_config_still_valid` – konfigurace bez nových klíčů projde a `overflow_cm` = 0.
- [ ] **Step 2: Spustit** → FAIL.
- [ ] **Step 3: Implementovat:**
  - typ `"table"` v `coerce`: float páry, 0 ≤ cm ≤ 2000, 0 ≤ L ≤ 1e7, seřadit podle výšky, ostře rostoucí výška i objem, 2–30 bodů; prázdný seznam je povolený (tvar ≠ table).
  - `_check_tank(eff)` volat hned za `_check_refill(eff)` v `Nodes.update` (ř. ~390). Pro `table` vyžaduje ≥ 2 body s hláškou „Pro tvar Vlastní tabulka vyplňte aspoň 2 řádky.“
  - `web.py`:
    - `RECALC_KEYS` += `tank_dome_cm`, `tank_cone_cm`, `tank_table`, `overflow_cm`,
    - popisky: „hloubka dna“, „výška kužele“, „tabulka objemu“, „výška přepadu“.
- [ ] **Step 4: Spustit** celou sadu → PASS.
- [ ] **Step 5: Commit** (`settings.py`, `web.py`, testy) `"Settings: new tank shapes, volume table and overflow height"`

---

### Task 3: Jména čidel v historii událostí

**Files:**
- Modify: `server/retencka/core.py` (`audit` volání na ř. ~790, ~848, ~857, ~1069) a přidat metodu
- Modify: `server/retencka/web.py` (ř. ~662, 672, 719, 744, 845, 854, 874)
- Test: `server/tests/test_server.py`

**Interfaces:**
- Produces: `Core.node_label(nid: int) -> str` → `"Retencka-V1 (BF1C)"`, bez jména `"BF1C"`.

- [ ] **Step 1: Test** `test_audit_uses_node_name`: založit čidlo BF1C s názvem „Retencka-V1“, poslat změnu nastavení přes API, ověřit, že poslední audit `detail` začíná `"Čidlo Retencka-V1 (BF1C): "`. Plus unit `node_label` pro neznámé ID → `"BF1C"`.
- [ ] **Step 2: Spustit** → FAIL.
- [ ] **Step 3: Implementovat** `node_label` a nahradit všechna `f"…{nid:04X}…"` v audit detailech.
  - Párovací záznamy (název ještě nemusí existovat) použijí `node_label` taky; u párování se vezme jméno z `pair_targets`, když v `nodes` chybí.
  - `NODE_UNPAIR_REQUESTED`: dnešní „{nid} ({name})“ převést na `node_label` (bez zdvojení).
- [ ] **Step 4: Spustit** celou sadu → PASS.
- [ ] **Step 5: Commit** `"Event history: sensor name next to its ID"`

---

### Task 4: Zálohy – 3 kusy, v UI jen poslední

**Files:**
- Modify: `server/retencka/core.py:66` (`BACKUP_KEEP = 3`)
- Modify: `server/static/index.html:795-820` (tabulka záloh → jeden řádek), `server/static/js/app.js` `renderBackups`
- Test: `server/tests/test_backup.py`

- [ ] **Step 1: Test** `test_keeps_three_backups`: 5× `backup_local()` s podvrženým datem (patch `datetime` nebo přejmenovat soubory) → ve složce 3 nejnovější.
- [ ] **Step 2: Spustit** → FAIL (zůstane 5).
- [ ] **Step 3: Implementovat:**
  - `BACKUP_KEEP = 3`.
  - Odstavec v HTML: „Poslední záloha: <b id="bkLast">–</b> · uchovávají se poslední <b id="bkKeep">3</b> (`<code id="bkDir">`)“.
  - `renderBackups` vyplní `bkLast` jako `fmtDateTime · num(size_mb) MB` z nejnovější položky, při prázdném seznamu „zatím žádná – první vznikne do 24 h od spuštění“.
  - `#backupBody` a jeho tabulku odstranit.
- [ ] **Step 4: Spustit** testy → PASS; v preview (Nastavení → Zálohy) ověřit jeden řádek.
- [ ] **Step 5: Commit** `"Backups: keep 3 nightly copies, show only the latest"`

---

### Task 5: Přehled bez mapy hubu, návrat přes „Spojení s hubem“

**Files:**
- Modify: `server/static/index.html` (`#hubMap` v `#overview`; `#fnHub` ř. ~159)
- Modify: `server/static/js/app.js` (`renderOverview`, smazat `renderHubMap`; obsluha `#fnHub`)
- Modify: `server/static/css/style.css` (smazat `.hm-*`, `#hubMap`; `#overviewGrid` přes celou šířku)

**Interfaces:**
- Consumes: existující `setViewAll(true)`, `renderNodeBar`, `renderOverview`, `loadAllCharts` – stejná sekvence jako handler pilulky „Všechny nádrže“ (`renderNodeBar`, ř. ~1378). Vytáhnout ji do `function showAllTanks(nodes)` a použít na obou místech.

- [ ] **Step 1: Implementovat:**
  - Smazat mapu (HTML, JS, CSS).
  - Kompaktní karty (doplněno uživatelem): `#overviewGrid` `repeat(auto-fill, minmax(220px, 1fr))`, menší kruh a písmo, aby se 2–4 nádrže vešly na jeden řádek od ~1000 px.
  - `#fnHub` změnit na `<button type="button">` se stejným vzhledem, `aria-label="Zpět na všechny nádrže"`, a `title` se stejným textem. Zobrazit ho jako klikatelný (kurzor, hover kruhu) jen při ≥ 2 nádržích, jinak je to obyčejný neaktivní uzel.
- [ ] **Step 2: Ověřit v preview** (2 demo nádrže):
  - přehled nemá mapu, karty vyplní šířku na 1440 px i na mobilu (375 px),
  - klik na „Spojení s hubem“ v detailu → přehled a pilulka „Všechny nádrže“ aktivní,
  - konzole bez chyb.
  - Screenshot.
- [ ] **Step 3: Commit** `"Overview: drop the hub map, hub link returns to all tanks"`

---

### Task 5b: Pořadí nádrží (doplněno uživatelem)

**Files:** `server/retencka/settings.py` (`Nodes.set_order`), `server/retencka/web.py` (`POST /api/nodes/order`, admin), `server/static/js/app.js` (`renderNodeBar`, `renderOverview`), CSS; test `server/tests/test_server.py`.

**Interfaces:**
- `Nodes.set_order(ids: list[int]) -> None` – zapíše sloupec `nodes.sort` (0…n-1), `reload()`. Seznam musí obsahovat přesně všechna ID, jinak `SettingsError("Pořadí musí obsahovat všechna čidla.")`.
- `POST /api/nodes/order {"order": ["BF1C", "F618"]}` → `{"ok": true}`, audit `NODES_REORDERED` „Pořadí nádrží: A, B“.

- [ ] Test `test_node_order`: dvě čidla, POST pořadí obráceně → `/api/state` `nodes` v novém pořadí; chybějící ID → 400; uživatel bez admin → 403.
- [ ] Implementace; UI: admin může přetáhnout pilulku v liště nahoře i kartu na přehledu (pointer events, funguje i dotykem po podržení 300 ms); klávesnice: Alt+←/→ na pilulce/kartě. Po puštění POST a překreslení.
- [ ] Commit `"Tanks can be reordered in the top bar and the overview"`

### Task 6: Formulář tvaru – nová pole, tabulka, přepad, obrázky

**Files:**
- Modify: `server/static/index.html:375-410` (karta `#tkTank`), SVG symboly tvarů do existujícího `<svg>` se symboly ikon
- Modify: `server/static/js/app.js` (`syncTankShape`, `applyNode`, odesílání `calForm`, help text `shape` ř. ~344)
- Modify: `server/static/css/style.css` (obrázek tvaru), `server/static/js/i18n.js` (EN, pokud karta překlad má)

**Interfaces:**
- Consumes: klíče a validace z Task 2; chyby API se zobrazují existujícím toastem.

- [ ] **Step 1: Implementovat:**
  - `<select name="tank_shape">`:
    - Kvádr (`area`),
    - Válec nastojato (`cyl_v`),
    - Válec nastojato s kuželovým dnem (`cyl_v_cone`),
    - Válec naležato (`cyl_h`),
    - Válec naležato s klenutými dny (`cyl_h_dished`),
    - Ležatý ovál (`oval_h`),
    - Koule (`sphere`),
    - Vlastní tabulka (`table`).
  - Pole s `data-shapes`:
    - `max_depth_cm` „Výška nádrže“ pro `area,cyl_v,cyl_v_cone,oval_h`,
    - `tank_diameter_cm` pro `cyl_h,cyl_v,cyl_v_cone,cyl_h_dished,sphere`,
    - `tank_length_cm` pro `cyl_h,cyl_h_dished,oval_h` (u `cyl_h_dished` popisek „Délka válcové části“),
    - `tank_width_cm` „Šířka“ pro `oval_h`,
    - `tank_dome_cm` „Hloubka dna“ pro `cyl_h_dished` (placeholder = D/4, prázdné → při změně průměru předvyplnit D/4),
    - `tank_cone_cm` „Výška kužele“ pro `cyl_v_cone`.
  - `overflow_cm` „Výška přepadu (volitelné)“ pro všechny tvary. Nápověda: „Výška spodní hrany přepadu nade dnem. Plná nádrž = hladina u přepadu; procenta se počítají z objemu po přepad.“ Prázdné pole se odešle jako 0.
  - Tabulka `tank_table` (jen `table`):
    - řádky výška [cm] / objem [L], tlačítka „Přidat řádek“ a odebrat, max 30,
    - `fillForm` / odeslání přes JSON pole (stejně jako `cal_points`).
  - Obrázek tvaru:
    - jeden `<symbol id="shape-<tvar>">` na tvar, schematický bokorys / řez,
    - kóty s popisky shodnými s poli (D, L, a, W, H, hc),
    - čárkovaná vodorovná čára „přepad“ ve výšce `overflow_cm / výška` (posun přes CSSOM `transform`), skrytá, když je přepad 0,
    - barvy přes `currentColor` / CSS proměnné, ať funguje světlý i tmavý motiv.
    - `syncTankShape` přepne `<use href>` a polohu čáry; volá se i na `input` u výšky a přepadu.
  - Help `shape` doplnit o věty pro nové tvary a o tabulku od výrobce.
- [ ] **Step 2: Ověřit v preview:**
  - pro každý tvar se zobrazí správná pole a obrázek,
  - uložení `cyl_h` D = 200, přepad 180 → karta na přehledu ukazuje „z <V(180)> L“,
  - neplatný přepad 210 → toast s hláškou z Task 2,
  - tabulka 3 řádků se uloží a po obnovení stránky načte,
  - tmavý i světlý motiv,
  - konzole bez chyb.
  - Screenshot formuláře (tmavý motiv).
- [ ] **Step 3: Commit** `"Tank settings: new shapes with diagrams, volume table, overflow height"`

---

### Task 7: Posuvné čárové grafy se středovou čárou

**Files:**
- Modify: `server/static/js/app.js` (sekce charts ř. ~700–1110: `rangeOf`, `getHistory`, `upsert` pro `level`, `all`, `klima`, `battery`)
- Modify: `server/static/css/style.css` (bublina), `server/static/index.html` (bublina v `.chart-container` jen pokud nejde vytvořit z JS)

**Interfaces:**
- Produces:
  - `const scrubPlugin` – Chart.js plugin `id: 'scrub'`, registrovaný jen pro čárové grafy (`level`, `all`, `klima`, `battery`; sloupcové ne).
  - Stav na grafu: `chart.$scrub = { offsetMs, loadedFrom }`.

- [ ] **Step 1: Implementovat:**
  - **Data:** při načtení grafu stáhnout `[from − span, to]`. Okno `x.min/max` je posledních `span`, tedy dvojnásobek viditelného rozsahu.
  - **Tažení:** `pointerdown/move/up` na canvasu (s `setPointerCapture`), `touch-action: pan-y` na kontejneru, aby svislé scrollování stránky dál fungovalo.
    - Posun = `dx / šířka plochy × span`.
    - `x.min/max` se posune a zavolá se `chart.update('none')`.
    - Doprava nejde za „teď“ (u živého rozsahu) ani za `to` (u vlastního).
  - **Dotažení dat:** když `x.min` < `loadedFrom + span/4`, stáhnout další `span` do minulosti (stejné API, `histCache` dál platí) a přilepit na začátek datasetů. Během načítání neblokovat tažení; jedno načítání naráz.
  - **Středová čára:** `afterDatasetsDraw` nakreslí svislou čáru uprostřed `chartArea` (barva `T.text`, 1 px) a bod na každé křivce v tom čase.
  - **Bublina:** HTML prvek nad grafem. Ukazuje čas pod čárou (`timeTooltip` formát) a hodnotu každé řady interpolovanou mezi sousedními body.
    - Mezera v datech delší než 3× běžný krok → „bez dat“.
    - Prázdný graf → bublina skrytá.
  - **Klávesnice:** canvas `tabindex=0`, šipky ←/→ posunou o 1/10 okna, `Home` vrátí na „teď“.
  - **Dvojklik / tlačítko „Teď“** v bublině vrátí posun na 0.
  - Změna rozsahu (24 h / 7 d…) posun vynuluje.
  - Stávající tooltip při najetí myší u těchto grafů vypnout (`plugins.tooltip.enabled = false`), hodnotu ukazuje bublina.
  - `prefers-reduced-motion`: beze změny (posun je přímý, bez animace).
- [ ] **Step 2: Ověřit v preview** s 30 dny demo dat:
  - tažení myší (`left_click_drag`) posune hladinu a bublina mění čas a hodnotu,
  - tažení na začátek dotáhne starší data (síťový požadavek `/api/history` s dřívějším `from`),
  - za „teď“ to nepustí,
  - sloupcové grafy beze změny,
  - mobil 375 px: svislý scroll stránky přes graf funguje,
  - nová nádrž bez dat i offline úsek: bez chyb v konzoli.
  - Screenshot.
- [ ] **Step 3: Commit** `"Charts: drag line charts under a fixed centre cursor"`

---

### Task 8: Dokumentace a závěrečná kontrola

**Files:**
- Modify: `docs/api.md` (nové klíče nastavení nádrže, pokud jsou tam vyjmenované), `README.md` (seznam tvarů, pokud tam je)

- [ ] **Step 1:** Doplnit nové tvary, `overflow_cm` a `tank_table` do dokumentace, kde se klíče nádrže popisují (`grep -rn "tank_shape" docs README.md`).
- [ ] **Step 2:** Celá sada testů `python -m unittest discover -s tests -v` → PASS. Projít celé UI v preview (přehled, detail, nastavení, historie, zálohy), konzole bez chyb.
- [ ] **Step 3: Commit** `"Docs: new tank shapes and overflow height"`. CHANGELOG a vydání řeší až release podle `docs/vydani.md` (v `CHANGELOG.md` jsou teď cizí rozpracované změny).
