# Linux portable builds

Keivotos is a local browser application. Linux delivery is an extracted x86_64
folder containing the frozen application, Python runtime, frontend and declared
tools. End users need no uv, Python installation, Node.js, sudo or container.
Source/developer requirements are separate in [source.md](source.md).

## Build

On Linux x86_64 or x64 WSL, with Linux uv and Node.js available:

```sh
bash scripts/release/build_linux.sh --target linux
```

The builder uses a temporary source/dependency workspace, the existing locks,
frontend checks/build, freezer specs and delivery catalog. Native tools/resources
and isolated HTTP startup are checked before creating the archive. It produces:

- `Keivotos-V<version>-linux-x64.tar.gz` and `.tar.gz.sha256`.

Linux ZIP output is no longer produced. The archive is made before any user profile is started
inside the package. Existing outputs are refused. `--output-directory` selects a
new directory inside the checkout. No user library, database or credentials belong
in a portable artifact. The build reads its version from `backend/product.py`.

The package adds `start-keivotos.sh`, `start-portable.sh`, `install-desktop.sh`,
the desktop template, `linux-tools.txt` derived from the installed modules' delivery
catalog, and a concise `LINUX-START-HERE.txt`. A physically absent optional module
does not create a required tool entry. Startup preserves the application's existing
data resolver and passes arguments/exit status through.

## Host baseline and verification limits

The hosted workflow builds on Ubuntu 22.04 x64 so its glibc baseline remains as
old as that build host. A package built locally on a newer distribution can require
newer host libraries; archive format and a bundled Python do not remove this limit.
Use a compatible native Linux filesystem and extract all files together.

Local automated package/permissions checks have been exercised under Ubuntu 26.04
WSL2. This does not certify Ubuntu 22.04/24.04 or Fedora desktop behavior, GNOME/KDE
menu launching, browser auto-opening, removable NTFS/exFAT media or Secret Service.
Those require native acceptance of the actual artifact on each claimed platform.
The hosted workflow's configured baseline is not itself a completed build result.

Installed key storage requires an unlocked Secret Service vault in the same session;
there is no plaintext fallback. Do not use a Windows profile as a Linux migration
shortcut. See [installation](../user/installation.md) for data modes and permissions,
and [remote builds](remote.md) for provenance/checksum verification.

## Optional desktop entry

The installer writes only a new user-level application-menu entry, when explicitly
invoked. It never installs system packages, starts the application, sets autostart,
overwrites an existing shortcut or changes user media. The entry opens a terminal
for the server. Its location depends on `XDG_DATA_HOME`; it keeps the extracted
application's absolute path, so moving that folder requires a new shortcut.
