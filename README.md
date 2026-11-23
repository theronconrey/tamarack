# Tamarack

Drobo-style storage for mixed-size drives, built entirely from stock Linux tools:
**dm-integrity → mdadm → LVM → Btrfs**. You never pick a RAID level; the planner
decides from the drives you insert.

> Named for the tamarack, the northern Minnesota conifer that drops its needles
> every fall and grows them back each spring. A Borealis project.

Command: `tamarack`. Python package: `tamarack-nas`.

## Status

Pre-alpha. Not for drives holding data you care about.

- **Planner**: the `tamarack` command plans a fresh layout, growing from one
  drive up to four, and replacing a drive (upgrade or failed). It only
  prints: layouts, layout files and dry-run commands.
- **Engine**: a sandboxed, SELinux-confined system service on D-Bus. It
  plans, has polkit authorize each change, checks safety (drive identity,
  degraded arrays, UPS, recent backup), and runs approved plans step by step
  with a power-cut-safe journal. **Execution is off** unless
  `/etc/tamarack/engine.conf` turns it on (see [Packaging](#packaging)).
- **Testing**: unit and D-Bus tests in Fedora CI on every pull request; a
  tmt VM matrix ([`plans/`](plans), [`tests/vm/`](tests/vm)) that runs with
  SELinux enforcing on Testing Farm once the Packit app is installed.
  Execution stays for throwaway VMs until that matrix passes.

The full design and its status: [docs/design/engine.md](docs/design/engine.md).

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

## Replacing a drive

```sh
tamarack replace --from now.json --old /dev/sda --with /dev/sde=8TB            # upgrade
tamarack replace --from now.json --old /dev/sdb --with /dev/sdb=4TB --failed   # failure
```

- **Upgrade** (old drive still works): `mdadm --replace` copies each of its
  slices to the new drive while the old copy stays active, so protection never
  drops. Then the old drive is retired and can be pulled.
- **Failed drive**: the failed slices are removed and rebuilt onto the new
  drive from the others. Protection is reduced until the rebuild finishes.
- The new drive must hold everything the old one held; Tamarack says the
  minimum size if it doesn't.
- Extra space goes to work automatically: the drive joins layers it wasn't in,
  and leftover space pairs with free space on other drives. Upgrading every
  drive of a 4 × 4 TB box to 8 TB, one at a time, ends at the same 24 TB a
  fresh 4 × 8 TB build has.
- A single drive that fails can't be rebuilt (both copies were on it):
  restore from backup.

## Layout files

A layout records what was actually built, which drive sizes alone can't tell
you (a two-drive box grown from one drive has two mirror layers; a fresh one
has one). Grow reads one and can write the next:

```sh
tamarack plan /dev/sda=4TB --layout > 1.json
tamarack grow --from 1.json --add /dev/sdb=4TB --layout-out 2.json
tamarack grow --from 2.json --add /dev/sdc=4TB --commands
```

Every plan ends by writing the same layout, framed with a checksum, to the
small `tk-meta` partition at the start of each drive, so the box can be read
from any one of its drives.

## Drive identities and review

Paths like `/dev/sdb` can point at a different drive after a reboot, so plans
can record each drive's identity: `wwn:` plus its World Wide Name, or
`serial:` plus its serial number. The engine will read these from UDisks2; on
the command line, give them with `--id` for listed drives and `--new-id` for
the drive being added or swapped in. Layout files keep them.

`--review` prints the plan exactly as it will be approved: every drive and its
role (`erase`, `keep`, `leave`, `failed`), every command as an argument list,
and the warnings, with a SHA-256 digest over all of it. The engine will only
run a plan whose digest matches the one that was reviewed.

```sh
tamarack replace /dev/sda=4TB /dev/sdb=4TB /dev/sdc=4TB \
  --id /dev/sdb=wwn:0x5000c500a1b2c3d4 \
  --old /dev/sdb --with /dev/sdb=4TB --failed --new-id serial:ZL2ABC --review
```

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

## Design

The storage engine (the part that will change disks) is designed in
[docs/design/engine.md](docs/design/engine.md), with draft D-Bus, polkit and
systemd files under [`data/`](data/).

## Develop

All contributions follow Fedora best practices; see
[CONTRIBUTING.md](CONTRIBUTING.md).


```sh
python3 -m pytest
ruff check . && ruff format --check .
mypy
```

The engine's D-Bus tests need `python3-gobject-base` and `dbus-daemon`
(`sudo dnf install python3-gobject-base dbus-daemon`); without them they are
skipped. To poke at the read-only engine by hand on your session bus:

```sh
python3 -m tamarack.service --session --state-dir /tmp/tamarack-state &
busctl --user introspect com.borealislogistics.Tamarack1 /com/borealislogistics/Tamarack1
```

It still looks drives up through UDisks2 on the system bus, and it never runs
a command.

## Packaging

Tamarack ships as an RPM built from [`tamarack.spec`](tamarack.spec) under the
Fedora Python Packaging Guidelines. Every push and pull request is tested in a
Fedora container: code checks, RPM build, rpmlint, and an install of the built
package. Packit Copr builds for every supported Fedora release are configured
in [`.packit.yaml`](.packit.yaml).

Two packages come out of it:

- `tamarack`: the planner and the `tamarack` command. Needs nothing but Python.
- `tamarack-engine`: the sandboxed system service, its D-Bus and polkit
  policy, and the storage tools it runs. It plans and reviews out of the box
  but **does not execute** anything unless `/etc/tamarack/engine.conf` says:

  ```ini
  [execution]
  enabled = yes
  ```

  Until the VM test matrix passes, only turn that on in a throwaway VM.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE).
