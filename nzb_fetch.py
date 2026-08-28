#!/usr/bin/env python3
"""
Pengunduh NZB langsung ke NNTP — dipakai di pod RunPod.

Kenapa bukan SABnzbd/nzbget: kita cuma butuh satu hal (ambil file dari NZB tsukihime
dan tulis ke disk). Keduanya bawa web UI, database antrian, dan file konfigurasi yang
harus diurus — semua itu beban tambahan untuk pod yang umurnya beberapa jam.

Pakai:
  python3 nzb_fetch.py file.nzb /workspace/dl --conn 40
Env yang dibaca: NNTP_HOST NNTP_PORT NNTP_USER NNTP_PASS
"""
import os
import re
import ssl
import sys
import time
import queue
import shutil
import socket
import threading
import subprocess
import xml.etree.ElementTree as ET

try:
    import sabyenc3                      # dekoder yEnc C, ~50x lebih cepat dari Python murni
except ImportError:
    sabyenc3 = None

NS = {"n": "http://www.newzbin.com/DTD/2003/nzb"}


def parse_nzb(path):
    """
    NZB → daftar file, tiap file berisi segmen terurut.

    Bagian par2 ikut dikembalikan dengan penanda `is_par2`, tidak lagi dibuang saat parsing.
    Berkas par2 hanya diunduh kalau ada segmen yang benar-benar hilang; itulah gunanya penanda
    ini, dan itu pula yang dulu tidak pernah terjadi karena par2 sudah tersaring di sini.
    """
    root = ET.parse(path).getroot()
    files = []
    for f in root.findall("n:file", NS) or root.findall("file"):
        subject = f.get("subject", "")
        groups = [g.text for g in f.iter() if g.tag.endswith("group")]
        segs = []
        for s in f.iter():
            if s.tag.endswith("segment") and s.text:
                segs.append((int(s.get("number", 0)), s.text.strip()))
        segs.sort()
        m = re.search(r'"([^"]+)"', subject)
        files.append({
            "name": m.group(1) if m else subject[:80],
            "groups": groups,
            "segments": segs,
            "is_par2": ".par2" in subject.lower(),
        })
    return files


def guess_episode(filename, fallback=None):
    """
    Nomor episode dari nama file — batch pack tidak memberitahu nomornya lewat API.

    Kurung siku/bulat dibuang lebih dulu karena hampir selalu berisi angka yang bukan nomor
    episode (`[1080p]`, `[10bit]`, `[E7991718]`, `(720p)`), dan angka-angka itu berada tepat
    sebelum ekstensi file — posisi yang paling menggoda untuk salah dibaca.

    Tinggal di sini, bukan di pipeline.py, karena dipakai lebih dulu: nama berkas di dalam NZB
    sudah cukup untuk tahu episode berapa yang dibawa sebuah segmen, jadi episode yang sudah
    kita punya bisa dilewati sebelum satu byte pun diunduh.
    """
    stem = re.sub(r"\.(mkv|mp4)$", "", filename, flags=re.I)

    # S01E17 paling tegas; dipakai kalau ada, sebelum apa pun dibuang.
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", stem)
    if m:
        return float(m.group(2))

    clean = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)          # buang metadata dalam kurung
    clean = re.sub(r"\b\d{3,4}p\b|\b(?:x|h)\.?26[45]\b|\b10\s?bits?\b", " ", clean, flags=re.I)

    m = re.search(r"\b(?:e|ep|episode)\s*[-_ ]?(\d{1,4})\b", clean, re.I)
    if m:
        return float(m.group(1))

    # Sisanya: angka terakhir yang berdiri sendiri, mis. "... - 151" atau "... - 05".
    nums = re.findall(r"(?<![\w.])(\d{1,4})(?:v\d)?(?![\w.])", clean)
    return float(nums[-1]) if nums else fallback


class Conn:
    """Satu koneksi NNTP ber-SSL. Dipakai ulang untuk banyak segmen."""

    def __init__(self, host, port, user, pw):
        ctx = ssl.create_default_context()
        # Beberapa penyedia Usenet memakai sertifikat yang tidak cocok dengan hostname
        # yang kita panggil; isinya sendiri sudah dilindungi enkripsi transport.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=30)
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.f = self.sock.makefile("rb")
        self._line()
        self._cmd(f"AUTHINFO USER {user}")
        r = self._cmd(f"AUTHINFO PASS {pw}")
        if not r.startswith(b"281"):
            raise RuntimeError(f"login ditolak: {r!r}")

    def _line(self):
        return self.f.readline()

    def _cmd(self, s):
        self.sock.sendall(s.encode() + b"\r\n")
        return self._line()

    def body(self, msgid):
        """Ambil satu segmen. None kalau server bilang tidak ada."""
        r = self._cmd(f"BODY <{msgid}>")
        if not r.startswith(b"222"):
            return None
        chunks = []
        while True:
            ln = self._line()
            if not ln or ln in (b".\r\n", b".\n"):
                break
            chunks.append(ln[1:] if ln.startswith(b"..") else ln)
        return chunks

    def close(self):
        try:
            self._cmd("QUIT")
            self.sock.close()
        except Exception:
            pass


def decode(chunks):
    """yEnc → byte mentah."""
    if sabyenc3:
        data, *_ = sabyenc3.decode_usenet_chunks(chunks)
        return bytes(data)

    # Cadangan Python murni: dipakai kalau sabyenc3 gagal dipasang.
    out = bytearray()
    body = b"".join(chunks)
    start = body.find(b"=ybegin")
    if start >= 0:
        body = body[body.find(b"\n", start) + 1:]
        if body.startswith(b"=ypart"):
            body = body[body.find(b"\n") + 1:]
    end = body.find(b"=yend")
    if end >= 0:
        body = body[:end]
    esc = False
    for b in body.replace(b"\r\n", b"").replace(b"\n", b""):
        if esc:
            out.append((b - 106) & 0xFF)
            esc = False
        elif b == 0x3D:
            esc = True
        else:
            out.append((b - 42) & 0xFF)
    return bytes(out)


def fetch_file(entry, outdir, host, port, user, pw, nconn):
    """
    Unduh semua segmen satu file secara paralel, lalu gabungkan berurutan.

    Tiap segmen ditulis ke berkas sementara begitu selesai di-decode, bukan ditahan di memori.
    Versi sebelumnya menyimpan seluruh isi file dalam list Python sampai unduhan rampung —
    pemakaian RAM sama besar dengan ukuran filenya, dan pod terpantau memakai 30-37% memori
    hanya untuk mengunduh. Satu batch berisi film 8-10 GB akan meminta RAM sebesar itu sekaligus
    dan proses dibunuh OOM di tengah jalan, menghanguskan seluruh unduhan.

    Sekarang pemakaian memori tetap konstan berapa pun ukuran filenya. Berkas sementara dihapus
    segera setelah disalin ke hasil akhir, jadi puncak pemakaian disk hanya sebesar file itu
    sendiri ditambah satu segmen.
    """
    segs = entry["segments"]
    total = len(segs)
    os.makedirs(outdir, exist_ok=True)
    parts_dir = os.path.join(outdir, f".parts-{os.getpid()}")
    os.makedirs(parts_dir, exist_ok=True)

    sizes = [0] * total
    q = queue.Queue()
    for i, (_, msgid) in enumerate(segs):
        q.put((i, msgid))

    done = [0]
    got = [0]
    lock = threading.Lock()
    t0 = time.time()

    def worker():
        try:
            c = Conn(host, port, user, pw)
        except Exception as e:
            print(f"  koneksi gagal: {e}", flush=True)
            return
        while True:
            try:
                i, msgid = q.get_nowait()
            except queue.Empty:
                break
            data = b""
            try:
                chunks = c.body(msgid)
                if chunks:
                    data = decode(chunks)
            except Exception:
                data = b""
            if data:
                with open(os.path.join(parts_dir, f"{i:06d}"), "wb") as fh:
                    fh.write(data)
            with lock:
                sizes[i] = len(data)
                got[0] += len(data)
                done[0] += 1
                if done[0] % 50 == 0 or done[0] == total:
                    el = time.time() - t0
                    print(f"  {done[0]}/{total} segmen  {got[0]/1e6:.0f} MB  "
                          f"{got[0]/1e6/max(el, 1):.1f} MB/s", flush=True)
        c.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(nconn)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    path = os.path.join(outdir, os.path.basename(entry["name"]))
    missing = sum(1 for s in sizes if not s)
    with open(path, "wb") as out:
        for i in range(total):
            p = os.path.join(parts_dir, f"{i:06d}")
            if not os.path.exists(p):
                continue
            with open(p, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
            os.remove(p)          # dibuang segera supaya disk tidak menampung dua salinan
    shutil.rmtree(parts_dir, ignore_errors=True)

    return path, missing, total


def repair_with_par2(outdir, par2_entries, host, port, user, pw, nconn):
    """
    Unduh berkas par2 lalu tambal segmen yang hilang. True kalau semua berhasil diperbaiki.

    Kenapa ini penting: unduhan di sini nyaris selalu selesai 96-98%, lalu dibuang seluruhnya.
    "Your Name" kehilangan 19 dari 1.030 segmen (1,8%) dan 271 dari 6.177 (4,4%) — dua-duanya
    jauh di bawah blok pemulihan 5-15% yang dibawa set par2 pada umumnya. Tanpa langkah ini
    4 GB terunduh penuh lalu terbuang, dan judulnya dihitung gagal.

    par2 memperbaiki berkas di tempat, jadi berkas cacat + volume pemulihan sudah cukup; tidak
    ada yang perlu diunduh ulang dari awal.
    """
    if not par2_entries:
        print("par2: tidak ada berkas pemulihan di NZB ini")
        return False
    if not shutil.which("par2"):
        print("par2: perkakas par2 tidak terpasang di pod — lewati perbaikan")
        return False

    print(f"par2: mengambil {len(par2_entries)} berkas pemulihan")
    for entry in par2_entries:
        try:
            fetch_file(entry, outdir, host, port, user, pw, nconn)
        except Exception as exc:                      # noqa: BLE001 - satu volume gagal masih bisa ditolong sisanya
            print(f"par2: gagal mengambil {entry['name']}: {exc}")

    index = sorted(
        (f for f in os.listdir(outdir) if f.lower().endswith(".par2")),
        key=len,                                       # indeks utama = nama terpendek, tanpa ".volNNN+NN"
    )
    if not index:
        print("par2: tidak ada indeks yang berhasil diunduh")
        return False

    cmd = ["par2", "repair", "-q", os.path.join(outdir, index[0])]
    print(f"par2: {' '.join(cmd[:3])} {index[0]}")
    try:
        proc = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        print("par2: perbaikan melewati batas waktu 1 jam")
        return False

    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
    for line in tail:
        print(f"par2: {line}")

    # Volume pemulihan besar dan tidak berguna lagi setelah ini.
    for f in os.listdir(outdir):
        if f.lower().endswith(".par2"):
            os.remove(os.path.join(outdir, f))

    return proc.returncode == 0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    nzb, outdir = sys.argv[1], sys.argv[2]
    nconn = int(sys.argv[sys.argv.index("--conn") + 1]) if "--conn" in sys.argv else 30

    host = os.environ["NNTP_HOST"]
    port = int(os.environ.get("NNTP_PORT", 563))
    user = os.environ["NNTP_USER"]
    pw = os.environ["NNTP_PASS"]

    print(f"server {host}:{port}  koneksi={nconn}  yenc={'sabyenc3' if sabyenc3 else 'python'}")

    entries = parse_nzb(nzb)
    par2_entries = [e for e in entries if e["is_par2"] and e["segments"]]
    media = [e for e in entries if not e["is_par2"] and e["segments"]]

    # Episode yang sudah tersimpan tidak perlu diunduh lagi. Tanpa ini satu batch penuh selalu
    # ditarik utuh lalu 25 dari 26 berkasnya dibuang tanpa diproses — menambal satu episode
    # Kimetsu no Yaiba yang hilang berarti mengunduh 9 GB untuk memakai 350 MB.
    skip = {float(x) for x in (sys.argv[sys.argv.index("--skip-eps") + 1].split(","))
            if x.strip()} if "--skip-eps" in sys.argv else set()

    if skip:
        keep = []
        for e in media:
            ep = guess_episode(e["name"])
            if ep is not None and ep in skip:
                print(f"lewati (sudah ada): ep{ep:g}  {e['name'][:70]}")
                continue
            keep.append(e)
        # Kalau semuanya tersaring, penebakan nomornya yang salah, bukan berkasnya yang tidak
        # perlu — arsip satu-berkas dan penamaan tak lazim jatuh ke sini. Lebih baik mengunduh
        # berlebih daripada memulangkan batch kosong dan menandai judulnya gagal.
        if keep:
            hemat = len(media) - len(keep)
            media = keep
            if hemat:
                print(f"hemat: {hemat} berkas dilewati, {len(media)} akan diunduh")
        else:
            print("peringatan: semua berkas tersaring — nomor episode tidak terbaca, unduh semua")

    damaged = []
    for entry in media:
        print(f"unduh: {entry['name']}  ({len(entry['segments'])} segmen)")
        path, missing, total = fetch_file(entry, outdir, host, port, user, pw, nconn)

        if missing:
            # Disimpan dulu, tidak langsung dibuang: par2 memperbaiki berkas di tempat, jadi
            # berkas cacatnya justru bahan yang dibutuhkan. Yang tidak tertolong dihapus nanti.
            damaged.append((path, missing, total))
            print(f"CACAT: {os.path.basename(path)}  KURANG {missing}/{total} segmen")
            continue

        print(f"selesai: {path}  {os.path.getsize(path)/1e6:.0f} MB  [LENGKAP]")

    if damaged:
        repaired = repair_with_par2(outdir, par2_entries, host, port, user, pw, nconn)
        for path, missing, total in damaged:
            if repaired and os.path.exists(path):
                print(f"DIPERBAIKI: {os.path.basename(path)}  {os.path.getsize(path)/1e6:.0f} MB")
                continue
            # Berkas dengan segmen hilang tetap terbentuk tapi isinya cacat — ffmpeg menolaknya
            # dengan "EBML header parsing failed". Dibiarkan lolos dengan exit 0 dulu membuat
            # pipeline mengira unduhan beres lalu gagal saat encode, dan satu judul penuh
            # ditandai gagal. Dibuang di sini supaya judulnya pindah ke kandidat berikutnya.
            if os.path.exists(path):
                os.remove(path)
            print(f"DIBUANG: {os.path.basename(path)}  KURANG {missing}/{total} segmen")

    # Exit != 0 hanya kalau TIDAK ADA berkas utuh sama sekali; batch yang sebagian rusak tetap
    # diteruskan supaya episode yang baik tidak ikut hangus.
    ok = [f for f in os.listdir(outdir) if f.lower().endswith((".mkv", ".mp4"))]
    print(f"ringkas: {len(ok)} berkas siap, {len(damaged)} sempat cacat")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
