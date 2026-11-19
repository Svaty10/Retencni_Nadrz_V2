# Osobní uspořádání nádrží a dashboardu

Schváleno uživatelem v konverzaci 19. 11. 2026.

## Proč

Pořadí nádrží bylo jedno pro celý hub (měnit ho mohl jen admin) a každé přesunutí se zapisovalo do historie
událostí. Uspořádání je ale osobní věc: každý uživatel chce svoje, stejné na všech svých zařízeních, a do
historie hubu nepatří. Navíc chce uživatel přeskládat a skrývat karty na dashboardu, na mobilu jinak než na PC.

## Co se mění

1. **Pořadí nádrží patří uživateli.** Ukládá se k účtu na hubu (jako motiv a jazyk), platí na všech
   zařízeních. Přesouvat může každý přihlášený, nejen admin. Přesun se nezapisuje do historie.
   Nádrž, kterou uživatel ve svém pořadí nemá (nově přidaná), se zařadí na konec ve výchozím pořadí.
   Endpoint `POST /api/nodes/order`, `Nodes.set_order` a zápis `NODES_REORDERED` mizí
   (kategorie v historii zůstává kvůli starým záznamům).
2. **Rozložení dashboardu nádrže.** Karty: `tank` (nádrž + údaje), `level`, `consumption`, `refill`,
   `klima`, `weather`, `battery`. Tlačítko „Upravit rozložení“ pod dashboardem přepne do úprav: lišta nahoře
   („Upravuješ rozložení pro počítač / mobil“, „Obnovit výchozí“, „Hotovo“), u každé karty úchyt na tažení,
   šipky nahoru/dolů a přepínač Skrýt/Zobrazit. Skryté karty jsou v úpravách zašedlé. „Hotovo“ uloží.
   Kartu nádrže skrýt nejde (můj návrh – bez ní dashboard nedává smysl). Rozložení je jedno pro všechny nádrže.
3. **Dvě rozložení:** `wide` (okno ≥ 768 px) a `narrow` (< 768 px). Dokud uživatel `narrow` neupraví,
   přebírá `wide`. „Obnovit výchozí“ smaže upravované rozložení.
4. **Rozsah grafů** zůstává uložený jen v prohlížeči (beze změny).

## Jak

- DB migrace v15: `users.prefs TEXT NOT NULL DEFAULT '{}'` (JSON).
- `GET /api/me` vrací `prefs`. `POST /api/me/prefs` (každý přihlášený, CSRF) přijme libovolnou podmnožinu
  klíčů `node_order` (seznam ID nádrží, max 64, bez duplicit), `dash_wide`, `dash_narrow`
  (`{"order": [karty], "hidden": [karty]}` nebo `null` = výchozí). Neznámé klíče/karty → 400.
  Ukládá se sloučení se stávajícími prefs.
- Frontend: seznam nádrží z `/api/state` se seřadí podle `ME.prefs.node_order`. Všechny karty dashboardu
  jsou v jedné mřížce; pořadí = pořadí v DOM, skrytí = třída. Neúplné řádky mřížky dorovná JS
  (poslední karta v řádku se roztáhne) – nahrazuje dřívější pevná CSS pravidla pro chybějící dopouštění.
