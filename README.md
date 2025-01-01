# ADS-B feeder + merge + merged-sky map

Three small pieces that sit around a single RTL-SDR ADS-B receiver:

- **`feeder/`**: ultrafeeder (readsb, tar1090, graphs1090) feeding adsb.fi, adsb.lol and ADS-B Exchange
  with MLAT, plus a runtime gain-sweep tool and a before/after coverage comparison tool.
- **`merge/`**: `adsb-merge`, a stdlib-only service that reconciles your local readsb feed with a public
  adsb.fi/adsb.lol radius query. Local data wins unless the public fix is clearly fresher, or local has no
  position (low helicopters, approaches below your horizon, Mode-S-only targets the mesh MLATs).
  It re-serves a readsb-compatible `/data/aircraft.json`.
- **`map/`**: a tar1090 patch plus install notes for a second map that shows the merged feed, with
  public "mesh" contacts ghosted. Optional replay from readsb's heatmaps.

Each folder has its own README. Tested on Ubuntu 24.04 (amd64) and Raspberry Pi OS (arm64).

## License

MIT (see `LICENSE`), except `map/adsb-merge-src.patch`: it modifies tar1090, so it is licensed like
tar1090 itself, GPL-2.0-or-later.
