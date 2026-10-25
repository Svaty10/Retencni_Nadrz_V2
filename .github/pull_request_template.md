## Summary

<!-- Briefly describe what this pull request changes and why. -->

## Affected Components

- [ ] `uzel/` — Sensor node C firmware (ATtiny1624 / CC1101 / SHT40)
- [ ] `server/retencka/` — Python hub backend / HTTP server / database / radio
- [ ] `server/static/` — Web UI (HTML / CSS / JS)
- [ ] `system/` — Raspberry Pi scripts / root helpers / systemd units
- [ ] `docs/` — Documentation & changelog

## Verification Checklist

- [ ] Server unit & integration tests pass (`cd server && python -m unittest discover -s tests -v`)
- [ ] Firmware builds and host self-tests pass, if `uzel/` was modified (`cd uzel && make`)
- [ ] `CHANGELOG.md` and relevant documentation (`README.md`, `docs/*.md`) are updated in sync with the changes
- [ ] Privilege boundaries (`O_NOFOLLOW`, atomic writes, CSRF, input validation) are preserved
