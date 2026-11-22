# Tamarack

Drobo-style storage for mixed-size drives, built entirely from stock Linux tools:
**dm-integrity → mdadm → LVM → Btrfs**. You never pick a RAID level; the planner
decides from the drives you insert.

> Named for the tamarack, the northern Minnesota conifer that drops its needles
> every fall and grows them back each spring. A Borealis project.

Command: `tamarack`. Python package: `tamarack-nas`.

## Status

Phase 1: **the planner** and **grow** from one drive up to four. Both are pure
functions with dry-run command output. Nothing in this repository
writes to a disk yet.

## Try it

```sh
# On Fedora: sudo dnf install python3-pytest python3-mypy ruff
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
shrunk or reshaped in place.

## Growing to three and four drives

The new drive joins existing layers in order while it has room:

- a two-drive mirror converts to single parity in place, then widens onto the
  new drive;
- a single-parity layer widens by one drive (PPL is paused for the reshape and
  switched back on after);
- space that was waiting for a partner (for example the top half of an 8 TB
  drive next to 4 TB drives) pairs with the new drive as a new layer.

Widening is an mdadm reshape: the data stays online and protected, and mdadm
resumes from its checkpoint after a power cut. Growing to five or more drives
(two-drive protection) is not supported yet.

## Layout files

A layout records what was actually built, which drive sizes alone can't tell
you (a two-drive box grown from one drive has two mirror layers; a fresh one
has one). Grow reads one and can write the next:

```sh
tamarack plan /dev/sda=4TB --layout > 1.json
tamarack grow --from 1.json --add /dev/sdb=4TB --layout-out 2.json
tamarack grow --from 2.json --add /dev/sdc=4TB --commands
```

The same format will be written to every `tk-meta` partition.

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

All contributions follow Fedora best practices; see
[CONTRIBUTING.md](CONTRIBUTING.md).


```sh
python3 -m pytest
ruff check . && ruff format --check .
mypy
```

## Packaging

Tamarack ships as an RPM built from [`tamarack.spec`](tamarack.spec) under the
Fedora Python Packaging Guidelines. Every push and pull request is tested in a
Fedora container: code checks, RPM build, rpmlint, and an install of the built
package. Packit Copr builds for every supported Fedora release are configured
in [`.packit.yaml`](.packit.yaml).

## License

GPL-3.0-or-later; see [LICENSE](LICENSE).
