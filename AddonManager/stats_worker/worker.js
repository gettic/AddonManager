// Addon Manager usage counter (Cloudflare Worker + D1). Public on purpose: this is
// everything the server does with a ping.
//
// POST /ping  {"v": 1, "first_this_week": true,
//              "addons": [{"a": "GuildHub", "ver": "0.1.2", "s": "latest"}, ...]}
//   -> adds 1 to today's count for each entry (and to this week's count when
//      first_this_week is true). Replies 204 with no body. Nothing else is readable.
//
// Stored: only totals per day/week, addon, version and state. Never stored or logged:
// the IP address, request headers, request.cf (location) or any ID. No console.log.
//
// Settings (Worker -> Settings -> Variables and Secrets, plain text):
//   ADDONS   = "GuildHub,TaxiTime,NotePad,AddonManager"  (names that may be counted)
//   PING_CAP = "200"  (pings counted per day at most; spam can't go past it)
// Binding: D1 database as DB (tables in schema.sql).

const MAX_BODY = 2048;
const MAX_ENTRIES = 20;
const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;
const VERSION_RE = /^\d+(\.\d+){0,3}(-[A-Za-z0-9.]{1,20})?$/;
const STATES = new Set(["latest", "outdated", "beta"]);

function isoWeek(date) {
  // ISO 8601: the week belongs to the year of its Thursday.
  const t = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const weekday = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - weekday);
  const year = t.getUTCFullYear();
  const week = Math.ceil(((t - Date.UTC(year, 0, 1)) / 86400000 + 1) / 7);
  return `${year}-W${String(week).padStart(2, "0")}`;
}

function parsePing(text, allowed) {
  // The checked entries, or null if anything is off (then nothing is counted).
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data) || data.v !== 1) return null;
  const keys = Object.keys(data).sort().join(",");
  if (keys !== "addons,first_this_week,v" || typeof data.first_this_week !== "boolean") return null;
  const list = data.addons;
  if (!Array.isArray(list) || list.length < 1 || list.length > MAX_ENTRIES) return null;
  const seen = new Set();
  const entries = [];
  for (const e of list) {
    if (!e || typeof e !== "object" || Array.isArray(e)) return null;
    if (Object.keys(e).sort().join(",") !== "a,s,ver") return null;
    const { a, ver, s } = e;
    if (typeof a !== "string" || typeof ver !== "string" || typeof s !== "string") return null;
    if (!NAME_RE.test(a) || seen.has(a)) return null;
    if (!VERSION_RE.test(ver) || !STATES.has(s)) return null;
    seen.add(a);
    if (allowed.has(a)) entries.push({ a, ver, s }); // not (yet) in ADDONS: skipped, not an error
  }
  return { firstThisWeek: data.first_this_week, entries };
}

const ADD_DAILY =
  "INSERT INTO daily (day, addon, version, state, n) VALUES (?1, ?2, ?3, ?4, 1) " +
  "ON CONFLICT (day, addon, version, state) DO UPDATE SET n = n + 1";
const ADD_WEEKLY =
  "INSERT INTO weekly (week, addon, version, state, n) VALUES (?1, ?2, ?3, ?4, 1) " +
  "ON CONFLICT (week, addon, version, state) DO UPDATE SET n = n + 1";

function reply(status) {
  return new Response(null, { status });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/ping") return reply(404);
    if (request.method !== "POST") return reply(405);
    if (Number(request.headers.get("content-length") || "0") > MAX_BODY) return reply(413);
    const text = await request.text();
    if (text.length > MAX_BODY) return reply(413);

    const allowed = new Set(String(env.ADDONS || "").split(",").map((s) => s.trim()).filter(Boolean));
    const ping = parsePing(text, allowed);
    if (!ping) return reply(400);
    if (ping.entries.length === 0) return reply(204);

    const now = new Date();
    const day = now.toISOString().slice(0, 10);
    const week = isoWeek(now);
    const cap = Number.parseInt(env.PING_CAP || "200", 10) || 200;
    try {
      const total = await env.DB.prepare(
        "SELECT n FROM daily WHERE day = ?1 AND addon = '_total' AND version = '' AND state = ''"
      ).bind(day).first("n");
      if ((total || 0) >= cap) return reply(204); // over the daily ceiling: not counted
      const statements = [env.DB.prepare(ADD_DAILY).bind(day, "_total", "", "")];
      for (const e of ping.entries) {
        statements.push(env.DB.prepare(ADD_DAILY).bind(day, e.a, e.ver, e.s));
        if (ping.firstThisWeek) statements.push(env.DB.prepare(ADD_WEEKLY).bind(week, e.a, e.ver, e.s));
      }
      await env.DB.batch(statements);
      return reply(204);
    } catch {
      return reply(503); // e.g. the free daily limit is used up; the app just tries tomorrow
    }
  },
};
