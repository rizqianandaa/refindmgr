<div align="center">
  <img src="assets/refindmgr.svg" width="450" alt="refindmgr logo">
  <p><strong>A simple CLI for installing and managing rEFInd themes.</strong></p>
  <p>
    <img src="https://img.shields.io/badge/version-1.6.0-8b5cf6.svg" alt="version 1.6.0">
    <img src="https://img.shields.io/badge/python-3.9%2B-3776ab.svg" alt="Python 3.9+">
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22c55e.svg" alt="MIT License"></a>
  </p>
</div>

refindmgr manages rEFInd themes without requiring manual edits to `refind.conf`. It provides one interactive CLI for installing themes, switching variants, simplifying the boot menu, creating backups, and checking the active UEFI setup.

## Features

- Install themes from the built-in catalog, public GitHub repositories, ZIP files, or local folders.
- Detect and switch theme variants without reinstalling the theme.
- Activate, deactivate, update, and remove themes through one CLI.
- Build an OS-only menu from verified EFI loaders instead of guessing from icons or filenames alone.
- Back up and restore `refind.conf` before configuration changes.
- Diagnose multi-ESP and firmware compatibility setups without changing the boot configuration.
- Preview sensitive NVRAM operations and provide recovery and rollback commands.

## Requirements

- Linux running in UEFI mode
- Python 3.9 or newer
- Git
- Root access for operations that write to the EFI System Partition or NVRAM
- Optional: `chafa` for catalog previews on terminals without the Kitty or iTerm2 image protocols

## Installation

Download or clone the project, then run:

```bash
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

The installer performs a safety check before configuring rEFInd. If the boot layout is ambiguous, the CLI remains available but automatic bootloader setup is stopped.

Install only the CLI without running rEFInd setup:

```bash
sudo ./install.sh --cli-only
```

Remove refindmgr:

```bash
sudo ./uninstall.sh
```

## Quick start

Open the interactive menu:

```bash
sudo refindmgr
```

The interactive menu is the recommended interface for normal use. It covers theme installation, activation, variant switching, backups, OS-only menu configuration, and diagnostics.

List installed themes:

```bash
refindmgr list
```

Install and activate a catalog theme:

```bash
sudo refindmgr install <catalog-key> --activate
```

Install from a public GitHub repository, ZIP file, or local folder:

```bash
sudo refindmgr install <source> --activate
```

List or switch variants:

```bash
refindmgr variant <theme>
sudo refindmgr variant <theme> --set <variant>
```

## OS detection and boot menu

refindmgr can inspect the EFI loaders on the active ESP and identify Windows and supported Linux layouts, including shim, GRUB, systemd-boot, and Unified Kernel Images. It also checks the EFI architecture and avoids selecting a loader that is actually another copy of rEFInd.

Show the detected operating systems:

```bash
refindmgr os list
```

Run the read-only loader health check:

```bash
sudo refindmgr os doctor
```

Create a trusted baseline after installation or after intentionally updating boot loaders:

```bash
sudo refindmgr os baseline --apply
```

Later `os doctor` runs compare the current shim, GRUB, rEFInd, Windows loader, and direct-boot files with that baseline. If a system update restores the vendor shim over a managed compatibility path, refindmgr reports it and offers an explicit backed-up reapply instead of overwriting it silently.

Preview an OS-only menu:

```bash
sudo refindmgr clean-menu --auto
```

Apply it only after reviewing the detected loaders:

```bash
sudo refindmgr clean-menu --auto --apply
```

Unknown or conflicting loaders are not selected automatically. Systems using managed firmware compatibility mode use the same dynamic inventory while keeping direct Linux boot, backups, and rollback safeguards.

See [support status](docs/SUPPORT.md) for the distro, architecture, firmware, and real-hardware validation matrix.

## Diagnostics and recovery

Standard diagnostics:

```bash
refindmgr doctor
```

Inspect the active boot chain and additional ESPs read-only:

```bash
sudo refindmgr doctor --forensic --scan-unmounted
```

Export a redacted diagnostic report:

```bash
sudo refindmgr doctor --forensic --scan-unmounted --export
```

Create and validate a recovery bundle:

```bash
sudo refindmgr recovery create --scan-unmounted
sudo refindmgr recovery validate --bundle <recovery.zip>
```

Commands that change BootNext, BootOrder, or NVRAM require explicit options and are preview-only by default. Use their `--help` output before applying a change:

```bash
refindmgr boot-test --help
refindmgr nvram-cleanup --help
refindmgr firmware-compat --help
```

## Command reference

### Themes

| Command | Description |
| --- | --- |
| `refindmgr list` | List installed and active themes |
| `refindmgr catalog` | Show the built-in catalog |
| `refindmgr install <source>` | Install a theme |
| `refindmgr activate <name>` | Activate an installed theme |
| `refindmgr deactivate` | Return to the default rEFInd theme |
| `refindmgr remove <name>` | Remove an installed theme |
| `refindmgr variant <name>` | List or switch theme variants |

### Configuration and boot

| Command | Description |
| --- | --- |
| `refindmgr backup` | Back up `refind.conf` |
| `refindmgr restore` | Restore `refind.conf` from a backup |
| `refindmgr declutter` | Simplify the rEFInd tools row |
| `refindmgr clean-menu` | Preview or create an OS-only menu |
| `refindmgr os list` | List detected OS loaders |
| `refindmgr os doctor` | Check loader identity and architecture |
| `refindmgr os baseline` | Preview or save a loader-health baseline |

### Diagnostics and recovery

| Command | Description |
| --- | --- |
| `refindmgr doctor` | Run diagnostics or export a report |
| `refindmgr preflight` | Check whether automatic setup is safe |
| `refindmgr setup` | Preview or install rEFInd |
| `refindmgr firmware-compat <action>` | Manage firmware compatibility mode |
| `refindmgr boot-test <action>` | Test BootNext and BootOrder across reboots |
| `refindmgr recovery <action>` | Create or validate a recovery bundle |
| `refindmgr nvram-cleanup <action>` | Preview, remove, or restore one verified NVRAM entry |

Use `refindmgr <command> --help` for the options accepted by a command.

## Diagnostic log

refindmgr keeps user-facing output concise while recording technical operations such as theme staging, clone results, copies, installs, removals, and failures in a rotating plain-text log.

- Commands run as root use `/var/log/refindmgr/refindmgr.log`.
- Commands run without root use `$XDG_STATE_HOME/refindmgr/refindmgr.log`, or `~/.local/state/refindmgr/refindmgr.log` when `XDG_STATE_HOME` is unset.
- The active log is limited to 2 MiB with three rotated backups.
- Log files use mode `0600`.
- Credential-bearing URLs, common secret query parameters, email addresses, and the user's home path are redacted.

Logging is best-effort: an unavailable log directory never blocks a theme or diagnostic command. Terminal messages remain the primary user interface; the log is supporting evidence for troubleshooting.

The interactive menu uses a thin Unicode rule and the `Pilih menu ❯` prompt on a compatible TTY. Non-TTY and non-Unicode environments fall back to plain `-` and `>` output. Existing command headings such as `=== Diagnostics ===` remain plain text for readable redirected reports.

## Catalog previews

The theme catalog shows every theme at once, each title with its own thumbnail beside it. Pick a number to install.

Entries are laid out in a **grid**, not a single column. Stacking eight thumbnails vertically means the terminal's height divided by eight is what caps the size of each one, while most of a wide terminal sits empty; two columns halve the grid rows and roughly double the thumbnail. refindmgr picks the column count that makes the previews largest while still fitting everything on one screen — typically two columns on a normal window and one on a narrow one. A terminal too narrow for a title and a thumbnail side by side falls back to a plain numbered list.

refindmgr probes the terminal once at startup and picks the best image backend it can actually display:

| Backend | Quality | Terminals | Needs a renderer? |
|---|---|---|---|
| Kitty graphics protocol | 24-bit, pixel-exact | kitty, Ghostty, WezTerm, Konsole 24.12+, wayst, Rio | No |
| iTerm2 inline images | 24-bit, pixel-exact | WezTerm, Konsole, VS Code, mintty, Rio, Ghostty, Tabby | No |
| Sixel | good, ~256 colours per image | foot, xterm, contour, mlterm, Konsole, Ghostty 1.1+, VS Code | img2sixel (preferred) or chafa |
| chafa symbols | truecolor character art at 2x3 sub-cell resolution | every terminal with truecolor, including GNOME Terminal and Alacritty | chafa |
| **Console framebuffer** | **real pixels, full colour** | **the Linux virtual console, where no protocol exists** | **nothing** |
| chafa ASCII | coarse but legible, 16 colours | consoles with no framebuffer access | chafa |

The two highest-quality backends need **no external program**: the bundled PNG is base64-encoded and handed to the terminal, which decodes it. `chafa` is only required for the lower two tiers, and `install.sh` installs it automatically when the distribution provides it.

Detection asks the terminal directly (a Kitty graphics query, XTVERSION, and a DA1 sentinel over `/dev/tty`) rather than reading environment variables. This matters because the documented way to run this tool is `sudo refindmgr`, and sudo's default `env_reset` policy deletes `TERM_PROGRAM`, `VTE_VERSION`, `WT_SESSION`, `KITTY_WINDOW_ID`, `TMUX`, and `STY`. Environment-based detection — including chafa's own auto-detection, which is why its output can otherwise degrade to a coarse 16-colour palette — cannot work under sudo.

Override the choice when needed:

```bash
refindmgr --preview kitty      # or iterm, sixel, chafa, none
REFINDMGR_PREVIEW=chafa refindmgr
```

Inside tmux, graphics require `tmux set -g allow-passthrough on`; refindmgr detects when it is off and falls back to character art with an explanatory message instead of emitting invisible escape sequences.

### Fixed-font consoles

The Linux virtual console — what a headless Ubuntu Server actually shows — renders from a fixed 256- or 512-glyph font. It has no sextants and no block elements, so character art built from them arrives as substitution glyphs: an unreadable mess of hashes and diamonds rather than a picture. refindmgr detects this case (from `TERM`, and independently from a VT102-level DA1 reply with no XTVERSION name) and switches to ASCII symbols with `--colors 16/8`, which avoids the bright backgrounds such consoles render inconsistently.

If your console font does carry block elements, ask for the richer output:

```bash
refindmgr --preview-symbols unicode      # or ascii to force the plain set
REFINDMGR_PREVIEW_SYMBOLS=unicode refindmgr
```

### The Linux console

No terminal protocol can show an image on the Linux virtual console: there is no Sixel, no Kitty protocol, no iTerm2 images, and the font is a fixed 256-glyph set with no block or sextant characters. Every terminal-side renderer therefore bottoms out at coloured ASCII, which cannot resemble a photograph however it is tuned. That is a property of the console, not of the renderer — `chafa`, `terminal-image` and the various Sixel libraries all hit the same wall, because they all draw *through* the terminal.

`fim` and `fbi` look right precisely because they do not. They write pixels straight to the kernel framebuffer, bypassing the terminal entirely.

refindmgr now does the same thing, but positioned at a character cell instead of full-screen, so **real thumbnails appear inline beside their titles on a console**. It needs nothing installed: the PNG decoder, the scaler and the framebuffer writer are all stdlib Python (`zlib`, `struct`, `ioctl`, `mmap`).

It activates automatically when all of the following hold, and never otherwise:

- the terminal advertises no image protocol and no rich glyphs (i.e. it is a console),
- `DISPLAY` and `WAYLAND_DISPLAY` are unset, so the framebuffer is what the user is actually looking at,
- `/dev/fb0` is readable and writable — usually meaning root, which the theme commands need anyway.

Two escape hatches exist for unusual setups:

```bash
REFINDMGR_FB_DEVICE=/dev/fb1        # a second framebuffer
REFINDMGR_FB_GEOMETRY=1024x768x32   # when the driver's ioctl misreports
```

If the framebuffer cannot be used, the catalog still offers `g<number>` (for example `g2`) to open one preview full-screen via `fim`, `fbi`, or `mpv --vo=drm`, and otherwise falls back to ASCII art.

Remote alternative: SSH into the server from a graphics-capable terminal on your desktop (kitty, WezTerm, foot, Konsole). Rendering then happens in your local terminal at full quality.

### Thumbnail size

Thumbnails are laid out in character cells but the encoders work in pixels, so refindmgr asks the terminal for its cell geometry (`TIOCGWINSZ`, falling back to the `CSI 16 t` reply) and converts. `img2sixel` is preferred over chafa for Sixel because it accepts an exact pixel size: chafa's `--size` is in cells, and when its output is a pipe it cannot query the terminal, so it assumes a square 8x8 cell and produces a thumbnail roughly a third of the intended height with the wrong aspect ratio.

## License

refindmgr is available under the [MIT License](LICENSE).
