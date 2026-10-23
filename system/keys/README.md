Sem patří **veřejný** klíč pro ověřování aktualizací: `release.pub` (Ed25519, PEM).

    openssl genpkey -algorithm ed25519 -out release.key        # soukromý – NIKDY do gitu
    openssl pkey -in release.key -pubout -out release.pub      # ten zkopírovat sem a commitnout

Soukromý `release.key` patří do GitHub secretu `RELEASE_SIGNING_KEY`.
Celý postup (secret, vydání verze, řešení problémů): [docs/instalace.md, oddíl 5](../../docs/instalace.md#5-aktualizace-z-githubu).

Bez `release.pub` zařízení automatické aktualizace odmítne (bezpečné výchozí chování).
