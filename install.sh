#!/usr/bin/env bash
# One-command installer. After installing the CLI it automatically bootstraps
# rEFInd on a real UEFI system and applies the known showtools compatibility
# fix so the OS-only mode really can hide unwanted tool buttons.
set -euo pipefail

CLI_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --cli-only) CLI_ONLY=1 ;;
    -h|--help)
      echo "Penggunaan: sudo ./install.sh [--cli-only]"
      echo "  --cli-only  Pasang CLI tanpa menjalankan setup/refind-install."
      exit 0
      ;;
    *) echo "Argumen tidak dikenal: $arg"; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Jalankan ulang dengan sudo: sudo ./install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/refindmgr"
STAGING="$(mktemp -d /opt/refindmgr-install.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 tidak ditemukan. Install Python 3.9+ lalu ulangi."
  exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("refindmgr membutuhkan Python 3.9+")
PY

mkdir -p "$STAGING/src"
cp -a "$SCRIPT_DIR/refindmgr" "$STAGING/src/refindmgr"
find "$STAGING" -type d -name __pycache__ -prune -exec rm -rf {} +
cat > "$STAGING/refindmgr" <<'WRAPPER'
#!/usr/bin/env bash
export PYTHONPATH="/opt/refindmgr/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m refindmgr.cli "$@"
WRAPPER
chmod 0755 "$STAGING/refindmgr"

# Commit the application directory atomically enough for upgrades: the old
# installation remains available until staging is complete.
rm -rf "${INSTALL_DIR}.old"
if [ -e "$INSTALL_DIR" ]; then mv "$INSTALL_DIR" "${INSTALL_DIR}.old"; fi
# mktemp -d creates the staging directory 0700 and mv preserves that mode, so
# without this chmod /opt/refindmgr stays root-only and every non-root command
# (refindmgr catalog, --version, read-only doctor) fails with Permission denied.
chmod 0755 "$STAGING"
mv "$STAGING" "$INSTALL_DIR"
trap - EXIT
ln -sfn "$INSTALL_DIR/refindmgr" /usr/local/bin/refindmgr
rm -rf "${INSTALL_DIR}.old"

# Two renderers, different jobs:
#   img2sixel  -- Sixel at an EXACT pixel size. chafa's --size is in cells and,
#                 when its stdout is a pipe, it cannot ask the terminal how big
#                 a cell is, so it assumes a square 8x8 one and the thumbnail
#                 comes out squashed and roughly a third of the intended size.
#   chafa      -- the character-art fallback for terminals with no image
#                 protocol at all (GNOME Terminal, Alacritty, Linux console).
# The two best backends (Kitty f=100 and iTerm2) need neither: they base64 the
# bundled PNG straight to the terminal. So this whole step stays non-fatal.
install_preview_renderer() {
  local want_chafa=1 want_sixel=1
  command -v chafa >/dev/null 2>&1 && want_chafa=0
  command -v img2sixel >/dev/null 2>&1 && want_sixel=0
  if [ "$want_chafa" -eq 0 ] && [ "$want_sixel" -eq 0 ]; then
    echo "Renderer preview siap: $(command -v chafa), $(command -v img2sixel)"
    return 0
  fi

  echo "Memasang renderer preview (chafa + libsixel) otomatis..."

  if command -v apt-get >/dev/null 2>&1; then
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y chafa libsixel-bin; then
      echo "Index paket belum siap; menjalankan apt-get update..."
      apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y chafa libsixel-bin
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y chafa libsixel-utils
  elif command -v yum >/dev/null 2>&1; then
    yum install -y chafa libsixel-utils
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm --needed chafa libsixel
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install chafa libsixel
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache chafa libsixel-tools
  else
    echo "PERINGATAN: package manager tidak dikenali; chafa tidak dapat dipasang otomatis."
    return 1
  fi

  if command -v chafa >/dev/null 2>&1 || command -v img2sixel >/dev/null 2>&1; then
    echo "Renderer preview berhasil dipasang."
    command -v chafa >/dev/null 2>&1 && echo "  chafa    : $(command -v chafa)"
    command -v img2sixel >/dev/null 2>&1 && echo "  img2sixel: $(command -v img2sixel)"
    return 0
  fi

  echo "PERINGATAN: package manager selesai tetapi chafa/img2sixel belum ditemukan."
  return 1
}

if ! install_preview_renderer; then
  echo "INFO: instalasi CLI tetap dilanjutkan tanpa renderer preview."
  echo "INFO: terminal dengan protokol Kitty/iTerm2 tetap menampilkan gambar tanpa renderer."
  echo "INFO: untuk terminal lain, pasang chafa lalu buka katalog kembali."
fi

echo "Selesai: $(refindmgr --version)"

if [ "$CLI_ONLY" -eq 1 ]; then
  echo "Mode --cli-only: setup bootloader dilewati."
  exit 0
fi

# A compatibility install deliberately owns a firmware-recognised vendor
# path. Never let the automatic refind-install refresh overwrite it.
COMPAT_FOUND=0
for root in /boot/efi /boot /efi; do
  if compgen -G "$root/EFI/*/.refindmgr/firmware-compat.json" >/dev/null || \
     compgen -G "$root/EFI/*/.refindmgr/hp-compat-state.txt" >/dev/null; then
    COMPAT_FOUND=1
    break
  fi
done
if [ "$COMPAT_FOUND" -eq 1 ]; then
  echo "Mode kompatibilitas firmware terdeteksi; setup/refind-install otomatis dilewati."
  echo "Gunakan 'sudo refindmgr firmware-compat status' untuk memeriksa statusnya."
  exit 0
fi

# Keep the one-command UX requested by the project: on a real UEFI boot the
# official refind-install flow is run automatically. A BIOS/legacy boot or a
# container/chroot without UEFI runtime variables is skipped rather than
# guessing and touching disks blindly.
if [ -d /sys/firmware/efi ]; then
  SETUP_LOG="$(mktemp /tmp/refindmgr-setup.XXXXXX.log)"
  echo "Menjalankan preflight boot read-only..."
  if ! refindmgr preflight --setup >"$SETUP_LOG" 2>&1; then
    cat "$SETUP_LOG"
    echo "INFO: setup bootloader otomatis dihentikan karena layout boot ambigu."
    echo "INFO: CLI tetap terpasang; ESP dan NVRAM tidak diubah."
    echo "INFO: jalankan 'sudo refindmgr doctor --forensic --scan-unmounted --export'."
    exit 0
  fi
  echo "Menyiapkan rEFInd otomatis (log: $SETUP_LOG)..."
  # --allow-direct-download is deliberately NOT passed here. It downloads a
  # .deb through a SourceForge mirror redirect and installs it as root; that
  # now requires an explicit --deb-sha256 and must be a human decision, not
  # something an unattended installer does on every UEFI machine.
  if refindmgr setup --yes --pin-version --refresh-esp >"$SETUP_LOG" 2>&1; then
    cat "$SETUP_LOG"
    rm -f "$SETUP_LOG"
    echo "Setup rEFInd selesai."
  else
    echo "PERINGATAN: CLI sudah terpasang, tetapi setup rEFInd gagal."
    echo "Detail disimpan di: $SETUP_LOG"
    echo "Periksa log sebelum mencoba mode 'Hanya tampilkan OS saja'."
  fi
else
  echo "INFO: runtime UEFI tidak terdeteksi; setup bootloader otomatis dilewati demi keamanan."
  echo "CLI tetap terpasang. Jalankan 'refindmgr doctor' untuk diagnosis."
fi
