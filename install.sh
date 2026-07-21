#!/usr/bin/env bash
# One-command installer. After installing the CLI it automatically bootstraps
# rEFInd on a real UEFI system, but never pins/downgrades its version unless the
# user later requests that explicitly.
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

echo "Selesai: $(refindmgr --version)"

# Optional Sixel renderer for real image previews in supported terminals.
if ! command -v img2sixel >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y libsixel-bin >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y libsixel-utils >/dev/null 2>&1 || true
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm libsixel >/dev/null 2>&1 || true
  fi
fi

# Keep the one-command UX requested by the project: on a real UEFI boot the
# official refind-install flow is run automatically. A BIOS/legacy boot or a
# container/chroot without UEFI runtime variables is skipped rather than
# guessing and touching disks blindly.
if [ -d /sys/firmware/efi ]; then
  SETUP_LOG="$(mktemp /tmp/refindmgr-setup.XXXXXX.log)"
  echo "Menyiapkan rEFInd otomatis (log: $SETUP_LOG)..."
  if refindmgr setup --yes >"$SETUP_LOG" 2>&1; then
    cat "$SETUP_LOG"
    rm -f "$SETUP_LOG"
    echo "Setup rEFInd selesai."
  else
    echo "PERINGATAN: CLI sudah terpasang, tetapi setup rEFInd gagal."
    echo "Detail disimpan di: $SETUP_LOG"
    echo "Tidak ada version pinning/downgrade otomatis yang dilakukan."
  fi
else
  echo "INFO: runtime UEFI tidak terdeteksi; setup bootloader otomatis dilewati demi keamanan."
  echo "CLI tetap terpasang. Jalankan 'refindmgr doctor' untuk diagnosis."
fi
