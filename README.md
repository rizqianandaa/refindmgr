# refindmgr

**refindmgr** is an open-source CLI for managing [rEFInd](https://www.rodsbooks.com/refind/)
boot menu themes — install, switch, and remove themes without ever touching the EFI
partition or hand-editing `refind.conf`.

It now also ships with a friendly **interactive menu** — just run `refindmgr` with no
arguments and pick what you want from a list, no flags to memorize.

## Features

- **Interactive menu** — run `refindmgr` with no arguments to get a numbered menu with
  a live status banner (rEFInd detected? which theme is active? running as root?).
- **Curated theme catalog** — install popular themes with a single short key.
- **Install themes from anywhere** — git URL, local folder, or a local `.zip` file.
- **Switch the active theme** in one command, no manual `refind.conf` editing.
- **Automatic backups** every time `refind.conf` is changed, plus a `restore` command
  to roll back at any time.
- **Theme name validation** that blocks path traversal (`../../etc` and similar are
  always rejected).
- **`refindmgr setup`** — installs rEFInd itself (via the system package manager plus
  the official `refind-install` script) if it isn't present yet. `./install.sh` now
  runs this automatically, so a fresh clone gets both refindmgr *and* rEFInd ready to
  go in one step.
- **`refindmgr declutter`** — tidies up the boot screen so it only shows your OS
  list plus **Shutdown** and **Reboot**, hiding tool icons like shell, MOK
  management, memtest, "about", hidden-tags, firmware setup, etc. Fully
  reversible with `refindmgr declutter --undo` or `restore`.
- **`refindmgr doctor`** — a one-shot diagnostic to confirm everything is detected
  correctly before you change anything.
- Never touches the boot loader/NVRAM directly — it only manages the `themes/` folder
  and the `include` lines inside `refind.conf`.

## Installation

Requires `python3` and its `venv` module (bundled with most distros; on Debian/Ubuntu
install it with `sudo apt-get install -y python3-venv` if it's missing).

```bash
git clone <this-repo>
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

Boot screen
  9) Tidy up the boot screen (OS list + Shutdown + Reboot only)
  10) Restore default tool icons

System
  11) Diagnostics (doctor)
  12) Install rEFInd itself (setup)

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

## All commands

| Command | Needs sudo? | What it does |
|---|---|---|
| `refindmgr` (no args) | no | Opens the interactive menu |
| `refindmgr doctor` | no | Diagnostics: rEFInd folder, git, root access |
| `refindmgr setup [--yes]` | yes (with `--yes`) | Installs rEFInd itself if missing |
| `refindmgr catalog` | no | Browse the curated theme catalog |
| `refindmgr list` | no | Show installed & active themes |
| `refindmgr install <source> [--activate] [--name NAME]` | yes | Install a theme (catalog/git URL/folder/`.zip`) |
| `refindmgr activate <name>` | yes | Make `<name>` the active theme |
| `refindmgr deactivate` | yes | Deactivate all themes (back to default look) |
| `refindmgr remove <name>` | yes | Remove an installed theme |
| `refindmgr backup` | yes | Save a copy of the current `refind.conf` |
| `refindmgr restore [--backup PATH]` | yes | Restore `refind.conf` from a backup |
| `refindmgr declutter [--undo]` | yes | Show only the OS list + Shutdown + Reboot on the boot screen (`--undo` reverts) |

Every command accepts `--refind-dir /path/to/EFI/refind` (before or after the
command name) to set the rEFInd folder location manually, or set it once via an
environment variable:

```bash
export REFIND_DIR=/boot/efi/EFI/refind
```

## Tidying up the boot screen (`declutter`)

A very common rEFInd complaint: right after install, the boot screen is full of
tool icons you'll basically never use (EFI shell, MOK management, memtest, "about",
hidden-tags recovery, firmware setup, fwupdate, ...) in addition to your actual OS
entries. `refindmgr declutter` fixes that in one step:

```bash
sudo refindmgr declutter          # keep only the OS list + Shutdown + Reboot
sudo refindmgr declutter --undo   # bring back rEFInd's default tool icons
```

What it actually changes in `refind.conf` (both are one-line, well-documented
rEFInd options — see the [official config reference](https://www.rodsbooks.com/refind/configfile.html)):

- `showtools shutdown,reboot` — only the Shutdown and Reboot icons appear on the
  tools row; everything else (shell, memtest, gdisk, mok_tool, about, hidden_tags,
  firmware, fwupdate, ...) is hidden.
- `scanfor internal,external,optical,manual` — keeps normal OS detection but drops
  the `firmware` scan source, which is what causes stray firmware-boot-list entries
  to show up as extra tags.

It does **not** touch `dont_scan_dirs`/`dont_scan_volumes` (which control which OS
entries get detected), since what counts as a "duplicate" or "junk" OS entry is
specific to what's actually on your disk — refindmgr won't guess and risk hiding a
boot loader you actually need. If you still see duplicate/unwanted OS entries after
`declutter`, that's a disk-cleanup problem (leftover EFI files from old installs),
not something a config flag can safely auto-fix.

Like every other write here, it backs up `refind.conf` automatically first, so
`declutter --undo` or `refindmgr restore` always gets you back to where you started.

> Known upstream quirk: some rEFInd 0.14.2+ builds have a reported bug where tool
> icons can appear duplicated on screen regardless of `showtools` — that's a rEFInd
> issue, not a refindmgr one. If you hit it, check for a rEFInd update/downgrade.

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

## Security & backups

- Never touches `refind_x64.efi`/`refind_ia32.efi`/`refind_aa64.efi` or any other boot
  loader/NVRAM files directly — the only operation at that level (`setup --yes`) is
  fully delegated to the official `refind-install` script.
- Every `refind.conf` change automatically creates a timestamped backup
  (`refind.conf.<timestamp>.bak`) before writing anything.
- Theme names are validated as safe folder names (path traversal is always rejected).

## Troubleshooting

**`sudo ./install.sh` → `Permission denied (os error 13)`.** This means `install.sh`
lost its executable bit — it's a file-permissions problem, not a bug in the script
itself, so you're not the only one who can hit this. It commonly happens when the repo
was downloaded as a `.zip` (GitHub's "Download ZIP" button, or any zip/tarball that
doesn't preserve Unix file permissions) instead of `git clone`. Fix it with either:

```bash
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

or skip the executable bit entirely by invoking it through bash directly:

```bash
sudo bash install.sh
```

**`sudo refindmgr ...` → `command not found`.** This usually happens if refindmgr was
installed manually into a virtualenv (instead of via `install.sh`), whose `PATH` isn't
carried over to the fresh shell that `sudo` spawns. Fix: `sudo ./install.sh`.

**`Permission denied` without `sudo`.** This is intentional — the EFI partition can
only be written by root. Add `sudo` for commands that change something.

**rEFInd folder not detected automatically.** Check your EFI partition's location
(`lsblk`/`sudo blkid`), then set it manually with `--refind-dir` or `REFIND_DIR`.

## Project structure

```
refindmgr/
├── refindmgr/
│   ├── paths.py     # Detects the rEFInd folder
│   ├── conf.py      # Safely reads/edits refind.conf + backups
│   ├── themes.py    # Install/remove/list themes (git, folder, zip) + name validation
│   ├── catalog.py   # Curated theme catalog
│   ├── system.py    # Detects & helps install rEFInd itself
│   └── cli.py       # Command-line interface + interactive menu
├── tests/           # Unit tests (66 tests)
├── install.sh       # One-shot install of refindmgr + rEFInd (needs sudo)
├── uninstall.sh     # Removes refindmgr from the system (needs sudo)
└── pyproject.toml   # Package metadata & the `refindmgr` command
```

## Development

```bash
python3 -m venv env && source env/bin/activate
pip install -e .
python3 -m unittest discover -s tests -v   # 66 tests
```

## Roadmap

1. ~~CLI (`refindmgr`)~~ — done, including the interactive menu, 66 tests passing.
2. Simple GUI on top of the same underlying logic (`cli.py` gets a GUI layer; the
   other modules stay unchanged).

## License

MIT License — see [`LICENSE`](LICENSE).
