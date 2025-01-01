#!/usr/bin/env python3
"""adsb-merge — reconcile a local readsb feed with public ADS-B.

Runs next to a readsb/ultrafeeder receiver. It is the *single* public poller
for a site: it polls the local readsb JSON (own antenna, sub-second, no rate
limit) and one public aggregator radius query (adsb.fi, adsb.lol as fallback),
reconciles them per-aircraft, and re-serves a readsb-shaped
``{now, aircraft:[...]}`` that any ``/data/aircraft.json`` consumer can read.

Reconciliation rule: **local is the source of truth**; public only wins for an
aircraft when it is *strictly fresher* (by ``MARGIN_SEC``) or when local has no
position for it at all — e.g. a low helicopter or approach below
the antenna's horizon, or a Mode-S target the wider feeder mesh MLATs that a
single receiver can't. That fills the coverage gaps without letting laggier
public data stomp the real-time local truth.

Key unit gotcha handled in ``normalize()``: readsb-local reports top-level
``now`` in **seconds** and uses the ``aircraft`` key; the adsb.fi / adsb.lol
re-API reports ``now`` in **milliseconds** and uses the ``ac`` key. Per-aircraft
``seen`` / ``seen_pos`` are seconds in both.

stdlib only — no pip installs.

Required env: ADSB_LAT, ADSB_LON (receiver location). See README.md.
"""

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- configuration (environment-overridable) --------------------------------
try:
    LAT = float(os.environ["ADSB_LAT"])
    LON = float(os.environ["ADSB_LON"])
except (KeyError, ValueError):
    raise SystemExit("adsb-merge: set ADSB_LAT and ADSB_LON (receiver location, decimal degrees)")
RADIUS_NM = int(os.environ.get("ADSB_RADIUS_NM", "50"))
LOCAL_URL = os.environ.get("ADSB_LOCAL_URL", "http://127.0.0.1/data/aircraft.json")
PUBLIC_URL = os.environ.get(
    "ADSB_PUBLIC_URL",
    "https://opendata.adsb.fi/api/v3/lat/%s/lon/%s/dist/%s" % (LAT, LON, RADIUS_NM))
PUBLIC_URL2 = os.environ.get(
    "ADSB_PUBLIC_URL2",
    "https://api.adsb.lol/v2/lat/%s/lon/%s/dist/%s" % (LAT, LON, RADIUS_NM))
PORT = int(os.environ.get("ADSB_PORT", "8754"))
LOCAL_POLL = float(os.environ.get("ADSB_LOCAL_POLL_SEC", "1.0"))
PUBLIC_POLL = float(os.environ.get("ADSB_PUBLIC_POLL_SEC", "3.0"))
MARGIN_SEC = float(os.environ.get("ADSB_MARGIN_SEC", "2.0"))
MAX_AGE_SEC = float(os.environ.get("ADSB_MAX_AGE_SEC", "30.0"))
LOCAL_TIMEOUT = float(os.environ.get("ADSB_LOCAL_TIMEOUT", "4"))
PUBLIC_TIMEOUT = float(os.environ.get("ADSB_PUBLIC_TIMEOUT", "8"))
STATUS_EVERY = float(os.environ.get("ADSB_STATUS_EVERY_SEC", "15"))
# Optional merged-feed snapshots (tmpfs) for the host tar1090 instance to
# display. Empty = disabled (standalone runs stay write-free); the systemd
# unit sets /run/adsb-merge via RuntimeDirectory.
SNAPSHOT_DIR = os.environ.get("ADSB_SNAPSHOT_DIR", "")
SNAPSHOT_SEC = float(os.environ.get("ADSB_SNAPSHOT_SEC", "1.0"))
# tar1090 only un-hides its replay button when receiver.json advertises
# haveReplay. We don't record history ourselves — set this only when the
# operator has wired globe_history into the serving instance (e.g.
# readsb's heatmap chunks bind-mounted to /var/globe_history). Note the
# replay is therefore LOCAL-ANTENNA ONLY; public mesh contacts are live-merged
# but never recorded, so they don't appear in playback.
HAVE_REPLAY = os.environ.get("ADSB_HAVE_REPLAY", "0") not in ("0", "", "no", "false")
UA = os.environ.get("ADSB_USER_AGENT",
                    "adsb-merge/1.0 (personal non-commercial; data c/o adsb.fi + adsb.lol)")

# Identity fields backfilled onto the winner from the loser when missing.
IDENT_FIELDS = ("flight", "t", "r", "desc", "category", "squawk", "ownOp", "year")


def log(msg):
    print("[adsb-merge] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def fetch_json(url, timeout):
    """GET url -> parsed JSON dict, or None on any failure (non-fatal)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # network / timeout / HTTP / JSON — all recoverable
        host = url.split("/api/")[0].split("/data/")[0]
        log("fetch fail %s: %s" % (host, exc))
        return None


def normalize(payload):
    """payload -> (now_seconds, {hex: (ac, pos_epoch|None, seen_epoch)}).

    Handles both schemas (``aircraft`` + seconds vs ``ac`` + milliseconds) and
    converts each aircraft's relative ``seen`` / ``seen_pos`` ages into absolute
    epoch-second timestamps so the two sources can be compared apples-to-apples.
    """
    if not payload:
        return None, {}
    now = payload.get("now")
    if now is None:
        now = time.time()
    elif now > 1e12:                       # adsb.fi / adsb.lol report now in ms
        now = now / 1000.0
    arr = payload.get("aircraft")
    if arr is None:
        arr = payload.get("ac") or []
    out = {}
    for ac in arr:
        hx = ac.get("hex")
        if not hx:
            continue
        positioned = ac.get("lat") is not None and ac.get("lon") is not None
        sp = ac.get("seen_pos")
        pos_epoch = (now - sp) if (positioned and sp is not None) else None
        sn = ac.get("seen")
        seen_epoch = (now - sn) if sn is not None else now
        out[hx.lower()] = (ac, pos_epoch, seen_epoch)
    return now, out


def choose(lac, pac, lpt, ppt):
    """Return 'local' or 'public' — which record to keep for one aircraft.

    lpt / ppt are absolute epoch-seconds of each source's last position fix, or
    None if that source has no position for this aircraft.
    """
    if lac and not pac:
        return "local"
    if pac and not lac:
        return "public"
    # both sources have the aircraft
    if lpt is None and ppt is None:
        return "local"                     # neither positioned — keep local record
    if lpt is None:
        return "public"                    # only public has a usable position
    if ppt is None:
        return "local"
    # both positioned: a real ADS-B fix beats an MLAT-derived one; otherwise
    # prefer the fresher fix, with a margin that favours local (lower latency).
    l_mlat = bool(lac.get("mlat"))
    p_mlat = bool(pac.get("mlat"))
    if l_mlat and not p_mlat and ppt > lpt - MARGIN_SEC:
        return "public"
    if p_mlat and not l_mlat and lpt > ppt - MARGIN_SEC:
        return "local"
    if ppt > lpt + MARGIN_SEC:
        return "public"
    return "local"


def build_merged(local_payload, public_payload):
    """Reconcile the two payloads -> (list[aircraft], counts)."""
    _, L = normalize(local_payload)
    _, P = normalize(public_payload)
    merged = []
    counts = {"local": 0, "public": 0}
    for hx in set(L) | set(P):
        lac, lpt, lst = L.get(hx, (None, None, None))
        pac, ppt, pst = P.get(hx, (None, None, None))
        src = choose(lac, pac, lpt, ppt)
        if src == "local":
            base, other, pt, st = dict(lac), pac, lpt, lst
        else:
            base, other, pt, st = dict(pac), lac, ppt, pst
        if other:                          # backfill identity from the loser
            for f in IDENT_FIELDS:
                if not base.get(f) and other.get(f):
                    base[f] = other[f]
        base.pop("dst", None)              # public-only distance/bearing —
        base.pop("dir", None)              # drop; consumers recompute from lat/lon
        base["hex"] = hx                   # canonical lowercase ICAO (stable track id)
        base["src"] = src                  # provenance (ignored by readsb consumers)
        base["_pt"] = pt                   # private: abs position-fix epoch
        base["_st"] = st                   # private: abs last-seen epoch
        merged.append(base)
        counts[src] += 1
    return merged, counts


def fetch_public(state):
    """Refresh the cached public snapshot (adsb.fi, falling back to adsb.lol).

    Runs on its own thread so the multi-second public fetch (and adsb.fi's
    frequent timeouts) never stalls the 1 Hz local rebuild. On failure the last
    good ``state["pub"]`` is kept and ages out naturally via MAX_AGE_SEC.
    """
    t = time.time()
    pp, src = fetch_json(PUBLIC_URL, PUBLIC_TIMEOUT), "adsb.fi"
    if not (pp and pp.get("ac") is not None):
        pp, src = fetch_json(PUBLIC_URL2, PUBLIC_TIMEOUT), "adsb.lol"
    with state["lock"]:
        if pp and pp.get("ac") is not None:
            state["pub"], state["pub_src"], state["last_pub"] = pp, src, t
        else:
            state["last_pub"], state["pub_src"] = t, "down"


def public_poller(state):
    while True:
        try:
            fetch_public(state)
        except Exception as exc:               # never let the poll loop die
            log("public poll error: %s" % exc)
        time.sleep(PUBLIC_POLL)


def local_poll_once(state):
    """One reconciliation cycle against the freshest local + cached public.

    Both sides are compared as absolute epoch-seconds (see ``normalize``), so
    reconciling against a public snapshot that is a few seconds old is honest —
    local's sub-second fixes win, public only fills genuine gaps. Because the
    local fetch is no longer serialized behind the slow public fetch, the two
    snapshots are captured close together in time, so ``choose`` compares them
    fairly instead of always seeing public as "fresher".
    """
    t = time.time()
    local_payload = fetch_json(LOCAL_URL, LOCAL_TIMEOUT)
    with state["lock"]:
        pub, pub_src, last_pub = state["pub"], state["pub_src"], state["last_pub"]
    merged, counts = build_merged(local_payload, pub)
    summary = {
        "aircraft": len(merged),
        "local": counts["local"],
        "public": counts["public"],
        "pub_src": pub_src,
        "pub_age_sec": round(t - last_pub, 1) if pub else None,
        "local_up": local_payload is not None,
    }
    with state["lock"]:
        state["latest"], state["summary"] = merged, summary
        # readsb's cumulative message counter, passed through so tar1090's
        # message-rate readout stays honest. Kept at last-known when local is
        # down (a frozen counter reads as rate 0, which is the truth).
        if local_payload and local_payload.get("messages") is not None:
            state["local_messages"] = local_payload["messages"]


def local_poller(state):
    last_status = 0.0
    while True:
        try:
            local_poll_once(state)
            now = time.time()
            if now - last_status >= STATUS_EVERY:
                s = state["summary"]
                log("%d ac (local %d / public %d), pub=%s age=%ss, local=%s"
                    % (s["aircraft"], s["local"], s["public"], s["pub_src"],
                       s["pub_age_sec"], "up" if s["local_up"] else "DOWN"))
                last_status = now
        except Exception as exc:               # never let the poll loop die
            log("local poll error: %s" % exc)
        time.sleep(LOCAL_POLL)


def serialize_feed(state):
    """Serialize the latest merge, re-aging seen/seen_pos to *right now* so
    downstream staleness logic (e.g. dropping seen_pos > 15 s) stays
    honest regardless of poll/serve jitter."""
    with state["lock"]:
        snapshot = state["latest"]
        summary = state["summary"]
        messages = state["local_messages"]
    now = time.time()
    out = []
    for a in snapshot:
        pt, st = a.get("_pt"), a.get("_st")
        if pt is not None and now - pt > MAX_AGE_SEC:
            continue                           # positioned but stale — drop
        if pt is None and st is not None and now - st > MAX_AGE_SEC:
            continue                           # positionless and stale — drop
        ac = {k: v for k, v in a.items() if not k.startswith("_")}
        if st is not None:
            ac["seen"] = round(max(0.0, now - st), 1)
        if pt is not None:
            ac["seen_pos"] = round(max(0.0, now - pt), 3)
        else:
            ac.pop("seen_pos", None)
        out.append(ac)
    return json.dumps(
        {"now": round(now, 1), "messages": messages, "aircraft": out,
         "merge": summary})


def snapshot_writer(state):
    """Mirror the merged feed into SNAPSHOT_DIR (tmpfs) for the host tar1090
    instance: aircraft.json every SNAPSHOT_SEC plus a one-time receiver.json
    (tar1090 reads lat/lon + refresh from it). Atomic os.replace so readers
    never see a torn file. Any failure just logs and retries — the HTTP feed
    is unaffected."""
    def put(name, body):
        tmp = os.path.join(SNAPSHOT_DIR, name + ".tmp")
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, os.path.join(SNAPSHOT_DIR, name))

    wrote_receiver = False
    while True:
        try:
            if not wrote_receiver:
                put("receiver.json", json.dumps(
                    {"version": "adsb-merge/1.0",
                     "refresh": int(SNAPSHOT_SEC * 1000),
                     "history": 0, "lat": LAT, "lon": LON,
                     "haveReplay": HAVE_REPLAY}))
                wrote_receiver = True
            put("aircraft.json", serialize_feed(state))
        except Exception as exc:               # dir missing / ENOSPC / ...
            log("snapshot error: %s" % exc)
            time.sleep(10)
        time.sleep(SNAPSHOT_SEC)


class Handler(BaseHTTPRequestHandler):
    server_version = "adsb-merge/1.0"

    def log_message(self, *_):                 # quiet; journald already has our logs
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass                               # client hung up mid-write

    def do_GET(self):
        state = self.server.state
        path = self.path.split("?", 1)[0]
        if path in ("/data/aircraft.json", "/aircraft.json"):
            self._send(200, serialize_feed(state))
        elif path in ("/", "/status"):
            with state["lock"]:
                summary = dict(state["summary"])
            self._send(200, json.dumps(summary, indent=2))
        else:
            self._send(404, '{"error":"not found"}')

def main():
    state = {
        "lock": threading.Lock(),
        "latest": [],
        "summary": {"aircraft": 0, "local": 0, "public": 0,
                    "pub_src": "-", "pub_age_sec": None, "local_up": False},
        "pub": None,
        "pub_src": "-",
        "last_pub": 0.0,
        "local_messages": 0,
    }
    log("start: local=%s public=%s serve=:%d/data/aircraft.json radius=%dnm"
        % (LOCAL_URL, PUBLIC_URL, PORT, RADIUS_NM))
    fetch_public(state)                        # seed the public cache first
    local_poll_once(state)                     # populate before opening the socket
    threading.Thread(target=public_poller, args=(state,), daemon=True).start()
    threading.Thread(target=local_poller, args=(state,), daemon=True).start()
    if SNAPSHOT_DIR:
        log("snapshots -> %s every %.1fs" % (SNAPSHOT_DIR, SNAPSHOT_SEC))
        threading.Thread(target=snapshot_writer, args=(state,), daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.state = state
    log("listening on :%d" % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
