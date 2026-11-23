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
    echo "=== SELinux denials (this boot, transition noise left out) ===" >&2
    ausearch -m AVC,USER_AVC,SELINUX_ERR -ts boot 2>/dev/null \
        | grep -Ev "$TRANSITION_NOISE" | grep -E 'denied|SELINUX_ERR' | head -100 >&2 || :
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
        | grep -Ei '\bmd[0-9]*:|md_mod|raid[0-9]|device-mapper|dm-integrity' | tail -60 >&2 || :
    echo "=== md devices ===" >&2
    cat /proc/mdstat >&2 || :
    lsmod | grep -E '^(raid1|raid456|dm_integrity|md_mod) ' >&2 || echo "md/dm modules: none loaded" >&2
    ls -lZ /dev/md* /sys/module/md_mod/parameters/new_array >&2 2>&1 || :
    echo "=== failed system calls by mdadm ===" >&2
    auditctl -l 2>&1 | grep tamarack-md >&2 || echo "no tamarack-md audit rule loaded" >&2
    ausearch -k tamarack-md -i 2>/dev/null | grep -E 'type=SYSCALL|type=PATH' \
        | tail -40 >&2 || :
    echo "=== the same mdadm --create, outside the engine's sandbox ===" >&2
    local members
    mapfile -t members < <(ls /dev/mapper/tk-d* 2>/dev/null | head -2)
    if [ "${#members[@]}" -eq 2 ] && [ ! -e /dev/md/tk-l1 ]; then
        mdadm --create /dev/md/tk-probe --run --metadata=1.2 --level=1 --raid-devices=2 \
            --bitmap=internal "${members[@]}" >&2 2>&1 && echo "outside the sandbox: created" >&2
        mdadm --stop /dev/md/tk-probe >/dev/null 2>&1 || :
    fi
    sandbox_probe
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

# Can a new md array be made from inside the engine's sandbox? Compares
# writing md_mod's new_array from the host and from the engine's mount
# namespace, and shows how the engine sees /sys and /dev.
sandbox_probe() {
    echo "=== engine sandbox probe ===" >&2
    systemctl start tamarack-engine 2>/dev/null || :
    local pid new=/sys/module/md_mod/parameters/new_array
    pid=$(systemctl show -p MainPID --value tamarack-engine)
    [ "${pid:-0}" != 0 ] || { echo "engine not running" >&2; return 0; }
    grep -E ' /(sys|dev)( |/(module|mapper|md)?)' "/proc/$pid/mountinfo" | head -20 >&2 || :
    grep -E '^(Cap(Eff|Bnd)|NoNewPrivs|Seccomp):' "/proc/$pid/status" >&2 || :
    systemctl show tamarack-engine -p DevicePolicy -p DeviceAllow >&2 || :
    local out
    out=$(sh -c "echo md120 > $new" 2>&1) && out="ok $out"
    echo "host new_array: $out" >&2
    out=$(nsenter -t "$pid" -m -- sh -c "echo md121 > $new" 2>&1) && out="ok $out"
    echo "engine namespace new_array: $out" >&2
    cat /proc/mdstat >&2
    ls -l /dev/md12[01] >&2 2>&1 || :
    mdadm --stop /dev/md120 /dev/md121 >/dev/null 2>&1 || :
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
    # Record every failed system call made in mdadm's domain, with its
    # error number: mdadm --create fails with no AVC and no kernel message.
    auditctl -D -k tamarack-md >/dev/null 2>&1 || :
    auditctl -a always,exit -F arch=b64 -S all -F exe="$(readlink -f "$(command -v mdadm)")" \
        -F success=0 -k tamarack-md >&2 || echo "auditctl: could not add the mdadm rule" >&2
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

# siginh, rlimitinh and noatsecure are checked on every domain transition
# and dontaudit'ed by Fedora's policy; they only show if dontaudit is off.
TRANSITION_NOISE='denied +\{ (siginh|rlimitinh|noatsecure) \}'

# Denials by Fedora's own domains, not Tamarack's, that do not stop
# anything working. Listed one by one, each to be raised with
# selinux-policy; any other denial still fails the test.
#  - snapperd asks systemd to reload when a config is created.
#  - mdadm checks /dev/kmsg, which these VMs label init_var_run_t.
FEDORA_POLICY_GAPS='scontext=system_u:system_r:snapperd_t:s0 tcontext=system_u:system_r:init_t:s0 tclass=system|denied +\{ getattr \} for .*comm="mdadm" path="/dev/kmsg"'

check_avc() {
    local denials
    denials=$(ausearch -m AVC,USER_AVC -ts "$START" 2>/dev/null | grep denied \
        | grep -Ev "$TRANSITION_NOISE" | grep -Ev "$FEDORA_POLICY_GAPS") || :
    if [ -n "$denials" ]; then
        echo "$denials" >&2
        fail "SELinux denials"
    fi
}

cleanup() {
    diagnostics || :
    systemctl stop tamarack-engine 2>/dev/null || :
    systemctl disable --now srv-tamarack.mount 2>/dev/null || :
    umount /srv/tamarack /mnt/rescue 2>/dev/null || :
    # Take the stack down from the top. The VG may not be in the host's LVM
    # devices file, so remove its dm devices directly as well.
    vgchange -an tamarack 2>/dev/null || :
    for m in /dev/mapper/tamarack-*; do
        if [ -e "$m" ]; then dmsetup remove --retry "$(basename "$m")" || :; fi
    done
    for md in /dev/md/tk-* /dev/md/rescue* /dev/md12[0-7]; do
        if [ -e "$md" ]; then mdadm --stop "$md" >/dev/null 2>&1 || :; fi
    done
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
        dmsetup ls >&2 || :
        cat /proc/mdstat >&2 || :
        lsblk -o NAME,TYPE,SIZE,MOUNTPOINTS >&2 || :
    fi
    remove_file_drives
    # The snapper config outlives the volume; the next pool makes a new one.
    rm -f /etc/snapper/configs/tamarack
    if [ -f /etc/sysconfig/snapper ]; then
        sed -i -E 's/^(SNAPPER_CONFIGS="[^"]*)\btamarack\b ?/\1/' /etc/sysconfig/snapper
    fi
    auditctl -D -k tamarack-md >/dev/null 2>&1 || :
}
