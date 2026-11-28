<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Tamarack storage engine: design

Status: **implemented through step 7 of the order in section 11 and roadmap
tracks 2 to 4** (health, snapshots, NFS shares), with the VM matrix passing.
Execution stays off by default until the reboot test passes and the box has
run on real disks. Each section ends with a status note saying what was
built and what changed from the plan.

The planners (`plan`, `grow`, `replace`) decide *what* to do and print it.
The engine is the only part of Tamarack that *does* it: change partitions,
dm-integrity mappings, md arrays, LVM and Btrfs. This document fixed its
shape before that code existed, following CONTRIBUTING.md: written for
Fedora, least privilege, SELinux enforcing, nothing destructive without
polkit authorization and a dry run the user has seen.

## 1. Goals and non-goals

**Goals**

- Execute a plan the user has reviewed, exactly as reviewed.
- Survive interruption: a reboot or power cut mid-change resumes safely.
- Refuse unsafe changes (degraded arrays, on battery, no recent backup).
- Give the web UI and CLI the same API, authorized by polkit.
- Run confined: systemd sandboxing plus its own SELinux domain.

**Non-goals for v1**

- Network-facing APIs (Cockpit is the remote path; the REST gateway is v2).
- Running anything without a plan (no free-form commands over D-Bus).
- Two-drive protection layouts (5+ drives), which the planners don't
  produce yet.

## 2. Components

| Component | Runs as | Talks to | Notes |
| --- | --- | --- | --- |
| `tamarack-engine.service` | root, sandboxed, `tamarack_engine_t` | D-Bus system bus, UDisks2, UPower, block devices | The only privileged part. D-Bus activated. |
| `tamarack-meta` | root, `tamarack_meta_t` | the `tk-meta` partitions | Run by the engine; the only code with raw-disk access. |
| `tamarack` CLI | the calling user | (later) engine over D-Bus | Plans locally today; executing through the engine is to come. |
| Cockpit plugin (later) | the Cockpit session user | engine over D-Bus | Cockpit handles login, TLS, sessions. |
| `tamarack-metrics.service` (later) | `DynamicUser=` | engine over D-Bus (read only) | Prometheus endpoint. |
| polkit | system | engine asks it | Decides every privileged call. |

## 3. D-Bus API

Bus name **`io.github.theronconrey.Tamarack1`** on the system bus: the
reversed name of the project's GitHub home, github.com/theronconrey, as the
D-Bus specification recommends for projects hosted there. It was
`com.borealislogistics.Tamarack1` until Nov 25, 2026, renamed before the
first release so that the name stays the project's for good. The trailing
`1` is the API version, as in `org.freedesktop.systemd1`: an incompatible
change gets `Tamarack2` alongside, so clients of `Tamarack1` keep working. Object paths and polkit action IDs use
the same prefix (`/io/github/theronconrey/Tamarack1`,
`io.github.theronconrey.tamarack.*`). The full interface is in
[`src/tamarack/io.github.theronconrey.Tamarack1.xml`](../../src/tamarack/io.github.theronconrey.Tamarack1.xml) (inside the package, so the engine loads the same file it is documented by).

Objects:

| Path | Interface | What it is |
| --- | --- | --- |
| `/io/github/theronconrey/Tamarack1` | `…Tamarack1.Manager` | The box: layout, health, plan factory |
| `/io/github/theronconrey/Tamarack1/Plans/N` | `…Tamarack1.Plan` | One reviewed-but-not-run change |
| `/io/github/theronconrey/Tamarack1/Jobs/N` | `…Tamarack1.Job` | One change being run |

### Flow: plan, review, execute

1. A client calls `Manager.PlanAdd(device)` (or `PlanCreate`, `PlanReplace`,
   `PlanRebuild`). The engine resolves the device through UDisks2, runs the
   same planner code the CLI uses, and exports a **Plan** object. Planning is
   read-only and needs only the `read` action.
2. The client shows the user the plan's `Summary`, `Steps` and `Warnings`,
   and the `Erases` list.
3. The user confirms by **typing the devices to be erased**, as the project
   plan requires. The client calls `Plan.Execute(digest, options)` with the
   plan's `Digest` and the typed list in `options["confirm-erase"]`.
4. polkit checks the action for that kind of plan (section 4). If allowed and
   every safety check passes (section 5), the engine creates a **Job** and
   starts the first step.

**The digest binds review to execution.** `Digest` is SHA-256 over the plan's
canonical JSON: the layout before and after, every step and every command,
and the identities of the drives involved. `Execute` refuses if the digest
the client sends differs, if the plan has expired (30 minutes), or if the
layout changed since the plan was made. What runs is byte-for-byte what was
shown.

### Drive identity

Device names like `/dev/sdb` can change between boots. Plans record each
drive's **WWN or serial** (from UDisks2) alongside its path. Before every
step the engine re-resolves identities to paths and refuses if a drive is
missing or a path now points at a different drive. Commands use
`/dev/disk/by-id/` paths, which the planners already support.

Status: the planners carry identities, and `tamarack.review` builds the
review document and digest (step 2). Each drive in the document has a role:
`erase` (new, wiped), `keep`, `leave` (a working drive being replaced, needed
until it is retired) or `failed` (not expected). `identity.verify` checks
every drive but the failed ones against what UDisks2 reports; plan time and
expiry stay outside the digest, so the same inputs always give the same one.

NVMe drives leave UDisks2's `Drive.WWN` empty, and their serial is the
controller's, shared by every namespace on it, so their identity comes from
the namespace (`NVMe.Namespace`: WWN, then NGUID, then EUI-64, else the
controller serial plus the namespace number).

### Which drives may join

A drive never joins on its own; the owner picks, and the engine classifies
every drive (`udisks.classify_drives`, the `Candidates` property). Planning
refuses a refused drive with the reason in words and puts a warned drive's
warnings at the top of the plan the owner reviews. Decided Nov 24, 2026,
after research on other hardware (project notes).

- **Refused:** the operating system's drives (`tamarack.system` follows the
  system's mount points and swap down through device-mapper, md, partitions
  and every device of a btrfs root, and compares device numbers); USB, SD or
  eMMC and FireWire drives (always); RAID-controller logical volumes and
  firmware RAID members; host-managed SMR drives; sectors other than 512 or
  4096 bytes; no identity, or an identity two drives report; SMART failing;
  a drive that holds a Tamarack pool (an import flow comes later); a drive
  something is using.
- **Warned:** existing data, described ("2.00 TB NTFS partition 'Photos'");
  no SMART data; about five years or more of use.
- **Suggested:** everything else.

Not yet: recording the OS drive at install (the bootc image), a list of
drive-managed SMR models, and naming bays.

### Drives coming and going

The engine follows UDisks2's `InterfacesAdded` and `InterfacesRemoved`
signals. A drive arrives as a burst of objects, so it waits 1.5 seconds
after the last one, then classifies every drive again: `Candidates`
changes, Health is checked at once (a pulled pool drive shows as missing
straight away, not at the next minute's check), and each drive that is new
and not in the pool is announced with the `DriveAppeared(device,
candidate)` signal. Cockpit turns that into "A new 8 TB drive is ready"
(or, with a drive missing, "rebuild onto it"). Nothing is written to a new
drive until the owner reviews a plan for it.

**A pool drive that comes back** (re-seated, a loose cable, late at boot)
is put back without asking, as a journaled job that erases nothing
(decided Nov 24, 2026; `tamarack.readd`). After drives settle, and once at
start, the engine looks for a pool drive that is attached again (found by
identity, so a new `/dev` name doesn't matter) while md lacks its members in
degraded layers (from `/sys/block/mdN/md/dev-*`). The job clears the stale
md members and integrity mappings, opens the drive's mappings again (never
formats), `mdadm --add`s each member back and waits: without a bitmap (PPL)
md copies that member's part of every stripe from the other drives, so
nothing the drive held is trusted until rewritten. A new path updates the
layout and the tk-meta records. Only with execution on, no other job, and
not on battery; the drive's identity is checked before every step as in any
job. The VM test `returning` pulls a drive from the running pool and finds
it again with a SCSI rescan.

### Status (step 4)

`tamarack.service` serves the Manager and Plan objects with GDBus from
PyGObject (`python3-gobject-base`), registering them straight from the
shipped XML so the documented interface is the served one. The engine core
(`tamarack.engine`) has no D-Bus code and is tested on its own; the service
is tested on a private `dbus-daemon` with a fake UDisks2 beside it, in CI
and in the RPM's `%check`.

- Drives are looked up in one UDisks2 `GetManagedObjects` call. A drive to
  be erased must be a whole, writable, physical drive with a WWN or serial,
  and neither it nor any partition on it may be mounted, active swap, an md
  member, an unlocked encrypted volume or an LVM physical volume. This is
  what keeps the boot drive out of every plan.
- The current layout is read from `/var/lib/tamarack/layout.json` until
  the `tk-meta` partitions are written. If that file exists but can't be
  read, every `Plan*` call fails with `LayoutUnreadable` rather than
  treating the box as empty.
- Planning is refused with `Busy` while any job journal lacks a `job-end`.
- `Degraded` comes from sysfs (`/sys/block/mdN/md/degraded` behind
  `/dev/md/tk-lN`); a layer whose array is not assembled counts as degraded.
  `OnBattery` comes from UPower and is false when UPower is not running.
- At most 16 plans wait at once; each expires 30 minutes after it is made
  and its object is removed.
- `Execute` and `ConfirmBackup` answered `NotSupported`, and no Job
  objects were exported, until step 5.

## 4. polkit actions

Defined in
[`data/polkit/io.github.theronconrey.tamarack.policy`](../../data/polkit/io.github.theronconrey.tamarack.policy).

| Action | Used by | Default (any / inactive / active) |
| --- | --- | --- |
| `…tamarack.read` | properties, `Plan*` methods | yes / yes / yes |
| `…tamarack.create` | `Execute` of a create plan (erases all listed drives) | no / no / `auth_admin` |
| `…tamarack.add-drive` | `Execute` of an add plan | no / no / `auth_admin` |
| `…tamarack.replace-drive` | `Execute` of a replace or rebuild plan | no / no / `auth_admin` |
| `…tamarack.resume` | `Job.Resume` after an interruption | no / no / `auth_admin` |
| `…tamarack.cancel` | `Job.Cancel` | no / no / `auth_admin_keep` |
| `…tamarack.confirm-backup` | `Manager.ConfirmBackup` | no / no / `auth_admin_keep` |
| `…tamarack.share` | `Manager.SetShare`, `Manager.RemoveShare` | no / no / `auth_admin_keep` |
| `…tamarack.configure` | `Manager.SetSnapshotSchedule` (and later settings) | no / no / `auth_admin_keep` |
| `…tamarack.restore` | `Manager.BrowseSnapshot`, `Manager.RestoreFromSnapshot` | no / no / `auth_admin_keep` |

Destructive actions use `auth_admin`, not `auth_admin_keep`: every change
asks for the admin password again. Each check passes polkit the plan's
`Erases` list as a detail, so the authentication dialog names the drives.

## 5. Safety checks at Execute (and before every step)

`Execute` refuses, with a specific error, when:

| Check | Source | Error |
| --- | --- | --- |
| Another job is running | engine lock | `…Error.Busy` |
| Digest mismatch, plan expired, layout changed | plan store | `…Error.StalePlan` |
| `confirm-erase` differs from `Erases` | request | `…Error.NotConfirmed` |
| A drive is missing or its identity changed | UDisks2 | `…Error.DriveChanged` |
| An array is degraded (except the layer a rebuild plan is fixing) | sysfs `md/degraded` | `…Error.Degraded` |
| A UPS is on battery | UPower `OnBattery` | `…Error.OnBattery` |
| No UPS, and `options["no-ups"]` was not typed | UPower | `…Error.NoUps` |
| No backup confirmed in the last 7 days (add, replace and rebuild plans) | `ConfirmBackup` timestamp | `…Error.NoRecentBackup` |

Before each step the job checks again that the UPS is not on battery and
that every drive it needs is present with the recorded identity; if not, it
**pauses** between steps rather than failing. A drive being replaced is
needed only until the "Retire the old drive" step, after which it may be
pulled. Degraded arrays are not re-checked between steps: each plan's own
health-check step stops it first, and rebuilds and reshapes degrade arrays
on purpose.

### Status (step 5)

Every method call is authorized by polkit, asynchronously, with the action
from its `PolkitAction` annotation; `Execute` uses the action for the
plan's kind and passes the kind and erased drives as details. The request
is validated before polkit is asked and again after (authentication can
take minutes), and the safety checks above run only after. A plan is used
up when it executes. The job runs on a worker thread; its records come back
to the main loop as `PropertiesChanged`, `StepStarted`, `StepFinished` and
`Finished`. When a job completes, its "after" layout replaces
`layout.json`. `Cancel` on a running job takes effect at the next step
boundary.

At start the engine exports a Job object for every unfinished journal and
re-attaches, without authorization, to one interrupted `mdadm --wait`.

In step 5 the service had no command runner, so `Execute` and `Resume`
answered `NotSupported` before polkit was asked; all of the above was
exercised with the fake runner, on a private bus with fake polkit and
UDisks2. Since step 6 the real runner is given to the engine only when
`/etc/tamarack/engine.conf` turns execution on (section 8).

`ConfirmBackup()` records "the admin says a current backup exists" with a
timestamp in `/var/lib/tamarack`. Tamarack can't verify backups; the check
makes skipping them a deliberate act.

### Health (track 2)

- **Scrub.** `tamarack-scrub.timer` runs `btrfs scrub` on the volume once a
  month (`tamarack-scrub.service`, sandboxed, idle I/O priority). A create
  plan enables it together with the mount unit.
- **md check.** Fedora's own `raid-check.timer` (from mdadm, enabled by
  default) checks every md array weekly, ours included.
- **SMART.** `smartd` (from smartmontools, enabled by Fedora's presets)
  watches the drives and runs their self-tests. The engine reads each pool
  drive's SMART state from UDisks2: ATA `SmartFailing` and bad-sector
  counts, NVMe critical warnings.
- **Alerts.** Every minute the engine compares the pool's health with the
  last check. A newly degraded layer, or a drive newly failing or missing,
  is logged to the journal with an `ALERT:` prefix and emitted as
  `ArrayDegraded` or `DriveFailing` on D-Bus. The first check after start
  reports problems that already exist. `Manager.Health` holds the whole
  picture as JSON for Cockpit and the CLI.

### Snapshots (track 3)

- **snapper** (Fedora's `snapper`) takes timeline snapshots of the `shares`
  subvolume and prunes them. The engine package ships a template,
  `/usr/share/snapper/config-templates/tamarack`, with the defaults: 24
  hourly, 7 daily, 4 weekly, 6 monthly.
- **A create plan** runs `snapper --config tamarack create-config --template
  tamarack` on `shares`, then enables snapper's own `snapper-timeline.timer`
  and `snapper-cleanup.timer`. The separate `snapshots` subvolume from the
  first design is gone: snapper keeps them in `shares/.snapshots`, which
  read-only NFS access can expose as "previous versions" later.
- **`Manager.BrowseSnapshot`** and **`Manager.RestoreFromSnapshot`**
  (polkit `…tamarack.restore`, admin, kept for a few minutes). Browsing
  needs it too: a snapshot holds every user's files. A restore copies a
  file or folder back next to the original as `name (from 2026-11-20
  14.00).ext` and never overwrites anything (`tamarack.restore`). The live
  shares are changed by NFS clients, so every path is walked component by
  component with `O_NOFOLLOW` from a folder descriptor and new files are
  made with `O_EXCL`: a symlink swapped in by a client stops the restore
  instead of steering the engine, which runs as root, elsewhere. Data is
  cloned on btrfs (`FICLONE`), so a restore costs no space until changed.
  Reading files their owners keep private needs `CAP_DAC_READ_SEARCH`
  (read and search only, never write), in the unit and in the SELinux
  module. A restore runs on its own thread and replies when it is done.
- **`Manager.SetSnapshotSchedule`** (polkit `…tamarack.configure`, admin,
  remembered for a few minutes) changes the four limits, or turns timeline
  snapshots off when all are zero, through `snapper set-config`.
  `Manager.SnapshotSchedule` reports them. From a terminal: `tamarack engine
  snapshots [--keep H D W M | --off]`.
- snapper is a D-Bus client of `snapperd`, which does the work in its own
  SELinux domain; the engine's policy only lets the two talk.

### Opening the pool at boot

Nothing in the kernel reopens dm-integrity mappings after a reboot, so
without help the arrays, the volume and the mount would not come back.
systemd's `/etc/integritytab` is the Fedora way: its generator turns each
line into a unit that opens the mapping early in boot. Then mdadm's udev
rules assemble the arrays from their superblocks, LVM activates the
`tamarack` volume group, and `srv-tamarack.mount` mounts it.

- The engine keeps a marked block of lines in `/etc/integritytab`, one per
  layer partition: `tk-dD-pN PARTUUID=<uuid> - integrity-algorithm=crc32c`.
  GPT partition UUIDs come from UDisks2 and don't change when a drive moves
  to another port. Lines outside the block are left alone.
- It rewrites the block at start and after every completed job. If a pool
  drive is missing, the file is left as it was, so the remaining
  partitions still open and the arrays start degraded.
- The unit may write that one file (`ReadWritePaths=-/etc/integritytab`;
  the package creates it if needed), labelled `tamarack_integritytab_t`,
  which systemd's generator may read.
- Still to prove in the VM: that the arrays come back as `/dev/md/tk-lN`
  after a reboot, which depends on how mdadm treats the home host.

### NFS shares (track 4)

- **A share is a folder** in the `shares` subvolume (`/srv/tamarack/shares/NAME`),
  not a subvolume of its own, so snapper's snapshots include it. A new
  folder gets the owner given (uid, gid) and mode 2775; an existing folder
  is left as it is. Removing a share stops exporting it and keeps the files.
- **NFSv4 only.** `/etc/nfs.conf.d/tamarack.conf` turns NFSv3 and UDP off,
  so only TCP port 2049 is used and no rpcbind or mountd faces the network.
  The package only makes the file exist, empty (tmpfiles.d, with its own
  SELinux label that nfs-utils' daemons may read); the engine writes the
  setting when the first share is made and restarts nfs-server once, since
  installing the package must not change the machine's NFS server. With
  shares, the engine checks it again at every start.
- **The engine owns the exports file**, `/etc/exports.d/tamarack.exports`,
  rendered from `/var/lib/tamarack/shares.json`. After writing it, the engine
  runs `systemctl enable --now` and `systemctl reload` on
  `nfs-server.service`. If nfs-server refuses the new exports, the old file
  is put back. The engine then has firewalld add or remove the `nfs` service,
  permanently and now.
- **Security is spelled out.** Every share lists its clients (addresses,
  networks or host names; nothing that could inject export options),
  read-only or read-write, and squashing (`root`, the default; `all`, to map
  everyone to the share's owner; or `none`). `Manager.Shares` adds plain
  statements: that `sec=sys` trusts the user IDs clients send, what
  squashing means, and a warning for very wide client ranges. Kerberos
  (`sec=krb5p`) is v2.
- **API:** `Manager.SetShare(name, options)` and `Manager.RemoveShare(name)`,
  authorized by the new `…tamarack.share` action. From a terminal: `tamarack
  engine share list | set NAME --client … | remove NAME`.

## 6. Steps become structured commands

Today a `Step` holds shell strings for display. The engine needs more:

```python
@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]  # run with subprocess.run(argv), never a shell
    tolerate_failure: bool = False  # replaces "|| :" in rendered output
    already_done: Check | None = None  # how to tell this ran before an interruption
```

`render()` keeps producing the same shell text for review (the digest covers
the argv it is rendered from). The engine runs `argv` lists only. This is the first
implementation step, and it changes no output.

Status: done in `tamarack.ops`. `Command` rejects programs outside a fixed
allow list and arguments that are empty or contain NUL or newline; the dry
run text is checked byte for byte against `tests/golden/`.

`already_done` became a table instead of a field: `tamarack.recovery` gives
every command a resume rule (`rerun`, `probe` with a read-only probe
command, or `ask`) from its argv, in one place that can be reviewed whole.
Each command's rule is written into the review document, so the digest
covers it, and the engine refuses a document whose rules differ from the
table.

## 7. Step journal and resume

Each job writes an append-only journal at
`/var/lib/tamarack/jobs/<job-id>.jsonl`, one JSON object per line, flushed and
`fsync`ed before the next action:

```json
{"t":"job","id":"7","digest":"…","plan":{…},"started":"2026-11-22T21:04:11Z"}
{"t":"step-start","step":3}
{"t":"cmd-start","step":3,"cmd":0,"argv":["mdadm","/dev/md/tk-l1","--add","/dev/mapper/tk-d3-p2"]}
{"t":"cmd-end","step":3,"cmd":0,"exit":0,"stderr_tail":""}
{"t":"step-end","step":3}
{"t":"job-end","result":"completed"}
```

On start, the engine reads every journal without a `job-end`. Such a job is
exported as **paused** with the step where it stopped. Resume rules:

| Interrupted at | What happens |
| --- | --- |
| Between steps | Waits for `Job.Resume` (admin), then continues with the next step. |
| Inside a `mdadm --wait` | The kernel resumes the resync or reshape by itself; the engine re-attaches and keeps reporting progress, no authorization needed. |
| Inside any other command | Waits for `Job.Resume`. Each remaining command's `already_done` check decides whether it reran; a command with no check is never rerun automatically, and the job reports which one needs a human. |

Journals are kept after completion (they are the audit trail) and pruned
after 90 days.

Status (step 3): `tamarack.journal` writes and replays journals and
`tamarack.job` runs and resumes jobs through a `Runner` interface, tested
against a fake runner that simulates a power cut during every command of
every plan kind. Details settled while building it:

- Only one command can be in doubt after a crash: the one with a
  `cmd-start` and no `cmd-end`. Earlier commands finished; later ones never
  started.
- A `probe` that exits 0 counts as the command having taken effect;
  otherwise the command runs again. An `ask` command pauses the job with
  the command named, and `Resume` then takes a resolution, `done` or
  `rerun`, after a person has looked. `Job.Resume` took an options
  argument (`resolution`) for this in step 5.
- A command failing (other than one marked `tolerate_failure`) ends the job
  as failed, with the command, exit status and the tail of its stderr.
  Failed jobs are not resumed; a person repairs the box and makes a new plan.
- A torn last line is ignored when reading and cut off before appending.
  Any other malformed or out-of-order record, a command that differs from
  the plan, or a plan that no longer matches its digest stops the engine
  from touching the job.
- Journal files are created `O_EXCL` with mode 0600, and the directory is
  fsynced after creation.

## 8. The service and its sandbox

[`data/systemd/tamarack-engine.service`](../../data/systemd/tamarack-engine.service):

- `Type=dbus` with `BusName=`, started on demand by D-Bus activation and at
  boot (to pick up interrupted jobs).
- `StateDirectory=tamarack` creates `/var/lib/tamarack` at runtime.
- `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `PrivateNetwork=yes` (the engine needs no network), `NoNewPrivileges=yes`,
  `RestrictAddressFamilies=AF_UNIX AF_NETLINK`, `LockPersonality=yes`,
  `MemoryDenyWriteExecute=yes`, `RestrictRealtime=yes`,
  `SystemCallArchitectures=native`, `SystemCallFilter=@system-service @raw-io`.
- `DevicePolicy=closed` with `DeviceAllow=` only for block devices,
  `/dev/mapper/control` and `/dev/md*`.
- `ProtectKernelModules=yes`: dm-integrity, raid1 and raid456 are loaded by
  systemd's `modprobe@` units, which the engine's unit `Wants=` and starts
  `After=`, so they load only when the engine runs.
- Relaxed, each with a comment in the unit: `ProtectKernelTunables=no`
  (mdadm writes md sysfs attributes), `PrivateDevices=no` (raw block
  access), a capability set limited to `CAP_SYS_ADMIN CAP_SYS_RAWIO
  CAP_MKNOD CAP_DAC_OVERRIDE CAP_CHOWN CAP_DAC_READ_SEARCH`.

`systemd-analyze security tamarack-engine.service` runs in CI once the unit
ships, and every remaining exposure is either fixed or explained.

### Status (step 6, first part)

- **Packaged** as the `tamarack-engine` subpackage: `/usr/libexec/tamarack-engine`,
  the unit, the bus policy and activation file, the interface XML in
  `/usr/share/dbus-1/interfaces`, the polkit policy and a tmpfiles.d entry
  that makes an empty `/etc/integritytab`. It requires the storage tools it runs. CI installs it, runs
  `systemd-analyze verify` on the unit and prints `systemd-analyze
  security --offline`.
- **The real runner** (`tamarack.runner`) runs only allowed programs, resolved
  under `/usr` and refused unless root-owned and writable only by root, with
  an argument list, stdin from `/dev/null`, a fixed `PATH` and `LC_ALL`, and
  no shell.
- **Execution is off by default.** The engine gets the runner only if
  `/etc/tamarack/engine.conf` contains `[execution]` `enabled = yes`. Nothing
  ships that file. Until the step 7 matrix passes it is for test VMs only.
- **systemd mounts the volume.** The unit has its own mount namespace, so a
  `mount` inside it would never reach the host. The package ships
  `srv-tamarack.mount` (not enabled), and a create plan ends its file
  system step with `systemctl enable --now srv-tamarack.mount`: systemd
  mounts it in the host's namespace now and at every boot.
- **Sandbox paths** the tools need are writable: `/run/mdadm`, `/run/lvm`,
  `/run/lock/lvm`, `/run/cryptsetup` and `/etc/lvm/{archive,backup,devices}`.

- **The layout is written to every drive.** Each plan ends with one
  `tamarack-meta write PARTITION LAYOUT` per drive, so the plan digest
  covers exactly the bytes written. `tamarack-meta` (in `/usr/libexec`)
  refuses any device whose udev `PARTNAME` is not `tk-meta` and any text
  that is not a valid layout, and writes a framed record (magic, version,
  length, SHA-256, padded to 4 KiB) so a torn or stale write is never read
  as a layout. It runs in its own SELinux domain, `tamarack_meta_t`, which
  is the only Tamarack code with raw-disk access.

- **The engine reads the records back.** At start and after every job it
  lists the `tk-meta` partitions through UDisks2 and reads each with
  `tamarack-meta read`, the one command it runs even with execution off
  (read-only, in `tamarack_meta_t`). With no state file it adopts the
  drives' layout and saves it. Planning is refused (`LayoutUnreadable`)
  when the drives disagree with each other or with the state file, or
  when `tk-meta` partitions exist and none can be read, so a pool is never
  mistaken for empty drives. One damaged record among good ones is logged
  and ignored. The cross-check is skipped while a job is unfinished,
  since records may be mid-update and planning is refused anyway. `systemd-analyze security` rates the unit 2.6
("OK"); the remaining exposures are the ones a storage service needs: root,
raw block devices, `CAP_SYS_ADMIN`, writable md sysfs.

## 9. SELinux

The engine runs in its own domain, `tamarack_engine_t`, from the
`tamarack-selinux` subpackage. When it runs a tool, it transitions into that
tool's existing Fedora domain (`mdadm_t`, `lvm_t`, `fsadm_t` and so on) rather
than being granted the tool's permissions itself. Its own files are
`tamarack_engine_exec_t` and `tamarack_var_lib_t`. The VM tests run
**enforcing** and fail on any AVC denial.

Status (step 6): the module is in [`selinux/`](../../selinux) and ships as
the `tamarack-selinux` subpackage, which `tamarack-engine` pulls in wherever
the targeted policy is installed. The engine domain may manage its state,
read its config, read `/proc` and sysfs, use the system bus (own its name;
talk to polkit, UDisks2, UPower and systemd) and transition into `mdadm_t`,
`lvm_t` and `fsadm_t`. It gets no raw-disk access of its own. The rules are
a first draft written without an SELinux machine to try them on; the step 7
VM matrix, run enforcing, is what will find what is missing.
`tamarack_dbus_chat` lets a confined client such as a Cockpit page talk to
the engine.

What the first enforcing VM runs taught us:

- The unit's `NoNewPrivileges=yes` blocks every SELinux domain transition
  unless the policy grants `nnp_transition`. Without it the engine ran as
  `init_t`. The module now grants it for systemd starting the engine and for
  the engine starting each tool.
- Fedora confines `mdadm` (`mdadm_t`), the LVM tools and `dmsetup`
  (`lvm_t`), and `parted`, `partprobe`, `mkfs.btrfs` and `blkid` (`fsadm_t`),
  but not `sgdisk` or `btrfs` (`bin_t`). Partitioning therefore moved from
  `sgdisk` to `parted`, so the engine never needs raw disk access;
  `integritysetup` is its own Fedora package and is now required.
- Fedora's policy labels `cryptsetup` `lvm_exec_t` but leaves
  `integritysetup` as `bin_t`, so it ran in the engine's own domain and was
  denied the raw partitions. The module labels `integritysetup` `lvm_exec_t`
  too, so it runs in `lvm_t` like `cryptsetup`; this belongs in
  selinux-policy upstream and should move there.
- `tamarack-meta` (Python) reads `/etc/passwd` and `/etc/pki` at start-up.
- The sandboxed engine cannot load modules, so on a freshly installed box
  `mdadm --create` failed with no md personality loaded. The unit now pulls
  in `modprobe@raid456.service` (and raid1, dm_integrity) before it starts;
  an earlier `%post` running `systemd-modules-load` was dropped, since it
  did nothing in image builds and loaded raid modules at every boot.
- **Upgrades don't restart a busy engine.** `%postun` doesn't restart it
  (a killed storage command would leave the job waiting for a person);
  `%posttrans` asks it over D-Bus and restarts it only when idle or paused.
- On Fedora 45, writing md_mod's `new_array` fails and `mdadm --create` falls
  back to making `/dev/mdN` itself with `mknod`. `DevicePolicy=closed` with
  `DeviceAllow=block-md rw` refused that, so md arrays now get `rwm`; the
  same create worked from outside the sandbox.
- `systemctl` runs in the engine's domain, so systemd checks the engine for
  `enable` (a daemon reload) and for each unit it starts. Tamarack's mount,
  scrub timer and scrub service are labelled `tamarack_unit_file_t`; the
  engine may also start snapper's timers and `nfs-server.service`.
- A new btrfs filesystem's root has no label (`unlabeled_t`). The volume now
  has its own type, `tamarack_data_t`, set at every mount with
  `rootcontext=` and inherited by everything below; the engine and snapperd
  may manage it. A private type rather than `public_content_rw_t`, so
  services allowed to read public content cannot read the shares.
- Partitions that parted adds one at a time have no `PARTNAME` in the
  kernel's uevent, so `tamarack-meta` refused to write the layout. It now
  falls back to udev's `ID_PART_ENTRY_NAME`.
- md refuses to reshape, replace a member or drop the bitmap while an array
  is still syncing, and a new array starts with a full resync. Every health
  check now runs `mdadm --wait` first; `--wait` exits 1 when there was
  nothing to wait for, so its failure is tolerated everywhere.
- btrfs leaves a new subvolume's root unlabelled, so the engine could not
  make share folders in `shares`; the create plan now labels it with
  `chcon`, and snapperd may relabel `.snapshots`.
- Every array is created with `--data-offset=128M`. On small members mdadm
  reserves much less, and after a mirror was reshaped into parity there was
  no room left to add RAID5's partial parity log.
- A new array reuses space an older one had, at the same data offset, so
  the old PV label showed through and `pvcreate` found a duplicate PV ID.
  `wipefs --all` now runs on every new array before `pvcreate`.
- Reshaping a mirror into parity still left no room for PPL: mdadm only puts
  PPL between the superblock and the bad block log, and a member written with
  a bitmap has its bad block log 8 sectors after the superblock (read from
  mdadm's super1.c). Layers with one-drive protection are now RAID5 from the
  start, two members included, so PPL is on from creation; a new member is
  added while PPL is still on (so mdadm reserves room for it) and PPL is only
  switched off for the reshape itself. During a reshape a layer has neither
  PPL nor a bitmap.
- By default `mdadm --create` builds RAID5 onto its last member as a spare,
  and until that recovery ends the array reports degraded, which blocked
  every following job. RAID5 is created with `--force`: all members active
  and an ordinary resync, which the health check waits for.
- btrfs runs in the engine's domain and needs `CAP_SYS_ADMIN` for resize
  and subvolumes; `RestrictSUIDSGID=` is off because share folders are
  setgid.
- A rebuild fails and removes the pulled drive's members by name: md may
  not have noticed the drive is gone while its mapping still answers.
- snapper's timers carry the generic `systemd_unit_file_t` label; the engine
  may ask for their status, not start or enable other generic units.

## 10. Testing on (virtual) disks

tmt plans, run by Packit and Fedora CI on Testing Farm VMs with extra
virtual disks:

- the matrix from the project plan: create, add, swap for larger, failed
  drive, single drive, mixed sizes;
- power cut mid-reshape (kill the VM) and resume;
- silent corruption: flip bytes on one member, read, confirm md repairs it
  and the scrub is clean;
- recovery elsewhere: assemble the disks on a stock Fedora VM by hand;
- every test with SELinux enforcing and a checksum manifest of test data.

Nothing destructive is enabled outside these VMs until the matrix passes.

Status (step 7, first part): [`plans/storage.fmf`](../../plans/storage.fmf)
and [`tests/vm/`](../../tests/vm) hold the first tests: create on two
drives then add a third and a fourth, and swap a drive while the data stays
online. Each checks a checksum manifest after every change, that every
drive carries the layout record, that a Btrfs scrub is clean and that there
were no AVC denials, with SELinux enforcing. The drives are RAM-backed
`scsi_debug` disks made inside the VM (one host and store per drive, so
each has its own WWN), and the client refuses any drive that is not one.
Packit runs the plan on Testing Farm once its GitHub app is installed; CI
runs `tmt lint` on it meanwhile.

Added since: a single drive grown to two, and a drive that disappears
mid-use (deleted from the SCSI layer) rebuilt onto a spare while the data
stays readable.

Added since then:
- **Silent corruption.** Four MiB of garbage is written under dm-integrity
  on one drive. The kernel must log the checksum failures, md must repair
  from the other drives, and the data and a scrub must come back clean.
- **Engine killed mid-reshape.** The reshape is slowed so it is still
  running, then `SIGKILL` hits the engine. On restart the engine must
  re-attach to the reshape by itself, pause, and finish on `Resume`. This
  is the engine's half of a power cut; the kernel's half is md's own
  checkpointing.
- **Recovery by hand.** With the engine stopped and everything torn down,
  `integritysetup`, `mdadm`, `vgchange` and `mount` alone bring the data
  back. The steps are written up for people in
  [docs/recovery.md](../recovery.md).

Added after that:
- **File-backed drives.** `scsi_debug` gives every drive one size and
  keeps it in RAM, so `make_file_drives` in `tests/vm/lib.sh` also makes
  drives of any size through the kernel's SCSI target (LIO, set up with
  `targetcli`): a sparse file under `/var/tmp/tamarack-drives/`, a fileio
  backstore named `tk-test-N` and a loopback (`tcm_loop`) target per drive,
  so each is its own SCSI disk with its own WWN and serial. The
  configuration is saved and `target.service` enabled, so the drives can
  come back after a reboot. The client accepts these only when configfs
  shows the disk is a `tk-test-*` fileio backstore on a file in that
  directory.
- **Mixed sizes.** Create on 1.25, 1.25 and 2.5 GB file-backed drives (one
  single-parity layer, the top half of the 2.5 GB drive unused), then add a
  second 2.5 GB drive: it widens the parity layer and its top half pairs
  with the unused space in a new two-copy layer, and the data, the layout
  records and a scrub stay clean.
- **NFS shares.** Two shares exported to 127.0.0.1 and mounted over NFSv4:
  the owner can write, root on the client is squashed, the read-only share
  refuses writes, the files land in the pool, and nfsd offers no NFSv3.
- **PPL cost.** Write speed on a two- and a three-drive layer with md's
  partial parity log on and off. The VM's drives live in RAM, so the numbers
  are noise; real disks will tell. It is a measurement, not a check, so it
  has its own plan (`plans/measure.fmf`) that runs only on request.
- **Every layer keeps PPL.** The grow tests end by checking that each layer's
  consistency policy is `ppl`.

The whole matrix passes with SELinux enforcing (PR #22, Nov 23, 2026),
and since PR #29 it includes a real reboot on file-backed drives: the pool
comes back by itself, arrays under their own names.

**Three VMs in parallel.** Testing Farm runs each tmt plan on its own
machine, at the same time, so the matrix is split into three plans in
`plans/storage.fmf`, balanced by run time: `/build` (create and grow, mixed
sizes, NFS, the Cockpit page, recovery by hand, reboot), `/failures` (a
pulled drive, an online swap, the engine killed mid-reshape) and
`/integrity` (one drive then two, silent corruption). Each test carries its
group as a tag (`vm-build`, `vm-failures`, `vm-integrity`) in
`tests/vm/main.fmf`; a new test goes in the group that keeps them even.
Run in a row the tests took about 72 minutes; split, the slowest group
takes about 25.

## 11. Implementation order

| Step | What | Done in |
| --- | --- | --- |
| 1 | `Command` with `argv` (section 6), no behaviour change | #7 |
| 2 | Plan digest and drive identities in the planners | #8 |
| 3 | Journal writer and reader, with resume logic, against a fake runner | #9 |
| 4 | Engine service on D-Bus with read-only methods and planning | #10 |
| 5 | polkit checks and `Execute` against the fake runner | #11 |
| 6 | Real runner, packaging, SELinux module, `tk-meta` records, boot mount | #12, #13, #14, #16, #18 |
| 7 | tmt VM matrix | #15, #16, #19, #22 (passing on Testing Farm) |

Then the roadmap tracks:

| Track | What | Done in |
| --- | --- | --- |
| 1 | Prove the engine safe: VM matrix green, mixed sizes, file-backed drives; reboot test | #22, #25; reboot in #29 |
| 2 | Health: scrub, SMART, md check, alerts | #24 |
| 3 | Snapshots: snapper, 24/7/4/6 | #26 |
| 4 | NFS shares | #27 |
| — | Opening the pool at boot (`/etc/integritytab`) | #28 |
| 5 | Cockpit page | next |
| 6 | bootc image | |
| 7 | TerraMaster F4-425 Pro bring-up | |

Destructive execution still stays off by default (`/etc/tamarack/engine.conf`)
until the reboot test passes and the box has run on real disks.

## 12. Decided defaults

- Plans expire 30 minutes after they are made.
- A backup counts as recent for 7 days.
- `Job.Cancel` is refused while a reshape runs: cancel only takes effect
  between steps, and reshapes always finish.

These can be revisited with real use; each is one constant.

## 13. Open questions

Settled:
- The VM tests make their own drives inside the VM (`scsi_debug` and
  LIO file-backed), so they don't depend on Testing Farm's disk options.
- Mixed sizes are tested on file-backed drives.
- The SELinux module covers everything the matrix does, enforcing; the
  denials left in the tests are Fedora's own (listed in `tests/vm/lib.sh`).
- `tamarack engine` is a working command-line client of the engine.
- Every one-drive-protection layer is RAID5 with PPL (section 9 notes,
  README).
- md arrays are created with `--homehost=any`. Without it mdadm stores the
  hostname at create time; early in boot the hostname is still `localhost`,
  so udev treated the array as foreign and brought it back as `tk-l1_0`,
  started read-auto (seen in the reboot test, #29). With `any` the arrays
  keep their names at boot and on any host the drives move to. Pools created
  before this change keep the old name until reassembled with
  `mdadm --assemble --update=homehost --homehost=any`; the engine does not
  do that yet.

Open:

- **PPL's write cost on real disks.**
- **Two-drive protection** (5+ drives) in grow.
- **The Cockpit page** as the main client (track 5).
- **Late-arriving drives.** If a pool drive appears after boot has opened
  the integrity mappings, nothing opens its mapping later; the engine could
  do it on start or on a udev event.
