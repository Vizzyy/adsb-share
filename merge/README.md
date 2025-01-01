# adsb-merge

Single-poller ADS-B reconciliation. Polls a local `readsb` feed (e.g. ultrafeeder, `../feeder`) *and* a
public adsb.fi radius query (adsb.lol as fallback), merges them per aircraft, and re-serves a readsb-shaped
`{now, aircraft:[...]}` on **`:8754/data/aircraft.json`**, so anything that reads a readsb feed can read it
unchanged. Python stdlib only.

## Why

A single antenna can't see below its radio horizon — low helicopters and
low approaches drop out, and a lone receiver can't MLAT a Mode-S-only
target. The public aggregate (stitched from the whole regional feeder mesh) *can*. This
service fills those gaps **without** letting laggier public data override the
real-time local truth: local always wins unless public is strictly fresher
(`MARGIN_SEC`, default 2 s) or local has no position at all.

Only this process ever polls the public API; local consumers just read `:8754`.

## Reconciliation rule (`choose()`)

Per aircraft, keyed on ICAO hex, comparing absolute position-fix timestamps:

| Situation | Winner |
|---|---|
| Only local has it | local |
| Only public has it (e.g. low heli below horizon) | public |
| Only one has a position fix | that one |
| Local is MLAT, public is real ADS-B (≈ as fresh) | public |
| Public fix newer than local by > `MARGIN_SEC` | public |
| Otherwise | local |

Identity fields (callsign, type, reg…) are backfilled from the loser; each
aircraft carries a `"src":"local"|"public"` tag for debugging. `seen`/`seen_pos`
are re-aged to serve time, so consumers' staleness cuts (e.g. `seen_pos > 15 s`) stay honest.

## Units gotcha

readsb-local `now` is **seconds** + key `aircraft`; adsb.fi/adsb.lol `now` is
**milliseconds** + key `ac`. `normalize()` handles both.

## Deploy

```bash
install -d ~/adsb-merge && install adsb_merge.py ~/adsb-merge/
sudo install -m 644 adsb-merge.service /etc/systemd/system/   # edit User, paths, ADSB_LAT/LON first
sudo systemctl daemon-reload && sudo systemctl enable --now adsb-merge
curl -s localhost:8754/status
curl -s localhost:8754/data/aircraft.json | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["aircraft"]),"ac",d["merge"])'
```

## Tunables (env / unit `Environment=`)

`ADSB_LAT` `ADSB_LON` (required) `ADSB_RADIUS_NM` (50) `ADSB_PORT` (8754)
`ADSB_MARGIN_SEC` (2) `ADSB_MAX_AGE_SEC` (30) `ADSB_PUBLIC_POLL_SEC` (3)
`ADSB_LOCAL_URL` `ADSB_PUBLIC_URL` `ADSB_PUBLIC_URL2` `ADSB_USER_AGENT`
`ADSB_SNAPSHOT_DIR` `ADSB_SNAPSHOT_SEC` (1) `ADSB_HAVE_REPLAY` (0).

## Snapshots

When `ADSB_SNAPSHOT_DIR` is set (the unit uses `/run/adsb-merge` via `RuntimeDirectory=`), a writer thread
mirrors the merged feed to `aircraft.json` every `ADSB_SNAPSHOT_SEC` plus a one-time `receiver.json`, with
atomic `os.replace()` writes. A tar1090 instance pointed at that dir shows the merged sky (see `../map`).
The feed's `messages` field is readsb's real cumulative counter, passed through so tar1090's message rate
stays honest.
