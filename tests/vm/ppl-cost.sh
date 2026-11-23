#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# What md's partial parity log costs in write speed, on a two-drive and a
# three-drive layer under dm-integrity. Reports numbers; fails only if a
# layer is not left with PPL on.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 3 3072
start_engine
d=("${DRIVES[@]}")

speed() {  # label: MB/s for 512 MiB of direct, synced writes, best of two
    local best=0 rate
    for _ in 1 2; do
        rate=$(dd if=/dev/zero of=/srv/tamarack/shares/speed bs=1M count=512 \
            oflag=direct conv=fdatasync 2>&1 | sed -nE 's/.*, ([0-9.]+) MB\/s$/\1/p')
        rm -f /srv/tamarack/shares/speed
        [ -n "$rate" ] && awk -v r="$rate" -v b="$best" 'BEGIN { exit !(r > b) }' && best=$rate
    done
    echo "SPEED $1: ${best} MB/s"
}

measure() {  # layer description
    mdadm --wait /dev/md/tk-l1 || :
    speed "$1, ppl"
    mdadm --grow /dev/md/tk-l1 --consistency-policy=resync
    speed "$1, no ppl"
    mdadm --grow /dev/md/tk-l1 --consistency-policy=ppl
}

drive create "${d[0]}" "${d[1]}"
measure "2 drives"
drive confirm-backup
drive add "${d[2]}"
measure "3 drives"
check_ppl
check_avc
