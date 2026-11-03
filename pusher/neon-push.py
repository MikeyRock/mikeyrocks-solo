#!/usr/bin/env python3
"""
neon-push.py — read AxeBTC / AxeBCH (ckpool-solo) stats on an Umbrel and publish a
sanitized, read-only stats.json to a GitHub branch. Nothing is exposed inbound.

Runs as root (needs docker + the app-data folders). Python stdlib only.
Config: /home/umbrel/neon-solo/config.env   (see install.sh)
"""
import calendar, json, os, re, subprocess, sys, time, base64, urllib.request, urllib.error
from pathlib import Path

HOME = Path(os.environ.get("NEON_HOME", "/home/umbrel/neon-solo"))
APPDATA = Path("/home/umbrel/umbrel/app-data")

POOLS = {
    "btc": {
        "name": "AxeBTC", "coin": "BTC",
        "pooldir": APPDATA / "willitmod-btc/data/pool/www",
        "container": "willitmod-btc_bitcoind_1",
        "cli": ["bitcoin-cli", "-datadir=/data"],
        "ckpool_container": "willitmod-btc_ckpool_1",
    },
    "bch": {
        "name": "AxeBCH", "coin": "BCH",
        "pooldir": APPDATA / "willitmod-dev-bch/data/pool/www",
        "container": "willitmod-dev-bch_bchn_1",
        "cli": ["bitcoin-cli", "-datadir=/data"],
        "ckpool_container": "willitmod-dev-bch_ckpool_1",
    },
}
HISTORY_SECS = 24 * 3600
OFFLINE_SECS = 10 * 60
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
ACCEPTED_RE = re.compile(r"BLOCK ACCEPTED", re.I)
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")


def rig_name(worker):
    """'bc1q...addr.RigName' -> 'RigName' (never publish the address)."""
    return (worker.split(".", 1)[1] if "." in worker else "unnamed")[:32]


def parse_blocks(text):
    """Return [{height, time, rig}] for each block ckpool says it solved."""
    out, pending = [], None
    for line in text.splitlines():
        m = TS_RE.match(line)
        ts = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else int(time.time())
        s = SOLVED_RE.search(line)
        if s:
            out.append({"height": int(s.group(1)), "time": ts, "rig": rig_name(s.group(2))})
            pending = None
        elif ACCEPTED_RE.search(line):
            pending = ts          # accepted but no "solved by" line (yet)
    if pending and not out:
        out.append({"height": None, "time": pending, "rig": "unknown"})
    return out


def block_history(key, cfg, state):
    """Keep a permanent list of blocks found. Only reads log lines since the last run;
    the list lives in the local state file so it survives ckpool restarts."""
    st = state.setdefault("_blockhist", {}).setdefault(key, {"since": None, "blocks": []})
    now = int(time.time())
    cmd = ["docker", "logs", "--timestamps", cfg["ckpool_container"]]
    if st["since"]:
        cmd[2:2] = ["--since", str(st["since"])]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            seen = {(b["height"], b["time"] // 600) for b in st["blocks"]}
            for b in parse_blocks(r.stdout + r.stderr):
                if (b["height"], b["time"] // 600) not in seen and not any(
                        b["height"] and b["height"] == x["height"] for x in st["blocks"]):
                    st["blocks"].append(b)
            st["since"] = now
    except Exception:
        pass
    st["blocks"].sort(key=lambda b: b["time"], reverse=True)
    return st["blocks"]


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
    pool["bestever"] = max(bestever, pool.get("bestshare") or 0) or None
    workers.sort(key=lambda w: (-(w["hashrate5m"] or 0)))
    blocks = block_history(key, cfg, history)
    # only publish rigs that submitted a share in the last 10 minutes
    workers = [w for w in workers if w["lastshare"] and now - w["lastshare"] < OFFLINE_SECS]
    out.update(pool=pool, workers=workers, payout=payout, blocks=blocks, blocks_found=len(blocks))

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
    s, r = gh(token, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"], "force": True})
    if s == 422 or s == 404:
        s, r = gh(token, "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    if s not in (200, 201):
        raise SystemExit(f"ref update failed {s}: {r}")


def main():
    env = load_env()
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

    if "--dry-run" in sys.argv:
        print(json.dumps(data, indent=2)[:6000])
        return
    token, repo = env.get("GITHUB_TOKEN"), env.get("NEON_REPO")
    if not token or not repo:
        raise SystemExit("set GITHUB_TOKEN and NEON_REPO in config.env")
    publish(token, repo, env.get("NEON_BRANCH", "data"), content)
    print(f"pushed {len(content)} bytes to {repo}@{env.get('NEON_BRANCH', 'data')}")


if __name__ == "__main__":
    main()
