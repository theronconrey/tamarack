#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The Cockpit page on a real pool: Cockpit lists the package, an ordinary
# user (as the page runs) can read every property it shows, and the page's
# own logic, fed those properties, says the pool is protected and fills the
# Drives, Shares and Activity tabs.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 2 3072
start_engine
d=("${DRIVES[@]}")
drive create "${d[0]}" "${d[1]}"
write_data
tamarack engine share set photos --client 192.0.2.0/24 >/dev/null || fail "could not create a share"

cockpit-bridge --packages | grep -qw tamarack || fail "Cockpit does not list the tamarack page"

id tk-viewer >/dev/null 2>&1 || useradd --no-create-home --shell /sbin/nologin tk-viewer
props=$(setpriv --reuid tk-viewer --regid tk-viewer --clear-groups python3 - <<'PY'
import json
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
reply = bus.call_sync(
    "com.borealislogistics.Tamarack1", "/com/borealislogistics/Tamarack1",
    "org.freedesktop.DBus.Properties", "GetAll",
    GLib.Variant("(s)", ("com.borealislogistics.Tamarack1.Manager",)),
    None, 0, 30_000, None)
print(json.dumps(reply.unpack()[0]))
PY
) || fail "an ordinary user cannot read the engine's properties"
echo "$props" | python3 -c 'import json,sys; s=json.loads(json.load(sys.stdin)["Space"]); assert s["used"] > 0 and s["free"] > 0, s' \
    || fail "Space does not describe the mounted volume"
banner=$(echo "$props" | node "$HERE/page-banner.js") || fail "the page logic failed"
echo "page says: $banner"
[ "$banner" = "ok: Your data is protected" ] || fail "the page should say the data is protected, not: $banner"

tabs=$(echo "$props" | node "$HERE/page-tabs.js") || fail "the tabs' logic failed"
echo "tabs show: $tabs"
echo "$tabs" | python3 -c '
import json, sys
t = json.load(sys.stdin)
assert [d["label"] for d in t["drives"]] == ["Drive 1", "Drive 2"], t["drives"]
# scsi_debug and LIO drives report no SMART data: "Health unknown", not "Healthy".
assert all(d["state"] in ("Healthy", "Health unknown") for d in t["drives"]), t["drives"]
assert all(d["model"] and d["holds"] for d in t["drives"]), t["drives"]
assert t["advice"] is not None, "no upgrade advice"
assert [s["name"] for s in t["shares"]] == ["photos"], t["shares"]
assert t["shares"][0]["address"].endswith(":/srv/tamarack/shares/photos"), t["shares"]
assert t["history"][0]["title"] == "Set up the pool" and t["history"][0]["result"] == "Done", t["history"]
' || fail "the Drives, Shares or Activity tab is wrong"
tamarack engine share remove photos >/dev/null
check_avc
