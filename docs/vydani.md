# Vydání nové verze

Postup pro správce repozitáře. Huby si nové verze stahují samy z **GitHub Releases** (podepsané
Ed25519, viz [instalace.md, oddíl 5](instalace.md#5-aktualizace-z-githubu)). Firmware čidla se
nahrává ručně (UPDI, [programovani_uzlu.md](programovani_uzlu.md)) – na vzduchu se neaktualizuje.

## 0. Jednorázově: podpisový klíč (dělá majitel repozitáře)
Bez toho žádný Release nevznikne a huby nic nepřijmou. Podrobně v
[instalace.md, oddíl 5.1](instalace.md#51-jednorázová-příprava-podpisového-klíče):
1. `openssl genpkey -algorithm ed25519 -out release.key` a `openssl pkey -in release.key -pubout -out release.pub`
   (mimo repozitář a mimo OneDrive).
2. GitHub → *Settings → Secrets and variables → Actions* → secret `RELEASE_SIGNING_KEY` = celý `release.key`.
3. `release.pub` → `system/keys/release.pub`, commit + push.
4. Huby instalované dřív než bod 3: jednou spustit instalaci znovu.

## 1. Jaké číslo verze
[SemVer](https://semver.org/) `MAJOR.MINOR.PATCH`:

| Změna | Zvýšit | Příklad |
|---|---|---|
| jen opravy, žádná nová funkce | PATCH | 1.0.0 → 1.0.1 |
| nová funkce, nastavení, API pole (staré funguje dál) | MINOR | 1.0.1 → 1.1.0 |
| nekompatibilní změna (rádiový protokol, odstraněné API) | MAJOR | 1.1.0 → 2.0.0 |
| zkušební verze jen pro kanál `beta` | přípona | `1.1.0-beta1`, `1.1.0-rc1` |

Pravidla po vydání 1.0.0 (huby už běží u zákazníků):
- **Databáze:** schéma se mění jen **novou** položkou na konci `MIGRATIONS` v `server/retencka/db.py`;
  existující migrace se nikdy neupravují. Návrat na starší verzi (rollback) musí s novějším schématem
  přežít – přidávat sloupce/tabulky, nemazat a nepřejmenovávat.
- **Rádiový protokol** (`uzel/src/proto.c` ↔ `server/retencka/crypto.py`): hub musí dál rozumět
  čidlům se starším firmwarem (čidla se nepřeprogramují).
- **Nastavení:** nové klíče potřebují výchozí hodnotu; nové proměnné v `/etc/default/retencka`
  mají v kódu výchozí hodnotu (instalace je do souboru doplní zakomentované).

## 2. Kontrola před vydáním
```bash
cd server && python -m unittest discover -s tests        # vše OK
cd ../uzel && make && make test                          # jen když se měnil firmware (Windows: C:\avr\bin v PATH, mingw32-make)
```
- Firmware změněn → zvýšit `FW_VERSION` v `uzel/include/config.h` (10 = 1.0, 11 = 1.1 …).
- `server/retencka/__init__.py` → `__version__ = "1.0.1"`.
- `CHANGELOG.md` → nová sekce `## [1.0.1] - RRRR-MM-DD` (Přidáno / Změněno / Opraveno), nahoře.
- Dokumentace odpovídá změnám (README, docs/).

## 3. Vydání
```bash
git commit -am "Verze 1.0.1"
git push
git tag v1.0.1                  # přesně "v" + __version__, jinak Action skončí chybou
git push origin v1.0.1
```
GitHub Action **release** (záložka *Actions*): zkontroluje tag ↔ verzi, spustí testy, sestaví
`retencka-1.0.1.tar.gz`, podepíše (`.sig`) a vytvoří Release. Tag s pomlčkou (`v1.1.0-beta1`) se
vydá jako *pre-release* – dostanou ho jen huby s `RETENCKA_CHANNEL=beta`.

Push do `main` se změnou v `uzel/**` navíc spustí Action **firmware**, která přeloží firmware a vystaví
ho v předběžném vydání `uzel-latest` (odtud si ho bere `uzel/nahraj.ps1`). To s verzí hubu nesouvisí.

## 4. Ověření
1. Release `v1.0.1` na GitHubu má soubory `retencka-1.0.1.tar.gz` **a** `retencka-1.0.1.tar.gz.sig`.
2. Na testovacím hubu: *Nastavení → Systém → Zkontrolovat aktualizace* → do ~2 min „Aktualizováno 1.0.0 → 1.0.1“.
   Podrobnosti: `journalctl -u retencka-update -n 50`.
3. Ostatní huby si verzi stáhnou samy do 24 h (03:30 + náhodné zpoždění).

## 5. Když se vydání nepovede
- **Action selhala** (špatný tag, testy, klíč): opravit, smazat tag a vydat znovu:
  ```bash
  git tag -d v1.0.1 && git push --delete origin v1.0.1
  ```
  Pokud už Release vznikl, smazat ho na GitHubu (*Releases → … → Delete*).
- **Hub verzi nenastartoval:** sám se vrátil na předchozí verzi a tuto verzi si zablokoval
  (`/var/lib/retencka/data/update-bad-versions.json`). Chybu opravit a vydat **vyšší** verzi (1.0.2) –
  stejné číslo už hub znovu nezkusí.
- Další hlášky a jejich řešení: [instalace.md, oddíl 5.3](instalace.md#53-když-aktualizace-neprojde).
