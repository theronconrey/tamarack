#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Silent corruption: overwrite part of one drive underneath dm-integrity.
# dm-integrity must notice, md must repair from the other drives, and the
# data must come back intact.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 3 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}" "${d[1]}" "${d[2]}"
mdadm --wait /dev/md/tk-l1 || :  # the new array's first sync; check needs it done
write_data
sync

# 4 MiB of garbage, well past dm-integrity's superblock and journal.
dd if=/dev/urandom of="${d[0]}2" bs=1M seek=512 count=4 conv=notrunc oflag=direct status=none

# Read every member: md asks for the stripe again from the other drives.
mdadm --action=check /dev/md/tk-l1
mdadm --wait /dev/md/tk-l1 || :
journalctl -k --since "$START" | grep -qi "checksum failed" \
    || fail "dm-integrity did not report the corruption"
echo 3 > /proc/sys/vm/drop_caches
check_data
[ "$(drive degraded)" = False ] || fail "a repaired read should not degrade the array"
btrfs scrub start -B /srv/tamarack || fail "scrub found errors"
check_avc
