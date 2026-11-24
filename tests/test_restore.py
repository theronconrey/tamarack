# SPDX-License-Identifier: GPL-3.0-or-later
"""Browsing and restoring from snapshots, on plain folders."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tamarack.restore import (
    MAX_ENTRIES,
    NAME_MAX,
    RestoreError,
    list_folder,
    restore,
    restored_name,
    split_path,
)

LABEL = "2026-11-20 14.00"


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """A snapshot of the shares and the live shares as they are now."""
    snap, live = tmp_path / "snapshot", tmp_path / "live"
    (snap / "photos" / "2025").mkdir(parents=True)
    (snap / "photos" / "2025" / "beach.jpg").write_bytes(b"sand" * 1000)
    (snap / "photos" / "notes.txt").write_text("hello")
    os.chmod(snap / "photos" / "notes.txt", 0o4640)  # set-user-ID: dropped on restore
    os.utime(snap / "photos" / "notes.txt", (1_700_000_000, 1_700_000_000))
    (snap / "photos" / "2025" / "latest").symlink_to("beach.jpg")
    (snap / "photos" / "escape").symlink_to("/etc")
    (snap / ".snapshots").mkdir()  # never listed at the top
    (live / "photos").mkdir(parents=True)
    return snap, live


@pytest.mark.parametrize("path", ["/etc", "../x", "a/../../b", "a/./b", "a\0b", ".snapshots/1"])
def test_paths_that_leave_the_shares_are_refused(path: str) -> None:
    with pytest.raises(RestoreError):
        split_path(path)


def test_split_path() -> None:
    assert split_path("") == []
    assert split_path("photos//2025/") == ["photos", "2025"]


def test_list_folder_shows_folders_first_and_never_follows_links(
    trees: tuple[Path, Path],
) -> None:
    snap, _ = trees
    top = list_folder(str(snap), [])
    assert [e["name"] for e in top["entries"]] == ["photos"]  # .snapshots hidden
    inside = list_folder(str(snap), ["photos"])["entries"]
    assert [(e["name"], e["kind"]) for e in inside] == [
        ("2025", "folder"),
        ("escape", "link"),
        ("notes.txt", "file"),
    ]
    assert inside[2]["size"] == 5 and inside[2]["modified"] == 1_700_000_000
    with pytest.raises(RestoreError, match="not a folder"):
        list_folder(str(snap), ["photos", "escape"])  # a symlink to /etc
    with pytest.raises(RestoreError, match="not there"):
        list_folder(str(snap), ["nope"])


def test_long_folders_are_cut_short(tmp_path: Path) -> None:
    for i in range(MAX_ENTRIES + 3):
        (tmp_path / f"f{i}").touch()
    listing = list_folder(str(tmp_path), [])
    assert listing["truncated"] and len(listing["entries"]) == MAX_ENTRIES


def test_restored_names() -> None:
    assert restored_name("notes.txt", LABEL, 1, False) == "notes (from 2026-11-20 14.00).txt"
    assert restored_name("notes.txt", LABEL, 3, False) == "notes (from 2026-11-20 14.00 3).txt"
    assert restored_name("my.folder", LABEL, 1, True) == "my.folder (from 2026-11-20 14.00)"
    assert restored_name(".bashrc", LABEL, 1, False) == ".bashrc (from 2026-11-20 14.00)"
    long = restored_name("é" * 200 + ".jpg", LABEL, 1, False)
    assert len(long.encode()) <= NAME_MAX and long.endswith(" (from 2026-11-20 14.00).jpg")


def test_restore_a_file_next_to_the_original(trees: tuple[Path, Path]) -> None:
    snap, live = trees
    (live / "photos" / "notes.txt").write_text("changed by mistake")
    new = restore(str(snap), str(live), ["photos", "notes.txt"], LABEL)
    assert new == "photos/notes (from 2026-11-20 14.00).txt"
    restored = live / new
    assert restored.read_text() == "hello"
    assert (live / "photos" / "notes.txt").read_text() == "changed by mistake"  # untouched
    st = restored.stat()
    assert stat.S_IMODE(st.st_mode) == 0o640  # set-user-ID dropped
    assert st.st_mtime == 1_700_000_000 and st.st_uid == os.geteuid()
    # Restoring again never overwrites the first copy.
    again = restore(str(snap), str(live), ["photos", "notes.txt"], LABEL)
    assert again == "photos/notes (from 2026-11-20 14.00 2).txt"
    assert restored.read_text() == "hello"


def test_restore_a_folder_copies_links_as_links(trees: tuple[Path, Path]) -> None:
    snap, live = trees
    new = restore(str(snap), str(live), ["photos", "2025"], LABEL)
    folder = live / new
    assert (folder / "beach.jpg").read_bytes() == b"sand" * 1000
    assert (folder / "latest").is_symlink() and os.readlink(folder / "latest") == "beach.jpg"


def test_a_missing_live_folder_is_made_again(trees: tuple[Path, Path]) -> None:
    snap, live = trees
    (live / "photos").rmdir()
    new = restore(str(snap), str(live), ["photos", "2025", "beach.jpg"], LABEL)
    assert new == "photos/2025/beach (from 2026-11-20 14.00).jpg"
    assert (live / new).read_bytes() == b"sand" * 1000


def test_a_symlink_in_the_live_share_stops_the_restore(
    trees: tuple[Path, Path], tmp_path: Path
) -> None:
    snap, live = trees
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (live / "photos").rmdir()
    (live / "photos").symlink_to(elsewhere)  # a client swapped the folder for a link
    with pytest.raises(RestoreError, match="not a folder any more"):
        restore(str(snap), str(live), ["photos", "notes.txt"], LABEL)
    assert list(elsewhere.iterdir()) == []


def test_links_and_the_whole_snapshot_are_not_restored(trees: tuple[Path, Path]) -> None:
    snap, live = trees
    with pytest.raises(RestoreError, match="not a file or folder"):
        restore(str(snap), str(live), ["photos", "escape"], LABEL)
    with pytest.raises(RestoreError, match="not a folder"):
        restore(str(snap), str(live), ["photos", "escape", "passwd"], LABEL)
    with pytest.raises(RestoreError, match="whole snapshot"):
        restore(str(snap), str(live), [], LABEL)
    with pytest.raises(RestoreError, match="not in this snapshot"):
        restore(str(snap), str(live), ["photos", "gone.txt"], LABEL)
