#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# A pool drive that drops out and comes back is put back by the engine on
# its own, erasing nothing (decided Nov 24, 2026): the drive is pulled from
# under the running pool, the pool runs degraded, then the same drive (same
# identity, data still on it: scsi_debug keeps its store) is found again by
# a SCSI rescan, perhaps under another name.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 3 3072
start_engine
d=("${DRIVES[@]}")
drive create "${d[0]}" "${d[1]}" "${d[2]}"
mdadm --wait /dev/md/tk-l1 || :
write_data
sync

# Remember where the middle drive lives, then pull it.
victim=$(basename "${d[1]}")
host=$(ls "/sys/block/$victim/device/scsi_device/" | cut -d: -f1)
[ -n "$host" ] || fail "cannot find the SCSI host of $victim"
echo 1 > "/sys/block/$victim/device/delete"
udevadm settle
# md only notices when I/O to the member fails.
dd if=/dev/urandom of=/srv/tamarack/shares/poke bs=1M count=64 oflag=direct conv=fsync status=none
rm -f /srv/tamarack/shares/poke
[ "$(drive degraded)" = True ] || fail "the pool should be degraded with a drive pulled"
check_data

# Plug it back in: the same drive, data and all.
echo "- - -" > "/sys/class/scsi_host/host$host/scan"
udevadm settle
back=""
for dev in /sys/block/sd*; do
    [ "$(cat "$dev/device/model" 2>/dev/null | tr -d ' ')" = "scsi_debug" ] || continue
    path="/dev/$(basename "$dev")"
    # The scsi_debug drive that isn't one of the two that stayed.
    if [ "$path" != "${d[0]}" ] && [ "$path" != "${d[2]}" ]; then back=$path; fi
done
[ -n "$back" ] || { lsblk >&2; fail "the drive did not come back after the rescan"; }
echo "the drive is back as $back"
DRIVES=("${d[0]}" "$back" "${d[2]}")  # cleanup wipes what is there now

# The engine notices, re-adds it and md copies its part back. No command
# from us: wait for the pool to be whole again.
for _ in $(seq 120); do
    [ "$(drive degraded)" = False ] && break
    sleep 5
done
[ "$(drive degraded)" = False ] || {
    journalctl -u tamarack-engine -b --no-pager | tail -60 >&2
    cat /proc/mdstat >&2
    fail "the returning drive was not put back"
}
journalctl -u tamarack-engine -b --no-pager | grep -q "came back: re-adding it" \
    || fail "the engine did not log the re-add"
mdadm --wait /dev/md/tk-l1 || :
[ "$(drive health)" = ok ] || fail "health is $(drive health) after the re-add"
check_data
check_ppl
check_avc
