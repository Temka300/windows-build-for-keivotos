"""Remove stray Windows Zone.Identifier files from a copied checkout.

Preview: python scripts/clean_zone_identifiers.py
Delete:  python scripts/clean_zone_identifiers.py --apply
Elsewhere: python scripts/clean_zone_identifiers.py PATH [--apply]

Includes hidden folders such as .git. Matches ordinary colons and the U+F03A
colon used by Windows/WSL filename translation, case-insensitively. Only
regular files with the exact suffix are removed; original files, directories,
symlinks, junctions and Git tracking are preserved. Native NTFS alternate
data streams are not enumerated by this tool.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys


SUFFIXES = (":zone.identifier", "\uf03azone.identifier")


def is_zone_identifier(name: str) -> bool:
    return name.casefold().endswith(SUFFIXES)


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def find_zone_identifiers(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Cleanup root must be a directory.")
    matches = []

    def fail(error: OSError) -> None:
        raise error

    for folder, directories, files in os.walk(root, onerror=fail, followlinks=False):
        parent = Path(folder)
        directories[:] = sorted(
            name for name in directories
            if not is_link((parent / name).lstat())
        )
        for name in sorted(files):
            if not is_zone_identifier(name):
                continue
            candidate = parent / name
            info = candidate.lstat()
            if stat.S_ISREG(info.st_mode) and not is_link(info):
                matches.append(candidate.relative_to(root))
    return sorted(matches)


def check_candidate(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or not is_zone_identifier(relative.name):
        raise ValueError(f"Refusing to delete an unexpected path: {relative}")
    candidate = root / relative
    for parent in relative.parents:
        if is_link((root / parent).lstat()):
            raise ValueError(f"Cleanup parent became a link: {relative}")
    info = candidate.lstat()
    if (
        is_link(info)
        or not stat.S_ISREG(info.st_mode)
        or candidate.resolve(strict=True) != candidate
    ):
        raise ValueError(f"Cleanup candidate changed or is not a regular file: {relative}")
    return candidate


def delete_zone_identifiers(root: Path, matches: list[Path]) -> None:
    root = root.resolve(strict=True)
    # Validate the whole list before deleting any file, then recheck each one.
    for relative in matches:
        check_candidate(root, relative)
    for relative in matches:
        check_candidate(root, relative).unlink()
        print(f"Deleted: {relative}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1],
        help="Folder to scan recursively; defaults to this build checkout."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Delete the listed metadata files."
    )
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        matches = find_zone_identifiers(root)
        print(f"Root: {root}")
        print(f"{len(matches)} Zone.Identifier file(s) found.")
        for relative in matches:
            print(f"Metadata: {relative}")
        if args.apply:
            delete_zone_identifiers(root, matches)
            print(f"Cleanup complete: {len(matches)} metadata file(s) deleted.")
        else:
            print("Preview only. Run again with --apply to delete these metadata files.")
        print("Tracked files still need their deletions committed by you to fix checkout.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Cleanup stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
