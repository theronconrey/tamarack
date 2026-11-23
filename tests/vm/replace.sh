#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 4 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}" "${d[1]}" "${d[2]}"
write_data
drive confirm-backup
drive replace "${d[1]}" "${d[3]}"
check_data
[ "$(drive tolerated)" = 1 ] || fail "should survive one drive failure"
check_avc
