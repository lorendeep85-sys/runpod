"""
Pemilih rilisan tsukihime.

Untuk satu judul, tsukihime bisa punya ratusan torrent dengan ukuran berbeda 7x
untuk konten yang sama persis. Modul ini memilih yang paling efisien:
sekecil mungkin, tapi kualitasnya masih layak dan subtitle-nya lengkap.

Dipakai dua tempat: (1) waktu ambil judul baru, (2) waktu deteksi upgrade.
"""
import re

# --- pola nama rilisan ---
RE_2160   = re.compile(r'\b(2160p|4k|uhd)\b', re.I)
RE_1080   = re.compile(r'\b1080p?\b', re.I)
RE_720    = re.compile(r'\b720p?\b', re.I)
RE_480    = re.compile(r'\b(480p|dvd|dvdrip|xvid)\b', re.I)
RE_REMUX  = re.compile(r'\b(remux|bdmv|\.iso\b|untouched)\b', re.I)
RE_HEVC   = re.compile(r'\b(hevc|x265|h\.?265)\b', re.I)
RE_AV1    = re.compile(r'\bav1\b', re.I)
RE_10BIT  = re.compile(r'\b10.?bits?\b', re.I)
RE_BATCH  = re.compile(r'\b(batch|complete|seasons?\s*\d|s\d{2}\b|\d+\s*-\s*\d+)\b', re.I)

# grup WEB-DL yang rilisannya konsisten & rapi (bukan penilaian kualitas mutlak,
# sekadar sinyal bahwa penamaan/struktur filenya bisa diandalkan)
GOOD_GROUPS = {"erai-raws","subsplease","toonshub","varyg","asw","judas","dkb",
               "onalrie","yameii","shridhuu","prejudice-studio","bonobosubs","huangsubs","fsp"}

MIN_GB_EP, MAX_GB_EP = 0.15, 3.0     # ukuran per episode yang masuk akal
IDEAL_GB_EP = 1.0


def size_per_ep(t):
    """GB per episode. filecount 0 = belum diproses, anggap 1 file."""
    n = max(t.get("filecount") or 1, 1)
    return (t.get("totalsize") or 0) / 1e9 / n


def score(t, want_episodes=None, relax=False):
    """Skor rilisan. Makin tinggi makin bagus. None = tolak.

    relax=True dipakai sebagai jalur cadangan: menerima rilisan tanpa NZB
    (harus diunduh lewat torrent) dan ukuran per episode sampai 5 GB.
    Dari sampel, ~18% judul — semuanya ekor panjang dengan 1-3 torrent saja —
    hanya bisa lewat jalur ini.
    """
    name = t.get("name") or ""
    low  = name.lower()

    # --- syarat mutlak ---
    if t.get("state") != "completed":      return None   # link/file belum siap
    if t.get("is_adult"):                  return None
    if RE_REMUX.search(low):               return None   # 40-90GB, mustahil
    if RE_2160.search(low):                return None   # decode 4x, output tetap 720p
    if not t.get("has_nzb") and not relax: return None   # jalur normal = Usenet

    gb = size_per_ep(t)
    hi = 5.0 if relax else MAX_GB_EP
    if not (MIN_GB_EP <= gb <= hi):        return None   # kekecilan/kebesaran

    s = 100.0

    # --- ukuran: makin dekat ideal makin bagus (ini faktor terbesar) ---
    s -= abs(gb - IDEAL_GB_EP) * 25

    # --- resolusi ---
    if   RE_1080.search(low): s += 30
    elif RE_720.search(low):  s += 18
    elif RE_480.search(low):  s -= 25

    # --- codec: makin efisien makin kecil unduhannya ---
    if RE_AV1.search(low):    s += 20
    elif RE_HEVC.search(low): s += 15
    if RE_10BIT.search(low):  s += 6

    # --- subtitle: sub 'id' resmi = hemat biaya translate AI ---
    subs = set(x.lower() for x in (t.get("sublangs") or []))
    if "id" in subs: s += 40
    if "en" in subs: s += 12
    if not subs:
        # Rilisan donghua (mis. BonoboSubs) menaruh subtitle sebagai track ASS
        # dengan lang="und", sehingga sublangs terlihat kosong padahal ada.
        # Menghukum grup fansub di sini akan membuang justru satu-satunya
        # sumber yang tersedia untuk sebagian besar donghua.
        if not (t.get("group") or {}).get("is_fansub"):
            s -= 15

    # --- audio ---
    auds = set(x.lower() for x in (t.get("audiolangs") or []))
    if any(a.startswith("ja") for a in auds): s += 8
    if any(not a.startswith("ja") for a in auds): s += 10   # ada dub

    # --- kelengkapan batch ---
    fc = t.get("filecount") or 0
    if want_episodes and fc:
        ratio = fc / want_episodes
        if   0.95 <= ratio <= 1.10: s += 35     # satu season utuh
        elif ratio > 1.10:          s += 10     # multi-season, boleh
        elif ratio >= 0.5:          s += 5
    if RE_BATCH.search(low) and fc > 1: s += 10

    # --- grup ---
    g = ((t.get("group") or {}).get("name") or "").lower()
    if g in GOOD_GROUPS: s += 15

    return s


def pick(torrents, want_episodes=None, top=1, allow_relax=True):
    """Rilisan terbaik. Coba jalur Usenet dulu; kalau kosong, jatuh ke torrent.

    Tiap hasil punya 'route': 'usenet' (pakai NZB) atau 'torrent' (cadangan).
    """
    for relax in ((False, True) if allow_relax else (False,)):
        out = []
        for t in torrents:
            sc = score(t, want_episodes, relax=relax)
            if sc is None:
                continue
            out.append({
                "torrent": t,
                "score": round(sc, 1),
                "gb_per_ep": round(size_per_ep(t), 2),
                "route": "usenet" if t.get("has_nzb") else "torrent",
            })
        if out:
            out.sort(key=lambda x: -x["score"])
            return out[:top]
    return []
