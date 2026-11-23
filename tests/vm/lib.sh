# SPDX-License-Identifier: GPL-3.0-or-later
#
# Shared setup for the VM tests. Sourced, not run.

set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

fail() { echo "FAIL: $*" >&2; exit 1; }

# On any failure, show what the engine and SELinux said, so a red run in
# Testing Farm explains itself.
diagnostics() {
    local status=$?
    [ "$status" -eq 0 ] && return 0
    echo "=== tamarack-engine journal ===" >&2
    journalctl -b -u tamarack-engine --no-pager -o short-precise 2>&1 | tail -200 >&2
    echo "=== SELinux denials ===" >&2
    ausearch -m AVC,USER_AVC,SELINUX_ERR -ts boot 2>&1 | tail -50 >&2
    echo "=== engine process context ===" >&2
    ps -eZ | grep -i tamarack >&2 || :
    echo "=== SELinux labels of the programs the engine runs ===" >&2
    for p in parted partprobe integritysetup mdadm pvcreate pvresize vgcreate vgextend \
        lvcreate lvextend mkfs.btrfs btrfs dmsetup systemctl blkid findmnt pvs vgs lvs; do
        ls -LZ "$(command -v "$p")" 2>&1 >&2 || echo "$p: not found" >&2
    done
    ls -Z /usr/libexec/tamarack-meta >&2 || :
    echo "=== kernel: md, device-mapper, integrity ===" >&2
    journalctl -b -k --no-pager -o short-precise 2>&1 \
        | grep -Ei 'md[0-9:/]|md_mod|raid|device-mapper|integrity' | tail -60 >&2 || :
    echo "=== md devices ===" >&2
    cat /proc/mdstat >&2 || :
    ls -lZ /dev/md* /sys/module/md_mod/parameters/new_array >&2 2>&1 || :
    return "$status"
}

# Fake drives: one scsi_debug host per drive, each with its own RAM store,
# so they behave as separate disks with their own WWN and serial.
make_drives() {  # count size_mb
    modprobe -r scsi_debug 2>/dev/null || :
    modprobe scsi_debug add_host="$1" num_tgts=1 max_luns=1 per_host_store=1 \
        dev_size_mb="$2" || fail "scsi_debug is not available"
    udevadm settle
    DRIVES=()
    for dev in /sys/block/sd*; do
        [ "$(cat "$dev/device/model" 2>/dev/null | tr -d ' ')" = "scsi_debug" ] || continue
        DRIVES+=("/dev/$(basename "$dev")")
    done
    [ "${#DRIVES[@]}" -eq "$1" ] || fail "expected $1 scsi_debug drives, found ${#DRIVES[@]}"
    echo "drives: ${DRIVES[*]}"
}

# File-backed drives of any size, through the kernel's SCSI target (LIO):
# one sparse file, fileio backstore and loopback (tcm_loop) target per
# drive, so each is its own SCSI disk with its own WWN and serial. Unlike
# scsi_debug the drives can differ in size, and the targets are saved for
# target.service, so they can come back after a reboot.
FILE_DRIVES=/var/tmp/tamarack-drives
# Loopback WWNs made here start with this, so cleanup removes only ours.
LIO_WWN_PREFIX=naa.50014050000

# The block device behind LUN 0 of a loopback target, found by SCSI address.
lio_disk() {  # wwn
    local addr
    addr=$(cat "/sys/kernel/config/target/loopback/$1/tpgt_1/address") \
        || fail "no SCSI address for loopback target $1"
    local dev=/sys/class/scsi_device/$addr:0/device
    [ "$(cat "$dev/vendor" 2>/dev/null | tr -d ' ')" = "LIO-ORG" ] \
        || fail "the disk at $addr:0 is not an LIO disk"
    for blk in "$dev"/block/*; do
        [ -e "$blk" ] && { echo "/dev/$(basename "$blk")"; return; }
    done
    fail "no disk for loopback target $1 at $addr:0"
}

make_file_drives() {  # size_mb...
    command -v targetcli >/dev/null || fail "targetcli is not installed"
    modprobe tcm_loop || fail "tcm_loop is not available"
    install -d -m 0700 "$FILE_DRIVES"
    DRIVES=()
    local i=0 name file wwn dev
    for size in "$@"; do
        i=$((i + 1))
        name="tk-test-$i"
        file="$FILE_DRIVES/$name.img"
        wwn=$(printf '%s%05x' "$LIO_WWN_PREFIX" "$i")
        rm -f "$file"
        truncate -s "${size}M" "$file"
        # write_back=false: writes reach the file before they are
        # acknowledged, as on a disk without a volatile cache.
        targetcli /backstores/fileio create name="$name" file_or_dev="$file" \
            write_back=false >/dev/null || fail "cannot create backstore $name"
        targetcli /loopback create wwn="$wwn" >/dev/null \
            || fail "cannot create loopback target $wwn"
        targetcli "/loopback/$wwn/luns" create "/backstores/fileio/$name" >/dev/null \
            || fail "cannot map $name to $wwn"
        udevadm settle
        dev=$(lio_disk "$wwn")
        DRIVES+=("$dev")
    done
    targetcli saveconfig >/dev/null || fail "targetcli saveconfig failed"
    systemctl enable target.service
    udevadm settle
    echo "drives: ${DRIVES[*]} (sizes MB: $*)"
}

remove_file_drives() {
    local changed=
    for t in /sys/kernel/config/target/loopback/"$LIO_WWN_PREFIX"*; do
        [ -e "$t" ] || continue
        targetcli /loopback delete "$(basename "$t")" >/dev/null || :
        changed=1
    done
    for b in /sys/kernel/config/target/core/fileio_*/tk-test-*; do
        [ -e "$b" ] || continue
        targetcli /backstores/fileio delete "$(basename "$b")" >/dev/null || :
        changed=1
    done
    if [ -n "$changed" ]; then
        targetcli saveconfig >/dev/null || :
        systemctl disable target.service 2>/dev/null || :
    fi
    rm -rf "$FILE_DRIVES"
}

start_engine() {
    [ "$(getenforce)" = "Enforcing" ] || fail "SELinux must be enforcing"
    # Test VMs only, while bringing the policy up (main.fmf sets it): log
    # the denials the policy normally hides (dontaudit), so a failure with
    # no visible AVC still shows its cause. SELinux stays enforcing, and
    # cleanup puts the dontaudit rules back. check_avc then also fails on
    # hidden denials that may be harmless, so judge those by hand.
    if [ "${TAMARACK_SHOW_DONTAUDIT:-}" = yes ]; then semodule -DB; fi
    install -d -m 0755 /etc/tamarack
    printf '[execution]\nenabled = yes\n' > /etc/tamarack/engine.conf
    # Test VMs only: let root use every Tamarack action without a prompt.
    install -D -m 0644 "$HERE/50-tamarack-test.rules" \
        /etc/polkit-1/rules.d/50-tamarack-test.rules
    systemctl restart polkit
    rm -rf /var/lib/tamarack
    systemctl restart tamarack-engine
    START=$(date '+%m/%d/%Y %H:%M:%S')
    ps -eo label,comm | grep -q '^system_u:system_r:tamarack_engine_t:s0 *tamarack-engine' \
        || fail "the engine is not running in its SELinux domain (tamarack_engine_t)"
}

drive() { python3 "$HERE/drive.py" "$@"; }

# A checksum manifest of random files, to prove the data survives each change.
write_data() {
    mkdir -p /srv/tamarack/shares/test
    for i in $(seq 1 20); do
        head -c 4M /dev/urandom > "/srv/tamarack/shares/test/file$i"
    done
    (cd /srv/tamarack/shares/test && sha256sum file* > /var/tmp/tamarack-manifest)
}

check_data() {
    (cd /srv/tamarack/shares/test && sha256sum --quiet -c /var/tmp/tamarack-manifest) \
        || fail "data changed"
}

check_avc() {
    if ausearch -m AVC,USER_AVC -ts "$START" 2>/dev/null | grep -q denied; then
        ausearch -m AVC,USER_AVC -ts "$START" >&2
        fail "SELinux denials"
    fi
}

cleanup() {
    diagnostics || :
    systemctl stop tamarack-engine 2>/dev/null || :
    systemctl disable --now srv-tamarack.mount 2>/dev/null || :
    umount /srv/tamarack /mnt/rescue 2>/dev/null || :
    vgchange -an tamarack 2>/dev/null || :
    mdadm --stop --scan 2>/dev/null || :
    for m in /dev/mapper/tk-* /dev/mapper/rescue-*; do
        if [ -e "$m" ]; then integritysetup close "$(basename "$m")" || :; fi
    done
    # Wipe the test drives, so a job that failed half way (partitions but
    # no layout record) cannot make the engine refuse the next test. Only
    # the drives this test made: scsi_debug and tk-test-* LIO disks.
    for d in "${DRIVES[@]:-}"; do
        [ -b "$d" ] && wipefs --all --force "$d"?* "$d" >/dev/null 2>&1 || :
    done
    udevadm settle
    if lsmod | grep -q '^scsi_debug ' && ! modprobe -r scsi_debug; then
        echo "WARNING: scsi_debug is still in use; the next test may see these drives" >&2
    fi
    remove_file_drives
    semodule -B 2>/dev/null || :
}
