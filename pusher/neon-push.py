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
    },
}
HISTORY_SECS = 24 * 3600
OFFLINE_SECS = 10 * 60
RIG_BEST_COOLDOWN = 10 * 60   # at most one 🎯 alert per rig per 10 min
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


def rpc(cfg, *args):
    try:
        r = subprocess.run(["docker", "exec", cfg["container"], *cfg["cli"], *args],
                           capture_output=True, text=True, timeout=30)
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


def alert_block(key, cfg, b):
    coin = cfg["coin"]
    stats = rpc(cfg, "getblockstats", str(b["height"]), '["subsidy","totalfee"]') or {}
    reward = (stats.get("subsidy") or ((50 * 100_000_000) >> (b["height"] // 210_000))) + (stats.get("totalfee") or 0)
    parts = [f"Rig: {rig_or_default(b['rig'])}"]
    if b.get("diff"):
        parts.append(f"Diff: {fmt_si(b['diff'])}")
    if b.get("effort") is not None:
        parts.append(f"Effort: {b['effort']:.1f}%")
    parts.append(f"Reward: {reward / 1e8:.4f} {coin}")
    res = push_alert(f"⛏️ BLOCK FOUND! {coin} #{b['height']:,}", " · ".join(parts), tag=f"block-{key}-{b['height']}")
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


def collect(key, cfg, history):
    now = int(time.time())
    out = {"name": cfg["name"], "coin": cfg["coin"], "updated": now}

    # --- node ---
    bci = rpc(cfg, "getblockchaininfo") or {}
    net = rpc(cfg, "getnetworkinfo") or {}
    out["node"] = {
        "blocks": bci.get("blocks"),
        "headers": bci.get("headers"),
        "progress": bci.get("verificationprogress"),
        "synced": bool(bci) and not bci.get("initialblockdownload", True) and bci.get("blocks") == bci.get("headers"),
        "difficulty": bci.get("difficulty"),
        "peers": net.get("connections"),
        "subversion": net.get("subversion"),
        "online": bool(bci),
    }

    # --- reward for the block being mined right now: subsidy + fees in the template ---
    if bci.get("blocks") is not None:
        height = bci["blocks"] + 1
        subsidy = (50 * 100_000_000) >> (height // 210_000)
        gbt = rpc(cfg, "getblocktemplate", *cfg.get("gbt", [])) if out["node"]["synced"] else None
        total = (gbt or {}).get("coinbasevalue")
        out["reward"] = {"height": height, "subsidy_sats": subsidy,
                         "fees_sats": (total - subsidy) if total else None,
                         "total_sats": total or subsidy, "live": bool(total)}

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
            top = " 🏆 pool best!" if cur >= pool_top else ""
            push_alert(f"🎯 NEW BEST DIFF · {cfg['coin']} · {w['name']}",
                       f"{w['name']} hit {fmt_si(cur)} (was {fmt_si(r['alerted'])}){top}",
                       tag=f"best-{key}-{w['name']}", kind="bestdiff")
            r["alerted"], r["at"] = cur, now
    workers.sort(key=lambda w: (-(w["hashrate5m"] or 0)))
    blocks, new_blocks = block_history(key, cfg, history)
    watch_rigs(key, cfg, workers, history, now)
    # only publish rigs that submitted a share in the last 10 minutes
    workers = [w for w in workers if w["lastshare"] and now - w["lastshare"] < OFFLINE_SECS]
    out.update(pool=pool, workers=workers, payout=payout, blocks=blocks, blocks_found=len(blocks),
               shares=recent_shares(cfg))
    cleanup_sharelogs(key, cfg, blocks, history, now, dry_run="--cleanup-dry-run" in sys.argv)

    # --- push alert for blocks found since the last run (skip old ones from the first log read) ---
    for b in new_blocks:
        if now - b["time"] < 2 * 3600:
            alert_block(key, cfg, b)

    # --- history (local file, last 24h of 5m hashrate) ---
    h = [p for p in history.get(key, []) if p[0] > now - HISTORY_SECS]
    if hrs["5m"] is not None and (not h or now - h[-1][0] >= 240):
        h.append([now, hrs["5m"]])
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
    if "--test-alert" in sys.argv:
        res = push_alert("🧪 TEST · BLOCK FOUND! BCH #000,000",
                         "Rig: TEST-RIG · Diff: 1.81T · Effort: 9.5% · Reward: 3.1251 BCH — this is only a test", tag="test", kind="test")
        print("test alert:", res if res is not None else "not sent (check NEON_PUSH_URL / NEON_PUSH_TOKEN in config.env)")
        return
    HOME.mkdir(parents=True, exist_ok=True)
    hist_file = HOME / "history.json"
    try:
        history = json.loads(hist_file.read_text())
    except Exception:
        history = {}

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
