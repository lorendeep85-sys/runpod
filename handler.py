#!/usr/bin/env python3
"""RunPod Serverless handler — TsukiHime encode worker (H.264 8-bit, universal).

Tiap invocation: klaim 1 job dari antrean Worker -> resolve tsukihime (episode/batch,
SubsPlease priority) -> download Usenet -> encode H.264 NVENC ladder -> ekstrak sub ->
upload R2 -> lapor /tsuki/done. Return status. RunPod autoscale = paralel.

Env (di endpoint RunPod): API, TOKEN, USENET_USER, USENET_PASS, USENET_HOST, USENET_PORT, USENET_CONN.
"""
import gzip, json, os, re, subprocess, sys, time, urllib.parse, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release_picker
import runpod

TSUKI = "https://api.tsukihime.org/v1"; STORAGE = "https://storage.tsukihime.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/120"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg"); FFPROBE = os.environ.get("FFPROBE", "ffprobe")
API = os.environ["API"]; TOKEN = os.environ["TOKEN"]
CONNS = os.environ.get("USENET_CONN", "50")
# medium, bukan slow: slow hanya memangkas ~5-8% ukuran tapi menambah hampir dua kali
# waktu encode. Bisa digeser lewat env tanpa build ulang.
X264_PRESET = os.environ.get("X264_PRESET", "medium")
# "cpu" (bawaan) = NVDEC decode di GPU + x264 paralel di CPU. Dipilih karena waktu encode
# dibayar sekali sedangkan ukuran berkas dibayar tiap kali ditonton: pada sumber setara
# x264 menghasilkan 205MB vs 371MB lewat NVENC, dan selisih 45% itu berlaku selamanya
# di penyimpanan maupun bandwidth R2.
# "gpu" = seluruh jalur di GPU: NVDEC + scale_cuda + NVENC. Pada uji berdampingan
# dengan sumber setara (1449MB vs 1410MB) jalur ini menghasilkan 282MB dalam 6,8 menit,
# dibanding 374MB dalam 18,8 menit lewat x264 -- lebih kecil sekaligus jauh lebih cepat.
# Catatan: keduanya tidak membidik kualitas yang sama (CQ 29 vs CRF 22), jadi selisih
# ukurannya sebagian berasal dari sasaran mutu yang berbeda, bukan efisiensi semata.
# "cpu" = NVDEC decode + x264 encode, untuk bila mutu perlu dinaikkan lagi.
PIPELINE = os.environ.get("PIPELINE", "cpu").lower()


def cpu_quota():
    """Jatah CPU yang benar-benar berlaku, bukan jumlah inti host.

    Di dalam container os.cpu_count() melaporkan CPU host (48 di mesin ini) padahal
    cgroup bisa membatasi jauh lebih kecil -- jebakan yang sama seperti /proc/meminfo.
    Menyetel thread x264 dari angka host akan membuat proses saling berebut inti.
    """
    try:
        v = open("/sys/fs/cgroup/cpu.max").read().split()          # cgroup v2
        if v[0] != "max":
            return max(1, round(int(v[0]) / int(v[1])))
    except Exception:
        pass
    try:                                                            # cgroup v1
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        pr = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(1, q // pr)
    except Exception:
        pass
    return os.cpu_count() or 4
TARGET_LANGS = ["id", "ar", "es", "pt", "fr", "de"]
TEXT_SUB = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
# H.264 8-bit High — universal Android compat (bukan HEVC 10-bit yg banyak HP tak bisa).
# Angka ketiga = CRF x264 (bukan CQ NVENC; skalanya tidak setara).
# CRF meniru kepadatan AnimePahe (sama-sama sumber SubsPlease, sama-sama libx264 High
# 8-bit level 4.0). Nilai CRF persisnya tak bisa dipulihkan dari berkas mereka karena
# string opsi x264-nya terhapus saat di-mux ulang ffmpeg, jadi dicocokkan lewat kepadatan:
# CRF 22 di sini menghasilkan ~2180 kb/s, sementara AnimePahe pada judul menuntut
# (Tsuihou Juukishi 09) memakai 2713 kb/s -- jadi CRF 22 sudah sedikit lebih padat.
#
# Sempat dinaikkan ke 26 atas dugaan mereka membidik 750-950 kb/s; dugaan itu keliru,
# ditarik dari tiga sampel yang kebetulan semuanya episode berkonten ringan.
# (nama, lebar, crf, maxrate kb/s). Maxrate adalah rem: CRF murni tak punya batas atas,
# dan sumber HEVC 10-bit yang padat membuatnya meledak -- Ghost in the Shell e8 keluar
# 746MB dari sumber 1014MB (4340 kb/s), lebih boros dari judul terberat AnimePahe.
# Dengan plafon ini konten ringan tetap turun jauh di bawahnya, yang berat mentok di plafon.
LADDER = [("1080p", 1920, 22, 2000), ("720p", 1280, 23, 1000), ("480p", 854, 24, 500)]
DL = "/tmp/dl"


def log(m): print(m, flush=True)
def hj(url, tries=6):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=40).read())
        except Exception:
            if i == tries - 1: raise
            time.sleep(min(5 * (i + 1), 30))
def api_post(path, body):
    r = urllib.request.Request(f"{API}/api/v1/scraper{path}", data=json.dumps(body).encode(),
        headers={"X-Scraper-Token": TOKEN, "Content-Type": "application/json", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(r, timeout=40).read())
def norm_lang(t):
    t = (t or "").lower().split("-")[0].split("_")[0]
    m = {"eng": "en", "spa": "es", "por": "pt", "fre": "fr", "fra": "fr", "ger": "de",
         "deu": "de", "ara": "ar", "ind": "id", "jpn": "ja", "und": ""}
    t = m.get(t, t); return t if len(t) == 2 else ""
def nzb_url(tid, name): return f"{STORAGE}/{'tosho/nzbs' if tid >= 1_000_000 else 'nzbs'}/{tid}/{urllib.parse.quote(name)}.nzb.gz"
def fetch_nzb(url, dest):
    raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "aniplay-mirror/1.0"}), timeout=120).read()
    open(dest, "wb").write(gzip.decompress(raw) if url.endswith(".gz") else raw); return dest
def ffdur(p):
    try:
        o = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", p], capture_output=True, text=True, timeout=60)
        return float((o.stdout or "0").strip() or 0)
    except Exception: return 0
def src_width(src):
    o = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "default=nk=1:nw=1", src], capture_output=True, text=True, timeout=60)
    try: return int((o.stdout or "0").strip() or 0)
    except Exception: return 0
def ladder_for(w):
    if not w: return [LADDER[1]]
    return [r for r in LADDER if r[1] <= w + 16] or [LADDER[-1]]

def _range_covers(name, ep):
    best = None
    for m in re.finditer(r"(\d{1,4})\s*[-~]\s*(\d{1,4})", name):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b and a <= ep <= b and (b - a) <= 200:
            if best is None or (b - a) < (best[1] - best[0]): best = (a, b)
    return best

# Nomor episode dari NAMA rilis, untuk berkas yang episode_no-nya kosong.
# TsukiHime tidak selalu menandai nomor episode: "[Erai-raws] Mahoutsukai no Yome
# - 16 [1080p].mkv" datang dengan episode_no=None dan filecount=1, sehingga luput
# dari jalur per-episode (yang mengandalkan episode_no) MAUPUN jalur batch (yang
# menuntut filecount>1). Rilisnya ada, tapi tak terlihat oleh keduanya.
#
# Sengaja ketat. Angka lain di nama rilis berlimpah -- 1080p, x264, 10bit, AAC
# 2.0, tahun, hash -- jadi tiap pola dikunci ke bentuk yang khas nomor episode,
# dan rentang seperti "01 ~ 24" ditolak lebih dulu karena itu batch.
_EP_POLA = [
    re.compile(r'\bS\d{1,2}E(\d{1,4})\b', re.I),                      # S01E16
    re.compile(r'\bEp(?:isode)?[ ._]*(\d{1,4})\b', re.I),             # Episode 16 / Ep.16
    re.compile(r'[-–] (\d{1,4})(?:v\d)?(?= *[\[(]| *$|\.mkv|\.mp4)', re.I),  # " - 16 [" / " - 16v2.mkv"
]
_EP_RENTANG = re.compile(r'\b\d{1,4} *[~–] *\d{1,4}\b|\bS\d{1,2}E\d{1,4} *- *E?\d{1,4}\b', re.I)


def _ep_dari_nama(nama, ep):
    """True bila nama rilis menunjuk TEPAT episode ini, bukan rentang."""
    n = str(nama or "")
    if _EP_RENTANG.search(n):
        return False
    for pola in _EP_POLA:
        m = pola.search(n)
        if m and int(m.group(1)) == int(ep):
            return True
    return False


def resolve(anilist_id, ep):
    """(rilis, rentang, sebab). Sebab berawalan "!" = PERMANEN, jangan diulang.

    Pembedaan ini yang selama ini hilang: "tidak menemukan kandidat" diucapkan
    sama persis entah TsukiHime menjawab "tidak ada" atau tidak menjawab sama
    sekali. Backend lalu mengunci job permanen pada percobaan pertama, sehingga
    gangguan sesaat mematikan job selamanya.
    """
    if not anilist_id: return None, None, "!no anilist_id"
    try: a = hj(f"{TSUKI}/animes/anilist/{anilist_id}")
    except Exception as e: return None, None, f"tsuki tak terjangkau: {e}"
    aid = (a.get("anime") or a).get("id") or a.get("id")
    if not aid: return None, None, "!not on tsuki"
    tor = []
    for off in (0, 100, 200, 300):
        d = hj(f"{TSUKI}/animes/{aid}?limit=100&offset={off}"); res = d.get("results", []); tor += res
        if len(res) < 100: break
    cands = [t for t in tor if t.get("episode_no") == ep and t.get("has_nzb")]
    if cands:
        # JANGAN pra-saring per grup di sini. Baris ini dulu memaksa
        # SubsPlease-saja, sehingga prioritas berjenjang di release_picker
        # (erai-raws > subsplease > lainnya) TIDAK PERNAH terpakai. Serahkan
        # seluruh kandidat ke penskor.
        p = release_picker.pick(cands, top=1, allow_relax=False)
        if p: return p[0], None, "episode"
    # Jalur kedua: rilis satu-episode yang nomornya hanya ada di NAMA.
    if not cands:
        byname = [t for t in tor
                  if t.get("has_nzb") and t.get("episode_no") is None
                  and (t.get("filecount") or 1) <= 1
                  and _ep_dari_nama(t.get("name"), ep)]
        if byname:
            p = release_picker.pick(byname, top=1, allow_relax=False)
            if p: return p[0], None, "episode"

    batch = []
    for t in tor:
        if t.get("has_nzb") and t.get("episode_no") is None and (t.get("filecount") or 0) > 1:
            rng = _range_covers(t.get("name", ""), ep)
            if rng: batch.append((t, rng))
    if batch:
        batch.sort(key=lambda x: (x[1][1] - x[1][0])); t, rng = batch[0]; return t, rng, "batch"
    return None, None, "no release (episode/batch)"

def encode_ladder_gpu(src, rungs, seconds=0):
    """Seluruh jalur di GPU: NVDEC -> scale_cuda -> NVENC, frame tak pernah turun ke RAM.

    Tercepat yang mungkin, tapi NVENC selalu lebih boros daripada x264 pada mutu setara --
    itu batas perangkat kerasnya. Dipakai hanya bila env PIPELINE=gpu.
    """
    b = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if seconds: b += ["-t", str(seconds)]
    b += ["-i", src]
    labels = [f"v{i}" for i in range(len(rungs))]
    fc = "[0:v]split=%d%s;" % (len(rungs), "".join(f"[s{i}]" for i in range(len(rungs))))
    # format=nv12 wajib: sumber HEVC 10-bit didecode jadi p010 di memori GPU, sementara
    # NVENC H.264 hanya menerima 8-bit. Tanpa konversi ini ffmpeg menolak dengan
    # "Invalid argument" -- dan penyebabnya tak terbaca dari pesan itu.
    fc += ";".join(f"[s{i}]scale_cuda={w}:-2:format=nv12[{labels[i]}]"
                   for i, (_, w, _, _, _) in enumerate(rungs))
    cmd = b + ["-filter_complex", fc]
    for i, (_, _, crf, mx, dest) in enumerate(rungs):
        cmd += ["-map", f"[{labels[i]}]", "-map", "0:a:0?",
                "-c:v", "h264_nvenc", "-preset", "p6", "-profile:v", "high", "-level", "4.0",
                "-rc", "vbr", "-cq", str(crf + 7), "-b:v", "0",
                "-maxrate", f"{mx}k", "-bufsize", f"{mx*2}k", "-bf", "3", "-b_ref_mode", "middle",
                "-rc-lookahead", "32", "-multipass", "fullres",
                "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "5",
                "-c:a", "aac", "-ac", "2", "-b:a", "96k", "-af", "aresample=async=1000",
                "-dn", "-movflags", "+faststart", "-y", dest]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    ok = r.returncode == 0 and all(os.path.exists(d) and os.path.getsize(d) > 200_000
                                  for _, _, _, _, d in rungs)
    if not ok:
        raise RuntimeError(f"ladder gpu gagal: {(r.stderr or '')[-500:]}")
    return "nvenc+nvdec"


def encode_ladder_cpu_par(src, rungs, seconds=0):
    """Tiap rung sebagai proses ffmpeg tersendiri, berjalan serentak.

    Decode sudah ditangani NVDEC di GPU, jadi mendecode tiga kali nyaris tak berbiaya --
    dan itu membebaskan tiap rung memakai kumpulan thread x264 sendiri. Lebih cepat
    daripada satu proses berisi tiga encoder yang berebut thread, karena x264 menskala
    makin buruk di atas belasan thread: tiga proses berthread sedang mengalahkan satu
    proses berthread besar.
    """
    total = cpu_quota()
    per = max(2, min(16, total // max(1, len(rungs))))
    log(f"  cpu quota {total}, {len(rungs)} rung x {per} thread")

    def cmd_for(name, w, crf, mx, dest, hw):
        b = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if hw: b += ["-hwaccel", "cuda"]
        if seconds: b += ["-t", str(seconds)]
        return b + ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale={w}:-2",
                    "-c:v", "libx264", "-preset", X264_PRESET, "-tune", "animation",
                    "-threads", str(per), "-profile:v", "high", "-level", "4.0",
                    "-pix_fmt", "yuv420p", "-crf", str(crf),
                    "-maxrate", f"{mx}k", "-bufsize", f"{mx*2}k",
                    "-c:a", "aac", "-ac", "2", "-b:a", "96k", "-af", "aresample=async=1000",
                    "-dn", "-movflags", "+faststart", "-y", dest]

    def launch(hw, subset):
        # stderr diarahkan ke berkas, bukan PIPE: tiga proses berjalan serentak sementara
        # kita menunggunya berurutan, dan pipa yang penuh akan membekukan proses yang
        # belum sempat dibaca.
        procs = []
        for r in subset:
            ef = open(os.path.join(DL, f".err-{r[0]}"), "w+b")
            procs.append((r, subprocess.Popen(cmd_for(*r, hw), stderr=ef), ef))
        bad = []
        for r, pr, ef in procs:
            try:
                pr.wait(timeout=14400)
            except subprocess.TimeoutExpired:
                pr.kill()
            ef.seek(0); err = ef.read().decode(errors="replace")[-300:]; ef.close()
            try: os.remove(ef.name)
            except OSError: pass
            if pr.returncode != 0 or not os.path.exists(r[4]) or os.path.getsize(r[4]) <= 200_000:
                bad.append((r, err))
        return bad

    bad = launch(True, rungs)
    if bad:
        # NVDEC menolak sebagian sumber (mis. H.264 10-bit). Ulangi hanya rung yang gagal.
        log(f"  {len(bad)} rung gagal dgn nvdec, ulang dengan decode CPU")
        bad2 = launch(False, [r for r, _ in bad])
        if bad2:
            raise RuntimeError(f"ladder cpu gagal: {bad2[0][1]}")
        return "x264" if len(bad) == len(rungs) else "x264+nvdec"
    return "x264+nvdec"


def encode_ladder(src, rungs, seconds=0):
    """Encode seluruh ladder dalam SATU perintah ffmpeg.

    Versi sebelumnya memanggil ffmpeg sekali per rung, sehingga sumber 1,4 GB dibongkar
    tiga kali -- dan decode justru bagian terberat bagi CPU, bukan encode. Dengan filter
    `split` sumber didecode sekali lalu dialirkan ke tiga encoder sekaligus, yang juga
    membuat ketiganya berjalan paralel dan mengisi vCPU jauh lebih rapat.

    rungs = [(name, width, crf, dest), ...]. Mengembalikan "x264+nvdec" atau "x264".
    """
    if PIPELINE == "gpu":
        return encode_ladder_gpu(src, rungs, seconds)
    if PIPELINE == "cpu":
        return encode_ladder_cpu_par(src, rungs, seconds)

    def build(hw):
        b = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
        # NVDEC adalah unit terpisah dari NVENC: decode berpindah ke GPU sementara x264
        # tetap mengencode di CPU. Tanpa -hwaccel_output_format frame disalin balik ke RAM,
        # yang memang dibutuhkan x264. Decode adalah rem utama pipeline ini, jadi
        # memindahkannya membebaskan CPU sepenuhnya untuk encode.
        if hw: b += ["-hwaccel", "cuda"]
        if seconds: b += ["-t", str(seconds)]
        return b + ["-i", src]

    labels = [f"v{i}" for i in range(len(rungs))]
    fc = "[0:v]split=%d%s;" % (len(rungs), "".join(f"[s{i}]" for i in range(len(rungs))))
    fc += ";".join(f"[s{i}]scale={w}:-2[{labels[i]}]"
                   for i, (_, w, _, _, _) in enumerate(rungs))
    tail = ["-filter_complex", fc]
    for i, (_, _, crf, mx, dest) in enumerate(rungs):
        tail += ["-map", f"[{labels[i]}]", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", X264_PRESET, "-tune", "animation",
                "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p", "-crf", str(crf),
                "-maxrate", f"{mx}k", "-bufsize", f"{mx*2}k",
                "-c:a", "aac", "-ac", "2", "-b:a", "96k", "-af", "aresample=async=1000",
                "-dn", "-movflags", "+faststart", "-y", dest]
    def run(hw):
        r = subprocess.run(build(hw) + tail, capture_output=True, text=True, timeout=14400)
        ok = r.returncode == 0 and all(os.path.exists(d) and os.path.getsize(d) > 200_000
                                       for _, _, _, _, d in rungs)
        return ok, (r.stderr or "")[-500:]

    ok, err = run(True)
    if ok: return "x264+nvdec"
    # NVDEC menolak sebagian sumber (mis. H.264 10-bit yang tak didukungnya). Ulangi
    # dengan decode CPU sebelum menyerah, supaya episode tidak gagal hanya karena itu.
    log(f"  nvdec gagal ({err[:120]}), ulang dengan decode CPU")
    ok, err2 = run(False)
    if ok: return "x264"
    raise RuntimeError(f"ladder gagal: {err2}")

def encode_one(src, dest, w, crf, mx=0, seconds=0):
    """H.264 8-bit High. x264 CPU sebagai encoder utama, NVENC hanya cadangan.

    x264 dengan `-tune animation` jauh lebih padat daripada NVENC pada mutu setara:
    tune itu melonggarkan deblocking dan menurunkan psy-rd agar cocok dengan konten
    bergaris tegas dan berbidang warna rata. NVENC tidak punya padanannya dan selalu
    boros -- ladder CQ 23 sempat menghasilkan 1120 MB dari sumber 1447 MB, nyaris tanpa
    pemampatan. NVENC tetap dipertahankan sebagai jaring pengaman bila x264 gagal.

    Tetap 8-bit High, bukan High 10: profil 10-bit bermasalah di decoder Android persis
    seperti HEVC Main10 yang sudah ditinggalkan project ini.
    """
    base = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if seconds: base += ["-t", str(seconds)]
    base += ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale={w}:-2"]
    aud = ["-c:a", "aac", "-ac", "2", "-b:a", "96k", "-af", "aresample=async=1000",
           "-dn", "-movflags", "+faststart", "-y", dest]

    x = base + ["-c:v", "libx264", "-preset", X264_PRESET, "-tune", "animation",
                "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
                "-crf", str(crf)] + (["-maxrate", f"{mx}k", "-bufsize", f"{mx*2}k"] if mx else []) + aud
    r = subprocess.run(x, capture_output=True, text=True, timeout=14400)
    if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 200_000:
        return "x264"

    # NVENC memakai skala kualitas sendiri; +7 mendekatkan ukurannya ke CRF x264 di atas.
    nv = base + ["-c:v", "h264_nvenc", "-preset", "p6", "-profile:v", "high", "-pix_fmt", "yuv420p",
                 "-rc", "vbr", "-cq", str(crf + 7), "-b:v", "0", "-bf", "3", "-b_ref_mode", "middle",
                 "-rc-lookahead", "32", "-multipass", "fullres",
                 "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "5"] \
                 + (["-maxrate", f"{mx}k", "-bufsize", f"{mx*2}k"] if mx else []) + aud
    r2 = subprocess.run(nv, capture_output=True, text=True, timeout=14400)
    if r2.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 200_000:
        return "nvenc"
    raise RuntimeError(f"encode gagal: x264={(r.stderr or '')[-400:]} | nvenc={(r2.stderr or '')[-200:]}")

def extract_subs(src, outdir):
    o = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "s", "-show_entries", "stream=index,codec_name:stream_tags=language", "-of", "json", src], capture_output=True, text=True, timeout=60)
    out = {}
    for i, s in enumerate(json.loads(o.stdout or "{}").get("streams", [])):
        if (s.get("codec_name") or "").lower() not in TEXT_SUB: continue
        lang = norm_lang((s.get("tags", {}) or {}).get("language")); key = lang or f"und{i}"
        d = os.path.join(outdir, f"sub.{key}.vtt")
        r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src, "-map", f"0:s:{i}", "-y", d], capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and os.path.exists(d) and os.path.getsize(d) > 50: out.setdefault(key, d)
    return out

def s3_client():
    import boto3
    c = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{API}/api/v1/scraper/r2-creds", headers={"X-Scraper-Token": TOKEN, "User-Agent": UA}), timeout=30).read())
    s3 = boto3.client("s3", endpoint_url=f"https://{c['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=c["access_key_id"], aws_secret_access_key=c["secret_access_key"], region_name="auto")
    return s3, c["bucket"]

_EV = {"e": None}
_JOB = {"id": None}

def prog(msg):
    """Tandai tahap berjalan. Kalau worker dibunuh, tahap terakhir tetap terbaca di /status,
    sehingga titik gagal tidak perlu ditebak dari log yang tak bisa diambil lewat API."""
    log(msg)
    try:
        if _EV["e"] is not None:
            runpod.serverless.progress_update(_EV["e"], msg)
    except Exception:
        pass
    # Diteruskan ke backend agar terbaca di dashboard. Status job saja tidak cukup:
    # tanpa ini panel hanya tahu "dikerjakan" tanpa tahu sedang mengunduh segmen ke berapa
    # atau mengencode rung yang mana.
    try:
        if _JOB["id"]:
            api_post("/tsuki/progress", {"job_id": _JOB["id"], "msg": msg})
    except Exception:
        pass

def process_job(job, s3, bucket, smoke=0):
    sid = job["series_id"]; ep = int(job["episode_number"]); jid = job["id"]
    _JOB["id"] = jid
    log(f"job {jid}: {job.get('title')} ep{ep} (series {sid})")
    # Rilis yang dipilih operator lewat panel dipakai apa adanya; pemilihan otomatis
    # hanya berjalan bila job tidak membawa torrent tertentu.
    # Job yang membawa nzb_url berasal dari sumber SELAIN TsukiHime (aninzb),
    # yang memberi tautan NZB langsung alih-alih id yang bisa disusun jadi URL.
    # Cabang ini harus di ATAS torrent_id: job aninzb tidak punya torrent_id, jadi
    # tanpa ini ia jatuh ke pemilih otomatis dan tautannya diabaikan diam-diam.
    if job.get("nzb_url"):
        rel = {"id": 0, "name": job.get("torrent_name") or "", "nzb_url": job["nzb_url"]}
        rng, mode = None, "episode"
        log(f"  sumber {job.get('sumber') or 'luar'}: {rel['name'][:66]}")
    elif job.get("torrent_id"):
        rel = {"id": job["torrent_id"], "name": job.get("torrent_name") or ""}
        rng = _range_covers(rel["name"], ep)
        mode = "batch" if rng else "episode"
        log(f"  rilis pilihan operator: {rel['name'][:66]}")
    else:
        rel, rng, mode = resolve(job.get("anilist_id"), ep)
    if not rel:
        # "!" di depan = pod YAKIN ini tidak akan berubah bila diulang. Tanpa
        # penanda itu backend mengulang, dan itu memang yang benar saat kita
        # tidak yakin.
        tetap = mode.startswith("!")
        api_post("/tsuki/fail", {"job_id": jid, "reason": mode.lstrip("!"), "permanent": tetap})
        return {"skip": mode}
    t = rel["torrent"] if isinstance(rel, dict) and "torrent" in rel else rel
    log(f"  [{mode}] {t['name'][:66]}")
    os.makedirs(DL, exist_ok=True)
    for f in os.listdir(DL):
        try: os.remove(os.path.join(DL, f))
        except OSError: pass
    try:
        # nzb_url dipakai apa adanya bila ada; kalau tidak, disusun dari id
        # TsukiHime seperti sebelumnya.
        nzb = fetch_nzb(t.get("nzb_url") or nzb_url(t["id"], t["name"]), os.path.join(DL, "job.nzb"))
        cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "nzb_fetch.py"), nzb, DL, "--conn", CONNS]
        if mode == "batch" and rng:
            a, b = rng; cmd += ["--skip-eps", ",".join(str(x) for x in range(a, b + 1) if x != ep)]
        # nzb_fetch membaca NNTP_*, sementara endpoint diisi USENET_* agar seragam dengan
        # runner lain. Tanpa jembatan ini subprocess mati seketika karena KeyError dan
        # kegagalannya menyamar jadi "episode tak ada di batch".
        senv = dict(os.environ)
        for src_k, dst_k in (("USENET_HOST", "NNTP_HOST"), ("USENET_PORT", "NNTP_PORT"),
                             ("USENET_USER", "NNTP_USER"), ("USENET_PASS", "NNTP_PASS")):
            if os.environ.get(src_k) and not senv.get(dst_k):
                senv[dst_k] = os.environ[src_k]
        miss = [k for k in ("NNTP_HOST", "NNTP_USER", "NNTP_PASS") if not senv.get(k)]
        if miss:
            r = "env usenet belum diset: " + ",".join(miss)
            api_post("/tsuki/fail", {"job_id": jid, "reason": r}); return {"fail": r}
        prog(f"unduh mulai [{mode}]")
        # Keluaran nzb_fetch dibaca baris demi baris supaya kemajuan segmen bisa diteruskan
        # ke dashboard saat berlangsung. Dengan subprocess.run biasa, unduhan 1,4 GB tampak
        # sebagai jeda hening berpuluh detik tanpa keterangan apa pun.
        dl = subprocess.Popen(cmd, env=senv, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, bufsize=1)
        t_last = 0.0
        for line in dl.stdout:
            line = line.rstrip()
            if line:
                log(line)
            if "segmen" in line and time.time() - t_last > 5:
                t_last = time.time()
                prog("unduh " + line.strip())
        try:
            dl.wait(timeout=10800)
        except subprocess.TimeoutExpired:
            dl.kill()
        rc = dl.returncode
        prog(f"unduh selesai rc={rc}")
        vids = [f for f in os.listdir(DL) if f.lower().endswith((".mkv", ".mp4")) and not f.startswith("e")]
        if not vids:
            r = f"download gagal (nzb_fetch rc={rc}) / episode tak ada di batch"
            api_post("/tsuki/fail", {"job_id": jid, "reason": r}); return {"fail": r}
        if mode == "batch" and len(vids) > 1:
            import importlib; nf = importlib.import_module("nzb_fetch")
            match = [f for f in vids if nf.guess_episode(f) == ep]; vids = match or vids
        src = os.path.join(DL, sorted(vids, key=lambda f: -os.path.getsize(os.path.join(DL, f)))[0])
        st = os.statvfs(DL)
        prog(f"src {os.path.getsize(src)/1e9:.2f}GB, sisa disk {st.f_bavail*st.f_frsize/1e9:.1f}GB")
        videos = []
        lad = ladder_for(src_width(src))
        rungs = [(name, w, cq, mx, os.path.join(DL, f"e{ep}.{name}.mp4")) for name, w, cq, mx in lad]
        prog(f"encode ladder {len(rungs)} rung (decode sekali)")
        try:
            enc = encode_ladder(src, rungs, smoke)
        except Exception as e:
            # Jatuh ke jalur per-rung bila filter_complex ditolak sumber tertentu.
            log(f"  ladder gabungan gagal ({str(e)[:120]}), jatuh ke per-rung")
            enc = None
        for name, w, cq, mx, out in rungs:
            if enc is None:
                prog(f"encode {name} ({w}p)")
                m = encode_one(src, out, w, cq, mx, smoke)
            else:
                m = enc
            prog(f"upload {name}")
            key = f"tsuki/{sid}/e{ep}.{name}.mp4"
            sz = os.path.getsize(out)
            s3.upload_file(out, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
            videos.append({"quality": name, "r2_key": key, "size": sz, "enc": m})
            log(f"    {name}: {m} -> {key} ({sz/1e6:.0f}MB)")
            try: os.remove(out)
            except OSError: pass
        prog("ekstrak subtitle"); subs = extract_subs(src, DL); subout = []
        for lang, path in subs.items():
            key = f"tsuki/{sid}/e{ep}.{lang}.vtt"
            s3.upload_file(path, bucket, key, ExtraArgs={"ContentType": "text/vtt"})
            subout.append({"lang": lang if len(lang) == 2 else "en", "r2_key": key})
        api_post("/tsuki/done", {"job_id": jid, "series_id": sid, "episode_number": ep, "videos": videos, "subs": subout})
        log(f"  DONE ep{ep}: {len(videos)} rung, {len(subout)} sub")
        # Ukuran dan encoder ikut dikembalikan: tanpa ini satu-satunya cara memeriksa hasil
        # encode adalah membuka log worker di dashboard, yang tak bisa diambil lewat API.
        return {"done": {"series_id": sid, "ep": ep, "subs": len(subout),
                         "src_mb": round(os.path.getsize(src) / 1e6),
                         "rungs": [{"q": v["quality"], "mb": round(v["size"] / 1e6), "enc": v["enc"]}
                                   for v in videos]}}
    except Exception as e:
        api_post("/tsuki/fail", {"job_id": jid, "reason": str(e)[:250]}); return {"fail": str(e)[:200]}


def handler(event):
    """1 invocation = 1 episode. input opsional {smoke:90} utk test klip pendek."""
    inp = (event or {}).get("input", {}) or {}
    smoke = int(inp.get("smoke", 0))
    worker = inp.get("worker_id", os.environ.get("RUNPOD_POD_ID", "rp"))

    # Mode diagnostik: memotret kondisi container tanpa mengklaim job, supaya menyelidiki
    # worker yang dibunuh tidak perlu mengorbankan episode dari antrean.
    if inp.get("diag"):
        d = {}
        for path in ("/", DL if os.path.isdir(DL) else "/tmp"):
            try:
                st = os.statvfs(path)
                d[f"disk {path}"] = f"{st.f_bavail*st.f_frsize/1e9:.1f}GB bebas / {st.f_blocks*st.f_frsize/1e9:.1f}GB total"
            except OSError as e:
                d[f"disk {path}"] = str(e)
        try:
            mi = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines() if ":" in l)
            d["mem"] = f"total {mi['MemTotal'].strip()}, avail {mi['MemAvailable'].strip()}"
        except Exception as e:
            d["mem"] = str(e)
        for name, cmd in (("nvenc", f"{FFMPEG} -hide_banner -encoders"), ("gpu", "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")):
            try:
                o = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=60).stdout
                d[name] = [l.strip() for l in o.splitlines() if "nvenc" in l] if name == "nvenc" else o.strip()
            except Exception as e:
                d[name] = str(e)
            # Jangkauan jaringan keluar. AniList MEMBLOKIR Cloudflare Workers --
            # diuji langsung dari dalam Worker: HTTP 403 dalam 3-16 ms dengan pesan
            # "You have been manually blocked". Pod berjalan di luar Cloudflare,
            # jadi pertanyaannya apakah ia bisa. Ini menjawabnya tanpa menebak.
            for nama, url, body in (
                ("anilist", "https://graphql.anilist.co",
                 json.dumps({"query": "query{Media(id:194219,type:ANIME){id idMal}}"}).encode()),
                ("tsukihime", f"{TSUKI}/torrents?limit=1", None),
            ):
                t0 = time.time()
                try:
                    req = urllib.request.Request(url, data=body, headers={
                        "Content-Type": "application/json", "User-Agent": "aniplay-pod"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        isi = r.read(200).decode("utf-8", "replace")
                    d[f"net {nama}"] = f"HTTP {r.status} ({(time.time()-t0)*1000:.0f}ms) {isi[:110]}"
                except urllib.error.HTTPError as e:
                    d[f"net {nama}"] = f"HTTP {e.code} ({(time.time()-t0)*1000:.0f}ms) {e.read(160).decode('utf-8','replace')}"
                except Exception as e:
                    d[f"net {nama}"] = f"GAGAL ({(time.time()-t0)*1000:.0f}ms) {type(e).__name__}: {str(e)[:90]}"

        try:
            d["cpu"] = f"host {os.cpu_count()}, kuota cgroup {cpu_quota()}"
            d["df"] = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        except Exception as e:
            d["df"] = str(e)
        d["env"] = {k: bool(os.environ.get(k)) for k in
                    ("API", "TOKEN", "USENET_HOST", "USENET_USER", "USENET_PASS",
                     "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
        return {"status": "diag", **d}

    # PIPELINE bisa ditimpa per-job supaya dua mode bisa dibandingkan berdampingan
    # tanpa mengubah env endpoint dan menunggu worker berganti.
    if inp.get("pipeline"):
        globals()["PIPELINE"] = str(inp["pipeline"]).lower()

    _EV["e"] = event
    s3, bucket = s3_client()
    r = api_post("/tsuki/claim", {"worker_id": worker}); job = r.get("job")
    if not job: return {"status": "empty"}
    res = process_job(job, s3, bucket, smoke)
    return {"status": "ok", **res}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
