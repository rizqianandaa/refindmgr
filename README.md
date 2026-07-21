# refindmgr

CLI untuk memasang, mengaktifkan, mengganti varian, dan menghapus tema rEFInd tanpa mengedit `refind.conf` secara manual.

Versi saat ini: **2.1.1**

## Fitur utama

- Menu terminal interaktif dengan header ASCII.
- Instalasi tema dari katalog, URL GitHub public, folder lokal, atau ZIP.
- Deteksi varian tema secara otomatis:
  - beberapa file konfigurasi seperti `latte.conf`, `frappe.conf`, `macchiato.conf`, dan `mocha.conf`;
  - beberapa folder yang masing-masing memiliki `theme.conf`;
  - satu konfigurasi dengan beberapa pilihan background.
- Ganti varian tema yang sudah terpasang tanpa clone atau instalasi ulang.
- Normalisasi dan validasi path banner, icon, selection image, dan font sebelum instalasi dinyatakan berhasil.
- Preview gambar asli setiap tema katalog menggunakan Sixel.
- Clone repository GitHub public melalui anonymous HTTPS tanpa prompt username/password.
- Backup `refind.conf` otomatis, deduplikasi backup identik, dan retensi lima backup terbaru.
- Penulisan konfigurasi secara atomik dan rollback untuk operasi tema yang gagal.
- `declutter`, `dedupe`, dan `clean-menu` untuk merapikan tampilan boot.
- Diagnostik melalui `doctor`.
- Setup rEFInd otomatis dari `install.sh` pada sistem UEFI.

## Persyaratan

- Linux dengan Python 3.9 atau lebih baru.
- `git` untuk sumber GitHub.
- Hak root untuk menulis ke EFI System Partition.
- Terminal dengan dukungan Sixel dan `img2sixel` untuk preview gambar.

## Instalasi

Ekstrak repository, lalu jalankan:

```bash
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

Installer akan:

1. memasang CLI ke `/opt/refindmgr`;
2. membuat command `/usr/local/bin/refindmgr`;
3. mencoba memasang `img2sixel` untuk preview katalog;
4. menjalankan setup rEFInd otomatis jika runtime UEFI terdeteksi.

Setup otomatis tidak melakukan version pinning atau downgrade rEFInd. Pada BIOS, container, atau chroot tanpa `/sys/firmware/efi`, setup bootloader dilewati agar tidak menebak lokasi disk.

Setelah instalasi:

```bash
sudo refindmgr
```

## Uninstall

```bash
sudo ./uninstall.sh
```

Uninstall hanya menghapus CLI dari `/opt/refindmgr` dan `/usr/local/bin/refindmgr`. Tema dan konfigurasi rEFInd di ESP tidak dihapus.

## Menu interaktif

```text
Tema
  1) Lihat tema terpasang & aktif
  2) Pasang tema dari katalog
  3) Pasang dari URL GitHub / ZIP / folder
  4) Aktifkan tema
  5) Nonaktifkan semua tema
  6) Hapus tema
  7) Ganti varian tema

Backup refind.conf
  8) Buat backup sekarang
  9) Restore dari backup

Tampilan boot
  10) Hanya tampilkan OS saja
  11) Batalkan mode OS saja

Sistem
  12) Diagnostik (doctor)
  13) Pasang rEFInd itu sendiri (setup)

  0) Keluar
```

## Preview katalog dengan Sixel

Saat menu **Pasang tema dari katalog** dibuka, setiap tema ditampilkan bersama preview asli berukuran kecil sebelum prompt pemilihan tema.

Preview:

- hanya berjalan dalam katalog interaktif;
- tidak diulang setelah pengguna memilih tema atau menyetujui instalasi;
- tidak berjalan untuk instalasi ZIP, folder lokal, atau URL GitHub langsung;
- disimpan dalam cache pengguna agar pembukaan katalog berikutnya lebih cepat;
- tidak mengirim escape sequence gambar jika terminal tidak mendukung Sixel.

Jika terminal mendukung Sixel tetapi tidak terdeteksi otomatis:

```bash
REFINDMGR_SIXEL=1 sudo refindmgr
```

Paket renderer yang digunakan:

- Debian/Ubuntu: `libsixel-bin`
- Fedora: `libsixel-utils`
- Arch Linux: `libsixel`

## Instalasi tema

### Dari katalog

```bash
sudo refindmgr install catppuccin --activate
sudo refindmgr install sublime --activate
```

### Dari GitHub public

```bash
sudo refindmgr install https://github.com/catppuccin/refind --activate
```

Bentuk URL GitHub berikut dinormalisasi menjadi anonymous HTTPS:

```text
github.com/owner/repo
https://github.com/owner/repo
git@github.com:owner/repo.git
ssh://git@github.com/owner/repo
```

Repository public tidak memerlukan login GitHub. Jika URL salah atau repository private, CLI berhenti dengan pesan error tanpa membuka prompt username/password.

### Dari ZIP atau folder lokal

```bash
sudo refindmgr install ./theme.zip --activate
sudo refindmgr install ./folder-theme --activate
```

### Memilih varian

Di terminal interaktif, CLI menampilkan pilihan bernomor jika menemukan beberapa varian.

Untuk script atau mode noninteraktif, tentukan varian secara eksplisit:

```bash
sudo refindmgr install https://github.com/catppuccin/refind \
  --variant mocha \
  --activate
```

## Ganti varian tanpa instalasi ulang

Lihat varian tema yang sudah terpasang:

```bash
sudo refindmgr variant catppuccin
```

Ganti langsung:

```bash
sudo refindmgr variant catppuccin --set latte
```

Pergantian varian hanya membangun ulang `themes/<nama>/theme.conf` secara atomik. Repository tidak di-clone ulang, folder tema tidak dihapus, dan baris include dalam `refind.conf` tidak berubah.

## Command

| Command | Fungsi |
| --- | --- |
| `refindmgr` | Buka menu interaktif |
| `refindmgr --version` | Tampilkan versi |
| `refindmgr list` | Tampilkan tema terpasang dan aktif |
| `refindmgr catalog` | Tampilkan katalog teks |
| `refindmgr install <source>` | Pasang tema |
| `refindmgr activate <name>` | Aktifkan tema |
| `refindmgr deactivate` | Kembali ke tema default |
| `refindmgr remove <name>` | Hapus tema |
| `refindmgr variant <name>` | Lihat atau ganti varian |
| `refindmgr backup` | Buat backup konfigurasi |
| `refindmgr restore` | Pulihkan konfigurasi |
| `refindmgr declutter` | Sisakan Shutdown dan Reboot pada tools row |
| `refindmgr dedupe` | Audit/sembunyikan kernel atau fallback duplikat secara bertahap |
| `refindmgr clean-menu` | Buat menu OS-only dari loader tervalidasi |
| `refindmgr doctor` | Jalankan diagnostik |
| `refindmgr setup` | Preview setup rEFInd |
| `refindmgr setup --yes` | Jalankan setup rEFInd |

Semua command menerima `--refind-dir` sebelum atau setelah nama command:

```bash
refindmgr --refind-dir /boot/efi/EFI/refind list
refindmgr list --refind-dir /boot/efi/EFI/refind
```

Alternatifnya:

```bash
export REFIND_DIR=/boot/efi/EFI/refind
```

## Backup dan restore

Backup dibuat hanya ketika `refind.conf` benar-benar akan berubah.

- Mengaktifkan tema yang sudah aktif tidak menulis ulang konfigurasi.
- Backup dengan isi identik menggunakan snapshot terbaru yang sudah ada.
- Hanya lima backup terbaru yang dipertahankan secara default.
- Backup lama dari versi terdahulu dipangkas ketika daftar restore dibuka atau operasi tema dijalankan.

Ubah batas retensi jika diperlukan:

```bash
REFINDMGR_BACKUP_LIMIT=10 sudo refindmgr
```

Restore backup terbaru:

```bash
sudo refindmgr restore
```

Restore backup tertentu yang dikenal oleh refindmgr:

```bash
sudo refindmgr restore --backup /path/refind.conf.TIMESTAMP.bak
```

## Merapikan menu boot

### Declutter

```bash
sudo refindmgr declutter
sudo refindmgr declutter --undo
```

`declutter` hanya mengatur tools row menjadi Shutdown dan Reboot. Fitur ini tidak mengubah daftar OS yang dipindai.

### Dedupe

Preview terlebih dahulu:

```bash
sudo refindmgr dedupe
```

Contoh menyembunyikan entri kernel mentah:

```bash
sudo refindmgr dedupe --apply \
  --disable-kernels \
  --keep-loader EFI/ubuntu/shimx64.efi
```

Fallback hanya dapat disembunyikan jika byte-identik dengan loader yang dipertahankan.

### Clean menu

Preview deteksi otomatis:

```bash
sudo refindmgr clean-menu --auto
```

Terapkan setelah diperiksa:

```bash
sudo refindmgr clean-menu --auto --apply
```

Batalkan:

```bash
sudo refindmgr clean-menu --undo
```

`clean-menu` memprioritaskan shim jika tersedia, sehingga lebih aman untuk sistem Secure Boot.

## Keamanan

- HTTP tanpa TLS ditolak secara default.
- Git clone memiliki timeout dan tidak meminta credential secara interaktif.
- ZIP diperiksa terhadap path traversal dan symbolic link.
- Jumlah file serta ukuran arsip dan hasil ekstraksi dibatasi.
- Referensi banner, icon, selection image, dan font diverifikasi sebelum instalasi berhasil.
- Directive tema yang dapat mengubah perilaku boot dinonaktifkan secara default.
- `refind.conf` ditulis melalui temporary file dan `os.replace`.
- Penghapusan tema menggunakan staging/rollback.
- Version pinning dan direct package download hanya berjalan jika diminta eksplisit.

## Diagnostik

```bash
sudo refindmgr doctor
```

`doctor` menampilkan:

- versi refindmgr;
- lokasi rEFInd dan `refind.conf`;
- status Git dan akses root;
- stanza boot manual;
- loader EFI yang ditemukan;
- kernel mentah serta `refind_linux.conf` di `/boot`.

## Pengembangan

Jalankan seluruh test:

```bash
python3 -m unittest discover -v
```

Versi 2.1.1 memiliki lebih dari 140 test untuk konfigurasi, CLI, source theme, keamanan ZIP, deteksi varian, setup, dedupe, dan clean-menu.

## Lisensi

MIT License. Lihat [LICENSE](LICENSE).
