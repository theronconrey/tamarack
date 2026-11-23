#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The pool comes back by itself after a reboot: integrity devices from
# /etc/integritytab, md arrays under their own names, the volume group,
# the mount, and the engine's view of it all. Runs twice: tmt-reboot
# restarts this script with TMT_REBOOT_COUNT=1.
. "$(dirname "$0")/lib.sh"

STATE=/var/tmp/tamarack-reboot

if [ "${TMT_REBOOT_COUNT:-0}" -eq 0 ]; then
    trap cleanup EXIT
    # File-backed drives: target.service brings them back after the reboot.
    make_file_drives 2048 2048 2048
    start_engine
    drive create "${DRIVES[@]}"
    findmnt /srv/tamarack >/dev/null || fail "not mounted"
    write_data
    grep -q '^tk-' /etc/integritytab || fail "no integrity devices in /etc/integritytab"
    install -d -m 0700 "$STATE"
    echo "$START" > "$STATE/start"
    ls /dev/md/ | grep '^tk-l' | sort > "$STATE/arrays"
    [ -s "$STATE/arrays" ] || fail "no /dev/md/tk-l* arrays"
    check_avc
    sync
    trap - EXIT  # keep the pool for the next boot
    tmt-reboot
    fail "tmt-reboot returned"
fi

trap cleanup EXIT
START=$(cat "$STATE/start")

# Everything below must have happened at boot, with no help from the test.
systemctl is-active --quiet target.service || fail "target.service did not bring the drives back"
for name in $(sed -n 's/^\(tk-[^ ]*\) .*/\1/p' /etc/integritytab); do
    [ -e "/dev/mapper/$name" ] || fail "integrity device $name not opened at boot"
done
ls /dev/md/ | grep '^tk-l' | sort > "$STATE/arrays-after"
diff -u "$STATE/arrays" "$STATE/arrays-after" \
    || { cat /proc/mdstat >&2; mdadm --detail --scan >&2; fail "md arrays came back under other names"; }
systemctl is-active --quiet srv-tamarack.mount || fail "srv-tamarack.mount is not active"
findmnt /srv/tamarack >/dev/null || fail "not mounted after reboot"
check_data
systemctl is-enabled --quiet tamarack-scrub.timer || fail "scrub timer not enabled"

# The engine reattaches to the pool it finds.
[ "$(drive health)" = ok ] || fail "health is $(drive health) after reboot"
[ "$(drive degraded)" = False ] || fail "degraded after reboot"
[ "$(drive tolerated)" = 1 ] || fail "should survive one drive failure"
check_ppl  # md kept the partial parity log across the reboot
check_avc
rm -rf "$STATE"
