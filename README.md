# refindmgr

**refindmgr** is an open-source CLI for managing [rEFInd](https://www.rodsbooks.com/refind/)
boot menu themes — install, switch, and remove themes without ever touching the EFI
partition or hand-editing `refind.conf`.

It now also ships with a friendly **interactive menu** — just run `refindmgr` with no
arguments and pick what you want from a list, no flags to memorize.

## Installation

Requires `python3` and its `venv` module (bundled with most distros; on Debian/Ubuntu
install it with `sudo apt-get install -y python3-venv` if it's missing).

```bash
git clone https://github.com/rizqianandaa/refindmgr.git
cd refindmgr
sudo ./install.sh
```

`install.sh` does four things automatically:

1. Creates an isolated Python environment in `/opt/refindmgr` (never touches your
   system Python/pip).
2. Installs refindmgr into that environment.
3. Installs the `refindmgr` command into `/usr/local/bin`, so it can be called from
   anywhere — including through `sudo` — with no manual `venv`/`pip install` step.
4. **Installs rEFInd itself** if it isn't already on the system (detects your package
   manager, installs the `refind` package, then runs the official `refind-install`
   script). If rEFInd is already installed, this step is a safe no-op. If your distro
   isn't supported yet, install.sh still finishes successfully and just prints
   instructions for installing rEFInd manually afterwards.

To uninstall:

```bash
sudo ./uninstall.sh
```

(Themes already installed on the EFI partition are left untouched — run
`refindmgr remove <name>` first if you want a fully clean removal.)

## Quick start

The easiest way to use refindmgr is the interactive menu:

```bash
refindmgr
```

That opens a menu like this:

```
========================================================
  refindmgr -- rEFInd Theme Manager
========================================================
  [ok] rEFInd detected: /boot/efi/EFI/refind
  [ok] 2 themes installed (active: rEFInd-minimal)
  [ok] Running as root
========================================================

Themes
  1) Show installed & active themes
  2) Browse the theme catalog
  3) Install a new theme
  4) Activate a theme
  5) Deactivate all themes
  6) Remove a theme

refind.conf backups
  7) Create a backup now
  8) Restore from a backup

System
  9) Diagnostics (doctor)
  10) Install rEFInd itself (setup)

  0) Quit
```

Just type a number and follow the prompts — refindmgr asks for whatever it needs
(theme source, theme name, confirmations) and shows the same messages as the direct
commands below.

Prefer scripting or one-off commands instead? All subcommands still work directly:

```bash
refindmgr doctor                          # confirm rEFInd is detected
refindmgr catalog                         # browse theme choices
sudo refindmgr install minimal --activate # install + activate
```

Reboot to see the new theme in the boot menu.

> Read-only commands (`list`, `catalog`, `doctor`) don't need `sudo`.
> Commands that write to the EFI partition (`install`, `activate`, `deactivate`,
> `remove`, `backup`, `restore`, `setup --yes`) need `sudo`.


Every command accepts `--refind-dir /path/to/EFI/refind` (before or after the
command name) to set the rEFInd folder location manually, or set it once via an
environment variable:

```bash
export REFIND_DIR=/boot/efi/EFI/refind
```

### Supported theme sources

```bash
sudo refindmgr install minimal                          # catalog key
sudo refindmgr install https://github.com/user/repo     # git URL
sudo refindmgr install ./already-downloaded-theme        # local folder
sudo refindmgr install ./theme.zip                        # local .zip file
```

Find many more theme options (140+) at
[refind-themes-collection](https://refind-themes-collection.netlify.app/).

## Don't have rEFInd yet?

`sudo ./install.sh` now installs rEFInd for you automatically as its last step, so in
most cases you don't need to think about this at all. If you skipped that, or want to
(re)run it manually, `refindmgr setup` can do it on its own:

```bash
refindmgr setup            # preview: show what would happen, no changes made
sudo refindmgr setup --yes # actually run the rEFInd installation
```

Behind the scenes: it detects your system's package manager (apt/dnf/pacman/zypper),
installs the `refind` package through it, then runs the **official** `refind-install`
script from the rEFInd project itself. refindmgr never writes to the EFI
partition/NVRAM with its own logic for this step. Other distros: follow the
[official rEFInd install guide](https://www.rodsbooks.com/refind/installing.html).

## Troubleshooting

**`sudo refindmgr ...` → `command not found`.** This usually happens if refindmgr was
installed manually into a virtualenv (instead of via `install.sh`), whose `PATH` isn't
carried over to the fresh shell that `sudo` spawns. Fix: `sudo ./install.sh`.

**`Permission denied` without `sudo`.** This is intentional — the EFI partition can
only be written by root. Add `sudo` for commands that change something.

**rEFInd folder not detected automatically.** Check your EFI partition's location
(`lsblk`/`sudo blkid`), then set it manually with `--refind-dir` or `REFIND_DIR`.

## License

MIT License — see [`LICENSE`](LICENSE).
