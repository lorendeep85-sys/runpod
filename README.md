# tsuki-runpod

Worker RunPod Serverless untuk pipeline TsukiHime → katalog Aniplay.

Satu invocation = satu episode:

1. Klaim job dari antrean (`/tsuki/claim`)
2. Resolve rilisan di TsukiHime (prioritas SubsPlease; fallback batch pack + ambil episode target saja)
3. Unduh lewat Usenet (NNTP paralel + repair par2)
4. Encode **H.264 8-bit High** NVENC, ladder 1080p/720p/480p, faststart
5. Ekstrak semua subtitle → VTT
6. Upload ke R2, lapor `/tsuki/done`

Bahasa yang kurang (`id, ar, es, pt, fr, de`) diterjemahkan terpisah oleh runner 9router di VPS — tidak memakai GPU.

## Kenapa H.264 8-bit, bukan HEVC 10-bit

HEVC Main10 gagal diputar di banyak perangkat Android: decoder-nya kerap hanya mendukung profil Main (8-bit). Terbukti pada emulator uji — konfigurasi decoder-nya hanya `ProfileMain`. H.264 High 8-bit diputar di semua perangkat dan konsisten dengan katalog lama.

## Env yang dibutuhkan endpoint

| Variabel | Isi |
|---|---|
| `API` | base URL Worker (mis. `https://restapi.kntl.app`) |
| `TOKEN` | scraper token (header `X-Scraper-Token`) |
| `USENET_HOST` / `USENET_PORT` | server NNTP, port SSL 563 |
| `USENET_USER` / `USENET_PASS` | kredensial Usenet |
| `USENET_CONN` | jumlah koneksi paralel (default 50) |

Kredensial R2 tidak disimpan di sini — worker mengambilnya saat runtime dari `/scraper/r2-creds`.

## Catatan build

- `NVIDIA_DRIVER_CAPABILITIES` wajib menyertakan `video`, jika tidak `libnvidia-encode` tidak di-mount dan NVENC gagal.
- ffmpeg dipin ke build 2025-06 (NVENC API 13.0). Build terbaru menuntut driver 610+, sementara GPU cloud umumnya masih di 580.
- Handler dijalankan lewat `CMD`. Field `dockerStartCmd` pada template **tidak** dieksekusi untuk endpoint serverless.

## Uji cepat

```json
{ "input": { "smoke": 60 } }
```

`smoke` memotong encode ke N detik pertama supaya rantai kerja bisa diverifikasi tanpa memproses episode penuh. Kosongkan untuk episode utuh.
