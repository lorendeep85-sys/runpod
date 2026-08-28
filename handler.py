#!/usr/bin/env python3
"""RunPod Serverless handler — TsukiHime encode worker (H.264 8-bit, universal).

Tiap invocation: klaim 1 job dari antrean Worker -> resolve tsukihime (episode/batch,
SubsPlease priority) -> download Usenet -> encode H.264 NVENC ladder -> ekstrak sub ->
upload R2 -> lapor /tsuki/done. Return status. RunPod autoscale = paralel.

Env (di endpoint RunPod): API, TOKEN, USENET_USER, USENET_PASS, USENET_HOST, USENET_PORT, USENET_CONN.
"""
import gzip, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release_picker
import runpod

TSUKI = "https://api.tsukihime.org/v1"; STORAGE = "https://storage.tsukihime.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/120"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg"); FFPROBE = os.environ.get("FFPROBE", "ffprobe")
API = os.environ["API"]; TOKEN = os.environ["TOKEN"]
CONNS = os.environ.get("USENET_CONN", "50")
X264_PRESET = os.environ.get("X264_PRESET", "slow")
TARGET_LANGS = ["id", "ar", "es", "pt", "fr", "de"]
TEXT_SUB = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
# H.264 8-bit High — universal Android compat (bukan HEVC 10-bit yg banyak HP tak bisa).
# Angka ketiga = CRF x264 (bukan CQ NVENC; skalanya tidak setara).
# CRF disetel meniru AnimePahe: berkas mereka terbukti libx264 High 8-bit level 4.0
# pada ~750-950 kb/s (140-175 MB per episode 24 menit). Bitrate yang berbeda-beda antar
# judul menandakan mereka memakai CRF, bukan bitrate tetap.
LADDER = [("1080p", 1920, 26), ("720p", 1280, 26), ("480p", 854, 27)]
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
def is_subsplease(t): return "subsplease" in (((t.get("group") or {}).get("name", "") + " " + (t.get("name") or "")).lower())
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

def resolve(anilist_id, ep):
    if not anilist_id: return None, None, "no anilist_id"
    try: a = hj(f"{TSUKI}/animes/anilist/{anilist_id}")
    except Exception as e: return None, None, f"anilist gagal: {e}"
    aid = (a.get("anime") or a).get("id") or a.get("id")
    if not aid: return None, None, "not on tsuki"
    tor = []
    for off in (0, 100, 200, 300):
        d = hj(f"{TSUKI}/animes/{aid}?limit=100&offset={off}"); res = d.get("results", []); tor += res
        if len(res) < 100: break
    cands = [t for t in tor if t.get("episode_no") == ep and t.get("has_nzb")]
    if cands:
        sp = [t for t in cands if is_subsplease(t)]
        p = release_picker.pick(sp or cands, top=1, allow_relax=False)
        if p: return p[0], None, "episode"
    batch = []
    for t in tor:
        if t.get("has_nzb") and t.get("episode_no") is None and (t.get("filecount") or 0) > 1:
            rng = _range_covers(t.get("name", ""), ep)
            if rng: batch.append((t, rng))
    if batch:
        batch.sort(key=lambda x: (x[1][1] - x[1][0])); t, rng = batch[0]; return t, rng, "batch"
    return None, None, "no release (episode/batch)"

def encode_one(src, dest, w, crf, seconds=0):
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
                "-crf", str(crf)] + aud
    r = subprocess.run(x, capture_output=True, text=True, timeout=14400)
    if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 200_000:
        return "x264"

    # NVENC memakai skala kualitas sendiri; +7 mendekatkan ukurannya ke CRF x264 di atas.
    nv = base + ["-c:v", "h264_nvenc", "-preset", "p6", "-profile:v", "high", "-pix_fmt", "yuv420p",
                 "-rc", "vbr", "-cq", str(crf + 7), "-b:v", "0", "-bf", "3", "-b_ref_mode", "middle",
                 "-rc-lookahead", "32", "-multipass", "fullres",
                 "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "5"] + aud
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

def prog(msg):
    """Tandai tahap berjalan. Kalau worker dibunuh, tahap terakhir tetap terbaca di /status,
    sehingga titik gagal tidak perlu ditebak dari log yang tak bisa diambil lewat API."""
    log(msg)
    try:
        if _EV["e"] is not None:
            runpod.serverless.progress_update(_EV["e"], msg)
    except Exception:
        pass

def process_job(job, s3, bucket, smoke=0):
    sid = job["series_id"]; ep = int(job["episode_number"]); jid = job["id"]
    log(f"job {jid}: {job.get('title')} ep{ep} (series {sid})")
    rel, rng, mode = resolve(job.get("anilist_id"), ep)
    if not rel:
        api_post("/tsuki/fail", {"job_id": jid, "reason": mode}); return {"skip": mode}
    t = rel["torrent"] if isinstance(rel, dict) and "torrent" in rel else rel
    log(f"  [{mode}] {t['name'][:66]}")
    os.makedirs(DL, exist_ok=True)
    for f in os.listdir(DL):
        try: os.remove(os.path.join(DL, f))
        except OSError: pass
    try:
        nzb = fetch_nzb(nzb_url(t["id"], t["name"]), os.path.join(DL, "job.nzb"))
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
        rc = subprocess.run(cmd, timeout=10800, env=senv).returncode
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
        for name, w, cq in ladder_for(src_width(src)):
            out = os.path.join(DL, f"e{ep}.{name}.mp4")
            prog(f"encode {name} ({w}p)")
            m = encode_one(src, out, w, cq, smoke)
            prog(f"upload {name}")
            key = f"tsuki/{sid}/e{ep}.{name}.mp4"
            sz = os.path.getsize(out)
            s3.upload_file(out, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
            videos.append({"quality": name, "r2_key": key, "size": sz, "enc": m})
            log(f"    {name}: {m} -> {key} ({sz/1e6:.0f}MB)")
            # Disk container serverless sempit. Tanpa penghapusan ini ketiga rung menumpuk
            # bersama sumber 1,4 GB sampai container dibunuh kehabisan ruang -- kegagalannya
            # muncul sebagai "job timed out", bukan sebagai error yang bisa ditangkap Python.
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
        try:
            d["df"] = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        except Exception as e:
            d["df"] = str(e)
        d["env"] = {k: bool(os.environ.get(k)) for k in
                    ("API", "TOKEN", "USENET_HOST", "USENET_USER", "USENET_PASS",
                     "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
        return {"status": "diag", **d}

    _EV["e"] = event
    s3, bucket = s3_client()
    r = api_post("/tsuki/claim", {"worker_id": worker}); job = r.get("job")
    if not job: return {"status": "empty"}
    res = process_job(job, s3, bucket, smoke)
    return {"status": "ok", **res}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
