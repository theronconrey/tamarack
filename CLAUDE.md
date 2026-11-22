# Tamarack: instructions for Claude

All code, packaging, image and unit-file changes in this repository must follow
Fedora best practices as written in `CONTRIBUTING.md`. Read it before making
changes, and treat its rules as requirements, not suggestions.

In particular:

- Only use dependencies that are packaged in Fedora; never add `pip install`
  steps to the image or vendor libraries.
- Runtime paths: code and defaults in `/usr`, admin config in `/etc/tamarack`,
  state in `/var/lib/tamarack` created by systemd at runtime.
- Assume SELinux enforcing. Never suggest disabling it or setting permissive.
- Keep everything a dry run until the step journal and safety checks exist;
  nothing in this repository may write to a disk without an explicit,
  reviewed execution path.
- Before finishing: `python3 -m pytest`, `ruff check .`, `ruff format --check .`
  and `mypy` must all pass; new files get the SPDX header.
- If a change can't follow a rule, stop and say so in the pull request instead
  of working around it.
