<div align="center">
  <img src="assets/refindmgr.svg" width="450" alt="refindmgr logo">
  <p><strong>A simple CLI for installing and managing rEFInd themes.</strong></p>
  <p>
    <img src="https://img.shields.io/badge/version-1.2.1-8b5cf6.svg" alt="version 1.2.1">
    <img src="https://img.shields.io/badge/python-3.9%2B-3776ab.svg" alt="Python 3.9+">
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22c55e.svg" alt="MIT License"></a>
  </p>
</div>

refindmgr manages rEFInd themes without requiring manual edits to `refind.conf`. It handles theme installation, activation, variant switching, configuration backups, and diagnostics through one CLI.

## Features

- Install themes from the built-in catalog, GitHub, local folders, or ZIP files.
- Automatically detect, configure, and switch theme variants.
- Activate, deactivate, update, and remove themes from one CLI.
- Back up and restore `refind.conf` safely.
- Diagnose and configure rEFInd through guided commands.
- Safely manage firmware compatibility mode when UEFI ignores custom BootOrder entries.

## Requirements

- Linux with EFI support
- Python 3.9 or newer
- Git

## Installation

```bash
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

Install only the CLI without running rEFInd setup:

```bash
sudo ./install.sh --cli-only
```

Run the interactive menu:

```bash
sudo refindmgr
```

## Commands

| Command | Description |
| --- | --- |
| `refindmgr` | Open the interactive menu |
| `refindmgr list` | List installed and active themes |
| `refindmgr catalog` | Show the theme catalog |
| `refindmgr install <source>` | Install a theme |
| `refindmgr activate <name>` | Activate a theme |
| `refindmgr deactivate` | Return to the default rEFInd theme |
| `refindmgr remove <name>` | Remove an installed theme |
| `refindmgr variant <name>` | List or switch theme variants |
| `refindmgr backup` | Back up `refind.conf` |
| `refindmgr restore` | Restore `refind.conf` from a backup |
| `refindmgr declutter` | Simplify the rEFInd tools row |
| `refindmgr dedupe` | Inspect and reduce duplicate boot entries |
| `refindmgr clean-menu` | Create an OS-only boot menu |
| `refindmgr doctor` | Run diagnostics |
| `refindmgr setup` | Preview or install rEFInd |
| `refindmgr firmware-compat <action>` | Detect, enable, adopt, refresh, or restore firmware compatibility mode |

## License

refindmgr is available under the [MIT License](LICENSE).
