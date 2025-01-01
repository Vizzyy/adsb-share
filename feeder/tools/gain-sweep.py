#!/usr/bin/env python3
"""Interleaved RTL gain sweep for ultrafeeder/readsb.
Usage: gain-sweep.py [ROUNDS=4] [FINAL_GAIN=3.7]; env ADSB_BASE = ultrafeeder web root
(default http://127.0.0.1:8080), ADSB_CONTAINER (default ultrafeeder).
Sets gain at runtime via /run/readsb/setGain (docker exec), holds HOLD s, then
records stats.json last1min only if that window started after the change."""
import json, random, subprocess, sys, time, urllib.request
import os
BASE = os.environ.get('ADSB_BASE', 'http://127.0.0.1:8080')
CONTAINER = os.environ.get('ADSB_CONTAINER', 'ultrafeeder')
GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 12.5]
ROUNDS, HOLD = int(sys.argv[1]) if len(sys.argv) > 1 else 4, 80
FINAL = sys.argv[2] if len(sys.argv) > 2 else '3.7'
out = open('/tmp/gainsweep.jsonl', 'a')
def get(p): return json.load(urllib.request.urlopen(BASE + p, timeout=5))
def setgain(g):
    subprocess.run(['docker', 'exec', CONTAINER, 'sh', '-c', f'echo {g} > /run/readsb/setGain'], check=True)
rng = random.Random(1)
for r in range(ROUNDS):
    order = GAINS[:]; rng.shuffle(order)
    for g in order:
        setgain(g); t0 = time.time()
        time.sleep(HOLD)
        for _ in range(4):
            s = get('/data/stats.json'); w = s['last1min']
            if w['start'] >= t0 + 0.5: break
            time.sleep(5)
        else:
            print(json.dumps({'gain': g, 'round': r, 'skipped': True}), file=out, flush=True); continue
        ac = get('/data/aircraft.json')['aircraft']
        l = w['local']; acc = sum(l.get('accepted', [0]))
        rec = {'gain': g, 'round': r, 'start': w['start'], 'end': w['end'],
               'accepted': acc, 'msg_s': acc / (w['end'] - w['start']),
               'strong': l.get('strong_signals'), 'signal': l.get('signal'), 'noise': l.get('noise'),
               'peak': l.get('peak_signal'), 'positions': w.get('position_count_total'),
               'max_nm': (w.get('max_distance') or 0) / 1852, 'tracks': w.get('tracks', {}).get('all'),
               'ac_pos': sum(1 for a in ac if a.get('seen_pos', 999) < 60),
               'ac_all': len(ac)}
        print(json.dumps(rec), file=out, flush=True)
setgain(FINAL)
print(json.dumps({'done': True, 'final_gain': FINAL}), file=out, flush=True)
