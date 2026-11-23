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

start_engine() {
    [ "$(getenforce)" = "Enforcing" ] || fail "SELinux must be enforcing"
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
        [ -e "$m" ] && integritysetup close "$(basename "$m")"
    done
    modprobe -r scsi_debug 2>/dev/null || :
}
