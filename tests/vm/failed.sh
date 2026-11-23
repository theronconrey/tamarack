#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 4 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}" "${d[1]}" "${d[2]}"
mdadm --wait /dev/md/tk-l1 || :  # a drive lost during the first sync is another test
write_data
sync  # on the drives, not only in the page cache
drive confirm-backup

# Pull the middle drive out from under the running arrays.
echo 1 > "/sys/block/$(basename "${d[1]}")/device/delete"
udevadm settle
# md only learns a member is gone when I/O to it fails (the member is the
# dm-integrity mapping, which outlives the disk): a direct write reaches
# every member of a RAID5 stripe.
dd if=/dev/urandom of=/srv/tamarack/shares/poke bs=1M count=64 oflag=direct conv=fsync status=none
rm -f /srv/tamarack/shares/poke
echo 3 > /proc/sys/vm/drop_caches  # make the reads reach the drives
check_data  # still readable while degraded
[ "$(drive degraded)" = True ] || fail "the arrays should report degraded"

drive rebuild "${d[1]}" "${d[3]}"
[ "$(drive degraded)" = False ] || fail "still degraded after the rebuild"
check_data
check_avc
