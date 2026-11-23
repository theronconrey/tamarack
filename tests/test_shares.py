# SPDX-License-Identifier: GPL-3.0-or-later
"""NFS shares: validation, security notes, the exports file, and the engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner
from test_engine import run_engine

from tamarack.engine import Busy, InvalidArgument, NoLayout, NotSupported
from tamarack.job import Result
from tamarack.shares import (
    SEC_SYS_NOTE,
    Share,
    ShareError,
    dump,
    exports,
    load,
    make_share,
    validate_client,
)


@pytest.mark.parametrize(
    ("client", "normal"),
    [
        ("192.168.1.0/24", "192.168.1.0/24"),
        ("192.168.1.7", "192.168.1.7/32"),
        ("192.168.1.7/24", "192.168.1.0/24"),
        ("fd00::/64", "fd00::/64"),
        ("media-vm", "media-vm"),
        ("*.lan", "*.lan"),
    ],
)
def test_clients(client: str, normal: str) -> None:
    assert validate_client(client) == normal


@pytest.mark.parametrize(
    "client", ["", "host(rw)", "a b", "x,y", "host\n", "-o", "a" * 300, "/etc/passwd"]
)
def test_bad_clients(client: str) -> None:
    with pytest.raises(ShareError):
        validate_client(client)


@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("Media", {"clients": ["10.0.0.0/24"]}),
        ("../x", {"clients": ["10.0.0.0/24"]}),
        ("media", {}),
        ("media", {"clients": []}),
        ("media", {"clients": ["10.0.0.0/24"], "squash": "some"}),
        ("media", {"clients": ["10.0.0.0/24"], "read-only": "yes"}),
        ("media", {"clients": ["10.0.0.0/24"], "uid": -1}),
        ("media", {"clients": ["10.0.0.0/24"], "fsid": 1}),
    ],
)
def test_bad_shares(name: str, options: dict[str, Any]) -> None:
    with pytest.raises(ShareError):
        make_share(name, options)


def test_exports_file() -> None:
    media = make_share("media", {"clients": ["192.168.1.0/24", "media-vm"], "read-only": True})
    backup = make_share(
        "backup", {"clients": ["10.0.0.5"], "squash": "all", "uid": 1000, "gid": 1000}
    )
    text = exports([media, backup])
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == [
        "/srv/tamarack/shares/backup 10.0.0.5/32(rw,sync,no_subtree_check,sec=sys,"
        "all_squash,anonuid=1000,anongid=1000)",
        "/srv/tamarack/shares/media 192.168.1.0/24(ro,sync,no_subtree_check,sec=sys,"
        "root_squash) media-vm(ro,sync,no_subtree_check,sec=sys,root_squash)",
    ]


def test_security_is_spelled_out() -> None:
    open_rw = make_share("x", {"clients": ["*"], "squash": "none"})
    notes = open_rw.security()
    assert notes[0] == SEC_SYS_NOTE
    assert "clients can change files" in notes
    assert "root on a client is root on this share (no squashing)" in notes
    assert any("wide range" in n for n in notes)
    narrow = make_share("y", {"clients": ["192.168.1.0/24"], "read-only": True})
    assert not any("wide range" in n for n in narrow.security())


def test_state_round_trip() -> None:
    shares = [make_share("media", {"clients": ["192.168.1.0/24"]})]
    assert load(dump(shares)) == shares
    assert json.loads(dump(shares))["version"] == 1


# --- the engine --------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_chown(chowns: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    return chowns


def nfs_engine(tmp_path: Path, runner: Any = None) -> Any:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb"], runner=runner or FakeRunner())
    root = Path(e.root)
    (root / "srv" / "tamarack" / "shares").mkdir(parents=True)
    (root / "etc" / "exports.d").mkdir(parents=True)
    return e


def test_first_share_starts_nfs_and_opens_the_firewall(
    tmp_path: Path, chowns: list[tuple[str, int, int]]
) -> None:
    e = nfs_engine(tmp_path)
    options = {"clients": ["192.168.1.0/24"], "uid": 1000, "gid": 1000}
    assert e.authorize_share("media", options) == "com.borealislogistics.tamarack.share"
    share = e.set_share("media", options)
    folder = Path(e.root) / "srv/tamarack/shares/media"
    assert folder.is_dir() and oct(folder.stat().st_mode & 0o7777) == "0o2775"
    assert chowns == [(str(folder), 1000, 1000)]
    exported = (Path(e.root) / "etc/exports.d/tamarack.exports").read_text()
    assert "/srv/tamarack/shares/media 192.168.1.0/24(rw," in exported
    assert e.runner.calls == [
        ("systemctl", "enable", "--now", "nfs-server.service"),
        ("systemctl", "reload", "nfs-server.service"),
        ("firewall-cmd", "--permanent", "--add-service=nfs"),
        ("firewall-cmd", "--add-service=nfs"),
    ]
    assert e.shares == [share]


def test_removing_the_last_share_keeps_files_and_closes_the_firewall(tmp_path: Path) -> None:
    e = nfs_engine(tmp_path)
    e.set_share("media", {"clients": ["192.168.1.0/24"]})
    (Path(e.root) / "srv/tamarack/shares/media/film.mkv").write_text("x")
    e.runner.calls.clear()
    e.remove_share("media")
    assert (Path(e.root) / "srv/tamarack/shares/media/film.mkv").exists()
    assert e.shares == []
    assert "/srv/" not in (Path(e.root) / "etc/exports.d/tamarack.exports").read_text()
    assert ("firewall-cmd", "--permanent", "--remove-service=nfs") in e.runner.calls
    with pytest.raises(InvalidArgument):
        e.authorize_share("media")


def test_refused_exports_are_rolled_back(tmp_path: Path) -> None:
    class Refusing(FakeRunner):
        def run(self, argv: tuple[str, ...]) -> Result:
            self.calls.append(argv)
            if argv[:2] == ("systemctl", "reload"):
                return Result(1, "exportfs: bad option\n")
            return Result(0)

    e = nfs_engine(tmp_path, Refusing())
    with pytest.raises(Exception, match="refused the new exports"):
        e.set_share("media", {"clients": ["192.168.1.0/24"]})
    assert not (Path(e.root) / "etc/exports.d/tamarack.exports").exists()
    assert e.shares == []


def test_shares_need_a_pool_a_runner_and_no_job(tmp_path: Path) -> None:
    good = {"clients": ["192.168.1.0/24"]}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(NoLayout):
        run_engine(tmp_path / "a", runner=FakeRunner()).set_share("media", good)
    with pytest.raises(NotSupported):
        run_engine(tmp_path / "b", ["/dev/sda"], runner=None).set_share("media", good)
    (tmp_path / "c").mkdir()
    e = nfs_engine(tmp_path / "c")
    e._running = "9"
    with pytest.raises(Busy):
        e.set_share("media", good)


def test_share_paths_cannot_escape(tmp_path: Path) -> None:
    e = nfs_engine(tmp_path)
    for name in ("..", "../etc", "a/b", ".hidden"):
        with pytest.raises(InvalidArgument):
            e.set_share(name, {"clients": ["10.0.0.1"]})


def test_existing_folders_are_left_as_they_are(
    tmp_path: Path, chowns: list[tuple[str, int, int]]
) -> None:
    e = nfs_engine(tmp_path)
    folder = Path(e.root) / "srv/tamarack/shares/media"
    folder.mkdir()
    folder.chmod(0o700)
    e.set_share("media", {"clients": ["10.0.0.1"], "uid": 1000})
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert chowns == []


def test_share_round_trip_type() -> None:
    assert isinstance(make_share("m", {"clients": ["10.0.0.1"]}), Share)
