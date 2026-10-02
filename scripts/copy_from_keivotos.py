"""Refresh this build checkout from its sibling Keivotos checkout.

Preview: python scripts/copy_from_keivotos.py
Copy:    python scripts/copy_from_keivotos.py --apply

Use --source PATH when Keivotos is elsewhere. Existing project files are
updated; destination-only files are retained. Git metadata, Windows zone
metadata, local settings, library data and dependency/cache folders are skipped.
The one-time .git/info/exclude setup is intentionally separate from this tool.
"""

from __future__ import annotations

import argparse
import filecmp
import os
from pathlib import Path
import shutil
import stat
import tempfile


HELPER = Path("scripts/copy_from_keivotos.py")
SKIP_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".svelte-kit",
}
SKIP_ROOT_NAMES = {
    "config.json", "run-lan.local.bat", "data", "library", "library_test",
    "_gallery-dl", "_gallery_dl", "gallery-dl", "artifacts", "downloads",
    "backups", "sidecars", "sidecar_history", "thumbnails", "build", "dist",
}


def excluded(relative: Path) -> bool:
    names = tuple(part.casefold() for part in relative.parts)
    return (
        names == tuple(part.casefold() for part in HELPER.parts)
        or any(name in SKIP_NAMES for name in names)
        or names[0] in SKIP_ROOT_NAMES
        or any(name.endswith(":zone.identifier") for name in names)
        or any(name == ".env" or name.startswith(".env.") for name in names)
        or names[-1].endswith((
            ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
            ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal",
        ))
        or names[:3] in (("packaging", "windows", "build"),
                         ("packaging", "windows", "dist"))
        or names[:3] == ("scripts", "local", "state")
    )


def check_target(destination: Path, relative: Path) -> Path:
    target = destination
    for index, part in enumerate(relative.parts):
        target = target / part
        if target.is_symlink():
            raise ValueError(f"Destination symlink would be overwritten: {target}")
        if target.exists():
            expected_directory = index < len(relative.parts) - 1
            if expected_directory and not target.is_dir():
                raise ValueError(f"Destination parent is not a directory: {target}")
            if not expected_directory and not target.is_file():
                raise ValueError(f"Destination is not a regular file: {target}")
    if not target.resolve().is_relative_to(destination):
        raise ValueError(f"Destination escapes the build checkout: {target}")
    return target


def plan_copy(source: Path, destination: Path) -> list[Path]:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=True)
    if not source.is_dir() or not destination.is_dir():
        raise ValueError("Source and destination must be directories.")
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("Source and destination must be separate, non-nested folders.")
    if not all((source / name).is_file() for name in ("app.py", "pyproject.toml")):
        raise ValueError("Source must be a Keivotos project containing app.py and pyproject.toml.")
    changes = []

    def fail(error: OSError) -> None:
        raise error

    for folder, directories, files in os.walk(source, onerror=fail, followlinks=False):
        parent = Path(folder)
        directories[:] = sorted(
            name for name in directories
            if not excluded((parent / name).relative_to(source))
        )
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError(f"Source directory is a symlink: {parent / name}")
        for name in sorted(files):
            original = parent / name
            relative = original.relative_to(source)
            if excluded(relative):
                continue
            if original.is_symlink() or not original.is_file():
                raise ValueError(f"Source is not a regular file: {original}")
            target = check_target(destination, relative)
            if (target.exists() and filecmp.cmp(original, target, shallow=False)
                    and stat.S_IMODE(original.stat().st_mode)
                    == stat.S_IMODE(target.stat().st_mode)):
                continue
            changes.append(relative)
    return changes


def copy_files(source: Path, destination: Path, changes: list[Path]) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=True)
    for relative in changes:
        if excluded(relative):
            raise ValueError(f"Refusing to copy a protected path: {relative}")
        original = source / relative
        if original.is_symlink() or original.resolve(strict=True) != original:
            raise ValueError(f"Source changed since preview: {original}")
        target = check_target(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".keivotos-copy-", dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(original, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    destination = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=destination.parent / "Keivotos")
    parser.add_argument("--apply", action="store_true", help="Copy the previewed project files.")
    args = parser.parse_args()
    try:
        source = args.source.resolve(strict=True)
        changes = plan_copy(source, destination)
        print(f"Source: {source}\nDestination: {destination}")
        for relative in changes:
            action = "Update" if (destination / relative).exists() else "Add"
            print(f"{action}: {relative}")
        print(f"{len(changes)} file(s) to copy. No destination files will be deleted.")
        if args.apply:
            copy_files(source, destination, changes)
            print("Copy complete. Git metadata and local data were preserved.")
        else:
            print("Preview only. Run again with --apply to copy.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Copy stopped: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
