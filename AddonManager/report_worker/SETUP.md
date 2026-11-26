# Guide reports: setup (Cloudflare + GitHub, free)

Players of All-in-One Guide report a wrong guide step without a GitHub account:

- **The easy way:** they press **Send** on the addon's Report page. The Addon Manager sends the
  report the next time it runs while the game is closed (`am\reports.py`; it asks the player once).
- **Without the Addon Manager:** they copy the report in the game and paste it on this Worker's web
  page (`https://aog-report.<subdomain>.workers.dev`).

This folder is the server: a Cloudflare Worker (`worker.js`) that checks each report and files it
as a GitHub issue on `gettic/All-in-One-Guide`, with a D1 database (`schema.sql`) so each report is
filed once and at most `REPORT_CAP` a day. Cost: $0 (free plans). It keeps no IP address and no logs.

The addon and the Addon Manager already use the address `https://aog-report.gtd-relay.workers.dev`
(`HR.WEB_URL` in the addon's `UI\HubReport.lua`, `REPORT_URL` in `am\config.py`). If the Worker
gets another address, both must be changed to match.

Everything below is done once, about 15 minutes. The commands run in a terminal opened in this
folder (`AddonManager\report_worker`). Wrangler runs through `npx` (Node.js is already installed on
this PC; nothing to install globally). Menu names on the web pages can differ a little.

## 1. The GitHub token (only you can do this, it's your login)

1. Open https://github.com/settings/personal-access-tokens/new (Settings, Developer settings,
   Personal access tokens, **Fine-grained tokens**, Generate new token).
2. Token name: `aog-report-worker`. Expiration: 1 year (put a reminder in `ideas.md` under
   "Private (never upload)", without the token itself).
3. Repository access: **Only select repositories**, pick `gettic/All-in-One-Guide`.
4. Permissions, Repository permissions: **Issues: Read and write**. Nothing else.
5. **Generate token** and copy it (it is shown only once). Don't paste it anywhere else.

Better, optional: make a free second GitHub account just for this (a bot), give it access to the
repository, and make the token there. Then issues aren't shown as written by you.

## 2. The labels on GitHub (once)

On https://github.com/gettic/All-in-One-Guide/labels add these three (the others in
`.github\labels.md` are there already):

| Label | Colour | Meaning |
|---|---|---|
| `via-app` | `#0075ca` | Sent by the Addon Manager. |
| `via-web` | `#0075ca` | Sent from the report web page. |
| `needs-review` | `#e99695` | A name was removed from the note, or it may hold a rude word: check before replying. |

## 3. The database

1. `npx wrangler login` (opens the browser; skip it if Wrangler is already logged in).
2. `npx wrangler d1 create guide-reports`
3. It prints a `database_id`. Put it into `wrangler.toml` instead of
   `PUT-THE-ID-FROM-wrangler-d1-create-HERE` (it is not a secret).
4. `npx wrangler d1 execute guide-reports --remote --file schema.sql`

## 4. Turnstile (the check against bots on the web page)

1. Cloudflare dashboard, **Turnstile**, **Add widget**. Name: `aog-report`. Hostname:
   `aog-report.gtd-relay.workers.dev`. Widget mode: **Managed**. Create.
2. Copy the **Site key** into `wrangler.toml` as `TURNSTILE_SITE_KEY = "..."` (it is public).
3. Keep the **Secret key** for the next step.

## 5. Deploy and add the two secrets

1. `npx wrangler deploy` (prints the Worker's address; check it is `https://aog-report.gtd-relay.workers.dev`).
2. `npx wrangler secret put GITHUB_TOKEN` and paste the GitHub token from step 1.
3. `npx wrangler secret put TURNSTILE_SECRET` and paste the Turnstile secret key from step 4.

The secrets live only in Cloudflare. They are never in `wrangler.toml`, the repository, the Addon
Manager or the addon. In the Worker's **Settings, Observability**, check that Workers Logs are
**off** (`wrangler.toml` turns them off; never turn on real-time logs or Logpush).

## Later

- Changes to `worker.js` or `wrangler.toml`: `npx wrangler deploy` again. Wrangler overwrites
  variables changed in the dashboard, so change `REPORT_CAP` in `wrangler.toml`.
- The token runs out after a year: make a new one (step 1) and run
  `npx wrangler secret put GITHUB_TOKEN` again. Until then, reports wait in the database and are
  filed by the 30-minute cron once the new token is in (after 20 failed tries a report is dropped).
- Run the Worker's own tests after a change: `node test.mjs` in this folder (no network used).

## Reading the numbers

D1, `guide-reports`, **Console**:

```sql
SELECT day, via, state, COUNT(*) AS n FROM reports
WHERE day >= date('now', '-13 days') GROUP BY day, via, state ORDER BY day DESC;
```

Reports still waiting for GitHub: `SELECT rid, day, tries FROM reports WHERE state = 'pending';`

## What a report holds

Only the report line from the addon: the addon and client version, the language, the guide and
step, the type of mistake, the player's level, class and race, the note (at most 200 characters),
and the map position (x and y rounded to whole percent in the public issue; no time of day is shown) unless the player ticked "Leave out my location". Never a name, account,
path or IP address. A "Name-Realm" in the note is removed before posting.
