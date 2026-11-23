# Tamarack: instructions for Claude

All code, packaging, image and unit-file changes in this repository must follow
Fedora best practices as written in `CONTRIBUTING.md`. Read it before making
changes, and treat its rules as requirements, not suggestions.

Tamarack is written for Fedora, not for generic Linux: use the Fedora
component that owns each job (systemd, D-Bus with polkit, Cockpit, UDisks2,
systemd credentials, the journal, firewalld, crypto-policies, bootc) instead
of building an equivalent.

In particular:

- Only use dependencies that are packaged in Fedora; never add `pip install`
  steps to the image or vendor libraries.
- Runtime paths: code and defaults in `/usr`, admin config in `/etc/tamarack`,
  state in `/var/lib/tamarack` created by systemd at runtime.
- Assume SELinux enforcing. Never suggest disabling it or setting permissive.
- Least privilege: only the storage engine is privileged; everything else
  talks to it over D-Bus and polkit authorizes. Sandbox every unit.
- External commands run with argument lists, never `shell=True`; validate
  device paths and all input; never put secrets in code, logs or the image.
- Keep everything a dry run until the step journal and safety checks exist;
  nothing in this repository may write to a disk without an explicit,
  reviewed, polkit-authorized execution path.
- Before finishing: `python3 -m pytest`, `ruff check .`, `ruff format --check .`
  and `mypy` must all pass; new files get the SPDX header; commits are signed
  off (`git commit -s`).
- If a change can't follow a rule, stop and say so in the pull request instead
  of working around it.

Attribution:

- Commits end with `Co-Authored-By: Claude <noreply@anthropic.com>` and the
  `Signed-off-by:` line from `git commit -s`.
- Pull request descriptions end with "Co-authored with Claude, reviewed by
  Theron."
- No session links and no "Generated with Claude Code" lines anywhere.
