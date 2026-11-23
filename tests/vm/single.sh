#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 2 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}"  # two copies on one drive
[ "$(drive tolerated)" = 0 ] || fail "one drive cannot survive a drive failure"
write_data
drive confirm-backup
drive add "${d[1]}"  # one copy moves to the new drive
check_data
[ "$(drive tolerated)" = 1 ] || fail "two drives should survive one failure"
check_ppl
check_avc
