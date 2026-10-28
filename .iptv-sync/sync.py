#!/usr/bin/env python3
"""
sync.py - keep n86cc/IPTV playlists in step with iptv-org, keeping Bro's format.

Playlists (target file <- iptv-org source):
  iptv-org-dansk-all.m3u  <- https://iptv-org.github.io/iptv/countries/dk.m3u
        every entry gets group-title="All", header #EXTM3U
  iptv-org-index-all.m3u  <- https://iptv-org.github.io/iptv/index.m3u
        every channel written once as group-title="All" and then once per
        category (upstream "A;B;C" split on ';', "Undefined" -> "General"),
        header: his current header line is kept (upstream dropped x-tvg-url
        after 2026-10-13; it was #EXTM3U x-tvg-url=".../guide.xml.gz" then)

  sport.m3u               <- https://iptv-org.github.io/iptv/categories/sports.m3u
        group-title removed and group-title="Sport" put right after the
        duration (#EXTINF:-1 group-title="Sport" tvg-id=...), header as upstream

Merge policy (default):
  * channels he already has: URL, logo, user-agent/referrer, EXTVLCOPT and
    name follow upstream; categories follow upstream.
  * BUT any field he changed by hand (detected against the last generated
    "base" copy in --state) is kept as he made it. Channels he added himself
    are kept. Channels he deleted (present in base, missing in his file) are
    not re-added.
  * new upstream channels are added in upstream (alphabetical) position.
  * channels gone from upstream are KEPT and listed in the report
    (--drop-dead deletes only those whose stream no longer answers,
     --drop-removed deletes all of them).

Usage:
  python3 sync.py                       # fetch his files + upstream, write ./out
  python3 sync.py --repo-dir ~/IPTV     # use a local clone of n86cc/IPTV
  python3 sync.py --upstream-dir DIR    # use local upstream copies
                                        # (DIR/index.m3u, DIR/countries/dk.m3u
                                        #  or DIR/countries_dk.m3u)
  python3 sync.py --drop-removed        # drop channels removed upstream
  python3 sync.py --probe-removed       # also test if removed-upstream streams answer
  python3 sync.py --drop-dead           # drop removed-upstream channels that are dead
  python3 sync.py --pure                # plain regeneration from upstream only
  python3 sync.py --accept              # after he has pushed out/*.m3u:
                                        # store them as the new base in --state
Line endings follow his current file (dansk = CRLF like upstream, index = LF).
  python3 sync.py --in-place            # GitHub Actions: run in repo checkout,
                                        # rewrite playlists in place, keep base in
                                        # .iptv-sync/state/ (created on first run
                                        # from his current files)
Outputs: out/<file>.m3u and out/report.json (+ printed summary).
Stdlib only, deterministic (no timestamps in playlists).
"""
import argparse, json, os, re, shutil, sys, urllib.request

UP = "https://iptv-org.github.io/iptv/"
RAW = "https://raw.githubusercontent.com/n86cc/IPTV/main/"
TARGETS = {
    "iptv-org-dansk-all.m3u": {"src": "countries/dk.m3u", "cats": False},
    "iptv-org-index-all.m3u": {"src": "index.m3u", "cats": True},
    "sport.m3u": {"src": "categories/sports.m3u", "cats": False, "main": "Sport", "group_first": True},
}
CAT_MAP = {"Undefined": "General"}
EXTINF = re.compile(r'^#EXTINF:(-?\d+)((?:\s+[\w-]+="[^"]*")*)\s*,(.*)$')
GROUP = re.compile(r'\s+group-title="[^"]*"')
FIELDS = ("attrs", "name", "extra", "url", "groups")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "n86cc-iptv-sync/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8")


def read(path_or_url):
    if re.match(r"https?://", path_or_url):
        return fetch(path_or_url)
    with open(path_or_url, encoding="utf-8", newline="") as f:
        return f.read()


def parse_blocks(text):
    lines = text.replace("\r\n", "\n").split("\n")
    header = lines[0] if lines and lines[0].startswith("#EXTM3U") else "#EXTM3U"
    blocks, cur = [], None
    for l in lines[1:]:
        if l.startswith("#EXTINF"):
            cur = [l]
        elif cur is not None and l.strip():
            cur.append(l)
            if not l.startswith("#"):
                blocks.append(cur)
                cur = None
    return header, blocks


def split_block(b):
    m = EXTINF.match(b[0])
    if not m:
        raise ValueError("bad EXTINF: " + b[0])
    dur, attrs, name = m.groups()
    g = re.search(r'group-title="([^"]*)"', attrs)
    group = g.group(1) if g else ""
    attrs = GROUP.sub("", attrs)
    return dict(dur=dur, attrs=attrs, name=name, extra=tuple(b[1:-1]), url=b[-1], group=group)


def tvg_id(u):
    m = re.search(r'tvg-id="([^"]*)"', u["attrs"])
    return m.group(1) if m else ""


def norm_name(n):
    n = re.sub(r"\s*\(\d+[pi]\)|\s*\[[^\]]*\]", "", n)
    return re.sub(r"\s+", " ", n).strip().casefold()


def units_from_upstream(text, cats):
    header, blocks = parse_blocks(text)
    out = []
    for b in blocks:
        s = split_block(b)
        groups = ()
        if cats:
            gs = [CAT_MAP.get(g, g) for g in s["group"].split(";") if g] or ["General"]
            groups = tuple(dict.fromkeys(gs))
        s["groups"] = groups
        del s["group"]
        out.append(s)
    return header, out


def units_from_target(text, cats, main="All"):
    """His format: main-group block (All/Sport) followed by its category blocks (same channel)."""
    header, blocks = parse_blocks(text)
    out, cur = [], None
    for b in blocks:
        s = split_block(b)
        same = cur is not None and all(cur[k] == s[k] for k in ("dur", "attrs", "name", "extra", "url"))
        if cats and same and s["group"] != main:
            cur["groups"] += (s["group"],)
            continue
        g = s.pop("group")
        s["groups"] = () if g == main else (g,)
        if g != main:
            s["_noall"] = True  # entry without the main group block (manual edit)
        out.append(s)
        cur = s
    return header, out


def render(header, units, cats, nl="\n", main="All", group_first=False):
    out = [header]
    for u in units:
        groups = ([main] if not u.get("_noall") else []) + list(u["groups"])
        for g in groups:
            if group_first:
                out.append(f'#EXTINF:{u["dur"]} group-title="{g}"{u["attrs"]},{u["name"]}')
            else:
                out.append(f'#EXTINF:{u["dur"]}{u["attrs"]} group-title="{g}",{u["name"]}')
            out.extend(u["extra"])
            out.append(u["url"])
    return nl.join(out) + nl


def sig(u):
    return (u["dur"], u["attrs"], u["name"], u["extra"], u["url"], u["groups"], bool(u.get("_noall")))


def match(A, B):
    """Pair units of A with units of B. Returns dict index_a -> index_b."""
    pa, pb = {}, set()
    passes = [
        lambda u: ("url", u["url"]),
        lambda u: ("idname", tvg_id(u), norm_name(u["name"])) if tvg_id(u) else None,
        lambda u: ("id", tvg_id(u)) if tvg_id(u) else None,
        lambda u: ("name", norm_name(u["name"])) if not tvg_id(u) else None,
    ]
    for i, key in enumerate(passes):
        idx = {}
        for j, u in enumerate(B):
            if j in pb:
                continue
            k = key(u)
            if k is not None:
                idx.setdefault(k, []).append(j)
        cand = {}
        for a, u in enumerate(A):
            if a in pa:
                continue
            k = key(u)
            if k is not None and k in idx:
                cand.setdefault(k, []).append(a)
        for k, alist in cand.items():
            blist = [j for j in idx[k] if j not in pb]
            if i >= 2 and (len(alist) != 1 or len(blist) != 1):
                continue  # loose keys only when unambiguous
            for a, b in zip(alist, blist):
                pa[a] = b
                pb.add(b)
    return pa


def probe(url, timeout=8):
    """True if the stream answers with HTTP < 400 (reads at most 2 KB)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2048)
            return r.status < 400
    except Exception:
        return False


def label(u):
    return f'{u["name"]} [{tvg_id(u) or "-"}] {u["url"]}'


def sync_one(fname, cfg, cur_text, up_text, base_text, drop_removed, pure, probe_removed=False, drop_dead=False):
    cats = cfg["cats"]
    nl = "\r\n" if "\r\n" in cur_text.split("\n", 1)[0] + "\n" else "\n"  # keep his line endings
    up_header, U = units_from_upstream(up_text, cats)
    main, gf = cfg.get("main", "All"), cfg.get("group_first", False)
    cur_header, C = units_from_target(cur_text, cats, main)
    if pure:
        return render(up_header, U, cats, nl, main, gf), {"mode": "pure", "upstream": len(U)}
    if base_text is None:
        base_text = cur_text  # first run: assume his file == our base
    _, B = units_from_target(base_text, cats, main)

    # manual edits: his units vs base
    cb = match(C, B)
    bsig = [sig(b) for b in B]
    manual = {}  # c index -> set of fields he changed (None = his own channel)
    for ci, c in enumerate(C):
        if ci not in cb:
            manual[ci] = None
        elif sig(c) != bsig[cb[ci]]:
            b = B[cb[ci]]
            manual[ci] = {f for f in FIELDS + ("dur", "_noall") if c.get(f) != b.get(f)}
    deleted = [B[j] for j in range(len(B)) if j not in set(cb.values())]

    cu = match(C, U)
    uc = {v: k for k, v in cu.items()}
    dmatch = match(U, deleted) if deleted else {}

    stats = dict(upstream=len(U), current=len(C), unchanged=0, updated=0, added=0,
                 removed_upstream=0, kept_manual=0, skipped_user_deleted=0)
    changes = {"updated": [], "added": [], "removed_upstream": [], "manual": [], "skipped_user_deleted": []}
    result, placed = [], {}
    for ui, u in enumerate(U):
        if ui in uc:
            ci = uc[ui]
            c = C[ci]
            new = dict(u)
            m = manual.get(ci, set())
            if m is None:
                m = set(FIELDS) | {"dur", "_noall"}
            for f in m:
                if f in c:
                    new[f] = c[f]
                elif f in new:
                    new.pop(f)
            if m:
                stats["kept_manual"] += 1
                changes["manual"].append({"channel": label(c), "fields": sorted(m)})
            if sig(new) == sig(c):
                stats["unchanged"] += 1
            else:
                stats["updated"] += 1
                diff = {f: [str(c.get(f)), str(new.get(f))] for f in FIELDS if c.get(f) != new.get(f)}
                changes["updated"].append({"channel": label(c), "changes": diff})
            result.append(new)
            placed[ci] = len(result) - 1
        elif ui in dmatch:
            stats["skipped_user_deleted"] += 1
            changes["skipped_user_deleted"].append(label(u))
        else:
            result.append(dict(u))
            stats["added"] += 1
            changes["added"].append(label(u))

    # channels in his file with no upstream match
    alive_map = {}
    if probe_removed or drop_dead:
        from concurrent.futures import ThreadPoolExecutor
        urls = sorted({C[ci]["url"] for ci in range(len(C)) if ci not in cu})
        with ThreadPoolExecutor(32) as ex:
            alive_map = dict(zip(urls, ex.map(probe, urls)))
    inserts = []
    for ci, c in enumerate(C):
        if ci in cu:
            continue
        own = manual.get(ci, 0) is None and base_text is not cur_text
        if not own:
            stats["removed_upstream"] += 1
            alive = alive_map.get(c["url"])
            changes["removed_upstream"].append(label(c) + ("" if alive is None else (" ALIVE" if alive else " DEAD")))
            if alive is False:
                stats["removed_dead"] = stats.get("removed_dead", 0) + 1
            if drop_removed or (drop_dead and alive is False):
                stats["dropped"] = stats.get("dropped", 0) + 1
                continue
        else:
            stats["kept_manual"] += 1
            changes["manual"].append({"channel": label(c), "fields": ["own channel"]})
        # anchor: nearest previous unit of his file that is placed
        anchor = next((placed[k] for k in range(ci - 1, -1, -1) if k in placed), -1)
        inserts.append((anchor, ci, c))
    # insert keeping order (stable for several after the same anchor)
    by_anchor = {}
    for anchor, ci, c in inserts:
        by_anchor.setdefault(anchor, []).append(c)
    final = list(by_anchor.get(-1, []))
    for i, u in enumerate(result):
        final.append(u)
        final.extend(by_anchor.get(i, []))
    stats["final"] = len(final)
    stats["kept_removed"] = stats["removed_upstream"] - stats.get("dropped", 0)
    header = cur_header  # his header (x-tvg-url etc.) is kept; upstream header only reported
    return render(header, final, cats, nl, main, gf), {"stats": stats, "changes": changes,
                                         "header_up": up_header, "header_cur": cur_header}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--repo-dir", help="local clone of n86cc/IPTV (default: fetch raw from GitHub main)")
    ap.add_argument("--upstream-dir", help="local iptv-org playlist copies (default: fetch from iptv-org.github.io)")
    ap.add_argument("--state", default=os.path.join(here, "state"), help="dir with last generated base copies")
    ap.add_argument("--out", default=os.path.join(here, "out"))
    ap.add_argument("--drop-removed", action="store_true")
    ap.add_argument("--probe-removed", action="store_true", help="test if channels gone upstream still answer (report only)")
    ap.add_argument("--drop-dead", action="store_true", help="drop channels gone upstream whose stream does not answer")
    ap.add_argument("--pure", action="store_true", help="plain regeneration from upstream")
    ap.add_argument("--accept", action="store_true", help="copy out/*.m3u into --state as new base and exit")
    ap.add_argument("--in-place", action="store_true",
                    help="CI mode: read and write the playlists in --repo-dir (default .), "
                         "state in .iptv-sync/state, report in .iptv-sync/report.json")
    ap.add_argument("--only", choices=list(TARGETS), action="append")
    a = ap.parse_args()

    report_path = None
    if a.in_place:
        a.repo_dir = a.repo_dir or "."
        a.out = a.repo_dir
        if a.state == os.path.join(here, "state"):
            a.state = os.path.join(a.repo_dir, ".iptv-sync", "state")
        report_path = os.path.join(a.repo_dir, ".iptv-sync", "report.json")
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.state, exist_ok=True)
    names = a.only or list(TARGETS)
    if a.accept:
        for n in names:
            shutil.copyfile(os.path.join(a.out, n), os.path.join(a.state, n + ".base"))
            print("base updated:", n)
        return
    report = {}
    for n in names:
        cfg = TARGETS[n]
        cur = read(os.path.join(a.repo_dir, n) if a.repo_dir else RAW + n)
        if a.upstream_dir:
            p = os.path.join(a.upstream_dir, cfg["src"])
            if not os.path.exists(p):
                p = os.path.join(a.upstream_dir, cfg["src"].replace("/", "_"))
            up = read(p)
        else:
            up = read(UP + cfg["src"])
        bp = os.path.join(a.state, n + ".base")
        base = read(bp) if os.path.exists(bp) else None
        text, rep = sync_one(n, cfg, cur, up, base, a.drop_removed, a.pure, a.probe_removed, a.drop_dead)
        with open(os.path.join(a.out, n), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if a.in_place:  # what we wrote becomes the base for detecting his later manual edits
            with open(bp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        report[n] = rep
        s = rep.get("stats", rep)
        print(n, json.dumps(s, ensure_ascii=False))
    with open(report_path or os.path.join(a.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
