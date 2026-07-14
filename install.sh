#!/usr/bin/env bash
# Installer refindmgr: sekali jalan, tidak perlu venv/pip manual.
# Setelah ini, 'refindmgr' (dan 'sudo refindmgr') langsung bisa dipanggil dari mana saja,
# dan skrip ini juga otomatis memasang rEFInd itu sendiri kalau belum ada di sistem ini
# (bukan cuma mengecek, tapi benar-benar memasangnya lewat 'refindmgr setup --yes').
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh butuh sudo, supaya command 'refindmgr' bisa ditempatkan di /usr/local/bin"
  echo "dan supaya rEFInd itu sendiri bisa dipasang ke partisi EFI kalau belum ada."
  echo ""
  echo "Jalankan ulang:"
  echo "  sudo ./install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/refindmgr"
VENV_DIR="$INSTALL_DIR/venv"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 tidak ditemukan di sistem ini."
  echo "Install dulu lewat package manager sistem kamu (contoh: sudo apt-get install -y python3), lalu jalankan ulang skrip ini."
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "Modul 'venv' bawaan python3 tidak ditemukan."
  echo "Di Debian/Ubuntu, install dulu dengan: sudo apt-get install -y python3-venv"
  echo "Lalu jalankan ulang skrip ini."
  exit 1
fi

echo "[1/4] Menyiapkan environment Python terisolasi di $VENV_DIR ..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"

echo "[2/4] Memasang refindmgr ke environment tersebut ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
# --force-reinstall --no-deps: pip's normal behavior treats a local-path install
# as "already satisfied" and silently SKIPS copying any updated files whenever
# the package version number (in pyproject.toml) hasn't changed -- even though
# refindmgr's actual source code did change. Without this flag, re-running
# install.sh after pulling/extracting a newer refindmgr would leave the OLD
# code running in $VENV_DIR forever, with no error or warning about it.
# --no-deps just skips reinstalling refindmgr's (currently nonexistent) runtime
# dependencies, since only refindmgr's own files need to be refreshed here.
"$VENV_DIR/bin/pip" install --quiet --force-reinstall --no-deps "$SCRIPT_DIR"

echo "[3/4] Memasang command 'refindmgr' ke /usr/local/bin ..."
cat > /usr/local/bin/refindmgr << WRAPPER
#!/usr/bin/env bash
exec "$VENV_DIR/bin/refindmgr" "\$@"
WRAPPER
chmod +x /usr/local/bin/refindmgr

echo "[4/4] Memastikan rEFInd itu sendiri sudah terpasang di sistem ini ..."
if "$VENV_DIR/bin/refindmgr" setup --yes; then
  echo ""
else
  echo ""
  echo "PERINGATAN: refindmgr tidak bisa memasang rEFInd secara otomatis di sistem ini"
  echo "(distro/package manager belum didukung, atau langkah instalasinya gagal)."
  echo "refindmgr sendiri tetap sudah terpasang dengan baik -- kamu masih bisa mencoba"
  echo "memasang rEFInd manual nanti dengan: sudo refindmgr setup --yes"
  echo "atau ikuti panduan resmi: https://www.rodsbooks.com/refind/installing.html"
  echo ""
fi

echo "Selesai! refindmgr sudah terpasang dan siap dipakai dari mana saja, contoh:"
echo "  refindmgr                                 # buka menu interaktif"
echo "  refindmgr doctor"
echo "  sudo refindmgr install minimal --activate"
echo ""
echo "(Tidak perlu lagi bikin venv/pip manual, dan tidak perlu 'source venv/bin/activate'.)"
