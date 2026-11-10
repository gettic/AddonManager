// Send-a-backup relay for the Addon Manager (am/share.py). Cloudflare Worker + one Durable
// Object per code ("mailbox"). The relay only passes bytes along: it never sees the file or
// the code's words (SPAKE2 + ChaCha20-Poly1305 happen on the two PCs), only the channel number,
// sizes and times. Nothing is logged; IP addresses are only used (hashed) for rate limits and
// never stored. A mailbox is deleted when the transfer ends, is cancelled, a wrong code is
// used, or it runs out of time. tests/fake_relay.py follows the same rules.
//
// API (all under /v1, token in the X-Token header, errors are {"error": "<kind>"}):
//   POST /v1/open                    -> {ch, token, ttl}        sender gets a free channel
//   POST /v1/join/<ch>  (body: id)   -> {token}                 first join only (409 used)
//   PUT  /v1/<ch>/msg/<slot>         write-once, <= 4 KB; sender: pakeA confA header,
//                                    receiver: pakeB confB done
//   GET  /v1/<ch>/msg/<slot>?wait=s  the other side's message; 204 = nothing yet
//   PUT  /v1/<ch>/chunk/<i>?wait=s   sender, in order; 429 {"error":"full"} if BUFFER wait
//   GET  /v1/<ch>/chunk/<i>?wait=s   receiver; deletes chunks before i (they arrived)
//   POST /v1/<ch>/close  {"reason"}  done / cancel / bad_code: everything deleted now
// Closed mailboxes answer 410 {"error": reason} for 5 minutes, then are gone (404).
// Plain http:// is refused (403 https_only), except on a local test address.

const SEND_SLOTS = new Set(["pakeA", "confA", "header"]);
const RECV_SLOTS = new Set(["pakeB", "confB", "done"]);
const REASONS = new Set(["done", "cancel", "bad_code"]);
const MSG_MAX = 4096;
const MAX_WAIT = 20;          // seconds a poll is held
const TOMBSTONE = 300;        // seconds a closed mailbox still answers "closed"

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}
const err = (status, kind) => json(status, { error: kind });
const empty = (status = 204) => new Response(null, { status, headers: { "Cache-Control": "no-store" } });

function num(env, name, fallback) {
  const v = Number(env[name]);
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256hex(text) {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

function randomToken() {
  return hex(crypto.getRandomValues(new Uint8Array(16)));
}

// Two limits per IP for new codes and code tries: Cloudflare's limiter (fast, approximate,
// per location) and a hard cap counted in a small Durable Object per IP (RateBox; memory
// only, nothing written). The IP is only used hashed, with the day in the hash.
async function allowed(env, request, kind) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const limiter = kind === "open" ? env.OPEN_LIMIT : env.JOIN_LIMIT;
  if (limiter) {
    const { success } = await limiter.limit({ key: await sha256hex("am-share|" + ip) });
    if (!success) return false;
  }
  if (!env.RATE) return true;
  const day = new Date().toISOString().slice(0, 10);
  const key = await sha256hex(`am-share|${day}|${ip}`);
  const max = num(env, kind === "open" ? "HARD_OPEN" : "HARD_JOIN", 20);
  const window = num(env, "HARD_WINDOW", 600);
  const r = await env.RATE.get(env.RATE.idFromName(key))
    .fetch(`https://rate/?kind=${kind}&max=${max}&window=${window}`, { method: "POST" });
  return r.status === 204;
}

export class RateBox {
  constructor() {
    this.hits = new Map();   // kind -> [times]; memory only
  }

  async fetch(request) {
    const url = new URL(request.url);
    const kind = url.searchParams.get("kind") === "join" ? "join" : "open";
    const max = Number(url.searchParams.get("max")) || 20;
    const window = (Number(url.searchParams.get("window")) || 600) * 1000;
    const now = Date.now();
    const list = (this.hits.get(kind) || []).filter((t) => now - t < window);
    if (list.length >= max) {
      this.hits.set(kind, list);
      return empty(429);
    }
    list.push(now);
    this.hits.set(kind, list);
    return empty();
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const local = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    if (url.protocol !== "https:" && !local) return err(403, "https_only");
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts[0] !== "v1") return err(404, "not_found");
    const maxCh = num(env, "MAX_CHANNEL", 499);
    const len = Number(request.headers.get("Content-Length") || 0);
    if (len > num(env, "CHUNK_MAX", 1048576 + 64)) return err(413, "too_big");

    if (parts.length === 2 && parts[1] === "open" && request.method === "POST") {
      if (!(await allowed(env, request, "open"))) return err(429, "rate");
      for (let tries = 0; tries < 12; tries++) {
        const ch = 1 + (crypto.getRandomValues(new Uint32Array(1))[0] % maxCh);
        const stub = env.BOX.get(env.BOX.idFromName("ch" + ch));
        const r = await stub.fetch(`https://box/open?ch=${ch}`, { method: "POST" });
        if (r.status !== 409) return r;
      }
      return err(503, "busy");
    }

    let ch, rest;
    if (parts.length === 3 && parts[1] === "join" && request.method === "POST") {
      if (!(await allowed(env, request, "join"))) return err(429, "rate");
      ch = parts[2];
      rest = "join";
    } else if (parts.length >= 3) {
      ch = parts[1];
      rest = parts.slice(2).join("/");
    } else {
      return err(404, "not_found");
    }
    if (!/^[0-9]{1,3}$/.test(ch) || Number(ch) < 1 || Number(ch) > maxCh) {
      return err(404, "not_found");
    }
    const stub = env.BOX.get(env.BOX.idFromName("ch" + Number(ch)));
    const headers = new Headers();
    const token = request.headers.get("X-Token");
    if (token) headers.set("X-Token", token);
    return stub.fetch(`https://box/${rest}${url.search}`, {
      method: request.method, headers,
      body: request.method === "GET" ? undefined : request.body,
    });
  },
};

export class ShareBox {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.waiters = new Set();
    this.meta = null;
    this.slots = new Map();
    this.chunks = new Map();
    ctx.blockConcurrencyWhile(async () => {
      const all = await ctx.storage.list();
      for (const [key, value] of all) {
        if (key === "meta") this.meta = value;
        else if (key.startsWith("slot:")) this.slots.set(key.slice(5), value);
        else if (key.startsWith("chunk:")) this.chunks.set(Number(key.slice(6)), value);
      }
      if (this.meta) this.meta.last = Date.now();   // activity time lives in memory only
    });
  }

  get ttl() { return num(this.env, "TTL_SECONDS", 600) * 1000; }
  get idle() { return num(this.env, "IDLE_SECONDS", 120) * 1000; }
  get maxTime() { return num(this.env, "MAX_SECONDS", 3600) * 1000; }
  get buffer() { return num(this.env, "BUFFER", 4); }

  notify() {
    for (const resolve of this.waiters) resolve();
    this.waiters.clear();
  }

  async waitFor(check, seconds) {
    const end = Date.now() + Math.max(0, Math.min(seconds, MAX_WAIT)) * 1000;
    for (;;) {
      const value = check();
      if (value !== undefined) return value;
      const left = end - Date.now();
      if (left <= 0) return undefined;
      await new Promise((resolve) => {
        this.waiters.add(resolve);
        setTimeout(resolve, Math.min(left, 5000));
      });
    }
  }

  saveMeta() {
    const { last, ...stored } = this.meta;   // eslint-disable-line no-unused-vars
    this.ctx.storage.put("meta", stored);
  }

  async close(reason) {
    const now = Date.now();
    this.slots.clear();
    this.chunks.clear();
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    this.meta = { closed: reason, closedAt: now, last: now };
    this.saveMeta();
    await this.ctx.storage.setAlarm(now + TOMBSTONE * 1000);
    this.notify();
  }

  async forget() {
    this.meta = null;
    this.slots.clear();
    this.chunks.clear();
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    this.notify();
  }

  // Closes or forgets an old mailbox. True if there is none (or it is gone now).
  async expire() {
    const m = this.meta;
    if (!m) return true;
    const now = Date.now();
    if (m.closed) {
      if (now - m.closedAt > TOMBSTONE * 1000) { await this.forget(); return true; }
      return false;
    }
    if (!m.recvHash ? now - m.created > this.ttl
      : now - m.last > this.idle || now - m.joinedAt > this.maxTime) {
      await this.close("expired");
    }
    return false;
  }

  async alarm() {
    await this.expire();
    const m = this.meta;
    if (m && !m.closed) {
      const next = m.recvHash ? Math.min(m.last + this.idle, m.joinedAt + this.maxTime)
        : m.created + this.ttl;
      await this.ctx.storage.setAlarm(Math.max(next, Date.now()) + 1000);
    }
  }

  async fetch(request) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);
    const wait = Number(url.searchParams.get("wait") || 0) || 0;
    const now = Date.now();
    const gone = await this.expire();

    if (parts[0] === "open") {
      if (!gone) return err(409, "busy");
      const token = randomToken();
      this.meta = {
        ch: Number(url.searchParams.get("ch")), created: now, last: now,
        sendHash: await sha256hex(token), recvHash: null, jid: null, joinedAt: null,
        nextPut: 1, acked: 0, bytes: 0, closed: null,
      };
      this.saveMeta();
      await this.ctx.storage.setAlarm(now + this.ttl + 1000);
      return json(200, { ch: this.meta.ch, token, ttl: Math.round(this.ttl / 1000) });
    }

    const m = this.meta;
    if (!m) return err(404, "not_found");
    if (m.closed) return err(410, m.closed);

    if (parts[0] === "join") {
      const body = await request.text();
      if (body.length > 64) return err(413, "too_big");
      const jid = await sha256hex(body);
      if (m.recvHash) {
        if (m.jid === jid && m.recvToken) return json(200, { token: m.recvToken });
        return err(409, "used");
      }
      const token = randomToken();
      // The plain token is kept only so a repeated join (same id, after a network blip)
      // gets the same answer; it dies with the mailbox.
      Object.assign(m, { recvHash: await sha256hex(token), recvToken: token, jid,
        joinedAt: now, last: now });
      this.saveMeta();
      await this.ctx.storage.setAlarm(now + this.idle + 1000);
      this.notify();
      return json(200, { token });
    }

    const tokenHash = await sha256hex(request.headers.get("X-Token") || "");
    const side = tokenHash === m.sendHash ? "send" : m.recvHash && tokenHash === m.recvHash
      ? "recv" : null;
    if (!side) return err(403, "token");
    m.last = now;

    if (parts[0] === "close" && request.method === "POST") {
      let reason = "cancel";
      try {
        const r = JSON.parse((await request.text()).slice(0, 200)).reason;
        if (REASONS.has(r)) reason = r;
      } catch { /* cancel */ }
      await this.close(reason);
      return empty();
    }
    if (parts[0] === "msg" && parts.length === 2) return this.msg(request, side, parts[1], wait);
    if (parts[0] === "chunk" && parts.length === 2 && /^[0-9]{1,6}$/.test(parts[1])) {
      return this.chunk(request, side, Number(parts[1]), wait);
    }
    return err(404, "not_found");
  }

  async msg(request, side, slot, wait) {
    const own = side === "send" ? SEND_SLOTS : RECV_SLOTS;
    if (!SEND_SLOTS.has(slot) && !RECV_SLOTS.has(slot)) return err(404, "not_found");
    if (request.method === "PUT") {
      if (!own.has(slot)) return err(403, "slot");
      const data = new Uint8Array(await request.arrayBuffer());
      if (data.length > MSG_MAX) return err(413, "too_big");
      const old = this.slots.get(slot);
      if (old) {
        const same = old.length === data.length && old.every((b, i) => b === data[i]);
        return same ? empty() : err(409, "taken");
      }
      this.slots.set(slot, data);
      this.ctx.storage.put("slot:" + slot, data);
      this.notify();
      return empty();
    }
    if (request.method !== "GET" || own.has(slot)) return err(403, "slot");
    const got = await this.waitFor(() => (this.meta?.closed || !this.meta ? null
      : this.slots.get(slot)), wait);
    if (got === null) return err(410, this.meta?.closed || "cancel");
    if (got === undefined) return empty();
    return new Response(got, { headers: { "Content-Type": "application/octet-stream" } });
  }

  async chunk(request, side, index, wait) {
    const m = this.meta;
    if (request.method === "PUT") {
      if (side !== "send") return err(403, "side");
      const data = new Uint8Array(await request.arrayBuffer());
      if (index < m.nextPut) return empty();            // a repeat after a network blip
      if (index !== m.nextPut) return err(409, "order");
      if (data.length > num(this.env, "CHUNK_MAX", 1048576 + 64)) return err(413, "too_big");
      if (m.bytes + data.length > num(this.env, "MAX_BYTES", 545259520)) {
        await this.close("cancel");
        return err(413, "too_big");
      }
      const room = await this.waitFor(() => (!this.meta || this.meta.closed ? null
        : this.chunks.size < this.buffer ? true : undefined), wait);
      if (room === null) return err(410, this.meta?.closed || "cancel");
      if (room === undefined) return err(429, "full");
      if (index !== m.nextPut) return empty();          // stored by a repeat meanwhile
      this.chunks.set(index, data);
      this.ctx.storage.put("chunk:" + index, data);
      m.bytes += data.length;
      m.nextPut += 1;
      this.saveMeta();
      this.notify();
      return empty();
    }
    if (request.method !== "GET" || side !== "recv") return err(403, "side");
    if (index <= m.acked) return err(409, "order");
    const done = [...this.chunks.keys()].filter((i) => i < index);
    for (const i of done) {
      this.chunks.delete(i);
      this.ctx.storage.delete("chunk:" + i);
    }
    if (done.length || m.acked !== index - 1) {
      m.acked = index - 1;
      this.saveMeta();
    }
    this.notify();
    const got = await this.waitFor(() => (this.meta?.closed || !this.meta ? null
      : this.chunks.get(index)), wait);
    if (got === null) return err(410, this.meta?.closed || "cancel");
    if (got === undefined) return empty();
    return new Response(got, { headers: { "Content-Type": "application/octet-stream" } });
  }
}
