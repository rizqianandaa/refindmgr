#!/usr/bin/env bash
# Melepas refindmgr yang dipasang lewat install.sh (tidak menyentuh tema/refind.conf yang sudah terpasang).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "uninstall.sh butuh sudo (untuk menghapus /usr/local/bin/refindmgr dan /opt/refindmgr)."
  echo "Jalankan ulang: sudo ./uninstall.sh"
  exit 1
fi

rm -f /usr/local/bin/refindmgr
rm -rf /opt/refindmgr

echo "refindmgr sudah dilepas dari sistem."
echo "Catatan: tema rEFInd yang sudah terpasang di partisi EFI TIDAK ikut terhapus oleh ini."
echo "Kalau mau menghapus tema juga, pakai 'refindmgr remove <nama>' sebelum melepas refindmgr."
