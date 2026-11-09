# Usage counter: setup (Cloudflare, free plan)

The Addon Manager sends one tiny anonymous ping a day (see `am\stats.py`), unless the player
unticked "Share anonymous counts" in its Settings window (on by default). This folder is the
server side: a Cloudflare Worker (`worker.js`) that adds the ping to daily and weekly totals in
a D1 database (`schema.sql`). Only the app's maker (the owner of the Cloudflare account) can
read the numbers; the Worker has no way to read them back. Cost: $0 (free plan). Nothing to install:
everything is done in the browser. Menu names can differ a little from what's written here.

## One-time setup (about 15-20 minutes)

1. Sign up at cloudflare.com with an email that doesn't show your real name (free plan).
2. **Workers & Pages**: when asked for a workers.dev subdomain, pick a neutral name (players can
   see it, since it is part of the address the app sends to).
3. **Storage & Databases -> D1 -> Create database**, name it `addon-stats`. Open it -> **Console**,
   paste everything from `schema.sql`, run it.
4. **Workers & Pages -> Create -> Worker**, start from "Hello World", name it `guild-stats`,
   **Deploy**. Then **Edit code**, replace everything with `worker.js`, **Deploy**.
5. The Worker -> **Settings -> Bindings -> Add -> D1 database**: variable name `DB`, database
   `addon-stats`.
6. The Worker -> **Settings -> Variables and Secrets**, add two plain-text variables:
   - `ADDONS` = `GuildHub,TaxiTime,NotePad,AddonManager`
   - `PING_CAP` = `200` (most pings counted per day; a guild sends far fewer, spam can't go past it)
7. The Worker -> **Settings -> Observability**: turn **Workers Logs off** (Cloudflare turns them
   on by default; with them off nothing about a request, the IP address included, is kept).
   Never open real-time logs and don't set up Logpush.
8. Give the Worker's address plus `/ping` (e.g. `https://guild-stats.<subdomain>.workers.dev/ping`)
   to the developer: it goes into `STATS_URL` in `am\config.py`. It's public, not a secret.

## Or: deploy with Wrangler (how the live one was set up)

From this folder, with Node.js installed (Wrangler runs through `npx`, nothing to install
globally). `wrangler.toml` already holds the settings: logs off, the D1 binding, `ADDONS` and
`PING_CAP`.

1. `npx wrangler login` (opens the browser to log in to Cloudflare).
2. `npx wrangler d1 create addon-stats`, then put the `database_id` it prints into
   `wrangler.toml` (not a secret).
3. `npx wrangler d1 execute addon-stats --remote --file schema.sql` (creates the tables).
4. `npx wrangler deploy`. It prints the Worker's address; add `/ping` for `STATS_URL`.

Later changes: edit `wrangler.toml` / `worker.js` and run `npx wrangler deploy` again. Wrangler
overwrites variables changed in the dashboard, so change `ADDONS` in `wrangler.toml`.

Later: when an addon is added to `addons.json`, add its folder name to `ADDONS` (until then it is
simply not counted; nothing breaks).

## Reading the numbers

D1 -> `addon-stats` -> **Console**, paste one query, run it. Days and weeks are in UTC; today's
and this week's numbers keep growing until the day/week is over.

**People this week** (each person once, by ISO week):
```sql
SELECT addon,
  SUM(CASE WHEN state = 'latest'   THEN n ELSE 0 END) AS on_latest,
  SUM(CASE WHEN state = 'outdated' THEN n ELSE 0 END) AS outdated,
  SUM(CASE WHEN state = 'beta'     THEN n ELSE 0 END) AS beta
FROM weekly
WHERE week = printf('%s-W%02d', strftime('%Y', date('now', '-3 days', 'weekday 4')),
  (CAST(strftime('%j', date('now', '-3 days', 'weekday 4')) AS INTEGER) - 1) / 7 + 1)
GROUP BY addon ORDER BY addon;
```
Last week: replace both `date('now', '-3 days', 'weekday 4')` with
`date('now', '-10 days', 'weekday 4')`.

**People today** (replace `date('now')` with `date('now', '-1 day')` for yesterday):
```sql
SELECT addon,
  SUM(CASE WHEN state = 'latest'   THEN n ELSE 0 END) AS on_latest,
  SUM(CASE WHEN state = 'outdated' THEN n ELSE 0 END) AS outdated,
  SUM(CASE WHEN state = 'beta'     THEN n ELSE 0 END) AS beta
FROM daily
WHERE day = date('now') AND addon <> '_total'
GROUP BY addon ORDER BY addon;
```

**Last 14 days, per day:**
```sql
SELECT day, addon, SUM(n) AS people FROM daily
WHERE day >= date('now', '-13 days') AND addon <> '_total'
GROUP BY day, addon ORDER BY day DESC, addon;
```

**Which versions people have this week:**
```sql
SELECT addon, version, state, n FROM weekly
WHERE week = (SELECT MAX(week) FROM weekly)
ORDER BY addon, version;
```

Don't add up daily numbers to get a week: the same person is in every day they opened the app.
Use the weekly table for "how many people".

If something looks faked (far more than the guild's size), delete that day or week:
`DELETE FROM daily WHERE day = '2026-11-09';` or `DELETE FROM weekly WHERE week = '2026-W46';`

## What the numbers mean

- Counted: people who opened the Addon Manager that day (daily) or at least once that week
  (weekly). Someone who has the addon but doesn't open the manager isn't counted.
- `latest`: installed version is the newest normal release (or newer). `outdated`: older.
  `beta`: a test version. `AddonManager` is the app itself.
- Each ping holds only addon folder names, versions and those states. No name, account,
  character, ID, path or IP address is stored.
