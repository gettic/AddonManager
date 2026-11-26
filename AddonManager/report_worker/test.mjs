// Tests for worker.js with a fake D1 and a fake GitHub (nothing goes online).
// Run from this folder: node test.mjs   (exit code 0 = all passed)
import assert from "node:assert/strict";
import worker, { adler32, strictEscape, parseLine, buildLine, cleanReport, issueFor, take, retry } from "./worker.js";

// Lines built by the addon's own code (same as tests\test_reports.py).
const LINE_A = "AOGR2 rid=k3x9qa;v=0.2.0;c=16001 1.60.1.70959 forever enUS;lang=enUS;ab=coords;" +
  "pk=demo-elwynn;sid=e5;lv=7;map=1429;x=41.2;y=65.9;" +
  "note=Wrong spot%3B a%3Db %7C 100%25%0A%C3%A6%C3%B8%C3%A5;ck=2c7d356c";
const LINE_B = "AOGR2 rid=zz9abc;v=0.2.0;ab=danger;pk=demo-elwynn;sid=e7;noloc=1;" +
  "note=Hi from Bob-Firemaw, you suck;ck=79e6220a";

function line(rid, note = "x", ab = "other") {
  const body = `AOGR2 rid=${rid};v=0.2.0;ab=${ab};pk=demo-elwynn;sid=e1;note=${note}`;
  return `${body};ck=${adler32(body)}`;
}

// ---------------------------------------------------------------- fake D1 (only what worker.js uses)
class FakeDB {
  constructor() { this.rows = new Map(); this.fail = false; }
  prepare(sql) {
    const db = this;
    let args = [];
    const st = {
      bind(...a) { args = a; return st; },
      async first(col) {
        if (db.fail) throw new Error("d1 down");
        if (sql.startsWith("SELECT 1 FROM reports WHERE rid")) return db.rows.has(args[0]) ? { 1: 1 } : null;
        if (sql.startsWith("SELECT COUNT(*)")) {
          const n = [...db.rows.values()].filter((r) => r.day === args[0]).length;
          return col ? n : { n };
        }
        throw new Error("unexpected first: " + sql);
      },
      async run() {
        if (db.fail) throw new Error("d1 down");
        if (sql.startsWith("INSERT OR IGNORE")) {
          if (db.rows.has(args[0])) return { meta: { changes: 0 } };
          db.rows.set(args[0], { rid: args[0], day: args[1], via: args[2], state: "pending", line: args[3], tries: 0, issue: null });
          return { meta: { changes: 1 } };
        }
        const r = db.rows.get(args[0]);
        if (sql.includes("state = 'filed'")) Object.assign(r, { state: "filed", issue: args[1], line: null });
        else if (sql.includes("state = 'dropped', line = NULL")) Object.assign(r, { state: "dropped", line: null });
        else if (sql.includes("tries = tries + 1, state = CASE")) {
          r.tries += 1;
          if (r.tries >= 20) Object.assign(r, { state: "dropped", line: null });
        } else if (sql.includes("tries = tries + 1")) r.tries += 1;
        else throw new Error("unexpected run: " + sql);
        return { meta: { changes: 1 } };
      },
      async all() {
        const results = [...db.rows.values()].filter((r) => r.state === "pending" && r.line).slice(0, 10)
          .map((r) => ({ rid: r.rid, via: r.via, line: r.line }));
        return { results };
      },
    };
    return st;
  }
}

// ---------------------------------------------------------------- fake fetch (GitHub, Turnstile)
const gh = { status: 201, issues: [], next: 100, turnstile: true, calls: [] };
globalThis.fetch = async (url, init) => {
  gh.calls.push(String(url));
  if (String(url).startsWith("https://api.github.com/")) {
    assert.match(init.headers.Authorization, /^Bearer test-token$/);
    if (gh.status !== 201) return new Response("{}", { status: gh.status });
    const body = JSON.parse(init.body);
    gh.issues.push({ url: String(url), ...body });
    return new Response(JSON.stringify({ number: gh.next++ }), { status: 201 });
  }
  if (String(url).startsWith("https://challenges.cloudflare.com/")) {
    const p = new URLSearchParams(init.body);
    assert.equal(p.get("remoteip"), null, "the IP is never sent to Turnstile");
    return new Response(JSON.stringify({ success: gh.turnstile && p.get("response") === "good" }));
  }
  throw new Error("unexpected fetch " + url);
};

function env(extra = {}) {
  return { DB: new FakeDB(), GITHUB_TOKEN: "test-token", REPORT_CAP: "3", REPO: "gettic/All-in-One-Guide", ...extra };
}

function appRequest(body, type = "application/json") {
  return new Request("https://aog-report.example.workers.dev/report", {
    method: "POST", headers: { "content-type": type }, body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function formRequest(fields) {
  return appRequest(new URLSearchParams(fields).toString(), "application/x-www-form-urlencoded");
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("lines from the addon pass; a rebuilt line is the same line", () => {
  for (const l of [LINE_A, LINE_B]) {
    const p = parseLine(l);
    assert.ok(p, l);
    assert.equal(buildLine(p.fields, p.order), l);
  }
  assert.equal(parseLine(LINE_A).fields.note, "Wrong spot; a=b | 100%\næøå");
});

test("bad lines are refused", () => {
  assert.equal(parseLine(LINE_A.replace("ab=coords", "ab=corrds")), null);
  assert.equal(parseLine(LINE_A.slice(0, -3)), null);
  assert.equal(parseLine("hello"), null);
  assert.equal(parseLine(line("abc", "x".repeat(1600))), null);
  assert.equal(parseLine(line("abc", "y".repeat(201))), null, "note over 200");
  const body = "AOGR2 rid=abc;zz=1";
  assert.equal(parseLine(`${body};ck=${adler32(body)}`), null, "unknown field");
  const dup = "AOGR2 rid=abc;ab=x;ab=y";
  assert.equal(parseLine(`${dup};ck=${adler32(dup)}`), null, "field twice");
});

test("Name-Realm is removed (new checksum), rude words flagged", () => {
  const c = cleanReport(parseLine(LINE_B));
  assert.ok(c.removed);
  assert.equal(c.fields.note, "Hi from [name removed], you suck");
  assert.ok(parseLine(c.line), "the rebuilt line checks out");
  assert.ok(!c.line.includes("Firemaw"));
  assert.ok(!c.rude);
  assert.ok(cleanReport(parseLine(line("r1", "what the fuck"))).rude);
  assert.ok(cleanReport(parseLine(line("r2", "sikke noget lort"))).rude);
  assert.ok(!cleanReport(parseLine(line("r3", "Wrong spot near the shitake farm"))).rude, "whole words only");
  const plain = cleanReport(parseLine(LINE_A));
  assert.ok(!plain.removed && !plain.rude);
  assert.equal(plain.fields.x, "41", "x rounded to whole percent");
  assert.equal(plain.fields.y, "66", "y rounded to whole percent");
  assert.ok(plain.line.includes(";x=41;y=66;") && !plain.line.includes("41.2") && !plain.line.includes("65.9"));
  assert.ok(parseLine(plain.line), "the rounded line checks out");
  assert.equal(cleanReport(parseLine(LINE_B)).fields.x, undefined, "no location stays none");
});

test("the issue shows whole-percent x / y and no time of day", () => {
  const i = issueFor(cleanReport(parseLine(LINE_A)), "app", "gettic/All-in-One-Guide");
  assert.ok(i.body.includes("x=41;y=66") && !i.body.includes("41.2") && !i.body.includes("65.9"));
  assert.ok(!/\d{1,2}:\d{2}/.test(i.body) && !/\d{4}-\d{2}-\d{2}/.test(i.body), "no clock time or timestamp in the body");
});

test("the issue: headings like the issue form, code blocks, labels", () => {
  const c = cleanReport(parseLine(line("r4", strictEscape("```@someone [x](http://a) ![i](http://b)"))));
  const i = issueFor(c, "app", "gettic/All-in-One-Guide");
  assert.equal(i.title, "[Report] demo-elwynn/e1: Something else");
  assert.deepEqual(i.labels, ["guide-report", "needs-triage", "type:other", "via-app"]);
  for (const h of ["### Report line", "### What is wrong?", "### Anything else?", "### Licence"]) assert.ok(i.body.includes(h), h);
  assert.ok(i.body.includes("'''@someone"), "backticks swapped, the mention sits in a code block");
  assert.equal((i.body.match(/````/g) || []).length, 4, "two fenced blocks, nothing closes them early");
  const w = issueFor(cleanReport(parseLine(LINE_B)), "web", "gettic/All-in-One-Guide");
  assert.deepEqual(w.labels, ["guide-report", "needs-triage", "type:danger", "via-web", "needs-review"]);
});

test("app: filed once, the second copy is 409", async () => {
  const e = env();
  gh.issues = [];
  let res = await worker.fetch(appRequest({ v: 1, a: "AllinOneGuide", r: LINE_A }), e);
  assert.equal(res.status, 201);
  assert.equal(gh.issues.length, 1);
  assert.equal(gh.issues[0].url, "https://api.github.com/repos/gettic/All-in-One-Guide/issues");
  assert.equal(e.DB.rows.get("k3x9qa").state, "filed");
  assert.equal(e.DB.rows.get("k3x9qa").line, null, "the line isn't kept once filed");
  res = await worker.fetch(appRequest({ v: 1, a: "AllinOneGuide", r: LINE_A }), e);
  assert.equal(res.status, 409);
  assert.equal(gh.issues.length, 1);
});

test("app: odd payloads refused", async () => {
  const e = env();
  for (const body of [{ v: 2, a: "AllinOneGuide", r: LINE_A }, { v: 1, a: "Other", r: LINE_A },
    { v: 1, a: "AllinOneGuide", r: LINE_A, x: 1 }, { v: 1, a: "AllinOneGuide", r: "hello" }, "not json", [1]]) {
    const res = await worker.fetch(appRequest(body), e);
    assert.equal(res.status, 400, JSON.stringify(body));
  }
  const big = await worker.fetch(appRequest({ v: 1, a: "AllinOneGuide", r: "x".repeat(5000) }), e);
  assert.equal(big.status, 413);
  assert.equal((await worker.fetch(appRequest("a=b", "text/plain"), e)).status, 415);
  assert.equal((await worker.fetch(new Request("https://x.example/other"), e)).status, 404);
});

test("daily cap", async () => {
  const e = env({ REPORT_CAP: "2" });
  assert.equal((await take(e, line("c1"), "app")).status, 201);
  assert.equal((await take(e, line("c2"), "app")).status, 201);
  assert.equal((await take(e, line("c3"), "app")).status, 429);
});

test("GitHub down: saved (202), the cron files it later", async () => {
  const e = env();
  gh.status = 502;
  assert.equal((await take(e, line("g1"), "app")).status, 202);
  assert.equal(e.DB.rows.get("g1").state, "pending");
  await retry(e);
  assert.equal(e.DB.rows.get("g1").state, "pending");
  gh.status = 201;
  gh.issues = [];
  await worker.scheduled({}, e);
  assert.equal(e.DB.rows.get("g1").state, "filed");
  assert.equal(gh.issues.length, 1);
  // no token at all: kept pending too, never lost
  const e2 = env({ GITHUB_TOKEN: "" });
  assert.equal((await take(e2, line("g2"), "app")).status, 202);
});

test("D1 down: 503", async () => {
  const e = env();
  e.DB.fail = true;
  assert.equal((await take(e, line("d1"), "app")).status, 503);
});

test("web: form page, Turnstile, licence tick", async () => {
  const e = env({ TURNSTILE_SECRET: "s", TURNSTILE_SITE_KEY: "site-key" });
  const get = await worker.fetch(new Request("https://x.example/"), e);
  assert.equal(get.status, 200);
  const htmlText = await get.text();
  assert.ok(htmlText.includes("Reports are public on GitHub. Don't write names in the note."));
  assert.ok(htmlText.includes('data-sitekey="site-key"'));
  assert.match(get.headers.get("content-security-policy"), /default-src 'none'/);
  let res = await worker.fetch(formRequest({ r: LINE_A, "cf-turnstile-response": "good" }), e);
  assert.equal(res.status, 400, "no licence tick");
  res = await worker.fetch(formRequest({ r: LINE_A, agree: "1", "cf-turnstile-response": "bad" }), e);
  assert.equal(res.status, 403);
  gh.issues = [];
  res = await worker.fetch(formRequest({ r: "  " + LINE_A + "\n", agree: "1", "cf-turnstile-response": "good" }), e);
  assert.equal(res.status, 200);
  assert.ok((await res.text()).includes("issue #"));
  assert.deepEqual(gh.issues[0].labels.slice(-1), ["via-web"]);
  res = await worker.fetch(formRequest({ r: "hello <script>", agree: "1", "cf-turnstile-response": "good" }), e);
  assert.equal(res.status, 400);
  assert.ok(!(await res.text()).includes("<script>"), "nothing from the form is echoed");
  // no Turnstile secret set up: the web page refuses (the app path still works)
  const e2 = env();
  res = await worker.fetch(formRequest({ r: line("w2"), agree: "1" }), e2);
  assert.equal(res.status, 403);
  assert.equal((await worker.fetch(appRequest({ v: 1, a: "AllinOneGuide", r: line("w3") }), e2)).status, 201);
});

test("rate limit binding", async () => {
  const e = env({ REPORT_LIMIT: { limit: async () => ({ success: false }) } });
  assert.equal((await worker.fetch(appRequest({ v: 1, a: "AllinOneGuide", r: line("rl") }), e)).status, 429);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log("ok   " + name);
  } catch (err) {
    failed++;
    console.log("FAIL " + name + "\n     " + (err && err.stack || err));
  }
}
console.log(failed ? `${failed} failed` : `all ${tests.length} passed`);
process.exit(failed ? 1 : 0);
