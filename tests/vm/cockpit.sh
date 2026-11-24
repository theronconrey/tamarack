#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The Cockpit page on a real pool: Cockpit lists the package, an ordinary
# user (as the page runs) can read every property it shows, and the page's
# own logic, fed those properties, says the pool is protected and fills the
# Drives, Shares and Activity tabs. Shares are created, changed and removed
# with the exact requests the page sends.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 2 3072
start_engine
d=("${DRIVES[@]}")
drive create "${d[0]}" "${d[1]}"
write_data

# Send the engine what the page's share form sends (as root: in Cockpit,
# administrative access; polkit decides either way).
page_share() {  # JSON {"form", "user", "existing"} -> SetShare
    node "$HERE/page-share.js" | python3 -c '
import json, sys
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
r = json.load(sys.stdin)
options = {k: GLib.Variant(v["t"], v["v"]) for k, v in r["options"].items()}
Gio.bus_get_sync(Gio.BusType.SYSTEM, None).call_sync(
    "com.borealislogistics.Tamarack1", "/com/borealislogistics/Tamarack1",
    "com.borealislogistics.Tamarack1.Manager", "SetShare",
    GLib.Variant("(sa{sv})", (r["name"], options)), None, 0, 60_000, None)
'
}
echo '{"form": {"name": "photos", "clients": "192.0.2.0/24\n198.51.100.7", "readOnly": false},
       "user": {"id": 1000, "gid": 1000}}' | page_share || fail "the page's new-share request was refused"
[ "$(stat -c %u:%g /srv/tamarack/shares/photos)" = 1000:1000 ] \
    || fail "a new share's folder should belong to the signed-in user"
grep -q "^/srv/tamarack/shares/photos .*198.51.100.7/32(rw," /etc/exports.d/tamarack.exports \
    || { cat /etc/exports.d/tamarack.exports >&2; fail "photos not exported as the form asked"; }

snapper --config tamarack create --description "before the test" || fail "snapper could not take a snapshot"

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
assert t["snapshots"] and t["snapshots"][0]["label"] == "before the test", t["snapshots"]
assert t["schedule"].startswith("A snapshot of your shares every hour"), t["schedule"]
' || fail "the Drives, Shares or Activity tab is wrong"

# Edit: the page sends the share it read, with only "what they can do" changed.
echo "$props" | python3 -c '
import json, sys
share = json.loads(json.load(sys.stdin)["Shares"])[0]
print(json.dumps({"form": {"clients": "\n".join(share["clients"]), "readOnly": True},
                  "user": {"id": 0, "gid": 0}, "existing": share}))
' | page_share || fail "the page's edit request was refused"
grep -q "^/srv/tamarack/shares/photos .*198.51.100.7/32(ro," /etc/exports.d/tamarack.exports \
    || { cat /etc/exports.d/tamarack.exports >&2; fail "photos should be read-only now"; }
[ "$(stat -c %u:%g /srv/tamarack/shares/photos)" = 1000:1000 ] || fail "an edit changed the owner"

# The schedule form: what the page sends, as root (administrative access).
node -e '
const S = require("/usr/share/cockpit/tamarack/status.js");
process.stdout.write(JSON.stringify(S.scheduleRequest({ hourly: "48", daily: "7", weekly: "0", monthly: "12" })));
' | python3 -c '
import json, sys
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
Gio.bus_get_sync(Gio.BusType.SYSTEM, None).call_sync(
    "com.borealislogistics.Tamarack1", "/com/borealislogistics/Tamarack1",
    "com.borealislogistics.Tamarack1.Manager", "SetSnapshotSchedule",
    GLib.Variant("(a{su})", (json.load(sys.stdin),)), None, 0, 60_000, None)
' || fail "the page's schedule request was refused"
snapper --config tamarack get-config | grep -Eq "^TIMELINE_LIMIT_HOURLY[[:space:]]*[|][[:space:]]*48[[:space:]]*$" \
    || { snapper --config tamarack get-config >&2; fail "snapper did not get the new schedule"; }

# Stop sharing: the export goes, the files stay.
gdbus call --system --dest com.borealislogistics.Tamarack1 --object-path /com/borealislogistics/Tamarack1 \
    --method com.borealislogistics.Tamarack1.Manager.RemoveShare photos >/dev/null || fail "RemoveShare failed"
grep -q "/photos " /etc/exports.d/tamarack.exports && fail "photos is still exported"
[ -d /srv/tamarack/shares/photos ] || fail "stopping a share must keep its folder"
check_avc
