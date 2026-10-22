# ARCDVN Code 🚀

ARCDVN Code adalah asisten AI coding (AI Agent) yang berjalan langsung di dalam terminal komputer Anda. Program ini dirancang dengan antarmuka yang modern, mudah dipasang, dan membantu Anda mengelola proyek dari terminal menggunakan bahasa alami. Berjalan di **Windows, macOS, dan Linux**.

## ✨ Fitur Utama

- **Berjalan Otomatis di Proyek**: Mampu membaca file, mengedit kode secara presisi (*search-and-replace* hemat token), dan melihat struktur folder secara mandiri.
- **Eksekusi Perintah Aman**: AI bisa menjalankan perintah terminal (seperti menginstal paket atau menjalankan script), namun akan **selalu** meminta izin `(y/N)` dari Anda terlebih dahulu. Menimpa atau mengedit file yang sudah ada juga selalu meminta izin.
- **Dukungan Multi-Provider (Google Gemini & OpenRouter)**: Bebas memilih antara **Google Gemini API** (default: `gemini-3.8-flash`) atau **OpenRouter** (berbagai model open-source/komersial). Kedua kunci API disimpan terpisah dan aman.
- **Konfigurasi Global & Uji Koneksi**: Pengaturan tersimpan secara aman di `~/.arcdvncli_env` (izin 600 di Unix). Fitur uji koneksi memungkinkan verifikasi status API sebelum memulai tugas.

## 🛠️ Cara Install

### Cara 1: pipx (semua OS, direkomendasikan)

Prasyarat: **Python 3.10+**, **git**, dan **pipx**.

| OS | Pasang pipx (sekali saja) |
|---|---|
| Windows | `python -m pip install --user pipx` lalu `python -m pipx ensurepath` |
| macOS | `brew install pipx` lalu `pipx ensurepath` |
| Ubuntu/Debian | `sudo apt install pipx` lalu `pipx ensurepath` |

Setelah itu **tutup dan buka lagi terminal**, lalu:

```bash
pipx install git+https://github.com/Magelanicc/arcdvncli.git@v0.2.0
arcdvncli --version
```

(Ganti `v0.2.0` dengan versi terbaru di halaman [Releases](https://github.com/Magelanicc/arcdvncli/releases).)

### Cara 2: Binary siap pakai (tanpa Python)

Unduh file sesuai OS Anda dari halaman [Releases](https://github.com/Magelanicc/arcdvncli/releases):
`arcdvncli-windows.exe`, `arcdvncli-linux`, atau `arcdvncli-macos` (Apple Silicon).
Di Linux/macOS jalankan dulu `chmod +x arcdvncli-linux` (atau `-macos`).

### Cara 3: Dari source code

- Windows: klik dua kali **`install.bat`**
- Linux/macOS: `./install.sh`

## 🎮 Cara Penggunaan

Masuk ke folder proyek Anda, lalu ketik:
```bash
arcdvncli
```
Jika ini pertama kalinya, ketik `/settings` untuk memilih provider (Google Gemini / OpenRouter) dan mengatur API Key Anda.
Perintah bawaan:
- `/help` : Melihat daftar bantuan lengkap
- `/settings` : Mengubah Provider AI (Gemini / OpenRouter), API Key, Model, dan Uji Koneksi
- `/status` : Melihat info proyek, git branch, jumlah pesan konteks, provider, dan model
- `/diff` : Melihat git diff perubahan kode di proyek saat ini
- `/details` : Mengaktifkan/menonaktifkan detail output tool
- `/clear` : Membersihkan riwayat percakapan jika AI mulai bingung
- `/uninstall` : Copot pemasangan ARCDVN Code dan bersihkan semua data
- `/exit` / `/quit` : Menutup aplikasi

## 🔄 Update

```bash
pipx install --force git+https://github.com/Magelanicc/arcdvncli.git@vX.Y.Z
```
ARCDVN akan memberi tahu saat ada versi baru (dicek maksimal sekali sehari). Untuk mematikan: setel variabel lingkungan `ARCDVN_NO_UPDATE_CHECK=1`.

## 🗑️ Uninstall Bersih (Full Clean Uninstall)

Untuk mencopot ARCDVN Code sekaligus menghapus seluruh API Key, riwayat chat, dan cache sampai bersih tanpa sisa:

```bash
arcdvncli --uninstall
```

*(Atau ketik `/uninstall` dari dalam aplikasi).*

Metode manual:
- pipx saja: `pipx uninstall arcdvncli`
- Windows: klik dua kali **`uninstall.bat`**
- Linux/macOS: `./uninstall.sh`

## 💻 Untuk Developer

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
python tests/smoke_test.py
python -m unittest discover -s tests -v
```

Rilis: ubah `__version__` di `arcdvncli/__init__.py`, commit, lalu `git tag vX.Y.Z && git push origin vX.Y.Z`. GitHub Actions otomatis menguji di 3 OS, membuat binary, dan menerbitkan Release.

---
