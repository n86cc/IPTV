#!/usr/bin/env python3
"""
sync.py - keep n86cc/IPTV playlists in step with iptv-org, keeping Bro's format,
          and publish only channels whose stream answers.

Playlists (target file <- iptv-org source):
  iptv-org-dansk-all.m3u  <- countries/dk.m3u
        every entry gets group-title="Danske kanaler", header #EXTM3U (CRLF kept)
  iptv-org-index-all.m3u  <- index.m3u
        every channel written once as group-title="Alle IPTV-kanaler" and then once per
        category (upstream "A;B;C" split on ';', "Undefined" -> "General");
        his header line (#EXTM3U x-tvg-url=...) is kept
  (main group names are TARGETS[...]["main"]; the old name "All" is still read)
  sport.m3u               <- categories/sports.m3u  UNION  index.m3u entries
        with "Sports" among their ';' categories whose URL is missing from
        sports.m3u (dedup by URL, sports.m3u order); group-title removed and
        group-title="Sport" put right after "#EXTINF:-1"; header as upstream

Merge (per list, every night):
  * channels he has: URL, name, logo, user-agent/referrer, EXTVLCOPT and
    categories follow upstream, EXCEPT fields he changed by hand. Manual edits
    are detected against state/<list>.base, which holds the plain
    upstream-derived version of every channel (never his overrides), so an
    edit stays detected night after night.
  * channels he added himself are kept; channels he deleted are remembered in
    the base (tombstone) and never re-added while upstream has them.
  * new upstream channels are added in upstream position.
  * channels removed upstream are kept (subject to the link check below);
    --drop-removed deletes them.

Link check (--check-links, used by the workflow):
  * every candidate channel of all three lists is checked; identical URLs are
    checked once. GET with the channel's own http-user-agent / http-referrer
    (EXTINF attrs or #EXTVLCOPT), else a Chrome user agent; TLS not verified
    (players don't either); 128 parallel workers, max 12 at a time per host
    (redirects followed by hand, each hop counts for its own host); timeout
    10 s, reads the first bytes; whole check capped at 30 min (anything not
    reached counts as UNSURE).
  * ALIVE  : final HTTP status < 400, and also 401/403/451 (token/geo
             protected - it may work from Denmark/Kodi, so it is kept)
    UNSURE : 429 (rate limited) and non-http URLs (rtmp, udp ...) - kept,
             fail counter untouched
    DEAD   : 404/410/other 4xx, 5xx, DNS/connection errors, timeouts.
  * a failing URL is retried twice in the same run (after 5 s and 20 s, 15 s
    timeout). Still failing = one failed night.
  * state/health.json keeps a per-URL count of consecutive failed nights.
    A channel is LEFT OUT when its URL has failed 2 nights in a row
    (HIDE_AFTER). It is reset to 0 the first time it answers again.
  * left-out channels are stored in state/<list>.hidden (with his edits) and are
    re-checked every night; they come back automatically when they answer.
    They are NOT treated as deletions by him. A left-out channel that is also
    gone upstream is forgotten after 14 failed nights in a row (PURGE_AFTER).

Usage:
  python3 sync.py --in-place --check-links   # GitHub Actions (repo checkout)
  python3 sync.py                            # fetch repo + upstream, write ./out
  python3 sync.py --repo-dir ~/IPTV --upstream-dir DIR --out DIR --state DIR
  python3 sync.py --pure                     # plain regeneration from upstream
  python3 sync.py --only sport.m3u ...
Line endings follow his current file (dansk = CRLF, index/sport = LF).
In-place mode writes playlists in the repo, state in .iptv-sync/state/ and
.iptv-sync/report.json. A one-time .iptv-sync/seed/<list>.m3u is merged into
the hidden set (fail count 1) and then deleted.
Stdlib only, deterministic output (no timestamps in playlists or state).
"""
import argparse, json, os, re, ssl, sys, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

UP = "https://iptv-org.github.io/iptv/"
RAW = "https://raw.githubusercontent.com/n86cc/IPTV/main/"
TARGETS = {
    "iptv-org-dansk-all.m3u": {"src": "countries/dk.m3u", "cats": False, "main": "Danske kanaler"},
    "iptv-org-index-all.m3u": {"src": "index.m3u", "cats": True, "main": "Alle IPTV-kanaler"},
    "sport.m3u": {"src": "categories/sports.m3u", "union_from": ("index.m3u", "Sports"),
                  "cats": False, "main": "Sport", "group_first": True},
}
# Former main-group names. Files/state written before the rename still use them;
# they are read as the main group and written back with the list's current name.
LEGACY_MAIN = ("All",)
HIDE_AFTER = 2      # consecutive failed nights before a channel is left out
PURGE_AFTER = 14    # failed nights before a left-out, gone-upstream channel is forgotten
WORKERS, PER_HOST, TIMEOUT = 128, 12, 10
TIME_BUDGET = 30 * 60  # seconds for the whole link check; leftovers count as "unsure"
RETRIES = ((5, 15), (20, 15))  # (wait seconds, timeout) for each retry round
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
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
    """His format: main-group block (Danske kanaler / Alle IPTV-kanaler / Sport) followed by
    its category blocks (same channel). LEGACY_MAIN names count as the main group too."""
    header, blocks = parse_blocks(text)
    mains = {main, *LEGACY_MAIN}
    out, cur = [], None
    for b in blocks:
        s = split_block(b)
        same = cur is not None and all(cur[k] == s[k] for k in ("dur", "attrs", "name", "extra", "url"))
        if cats and same and s["group"] not in mains:
            cur["groups"] += (s["group"],)
            continue
        g = s.pop("group")
        s["groups"] = () if g in mains else (g,)
        if g not in mains:
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


def load_upstream(src, upstream_dir=None):
    if upstream_dir:
        p = os.path.join(upstream_dir, src)
        if not os.path.exists(p):
            p = os.path.join(upstream_dir, src.replace("/", "_"))
        return read(p)
    return read(UP + src)


def union_category(main_text, index_text, cat):
    """main list (e.g. categories/sports.m3u) + every entry of index.m3u whose
    ';'-separated group-title contains `cat` and whose URL is not already in the
    main list. Extra entries go right after their predecessor in index order
    (both lists are sorted the same way upstream), else at the end."""
    nl = "\r\n" if "\r\n" in main_text else "\n"
    header, mb = parse_blocks(main_text)
    _, ib = parse_blocks(index_text)
    urls = {b[-1] for b in mb}
    pos = {b[-1]: i for i, b in enumerate(mb)}
    after = {}
    prev = -1
    n_cat = extra = 0
    for b in ib:
        g = re.search(r'group-title="([^"]*)"', b[0])
        if not g or cat not in g.group(1).split(";"):
            continue
        n_cat += 1
        if b[-1] in urls:
            prev = pos[b[-1]]
            continue
        urls.add(b[-1])
        after.setdefault(prev, []).append(b)
        extra += 1
    out = [header] + [l for b in after.get(-1, []) for l in b]
    for i, b in enumerate(mb):
        out += b
        for e in after.get(i, []):
            out += e
    info = {"main_list": len(mb), "index_" + cat: n_cat, "only_in_index": extra, "union": len(mb) + extra}
    return nl.join(out) + nl, info




def label(u):
    return f'{u["name"]} [{tvg_id(u) or "-"}] {u["url"]}'


# ---------------------------------------------------------------- link check
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_host_sem = {}
_host_lock = threading.Lock()


def headers_for(u):
    h = {}
    txt = u["attrs"] + " " + " ".join(u["extra"])
    m = re.search(r'http-user-agent="([^"]*)"', u["attrs"]) or re.search(r"#EXTVLCOPT:http-user-agent=(.*)", "\n".join(u["extra"]))
    h["User-Agent"] = m.group(1).strip() if m else CHROME_UA
    m = re.search(r'http-referrer="([^"]*)"', u["attrs"]) or re.search(r"#EXTVLCOPT:http-referrer=(.*)", "\n".join(u["extra"]))
    if m and m.group(1).strip():
        h["Referer"] = m.group(1).strip()
    return h


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ctx))
_deadline = [float("inf")]


def _sem(host):
    with _host_lock:
        return _host_sem.setdefault(host or "", threading.BoundedSemaphore(PER_HOST))


def check_url(url, hdrs, timeout):
    """Return 'alive', 'unsure' or 'dead:<reason>'. Redirects are followed by
    hand (max 5 hops) so each hop only holds its own host's slot."""
    for hop in range(6):
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            return "unsure"
        if time.time() > _deadline[0]:
            return "unsure"  # out of time budget: don't count as a failure
        with _sem(p.hostname):
            try:
                req = urllib.request.Request(url, headers=dict(hdrs, Accept="*/*"))
                with _opener.open(req, timeout=timeout) as r:
                    r.read(512)
                    return "alive" if r.status < 400 else f"dead:{r.status}"
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    url = urllib.parse.urljoin(url, e.headers["Location"])
                    continue
                if e.code in (401, 403, 451):
                    return "alive"
                if e.code == 429:
                    return "unsure"
                return f"dead:{e.code}"
            except Exception as e:
                return "dead:" + type(e).__name__
    return "dead:too_many_redirects"


def check_all(jobs, log=lambda *x: print(*x, flush=True)):
    """jobs: {url: headers}. Returns {url: status}."""
    def interleave(urls):  # round-robin by host so per-host limits don't idle workers
        by = {}
        for u in sorted(urls):
            by.setdefault(urllib.parse.urlsplit(u).hostname or "", []).append(u)
        out, lists = [], list(by.values())
        i = 0
        while lists:
            lists = [l for l in lists if l]
            for l in lists:
                out.append(l.pop(0))
        return out

    t0 = time.time()
    _deadline[0] = t0 + TIME_BUDGET
    res = {}
    todo = interleave(jobs)
    for rnd, (wait, tmo) in enumerate(((0, TIMEOUT),) + RETRIES):
        if not todo:
            break
        time.sleep(wait)
        with ThreadPoolExecutor(WORKERS) as ex:
            for url, st in zip(todo, ex.map(lambda u: check_url(u, jobs[u], tmo), todo)):
                res[url] = st
        todo = interleave([u for u in todo if res[u].startswith("dead")])
        log(f"link check round {rnd + 1}: {len(res) - len(todo)}/{len(res)} ok/unsure, "
            f"{len(todo)} failing, {time.time() - t0:.0f}s")
    return res


# ---------------------------------------------------------------- merge
def merge(cfg, C, B, U, drop_removed):
    """C: his channels (+ previously hidden), B: base, U: upstream.
    Returns records [{pub, base, upstream, deleted}] in output order + stats."""
    cb = match(C, B)
    manual = {}
    for ci, c in enumerate(C):
        if ci not in cb:
            manual[ci] = None
        elif sig(c) != sig(B[cb[ci]]):
            b = B[cb[ci]]
            manual[ci] = {f for f in FIELDS + ("dur", "_noall") if c.get(f) != b.get(f)}
    used_b = set(cb.values())
    deleted = [B[j] for j in range(len(B)) if j not in used_b]
    cu = match(C, U)
    uc = {v: k for k, v in cu.items()}
    dmatch = match(U, deleted) if deleted else {}

    st = dict(upstream=len(U), unchanged=0, updated=0, added=0, removed_upstream=0,
              kept_manual=0, user_deleted=0)
    ch = {"updated": [], "added": [], "removed_upstream": [], "manual": [], "user_deleted": []}
    recs, placed = [], {}
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
                else:
                    new.pop(f, None)
            if m:
                st["kept_manual"] += 1
                ch["manual"].append({"channel": label(c), "fields": sorted(m)})
            if sig(new) == sig(c):
                st["unchanged"] += 1
            else:
                st["updated"] += 1
                ch["updated"].append({"channel": label(c), "changes":
                                      {f: [str(c.get(f)), str(new.get(f))] for f in FIELDS if c.get(f) != new.get(f)}})
            recs.append(dict(pub=new, base=u, upstream=True, deleted=False))
            placed[ci] = len(recs) - 1
        elif ui in dmatch:
            st["user_deleted"] += 1
            ch["user_deleted"].append(label(u))
            recs.append(dict(pub=None, base=u, upstream=True, deleted=True))  # tombstone
        else:
            st["added"] += 1
            ch["added"].append(label(u))
            recs.append(dict(pub=dict(u), base=u, upstream=True, deleted=False))
    by_anchor = {}
    for ci, c in enumerate(C):
        if ci in cu:
            continue
        if manual.get(ci, 0) is None:
            st["kept_manual"] += 1
            ch["manual"].append({"channel": label(c), "fields": ["own channel"]})
            base = None
        else:
            st["removed_upstream"] += 1
            ch["removed_upstream"].append(label(c))
            if drop_removed:
                continue
            base = B[cb[ci]]
        anchor = next((placed[k] for k in range(ci - 1, -1, -1) if k in placed), -1)
        by_anchor.setdefault(anchor, []).append(dict(pub=c, base=base, upstream=False, deleted=False))
    out = list(by_anchor.get(-1, []))
    for i, r in enumerate(recs):
        out.append(r)
        out.extend(by_anchor.get(i, []))
    return out, st, ch


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--repo-dir", help="local clone of n86cc/IPTV (default: fetch raw from GitHub main)")
    ap.add_argument("--upstream-dir", help="local iptv-org playlist copies")
    ap.add_argument("--state", help="state dir (default ./state, or .iptv-sync/state with --in-place)")
    ap.add_argument("--out", default=os.path.join(here, "out"))
    ap.add_argument("--in-place", action="store_true", help="CI mode, see above")
    ap.add_argument("--check-links", action="store_true", help="check every stream and leave out dead ones")
    ap.add_argument("--drop-removed", action="store_true", help="delete channels removed upstream")
    ap.add_argument("--pure", action="store_true", help="plain regeneration from upstream")
    ap.add_argument("--only", choices=list(TARGETS), action="append")
    a = ap.parse_args()
    t_start = time.time()

    report_path = os.path.join(a.out, "report.json")
    seed_dir = None
    if a.in_place:
        a.repo_dir = a.repo_dir or "."
        a.out = a.repo_dir
        a.state = a.state or os.path.join(a.repo_dir, ".iptv-sync", "state")
        report_path = os.path.join(a.repo_dir, ".iptv-sync", "report.json")
        seed_dir = os.path.join(a.repo_dir, ".iptv-sync", "seed")
    a.state = a.state or os.path.join(here, "state")
    names = a.only or list(TARGETS)

    hp = os.path.join(a.state, "health.json")
    fails = json.load(open(hp)).get("fails", {}) if os.path.exists(hp) else {}

    lists = {}
    up_cache = {}

    def up(src):
        if src not in up_cache:
            up_cache[src] = load_upstream(src, a.upstream_dir)
        return up_cache[src]

    for n in names:
        cfg = TARGETS[n]
        cats, main_g = cfg["cats"], cfg.get("main", "All")
        cur = read(os.path.join(a.repo_dir, n) if a.repo_dir else RAW + n)
        nl = "\r\n" if cur.split("\n", 1)[0].endswith("\r") else "\n"
        uptext, uinfo = up(cfg["src"]), None
        if cfg.get("union_from"):
            src2, cat = cfg["union_from"]
            uptext, uinfo = union_category(uptext, up(src2), cat)
        up_header, U = units_from_upstream(uptext, cats)
        cur_header, C = units_from_target(cur, cats, main_g)
        L = dict(cfg=cfg, nl=nl, header=cur_header, up_header=up_header, uinfo=uinfo)
        if a.pure:
            L["recs"] = [dict(pub=u, base=u, upstream=True, deleted=False) for u in U]
            L["st"], L["ch"] = {"upstream": len(U)}, {}
            lists[n] = L
            continue
        bp, hdp = os.path.join(a.state, n + ".base"), os.path.join(a.state, n + ".hidden")
        B = units_from_target(read(bp), cats, main_g)[1] if os.path.exists(bp) else list(C)
        H = units_from_target(read(hdp), cats, main_g)[1] if os.path.exists(hdp) else []
        if seed_dir and os.path.exists(os.path.join(seed_dir, n)):
            S = units_from_target(read(os.path.join(seed_dir, n)), cats, main_g)[1]
            H += S
            B += S  # seeded channels count as known (not his own, not deleted)
            for s in S:
                fails[s["url"]] = max(fails.get(s["url"], 0), 1)
            L["seeded"] = len(S)
        recs, st, ch = merge(cfg, C + H, B, U, a.drop_removed)
        st["current"], st["hidden_before"] = len(C), len(H)
        L.update(recs=recs, st=st, ch=ch)
        lists[n] = L

    # ---- link check over all lists, each URL once
    status = {}
    if a.check_links and not a.pure:
        jobs = {}
        for L in lists.values():
            for r in L["recs"]:
                if r["pub"] is not None:
                    jobs.setdefault(r["pub"]["url"], headers_for(r["pub"]))
        print(f"checking {len(jobs)} unique stream URLs", flush=True)
        status = check_all(jobs)
        for url, s in status.items():
            if s == "alive":
                fails.pop(url, None)
            elif s.startswith("dead"):
                fails[url] = fails.get(url, 0) + 1
        live = set(status)
        fails = {u: k for u, k in fails.items() if u in live}  # forget URLs no longer in any list

    report = {}
    for n, L in lists.items():
        cfg = L["cfg"]
        cats, main_g, gf = cfg["cats"], cfg.get("main", "All"), cfg.get("group_first", False)
        pub, hidden, base = [], [], []
        st, ch = L["st"], L["ch"]
        ch["left_out_dead"], ch["purged"] = [], []
        for r in L["recs"]:
            p = r["pub"]
            if p is not None:
                k = fails.get(p["url"], 0) if a.check_links else 0
                if k >= HIDE_AFTER:
                    if not r["upstream"] and k >= PURGE_AFTER:
                        ch["purged"].append(label(p))
                        continue
                    hidden.append(p)
                    ch["left_out_dead"].append(f"{label(p)} ({k} nights, {status.get(p['url'], '')})")
                else:
                    pub.append(p)
            if r["base"] is not None:
                base.append(r["base"])
        if not a.pure:
            st["left_out_dead"] = len(hidden)
            st["final"] = len(pub)
            st["purged"] = len(ch["purged"])
            if a.check_links:
                urls = {p["url"] for p in pub + hidden}
                st["urls_dead_tonight"] = sum(1 for u in urls if status.get(u, "").startswith("dead"))
        header = L["up_header"] if a.pure else L["header"]
        write(os.path.join(a.out, n), render(header, pub, cats, L["nl"], main_g, gf))
        if a.in_place:
            write(os.path.join(a.state, n + ".base"), render(L["header"], base, cats, L["nl"], main_g, gf))
            hdp = os.path.join(a.state, n + ".hidden")
            if hidden:
                write(hdp, render(L["header"], hidden, cats, L["nl"], main_g, gf))
            elif os.path.exists(hdp):
                os.remove(hdp)
            if seed_dir and os.path.exists(os.path.join(seed_dir, n)):
                os.remove(os.path.join(seed_dir, n))
        rep = {"stats": st, "changes": ch, "header_up": L["up_header"], "header_cur": L["header"]}
        if L.get("uinfo"):
            rep["union"] = L["uinfo"]
        if L.get("seeded"):
            rep["seeded"] = L["seeded"]
        report[n] = rep
        print(n, json.dumps(st, ensure_ascii=False))
    if a.in_place and not a.pure:
        os.makedirs(a.state, exist_ok=True)
        with open(hp, "w") as f:
            json.dump({"hide_after": HIDE_AFTER, "fails": dict(sorted(fails.items()))}, f, indent=0, sort_keys=False)
            f.write("\n")
        if seed_dir and os.path.isdir(seed_dir) and not os.listdir(seed_dir):
            os.rmdir(seed_dir)
    report["_run"] = {"seconds": round(time.time() - t_start), "urls_checked": len(status),
                      "status_counts": {k: v for k, v in sorted(
                          __import__("collections").Counter(s.split(":")[0] for s in status.values()).items())}}
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("done in", report["_run"]["seconds"], "s", report["_run"]["status_counts"])


if __name__ == "__main__":
    main()
