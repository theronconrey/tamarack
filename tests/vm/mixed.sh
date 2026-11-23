#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

# File-backed drives, so they can differ in size.
make_file_drives 2048 2048 4096 4096
start_engine
d=("${DRIVES[@]}")

# 2 + 2 + 4 GB: one single-parity layer across all three, and 2 GB of the
# 4 GB drive left unused until something can pair with it.
drive create "${d[0]}" "${d[1]}" "${d[2]}"
findmnt /srv/tamarack >/dev/null || fail "not mounted"
write_data

# The second 4 GB drive widens the parity layer to four drives, and its
# other 2 GB pair with the unused 2 GB of the first in a new mirror layer.
drive confirm-backup
drive add "${d[3]}"
check_data

[ "$(drive tolerated)" = 1 ] || fail "should survive one drive failure"
for d1 in "${d[@]}"; do
    /usr/libexec/tamarack-meta read "${d1}1" >/dev/null || fail "no layout on ${d1}1"
done
btrfs scrub start -B /srv/tamarack || fail "scrub found errors"
check_avc
