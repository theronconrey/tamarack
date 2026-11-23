#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Kill the engine in the middle of a reshape. The kernel carries on; the
# engine must pick the job up again on restart, wait for the reshape by
# itself, then pause until an admin resumes it.
. "$(dirname "$0")/lib.sh"
restore_speed() { echo 200000 > /proc/sys/dev/raid/speed_limit_max; cleanup; }
trap restore_speed EXIT

make_drives 3 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}" "${d[1]}"
write_data
drive confirm-backup
echo 20000 > /proc/sys/dev/raid/speed_limit_max  # KB/s: a reshape of minutes

job=$(drive start-add "${d[2]}" | tail -1)
drive wait-for "$job" waiting
systemctl kill --signal=KILL tamarack-engine
sleep 1
systemctl start tamarack-engine

drive wait-for "$job" paused  # re-attached to the reshape, then paused
echo 200000 > /proc/sys/dev/raid/speed_limit_max
drive resume "$job"
check_data
[ "$(drive tolerated)" = 1 ] || fail "should survive one drive failure"
check_avc
