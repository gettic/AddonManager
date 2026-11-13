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
//   POST /v1/<ch>/ping               keeps the mailbox alive (send later: upload progress)
// Closed mailboxes answer 410 {"error": reason} for 5 minutes, then are gone (404).
// Plain http:// is refused (403 https_only), except on a local test address.
//
// Send later (the player opted in; am/share_later.py): when nobody joined after 5 minutes the
// sender uploads the sealed backup to file.kiwi and leaves a small sealed record here.
//   POST /v1/<ch>/later {hours, salt, verifier}  sender, before anyone joined (409 joined)
//   POST /v1/<ch>/ping {"p": 0-1000}             sender while uploading (progress, alive)
//   PUT  /v1/<ch>/record  (<= 4 KB)              sender: upload done -> {left} seconds
//   GET  /v1/<ch>/later?wait=s                   anyone: {state, salt, left, p} (no secrets)
//   POST /v1/<ch>/take {verifier}                anyone, rate-limited like join: the record
//                                                once, then the mailbox closes; a wrong
//                                                verifier closes it too (bad_code)
// A new app joins with "<id>|caps=later"; an app without "later" gets 410 "later".
// The record is sealed with a key from the code (scrypt on the PCs); the verifier is a hash.
//
// The guild storage (v0.15.0-v0.16.x: /v1/<ch>/store and /v1/s/...) is gone: those paths
// answer 404, and older apps then upload to their other hosts. Migration v4 deleted its
// StoreLedger/StoreSlice objects and all their data.

const SEND_SLOTS = new Set(["pakeA", "confA", "header"]);
const RECV_SLOTS = new Set(["pakeB", "confB", "done"]);
const REASONS = new Set(["done", "cancel", "bad_code"]);
const MSG_MAX = 4096;
const MAX_WAIT = 20;          // seconds a poll is held
const TOMBSTONE = 300;        // seconds a closed mailbox still answers "closed"
const BOX_ROUTES = new Set(["msg", "chunk", "close", "ping", "later", "record", "take"]);
const RECORD_MAX = 4096;

// What the receiving app can do, from its join body ("<id>|caps=later").
function joinCaps(body) {
  const m = /\|caps=([a-z0-9,]{1,30})$/.exec(body);
  return m ? m[1].split(",") : [];
}

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

// Read a request body, at most `max` bytes (null if longer), so a body without a
// Content-Length can't fill a mailbox's memory.
async function readCapped(request, max) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const got = [];
  let n = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    n += value.byteLength;
    if (n > max) {
      try { await reader.cancel(); } catch { /* gone */ }
      return null;
    }
    got.push(value);
  }
  const out = new Uint8Array(n);
  let o = 0;
  for (const g of got) { out.set(g, o); o += g.byteLength; }
  return out;
}

async function readText(request, max) {
  const data = await readCapped(request, max);
  return data === null ? null : new TextDecoder().decode(data);
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
  const limiter = kind === "open" ? env.OPEN_LIMIT : kind === "join" ? env.JOIN_LIMIT : null;
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
    const asked = url.searchParams.get("kind");
    const kind = asked === "join" ? asked : "open";
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
    const token = request.headers.get("X-Token");
    if (rest !== "join" && !BOX_ROUTES.has(rest.split("/")[0])) return err(404, "not_found");
    // Taking a send-later record is a code try: the same per-IP limits as join.
    if (rest === "take" && !(await allowed(env, request, "join"))) return err(429, "rate");
    const headers = new Headers();
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
      if (this.meta) {   // activity times live in memory only
        const now = Date.now();
        Object.assign(this.meta, { last: now, sendLast: now });
      }
    });
  }

  get ttl() { return num(this.env, "TTL_SECONDS", 600) * 1000; }
  get idle() { return num(this.env, "IDLE_SECONDS", 120) * 1000; }
  get maxTime() { return num(this.env, "MAX_SECONDS", 3600) * 1000; }
  get buffer() { return num(this.env, "BUFFER", 4); }
  get laterIdle() { return num(this.env, "LATER_IDLE", 300) * 1000; }
  get laterUpload() { return num(this.env, "LATER_UPLOAD_SECONDS", 21600) * 1000; }
  get laterMaxHours() { return num(this.env, "LATER_MAX_HOURS", 24); }

  // A send-later mailbox: the sender must stay while uploading; then it lasts its hours.
  laterDeadline(m) {
    if (m.later.state === "ready") return m.later.until;
    return Math.min(m.sendLast + this.laterIdle, m.later.started + this.laterUpload);
  }

  // When an open (joined) mailbox runs out.
  deadline(m) {
    return Math.min(m.last + this.idle, m.joinedAt + this.maxTime);
  }

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
    const { last, sendLast, ...stored } = this.meta;   // eslint-disable-line no-unused-vars
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
    if (m.later ? now > this.laterDeadline(m)
      : !m.recvHash ? now - m.created > this.ttl : now > this.deadline(m)) {
      await this.close("expired");
    }
    return false;
  }

  async alarm() {
    await this.expire();
    const m = this.meta;
    if (m && !m.closed) {
      const next = m.later ? this.laterDeadline(m)
        : m.recvHash ? this.deadline(m) : m.created + this.ttl;
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
        sendLast: now,
        sendHash: await sha256hex(token), recvHash: null, jid: null, joinedAt: null,
        nextPut: 1, acked: 0, bytes: 0, closed: null, later: null,
      };
      this.saveMeta();
      await this.ctx.storage.setAlarm(now + this.ttl + 1000);
      return json(200, { ch: this.meta.ch, token, ttl: Math.round(this.ttl / 1000) });
    }

    const m = this.meta;
    if (!m) return err(404, "not_found");
    if (m.closed) return err(410, m.closed);

    if (parts[0] === "join") {
      const body = await readText(request, 64);
      if (body === null) return err(413, "too_big");
      if (m.later) return this.joinLater(body);
      const jid = await sha256hex(body);
      const token = randomToken();
      const recvHash = await sha256hex(token);
      // The last checks and the write with no await in between: the sender may have
      // turned to send later (or closed) while the hashes were made.
      if (this.meta !== m || m.closed) return err(410, this.meta?.closed || "cancel");
      if (m.later) return this.joinLater(body);
      if (m.recvHash) {
        if (m.jid === jid && m.recvToken) return json(200, { token: m.recvToken });
        return err(409, "used");
      }
      // The plain token is kept only so a repeated join (same id, after a network blip)
      // gets the same answer; it dies with the mailbox.
      Object.assign(m, { recvHash, recvToken: token, jid, joinedAt: now, last: now,
        sendLast: now });
      this.saveMeta();
      await this.ctx.storage.setAlarm(now + this.idle + 1000);
      this.notify();
      return json(200, { token });
    }

    // Send later, without a token: the state, and the one try at the record.
    if (parts[0] === "later" && request.method === "GET") {
      return this.laterState(wait, request.headers.get("X-Token") || "");
    }
    if (parts[0] === "take" && request.method === "POST") return this.take(request);

    const tokenHash = await sha256hex(request.headers.get("X-Token") || "");
    const side = tokenHash === m.sendHash ? "send" : m.recvHash && tokenHash === m.recvHash
      ? "recv" : null;
    if (!side) return err(403, "token");
    m.last = now;
    if (side === "send") m.sendLast = now;

    if (parts[0] === "close" && request.method === "POST") {
      let reason = "cancel";
      try {
        const r = JSON.parse(await readText(request, 200)).reason;
        if (REASONS.has(r)) reason = r;
      } catch { /* cancel */ }
      await this.close(reason);
      return empty();
    }
    if (parts[0] === "ping" && request.method === "POST") {
      if (m.later && side === "send") {   // upload progress for the receiver (memory only)
        try {
          const p = JSON.parse(await readText(request, 100)).p;
          if (Number.isInteger(p) && p >= 0 && p <= 1000) this.laterP = p;
        } catch { /* a plain ping */ }
        this.notify();
      }
      return empty();
    }
    if (parts[0] === "later" && request.method === "POST") return this.startLater(request, side);
    if (parts[0] === "record" && request.method === "PUT") return this.putRecord(request, side);
    if (parts[0] === "msg" && parts.length === 2) return this.msg(request, side, parts[1], wait);
    if (parts[0] === "chunk" && parts.length === 2 && /^[0-9]{1,6}$/.test(parts[1])) {
      return this.chunk(request, side, Number(parts[1]), wait);
    }
    return err(404, "not_found");
  }

  // ---------- send later ----------
  async startLater(request, side) {
    const m = this.meta;
    if (side !== "send") return err(403, "side");
    if (m.recvHash) return err(409, "joined");   // the other PC came just in time: live
    let body;
    try {
      body = JSON.parse(await readText(request, 400));
    } catch {
      return err(400, "bad");
    }
    const hours = body && body.hours;
    const salt = body && body.salt;
    const verifier = body && body.verifier;
    if (!Number.isInteger(hours) || hours < 1 || hours > this.laterMaxHours
        || typeof salt !== "string" || !/^[0-9a-f]{32}$/.test(salt)
        || typeof verifier !== "string" || !/^[0-9a-f]{64}$/.test(verifier)) {
      return err(400, "bad");
    }
    // One watch token for the box: a join (rate-limited) hands it out, and GET /later
    // needs it, so pending codes can't be found without paying for code tries.
    const watch = randomToken();
    const watchHash = await sha256hex(watch);
    // The last checks and the write with no await in between (a join may have come
    // while the body was read or the hash made).
    if (this.meta !== m || m.closed) return err(410, this.meta?.closed || "cancel");
    if (m.recvHash) return err(409, "joined");
    if (m.later) {   // a repeat after a network blip
      return m.later.salt === salt && m.later.verifier === verifier ? empty()
        : err(409, "taken");
    }
    const now = Date.now();
    m.later = { hours, salt, verifier, state: "uploading", started: now, until: null,
      record: null, watch, watchHash };
    m.sendLast = now;
    this.laterP = 0;
    this.saveMeta();
    await this.ctx.storage.setAlarm(this.laterDeadline(m) + 1000);
    this.notify();
    return empty();
  }

  async putRecord(request, side) {
    const m = this.meta;
    if (side !== "send") return err(403, "side");
    if (!m.later) return err(409, "order");
    const data = await readCapped(request, RECORD_MAX);
    if (data === null || data.length < 28) return err(413, "too_big");
    const b64 = btoa(String.fromCharCode(...data));
    if (m.later.state === "ready") {
      return m.later.record === b64 ? json(200, { left: Math.round((m.later.until - Date.now()) / 1000) })
        : err(409, "taken");
    }
    const now = Date.now();
    const until = now + m.later.hours * 3600000;
    Object.assign(m.later, { state: "ready", record: b64, until });
    this.saveMeta();
    await this.ctx.storage.setAlarm(m.later.until + 1000);
    this.notify();
    return json(200, { left: Math.round((until - now) / 1000) });
  }

  joinLater(body) {
    // send later: no live join; a new app goes on to /later and /take with the watch token
    const caps = joinCaps(body);
    if (!caps.includes("later")) return err(410, "later");
    return json(200, { later: true, token: this.meta.later.watch });
  }

  async laterState(wait, token) {
    const m = this.meta;
    if (!m.later) return err(404, "not_found");
    if ((await sha256hex(token)) !== m.later.watchHash) return err(403, "token");
    if (!this.meta || this.meta.closed || !this.meta.later) return err(410, this.meta?.closed || "cancel");
    const start = m.later.state;
    const p = this.laterP || 0;
    // Uploaded already: answer at once (only an upload in progress is worth waiting for).
    if (start === "uploading") {
      await this.waitFor(() => (!this.meta || this.meta.closed || !this.meta.later
        || this.meta.later.state !== start || (this.laterP || 0) !== p ? true : undefined), wait);
    }
    const now = this.meta;
    if (!now || now.closed || !now.later) return err(410, now?.closed || "cancel");
    return json(200, {   // "taking": another /take is on it; ours will get 409
      state: now.later.state === "taking" ? "ready" : now.later.state, salt: now.later.salt, p: this.laterP || 0,
      left: now.later.until ? Math.max(0, Math.round((now.later.until - Date.now()) / 1000)) : null,
    });
  }

  async take(request) {
    const m = this.meta;
    if (!m.later) return err(404, "not_found");
    if (m.later.state === "taking") return err(409, "taking");
    if (m.later.state !== "ready") return err(409, "uploading");
    // Claimed before any await: a /take sent at the same moment gets 409, so each record
    // gets exactly one guess. The claim is never undone: a bad body counts as the try.
    m.later.state = "taking";
    let verifier;
    try {
      verifier = JSON.parse(await readText(request, 200)).verifier;
    } catch {
      verifier = null;
    }
    if (typeof verifier !== "string" || !/^[0-9a-f]{64}$/.test(verifier)) {
      await this.close("bad_code");
      return err(400, "bad");
    }
    // Compare hashes of both, so the time taken says nothing about the stored verifier.
    const same = (await sha256hex(verifier)) === (await sha256hex(m.later.verifier));
    if (!same) {   // one try per code: a wrong one deletes the record
      await this.close("bad_code");
      return err(403, "bad_code");
    }
    const record = m.later.record;
    await this.close("done");
    return json(200, { record });
  }

  async msg(request, side, slot, wait) {
    const own = side === "send" ? SEND_SLOTS : RECV_SLOTS;
    if (!SEND_SLOTS.has(slot) && !RECV_SLOTS.has(slot)) return err(404, "not_found");
    if (request.method === "PUT") {
      if (!own.has(slot)) return err(403, "slot");
      const data = await readCapped(request, MSG_MAX);
      if (data === null) return err(413, "too_big");
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
      const data = await readCapped(request, num(this.env, "CHUNK_MAX", 1048576 + 64));
      if (data === null) return err(413, "too_big");
      if (index < m.nextPut) return empty();            // a repeat after a network blip
      if (index !== m.nextPut) return err(409, "order");
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
