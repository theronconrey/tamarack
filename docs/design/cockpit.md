<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Tamarack in Cockpit: design

Status: **the Overview is built, read-only** (`data/cockpit/tamarack/`,
package `tamarack-cockpit`). Changes are still made with `tamarack engine`;
the guided flows come one at a time.

## Who it is for

The owner of the box, who should never need to know what md, PPL or a
bitmap is. The page answers three questions at a glance: is my data safe,
how much space do I have, and what should I do next. Everything else is one
click deeper.

## Rules

- Never ask the owner a storage-engineering question.
- One banner, one sentence, one action. "Critical" never appears without
  its action.
- Every change shows its plan in plain words (the exact commands one click
  away), is checked again before it runs, needs the bay typed before
  anything is erased, then the admin password (polkit).
- The page shows only what the engine confirmed, and talks only to the
  engine over D-Bus: never to disks, md or LVM.
- Status is never colour alone: an icon and words too. Touch targets at
  least 44 px; it works at phone width.
- Engine text goes in with `textContent`, never as HTML; no inline styles or
  scripts (Cockpit's content security policy refuses them anyway).

## Where it lives

- **Installed as a package on any Fedora machine:** its own menu entry,
  "Tamarack", next to Cockpit's Storage page.
- **On the Tamarack appliance (the bootc image):** the entry is "Storage" and
  it is the only storage page; Cockpit's own Storage page is hidden and its
  low-level view moves into Tamarack's Advanced section. The mechanism
  (Cockpit's manifest overrides) is to be confirmed when the image is built.

Inside, the heading says "Storage", with "Managed by Tamarack" beside it.

## The Overview (built)

- **Banner**, worked out in `status.js` in this order: no pool yet; a job
  running (its step, "keep the power on"); a job waiting for a person; a
  drive failed or missing (which one, what size to buy, one action); a
  drive wearing out; one drive only (not protected against its failure);
  85% and 95% full (add space soon, now); otherwise "Your data is
  protected".
- **Drives**, in the layout's order as "Drive 1…N" with size and health in
  words. Bays replace them at hardware bring-up, when slot numbers are
  known.
- **Space**: free of total for your files, and a bar of your files, free,
  what keeps your data safe and space waiting for a partner drive, worked
  out from the layout and the engine's `Space` property.
- **Looking after your data**: the snapshot schedule, the last backup
  confirmation, and power.

`status.js` holds all of that as pure functions; `tests/cockpit/` runs it
under node in CI, and the `cockpit` VM test feeds it a real pool's
properties, read as an ordinary user.

## Still to build

The Drives, Shares, Snapshots and Activity tabs and the Advanced view;
then the guided flows (add, replace, rebuild, shares, schedule, resume),
each a plan reviewed on the page before polkit asks for the password.
Mockups of all of these, and the ideas taken from Drobo's dashboard (the
box as the interface, a small vocabulary of states tied to physical
actions, honest capacity, long jobs that set expectations), are in the
project's design notes.
