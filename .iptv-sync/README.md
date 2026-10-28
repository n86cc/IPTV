# IPTV sync

`.github/workflows/iptv-sync.yml` kører `sync.py --in-place --check-links` hver dag kl. 02:00 UTC
(03:00 dansk vintertid, 04:00 sommertid). Du kan også starte det manuelt under Actions.

## Kilder og format
- `iptv-org-dansk-all.m3u` kommer fra iptv-org `countries/dk.m3u`. Alle kanaler ligger i gruppen "All", og filen har CRLF-linjeskift.
- `iptv-org-index-all.m3u` kommer fra iptv-org `index.m3u`. Hver kanal står i "All" og i hver af sine kategorier
  (`;` splittes, "Undefined" bliver til "General"). Headeren med x-tvg-url bevares.
- `sport.m3u` er foreningsmængden af `categories/sports.m3u` og alle kanaler i `index.m3u`, der har "Sports"
  blandt kategorierne. Dubletter fjernes efter URL. Rækkefølgen følger sports.m3u, og de ekstra kanaler kommer bagefter.
  group-title fjernes, og `group-title="Sport"` sættes lige efter `#EXTINF:-1`. Filen har LF-linjeskift.

## Opdatering
- URLer, navne, logoer og user-agents følger upstream, og nye kanaler tilføjes.
- Dine egne rettelser bevares. `state/<liste>.base` indeholder den rene upstream-version, så felter du selv
  har ændret (navn, kategori, URL ...), kanaler du selv har tilføjet, og kanaler du har slettet, bliver respekteret.
- Kanaler, som iptv-org har fjernet, beholdes, så længe streamen virker. De gennemgår samme link-tjek som alle andre.

## Tjek af døde links (alle kanaler i alle tre lister)
- Hver URL tjekkes kun én gang pr. nat, også selvom den står i flere lister. Tjekket er en GET, der læser de første 512 bytes.
  Det bruger kanalens egen `http-user-agent`/`http-referrer` (EXTINF-attributter eller `#EXTVLCOPT`) og ellers en
  Chrome-UA. Redirects følges op til 5 gange. Der køres 128 tjek parallelt med højst 12 pr. host, timeout er 10 s, og hele tjekket har et budget på 30 min.
- **Levende:** HTTP-status under 400 samt 401, 403 og 451. Det betyder typisk geo- eller tokenspærring,
  og kanalen virker ofte i din egen afspiller.
- **Usikker:** 429, URLer der ikke er http(s), og URLer der ikke nås inden for tidsbudgettet. Tælleren ændres ikke.
- **Død:** 404, 410, andre 4xx, 5xx, timeout og forbindelses- eller DNS-fejl. Den samme nat prøves en død URL igen
  2 gange (efter 5 s og efter 20 s, med 15 s timeout).
- `state/health.json` tæller for hver URL, hvor mange nætter i træk den har fejlet. En kanal **udelades først efter
  2 fejlede nætter i træk**. Når URLen virker igen, nulstilles tælleren.
- Udeladte kanaler gemmes i `state/<liste>.hidden` med dine rettelser og tjekkes igen hver nat. De tæller
  ikke som slettet af dig. De **kommer automatisk tilbage**, når streamen svarer igen (og kanalen stadig findes upstream
  eller er din egen). En udeladt kanal, som heller ikke længere findes upstream, glemmes efter 14 fejlede nætter.
- `report.json` viser nattens tal: antal fra upstream, udeladt som død, endeligt antal, køretid og ændringer.
- `seed/` bruges én gang til at sætte tidligere tabte kanaler (fx AXN Asia Malaysia) ind igen, hvis de virker.
  Mappen slettes efter brug.

## EPG-guide (guide.xml.gz)
- `.github/workflows/epg-guide.yml` kører `build_guide.py` hver 4. time (kl. xx:20 UTC) og kan også startes manuelt.
  Den kører så tit, fordi Pluto- og Samsung-guiderne kun dækker ca. 12-18 timer frem.
- Guiden ligger på branchen `epg` som eneste commit (den force-pushes, så repoet ikke vokser med 12 MB pr. kørsel):
  **https://raw.githubusercontent.com/n86cc/IPTV/epg/guide.xml.gz**. Tallene står i `guide-report.json` samme sted.
- Brug URLen som `epgUrl` i alle tre IPTV Simple-instanser i Kodi. Listerne og deres `x-tvg-url`-header ændres **ikke**.
- Kilder: i.mjh.nz Pluto TV og Samsung TV Plus samt epgshare01 DK1 og ALL_SOURCES1.
- Kanal-id'erne i guiden omskrives til listernes tvg-id'er, så Kodi kan matche dem 1:1:
  1. Pluto og Samsung matches på stream-URLen (`jmp2.uk/plu-<id>`, `jmp2.uk/stvp-<id>`).
  2. Andre kanaler matches på tvg-id uden `@feed`, hvor kun bogstaver og tal sammenlignes. Fx `DR1.dk@SD` = `DR1.dk` og
     `ORF1.at@SD` = `ORF.1.at`. DK1 bruges før ALL_SOURCES.
- Kun kanaler fra de tre lister kommer med, og kun programmer fra 12 timer tilbage til 7 dage frem.
  Hvis guiden har under 20.000 programmer, bliver den ikke udgivet.
