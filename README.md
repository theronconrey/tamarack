# Tamarack

Drobo-style storage for mixed-size drives, built entirely from stock Linux tools:
**dm-integrity → mdadm → LVM → Btrfs**. You never pick a RAID level; the planner
decides from the drives you insert.

> Named for the tamarack, the northern Minnesota conifer that drops its needles
> every fall and grows them back each spring. A Borealis project.

Command: `tamarack`. Python package: `tamarack-nas`.

## Status

Phase 1: **the planner** and the first **grow** path (one drive to two). Both
are pure functions with dry-run command output. Nothing in this repository
writes to a disk yet.

## Try it

```sh
python3 -m pip install -e '.[dev]'
tamarack plan 4TB 4TB 8TB 8TB
tamarack plan /dev/sda=4TB /dev/sdb=8TB --json
tamarack plan /dev/sda=4TB /dev/sdb=4TB /dev/sdc=8TB /dev/sdd=8TB --commands   # dry run
tamarack grow /dev/sda=4TB --add /dev/sdb=4TB              # add a second drive
tamarack grow /dev/sda=4TB --add /dev/sdb=4TB --commands   # dry run
```

## Adding a second drive

One drive starts as two mirrored copies on itself. Adding a second drive of at
least the same size:

1. moves one copy onto the new drive with `mdadm --replace` (the old copy stays
   active until the new one is complete, so protection never drops);
2. pairs the freed half of the first drive with the new drive as a second
   mirror layer;
3. grows LVM and Btrfs onto it.

Result: survives the loss of either drive, and usable space doubles. Nothing is
shrunk or reshaped in place. Other transitions (2 to 3 drives and up) come next.

Without installing: `PYTHONPATH=src python3 -m tamarack plan 4TB 8TB`.

## How the layout is chosen

Protection is picked from the drive count and only ever moves up:

| Drives | Protection |
| --- | --- |
| 1 | Two partitions mirrored on that drive (bad sectors, not drive loss) + warning |
| 2 | Mirror |
| 3–4 | Survives one drive failure |
| 5+ | Survives two drive failures |

Drives are cut into layers at each size step, SHR / BeyondRAID style. Example,
4 + 4 + 8 + 8 TB:

| Layer | Drives | Slice | Protection | Usable |
| --- | --- | --- | --- | --- |
| 1 | all 4 | 4 TB | single parity (RAID5) | 12 TB |
| 2 | the two 8 TB | 4 TB | mirror (RAID1) | 4 TB |

A slice that exists on only one drive stays unallocated until a matching drive
arrives. Drives within 1 GiB or 0.5% of each other count as equal.

Per layer, under the hood:

- every partition gets **dm-integrity** (crc32c, journal mode), so a corrupted
  block reads as an error and mdadm repairs it from the other copy or parity;
- RAID5 layers use **PPL** to close the write hole; mirror and RAID6 layers use
  an internal write-intent bitmap;
- **LVM** joins the layers into one volume; **Btrfs** sits on top with
  metadata DUP, zstd compression and snapshots.

## Your data never depends on this project

Everything on disk is standard mdadm, LVM and Btrfs. If this software
disappears, any Linux system can open the drives with `integritysetup open`,
`mdadm --assemble --scan`, `vgchange -ay` and `mount`.

## Develop

```sh
python3 -m pytest
ruff check . && ruff format --check .
mypy
```

## License

GPL-3.0-or-later. Add the official license text from
<https://www.gnu.org/licenses/gpl-3.0.txt> as `LICENSE` before publishing.
