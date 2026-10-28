#!/usr/bin/env python3
"""
build_guide.py - build guide.xml.gz (XMLTV) for Bro's three playlists.

The playlists use iptv-org tvg-ids (e.g. "DR1.dk@SD", "7thHeaven.se@DK"), which
no public EPG uses. This script takes public guides and rewrites their channel
ids to the tvg-ids in the playlists, so Kodi IPTV Simple can match them 1:1.
The playlists themselves are only read, never changed.

Matching (first hit wins, per tvg-id):
  1. Pluto TV   : stream URL jmp2.uk/plu-<id>   -> i.mjh.nz/PlutoTV/all.xml.gz  channel <id>
  2. Samsung TV+: stream URL jmp2.uk/stvp-<id>  -> i.mjh.nz/SamsungTVPlus/all.xml.gz channel <id>
  3. tvg-id without "@feed", compared on letters/digits only (DR1.dk@SD ~ DR1.dk,
     TV2Bornholm.dk ~ TV.2.Bornholm.dk; epgshare "us2"-style suffixes count as "us")
     against epgshare01 DK1 first, then epgshare01 ALL_SOURCES1.
Runs every 4 hours (.github/workflows/epg-guide.yml) because the Pluto/Samsung
guides only cover roughly the next 12-18 hours. The result is force-pushed as the
single commit of branch 'epg' (no history), so the repo does not grow.
Only channels present in the playlists are written, and only programmes that end
after now-12h and start before now+7d.

Usage: python3 build_guide.py [--repo-dir .] [--out guide.xml.gz] [--cache DIR]
"""
import argparse, collections, datetime as dt, gzip, io, os, re, sys, time, urllib.request, json
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

LISTS = ["iptv-org-dansk-all.m3u", "sport.m3u", "iptv-org-index-all.m3u"]
SOURCES = [  # (key, url)
    ("pluto", "https://i.mjh.nz/PlutoTV/all.xml.gz"),
    ("samsung", "https://i.mjh.nz/SamsungTVPlus/all.xml.gz"),
    ("dk1", "https://epgshare01.online/epgshare01/epg_ripper_DK1.xml.gz"),
    ("all", "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"),
]
ID_SOURCES = ["dk1", "all"]          # matched on normalised tvg-id, in this order
URL_SOURCES = {"plu": "pluto", "stvp": "samsung"}
PAST = dt.timedelta(hours=12)
FUTURE = dt.timedelta(days=7)
MIN_PROGRAMMES = 20000
UA = "Mozilla/5.0 (n86cc/IPTV guide builder)"


def norm_id(i):
    i = i.split("@")[0].lower()
    m = re.match(r"^(.*)\.([a-z]{2})\d*$", i)
    if m:
        i = m.group(1) + "." + m.group(2)
    return re.sub(r"[^a-z0-9]", "", i)


def read_lists(repo):
    chans = collections.OrderedDict()      # tvg-id -> {"name":..., "urlkeys": set(), "lists": set()}
    for lst in LISTS:
        lines = open(os.path.join(repo, lst), encoding="utf-8", errors="replace").read().splitlines()
        for n, line in enumerate(lines):
            if not line.startswith("#EXTINF"):
                continue
            m = re.search(r'tvg-id="([^"]+)"', line)
            if not m:
                continue
            tid = m.group(1)
            name = line.rsplit(",", 1)[-1].strip()
            url = ""
            for nxt in lines[n + 1:n + 6]:
                if nxt and not nxt.startswith("#"):
                    url = nxt.strip(); break
            c = chans.setdefault(tid, {"name": name, "urlkeys": set(), "lists": set()})
            c["lists"].add(lst)
            u = re.search(r"jmp2\.uk/(plu|stvp)-([A-Za-z0-9_-]+)", url)
            if u:
                c["urlkeys"].add((URL_SOURCES[u.group(1)], u.group(2)))
    return chans


def fetch(key, url, cache):
    path = os.path.join(cache, key + ".xml.gz")
    for attempt in range(3):
        try:
            t = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            print(f"  {key}: {os.path.getsize(path)/1e6:.1f} MB in {time.time()-t:.1f}s", flush=True)
            return path
        except Exception as e:
            print(f"  {key}: attempt {attempt+1} failed: {e}", flush=True)
            time.sleep(10)
    return None


def opener(path):
    with open(path, "rb") as f:
        magic = f.read(2)
    return gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")


def parse_time(s):
    s = (s or "").strip()
    try:
        t = dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    off = s[14:].strip()
    if re.match(r"^[+-]\d{4}$", off):
        sign = 1 if off[0] == "+" else -1
        t -= sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
    return t.replace(tzinfo=dt.timezone.utc)


def channel_ids(path):
    ids = set()
    for _, el in ET.iterparse(opener(path)):
        if el.tag == "channel":
            ids.add(el.get("id", ""))
            el.clear()
        elif el.tag == "programme":
            el.clear()
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--out", default="guide.xml.gz")
    ap.add_argument("--cache", default="/tmp/epg-cache")
    ap.add_argument("--report", default=None)
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(a.cache, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    lo, hi = now - PAST, now + FUTURE

    chans = read_lists(a.repo_dir)
    print(f"{len(chans)} unique tvg-ids in the lists", flush=True)

    print("Downloading sources:", flush=True)
    paths = {k: fetch(k, u, a.cache) for k, u in SOURCES}

    # source channel (key, id) -> list of tvg-ids
    want = collections.defaultdict(list)
    matched = {}
    for tid, c in chans.items():
        for key, sid in sorted(c["urlkeys"]):
            if paths.get(key):
                want[(key, sid)].append(tid); matched[tid] = key; break
    for key in ID_SOURCES:
        if not paths.get(key):
            continue
        byn = {}
        for sid in channel_ids(paths[key]):
            byn.setdefault(norm_id(sid), sid)
        for tid in chans:
            if tid in matched:
                continue
            sid = byn.get(norm_id(tid))
            if sid:
                want[(key, sid)].append(tid); matched[tid] = key

    # stream programmes, rewrite channel id, keep window
    tmp = a.out + ".tmp"
    have = collections.Counter()           # tvg-id -> programmes written
    today = collections.Counter()
    dk_today = now.astimezone(dt.timezone(dt.timedelta(hours=1))).date()
    nprog = 0
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write('<tv generator-info-name="n86cc/IPTV build_guide.py" '
                  f'date="{now:%Y%m%d%H%M%S} +0000">\n')
        for tid, c in chans.items():
            if tid in matched:
                out.write(f'<channel id={quoteattr(tid)}><display-name>{escape(c["name"])}</display-name></channel>\n')
        for key, _ in SOURCES:
            if not paths.get(key):
                continue
            keyed = {sid: tids for (k, sid), tids in want.items() if k == key}
            if not keyed:
                continue
            for _, el in ET.iterparse(opener(paths[key])):
                if el.tag == "channel":
                    el.clear(); continue
                if el.tag != "programme":
                    continue
                tids = keyed.get(el.get("channel", ""))
                if tids:
                    s, e = parse_time(el.get("start")), parse_time(el.get("stop"))
                    if s and (e or s) > lo and s < hi:
                        for tid in tids:
                            el.set("channel", tid)
                            out.write(ET.tostring(el, encoding="unicode").strip() + "\n")
                            have[tid] += 1; nprog += 1
                            if s.astimezone(dt.timezone(dt.timedelta(hours=1))).date() == dk_today or (s <= now and (e or s) >= now):
                                today[tid] += 1
                el.clear()
        out.write("</tv>\n")
    os.replace(tmp, a.out)

    rep = {"built_utc": now.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - t0),
           "size_bytes": os.path.getsize(a.out), "programmes": nprog,
           "sources": {k: bool(p) for k, p in paths.items()},
           "matched_by_source": dict(collections.Counter(matched.values())), "lists": {}}
    for lst in LISTS:
        ids = [t for t, c in chans.items() if lst in c["lists"]]
        rep["lists"][lst] = {"tvg_ids": len(ids), "with_guide": sum(1 for t in ids if have[t]),
                             "with_programmes_today": sum(1 for t in ids if today[t])}
    print(json.dumps(rep, indent=1))
    if a.report:
        json.dump(rep, open(a.report, "w"), indent=1)
    if nprog < MIN_PROGRAMMES:
        sys.exit(f"Only {nprog} programmes - refusing to publish a near-empty guide")


if __name__ == "__main__":
    main()
