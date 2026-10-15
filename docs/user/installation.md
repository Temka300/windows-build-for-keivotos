# Installation

## Portable Windows build

1. Verify the downloaded ZIP against its adjacent `.sha256` file when one is provided.
2. Extract the entire folder to a normal writable location.
3. Run `Keivotos.exe`.

By default data is kept under `%LOCALAPPDATA%\Keivotos`. To keep the data in portable mode, add an empty `portable.txt` beside `Keivotos.exe` then everrything is written to a `data\` subfolder of the portable folder.

## Source build

See [`../build/source.md`](../build/source.md). Both a Git clone and an extracted source archive can start through `run.bat` when uv and Node.js are available.

## Portable Linux build

Linux artifacts target x86_64. Download the `.tar.gz` and its `.sha256`, verify
with `sha256sum -c <archive>.tar.gz.sha256`, then extract with
`tar -xzf <archive>.tar.gz`. Replace `<archive>` with the downloaded filename
without its `.tar.gz` suffix. Keep the entire extracted folder together.
The compatibility ZIP and its checksum contain the same application files.

In a terminal inside the extracted folder, choose one launch:

```sh
sh ./start-keivotos.sh
```

Normal launch uses `$XDG_DATA_HOME/Keivotos`, or `~/.local/share/Keivotos`.
For data beside the application, use:

```sh
sh ./start-portable.sh
```

Portable launch chooses the adjacent `data/` folder. Both launches honor an
explicit `KEIVOTOS_HOME` and existing empty/custom `portable.txt` settings;
they do not move or convert an existing profile. Keep the terminal open while
using the app; Ctrl+C stops it. The browser opens when ready at
<http://localhost:52325/>. `--no-browser` keeps it closed; `--port N` chooses a port.

No uv, Python, Node.js or administrator access is needed to run the package.
Your Linux system still needs compatible host libraries. The build baseline and
verification limits are in [Linux builds](../build/linux.md).

To check execution and write permissions without starting the server, run
`sh ./start-portable.sh --launcher-check`. Standard tar extraction retains execute
permissions. If an extractor removes them, allow execution for `Keivotos` and
each tool listed in `linux-tools.txt`, or run `chmod u+x` on those files.
If the drive is mounted `noexec`, copy the entire folder to an executable location
under your Linux home; `chmod` cannot change a mount restriction. The launcher
reports missing files and unwritable data folders without replacing existing data.

An optional user-level menu shortcut is created only when you run
`sh ./install-desktop.sh`, or `sh ./install-desktop.sh --portable`. It opens a
terminal for the server and preserves existing shortcuts by refusing replacement.
Keep the application folder in place after installing its shortcut.

Saving Danbooru credentials requires an unlocked Secret Service vault in the
same Linux desktop session; environment overrides remain available. Portable
profiles do not make Windows-encrypted or Linux vault credentials transferable.
See [troubleshooting](troubleshooting.md) for host/permission diagnostics.
