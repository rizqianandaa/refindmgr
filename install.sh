#!/usr/bin/env bash
# Installer refindtm: sekali jalan, tidak perlu venv/pip manual.
# Setelah ini, 'refindtm' (dan 'sudo refindtm') langsung bisa dipanggil dari mana saja.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh butuh sudo, supaya command 'refindtm' bisa ditempatkan di /usr/local/bin"
  echo "dan supaya 'sudo refindtm ...' nanti benar-benar bisa menemukan commandnya."
  echo ""
  echo "Jalankan ulang:"
  echo "  sudo ./install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/refindtm"
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

echo "[1/3] Menyiapkan environment Python terisolasi di $VENV_DIR ..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"

echo "[2/3] Memasang refindtm ke environment tersebut ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$SCRIPT_DIR"

echo "[3/3] Memasang command 'refindtm' ke /usr/local/bin ..."
cat > /usr/local/bin/refindtm << WRAPPER
#!/usr/bin/env bash
exec "$VENV_DIR/bin/refindtm" "\$@"
WRAPPER
chmod +x /usr/local/bin/refindtm

echo ""
echo "Selesai! refindtm sudah terpasang dan siap dipakai dari mana saja, contoh:"
echo "  refindtm doctor"
echo "  sudo refindtm install minimal --activate"
echo ""
echo "(Tidak perlu lagi bikin venv/pip manual, dan tidak perlu 'source venv/bin/activate'.)"
