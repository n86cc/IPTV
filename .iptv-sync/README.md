# IPTV sync

`.github/workflows/iptv-sync.yml` kører `sync.py --in-place` hver dag kl. 02:00 UTC
(03:00 dansk vintertid, 04:00 sommertid) og kan startes manuelt under Actions.

- `iptv-org-dansk-all.m3u` <- iptv-org `countries/dk.m3u`, alle kanaler i gruppen "All".
- `iptv-org-index-all.m3u` <- iptv-org `index.m3u`, hver kanal i "All" + hver af dens kategorier
  (`;` splittes, "Undefined" -> "General"). Headeren (x-tvg-url) bevares.
- URL'er, navne, logoer og user-agents følger upstream. Nye kanaler tilføjes.
- Kanaler der er fjernet hos iptv-org BEHOLDES (se `report.json`). `--drop-dead` / `--drop-removed` kan slå det om.
- Dine egne rettelser bevares: `state/` indeholder den sidst genererede version, og felter du har
  ændret i hånden (navn, kategori, URL ...), kanaler du har tilføjet, og kanaler du har slettet, respekteres.
  `state/` oprettes ved første kørsel ud fra de nuværende filer.
