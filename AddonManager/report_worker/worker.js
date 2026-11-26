// Guide reports for All-in-One Guide (Cloudflare Worker + D1). Public on purpose: this is
// everything the server does with a report.
//
// A player who found a wrong guide step presses Send on the addon's Report page (the Addon
// Manager sends it later) or pastes the report line into the web page below. This Worker
// checks the line and files it as a GitHub issue, so players need no GitHub account.
//
//   POST /report  {"v": 1, "a": "AllinOneGuide", "r": "AOGR2 rid=...;ck=..."}   (Addon Manager)
//     201 filed, 202 saved (GitHub was busy: posted later by the cron), 400 not a report,
//     409 already received, 413 too big, 429 today's cap reached or too fast, 503 try later.
//   GET  /  or /report   the web page (a form; Cloudflare Turnstile against bots)
//   POST /report  (form: r, agree, cf-turnstile-response)                         (web page)
//
// Checks: size, the "AOGR2 " start, the allowed characters, the field names, the Adler-32
// checksum (the same as the addon's), the note's length, one issue per report id (D1), a
// daily cap (REPORT_CAP). Names: a "Name-Realm" in the note is removed (the line is rebuilt
// with a new checksum) and the issue gets the needs-review label; so does a note with a
// rude word. The issue body is written here from the checked line, inside code blocks, so
// nothing in it can mention anyone, link or load an image.
//
// Stored (D1): per report id the day, the way it came (app / web), the state and the issue
// number; the line itself only while it waits for GitHub. Never stored or logged: the IP
// address, headers, request.cf (location). The IP is only used, never kept, as the key of the
// optional rate limit. No console.log.
//
// Settings (wrangler.toml [vars]): REPO, REPORT_CAP, TURNSTILE_SITE_KEY (public).
// Secrets (npx wrangler secret put ...; never in a file): GITHUB_TOKEN, TURNSTILE_SECRET.
// Bindings: D1 database as DB (schema.sql), optional rate limit REPORT_LIMIT.

const MAX_BODY = 4096;
const MAX_LEN = 1500;
const NOTE_MAX = 200;
const ADDONS = new Set(["AllinOneGuide"]);
const LINE_RE = /^AOGR2 rid=([a-z0-9]{1,16});[A-Za-z0-9 .,:_\/+()'!?\-%=;]*;ck=([0-9a-f]{8})$/;
// The fields of the line (DB\Serializer.lua kind R2), in order.
const FIELDS = ["rid", "v", "c", "st", "lang", "ab", "pk", "pv", "fv", "rt", "sid", "sh", "inc", "qs",
  "cnd", "lv", "xp", "fa", "ra", "cl", "hc", "map", "x", "y", "r", "d", "noloc", "npc", "ki", "cf", "l3",
  "eng", "note"];
const FIELD_SET = new Set(FIELDS);
const TYPES = {
  coords: "Wrong place on the map", order: "Wrong order", missing: "Quest isn't there",
  notick: "Step doesn't tick", text: "Text is wrong", danger: "Dangerous spot", other: "Something else",
};
// A character name with its realm, the way the game writes it ("Name-Realm"), maybe with
// accents; realms may have no space ("ArgentDawn").
const NAME_REALM_RE = /\b\p{Lu}[\p{Ll}]{1,11}-\p{Lu}[\p{L}']{2,24}\b/gu;
// A basic list (English and Danish) for the needs-review label; nothing is removed for these.
const RUDE = ["fuck", "fucking", "shit", "cunt", "bitch", "asshole", "bastard", "dick", "pussy",
  "whore", "slut", "nigger", "nigga", "faggot", "fag", "retard", "kys", "kill yourself",
  "lort", "fisse", "luder", "kælling", "pik", "røvhul", "spasser", "bøsserøv"];

// ---------------------------------------------------------------- the line
export function adler32(text) {
  let a = 1, b = 0;
  for (const byte of new TextEncoder().encode(text)) {
    a = (a + byte) % 65521;
    b = (b + a) % 65521;
  }
  return b.toString(16).padStart(4, "0") + a.toString(16).padStart(4, "0");
}

// The addon's strict escaping (DB\Serializer.lua StrictEscape): every byte outside a small
// plain set becomes %XX.
export function strictEscape(text) {
  let out = "";
  for (const byte of new TextEncoder().encode(text)) {
    const c = String.fromCharCode(byte);
    out += /[A-Za-z0-9 .,:_\/+()'!?-]/.test(c) ? c : "%" + byte.toString(16).toUpperCase().padStart(2, "0");
  }
  return out;
}

function unescapeValue(v) {
  const bytes = [];
  for (let i = 0; i < v.length; i++) {
    if (v[i] === "%" && /^[0-9A-Fa-f]{2}$/.test(v.slice(i + 1, i + 3))) {
      bytes.push(parseInt(v.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      bytes.push(v.charCodeAt(i));
    }
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

// The checked fields ({rid, ab, note, ...} as plain text) and the order they came in, or null.
export function parseLine(line) {
  if (typeof line !== "string" || line.length > MAX_LEN) return null;
  const m = LINE_RE.exec(line);
  if (!m) return null;
  const body = line.slice(0, -";ck=".length - 8);
  if (adler32(body) !== m[2]) return null;
  const fields = {};
  const order = [];
  for (const part of body.slice("AOGR2 ".length).split(";")) {
    const eq = part.indexOf("=");
    if (eq < 1) return null;
    const key = part.slice(0, eq);
    if (!FIELD_SET.has(key) || key in fields) return null;
    fields[key] = unescapeValue(part.slice(eq + 1));
    order.push(key);
  }
  if (fields.rid !== m[1]) return null;
  if (fields.note !== undefined && [...fields.note].length > NOTE_MAX) return null;
  return { fields, order };
}

// The line again from fields (after a name was removed), with a new checksum.
export function buildLine(fields, order) {
  const body = "AOGR2 " + order.filter((k) => fields[k] !== undefined)
    .map((k) => k + "=" + strictEscape(String(fields[k]))).join(";");
  return body + ";ck=" + adler32(body);
}

// Removes Name-Realm from the note; rounds x / y to whole percent; flags rude words.
// Issues show no time of day either: the only date kept is the day (D1) and none goes in the body.
// -> {line, fields, removed, rude}
export function cleanReport(parsed) {
  const fields = { ...parsed.fields };
  let removed = false;
  let rude = false;
  if (fields.note) {
    const cleaned = fields.note.replace(NAME_REALM_RE, () => {
      removed = true;
      return "[name removed]";
    });
    if (removed) fields.note = cleaned;
    const low = " " + fields.note.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ") + " ";
    rude = RUDE.some((w) => low.includes(" " + w + " "));
  }
  // Public issues get a vaguer place: the map x / y rounded to whole percent.
  for (const k of ["x", "y"]) {
    if (fields[k] !== undefined && /^\d{1,3}(\.\d+)?$/.test(fields[k])) fields[k] = String(Math.round(Number(fields[k])));
  }
  return { line: buildLine(fields, parsed.order), fields, removed, rude };
}

// ---------------------------------------------------------------- the issue
function plain(text, max) {
  // one line, only harmless characters (titles)
  return String(text || "").replace(/[^A-Za-z0-9 ._\/:-]/g, "").slice(0, max);
}

function fenced(text) {
  // inside a code block nothing renders; backticks are swapped so the block can't be closed
  return "````text\n" + String(text).replace(/`/g, "'") + "\n````";
}

export function issueFor(clean, via, repo) {
  const f = clean.fields;
  const what = TYPES[f.ab] || TYPES.other;
  const title = `[Report] ${plain(f.pk || "?", 40)}/${plain(f.sid || "?", 40)}: ${what}`;
  const labels = ["guide-report", "needs-triage", "type:" + (TYPES[f.ab] ? f.ab : "other"),
    via === "web" ? "via-web" : "via-app"];
  if (clean.removed || clean.rude) labels.push("needs-review");
  const parts = [
    "### Report line", "", fenced(clean.line), "",
    "### What is wrong?", "", what, "",
    "### Anything else?", "",
    f.note ? fenced(f.note) : "_No response_", "",
    "### Licence", "",
    "- [X] I agree that what I report here may be used to fix the guides under the addon's licence " +
      "(GNU GPL, version 3 or later).", "",
    "---",
    via === "web"
      ? "_Sent through the report web page (no GitHub account needed). There is no reporter to ask; decide from the line._"
      : "_Sent by the Addon Manager (no GitHub account needed). There is no reporter to ask; decide from the line._",
  ];
  if (clean.removed) parts.push("_A character name was removed from the note before posting._");
  if (clean.rude) parts.push("_The note may hold a rude word: check it before replying._");
  return { title, body: parts.join("\n"), labels, repo };
}

async function fileIssue(env, issue) {
  // -> issue number, or null (GitHub busy / down: try later)
  if (!env.GITHUB_TOKEN) return null;
  const res = await fetch(`https://api.github.com/repos/${issue.repo}/issues`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "aog-report-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ title: issue.title, body: issue.body, labels: issue.labels }),
  });
  if (res.status !== 201) return null;
  const data = await res.json();
  return typeof data.number === "number" ? data.number : null;
}

// ---------------------------------------------------------------- storing and filing
function today() {
  return new Date().toISOString().slice(0, 10);
}

// The whole path for one checked line: dedupe, cap, file (or keep for the cron).
// -> {status, issue?}
export async function take(env, line, via) {
  const parsed = parseLine(line);
  if (!parsed) return { status: 400 };
  const clean = cleanReport(parsed);
  const rid = parsed.fields.rid;
  const day = today();
  const cap = Number.parseInt(env.REPORT_CAP || "30", 10) || 30;
  const repo = env.REPO || "gettic/All-in-One-Guide";
  try {
    if (await env.DB.prepare("SELECT 1 FROM reports WHERE rid = ?1").bind(rid).first()) return { status: 409 };
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM reports WHERE day = ?1").bind(day).first("n");
    if ((n || 0) >= cap) return { status: 429 };
    // claim the id first (a second copy at the same moment gets 409)
    const ins = await env.DB.prepare(
      "INSERT OR IGNORE INTO reports (rid, day, via, state, line, tries) VALUES (?1, ?2, ?3, 'pending', ?4, 0)"
    ).bind(rid, day, via, clean.line).run();
    if (ins.meta && ins.meta.changes === 0) return { status: 409 };
  } catch {
    return { status: 503 };
  }
  let number = null;
  try {
    number = await fileIssue(env, issueFor(clean, via, repo));
  } catch {
    number = null;
  }
  try {
    if (number) {
      await env.DB.prepare("UPDATE reports SET state = 'filed', issue = ?2, line = NULL WHERE rid = ?1")
        .bind(rid, number).run();
      return { status: 201, issue: number, repo };
    }
    await env.DB.prepare("UPDATE reports SET tries = tries + 1 WHERE rid = ?1").bind(rid).run();
  } catch {
    // the row stays pending; the cron files it
  }
  return { status: 202 };
}

// Cron: files reports GitHub didn't take at once (at most 10 a run; gives up after 20 tries).
export async function retry(env) {
  const repo = env.REPO || "gettic/All-in-One-Guide";
  const rows = await env.DB.prepare(
    "SELECT rid, via, line FROM reports WHERE state = 'pending' AND line IS NOT NULL ORDER BY day LIMIT 10"
  ).all();
  for (const row of rows.results || []) {
    const parsed = parseLine(row.line);
    if (!parsed) {
      await env.DB.prepare("UPDATE reports SET state = 'dropped', line = NULL WHERE rid = ?1").bind(row.rid).run();
      continue;
    }
    let number = null;
    try {
      number = await fileIssue(env, issueFor(cleanReport(parsed), row.via, repo));
    } catch {
      number = null;
    }
    if (number) {
      await env.DB.prepare("UPDATE reports SET state = 'filed', issue = ?2, line = NULL WHERE rid = ?1")
        .bind(row.rid, number).run();
    } else {
      await env.DB.prepare(
        "UPDATE reports SET tries = tries + 1, state = CASE WHEN tries + 1 >= 20 THEN 'dropped' ELSE state END, " +
        "line = CASE WHEN tries + 1 >= 20 THEN NULL ELSE line END WHERE rid = ?1"
      ).bind(row.rid).run();
      break; // GitHub is still down: the rest waits for the next run
    }
  }
}

// ---------------------------------------------------------------- the web page
function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const STYLE = `body{font:16px/1.5 system-ui,sans-serif;background:#16181d;color:#e6e6e6;margin:0}
main{max-width:640px;margin:0 auto;padding:24px}h1{font-size:1.5em;margin:0 0 12px}
ol{padding-left:1.3em}textarea{width:100%;box-sizing:border-box;min-height:9em;font:14px monospace;
background:#0e1014;color:#e6e6e6;border:1px solid #444;border-radius:6px;padding:8px}
.warn{background:#3a1d1d;border-left:4px solid #d9534f;padding:8px 12px;border-radius:4px}
.ok{background:#1d3a24;border-left:4px solid #4caf50;padding:8px 12px;border-radius:4px}
button{font-size:1.1em;padding:10px 28px;border:0;border-radius:6px;background:#f0b232;color:#111;cursor:pointer}
label{display:block;margin:12px 0}small{color:#9aa0a6}a{color:#7ab7ff}`;

function page(title, inner, siteKey) {
  const script = siteKey
    ? '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
    : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>${esc(title)}</title><style>${STYLE}</style>${script}</head><body><main>${inner}</main></body></html>`;
}

export function formPage(env, message) {
  const siteKey = env.TURNSTILE_SITE_KEY || "";
  const widget = siteKey ? `<div class="cf-turnstile" data-sitekey="${esc(siteKey)}" data-theme="dark"></div>` : "";
  const note = message ? `<p class="warn">${esc(message)}</p>` : "";
  return page("Report a mistake: All-in-One Guide", `
<h1>Report a mistake in All-in-One Guide</h1>
<p>No GitHub account needed. Have the Addon Manager? Then just press <b>Send</b> on the report page in the game instead.</p>
${note}
<ol>
<li>In the game, type <b>/aog report</b>, pick what is wrong and which step.</li>
<li>Press <b>Copy the report</b>, then Ctrl+C.</li>
<li>Paste it below with Ctrl+V and press <b>Send</b>.</li>
</ol>
<p class="warn">Reports are public on GitHub. Don't write names in the note.</p>
<form method="post" action="/report">
<label>Report line (starts with AOGR2)<textarea name="r" required maxlength="1600" spellcheck="false"></textarea></label>
<label><input type="checkbox" name="agree" value="1" required> I agree that what I report may be used to fix the guides under the addon's licence (GNU GPL, version 3 or later).</label>
${widget}
<p><button type="submit">Send</button></p>
</form>
<p><small>We keep no IP address and no logs. The report holds no names: the guide, the step, the addon version, your level, class and race, and where you stood (unless you ticked "Leave out my location" in the game).</small></p>`, siteKey);
}

function donePage(env, result) {
  if (result.status === 201) {
    const url = `https://github.com/${result.repo}/issues/${result.issue}`;
    return page("Thanks", `<h1>Thanks!</h1><p class="ok">Your report is in: <a href="${esc(url)}">issue #${esc(result.issue)}</a>.</p>`);
  }
  if (result.status === 202) return page("Thanks", `<h1>Thanks!</h1><p class="ok">Your report is saved and goes up on GitHub soon.</p>`);
  if (result.status === 409) return page("Already in", `<h1>Already in</h1><p class="ok">We already have this report. Thanks!</p>`);
  const why = {
    400: "That isn't a whole report line. Copy it again in the game (it starts with AOGR2) and paste all of it.",
    403: "The check against bots didn't pass. Please try again.",
    429: "We got a lot of reports today. Please try again tomorrow.",
  }[result.status] || "Something went wrong on our side. Please try again later.";
  return formPage(env, why);
}

function html(body, status) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; " +
        "script-src https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; " +
        "connect-src https://challenges.cloudflare.com; form-action 'self'; base-uri 'none'",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function reply(status) {
  return new Response(null, { status });
}

async function turnstileOk(env, token) {
  // no secret set up: the web page refuses (tests set ALLOW_NO_TURNSTILE = "1")
  if (!env.TURNSTILE_SECRET) return env.ALLOW_NO_TURNSTILE === "1";
  if (!token || token.length > 2048) return false;
  try {
    const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token }),
    });
    const data = await res.json();
    return data.success === true;
  } catch {
    return false;
  }
}

async function limited(env, request) {
  // optional per-IP limit; the IP is only the key, never stored
  if (!env.REPORT_LIMIT) return false;
  const ip = request.headers.get("cf-connecting-ip") || "none";
  try {
    const { success } = await env.REPORT_LIMIT.limit({ key: ip });
    return !success;
  } catch {
    return false;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/" && url.pathname !== "/report") return reply(404);
    if (request.method === "GET") return html(formPage(env), 200);
    if (request.method !== "POST" || url.pathname !== "/report") return reply(405);
    if (Number(request.headers.get("content-length") || "0") > MAX_BODY) return reply(413);
    const text = await request.text();
    if (text.length > MAX_BODY) return reply(413);
    const type = (request.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();

    if (type === "application/json") {
      // the Addon Manager
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        return reply(400);
      }
      if (!data || typeof data !== "object" || Array.isArray(data) || data.v !== 1) return reply(400);
      if (Object.keys(data).sort().join(",") !== "a,r,v") return reply(400);
      if (typeof data.a !== "string" || !ADDONS.has(data.a) || typeof data.r !== "string") return reply(400);
      if (await limited(env, request)) return reply(429);
      const result = await take(env, data.r, "app");
      return reply(result.status);
    }

    if (type === "application/x-www-form-urlencoded") {
      // the web page
      const form = new URLSearchParams(text);
      if (form.get("agree") !== "1") return html(formPage(env, "Please tick the box to agree, then send it again."), 400);
      if (await limited(env, request)) return html(donePage(env, { status: 429 }), 429);
      if (!(await turnstileOk(env, form.get("cf-turnstile-response")))) return html(donePage(env, { status: 403 }), 403);
      const line = String(form.get("r") || "").trim();
      const result = await take(env, line, "web");
      return html(donePage(env, result), result.status === 201 || result.status === 202 || result.status === 409 ? 200 : result.status);
    }
    return reply(415);
  },

  async scheduled(_event, env) {
    try {
      await retry(env);
    } catch {
      // next run
    }
  },
};
