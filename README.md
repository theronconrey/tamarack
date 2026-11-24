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
- **Cockpit page** (`tamarack-cockpit`): the Overview
  (whether your data is protected, the drives, the space and how your data
  is looked after), Drives (each drive's health, temperature and age, and
  the best next upgrade), Shares (create, change and stop sharing; how to
  connect and who you are trusting), Snapshots (the schedule, which you can
  change, and restoring a file or folder next to the original)
  and Activity (what ran and how it ended), in plain words
  ([design](docs/design/cockpit.md)).
- **Testing**: unit and D-Bus tests in Fedora CI on every pull request, and
  Packit Copr builds plus a tmt VM matrix ([`plans/`](plans),
  [`tests/vm/`](tests/vm)) that runs on Testing Farm with SELinux
  enforcing, on three VMs in parallel. The matrix passes (11 tests:
  create, grow, mixed sizes, a pulled drive, silent corruption, an
  interrupted reshape, an online swap, one drive to two, recovery by hand,
  NFS, the Cockpit page, and a reboot). Execution stays for throwaway
  VMs until the box has run on real disks.

## Roadmap

Next: the rest of the Cockpit page (the guided drive changes),
then a Fedora bootc image and
bring-up on the TerraMaster F4-425 Pro. After that, roughly in order:

- automatic checks after an unclean or degraded start, with a plain-words
  list of any damaged files;
- growing only when every drive is healthy, and finishing an interrupted
  reshape first;
- clean shutdown on a UPS, detected automatically;
- spotting a pulled or failing drive straight away, and replacing a failing
  drive while it still reads;
- verified backups built in: btrfs send/receive to a second Tamarack or a USB
  drive, and restic to a cloud bucket, with a scheduled test restore;
- later, folder-level replication (for example to an S3 bucket) and growth
  to five or more drives.

The full design and its status: [docs/design/engine.md](docs/design/engine.md).

## Try it

From a checkout, without installing anything:

```sh
alias tamarack='PYTHONPATH=src python3 -m tamarack'
tamarack plan 4TB 4TB 8TB 8TB
tamarack plan /dev/sda=4TB /dev/sdb=8TB --json
tamarack plan /dev/sda=4TB /dev/sdb=4TB /dev/sdc=8TB /dev/sdd=8TB --commands   # dry run
tamarack grow /dev/sda=4TB --add /dev/sdb=4TB              # add a second drive
tamarack grow /dev/sda=4TB --add /dev/sdb=4TB --commands   # dry run
```

## Adding a second drive

One drive starts as two copies on itself. Adding a second drive of at
least the same size:

1. moves one copy onto the new drive with `mdadm --replace` (the old copy stays
   active until the new one is complete, so protection never drops);
2. pairs the freed half of the first drive with the new drive as a second
   two-copy layer;
3. grows LVM and Btrfs onto it.

Result: survives the loss of either drive, and usable space doubles. Nothing is
shrunk or reshaped in place.

## Growing to three and four drives

The new drive joins existing layers in order while it has room:

- a single-parity layer widens by one drive; a two-drive layer is already
  single parity, so it simply widens (PPL is paused for the reshape and
  switched back on after);
- space that was waiting for a partner (for example the top half of an 8 TB
  drive next to 4 TB drives) pairs with the new drive as a new layer.

Widening is an mdadm reshape: the data stays online and readable, and mdadm
resumes from its checkpoint after a power cut. During the reshape the layer has
neither PPL nor a bitmap, so a power cut while a drive is also missing can
damage data: keep the power on (a UPS helps) and let it finish. Growing to five or more drives
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
you (a two-drive box grown from one drive has two two-copy layers; a fresh one
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
the drive being added or swapped in. Layout files keep them. The engine reads
them from UDisks2 and checks them again before every step.

`--review` prints the plan exactly as it will be approved: every drive and its
role (`erase`, `keep`, `leave`, `failed`), every command as an argument list,
and the warnings, with a SHA-256 digest over all of it. The engine only runs
a plan whose digest matches the one that was reviewed.

```sh
tamarack replace /dev/sda=4TB /dev/sdb=4TB /dev/sdc=4TB \
  --id /dev/sdb=wwn:0x5000c500a1b2c3d4 \
  --old /dev/sdb --with /dev/sdb=4TB --failed --new-id serial:ZL2ABC --review
```

## How the layout is chosen

Protection is picked from the drive count and only ever moves up:

| Drives | Protection |
| --- | --- |
| 1 | Two copies on that drive, in two partitions (bad sectors, not drive loss) + warning |
| 2 | Survives one drive failure (two copies) |
| 3–4 | Survives one drive failure |
| 5+ | Survives two drive failures |

Drives are cut into layers at each size step, SHR / BeyondRAID style. Example,
4 + 4 + 8 + 8 TB:

| Layer | Drives | Slice | Protection | Usable |
| --- | --- | --- | --- | --- |
| 1 | all 4 | 4 TB | single parity (RAID5) | 12 TB |
| 2 | the two 8 TB | 4 TB | single parity on two drives (two copies) | 4 TB |

A slice that exists on only one drive stays unallocated until a matching drive
arrives. Drives within 1 GiB or 0.5% of each other count as equal.

Per layer, under the hood:

- every partition gets **dm-integrity** (crc32c, journal mode), so a corrupted
  block reads as an error and mdadm repairs it from the other copy or parity;
- layers with one-drive protection are RAID5 from the start, even on two
  drives (where parity is a plain copy, so it holds what a mirror would), and
  use **PPL** to close the write hole: after a power cut, even with a drive
  missing, md replays the log and the pool starts on its own. A mirror can't
  keep PPL once it becomes parity (mdadm has no room for it next to the bad
  block log), which is why two-drive layers are not mirrors. RAID6 layers use
  an internal write-intent bitmap;
- **LVM** joins the layers into one volume; **Btrfs** sits on top with
  metadata DUP, zstd compression and snapshots.

## Your data never depends on this project

Everything on disk is standard mdadm, LVM and Btrfs. If this software
disappears, any Linux system can open the drives with `integritysetup open`,
`mdadm --assemble --scan`, `vgchange -ay` and `mount`. The steps are in
[docs/recovery.md](docs/recovery.md), and a VM test does exactly that on every
pull request.

## The engine

The engine is the part that changes disks: a system service,
`tamarack-engine.service`, on the D-Bus system bus as
`com.borealislogistics.Tamarack1`. A client (a Cockpit page, later the CLI)
works with it in four steps:

1. **Plan.** `PlanCreate`, `PlanAdd`, `PlanReplace` or `PlanRebuild` returns
   a Plan object with its steps, warnings, the drives it erases and a digest.
2. **Review.** The person reads the plan and types the drives to be erased.
3. **Execute.** `Plan.Execute(digest, {"confirm-erase": [...]})`. polkit asks
   for the admin password, then the engine checks again that nothing changed:
   the drives, degraded arrays, battery, UPS, a backup confirmed in the last
   7 days. Then it runs the plan as a Job.
4. **Follow.** The Job reports each step. After a power cut it picks up where
   its journal says it stopped, and anything it can't verify waits for a
   person.

Every drive also carries the layout on its `tk-meta` partition; the engine
reads it back on start and refuses to plan if the drives disagree.

From a terminal, `tamarack engine` is that client:

```sh
tamarack engine status                        # the pool, its health, any job
tamarack engine add /dev/disk/by-id/ata-… --dry-run --commands   # review only
tamarack engine confirm-backup                # needed before add, replace, rebuild
tamarack engine add /dev/disk/by-id/ata-…     # type the drive to confirm, then
                                              # polkit asks for the admin password
tamarack engine resume 3 [--done|--rerun]     # after a pause or a power cut
tamarack engine snapshots --keep 24 7 4 6     # hourly daily weekly monthly; or --off
tamarack engine share set media --client 192.168.1.0/24 --read-only   # NFSv4 share
```

It shows the plan and its digest, asks you to type every drive it will
erase, and follows the job step by step. A text polkit agent is started for
the password prompt when you run it from a terminal, as `systemctl` does.

The design, with the reasoning and the status of each part:
[docs/design/engine.md](docs/design/engine.md). The interface:
[`src/tamarack/com.borealislogistics.Tamarack1.xml`](src/tamarack/com.borealislogistics.Tamarack1.xml).
Unit, bus and polkit policy: [`data/`](data/). SELinux policy:
[`selinux/`](selinux/).

## Develop

All contributions follow Fedora best practices; see
[CONTRIBUTING.md](CONTRIBUTING.md).


```sh
sudo dnf install python3-pytest python3-mypy ruff tmt python3-gobject-base dbus-daemon
python3 -m pytest
ruff check . && ruff format --check .
mypy
tmt lint
```

Without `python3-gobject-base` and `dbus-daemon` the engine's D-Bus tests
are skipped; CI requires them. The VM matrix runs on Testing Farm through
Packit; `tmt run` runs it locally in a VM it provisions. To poke at the
engine by hand on your session bus:

```sh
python3 -m tamarack.service --session --state-dir /tmp/tamarack-state &
busctl --user introspect com.borealislogistics.Tamarack1 /com/borealislogistics/Tamarack1
```

On the session bus it only plans: execution is never enabled there, and
polkit isn't consulted, so only reading is allowed. It still looks drives up
through UDisks2 on the system bus.

## Packaging

Tamarack ships as an RPM built from [`tamarack.spec`](tamarack.spec) under the
Fedora Python Packaging Guidelines. Every pull request is tested in Fedora
containers (latest and rawhide): code checks, RPM build, rpmlint, and an
install of the built packages with `systemd-analyze verify` and the SELinux
module loaded. Packit ([`.packit.yaml`](.packit.yaml)) builds every pull
request in Copr for every supported Fedora release and runs the VM matrix on
the result.

Three packages come out of it:

- `tamarack`: the planner and the `tamarack` command. Needs nothing but Python.
- `tamarack-engine`: the sandboxed system service, its D-Bus and polkit
  policy, and the storage tools it runs. It plans and reviews out of the box
  but **does not execute** anything unless `/etc/tamarack/engine.conf` says:

  ```ini
  [execution]
  enabled = yes
  ```

  Until the VM test matrix passes, only turn that on in a throwaway VM.
- `tamarack-selinux`: the engine's SELinux policy module, pulled in by
  `tamarack-engine` wherever the targeted policy is installed.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE).
