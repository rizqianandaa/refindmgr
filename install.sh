#!/usr/bin/env bash
# One-command installer. After installing the CLI it automatically bootstraps
# rEFInd on a real UEFI system and applies the known showtools compatibility
# fix so the OS-only mode really can hide unwanted tool buttons.
set -euo pipefail

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
mv "$STAGING" "$INSTALL_DIR"
trap - EXIT
ln -sfn "$INSTALL_DIR/refindmgr" /usr/local/bin/refindmgr
rm -rf "${INSTALL_DIR}.old"

# Install the img2sixel renderer automatically.  The renderer is required to
# encode previews, while the terminal itself must also support the Sixel
# protocol.  Failure remains non-fatal so refindmgr can still manage themes on
# Linux consoles and other terminals that cannot display graphics.
install_sixel_renderer() {
  if command -v img2sixel >/dev/null 2>&1; then
    echo "Sixel renderer siap: $(command -v img2sixel)"
    return 0
  fi

  echo "Memasang renderer Sixel (img2sixel) otomatis..."

  if command -v apt-get >/dev/null 2>&1; then
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y libsixel-bin; then
      echo "Index paket belum siap; menjalankan apt-get update..."
      apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libsixel-bin
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y libsixel-utils
  elif command -v yum >/dev/null 2>&1; then
    yum install -y libsixel-utils
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm --needed libsixel
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install libsixel
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache libsixel-tools
  else
    echo "PERINGATAN: package manager tidak dikenali; img2sixel tidak dapat dipasang otomatis."
    return 1
  fi

  if command -v img2sixel >/dev/null 2>&1; then
    echo "Sixel renderer berhasil dipasang: $(command -v img2sixel)"
    return 0
  fi

  echo "PERINGATAN: package manager selesai tetapi command img2sixel belum ditemukan."
  return 1
}

if ! install_sixel_renderer; then
  echo "INFO: instalasi CLI tetap dilanjutkan tanpa renderer preview."
  echo "INFO: pasang img2sixel secara manual lalu buka katalog kembali."
fi

echo "Selesai: $(refindmgr --version)"

# Keep the one-command UX requested by the project: on a real UEFI boot the
# official refind-install flow is run automatically. A BIOS/legacy boot or a
# container/chroot without UEFI runtime variables is skipped rather than
# guessing and touching disks blindly.
if [ -d /sys/firmware/efi ]; then
  SETUP_LOG="$(mktemp /tmp/refindmgr-setup.XXXXXX.log)"
  echo "Menyiapkan rEFInd otomatis (log: $SETUP_LOG)..."
  if refindmgr setup --yes --pin-version --refresh-esp --allow-direct-download >"$SETUP_LOG" 2>&1; then
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
