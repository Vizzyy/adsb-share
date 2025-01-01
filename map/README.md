# map: merged-sky tar1090

A second, host-side [tar1090](https://github.com/wiedehopf/tar1090) instance that displays the
`adsb-merge` feed (`../merge`) instead of raw readsb, so mesh gap-fill contacts show up on the map.

`adsb-merge-src.patch` (against tar1090 upstream `b018536`) makes tar1090 render the merge's provenance tag
(`src=local|public`):

- **public (mesh gap-fill) contacts are ghosted**: `ColorBySrcPublic` (defaults.js, `{s:-55, l:10}`,
  overridable in config.js) desaturates them; local contacts keep the stock altitude colors.
- Info panel, hover box and the table "Source" column append **" · mesh"**.

The patch modifies tar1090, so it is licensed GPL-2.0-or-later like tar1090 itself.

## Install

Needs `adsb-merge` running with `ADSB_SNAPSHOT_DIR=/run/adsb-merge`.

```bash
git clone https://github.com/wiedehopf/tar1090 ~/tar1090-git && cd ~/tar1090-git
git checkout b018536 && git apply /path/to/adsb-merge-src.patch
# Always pass the patched checkout as arg 4: a bare install.sh pulls vanilla upstream and deploys unpatched html.
sudo bash install.sh /run/adsb-merge tar1090 /usr/local/share/tar1090 ~/tar1090-git
cat config-snippet.js | sudo tee -a /usr/local/share/tar1090/html/config.js
```

tar1090's installer sets up lighttpd. If something else owns :80, change lighttpd's `server.port` first
(install it with a temporary `policy-rc.d` that blocks starting the service, so it never fights for :80).

**Replay** (optional): readsb's heatmap chunks live in the ultrafeeder volume, but tar1090 serves
`/globe_history/` from the literal path `/var/globe_history`. Install `var-globe_history.mount` (edit the
`What=` path) and set `ADSB_HAVE_REPLAY=1` on adsb-merge. Replay is local-antenna only, because mesh
contacts are merged live and never recorded. Keep the mount free of `After=local-fs.target`, which makes
an ordering cycle that silently drops it at boot.

## Updating upstream

```bash
cd ~/tar1090-git
git stash && git fetch --depth 1 origin master && git reset --hard FETCH_HEAD
git apply /path/to/adsb-merge-src.patch   # fix conflicts if upstream moved
sudo bash install.sh /run/adsb-merge tar1090 /usr/local/share/tar1090 ~/tar1090-git
```
