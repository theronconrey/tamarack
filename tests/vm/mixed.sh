#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

# File-backed drives, so they can differ in size.
# As small as the planner allows (1 GiB usable, sizes more than 1 GiB apart),
# with LIO's write-back cache: synchronous fileio under dm-integrity's journal
# reshaped at under 1 MB/s. This test is about layout, not power cuts.
export TAMARACK_JOB_TIMEOUT=2100  # inside the 45-minute test limit
FILE_DRIVE_WRITE_BACK=true make_file_drives 1280 1280 2560 2560
start_engine
d=("${DRIVES[@]}")

# 1.25 + 1.25 + 2.5 GB: one single-parity layer across all three, and the
# top half of the 2.5 GB drive left unused until something can pair with it.
drive create "${d[0]}" "${d[1]}" "${d[2]}"
findmnt /srv/tamarack >/dev/null || fail "not mounted"
write_data

# The second 2.5 GB drive widens the parity layer to four drives, and its top
# half pairs with the unused half of the first in a new two-copy layer.
drive confirm-backup
drive add "${d[3]}"
check_data

[ "$(drive tolerated)" = 1 ] || fail "should survive one drive failure"
for d1 in "${d[@]}"; do
    /usr/libexec/tamarack-meta read "${d1}1" >/dev/null || fail "no layout on ${d1}1"
done
btrfs scrub start -B /srv/tamarack || fail "scrub found errors"
check_ppl
check_avc
