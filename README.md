# refindmgr

**refindmgr** adalah CLI open source untuk mengelola tema tampilan boot menu
[rEFInd](https://www.rodsbooks.com/refind/) — pasang, ganti, dan hapus tema tanpa
perlu masuk ke partisi EFI dan mengedit `refind.conf` secara manual.

Versi ini adalah CLI. Rencana selanjutnya: GUI sederhana di atas logic yang sama.

## Fitur

- **Katalog tema siap pakai** — pasang tema populer hanya dengan satu nama key.
- **Pasang tema dari mana pun** — URL git, folder lokal, atau file `.zip`.
- **Ganti tema aktif** dalam satu perintah, tanpa edit manual `refind.conf`.
- **Backup otomatis** setiap kali `refind.conf` diubah, dengan perintah `restore`
  untuk mengembalikannya kapan saja.
- **Validasi nama tema** yang mencegah path traversal (`../../etc` dan sejenisnya
  akan selalu ditolak).
- **`refindmgr setup`** — bantu memasang rEFInd itu sendiri (lewat package manager
  sistem + skrip resmi `refind-install`) kalau belum terpasang sama sekali.
- **`refindmgr doctor`** — diagnostik satu perintah untuk memastikan semuanya
  terdeteksi dengan benar sebelum melakukan perubahan apa pun.
- Tidak pernah menyentuh boot loader/NVRAM secara langsung — hanya mengelola folder
  `themes/` dan baris `include` di `refind.conf`.

## Instalasi

Butuh `python3` dan modul `venv`-nya (bawaan di kebanyakan distro; di Debian/Ubuntu
install dengan `sudo apt-get install -y python3-venv` kalau belum ada).

```bash
git clone https://github.com/rizqianandaa/refindmgr.git
cd refindmgr
sudo ./install.sh
```

`install.sh` membuat environment Python terisolasi di `/opt/refindmgr` (tidak
menyentuh Python/pip sistem) dan memasang command `refindmgr` ke `/usr/local/bin`,
supaya langsung bisa dipanggil dari mana saja — termasuk lewat `sudo` — tanpa perlu
`venv`/`pip install` manual.

Untuk melepasnya:

```bash
sudo ./uninstall.sh
```

(Tema yang sudah terpasang di partisi EFI tidak ikut terhapus oleh ini — hapus dulu
dengan `refindmgr remove <nama>` kalau memang mau bersih total.)

## Pakai cepat

```bash
refindmgr doctor                          # pastikan rEFInd terdeteksi
refindmgr catalog                         # lihat pilihan tema
sudo refindmgr install minimal --activate # pasang + aktifkan
```

Reboot untuk melihat tema barunya di boot menu.

> Perintah yang hanya membaca (`list`, `catalog`, `doctor`) tidak butuh `sudo`.
> Perintah yang menulis ke partisi EFI (`install`, `activate`, `deactivate`,
> `remove`, `backup`, `restore`, `setup --yes`) butuh `sudo`.

## Semua perintah

| Perintah | Butuh sudo? | Fungsi |
|---|---|---|
| `refindmgr doctor` | tidak | Diagnostik: folder rEFInd, git, akses root |
| `refindmgr setup [--yes]` | ya (dengan `--yes`) | Pasang rEFInd itu sendiri jika belum terpasang |
| `refindmgr catalog` | tidak | Lihat katalog tema pilihan |
| `refindmgr list` | tidak | Lihat tema terpasang & yang aktif |
| `refindmgr install <sumber> [--activate] [--name NAMA]` | ya | Pasang tema (katalog/URL git/folder/`.zip`) |
| `refindmgr activate <nama>` | ya | Jadikan tema `<nama>` aktif |
| `refindmgr deactivate` | ya | Nonaktifkan semua tema (kembali ke default) |
| `refindmgr remove <nama>` | ya | Hapus tema terpasang |
| `refindmgr backup` | ya | Simpan salinan `refind.conf` saat ini |
| `refindmgr restore [--backup PATH]` | ya | Kembalikan `refind.conf` dari backup |

Setiap perintah menerima `--refind-dir /path/ke/EFI/refind` (sebelum atau setelah
nama perintah) untuk menentukan lokasi folder rEFInd secara manual, atau set sekali
lewat environment variable:

```bash
export REFIND_DIR=/boot/efi/EFI/refind
```

### Sumber tema yang didukung

```bash
sudo refindmgr install minimal                          # key dari katalog
sudo refindmgr install https://github.com/user/repo     # URL git
sudo refindmgr install ./tema-yang-sudah-didownload      # folder lokal
sudo refindmgr install ./tema.zip                        # file .zip lokal
```

Cari lebih banyak pilihan tema (140+) di [refind-themes-collection](https://refind-themes-collection.netlify.app/).

## Belum punya rEFInd?

`refindmgr setup` bisa membantu memasang rEFInd itu sendiri:

```bash
refindmgr setup            # pratinjau: tampilkan apa yang akan dilakukan
sudo refindmgr setup --yes # jalankan instalasi rEFInd sesungguhnya
```

Di balik layar: mendeteksi package manager sistem (apt/dnf/pacman/zypper), memasang
paket `refind` lewat itu, lalu menjalankan skrip **resmi** `refind-install` dari
proyek rEFInd sendiri. refindmgr tidak pernah menulis ke partisi EFI/NVRAM dengan
logikanya sendiri untuk langkah ini. Distro lain: ikuti [panduan resmi rEFInd](https://www.rodsbooks.com/refind/installing.html).

## Keamanan & backup

- Tidak pernah menyentuh `refind_x64.efi`/`refind_ia32.efi`/`refind_aa64.efi` atau
  berkas boot loader/NVRAM lain — satu-satunya operasi di level itu (`setup --yes`)
  didelegasikan penuh ke `refind-install` resmi.
- Setiap perubahan `refind.conf` membuat backup timestamped otomatis lebih dulu
  (`refind.conf.<waktu>.bak`) sebelum menulis apa pun.
- Nama tema divalidasi sebagai nama folder aman (menolak path traversal).

## Troubleshooting

**`sudo refindmgr ...` → `command not found`.** Biasanya karena refindmgr dipasang
manual di virtual environment (bukan lewat `install.sh`), yang PATH-nya tidak
terbawa ke shell baru milik `sudo`. Solusi: `sudo ./install.sh`.

**`Permission denied` tanpa `sudo`.** Ini disengaja — partisi EFI hanya bisa
ditulis oleh root. Tambahkan `sudo` untuk perintah yang mengubah sesuatu.

**Folder rEFInd tidak terdeteksi otomatis.** Cek lokasi partisi EFI kamu
(`lsblk`/`sudo blkid`), lalu tentukan manual lewat `--refind-dir` atau
`REFIND_DIR`.

## Development

```bash
python3 -m venv env && source env/bin/activate
pip install -e .
python3 -m unittest discover -s tests -v   # 64 test
```

## Lisensi

MIT License — lihat berkas [`LICENSE`](LICENSE).
