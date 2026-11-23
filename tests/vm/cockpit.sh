#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The Cockpit page on a real pool: Cockpit lists the package, an ordinary
# user (as the page runs) can read every property it shows, and the page's
# own logic, fed those properties, says the pool is protected.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 2 3072
start_engine
d=("${DRIVES[@]}")
drive create "${d[0]}" "${d[1]}"
write_data

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
check_avc
