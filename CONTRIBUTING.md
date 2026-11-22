# Contributing to Tamarack

**Rule: everything in this repository follows Fedora best practices.** That
covers code, packaging, the bootc image, systemd units and docs. If a change
can't follow them, the pull request must say why and what the exception is.

The authoritative sources are the
[Fedora Packaging Guidelines](https://docs.fedoraproject.org/en-US/packaging-guidelines/),
the [Python Packaging Guidelines](https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/)
and the [Fedora bootc documentation](https://docs.fedoraproject.org/en-US/bootc/).
Where this file and those disagree, those win; open an issue to fix this file.

## 1. Packaging

- Tamarack ships as an RPM built from `tamarack.spec` in this repository, and
  the bootc image installs that RPM. Nothing is copied into the image by hand.
- Python packaging uses the current (2021+) Python guidelines and
  `pyproject-rpm-macros`: `BuildRequires: python3-devel`,
  `%pyproject_buildrequires` in `%generate_buildrequires`, then
  `%pyproject_wheel`, `%pyproject_install`, `%pyproject_save_files`, and tests
  in `%check` with `%pytest`.
- `License:` uses an SPDX expression (`GPL-3.0-or-later`).
- No bundled or vendored libraries. Every dependency must exist as a Fedora
  package.
- Builds work offline (as in `mock` and Koji). `rpmlint` must be clean, or
  each remaining warning explained in an rpmlintrc.

## 2. Python

- Target the `python3` that ships in the supported Fedora releases.
- Runtime dependencies come from Fedora (`python3-*` packages). Prefer the
  standard library. Never `pip install` anything into the image.
- Style and checks: ruff (lint and format), mypy in strict mode, pytest. All
  must pass in CI.
- Every source file starts with an SPDX header:
  `# SPDX-License-Identifier: GPL-3.0-or-later`.

## 3. The bootc image

- The `Containerfile` starts `FROM quay.io/fedora/fedora-bootc:<release>`,
  installs packages with `dnf` (then `dnf clean all`), and ends with
  `RUN bootc container lint`.
- Know where things live at runtime: image content is read-only, `/etc` is
  persistent machine-local config, and `/var` is persistent state that the
  image only seeds at first install.
  - Code and defaults: `/usr` (`/usr/bin`, `/usr/lib/tamarack`,
    `/usr/share/tamarack`).
  - Admin-editable config: small files in `/etc/tamarack`, with working
    defaults shipped in `/usr` so `/etc` stays optional.
  - State (layout files, journals): `/var/lib/tamarack`, created at runtime by
    systemd (`StateDirectory=` or `tmpfiles.d`), never baked into the image.
  - Nothing shipped in `/run`.
- Kernel arguments go in `/usr/lib/bootc/kargs.d/`.

## 4. systemd

- Units ship in `/usr/lib/systemd/system`; default enablement via presets in
  `/usr/lib/systemd/system-preset`, never by symlinking in the Containerfile.
- Log to stdout/stderr (the journal). No private log files.
- Use systemd sandboxing options where the service allows it. The storage
  engine needs raw block-device access, so document each option it can't use.

## 5. SELinux and security

- Everything must work with SELinux **enforcing**. Never disable SELinux or
  set it to permissive, in code, docs or the image.
- If Tamarack needs its own SELinux policy, it ships as a `tamarack-selinux`
  subpackage, and custom paths get proper file contexts.
- Network ports are opened through firewalld service definitions, never by
  turning the firewall off.
- TLS follows the system crypto-policies; no hard-coded cipher lists.
- Telemetry is off unless the user turns it on.

## 6. Upstream first

- Use the tools Fedora already ships (mdadm, lvm2, cryptsetup's
  integritysetup, btrfs-progs, snapper, smartmontools, cockpit, nfs-utils)
  rather than reimplementing them.
- Bugs found in those tools are reported upstream, with a workaround here only
  until the fix lands.

## 7. Pull requests

Every PR confirms:

- [ ] Tests, ruff, mypy and (once the spec exists) rpmlint pass
- [ ] New files have SPDX headers
- [ ] No new dependency that isn't packaged in Fedora
- [ ] Runtime paths follow section 3 (`/usr`, `/etc`, `/var/lib/tamarack`)
- [ ] Works with SELinux enforcing, or the PR says what policy is needed
- [ ] Any exception to these rules is explained in the PR

Commits are signed off (`git commit -s`, Developer Certificate of Origin).
