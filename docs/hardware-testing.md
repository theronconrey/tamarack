<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Testing on real hardware

The quickest way to run Tamarack on a real box is the RPMs Packit builds
from every merge to `main`, published in the Copr project
`packit/theronconrey-tamarack-main` (Fedora 43, 44, 45 and rawhide). The
bootc appliance image comes later; nothing here depends on it.

**Only use drives you are ready to lose.** Execution is off by default, and
it stays off on any box that holds data you care about until the hardware
tests below have passed.

## 1. The box

- Fedora 45 (Server or Workstation) installed on its **own** drive, for
  example an M.2 NVMe drive. Tamarack refuses the drive the operating system
  is on, even under LUKS or LVM.
- Two to four blank SATA drives in the bays for the pool. USB drives are
  always refused.
- A UPS if you have one (UPower must see it). Without one, every change
  needs `--no-ups`.

## 2. Install

```sh
sudo dnf copr enable packit/theronconrey-tamarack-main
sudo dnf install tamarack tamarack-engine tamarack-selinux tamarack-cockpit cockpit
sudo systemctl enable --now cockpit.socket
```

The engine starts on demand over D-Bus. Cockpit is at
`https://<box>:9090`; the page is **Tamarack** in the menu.

To update after more pull requests merge: `sudo dnf upgrade --refresh`.

## 3. Turn execution on (test boxes only)

```sh
sudo install -d -m 0755 /etc/tamarack
printf '[execution]\nenabled = yes\n' | sudo tee /etc/tamarack/engine.conf
sudo systemctl restart tamarack-engine
```

Check that SELinux is enforcing (`getenforce`); never test with it off.

## 4. First tests, in order

Use `/dev/disk/by-id/` paths, so a renamed `/dev/sdX` can't point at the
wrong drive. `tamarack engine status` shows the pool at any time; the
Cockpit page shows the same.

1. **Look first.** `tamarack engine create --dry-run <drive> <drive>` shows
   the plan, the space it gives and any warnings, and changes nothing.
2. **Create** a pool on two drives: `tamarack engine create <drive> <drive>`.
   You type the drives back to confirm.
3. **Write data** to a share (Cockpit, Shares, New share), and keep
   checksums: `find /srv/tamarack/shares -type f -exec sha256sum {} + > ~/before.sha256`.
4. **Reboot.** The pool must come back by itself; check the data with
   `sha256sum -c ~/before.sha256`.
5. **Add** a third drive (`tamarack engine add <drive>`), then a fourth.
   Large drives take hours per step; the page shows progress.
6. **Pull a drive** while the box is running. The banner must say which
   one, and the data must stay readable. Put in a blank drive and
   `tamarack engine rebuild <failed> <new>`.
7. **Cut the power** (the switch, not shutdown) while nothing is running,
   then again during a resync. The pool must come back; a paused job must
   say what it needs.
8. **Snapshots:** delete a file, then restore it from the Snapshots tab
   (once snapshot restore is in `main`).

After each step: `tamarack engine status`, `sudo btrfs scrub start -B
/srv/tamarack`, and `sudo ausearch -m avc -ts recent` (any denial that
mentions tamarack is a bug).

## 5. What to send back

- The model of the box and of each drive, and `lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN`.
- What happened at each step, and how long the long steps took.
- `journalctl -u tamarack-engine -b` and any AVC denials.
- Anything the Cockpit page said that was unclear or wrong.

These results decide when execution can be on by default, and they give
the real numbers for the write cost of PPL and dm-integrity.
