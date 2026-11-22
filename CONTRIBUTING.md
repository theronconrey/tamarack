# Contributing to Tamarack

**Rule: everything in this repository follows Fedora best practices.** That
covers code, packaging, the bootc image, systemd units and docs.

**Tamarack is written for Fedora, not for "any Linux".** Code uses the
mechanisms Fedora provides for services, privileges, security and updates
instead of building its own or aiming for the lowest common denominator. When
Fedora has a standard way to do something, that is the way Tamarack does it.

If a change can't follow these rules, the pull request must say why and what
the exception is.

The authoritative sources are the
[Fedora Packaging Guidelines](https://docs.fedoraproject.org/en-US/packaging-guidelines/),
the [Python Packaging Guidelines](https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/)
and the [Fedora bootc documentation](https://docs.fedoraproject.org/en-US/bootc/).
Where this file and those disagree, those win; open an issue to fix this file.

## 1. Design: built on Fedora's platform

Use the Fedora component that owns each job, rather than a home-grown one:

| Job | Use | Not |
| --- | --- | --- |
| Run and supervise services | systemd units, `Type=notify`, socket activation where it fits | Custom daemons that fork, PID files, cron |
| Local API between UI, CLI and engine | D-Bus on the system bus | A private socket protocol or HTTP server |
| Who may do what | polkit actions for every privileged D-Bus method | Custom user checks, blanket `wheel`/root rules |
| Login and the web UI | Cockpit (PAM, polkit, its TLS and session handling) | Our own web server, login page or TLS |
| Disk discovery and events | udev, UDisks2 | Polling `/dev` or parsing `/proc` by hand |
| Secrets (alert tokens, API tokens) | systemd credentials (`LoadCredentialEncrypted=`, `systemd-creds`) | Plain-text secrets in `/etc` |
| Logging | The journal, with structured fields | Log files, ad-hoc rotation |
| Firewall | firewalld service definitions | iptables/nftables rules written by us |
| TLS settings | System crypto-policies | Hard-coded protocols or ciphers |
| Updates | bootc images, signed, with rollback | In-place package changes on a running box |
| Time-based jobs | systemd timers | cron |
| Tests on real systems | tmt plans run by Fedora CI / Packit | Ad-hoc CI scripts only |

## 2. Security

Least privilege and defence in depth, the Fedora way:

- **SELinux is always enforcing.** Never disable it or set it permissive, in
  code, docs or the image. The engine runs in its own confined domain; its
  policy ships as a `tamarack-selinux` subpackage, and every custom path has a
  file context.
- **Run with the least privilege possible.** Only the storage engine is
  privileged, and only to the extent block-device work requires. Everything
  else (UI, metrics exporter, alerting) runs unprivileged and asks the engine
  over D-Bus, where polkit decides.
- **Sandbox every unit.** Start from `ProtectSystem=strict`,
  `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, a minimal
  `CapabilityBoundingSet=`, `RestrictAddressFamilies=`,
  `SystemCallFilter=@system-service`, and relax only what a unit provably
  needs, with a comment saying why. Check with `systemd-analyze security`.
- **Destructive actions need explicit authorization.** Every method that can
  erase or reshape storage is a separate polkit action requiring admin
  authentication, and runs only after a dry run the user has seen.
- **Running external tools safely.** Call `mdadm`, `sgdisk`, `integritysetup`
  and friends with argument lists (`subprocess.run([...], check=True)`), never
  `shell=True`, never strings built from user input. Validate device paths
  against UDisks/udev before use.
- **Input is untrusted.** Validate everything that arrives over D-Bus, from
  layout files and from the network, with clear errors.
- **No secrets in the image, logs or repo.** Secrets come from systemd
  credentials; logs never include them.
- **Signed everything.** The bootc image is signed and clients enforce the
  signature through the containers signature policy; RPMs are signed.
- **Privacy.** Telemetry is off unless the user turns it on, and it can always
  be turned off.

## 3. Packaging

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
- Any compiled code uses Fedora's standard build flags (`%{build_cflags}`,
  `%{build_ldflags}`), which carry Fedora's hardening options.

## 4. Python

- Target the `python3` that ships in the supported Fedora releases.
- Runtime dependencies come from Fedora (`python3-*` packages). Prefer the
  standard library. Never `pip install` anything into the image.
- Style and checks: ruff (lint and format), mypy in strict mode, pytest. All
  must pass in CI.
- Every source file starts with an SPDX header:
  `# SPDX-License-Identifier: GPL-3.0-or-later`.

## 5. The bootc image

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

## 6. systemd

- Units ship in `/usr/lib/systemd/system`; default enablement via presets in
  `/usr/lib/systemd/system-preset`, never by symlinking in the Containerfile.
- Log to stdout/stderr (the journal). No private log files.
- Every unit is sandboxed as described in section 2.

## 7. Upstream first

- Use the tools Fedora already ships (mdadm, lvm2, cryptsetup's
  integritysetup, btrfs-progs, snapper, smartmontools, UDisks2, cockpit,
  nfs-utils) rather than reimplementing them.
- Bugs found in those tools are reported upstream, with a workaround here only
  until the fix lands.

## 8. Pull requests

Every PR confirms:

- [ ] Tests, ruff, mypy and (once the spec exists) rpmlint pass
- [ ] New files have SPDX headers
- [ ] Uses the Fedora component for the job (section 1), not a home-grown one
- [ ] No new dependency that isn't packaged in Fedora
- [ ] Runtime paths follow section 5 (`/usr`, `/etc`, `/var/lib/tamarack`)
- [ ] Works with SELinux enforcing, or the PR says what policy is needed
- [ ] New or changed units pass `systemd-analyze security` review
- [ ] Privileged or destructive actions go through polkit
- [ ] No `shell=True`, no secrets in code, logs or the image
- [ ] Any exception to these rules is explained in the PR

Commits are signed off (`git commit -s`, Developer Certificate of Origin).
