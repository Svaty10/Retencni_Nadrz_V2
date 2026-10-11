Sem patří **veřejný** klíč pro ověřování aktualizací: `release.pub` (Ed25519, PEM).

    openssl genpkey -algorithm ed25519 -out release.key        # soukromý – NIKDY do gitu
    openssl pkey -in release.key -pubout -out system/keys/release.pub

Bez `release.pub` zařízení automatické aktualizace odmítne (bezpečné výchozí chování).
