# MikeyRocks//Solo

Public, read-only status page for my home solo-mining setup: **AxeBTC** and **AxeBCH** (ckpool-solo) running on Umbrel.

**Live:** https://mikeyrock.github.io/mikeyrocks-solo/

## How it works

```
Umbrel ──(every 1 min, outbound only)──▶ GitHub `data` branch: stats.json ──▶ GitHub Pages (index.html)
```

- `pusher/neon-push.py` runs on the Umbrel. It reads ckpool's `pool.status` and `users/` files plus the node's
  `getblockchaininfo`, then pushes a **sanitized** `stats.json` to the `data` branch. Payout addresses are
  shortened, and worker names have the address stripped off.
- The `data` branch is a single orphan commit that gets force-updated each time, so the repo history doesn't grow.
- Nothing on the Umbrel is reachable from the internet. No ports are opened, and the AxeBTC/AxeBCH UIs stay private.

## Install the pusher (on the Umbrel)

1. Create a **fine-grained token**: GitHub → Settings → Developer settings → Fine-grained tokens.
   For repository access, choose *Only select repositories* → `mikeyrocks-solo`. For permissions, set **Contents: Read and write**. Nothing else is needed.
2. Run:
   ```bash
   git clone https://github.com/MikeyRock/mikeyrocks-solo.git ~/mikeyrocks-solo-src
   sudo bash ~/mikeyrocks-solo-src/pusher/install.sh
   ```
3. Check that it's running: `systemctl status mikeyrocks-solo.timer` and `journalctl -u mikeyrocks-solo -n 20`.

To pause publishing, run `sudo systemctl disable --now mikeyrocks-solo.timer`.
To hide one pool, add `NEON_SKIP_BCH=1` (or `NEON_SKIP_BTC=1`) to `/home/umbrel/mikeyrocks-solo/config.env`.

If the page shows **DEMO DATA**, it hasn't received a `stats.json` from the Umbrel yet.

## Block alerts (push notifications)

`worker/worker.js` is a Cloudflare Worker that stores subscribed devices and sends web-push alerts. The page's
🔔 toggle subscribes a device, and the Umbrel script calls the Worker's `/notify` endpoint when a new block shows up in `ckpool.log`.

- Bindings: KV namespace `SUBS`, secret `NOTIFY_TOKEN`, and optionally the variable `ALLOW_ORIGIN=https://mikeyrock.github.io`
- Page: set `PUSH_API` in `index.html` to the Worker URL
- Umbrel: add `NEON_PUSH_URL=` and `NEON_PUSH_TOKEN=` to `/home/umbrel/neon-solo/config.env`
- Test: `sudo python3 /home/umbrel/neon-solo/neon-push.py --test-alert`
- iPhone: Safari → Share → **Add to Home Screen**, open it from the home screen, then tap 🔔 (needs iOS 16.4+)

## Rental scoreboard

Put your hashrate rentals in `/home/umbrel/neon-solo/rentals.json` on the Umbrel. Blocks found by that rig name, on that coin, between `start` and `end`, count toward each rental:

```json
[
  {"name": "MRR 84 PH/s · Nov 5", "rig": "MRR",   "coin": "bch", "start": "2026-11-05 00:00", "end": "2026-11-05 12:00", "cost_usd": 1200},
  {"name": "80 PH/s rental",      "rig": "80Phs", "coin": "bch", "start": "2026-11-05 12:00", "end": "",                 "cost_usd": 950}
]
```

- Times are local (`NEON_TZ` in config.env, default `America/Los_Angeles`). Leave `end` empty for a rental that's still running.
- `rig` is the worker name as it appears on the site. Use `"*"` to count every block on that coin during the window, or a list such as `["MRR", "80Phs"]`.
