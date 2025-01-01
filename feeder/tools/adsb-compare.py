#!/usr/bin/env python3
"""adsb-compare — objective before/after comparison for the ADS-B receiver.

Compares an "after" window against the same clock-time window on each of the
previous N days, from two independent data sources that both survive a
receiver move or antenna change:

  1. graphs1090 RRDs  (readsb stats, 1/3-min resolution): message rate,
     signal / noise / peak dBFS, strong-signal %, range (max + median),
     aircraft counts, positions/s, MLAT aircraft.
  2. readsb heatmap chunks in globe_history (one position per aircraft per
     15 s, tagged with its source): unique aircraft, aircraft-time, range
     percentiles, per-bearing coverage, low-altitude coverage — split into
     LOCAL RF (ADS-B / ADS-R / TIS-B heard by this antenna) vs MLAT (solved by
     the network from several receivers' timing).

Stdlib only. Run on the receiver host (needs ADSB_LAT/ADSB_LON or --lat/--lon):

  python3 adsb-compare.py --after "2025-01-15 19:00" "2025-01-15 21:00" --days 7 \
      --cutover "2025-01-15 12:00" --json out.json

Heatmap format (readsb): gzip despite the .ttf name; 16-byte records <iiii>
= word0, lat*1e6, lon*1e6, alt/gs packed. word0 == 0xe7f7c9d marks a 15 s
slice and carries epoch-ms split hi(lat)/lo(lon). Otherwise low 24 bits = ICAO
hex, bit 24 = non-ICAO, bits 27-31 = address type (tar1090 mapping below).
"""
import argparse, bisect, datetime as dt, glob, gzip, json, math, os, struct, subprocess, sys
from collections import defaultdict

LAT0 = float(os.environ.get('ADSB_LAT', 'nan'))
LON0 = float(os.environ.get('ADSB_LON', 'nan'))
GLOBE = os.environ.get('ADSB_GLOBE_HISTORY', os.path.expanduser('~/ultrafeeder/globe_history'))
RRD = '/var/lib/collectd/rrd/localhost/dump1090-localhost'  # inside the container
MAGIC = 0xe7f7c9d
LOCAL_TYPES = {0, 2, 3, 8, 9, 10, 11}  # adsb, adsr, tisb (+ _other variants): heard over the air here
MLAT_TYPES = {5}
NM = 1852.0


def haversine_nm(lat, lon):
    p1, p2 = math.radians(LAT0), math.radians(lat)
    dphi, dl = p2 - p1, math.radians(lon - LON0)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * 6371008.8 * math.asin(min(1, math.sqrt(a))) / NM
    brg = (math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                   math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
    return d, brg


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q / 100
    f = math.floor(k)
    return xs[f] if f == len(xs) - 1 else xs[f] + (xs[f + 1] - xs[f]) * (k - f)


# ---------------------------------------------------------------- heatmaps --
def heat_chunks(t0, t1):
    """Yield heatmap files overlapping [t0, t1) (epoch s). Files are UTC half-hours."""
    t = int(t0 // 1800 * 1800)
    while t < t1:
        u = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        f = f'{GLOBE}/{u:%Y/%m/%d}/heatmap/{u.hour * 2 + u.minute // 30:02d}.bin.ttf'
        if os.path.exists(f):
            yield f
        t += 1800


def heat_window(t0, t1):
    loc_pts, mlat_pts = [], []          # (dist_nm, bearing, alt_ft or None)
    loc_ac, mlat_ac, all_ac = set(), set(), set()
    loc_slots = mlat_slots = 0
    slices = 0
    for f in heat_chunks(t0, t1):
        try:
            raw = gzip.decompress(open(f, 'rb').read())
        except OSError:
            raw = open(f, 'rb').read()
        ts = None
        for w0, lat, lon, w3 in struct.iter_unpack('<iiii', raw[: len(raw) // 16 * 16]):
            u0 = w0 & 0xffffffff
            if u0 == MAGIC:
                ts = ((lat << 32) | (lon & 0xffffffff)) / 1000.0
                if t0 <= ts < t1:
                    slices += 1
                continue
            if ts is None or not (t0 <= ts < t1) or abs(lat) > 90_000_000:
                continue
            typ = (u0 >> 27) & 0x1f
            hexid = u0 & 0x1ffffff
            alt = w3 & 0xffff
            if alt & 0x8000:
                alt -= 0x10000
            alt = None if alt in (-123, -124) else alt * 25
            d, b = haversine_nm(lat / 1e6, lon / 1e6)
            if d > 400:            # junk / far-away decodes
                continue
            all_ac.add(hexid)
            if typ in LOCAL_TYPES:
                loc_pts.append((d, b, alt)); loc_ac.add(hexid); loc_slots += 1
            elif typ in MLAT_TYPES:
                mlat_pts.append((d, b, alt)); mlat_ac.add(hexid); mlat_slots += 1
    hours = max(slices * 15 / 3600, 1e-9)
    dists = [p[0] for p in loc_pts]
    sectors = []
    for s in range(36):
        ds = [p[0] for p in loc_pts if int(p[1] // 10) == s]
        sectors.append({'p95': pct(ds, 95), 'max': max(ds) if ds else None, 'n': len(ds)})
    low = [p for p in loc_pts if p[2] is not None and p[2] < 3000 and p[0] < 25]
    low_m = [p for p in mlat_pts if p[2] is not None and p[2] < 3000 and p[0] < 25]
    return {
        'hours_covered': round(slices * 15 / 3600, 2),
        'local_unique_ac': len(loc_ac), 'mlat_unique_ac': len(mlat_ac),
        'local_ac_minutes_per_hr': round(loc_slots * 15 / 60 / hours, 1),
        'mlat_ac_minutes_per_hr': round(mlat_slots * 15 / 60 / hours, 1),
        'local_range_nm': {q: round(pct(dists, q), 1) if dists else None for q in (50, 90, 99)},
        'local_range_max_nm': round(max(dists), 1) if dists else None,
        'low_alt_lt3000ft_lt25nm_ac_minutes_per_hr': round(len(low) * 15 / 60 / hours, 1),
        'low_alt_mlat_ac_minutes_per_hr': round(len(low_m) * 15 / 60 / hours, 1),
        'sectors': sectors,
    }


# --------------------------------------------------------------------- RRDs --
RRD_SERIES = {
    'msgs_per_s': ('dump1090_messages-local_accepted', 'AVERAGE', 0),
    'positions_per_s': ('dump1090_messages-positions', 'AVERAGE', 0),
    'strong_per_s': ('dump1090_messages-strong_signals', 'AVERAGE', 0),
    'signal_dbfs': ('dump1090_dbfs-signal', 'AVERAGE', 0),
    'noise_dbfs': ('dump1090_dbfs-noise', 'AVERAGE', 0),
    'peak_dbfs': ('dump1090_dbfs-peak_signal', 'AVERAGE', 0),
    'aircraft_total': ('dump1090_aircraft-recent', 'AVERAGE', 0),
    'aircraft_with_pos': ('dump1090_aircraft-recent', 'AVERAGE', 1),
    'mlat_aircraft': ('dump1090_mlat-recent', 'AVERAGE', 0),
    'range_max_nm': ('dump1090_range-max_range', 'AVERAGE', 0),
    'range_max_peak_nm': ('dump1090_range-max_range', 'MAX', 0),
    'range_median_nm': ('dump1090_range-median', 'AVERAGE', 0),
    'gain_db': ('dump1090_misc-gain_db', 'AVERAGE', 0),
}


def rrd_fetch(name, cf, t0, t1, res):
    cmd = ['docker', 'exec', 'ultrafeeder', 'rrdtool', 'fetch', f'{RRD}/{name}.rrd', cf,
           '-r', str(res), '-s', str(int(t0)), '-e', str(int(t1))]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        if ':' not in line:
            continue
        ts, rest = line.split(':', 1)
        vals = [None if v.strip().lower() in ('nan', '-nan') else float(v) for v in rest.split()]
        if t0 < int(ts) <= t1:
            rows.append(vals)
    return rows


def rrd_window(t0, t1, res):
    out = {}
    for key, (name, cf, col) in RRD_SERIES.items():
        rows = [r[col] for r in rrd_fetch(name, cf, t0, t1, res) if len(r) > col and r[col] is not None]
        if not rows:
            out[key] = None; continue
        v = max(rows) if key == 'range_max_peak_nm' else sum(rows) / len(rows)
        if key.startswith('range'):
            v /= NM
        out[key] = round(v, 2)
    if out.get('msgs_per_s') and out.get('strong_per_s') is not None:
        out['strong_pct'] = round(100 * out['strong_per_s'] / out['msgs_per_s'], 2)
    if out.get('signal_dbfs') is not None and out.get('noise_dbfs') is not None:
        out['snr_db'] = round(out['signal_dbfs'] - out['noise_dbfs'], 2)
    if out.get('msgs_per_s') and out.get('aircraft_total'):
        out['msgs_per_s_per_aircraft'] = round(out['msgs_per_s'] / out['aircraft_total'], 2)
    return out


# --------------------------------------------------------------------- main --
def parse_local(s):
    return dt.datetime.strptime(s, '%Y-%m-%d %H:%M').astimezone().timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--after', nargs=2, required=True, metavar=('START', 'END'), help='local "YYYY-MM-DD HH:MM"')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--res', type=int, default=180, help='RRD resolution (60 only reaches ~2 days back)')
    ap.add_argument('--cutover', default=None,
                    help='old setup ended here; "before" windows must end by then (default: --after start)')
    ap.add_argument('--lat', type=float, help='receiver latitude (default: env ADSB_LAT)')
    ap.add_argument('--lon', type=float, help='receiver longitude (default: env ADSB_LON)')
    ap.add_argument('--globe', help='readsb globe_history dir (default: env ADSB_GLOBE_HISTORY or ~/ultrafeeder/globe_history)')
    ap.add_argument('--json')
    a = ap.parse_args()
    A0, A1 = parse_local(a.after[0]), parse_local(a.after[1])
    global LAT0, LON0, GLOBE
    if a.lat is not None: LAT0 = a.lat
    if a.lon is not None: LON0 = a.lon
    if a.globe: GLOBE = a.globe
    if math.isnan(LAT0) or math.isnan(LON0):
        sys.exit('adsb-compare: set ADSB_LAT/ADSB_LON or pass --lat/--lon')
    CUT = parse_local(a.cutover) if a.cutover else A0
    wins = [('after', A0, A1)]
    d = 0
    while len(wins) <= a.days and d < 60:   # N clean prior days, skipping any that straddle the cutover
        d += 1
        b0 = (dt.datetime.fromtimestamp(A0) - dt.timedelta(days=d)).timestamp()
        b1 = (dt.datetime.fromtimestamp(A1) - dt.timedelta(days=d)).timestamp()
        if b1 > CUT:
            continue
        wins.append((f'before-{d}d', b0, b1))
    res = {'after_window': a.after, 'generated': dt.datetime.now().isoformat(timespec='seconds'), 'windows': []}
    for label, t0, t1 in wins:
        w = {'label': label, 'start': dt.datetime.fromtimestamp(t0).strftime('%a %m-%d %H:%M'),
             'end': dt.datetime.fromtimestamp(t1).strftime('%H:%M'),
             'rrd': rrd_window(t0, t1, a.res), 'heat': heat_window(t0, t1)}
        res['windows'].append(w)
        r, h = w['rrd'], w['heat']
        print(f"{label:10} {w['start']}-{w['end']}  msg/s={r.get('msgs_per_s')} pos/s={r.get('positions_per_s')} "
              f"sig={r.get('signal_dbfs')} noise={r.get('noise_dbfs')} snr={r.get('snr_db')} strong%={r.get('strong_pct')} "
              f"ac={r.get('aircraft_total')}/{r.get('aircraft_with_pos')} mlat={r.get('mlat_aircraft')} "
              f"rmax={r.get('range_max_nm')}/{r.get('range_max_peak_nm')} rmed={r.get('range_median_nm')} | "
              f"heat uniq={h['local_unique_ac']} acmin/h={h['local_ac_minutes_per_hr']} p50/90/99={list(h['local_range_nm'].values())} "
              f"max={h['local_range_max_nm']} low={h['low_alt_lt3000ft_lt25nm_ac_minutes_per_hr']} mlat_uniq={h['mlat_unique_ac']}",
              flush=True)
    if a.json:
        json.dump(res, open(a.json, 'w'), indent=1)


if __name__ == '__main__':
    main()
