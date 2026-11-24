<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Recovering a Tamarack volume by hand

Everything Tamarack puts on a drive is standard: GPT partitions,
dm-integrity, mdadm, LVM and Btrfs. If Tamarack is gone, or the box that ran
it has died, any Linux system with those tools can bring the volume back.
On Fedora they come from `cryptsetup`, `mdadm`, `lvm2` and `btrfs-progs`.

This is what the `recover-by-hand` VM test does, step by step.

1. **Connect all the drives** you can. With one-drive protection the volume
   still opens with one drive missing.

2. **Open dm-integrity on every layer partition.** Tamarack's layer
   partitions are labelled `tk-l1`, `tk-l2` and so on (partition 1 on each
   drive, `tk-meta`, holds the layout record and is not part of the volume).
   The names you give the mappings don't matter:

   ```sh
   lsblk -nrpo NAME,PARTLABEL | awk '$2 ~ /^tk-l/ {print $1}' | while read -r part; do
       sudo integritysetup open --integrity crc32c "$part" "rescue-$(basename "$part")"
   done
   ```

3. **Assemble the md arrays.** mdadm finds them by their superblocks. Fedora
   may already have assembled them as the mappings appeared; then this says
   "No arrays found" and there is nothing more to do. Check that
   `/dev/md/tk-l1` (and any further layers) exist:

   ```sh
   sudo mdadm --assemble --scan
   ls /dev/md/
   ```

4. **Activate the volume group and mount**, read-only to be safe:

   ```sh
   sudo vgchange -ay tamarack
   sudo mkdir -p /mnt/rescue
   sudo mount -o ro /dev/tamarack/data /mnt/rescue
   ```

Your files are under `/mnt/rescue/shares`, and snapper's snapshots of them under
`/mnt/rescue/shares/.snapshots/N/snapshot` (one numbered folder per snapshot).

To see how the volume was laid out, read the record on any drive's first
partition: it starts with a `TAMARACK-META` line, followed by the layout as
JSON.

```sh
sudo head -c 4096 /dev/sdX1 | head -2
```

## Which kernel to recover with

Arrays made by recent kernels record their logical block size, and the kernel
warns that they "will not be assembled in old kernels" (6.18 and older). Use
a rescue system with a kernel at least as new as the box's: a current Fedora
live image works.
