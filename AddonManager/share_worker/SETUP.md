# Send-a-backup relay: setup (Cloudflare, free plan)

"Send to another PC" in the Addon Manager (am\share.py) passes an encrypted backup from one PC
to the other through this small Cloudflare Worker (`worker.js`) with one Durable Object per code.
The relay only sees the channel number (the "37" in `37-amber-lamp-river`), sizes and times:
never the words of the code, the file name or what's inside (SPAKE2 + ChaCha20-Poly1305 on the
PCs). Everything is deleted when the transfer ends, is cancelled, a wrong code is used, or after
10 minutes without the other PC (2 minutes idle / 1 hour at most once it runs). Logs are off; IP
addresses are only hashed for the rate limit and never stored. Cost: $0 on the free plan.

It is a **separate Worker** from `guild-stats` (the usage counter): a problem here can't touch
the counts, and it can be switched off alone. With `SHARE_URL` empty in `am\config.py` the app
hides the Send/Receive buttons.

## Deploy with Wrangler (from this folder, Node.js installed)

1. `npx wrangler login` (opens the browser to log in to Cloudflare; same account as guild-stats).
2. `npx wrangler deploy`. `wrangler.toml` already holds everything: name `guild-share`, logs off,
   the `BOX` Durable Object (SQLite-backed, needed on the free plan) with its migration, the two
   rate limits (6 new codes and 6 code tries per minute per IP) and the limits in `[vars]`.
3. It prints the address (e.g. `https://guild-share.<subdomain>.workers.dev`). That address (no
   path) goes into `SHARE_URL` in `am\config.py`. It's public, not a secret.
4. Dashboard -> the Worker -> **Settings -> Observability**: check Workers Logs are **off**. Never
   open real-time logs, no Logpush.

Later changes: edit `worker.js` / `wrangler.toml`, `npx wrangler deploy` again. If the Durable
Object class is ever renamed, add a new `[[migrations]]` entry (never edit the old one).

## Test locally first (nothing online)

`npx wrangler dev --local --port 8787`, then from `AddonManager\`:
`$env:ADDONMANAGER_SHARE_TEST_URL="http://127.0.0.1:8787"; python -m unittest tests.test_share`
(raise the two `limit = 6` in a temp copy of `wrangler.toml` first, the tests open many codes).

## Free-plan budget (Cloudflare docs, Nov 2026)

| Limit (free) | Amount | One 50 MB transfer |
|---|---|---|
| Worker + Durable Object requests | 100,000 / day each | ~150 |
| Durable Object duration | 13,000 GB-s / day | 15-80 GB-s |
| Durable Object rows written | 100,000 / day | ~160 |
| Durable Object storage | 5 GB | at most 4 MiB per code |
Duration is the tight one: about 150+ transfers a day even when everyone waits the full 10
minutes. Over a limit, sends fail until 00:00 UTC (the app says "busy, try later"); no bill.

## Send later (file.kiwi)

Optional for players and off by default. If the other PC hasn't joined after 5 minutes (or right away for a backup over 500 MB, which the relay can't carry), the app uploads the backup to file.kiwi, a third-party file host. It is sealed with its own random key, with file.kiwi's encryption on top. The app then stores a small sealed record (the file.kiwi link plus that key) in this Worker's mailbox for the same code.

- **What the Worker keeps:** the record (at most 4 KB, unreadable without the code), a random salt and a hash used to check the code. It never holds the file.
- **Fetching the record:** `take` gives it out once and counts as a code try (the same per-IP limits as join). A wrong code deletes it.
- **Limits** (`[vars]`): `LATER_IDLE` 300 s without the sender while uploading, `LATER_UPLOAD_SECONDS` 6 h of uploading at most, `LATER_MAX_HOURS` 24 (the player picks 1-24 h).
- **Nothing to set up** in Cloudflare or at file.kiwi: no account and no key. The PCs talk to file.kiwi directly. This Worker never contacts file.kiwi.
- **Can't be deleted early:** file.kiwi deletes files on its own schedule (a few days). Once the code ends, the stored file can't be opened.
- **Switch it off:** remove `"later"` from `BOX_ROUTES` in `worker.js` and deploy. Setting `LATER_MAX_HOURS` to `"0"` doesn't work, because `num()` falls back to 24. Once it is off, the app's tick box fails cleanly with "Uploading to file.kiwi didn't work".

## Switch it off

Set `SHARE_URL = ""` in the app (next release hides the buttons), or delete the Worker in the
dashboard (the app then says the send service can't be reached).
