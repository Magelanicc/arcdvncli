# ARCDVN Code 🚀

ARCDVN Code adalah asisten AI coding (AI Agent) yang berjalan langsung di dalam terminal komputer Anda. Program ini dirancang dengan antarmuka yang modern, mudah dipasang, dan membantu Anda mengelola proyek dari terminal menggunakan bahasa alami.

## ✨ Fitur Utama

- **Berjalan Otomatis di Proyek**: Mampu membaca file, mengedit file, dan melihat struktur folder secara mandiri (jika Anda suruh).
- **Eksekusi Perintah Aman**: AI bisa menjalankan perintah CMD (seperti menginstal paket atau menjalankan script), namun akan **selalu** meminta izin `(y/N)` dari Anda terlebih dahulu untuk keamanan.
- **Bebas Pilih Model AI**: Anda bisa memasukkan API Key dari **OpenRouter** dan bebas memilih model AI apa pun yang ingin dipakai.
- **Konfigurasi Global**: Pengaturan API Key akan tersimpan aman di komputer Anda, cukup disetel sekali via terminal.

## 🛠️ Cara Install (Pengguna Windows)

Sangat gampang! Jika Anda menggunakan Windows, Anda tidak perlu repot mengetik perintah yang rumit. 
1. Cukup klik dua kali pada file **`install.bat`**.
2. Tunggu prosesnya selesai.
3. Buka Terminal / CMD baru, lalu ketik `arcdvncli`!

Untuk **menghapusnya**, Anda juga cukup mengklik dua kali file **`uninstall.bat`** (100% bersih tanpa sisa).

## 🎮 Cara Penggunaan
Buka terminal dan ketik:
```bash
arcdvncli
```
Jika ini adalah pertama kalinya Anda menjalankannya, ketik `/settings` untuk mengatur API Key OpenRouter Anda. 
Beberapa perintah bawaan yang tersedia:
- `/help` : Melihat daftar bantuan
- `/clear` : Membersihkan memori/history percakapan jika AI mulai bingung
- `/settings` : Mengubah API Key dan Model
- `/exit` : Menutup aplikasi

## 💻 Fitur Untuk Developer
Jika Anda seorang Developer dan ingin mem-build project ini menjadi sebuah program mandiri (`.exe`), cukup jalankan file `build_exe.bat`. File hasil jadinya akan otomatis muncul di dalam folder `dist`.

---
*Dibuat untuk mempermudah coding langsung dari terminal.*
