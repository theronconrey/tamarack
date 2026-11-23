#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 4 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}" "${d[1]}"
findmnt /srv/tamarack >/dev/null || fail "not mounted"
write_data

drive confirm-backup
drive add "${d[2]}"
check_data
drive add "${d[3]}"
check_data

[ "$(drive tolerated)" = 1 ] || fail "should survive one drive failure"
for d1 in "${d[@]}"; do
    /usr/libexec/tamarack-meta read "${d1}1" >/dev/null || fail "no layout on ${d1}1"
done
btrfs scrub start -B /srv/tamarack || fail "scrub found errors"

# Lose the engine's state: it must read the layout back from the drives.
systemctl stop tamarack-engine
rm -f /var/lib/tamarack/layout.json
systemctl start tamarack-engine
[ "$(drive tolerated)" = 1 ] || fail "layout not recovered from the drives"
[ -s /var/lib/tamarack/layout.json ] || fail "recovered layout not saved"
check_ppl
check_avc
