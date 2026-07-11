# refindtm — rEFInd Theme Manager (CLI)

`refindtm` adalah CLI gratis dan open source untuk mengganti tema tampilan boot
menu [rEFInd](https://www.rodsbooks.com/refind/), tanpa perlu masuk ke folder
EFI dan edit `refind.conf` secara manual.

Ini versi **CLI**. Setelah dipakai & stabil, akan dibuatkan GUI sederhana di atasnya.

---

## Mulai cepat (2 langkah, tanpa bikin venv/pip manual)

**Langkah 0 — Sudah punya rEFInd terpasang di komputer kamu?**

- **Sudah** → lanjut ke Langkah 1.
- **Belum tahu / belum pernah install** → lanjutkan dulu Langkah 1 di bawah supaya
  command `refindtm` terpasang, lalu jalankan `refindtm setup`. Detail lengkap ada
  di bagian [Belum punya rEFInd?](#belum-punya-refind-pakai-refindtm-setup) di bawah.

**Langkah 1 — Install refindtm (sekali saja)**

```bash
git clone <repo-ini>
cd refindtm
sudo ./install.sh
```

`install.sh` melakukan semuanya secara otomatis:
- Membuat environment Python-nya sendiri yang terisolasi di `/opt/refindtm`
  (tidak menyentuh/mengubah Python atau pip sistem kamu).
- Memasang command `refindtm` ke `/usr/local/bin`, lokasi standar yang selalu
  ada di PATH baik dipanggil biasa **maupun lewat `sudo`**.

Setelah ini, **tidak perlu lagi** `python3 -m venv`, `pip install`, atau
`source venv/bin/activate` manual — cukup panggil `refindtm` dari folder mana pun.

> Kalau kamu mengalami error `sudo: 'refindtm': command not found` atau harus
> aktifkan venv manual sebelumnya, itu karena refindtm dipasang dengan cara lama
> (`pip install -e .` langsung di virtual environment). Lihat penjelasan & perbaikannya
> di bagian [Kalau masih menemui masalah](#kalau-masih-menemui-masalah) di bawah.

**Langkah 2 — Cek semuanya terdeteksi dengan baik**

```bash
refindtm doctor
```

Kalau ini menunjukkan `[OK] Folder rEFInd ditemukan: ...`, kamu siap lanjut.
Kalau tidak ketemu otomatis, lihat bagian [Kalau folder rEFInd tidak terdeteksi](#kalau-folder-refind-tidak-terdeteksi) di bawah.

**Langkah 3 — Pasang & aktifkan tema pertama kamu**

```bash
refindtm catalog                          # lihat pilihan tema
sudo refindtm install minimal --activate  # pasang + langsung aktifkan
```

Selesai — reboot untuk melihat tema barunya di boot menu.

> **Kenapa pakai `sudo`?** Karena tema disimpan di partisi EFI, yang di kebanyakan
> sistem hanya bisa ditulis oleh root. Perintah yang hanya membaca (`list`, `catalog`,
> `doctor`) tidak butuh `sudo`. Perintah yang menulis (`install`, `activate`,
> `deactivate`, `remove`, `backup`, `restore`, `setup --yes`) butuh `sudo`.

---

## Kalau masih menemui masalah

### `sudo refindtm ...` → `command not found`

Ini terjadi kalau refindtm dipasang manual di dalam virtual environment
(`python3 -m venv env` + `source env/bin/activate` + `pip install -e .`) tanpa
memakai `install.sh`. Command `refindtm` waktu itu hanya ada di dalam folder
`env/bin/` venv kamu, yang cuma masuk PATH selama venv itu aktif di shell kamu.
`sudo` menjalankan shell baru dengan PATH bawaan sistem (demi keamanan), yang
tidak tahu-menahu soal venv itu — makanya `refindtm` dianggap tidak dikenali,
walau tanpa `sudo` command yang sama jalan normal.

**Solusi:** hapus venv manual itu (opsional) dan pasang lewat `install.sh`:

```bash
sudo ./install.sh
```

Ini menaruh command `refindtm` di `/usr/local/bin`, folder yang selalu ada di
PATH baik dipanggil langsung maupun lewat `sudo`, jadi masalah ini tidak akan
terjadi lagi.

### `Permission denied` waktu dijalankan tanpa `sudo`

Ini **normal dan disengaja**, bukan bug — partisi EFI (tempat tema & `refind.conf`
disimpan) di hampir semua sistem cuma bisa ditulis oleh root. Command yang
hanya membaca (`list`, `catalog`, `doctor`) tidak butuh `sudo`. Command yang
mengubah sesuatu (`install`, `activate`, `deactivate`, `remove`, `backup`,
`restore`, `setup --yes`) memang harus dijalankan dengan `sudo refindtm ...`.

### Melepas refindtm dari sistem

```bash
sudo ./uninstall.sh
```

Ini hanya melepas command `refindtm` dan environment Python-nya di `/opt/refindtm`.
Tema yang sudah terpasang di partisi EFI **tidak ikut terhapus** — hapus tema
dulu dengan `refindtm remove <nama>` sebelum melepas refindtm kalau memang mau
bersih total.

### `install.sh` gagal karena `python3-venv` tidak ada

Di Debian/Ubuntu, modul `venv` bawaan Python kadang perlu dipasang terpisah:

```bash
sudo apt-get install -y python3-venv
```

Lalu jalankan ulang `sudo ./install.sh`.

---

## Semua perintah

| Perintah | Butuh sudo? | Fungsi |
|---|---|---|
| `refindtm doctor` | tidak | Diagnostik: folder rEFInd ketemu? git ada? jalan sebagai root? |
| `refindtm setup [--yes]` | ya (dengan `--yes`) | Bantu install rEFInd itu sendiri jika belum terpasang |
| `refindtm catalog` | tidak | Lihat daftar tema pilihan |
| `refindtm list` | tidak | Lihat tema yang sudah terpasang & yang aktif |
| `refindtm install <sumber> [--activate] [--name NAMA]` | ya | Pasang tema baru (dari katalog/URL git/folder/.zip) |
| `refindtm activate <nama>` | ya | Jadikan tema `<nama>` aktif |
| `refindtm deactivate` | ya | Matikan semua tema, balik ke tampilan default |
| `refindtm remove <nama>` | ya | Hapus tema yang terpasang |
| `refindtm backup` | ya | Simpan salinan `refind.conf` saat ini |
| `refindtm restore [--backup PATH]` | ya | Kembalikan `refind.conf` dari backup |

Setiap perintah bisa ditambahkan `--refind-dir /path/ke/EFI/refind` (di mana saja,
sebelum atau setelah nama perintah) untuk menentukan lokasi folder rEFInd secara manual.

### Sumber tema yang didukung oleh `install`

```bash
sudo refindtm install minimal                          # key dari katalog
sudo refindtm install https://github.com/user/repo     # URL git (butuh git terpasang)
sudo refindtm install ./tema-yang-sudah-didownload      # folder lokal
sudo refindtm install ./tema.zip                        # file .zip lokal
```

---

## Belum punya rEFInd? Pakai `refindtm setup`

Jika `refindtm doctor` melaporkan folder rEFInd tidak ketemu karena rEFInd memang
belum pernah diinstall, kamu **tidak perlu install manual** — `refindtm setup` bisa
membantu:

```bash
refindtm setup            # pratinjau: tampilkan apa yang AKAN dilakukan, tanpa mengubah apa pun
sudo refindtm setup --yes # benar-benar jalankan instalasi rEFInd
```

Yang terjadi di balik layar (transparan, tidak ada langkah tersembunyi):
1. Kalau paket `refind` belum ada, `setup` mendeteksi package manager sistem kamu
   (apt/dnf/pacman/zypper) dan memasangnya lewat package manager itu.
2. Kemudian `setup` menjalankan skrip **resmi** `refind-install` dari proyek rEFInd
   sendiri untuk memasang rEFInd ke partisi EFI kamu.

refindtm **tidak pernah menulis ke partisi EFI/NVRAM dengan logikanya sendiri** untuk
langkah ini — semuanya didelegasikan ke `refind-install` resmi, yang sudah diuji
luas oleh proyek rEFInd sendiri. Tanpa flag `--yes`, `setup` hanya menampilkan pratinjau dan tidak
mengubah apa pun di sistem kamu.

Jika distro kamu tidak memakai salah satu dari apt/dnf/pacman/zypper, `setup` akan
memberi tahu dan mengarahkan ke [panduan instalasi resmi rEFInd](https://www.rodsbooks.com/refind/installing.html).

---

## Kalau folder rEFInd tidak terdeteksi

`refindtm` mencari otomatis di lokasi umum seperti `/boot/efi/EFI/refind`. Kalau
tidak ketemu (misal partisi EFI kamu di-mount di tempat lain), cari dulu lokasinya:

```bash
lsblk   # atau: sudo blkid
```

Lalu tentukan manual dengan `--refind-dir`, contoh:

```bash
refindtm --refind-dir /boot/efi/EFI/refind doctor
```

Atau set sekali lewat environment variable supaya tidak perlu diulang setiap kali:

```bash
export REFIND_DIR=/boot/efi/EFI/refind
```

---

## Keamanan & backup — apa yang disentuh, apa yang tidak

Karena tool ini bekerja di partisi EFI, berikut batasan yang sengaja dijaga:

- **Tidak pernah** menyentuh `refind_x64.efi`, `refind_ia32.efi`, `refind_aa64.efi`,
  atau berkas boot loader/NVRAM lain. Satu-satunya operasi yang menyentuh level itu
  adalah `refindtm setup --yes`, dan itu pun didelegasikan penuh ke skrip resmi
  `refind-install` (lihat bagian di atas), bukan logika buatan sendiri.
- Operasi tema (`install`/`activate`/`deactivate`/`remove`) hanya menyalin folder ke
  `themes/` dan mengedit baris `include themes/.../theme.conf` di `refind.conf`.
- **Setiap kali `refind.conf` diubah, backup timestamped otomatis dibuat lebih dulu**
  di folder yang sama (`refind.conf.<waktu>.bak`). Kalau ada yang salah:
  ```bash
  sudo refindtm restore              # pulihkan dari backup terbaru
  sudo refindtm restore --backup /path/ke/refind.conf.xxxxx.bak  # pulihkan backup tertentu
  ```
- Nama tema divalidasi sebelum dipakai sebagai nama folder, jadi nama seperti
  `../../etc` akan ditolak (mencegah tool menulis/menghapus di luar folder `themes/`).
- `install.sh`/`uninstall.sh` sendiri juga tidak pernah menyentuh partisi EFI —
  keduanya hanya mengelola environment Python di `/opt/refindtm` dan command di
  `/usr/local/bin/refindtm`.

---

## Menjalankan unit test

```bash
python3 -m unittest discover -s tests -v
```

Semua 64 test (deteksi path, parsing & edit `refind.conf`, backup, validasi nama tema
/ anti path-traversal, install/remove tema dari folder lokal & `.zip`, katalog,
deteksi package manager untuk `setup`, dan smoke test CLI end-to-end) lolos sebelum
rilis ini dibuat.

---

## Struktur proyek

```
refindtm/
├── refindtm/
│   ├── paths.py     # Deteksi folder rEFInd
│   ├── conf.py      # Baca/ubah refind.conf dengan aman + backup
│   ├── themes.py    # Install/remove/list tema (git, folder, zip) + validasi nama
│   ├── catalog.py   # Katalog tema pilihan
│   ├── system.py    # Deteksi & bantu instalasi rEFInd itu sendiri (bukan tema)
│   └── cli.py       # Antarmuka command-line
├── tests/           # Unit test (64 test)
├── install.sh       # Pasang refindtm sekali jalan, tanpa venv/pip manual (butuh sudo)
├── uninstall.sh     # Lepas refindtm dari sistem (butuh sudo)
├── main.py          # Entry point tanpa perlu install package (untuk pengembangan)
├── pyproject.toml   # Metadata package & command 'refindtm'
└── .github/workflows/test.yml  # CI: jalankan test di setiap push
```

### Untuk pengembang (kontribusi/modifikasi kode)

Kalau kamu mau mengubah kode ini (bukan sekadar memakainya), tetap boleh pasang
secara manual dengan venv seperti biasa:

```bash
python3 -m venv env
source env/bin/activate
pip install -e .
refindtm doctor   # jalan normal selama venv ini aktif di shell yang sama
```

Ingat: dengan cara ini, `sudo refindtm ...` tidak akan menemukan commandnya
(lihat penjelasan di [Kalau masih menemui masalah](#kalau-masih-menemui-masalah)).
Untuk pemakaian sehari-hari (bukan pengembangan), tetap disarankan pakai `install.sh`.

## Keterbatasan yang disengaja (demi tetap ringan & fokus)

- Belum ada preview visual tema sebelum dipasang (rencana untuk versi GUI nanti).
- Instalasi tema dari URL git butuh `git` terpasang. Tanpa `git`, download tema sebagai
  `.zip` lalu pasang dari file lokal.
- `refindtm setup` mendukung distro berbasis apt/dnf/pacman/zypper. Distro lain perlu
  install rEFInd manual sesuai [panduan resmi](https://www.rodsbooks.com/refind/installing.html).
- `install.sh` butuh `python3` (dan modul `venv`-nya) serta akses internet sekali di
  awal untuk memasang dependensi package Python.

## Roadmap

1. ~~Versi CLI (`refindtm`)~~ — selesai, 64 test lolos, termasuk `refindtm setup`.
2. GUI sederhana di atas logic yang sama (`paths.py`, `conf.py`, `themes.py`,
   `catalog.py`, `system.py` tidak berubah, hanya `cli.py` yang digantikan/ditambah lapisan GUI).

## Lisensi

MIT License — lihat berkas `LICENSE`.
