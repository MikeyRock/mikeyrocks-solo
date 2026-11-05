// MikeyRocks//Solo — push alert Worker (Cloudflare Workers, module syntax).
//
// Bindings (Settings → Bindings / Variables in the Cloudflare dashboard):
//   KV namespace  SUBS          stores phone subscriptions + the VAPID keypair
//   Secret        NOTIFY_TOKEN  shared with the Umbrel; required to send alerts
//   Variable      ALLOW_ORIGIN  e.g. https://mikeyrock.github.io  (optional, default *)
//
// Routes:
//   GET  /vapid                      -> { publicKey }   (keypair is created on first use)
//   POST /subscribe   {subscription, prefs:{block,bestdiff,offline}} -> 201  (also used to change prefs)
//   POST /unsubscribe {endpoint}     -> 200
//   POST /notify  (Bearer NOTIFY_TOKEN) {type, title, body, url?, tag?} -> { sent, skipped, removed, failed }
//        type: "block" | "bestdiff" | "offline" | "test" — each device only gets the types it turned on
//   GET  /health                     -> { subscribers }
//   GET  /stats                      -> latest stats.json (from GitHub, cached 10 s)  needs secret GH_TOKEN, var STATS_REPO
//
// Web Push (RFC 8030/8291/8292) is implemented with WebCrypto only; no dependencies.

const enc = new TextEncoder();
const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64u = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0));
const concat = (...a) => { const o = new Uint8Array(a.reduce((n, x) => n + x.length, 0)); let i = 0; for (const x of a) { o.set(x, i); i += x.length; } return o; };

function cors(env, extra = {}) {
  return { "Access-Control-Allow-Origin": env.ALLOW_ORIGIN || "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
           "Access-Control-Allow-Headers": "Content-Type,Authorization", "Access-Control-Max-Age": "86400", ...extra };
}
const TYPES = ["block", "bestdiff", "offline"];
const cleanPrefs = p => Object.fromEntries(TYPES.map(t => [t, p ? !!p[t] : t === "block"]));
const json = (env, data, status = 200) => new Response(JSON.stringify(data), { status, headers: cors(env, { "Content-Type": "application/json" }) });

// ---------- VAPID keys (generated once, kept in KV) ----------
async function vapidKeys(env) {
  let k = await env.SUBS.get("_vapid", "json");
  if (!k) {
    const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
    k = { privateJwk: await crypto.subtle.exportKey("jwk", kp.privateKey),
          publicRaw: b64u(await crypto.subtle.exportKey("raw", kp.publicKey)) };
    await env.SUBS.put("_vapid", JSON.stringify(k));
  }
  return k;
}

async function vapidAuth(env, endpoint) {
  const k = await vapidKeys(env);
  const key = await crypto.subtle.importKey("jwk", k.privateJwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const header = b64u(enc.encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const claims = b64u(enc.encode(JSON.stringify({ aud: new URL(endpoint).origin, exp: Math.floor(Date.now() / 1000) + 12 * 3600,
                                                   sub: env.VAPID_SUBJECT || "mailto:alerts@example.com" })));
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, enc.encode(`${header}.${claims}`));
  return `vapid t=${header}.${claims}.${b64u(sig)}, k=${k.publicRaw}`;
}

// ---------- RFC 8291 aes128gcm payload encryption ----------
async function hkdf(salt, ikm, info, len) {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, len * 8));
}

async function encryptPayload(sub, payload) {
  const uaPublic = unb64u(sub.keys.p256dh), authSecret = unb64u(sub.keys.auth);
  const as = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const asPublic = new Uint8Array(await crypto.subtle.exportKey("raw", as.publicKey));
  const uaKey = await crypto.subtle.importKey("raw", uaPublic, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: uaKey }, as.privateKey, 256));

  const ikm = await hkdf(authSecret, shared, concat(enc.encode("WebPush: info\0"), uaPublic, asPublic), 32);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(salt, ikm, enc.encode("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, enc.encode("Content-Encoding: nonce\0"), 12);

  const aes = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, aes, concat(enc.encode(payload), new Uint8Array([2]))));
  const rs = new Uint8Array([0, 0, 16, 0]);                 // record size 4096
  return concat(salt, rs, new Uint8Array([asPublic.length]), asPublic, ct);
}

async function sendPush(env, sub, payload) {
  const body = await encryptPayload(sub, payload);
  return fetch(sub.endpoint, { method: "POST", body, headers: {
    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
    TTL: "86400", Urgency: "high", Authorization: await vapidAuth(env, sub.endpoint) } });
}

// ---------- subscription storage ----------
async function subKey(endpoint) {
  return "sub:" + b64u(await crypto.subtle.digest("SHA-256", enc.encode(endpoint))).slice(0, 32);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(env) });

    try {
      if (req.method === "GET" && url.pathname === "/vapid")
        return json(env, { publicKey: (await vapidKeys(env)).publicRaw });

      if (req.method === "GET" && url.pathname === "/stats") {
        const cache = caches.default, ck = new Request("https://stats.cache/v1");
        let hit = await cache.match(ck);
        if (!hit) {
          const repo = env.STATS_REPO || "MikeyRock/mikeyrocks-solo";
          const gh = await fetch(`https://api.github.com/repos/${repo}/contents/stats.json?ref=data`, { headers: {
            Accept: "application/vnd.github.raw+json", "User-Agent": "mikeyrocks-solo-worker",
            ...(env.GH_TOKEN ? { Authorization: `Bearer ${env.GH_TOKEN}` } : {}) } });
          if (!gh.ok) return json(env, { error: "github " + gh.status }, 502);
          hit = new Response(await gh.text(), { headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=10" } });
          await cache.put(ck, hit.clone());
        }
        return new Response(hit.body, { headers: cors(env, { "Content-Type": "application/json", "Cache-Control": "no-store" }) });
      }

      if (req.method === "GET" && url.pathname === "/health") {
        const list = await env.SUBS.list({ prefix: "sub:" });
        return json(env, { ok: true, subscribers: list.keys.length });
      }

      if (req.method === "POST" && url.pathname === "/subscribe") {
        const { subscription: s, prefs } = await req.json();
        if (!s?.endpoint?.startsWith("https://") || !s.keys?.p256dh || !s.keys?.auth) return json(env, { error: "bad subscription" }, 400);
        await env.SUBS.put(await subKey(s.endpoint), JSON.stringify({ endpoint: s.endpoint, keys: s.keys, prefs: cleanPrefs(prefs), added: Date.now() }));
        return json(env, { ok: true }, 201);
      }

      if (req.method === "POST" && url.pathname === "/unsubscribe") {
        const { endpoint } = await req.json();
        if (endpoint) await env.SUBS.delete(await subKey(endpoint));
        return json(env, { ok: true });
      }

      if (req.method === "POST" && url.pathname === "/notify") {
        if (!env.NOTIFY_TOKEN || req.headers.get("Authorization") !== `Bearer ${env.NOTIFY_TOKEN}`) return json(env, { error: "unauthorized" }, 401);
        const msg = await req.json();
        const type = msg.type || "block";
        const payload = JSON.stringify({ title: String(msg.title || "Block found!").slice(0, 120), body: String(msg.body || "").slice(0, 500),
                                         url: msg.url || "./", tag: msg.tag || "block" });
        let sent = 0, skipped = 0, removed = 0, failed = 0, cursor;
        do {
          const page = await env.SUBS.list({ prefix: "sub:", cursor });
          await Promise.all(page.keys.map(async ({ name }) => {
            const sub = await env.SUBS.get(name, "json");
            if (!sub) return;
            if (type !== "test" && !cleanPrefs(sub.prefs)[type]) { skipped++; return; }
            const r = await sendPush(env, sub, payload).catch(() => null);
            if (r && r.status < 300) sent++;
            else if (r && (r.status === 404 || r.status === 410)) { await env.SUBS.delete(name); removed++; }
            else failed++;
          }));
          cursor = page.list_complete ? null : page.cursor;
        } while (cursor);
        return json(env, { sent, skipped, removed, failed });
      }

      return json(env, { error: "not found" }, 404);
    } catch (e) {
      return json(env, { error: String(e?.message || e) }, 500);
    }
  },
};
