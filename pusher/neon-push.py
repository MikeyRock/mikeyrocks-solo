#!/usr/bin/env python3
"""
neon-push.py — read AxeBTC / AxeBCH (ckpool-solo) stats on an Umbrel and publish a
sanitized, read-only stats.json to a GitHub branch. Nothing is exposed inbound.

Runs as root (needs docker + the app-data folders). Python stdlib only.
Config: /home/umbrel/neon-solo/config.env   (see install.sh)
"""
import calendar, json, os, re, shutil, subprocess, sys, time, base64, urllib.request, urllib.error
from pathlib import Path

HOME = Path(os.environ.get("NEON_HOME", "/home/umbrel/neon-solo"))
APPDATA = Path("/home/umbrel/umbrel/app-data")

POOLS = {
    "btc": {
        "name": "AxeBTC", "coin": "BTC",
        "pooldir": APPDATA / "willitmod-btc/data/pool/www",
        "container": "willitmod-btc_bitcoind_1",
        "cli": ["bitcoin-cli", "-datadir=/data"],
        "gbt": ['{"rules":["segwit"]}'],
    },
    "bch": {
        "name": "AxeBCH", "coin": "BCH",
        "pooldir": APPDATA / "willitmod-dev-bch/data/pool/www",
        "container": "willitmod-dev-bch_bchn_1",
        "cli": ["bitcoin-cli", "-datadir=/data"],
        "gbt": [],
        "stats_in_coins": True,          # BCHN getblockstats reports BCH, not satoshis
    },
}
HISTORY_SECS = 24 * 3600
OFFLINE_SECS = 10 * 60
NODE_EVERY = 55          # node RPCs (slow) refresh at most this often; pool/shares every run
RIG_BEST_COOLDOWN = 0   # alert on every new best (checked each run, i.e. every minute)
UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "Z": 1e21}


def load_env():
    env = {}
    f = HOME / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("NEON_") or k == "GITHUB_TOKEN"})
    return env


def hr(v):
    """ckpool hashrate strings like '88.1T' -> float H/s"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGTPEZ]?)\s*", str(v))
    return float(m.group(1)) * UNITS[m.group(2)] if m else None


def mask(addr):
    if not addr:
        return None
    a = addr.split(":", 1)[-1]
    return (addr.split(":")[0] + ":" if ":" in addr else "") + (a[:5] + "…" + a[-4:] if len(a) > 12 else a)


def read_jsonl(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.update(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def rpc(cfg, *args, timeout=30):
    try:
        r = subprocess.run(["docker", "exec", cfg["container"], *cfg["cli"], *args],
                           capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


SOLVED_RE = re.compile(r"Solved and confirmed block (\d+) by (\S+)", re.I)
DIFF_RE = re.compile(r"Possible block solve diff ([\d.]+)", re.I)
EFFORT_RE = re.compile(r"Block solved after \d+ shares at ([\d.]+)% diff", re.I)
TS_RE = re.compile(r"^\[?(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)")


DEFAULT_RIG = "MikeyRocks"   # shown when a block has no rig name


def rig_or_default(rig):
    return rig if rig and str(rig).strip().lower() not in ("—", "-", "unnamed", "unknown", "null", "none") else DEFAULT_RIG


def rig_name(worker):
    """'bc1q...addr.RigName' -> 'RigName' (never publish the address)."""
    return (worker.split(".", 1)[1] if "." in worker else "unnamed")[:32]


def parse_blocks(text):
    """Return [{height, time, rig, effort}] for each block in ckpool.log text.
    ckpool.log timestamps are UTC: '[2026-10-22 06:27:22.094] Solved and confirmed block 973980 by addr.RIG'"""
    out, last_diff = [], None
    for line in text.splitlines():
        m = TS_RE.match(line)
        ts = calendar.timegm(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")) if m else int(time.time())
        dm = DIFF_RE.search(line)
        if dm:
            last_diff = float(dm.group(1))
            continue
        s = SOLVED_RE.search(line)
        if s:
            out.append({"height": int(s.group(1)), "time": ts, "rig": rig_name(s.group(2)), "effort": None,
                        "diff": last_diff})
            last_diff = None
            continue
        e = EFFORT_RE.search(line)
        if e:
            if out and out[-1]["effort"] is None:
                out[-1]["effort"] = float(e.group(1))
            elif not out:   # effort line arrived in a later read than its block line
                out.append({"height": None, "time": ts, "rig": None, "effort": float(e.group(1))})
    return out


SHARES_PER_POOL = 60


def device_name(agent):
    """'NerdQAxe++/BM1370/v1.0.37.1' -> 'NerdQAxe++'; Braiins/bosminer strings -> 'Braiins OS'."""
    a = str(agent or "").strip()
    if not a:
        return None
    low = a.lower()
    if "bosminer" in low or "braiins" in low:
        return "Braiins OS"
    for key, name in (("whatsminer", "Whatsminer"), ("antminer", "Antminer"), ("cgminer", "cgminer"), ("bmminer", "Antminer")):
        if key in low:
            return name
    return a.split("/")[0].split(" ")[0][:24]


def recent_shares(cfg, n=SHARES_PER_POOL):
    """Newest accepted shares from ckpool's sharelogs (www/<height>/<workinfo>.sharelog, one JSON per line).
    Published per share: time, rig, share diff, target diff, device. Never the miner's IP or address."""
    www = cfg["pooldir"]
    try:
        dirs = sorted((d for d in www.iterdir() if d.is_dir() and re.fullmatch(r"[0-9a-f]{8}", d.name)),
                      key=lambda d: d.stat().st_mtime, reverse=True)[:2]
    except Exception:
        return []
    out = []
    for d in dirs:
        try:
            files = sorted(d.glob("*.sharelog"), key=lambda f: f.stat().st_mtime, reverse=True)
        except Exception:
            continue
        for f in files:
            try:
                with open(f, "rb") as fh:
                    fh.seek(0, 2); size = fh.tell()
                    fh.seek(max(0, size - 65536))
                    lines = fh.read().decode("utf-8", "replace").splitlines()
            except Exception:
                continue
            for line in reversed(lines):
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if not j.get("result"):
                    continue
                cd = str(j.get("createdate", "")).split(",")
                try:
                    t = int(cd[0]) + (int(cd[1][:3]) / 1000 if len(cd) > 1 and cd[1] else 0)
                except ValueError:
                    continue
                out.append({"t": round(t, 3), "rig": rig_name(str(j.get("workername", ""))),
                            "sdiff": round(float(j.get("sdiff") or 0), 2), "diff": float(j.get("diff") or 0),
                            "dev": device_name(j.get("agent"))})
            if len(out) >= n * 2:
                break
        if len(out) >= n * 2:
            break
    out.sort(key=lambda x: x["t"], reverse=True)
    return out[:n]


KEEP_SHARELOG_DAYS = 14
CLEANUP_EVERY = 6 * 3600


def round_start(cfg, blocks):
    """When the current round began: the app's own record, else our newest block. None if unknown."""
    sdir = cfg["pooldir"].parent.parent / "ui" / "state"
    cands = []
    pc = read_json_retry(sdir / "pool_cache.json") or {}
    started = ((pc.get("pool") or {}).get("current_round") or {}).get("started")
    if started:
        try:
            cands.append(calendar.timegm(time.strptime(started[:19], "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            pass
    ps = read_json_retry(sdir / "pool_state.json") or {}
    if isinstance(ps.get("round_reset_at"), (int, float)):
        cands.append(int(ps["round_reset_at"]))
    if blocks:
        cands.append(blocks[0]["time"])
    return max(cands) if cands else None


def cleanup_sharelogs(key, cfg, blocks, state, now, dry_run=False):
    """Delete ckpool sharelog folders (www/<8 hex>/) older than KEEP_SHARELOG_DAYS,
    but never anything from the current round (the apps compute effort/luck from it)."""
    last = state.setdefault("_cleanup", {}).get(key, 0)
    if not dry_run and now - last < CLEANUP_EVERY:
        return None
    cutoff = now - KEEP_SHARELOG_DAYS * 86400
    rs = round_start(cfg, blocks)
    if rs:
        cutoff = min(cutoff, rs - 3600)          # keep the whole current round (+1h margin)
    removed, freed = 0, 0
    try:
        dirs = [d for d in cfg["pooldir"].iterdir() if d.is_dir() and re.fullmatch(r"[0-9a-f]{8}", d.name)]
    except Exception:
        dirs = []
    newest = max(dirs, key=lambda d: d.stat().st_mtime, default=None)
    for d in dirs:
        try:
            if d == newest or d.stat().st_mtime >= cutoff:
                continue
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            if not dry_run:
                shutil.rmtree(d)
            removed += 1; freed += size
        except Exception:
            continue
    if not dry_run:
        state["_cleanup"][key] = now
    msg = (f"{'would delete' if dry_run else 'deleted'} {removed} sharelog folders ({freed / 1e6:.1f} MB) for {key}; "
           f"keeping since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(cutoff))}")
    if removed or dry_run:
        print(msg)
    return msg


COINGECKO_ID = {"btc": "bitcoin", "bch": "bitcoin-cash"}


def price_now(key):
    """Spot USD price right now (used to stamp a newly found block)."""
    cid = COINGECKO_ID.get(key)
    try:
        req = urllib.request.Request(f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=usd",
                                     headers={"User-Agent": "mikeyrocks-solo-pusher", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return float(json.loads(r.read())[cid]["usd"])
    except Exception as e:
        print(f"price lookup failed for {key}: {e}", file=sys.stderr)
        return None


def price_history(key, t0, t1):
    """[(unix_seconds, usd)] from CoinGecko, hourly for ranges up to 90 days (chunked)."""
    cid, pts, start = COINGECKO_ID.get(key), [], int(t0) - 7200
    while start < t1:
        end = min(int(t1) + 7200, start + 89 * 86400)
        url = f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart/range?vs_currency=usd&from={start}&to={end}"
        req = urllib.request.Request(url, headers={"User-Agent": "mikeyrocks-solo-pusher", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            pts += [(ms / 1000, px) for ms, px in json.loads(r.read()).get("prices", [])]
        start = end
        time.sleep(2)
    return pts


def to_sats(v, coins=False):
    """getblockstats amounts: Bitcoin Core gives satoshis, Bitcoin Cash Node gives coins (e.g. 3.125)."""
    if v is None:
        return None
    return int(round(float(v) * 1e8)) if coins else int(v)


def read_app_blocks(cfg):
    d = read_json_retry(cfg["pooldir"].parent.parent / "ui" / "state" / "blocks_state.json")
    if d is None:
        return []
    ev = d.get("events") if isinstance(d, dict) else d
    return [e for e in (ev or []) if isinstance(e, dict)]


def block_history(key, cfg, state):
    """Permanent list of blocks found, read from ckpool.log. Only new bytes are read each run;
    the list lives in the local state file so it survives log rotation and app restarts."""
    st = state.setdefault("_blockhist", {}).setdefault(key, {"offset": 0, "blocks": []})
    st.pop("since", None)
    new = []
    if any("diff" not in b for b in st["blocks"]):   # one-time re-read to backfill share diff
        st["offset"] = 0
    log = cfg["pooldir"] / "ckpool.log"
    try:
        size = log.stat().st_size
        if size < st.get("offset", 0):          # log was rotated / truncated
            st["offset"] = 0
        with open(log, "rb") as fh:
            fh.seek(st.get("offset", 0))
            chunk = fh.read()
        # don't split a line: stop at the last newline
        cut = chunk.rfind(b"\n") + 1
        st["offset"] = st.get("offset", 0) + cut
        known = {b["height"] for b in st["blocks"]}
        for b in parse_blocks(chunk[:cut].decode("utf-8", "replace")):
            if b["height"] is None:
                last = max(st["blocks"], key=lambda x: x["time"], default=None)
                if last and last.get("effort") is None:
                    last["effort"] = b["effort"]
                continue
            if b["height"] not in known:
                st["blocks"].append(b)
                known.add(b["height"])
                new.append(b)
            else:
                for x in st["blocks"]:
                    if x["height"] == b["height"]:
                        if x.get("effort") is None and b["effort"] is not None:
                            x["effort"] = b["effort"]
                        if x.get("diff") is None:
                            x["diff"] = b.get("diff")
    except FileNotFoundError:
        pass

    # merge the app's own block record (it also finds blocks by scanning the chain for our payouts)
    known = {b["height"]: b for b in st["blocks"]}
    for e in read_app_blocks(cfg):
        h = e.get("height")
        if not isinstance(h, int) or (isinstance(e.get("confirmations"), int) and e["confirmations"] < 0):
            continue                                   # skip malformed / orphaned
        w = str(e.get("solve_worker") or "")
        rig = rig_name(w) if "." in w else None
        diff = None if e.get("source") == "backscan" else e.get("solve_diff")   # backscan diff is a placeholder
        if h not in known:
            b = {"height": h, "time": int(e.get("t") or time.time()), "rig": rig or "—", "effort": None, "diff": diff}
            st["blocks"].append(b); known[h] = b; new.append(b)
        else:
            b = known[h]
            if rig and b.get("rig") in (None, "—", "unnamed", "unknown"):
                b["rig"] = rig
            if b.get("diff") is None and diff:
                b["diff"] = diff
    st["blocks"].sort(key=lambda b: b["time"], reverse=True)
    return st["blocks"], new


def fmt_si(n):
    for u in ["", "K", "M", "G", "T", "P", "E"]:
        if abs(n) < 1000:
            return f"{n:.2f}{u}".replace(".00", "")
        n /= 1000
    return f"{n:.2f}Z"


def push_alert(title, body, tag="block", kind="block"):
    env = load_env()
    url, token = env.get("NEON_PUSH_URL"), env.get("NEON_PUSH_TOKEN")
    if not url or not token:
        return None
    req = urllib.request.Request(url.rstrip("/") + "/notify", method="POST",
                                 data=json.dumps({"type": kind, "title": title, "body": body, "tag": tag}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "User-Agent": "mikeyrocks-solo-pusher"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read() or b"{}")
            print(f"alert [{kind}] {title} -> {res}")
            return res
    except Exception as e:
        print(f"alert failed [{kind}] {title}: {e}", file=sys.stderr)
        return None


def alert_block(key, cfg, b, test=False):
    coin = cfg["coin"]
    stats = rpc(cfg, "getblockstats", str(b["height"]), '["subsidy","totalfee"]', timeout=10) or {}
    c = cfg.get("stats_in_coins", False)
    reward = (to_sats(stats.get("subsidy"), c) or ((50 * 100_000_000) >> (b["height"] // 210_000))) + (to_sats(stats.get("totalfee"), c) or 0)
    parts = [f"Rig: {rig_or_default(b['rig'])}"]
    if b.get("diff"):
        parts.append(f"Diff: {fmt_si(b['diff'])}")
    if b.get("effort") is not None:
        parts.append(f"Effort: {b['effort']:.1f}%")
    parts.append(f"Reward: {reward / 1e8:.4f} {coin}")
    res = push_alert(f"🏆 {coin} BLOCK FOUND! #{b['height']:,} 🏆" + (" (resend)" if test else ""), " · ".join(parts),
                     tag=f"block-{key}-{b['height']}" + ("-test" if test else ""))
    print(f"block alert {coin} #{b['height']}: {res}")


def read_json_retry(path, tries=4):
    """Read a JSON file another program is rewriting; retry if we catch it half-written."""
    for i in range(tries):
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            time.sleep(0.25 * (i + 1))
    return None


def find_dicts(obj, key):
    """All dicts anywhere inside obj that contain `key`."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from find_dicts(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_dicts(v, key)


def read_app(cfg):
    """The AxeBTC/AxeBCH app keeps its own best shares (these follow the app's 'reset best share' button).
    Returns (pool dict, {rig: best since reset}) or ({}, {}) if the app files aren't there."""
    sdir = cfg["pooldir"].parent.parent / "ui" / "state"
    pool, rigs = {}, {}
    pc = read_json_retry(sdir / "pool_cache.json")
    if isinstance(pc, dict):
        pool = pc.get("pool") or {}
    for fname in ("pool_cache.json", "pool_workers_cache.json"):
        data = pc if fname == "pool_cache.json" else read_json_retry(sdir / fname)
        if data is None:
            continue
        for d in find_dicts(data, "workername"):
            if "bestshare_since_block" in d:
                rigs.setdefault(rig_name(str(d["workername"])), d["bestshare_since_block"])
    return pool, rigs


def watch_rigs(key, cfg, workers, state, now):
    """Alert on a rig going offline (>10 min) and coming back."""
    rigs = state.setdefault("_rigs", {}).setdefault(key, {})
    first_run = not rigs
    seen = set()
    for w in workers:
        name = w["name"]; seen.add(name)
        online = bool(w.get("lastshare")) and now - w["lastshare"] < OFFLINE_SECS
        best = float(w.get("bestever") or 0)
        r = rigs.get(name)
        if r is None:
            rigs[name] = {"online": online, "best": best}
            continue
        if r["online"] and not online:
            push_alert(f"📴 RIG OFFLINE · {cfg['coin']} · {name}",
                       f"No shares from {name} for 10+ min (last share {(now - w['lastshare']) // 60} min ago)" if w.get("lastshare") else f"No shares from {name} for 10+ min",
                       tag=f"rig-{key}-{name}", kind="offline")
        elif not r["online"] and online:
            push_alert(f"✅ BACK ONLINE · {cfg['coin']} · {name}", f"{name} is submitting shares again",
                       tag=f"rig-{key}-{name}", kind="offline")
        r["online"], r["best"] = online, max(best, r["best"])
    for name, r in rigs.items():          # rig vanished from ckpool entirely
        if name not in seen and r["online"]:
            push_alert(f"📴 RIG OFFLINE · {cfg['coin']} · {name}", f"{name} disconnected from the pool",
                       tag=f"rig-{key}-{name}", kind="offline")
            r["online"] = False


def node_info(key, cfg, cache, now):
    """Ask the node for height/difficulty/peers and the current block reward (can be slow)."""
    out = {}
    out_stale = False
    bci = rpc(cfg, "getblockchaininfo", timeout=25) or {}
    net = rpc(cfg, "getnetworkinfo", timeout=25) or {}
    if bci:
        cache[key] = {k: bci.get(k) for k in ("blocks", "headers", "verificationprogress", "initialblockdownload", "difficulty")}
    else:
        cnt = rpc(cfg, "getblockcount", timeout=40)
        dif = rpc(cfg, "getdifficulty", timeout=20) if isinstance(cnt, int) else None
        last = cache.get(key) or {}
        if not isinstance(cnt, int) and last.get("blocks"):
            cnt = last["blocks"]
            out_stale = True
        if isinstance(cnt, int):
            hdr = max(cnt, last.get("headers") or 0)
            bci = {"blocks": cnt, "headers": hdr, "difficulty": dif if isinstance(dif, (int, float)) else last.get("difficulty"),
                   "verificationprogress": last.get("verificationprogress") if cnt < hdr else 1,
                   "initialblockdownload": bool(last.get("initialblockdownload")) and cnt < hdr}
            if not out_stale:
                cache[key] = {**last, "blocks": cnt, "headers": hdr, "difficulty": bci["difficulty"]}
    out["node"] = {
        "blocks": bci.get("blocks"), "headers": bci.get("headers"), "progress": bci.get("verificationprogress"),
        "synced": bool(bci) and not bci.get("initialblockdownload", True) and bci.get("blocks") == bci.get("headers"),
        "difficulty": bci.get("difficulty"), "peers": net.get("connections"), "subversion": net.get("subversion"),
        "online": bool(bci), "stale": out_stale,
    }
    nh = rpc(cfg, "getnetworkhashps", timeout=25)
    if not isinstance(nh, (int, float)) and isinstance(bci.get("difficulty"), (int, float)):
        nh = bci["difficulty"] * 2 ** 32 / 600          # estimate from difficulty (600 s target)
    out["node"]["nethash"] = float(nh) if isinstance(nh, (int, float)) else None
    if bci.get("blocks") is not None:
        height = bci["blocks"] + 1
        subsidy = (50 * 100_000_000) >> (height // 210_000)
        gbt = rpc(cfg, "getblocktemplate", *cfg.get("gbt", []), timeout=40) if out["node"]["synced"] else None
        total = (gbt or {}).get("coinbasevalue")
        out["reward"] = {"height": height, "subsidy_sats": subsidy, "fees_sats": (total - subsidy) if total else None,
                         "total_sats": total or subsidy, "live": bool(total)}
    out["_at"] = now
    return out


NODE_FILE = HOME / "node.json"


def load_node_cache():
    return read_json_retry(NODE_FILE) or {}


SCAN_FILE = HOME / "scan.json"
NEAR_KEEP = 10
SCAN_BUDGET = 40 * 1024 * 1024        # bytes of sharelog read per background run (backfill catches up over a few runs)


def scan_sharelogs(key, cfg, st, netdiff, rstart, now):
    """Read new sharelog lines since last time. Keeps:
       - near: top NEAR_KEEP shares by share diff / network diff (below 100%, i.e. not blocks)
       - effort_sum: sum of credited share difficulty since the round started (for round effort)"""
    ps = st.setdefault(key, {"files": {}, "near": [], "effort_sum": 0.0, "round": None})
    oldest = now - KEEP_SHARELOG_DAYS * 86400
    try:
        dirs = [d for d in cfg["pooldir"].iterdir() if d.is_dir() and re.fullmatch(r"[0-9a-f]{8}", d.name)
                and d.stat().st_mtime >= oldest]
    except Exception:
        return ps
    if rstart and ps.get("round") != rstart:               # new round (block found / reset): start counting again
        first = ps.get("round") is None
        ps["round"], ps["effort_sum"] = rstart, 0.0
        for k in list(ps["files"]):                         # re-read only files written since the round began
            try:
                if first or Path(k).stat().st_mtime >= rstart - 60:
                    ps["files"][k] = 0
            except Exception:
                pass
    files = []
    for d in dirs:
        try:
            files += [f for f in d.glob("*.sharelog")]
        except Exception:
            pass
    try:
        files.sort(key=lambda f: f.stat().st_mtime)
    except Exception:
        pass
    budget, seen = SCAN_BUDGET, set()
    near = ps["near"]
    for f in files:
        k = str(f); seen.add(k)
        try:
            size = f.stat().st_size
        except Exception:
            continue
        off = ps["files"].get(k, 0)
        if size <= off:
            continue
        if budget <= 0:
            break
        try:
            with open(f, "rb") as fh:
                fh.seek(off); chunk = fh.read(min(size - off, budget))
        except Exception:
            continue
        cut = chunk.rfind(b"\n") + 1
        if cut == 0:
            continue
        ps["files"][k] = off + cut; budget -= cut
        for line in chunk[:cut].decode("utf-8", "replace").splitlines():
            try:
                j = json.loads(line)
            except Exception:
                continue
            if not j.get("result"):
                continue
            try:
                t = int(str(j.get("createdate", "")).split(",")[0])
            except ValueError:
                continue
            sd = float(j.get("sdiff") or 0)
            if rstart is None or t >= rstart:
                ps["effort_sum"] += float(j.get("diff") or 0)
            if netdiff and sd < netdiff and (len(near) < NEAR_KEEP or sd / netdiff > near[-1]["pct"] / 100):
                near.append({"t": t, "rig": rig_name(str(j.get("workername", ""))), "sdiff": round(sd, 2),
                             "pct": round(sd / netdiff * 100, 4), "dev": device_name(j.get("agent"))})
                near.sort(key=lambda x: -x["pct"])
                # one entry per share (same share can't appear twice, but guard against re-reads)
                uniq, out_ = set(), []
                for x in near:
                    if (x["t"], x["rig"], x["sdiff"]) not in uniq:
                        uniq.add((x["t"], x["rig"], x["sdiff"])); out_.append(x)
                ps["near"] = near = out_[:NEAR_KEEP]
    ps["files"] = {k: v for k, v in ps["files"].items() if k in seen}   # forget deleted files
    ps["caught_up"] = budget > 0
    return ps


def refresh_nodes():
    """--node-only: one background refresh of every pool's node info into node.json."""
    import fcntl
    lock = open(HOME / "node.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return                                   # a refresh is already running
    data = load_node_cache()
    cache = data.pop("_cache", {})
    for key, cfg in POOLS.items():
        try:
            data[key] = node_info(key, cfg, cache, int(time.time()))
        except Exception as e:
            print(f"node refresh {key} failed: {e}", file=sys.stderr)
    data["_cache"] = cache
    # block rewards (subsidy + fees) for found blocks, looked up once each
    hist = read_json_retry(HOME / "history.json") or {}
    rw = data.setdefault("_rewards", {})
    for key in list(rw):
        rw[key] = {h: r for h, r in rw[key].items() if r.get("v") == 2 or r.get("fees") is None and r.get("subsidy", 0) > 1000}
    for key, cfg in POOLS.items():
        have = rw.setdefault(key, {})
        todo = [b["height"] for b in hist.get("_blockhist", {}).get(key, {}).get("blocks", [])
                if isinstance(b.get("height"), int) and (str(b["height"]) not in have or
                    (have[str(b["height"])].get("v") != 2 and have[str(b["height"])].get("fees") is not None))][:15]
        for h in todo:
            st = rpc(cfg, "getblockstats", str(h), '["subsidy","totalfee"]', timeout=15)
            if isinstance(st, dict) and "subsidy" in st:
                c = cfg.get("stats_in_coins", False)
                have[str(h)] = {"subsidy": to_sats(st["subsidy"], c), "fees": to_sats(st.get("totalfee"), c), "v": 2}
            else:   # pruned / unavailable: subsidy is exact from height, fees unknown
                have[str(h)] = {"subsidy": (50 * 100_000_000) >> (h // 210_000), "fees": None}
    # historical USD price for blocks that weren't stamped when found (one CoinGecko request per coin)
    pr = data.setdefault("_prices", {})
    if time.time() - (data.get("_prices_try") or 0) > 600:
        data["_prices_try"] = time.time()
        for key in POOLS:
            have = pr.setdefault(key, {})
            todo = [b for b in hist.get("_blockhist", {}).get(key, {}).get("blocks", [])
                    if isinstance(b.get("height"), int) and not b.get("price_usd") and str(b["height"]) not in have]
            if not todo:
                continue
            try:
                pts = price_history(key, min(b["time"] for b in todo), max(b["time"] for b in todo))
            except Exception as e:
                print(f"price history {key} failed: {e}", file=sys.stderr)
                continue
            for b in todo:
                near = min(pts, key=lambda p: abs(p[0] - b["time"]), default=None)
                if near and abs(near[0] - b["time"]) < 6 * 3600:
                    have[str(b["height"])] = round(near[1], 2)
    # near misses + round effort from the sharelogs
    scan = read_json_retry(SCAN_FILE) or {}
    rounds = data.setdefault("_round", {})
    for key, cfg in POOLS.items():
        nd = (data.get(key) or {}).get("node") or {}
        blocks = hist.get("_blockhist", {}).get(key, {}).get("blocks", [])
        rs = round_start(cfg, blocks)
        try:
            ps = scan_sharelogs(key, cfg, scan, nd.get("difficulty"), rs, int(time.time()))
        except Exception as e:
            print(f"sharelog scan {key} failed: {e}", file=sys.stderr); continue
        rounds[key] = {"start": rs, "work": ps.get("effort_sum"), "near": ps.get("near", []), "caught_up": ps.get("caught_up")}
    stmp = SCAN_FILE.with_suffix(".tmp"); stmp.write_text(json.dumps(scan)); stmp.replace(SCAN_FILE)
    tmp = NODE_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(data)); tmp.replace(NODE_FILE)


def kick_node_refresh():
    """Start a background node refresh if the cache is older than NODE_EVERY (never waits for it)."""
    d = load_node_cache()
    ages = [d[k].get("_at", 0) for k in POOLS if isinstance(d.get(k), dict)]
    if len(ages) == len(POOLS) and time.time() - min(ages) < NODE_EVERY:
        return
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--node-only"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "NEON_HOME": str(HOME)})


def collect(key, cfg, history):
    now = int(time.time())
    out = {"name": cfg["name"], "coin": cfg["coin"], "updated": now}

    # --- node + reward: refreshed by a background job (slow node never delays this run) ---
    nc = load_node_cache()
    nd = nc.get(key) or {}
    rd = (nc.get("_round") or {}).get(key) or {}
    rws = (nc.get("_rewards") or {}).get(key) or {}
    out["node"] = nd.get("node") or {"blocks": None, "headers": None, "progress": None, "synced": False,
                                      "difficulty": None, "peers": None, "subversion": None, "online": False, "stale": True}
    if nd.get("reward"):
        out["reward"] = nd["reward"]
    if nd.get("_at") and now - nd["_at"] > 300:
        out["node"] = {**out["node"], "stale": True}

    # --- pool ---
    status = cfg["pooldir"] / "pool" / "pool.status"
    ps = read_jsonl(status) if status.exists() else {}
    hrs = {"1m": hr(ps.get("hashrate1m")), "5m": hr(ps.get("hashrate5m")), "15m": hr(ps.get("hashrate15m")),
           "1h": hr(ps.get("hashrate1hr")), "6h": hr(ps.get("hashrate6hr")), "1d": hr(ps.get("hashrate1d")),
           "7d": hr(ps.get("hashrate7d"))}
    pool = {"workers": ps.get("Workers"), "idle": ps.get("Idle"), "hashrate": hrs,
            "bestshare": ps.get("bestshare"), "accepted": ps.get("accepted"), "rejected": ps.get("rejected"),
            "runtime": ps.get("runtime"), "lastupdate": ps.get("lastupdate")}

    # --- users / workers ---
    workers, payout, bestever = [], None, 0
    udir = cfg["pooldir"] / "users"
    if udir.is_dir():
        for uf in sorted(udir.iterdir()):
            try:
                u = json.loads(uf.read_text())
            except Exception:
                continue
            payout = payout or mask(uf.name)
            bestever = max(bestever, u.get("bestever") or 0)
            for w in u.get("worker", []):
                workers.append({"name": rig_name(w.get("workername", "")),
                                "hashrate1m": hr(w.get("hashrate1m")), "hashrate5m": hr(w.get("hashrate5m")),
                                "hashrate1h": hr(w.get("hashrate1hr")), "hashrate1d": hr(w.get("hashrate1d")),
                                "lastshare": w.get("lastshare"), "bestshare": w.get("bestshare"),
                                "bestever": w.get("bestever")})
    app, app_rigs = read_app(cfg)
    if app:
        # match the app: its round best follows the "reset best share" button
        rb = app.get("best_share_since_block") if "best_share_since_block" in app else app.get("best_share")
        rig = app.get("best_share_since_block_worker") or app.get("best_share_worker")
        pool["bestshare"] = int(rb or 0)
        pool["bestshare_rig"] = rig_name(rig) if rig and "." in rig else (rig or None)
        pool["bestever"] = int(app.get("best_share_all_time") or bestever or 0) or None
        for w in workers:
            if app_rigs:
                v = app_rigs.get(w["name"])
                w["bestround"] = int(v) if v else None
            elif pool["bestshare_rig"] and w["name"].lower() == pool["bestshare_rig"].lower():
                w["bestround"] = pool["bestshare"]   # app only reports the pool's top rig
    else:
        # no app files: ckpool's pool figure, or the best share of a rig hashing right now
        rig_best = max((w.get("bestshare") or 0 for w in workers
                        if w.get("lastshare") and now - w["lastshare"] < OFFLINE_SECS), default=0)
        pool["bestshare"] = int(max(pool.get("bestshare") or 0, rig_best)) or 0
        pool["bestever"] = max(bestever, pool["bestshare"]) or None

    # 🎯 alert when any rig beats its own best since block/reset (a reset lowers it silently)
    rigbest = history.setdefault("_rigbest", {}).setdefault(key, {})
    pool_top = pool["bestshare"] or 0
    for w in workers:
        cur = w.get("bestround")
        if cur is None and not app:
            cur = w.get("bestshare")
        if not cur:
            continue
        r = rigbest.setdefault(w["name"], {"alerted": None, "at": 0})
        if r["alerted"] is None or cur < r["alerted"]:      # first sight, or the app was reset
            r["alerted"] = cur
            continue
        if cur > r["alerted"] and now - r["at"] >= RIG_BEST_COOLDOWN:
            top = " 👑 pool best!" if cur >= pool_top else ""
            push_alert(f"🎯 NEW BEST DIFF · {cfg['coin']} · {w['name']}",
                       f"{w['name']} hit {fmt_si(cur)} (was {fmt_si(r['alerted'])}){top}",
                       tag=f"best-{key}-{w['name']}", kind="bestdiff")
            r["alerted"], r["at"] = cur, now
    workers.sort(key=lambda w: (-(w["hashrate5m"] or 0)))
    blocks, new_blocks = block_history(key, cfg, history)
    watch_rigs(key, cfg, workers, history, now)
    # only publish rigs that submitted a share in the last 10 minutes
    workers = [w for w in workers if w["lastshare"] and now - w["lastshare"] < OFFLINE_SECS]
    hp = (nc.get("_prices") or {}).get(key) or {}
    for b in blocks:
        if not b.get("price_usd") and hp.get(str(b.get("height"))):
            b["price_usd"], b["price_src"] = hp[str(b["height"])], "hourly"
        r = rws.get(str(b.get("height")))
        if r:
            b["reward_sats"] = int(r["subsidy"]) + int(r["fees"] or 0)
            b["fees_known"] = r["fees"] is not None
    netd = (out.get("node") or {}).get("difficulty")
    if rd:
        out["round"] = {"start": rd.get("start"), "work": rd.get("work"),
                        "effort": round(rd["work"] / netd * 100, 5) if rd.get("work") is not None and netd else None,
                        "partial": not rd.get("caught_up", True)}
        out["near"] = rd.get("near", [])
    out.update(pool=pool, workers=workers, payout=payout, blocks=blocks, blocks_found=len(blocks),
               shares=recent_shares(cfg))
    cleanup_sharelogs(key, cfg, blocks, history, now, dry_run="--cleanup-dry-run" in sys.argv)

    # --- push alert for blocks found since the last run (skip old ones from the first log read) ---
    for b in new_blocks:
        if now - b["time"] < 2 * 3600:
            px = price_now(key)
            if px:
                b["price_usd"] = px            # stamped at the time the block was hit
            alert_block(key, cfg, b)

    # --- history (local file, last 24h of 5m hashrate) ---
    h = [p for p in history.get(key, []) if p[0] > now - HISTORY_SECS]
    if hrs["5m"] is not None and (not h or now - h[-1][0] >= 240):
        h.append([now, hrs["5m"], out["node"].get("nethash")])
    history[key] = h
    out["history"] = h
    return out


def gh(token, method, url, body=None):
    req = urllib.request.Request("https://api.github.com" + url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mikeyrocks-solo-pusher"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def publish(token, repo, branch, content):
    """One-commit orphan branch, force-updated each time: history never grows."""
    s, tree = gh(token, "POST", f"/repos/{repo}/git/trees",
                 {"tree": [{"path": "stats.json", "mode": "100644", "type": "blob", "content": content}]})
    if s != 201:
        raise SystemExit(f"tree failed {s}: {tree}")
    s, commit = gh(token, "POST", f"/repos/{repo}/git/commits",
                   {"message": f"stats {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}", "tree": tree["sha"], "parents": []})
    if s != 201:
        raise SystemExit(f"commit failed {s}: {commit}")
    for attempt in range(3):
        s, r = gh(token, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"], "force": True})
        if s in (200, 201):
            break
        if s == 404 or (s == 422 and "does not exist" in str(r)):
            s, r = gh(token, "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
            break
        time.sleep(2 * (attempt + 1))          # another upload was in flight; try again
    if s not in (200, 201):
        raise SystemExit(f"ref update failed {s}: {r}")


def main():
    if "--node-only" in sys.argv:
        HOME.mkdir(parents=True, exist_ok=True)
        refresh_nodes()
        return
    import fcntl
    HOME.mkdir(parents=True, exist_ok=True)
    lock = open(HOME / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if "--test-alert" not in sys.argv:
            print("another run is in progress; waiting for it")
        fcntl.flock(lock, fcntl.LOCK_EX)        # wait our turn instead of colliding
    env = load_env()
    if "--test-block" in sys.argv:
        # resend a real block alert (latest block found) exactly as it would go out
        hist = json.loads((HOME / "history.json").read_text()) if (HOME / "history.json").exists() else {}
        best = None
        for key, cfg in POOLS.items():
            for b in hist.get("_blockhist", {}).get(key, {}).get("blocks", []):
                if not best or b["time"] > best[2]["time"]:
                    best = (key, cfg, b)
        if not best:
            print("no blocks recorded yet"); return
        alert_block(best[0], best[1], best[2], test=True)
        return
    if "--test-alert" in sys.argv:
        res = push_alert("🧪 TEST · 🏆 BCH BLOCK FOUND! #000,000 🏆",
                         "Rig: TEST-RIG · Diff: 1.81T · Effort: 9.5% · Reward: 3.1251 BCH — this is only a test", tag="test", kind="test")
        print("test alert:", res if res is not None else "not sent (check NEON_PUSH_URL / NEON_PUSH_TOKEN in config.env)")
        return
    HOME.mkdir(parents=True, exist_ok=True)
    hist_file = HOME / "history.json"
    try:
        history = json.loads(hist_file.read_text())
    except Exception:
        history = {}

    if not load_node_cache():
        refresh_nodes()          # first run ever: fetch node info inline once
    else:
        kick_node_refresh()      # later runs: refresh in the background, never wait
    data = {"updated": int(time.time()), "pools": {}}
    for key, cfg in POOLS.items():
        if env.get(f"NEON_SKIP_{key.upper()}") == "1":
            continue
        data["pools"][key] = collect(key, cfg, history)
    history.pop("_blocks", None)   # old counter format
    hist_file.write_text(json.dumps(history))
    content = json.dumps(data, separators=(",", ":"))
    (HOME / "stats.json").write_text(content)

    if "--dry-run" in sys.argv or "--cleanup-dry-run" in sys.argv:
        print(json.dumps(data, indent=2)[:6000])
        return
    token, repo = env.get("GITHUB_TOKEN"), env.get("NEON_REPO")
    if not token or not repo:
        raise SystemExit("set GITHUB_TOKEN and NEON_REPO in config.env")
    publish(token, repo, env.get("NEON_BRANCH", "data"), content)
    print(f"pushed {len(content)} bytes to {repo}@{env.get('NEON_BRANCH', 'data')}")


if __name__ == "__main__":
    main()
