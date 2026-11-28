#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The pool comes back by itself after a reboot: integrity devices from
# /etc/integritytab, md arrays under their own names, the volume group,
# the mount, and the engine's view of it all. Runs twice: tmt-reboot
# restarts this script with TMT_REBOOT_COUNT=1.
. "$(dirname "$0")/lib.sh"

STATE=/var/tmp/tamarack-reboot
EARLY_DRIVES=tamarack-test-drives.service

# What boot did with the pool's pieces, printed when the second half fails.
boot_report() {
    echo "=== boot: integritytab, devices, units ===" >&2
    cat /etc/integritytab >&2 || :
    lsblk -o NAME,SIZE,TYPE,PARTLABEL,PARTUUID >&2 || :
    systemctl --no-pager status "$EARLY_DRIVES" target.service 'systemd-integritysetup@*' \
        srv-tamarack.mount >&2 2>&1 || :
    systemctl --no-pager list-units --failed >&2 || :
    journalctl -b --no-pager -o short-precise -u "$EARLY_DRIVES" -u target.service \
        -u 'systemd-integritysetup@*' -u srv-tamarack.mount -u var.mount | tail -80 >&2 || :
    journalctl -b --no-pager -o short-precise -k | grep -Ei 'sd[a-z]|tcm_loop|md[0-9]|integrity' \
        | tail -60 >&2 || :
}

boot_exit() {
    local status=$?
    [ "$status" -eq 0 ] || boot_report
    systemctl disable "$EARLY_DRIVES" 2>/dev/null || :
    rm -f "/etc/systemd/system/$EARLY_DRIVES"
    cleanup "$status"
}

if [ "${TMT_REBOOT_COUNT:-0}" -eq 0 ]; then
    trap boot_exit EXIT
    # File-backed drives, brought back early in the next boot (see the unit).
    make_file_drives 2048 2048 2048
    systemctl disable target.service
    install -m 0644 "$HERE/$EARLY_DRIVES" "/etc/systemd/system/$EARLY_DRIVES"
    systemctl daemon-reload
    systemctl enable "$EARLY_DRIVES"
    start_engine
    drive create "${DRIVES[@]}"
    findmnt /srv/tamarack >/dev/null || fail "not mounted"
    write_data
    grep -q '^tk-' /etc/integritytab || fail "no integrity devices in /etc/integritytab"
    install -d -m 0700 "$STATE"
    echo "$AUDIT_START" > "$STATE/audit-start"
    echo "$AUDIT_MARK" > "$STATE/audit-mark"
    ls /dev/md/ | grep '^tk-l' | sort > "$STATE/arrays"
    [ -s "$STATE/arrays" ] || fail "no /dev/md/tk-l* arrays"
    # Created with --homehost=any, so every host (and boot, before the
    # hostname is set) treats the arrays as local and keeps their names.
    echo "hostname before reboot: $(hostname)"
    for md in $(cat "$STATE/arrays"); do
        mdadm --detail "/dev/md/$md" | grep -q "Name : any:$md\b" \
            || { mdadm --detail "/dev/md/$md" >&2; fail "$md is not stored as any:$md"; }
    done
    check_avc
    sync
    trap - EXIT  # keep the pool for the next boot
    tmt-reboot
    fail "tmt-reboot returned"
fi

trap boot_exit EXIT
AUDIT_LOG=/var/log/audit/audit.log
AUDIT_START=$(cat "$STATE/audit-start")
AUDIT_MARK=$(cat "$STATE/audit-mark")

# Everything below must have happened at boot, with no help from the test.
systemctl is-active --quiet "$EARLY_DRIVES" || fail "the test drives did not come back"
for name in $(sed -n 's/^\(tk-[^ ]*\) .*/\1/p' /etc/integritytab); do
    [ -e "/dev/mapper/$name" ] || fail "integrity device $name not opened at boot"
done
ls /dev/md/ | grep '^tk-l' | sort > "$STATE/arrays-after"
diff -u "$STATE/arrays" "$STATE/arrays-after" \
    || { echo "hostname: $(hostname)" >&2; cat /proc/mdstat >&2; mdadm --detail --scan >&2; fail "md arrays came back under other names"; }
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
