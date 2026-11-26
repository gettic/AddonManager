// Tests for worker.js with a fake D1 and a fake GitHub (nothing goes online).
// Run from this folder: node test.mjs   (exit code 0 = all passed)
import assert from "node:assert/strict";
import worker, {
  adler32, strictEscape, parseLine, buildLine, cleanReport, issueFor, take, retry,
  checkPage, D1_KEYS, cleanUp, CLEAN_SQL,
} from "./worker.js";

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

// ---------------------------------------------------------------- game data (POST /data)
// Pages built by All-in-One Guide's own code (same as tests\test_gather.py).
const PAGE_A = "AOGD1 rid=k3x9q2ab;h=1;v=0.3.0;b=1.60.1.70959;fac=A;cls=WARRIOR;race=Human;" +
  "realm=Forever Test;loc=enUS;day=2026-11-26;cnt=3 AOGD1 k=qxp;q=783;xp=85;pl=2;ql=1 " +
  "AOGD1 k=camp;npc=80;m=1429;x=27;y=46;n=11 AOGD1 k=drop;npc=38;it=752;loot=14;got=3 AOGD1 ck=bfc547a4";
const PAGE_B = "AOGD1 rid=zz9abc12;h=1;v=0.3.0;b=1.60.1.70959;fac=A;cls=WARRIOR;race=Human;" +
  "realm=Forever Test;loc=enUS;day=2026-11-26;cnt=2 AOGD1 k=err;msg=Gather.lua:12: " +
  "attempt%3B a%3Db %7C 100%25 AOGD1 k=fly;fn=2;tn=4;sec=95 AOGD1 ck=50513f3a";
const SID = "abcdefghij12";

function page(rid, rec = "k=camp;npc=80;n=1") {
  const core = `AOGD1 rid=${rid};h=1;v=0.3.0;cnt=1 AOGD1 ${rec}`;
  return `${core} AOGD1 ck=${adler32(core)}`;
}

class FakeDataDB {
  constructor() { this.rows = new Map(); this.blocked = new Set(); this.fail = false; this.ran = []; }
  prepare(sql) {
    const db = this;
    let args = [];
    const st = {
      bind(...a) { args = a; return st; },
      async first(col) {
        if (db.fail) throw new Error("d1 down");
        if (sql.startsWith("SELECT 1 FROM blocked_sids")) return db.blocked.has(args[0]) ? { 1: 1 } : null;
        if (sql.startsWith("SELECT 1 FROM gather_pages WHERE rid")) return db.rows.has(args[0]) ? { 1: 1 } : null;
        if (sql.includes("WHERE sid = ?1 AND day = ?2")) {
          const n = [...db.rows.values()].filter((r) => r.sid === args[0] && r.day === args[1]).length;
          return col ? n : { n };
        }
        if (sql.includes("FROM gather_pages WHERE day = ?1")) {
          const n = [...db.rows.values()].filter((r) => r.day === args[0]).length;
          return col ? n : { n };
        }
        throw new Error("unexpected first: " + sql);
      },
      async run() {
        if (db.fail) throw new Error("d1 down");
        db.ran.push(sql);
        if (sql.startsWith("INSERT OR IGNORE INTO gather_pages")) {
          if (db.rows.has(args[0])) return { meta: { changes: 0 } };
          db.rows.set(args[0], { rid: args[0], sid: args[1], page: args[2], day: args[3] });
          return { meta: { changes: 1 } };
        }
        if (sql.startsWith("DELETE FROM gather_pages")) return { meta: { changes: 0 } };
        throw new Error("unexpected run: " + sql);
      },
    };
    return st;
  }
}

function dataEnv(extra = {}) {
  return { DB: new FakeDB(), DATA: new FakeDataDB(), DATA_SID_CAP: "120", DATA_CAP: "800", ...extra };
}

function dataRequest(body, type = "application/json") {
  return new Request("https://aog-report.example.workers.dev/data", {
    method: "POST", headers: { "content-type": type }, body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

async function postData(e, body) {
  const res = await worker.fetch(dataRequest(body), e);
  return { status: res.status, body: res.status === 200 ? await res.json() : null };
}

test("data: pages from the addon pass the checks", () => {
  assert.equal(checkPage(PAGE_A), "k3x9q2ab");
  assert.equal(checkPage(PAGE_B), "zz9abc12");
  assert.equal(checkPage(PAGE_A.replace("xp=85", "xp=86")), null, "checksum");
  assert.equal(checkPage(PAGE_A.slice(0, -2)), null, "cut");
  assert.equal(checkPage(page("u1", "k=camp;zz=1")), null, "unknown key");
  assert.equal(checkPage(page("u2", "k=camp;k=drop")), null, "key twice");
  assert.equal(checkPage(page("u3", "k=camp;ck=1")), null, "ck inside");
  assert.equal(checkPage(page("u4", "k=camp;n=" + "1".repeat(3000))), null, "over 3000");
  assert.equal(checkPage(page("u5", "k=camp;noequals")), null, "no value");
  assert.equal(checkPage(LINE_A), null, "a report line isn't a page");
  // the addon's DB\Serializer.lua kinds.D1, in order (printed by LuaJIT from the addon's file)
  const ADDON_D1 = "rid,h,v,b,fac,cls,race,realm,loc,day,cnt,k,q,npc,obj,ins,from,m,x,y,c,n,xp,mo,pl,ql,it,loot," +
    "got,cu,sh,gray,cloth,herb,ore,pk,s,t,fn,tn,sec,lo,hi,cl,fa,gain,sp,sk,lv,dif,tries,ups,sv,rc,sup,min,boss,qs,pz," +
    "pn,dd,gs,gx,rt,gph,e,st,skp,und,nd,bk,spc,ns,hs,fl,wk,hr,mt,sw,ft,dev,pct,rd,kv,ms,rf,uk,on,fe,msg,at,stk,tt,nq," +
    "oi,why,ch,sz,ct,cf,ml,hc,cn,cb,eq,stt,am,qcu,vcu,dcu,mcu,gv,sks,ck";
  assert.equal([...D1_KEYS].join(","), ADDON_D1, "the addon's D1 list (DB\\Serializer.lua)");
});

test("data: stored with sid + day only, each page once", async () => {
  const e = dataEnv();
  let r = await postData(e, { v: 1, sid: SID, pages: [PAGE_A, PAGE_B, PAGE_A] });
  assert.equal(r.status, 200);
  assert.deepEqual(r.body, { took: ["k3x9q2ab", "zz9abc12"], dup: [], bad: [], over: [] });
  const row = e.DATA.rows.get("k3x9q2ab");
  assert.deepEqual(Object.keys(row).sort(), ["day", "page", "rid", "sid"]);
  assert.equal(row.sid, SID);
  assert.match(row.day, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(row.page, PAGE_A);
  r = await postData(e, { v: 1, sid: "zyxwvutsrq98", pages: [PAGE_A, PAGE_A.replace("xp=85", "xp=86")] });
  assert.deepEqual(r.body, { took: [], dup: ["k3x9q2ab"], bad: ["k3x9q2ab"], over: [] });
  assert.equal(e.DATA.rows.get("k3x9q2ab").sid, SID, "a duplicate changes nothing");
});

test("data: odd requests refused", async () => {
  const e = dataEnv();
  for (const body of [{ v: 2, sid: SID, pages: [PAGE_A] }, { v: 1, sid: "short", pages: [PAGE_A] },
    { v: 1, sid: SID.toUpperCase(), pages: [PAGE_A] }, { v: 1, sid: SID, pages: [] },
    { v: 1, sid: SID, pages: PAGE_A }, { v: 1, sid: SID, pages: [PAGE_A], x: 1 },
    { v: 1, pages: [PAGE_A] }, { v: 1, sid: SID, pages: Array(31).fill(PAGE_A) }, "not json", [1]]) {
    assert.equal((await postData(e, body)).status, 400, JSON.stringify(body).slice(0, 60));
  }
  assert.equal((await worker.fetch(dataRequest("a=b", "text/plain"), e)).status, 415);
  assert.equal((await worker.fetch(dataRequest({ v: 1, sid: SID, pages: ["x".repeat(120000)] }), e)).status, 413);
  assert.equal((await worker.fetch(new Request("https://x.example/data"), e)).status, 405, "nothing to read");
  assert.equal(e.DATA.rows.size, 0);
  assert.equal((await worker.fetch(dataRequest({ v: 1, sid: SID, pages: [PAGE_A] }), env())).status, 503, "no DATA binding");
});

test("data: 120 a day per sender code, then over (sent the next day); total cap 429", async () => {
  const e = dataEnv({ DATA_SID_CAP: "3", DATA_CAP: "5" });
  let r = await postData(e, { v: 1, sid: SID, pages: [page("a1"), page("a2")] });
  assert.deepEqual(r.body.took, ["a1", "a2"]);
  r = await postData(e, { v: 1, sid: SID, pages: [page("a3"), page("a4"), page("a5")] });
  assert.deepEqual([r.body.took, r.body.over], [["a3"], ["a4", "a5"]]);
  r = await postData(e, { v: 1, sid: "zyxwvutsrq98", pages: [page("b1"), page("b2")] });
  assert.deepEqual(r.body.took, ["b1", "b2"]);
  r = await postData(e, { v: 1, sid: "zyxwvutsrq97", pages: [page("c1")] });
  assert.equal(r.status, 429, "today's total cap");
});

test("data: a blocked sender code gets every page as bad", async () => {
  const e = dataEnv();
  e.DATA.blocked.add(SID);
  const r = await postData(e, { v: 1, sid: SID, pages: [PAGE_A] });
  assert.deepEqual(r.body, { took: [], dup: [], bad: ["k3x9q2ab"], over: [] });
  assert.equal(e.DATA.rows.size, 0);
});

test("data: D1 down 503; rate limit 429", async () => {
  const e = dataEnv();
  e.DATA.fail = true;
  assert.equal((await postData(e, { v: 1, sid: SID, pages: [PAGE_A] })).status, 503);
  const e2 = dataEnv({ DATA_LIMIT: { limit: async () => ({ success: false }) } });
  assert.equal((await postData(e2, { v: 1, sid: SID, pages: [PAGE_A] })).status, 429);
});

test("data: the cron deletes raw pages older than 183 days, once a day at 03:00 UTC", async () => {
  const e = dataEnv();
  assert.equal(CLEAN_SQL, "DELETE FROM gather_pages WHERE day < date('now', '-183 days')");
  assert.equal(await cleanUp(e, Date.UTC(2026, 10, 26, 2, 30)), false);
  assert.equal(await cleanUp(e, Date.UTC(2026, 10, 26, 3, 30)), false);
  await worker.scheduled({ scheduledTime: Date.UTC(2026, 10, 26, 3, 0) }, e);
  assert.deepEqual(e.DATA.ran, [CLEAN_SQL]);
  assert.equal(await cleanUp(env(), Date.UTC(2026, 10, 26, 3, 0)), false, "no DATA binding");
});

test("data: logs and observability off in wrangler.toml; nothing logged", async () => {
  const { readFileSync } = await import("node:fs");
  const toml = readFileSync(new URL("./wrangler.toml", import.meta.url), "utf8");
  assert.match(toml, /\[observability\]\s*\nenabled = false/);
  assert.ok(!/logpush\s*=\s*true/i.test(toml));
  assert.match(toml, /binding = "DATA"\s*\ndatabase_name = "guide-data"/);
  const src = readFileSync(new URL("./worker.js", import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, "");
  assert.ok(!src.includes("console."), "no console logging");
  assert.ok(!/request\.cf\b/.test(src), "request.cf never read");
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
