# Home Assistant (MQTT)

Hub se připojí k MQTT brokeru Home Assistantu a každou nádrž mu ohlásí jako **zařízení** se všemi
entitami (MQTT Discovery). V Home Assistantu se nic nepíše do YAML; nové čidlo, přejmenování nebo
odebrání nádrže se projeví samo.

## Propojení (asi 5 minut)
1. **Home Assistant → Nastavení → Doplňky → Obchod s doplňky → Mosquitto broker** → *Instalovat* → *Spustit*.
2. **Nastavení → Zařízení a služby**: u nalezené integrace **MQTT** klikněte na *Přidat* (potvrdit).
3. **Nastavení → Lidé → Uživatelé** → *Přidat uživatele*, např. `retencka` s heslem. Tímto účtem se hub
   přihlásí k brokeru (Mosquitto přijímá účty Home Assistantu). Uživatel nemusí být administrátor.
4. **Web nádrže → Nastavení → Obecné → Home Assistant**: adresa Home Assistantu (např.
   `homeassistant.local` nebo `192.168.1.20`), jméno a heslo z bodu 3 → *Uložit a připojit*.
   Stav se změní na **MQTT: připojeno**.
5. V Home Assistantu: *Nastavení → Zařízení a služby → MQTT → zařízení* – každá nádrž pod svým názvem.

Port je 1883 (bez šifrování, jen v domácí síti). Broker mimo domácí síť: v *Pokročilých* zapnout
**Šifrovat (TLS)** a port 8883; broker musí mít platný certifikát pro zadaný název.

## Entity jedné nádrže

| Entita | Typ | Jednotka | Poznámka |
|---|---|---|---|
| Hladina | sensor | % | |
| Výška hladiny | sensor | cm | |
| Objem | sensor | L | |
| Teplota, Vlhkost | sensor | °C, % | v šachtě u čidla |
| Baterie | sensor | % | |
| Slabá baterie | binary_sensor | | zapne se včas před výměnou |
| Spotřeba dnes | sensor (water, total_increasing) | L | lze přidat do *Energie → Spotřeba vody* |
| Očekávaný déšť (2 dny) | sensor | mm | předpověď Open-Meteo |
| Dopouštění | binary_sensor | | jen nádrž s ventilem |
| Dopustit, Zastavit dopouštění | button | | jen nádrž s ventilem a zapnutým *Dopouštění z Home Assistantu* |
| Stav čidla, Poslední měření, Napětí baterie, Signál | diagnostika | | |

*Dopustit* napouští na **cíl automatického dopouštění** nádrže (stejně jako `dopustit` v Telegramu bez čísla),
se všemi pojistkami (maximální hladina, nejdelší doba). Akce se zapíše do Historie událostí jako
„Home Assistant“. Kdo se dostane do Home Assistantu (nebo k brokeru), může tedy dopouštět – když to
nechcete, vypněte *Dopouštění z Home Assistantu*; tlačítka zmizí a příkazy hub ignoruje.

Když čidlo přestane posílat data, jeho měřené hodnoty jsou v Home Assistantu *nedostupné*; když se vypne
hub, nedostupné je všechno (poslední vůle MQTT).

## Odpojení
*Odpojit* v kartě Home Assistant (nebo smazání adresy): hub z Home Assistantu odebere svá zařízení
(historie v Home Assistantu zůstane) a odpojí se.

## Pro pokročilé – témata
`<uid>` je náhodný identifikátor hubu (8 znaků), `<id>` ID čidla malými písmeny (např. `3f2a`).

| Téma | Obsah |
|---|---|
| `retencka/<uid>/status` | `online` / `offline` (poslední vůle), retained |
| `retencka/<uid>/<id>/state` | JSON stavu nádrže, retained, při každé změně a aspoň jednou za minutu |
| `retencka/<uid>/<id>/availability` | `online` / `offline` (čidlo mlčí déle než limit zpoždění dat) |
| `retencka/<uid>/<id>/refill/set` | příkaz `START` / `STOP` (z tlačítek) |
| `homeassistant/<komponenta>/retencka_<uid>_<id>/<entita>/config` | Discovery, retained |

Stav:
```json
{"status": "online", "level_pct": 59.2, "level_cm": 54.4, "volume_l": 5933, "max_l": 10028,
 "temperature": 12.0, "humidity": 93.0, "battery_pct": 100.0, "battery_v": 4.95, "battery_low": false,
 "battery_days_left": 1460, "rssi": -71.5, "last_seen": "2026-10-28T09:12:03+00:00",
 "refilling": false, "consumption_today": 182, "expected_rain_mm": 3.4}
```
Hub po restartu Home Assistantu (`homeassistant/status` = `online`) pošle Discovery i stavy znovu.
Klient je MQTT 3.1.1, QoS 0, jen standardní knihovna Pythonu (`server/retencka/mqtt.py`).

Do verze 1.21 měl hub místo MQTT REST API na portu 8081 s tokenem a YAML pro Home Assistant;
to bylo odstraněno. Starou konfiguraci `rest:` z `configuration.yaml` smažte.
