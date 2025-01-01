# feeder

[ultrafeeder](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder) for one RTL-SDR dongle:
readsb, tar1090, graphs1090, and feeding + MLAT to adsb.fi, adsb.lol and ADS-B Exchange.

## Setup

1. **Blacklist the DVB driver before the first plug-in** (`sudo install -m 644 blacklist-rtl-sdr.conf
   /etc/modprobe.d/`). On Ubuntu, `dvb_usb_rtl28xxu` grabbed an R820T dongle on first plug-in and wedged
   the tuner ("PLL not locked"; the output was one fixed tone at every frequency and gain). Unloading the
   module and a USB reset didn't fix it; only a physical replug did.
2. `cp .env.example .env`, fill in location, dongle serial (`rtl_test` or `lsusb -v`) and one `uuidgen` UUID per
   aggregator. `ADSB_ALT` is height above the WGS84 ellipsoid: ground elevation + antenna height − geoid
   undulation (NOAA GEOID18 in the US).
3. `docker compose up -d`, then open `http://<host>:8080/`.
4. Sweep the gain (below), set `READSB_GAIN`, and pin the image digest so later comparisons aren't
   confounded by a software change.

## Tools

- `tools/gain-sweep.py [ROUNDS] [FINAL_GAIN]`: interleaved runtime gain sweep through readsb's
  `/run/readsb/setGain` (no restart), shuffled order per round, logs one `stats.json` minute per step to
  `/tmp/gainsweep.jsonl`. Env `ADSB_BASE` (web root), `ADSB_CONTAINER`.
- `tools/adsb-compare.py --after START END [--days N] [--cutover T]`: compares a window against the same
  clock-time window on N previous days, from graphs1090 RRDs and readsb heatmap chunks. Local RF and MLAT are
  split, with range percentiles, per-bearing and low-altitude coverage. Good for judging an antenna or LNA change.
  Needs `ADSB_LAT`/`ADSB_LON` (or `--lat/--lon`) and the `globe_history` dir.
- Runtime gain change: `docker exec ultrafeeder sh -c 'echo 2.7 > /run/readsb/setGain'` (not persistent).
