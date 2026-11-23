# SPDX-License-Identifier: GPL-3.0-or-later
"""NFS shares: what they are, how they are checked, and the exports file.

A share is a directory under the shares subvolume, so snapper's snapshots
include it, exported over NFSv4 to the clients the admin lists. This module
is pure: it validates shares and renders ``/etc/exports.d/tamarack.exports``.
The engine stores the list, writes the file and asks nfs-server and firewalld
to pick it up.

Security is spelled out per share rather than hidden. v1 offers ``sec=sys``
only, which trusts the user IDs the client sends: anyone who controls an
allowed client machine can act as any user except root (with root squashing
on). Kerberos (``sec=krb5p``) is planned for v2.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .commands import SHARES

EXPORTS_FILE = "/etc/exports.d/tamarack.exports"
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# Host names and NFS wildcards (*.lan); nothing that could close or add options.
_HOST = re.compile(r"[A-Za-z0-9*?]([A-Za-z0-9*?.-]{0,252})")
SQUASH = ("root", "all", "none")
SEC_SYS_NOTE = (
    "sec=sys trusts the user IDs each client sends: anyone in control of an "
    "allowed client can act as any user on this share"
)


class ShareError(ValueError):
    pass


@dataclass(frozen=True)
class Share:
    name: str
    clients: tuple[str, ...]
    read_only: bool = False
    squash: str = "root"  # root (root_squash), all (all_squash), none (no_root_squash)
    uid: int = 0  # owner of a new folder; with squash "all", every client user
    gid: int = 0

    @property
    def path(self) -> str:
        return f"{SHARES}/{self.name}"

    def security(self) -> list[str]:
        """Plain statements about who can do what, for the UI and the CLI."""
        notes = [SEC_SYS_NOTE]
        notes.append("read-only for every client" if self.read_only else "clients can change files")
        if self.squash == "root":
            notes.append("root on a client is treated as the anonymous user nobody")
        elif self.squash == "all":
            notes.append(f"every client user is treated as uid {self.uid}, gid {self.gid}")
        else:
            notes.append("root on a client is root on this share (no squashing)")
        if any(_is_wide(c) for c in self.clients):
            notes.append("open to a wide range of addresses; consider narrowing it")
        return notes

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["clients"] = list(self.clients)
        data["path"] = self.path
        data["security"] = self.security()
        return data


def _is_wide(client: str) -> bool:
    if client in ("*", "0.0.0.0/0", "::/0"):
        return True
    try:
        net = ipaddress.ip_network(client, strict=False)
    except ValueError:
        return "*" in client
    return net.prefixlen < (16 if net.version == 4 else 48)


def validate_client(client: str) -> str:
    try:
        return str(ipaddress.ip_network(client, strict=False))
    except ValueError:
        pass
    if not _HOST.fullmatch(client):
        raise ShareError(f"not an address, network or host name: {client!r}")
    return client


def make_share(name: str, options: dict[str, Any]) -> Share:
    """Validate a share from the D-Bus options dictionary."""
    if not _NAME.fullmatch(name):
        raise ShareError("share names use lowercase letters, digits, '.', '_' and '-' (up to 64)")
    unknown = set(options) - {"clients", "read-only", "squash", "uid", "gid"}
    if unknown:
        raise ShareError(f"unknown options: {', '.join(sorted(unknown))}")
    clients = options.get("clients")
    if not isinstance(clients, list) or not clients or not all(isinstance(c, str) for c in clients):
        raise ShareError("list at least one client: an address, network or host name")
    read_only = options.get("read-only", False)
    squash = options.get("squash", "root")
    if not isinstance(read_only, bool):
        raise ShareError("read-only must be true or false")
    if squash not in SQUASH:
        raise ShareError(f"squash must be one of {', '.join(SQUASH)}")
    ids = {}
    for key in ("uid", "gid"):
        value = options.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**32 - 1:
            raise ShareError(f"{key} must be a user or group number")
        ids[key] = value
    unique = tuple(dict.fromkeys(validate_client(c) for c in clients))
    return Share(name, unique, read_only, squash, ids["uid"], ids["gid"])


def exports(shares: list[Share]) -> str:
    """The contents of the exports file, one line per share."""
    lines = [
        "# Written by the Tamarack engine from /var/lib/tamarack/shares.json.",
        "# Changes made here are overwritten; use tamarack engine share.",
    ]
    for share in sorted(shares, key=lambda s: s.name):
        options = [
            "ro" if share.read_only else "rw",
            "sync",
            "no_subtree_check",
            "sec=sys",
            {"root": "root_squash", "all": "all_squash", "none": "no_root_squash"}[share.squash],
        ]
        if share.squash == "all":
            # Everyone becomes the share's owner. Otherwise squashed root is
            # NFS's own anonymous user (nobody), never the owner or root.
            options += [f"anonuid={share.uid}", f"anongid={share.gid}"]
        opts = ",".join(options)
        lines.append(f"{share.path} " + " ".join(f"{c}({opts})" for c in share.clients))
    return "\n".join(lines) + "\n"


def load(text: str) -> list[Share]:
    data = json.loads(text)
    return [
        Share(
            d["name"],
            tuple(d["clients"]),
            bool(d["read_only"]),
            d["squash"],
            int(d["uid"]),
            int(d["gid"]),
        )
        for d in data["shares"]
    ]


def dump(shares: list[Share]) -> str:
    rows = [
        {k: v for k, v in s.to_dict().items() if k not in ("path", "security")}
        for s in sorted(shares, key=lambda s: s.name)
    ]
    return json.dumps({"version": 1, "shares": rows}, indent=2, sort_keys=True) + "\n"
