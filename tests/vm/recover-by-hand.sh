#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Your data never depends on Tamarack: with the engine gone, stock Fedora
# tools alone bring the volume back. These are the steps a person would
# type; docs/recovery.md describes them.
. "$(dirname "$0")/lib.sh"
trap cleanup EXIT

make_drives 3 3072
start_engine
d=("${DRIVES[@]}")

drive create "${d[0]}" "${d[1]}" "${d[2]}"
write_data

# Take everything down, as if the box had died and Tamarack were gone.
systemctl stop tamarack-engine
systemctl disable --now srv-tamarack.mount
vgchange -an tamarack
for md in /dev/md/tk-l*; do mdadm --stop "$md"; done
for m in /dev/mapper/tk-*; do integritysetup close "$(basename "$m")"; done

# By hand, with nothing but cryptsetup, mdadm, lvm2 and mount.
lsblk -nrpo NAME,PARTLABEL | awk '$2 ~ /^tk-l/ {print $1}' | while read -r part; do
    integritysetup open --integrity crc32c "$part" "rescue-$(basename "$part")"
done
# udev may assemble them first (Fedora's incremental rules); then --scan
# finds nothing left to do and exits 1, which is fine.
udevadm settle
mdadm --assemble --scan || :
for md in /dev/md/tk-l*; do
    [ -e "$md" ] || fail "md arrays not assembled"
    mdadm --readwrite "$md" 2>/dev/null || :
done
vgchange -ay tamarack
mkdir -p /mnt/rescue
mount -o ro /dev/tamarack/data /mnt/rescue
(cd /mnt/rescue/shares/test && sha256sum --quiet -c /var/tmp/tamarack-manifest) \
    || fail "data not recovered by hand"
umount /mnt/rescue
