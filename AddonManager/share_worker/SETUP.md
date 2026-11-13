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

## Guild storage for big sends (tried before file.kiwi)

**Stay on Workers Free. Never move this account to Workers Paid:** on Free, going over a limit
is an error ("busy"), never a bill; on Paid, storage over the free amount is billed.

With the send-later tick on, the app first keeps the sealed backup on this Worker itself
(Durable Object SQLite storage: `StoreLedger` keeps the list, `StoreSlice` holds < 1 GB each),
so the other PC's app can download it by itself. Only when that is full, switched off, too big
or stops answering does it use file.kiwi.

- **What it keeps:** the already-encrypted blob (random id, no names), the sizes and two token
  hashes (the tokens come with each request over HTTPS and are only hashed, never kept). The
  tokens themselves are made by the apps; the get token is only inside the sealed
  record. No IP addresses (the per-IP count is in RateBox's memory only).
- **Deleted:** when the receiver has downloaded it (the app deletes it), when the code is
  cancelled, used wrongly or runs out, when the sender stops while uploading, 6 hours after
  the code was used, and after 24 hours at most in any case (alarms). The code ends 2 h
  before that hard end.
- **Limits** (`[vars]`, change and deploy): `STORE_PIECE` 4 MiB per request, `STORE_ROW`
  1 MiB per row, `STORE_SLICE` 900 MiB per object, `STORE_MAX_SEND` 4 GB per send,
  `STORE_LIVE_CAP` 4.4 GB for everyone at once (Free: 5 GB of DO storage in all),
  `STORE_DAY_CAP` 20 GB per UTC day, `HARD_STORE` 3 stored sends per IP per day.
- **CPU (10 ms per request on Free):** after deploying, send a ~300 MB and a ~2 GB test backup
  and look at the Worker's CPU time in the dashboard metrics. If requests hit the limit (error
  1102), set `STORE_PIECE = "1048576"`. If that still fails, switch it off (below).
- **See what's stored:** dashboard -> Durable Objects -> the namespaces' storage; after a test
  it should be back near zero.
- **Switch it off:** `STORE_ON = "0"` and deploy. No new stores and no more uploads (an upload
  that is running stops and the app goes on with file.kiwi); downloads and deletes of sends
  already stored still work, so receivers can finish. Everything stored is gone 24 h later.
- **Unused reservations:** a reservation that gets no piece within `STORE_START_SECONDS` (5 min)
  is dropped, and today's count (`STORE_DAY_CAP`) keeps only the bytes really stored.
- Migration `v3` adds the two classes (SQLite-backed, as Free needs).

Local test: the same `wrangler dev --local` copy as above, ideally with `STORE_PIECE = "65536"`,
`STORE_SLICE = "262144"` and `STORE_ROW = "20000"` so one small backup crosses several slices
and rows (`LocalLaterWorkerTests.test_guild_storage_round_trip`).

## Switch it off

Set `SHARE_URL = ""` in the app (next release hides the buttons), or delete the Worker in the
dashboard (the app then says the send service can't be reached).
