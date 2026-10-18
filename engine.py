# ============ POLYMARKET MESIN v1 : Pemburu + Paper Trading + Dashboard ============
# Tidak butuh akun Polymarket / wallet / uang. Hanya membaca data publik.
import requests, json, os, math, html, hashlib, datetime as dt

# ---------- 1. Tempat penyimpanan (Google Drive kalau di Colab) ----------
BASE = os.environ.get('PM_BASE')          # diisi oleh GitHub Actions
if BASE:
    IN_COLAB = False
else:
    try:
        from google.colab import drive
        drive.mount('/content/drive')
        BASE = '/content/drive/MyDrive/polymarket_bot'
        IN_COLAB = True
    except Exception:
        BASE = './polymarket_bot'
        IN_COLAB = False
os.makedirs(BASE, exist_ok=True)
POS_FILE = f'{BASE}/positions.json'

# ---------- 2. PENGATURAN (boleh kamu ubah angkanya) ----------
CFG = dict(
    MIN_LIQUIDITY = 5000,   # minimal likuiditas ($)
    MIN_VOLUME24H = 1000,   # minimal volume 24 jam ($)
    MIN_DAYS = -7,          # boleh sedikit lewat tanggal tutup (market pemilu sering begitu)
    MAX_DAYS = 60,          # market tutup maksimal X hari lagi
    PRICE_MIN = 0.10,       # abaikan harga terlalu murah/ekstrem
    PRICE_MAX = 0.95,
    TOP_N = 15,             # berapa kandidat ditampilkan
    PAPER_STAKE = 10,       # taruhan simulasi per posisi ($ virtual)
    MAX_OPEN = 10,          # maksimal posisi simulasi terbuka
    SLIPPAGE = 0.01,        # biaya beli simulasi (1 sen) biar realistis
    FADE_MIN = 0.70, FADE_MAX = 0.93,   # strategi 'fade-favorit': lawan favorit di rentang harga ini
    FADE_MIN_DAYS = 3, FADE_MAX_DAYS = 14, FADE_MAX_OPEN = 40, FADE_MAX_PER_CLUSTER = 3, FADE_MIN_LIQ = 5000, FADE_SLIPPAGE = 0.02,
    YP_MIN = 0.30, YP_MAX = 0.70, YP_MIN_DAYS = 3, YP_MAX_DAYS = 30, YP_MIN_LIQ = 5000, YP_MAX_OPEN = 0, YP_SLIPPAGE = 0.02,
    PAGES = 10,             # 10 halaman x 100 = 1000 market (lebih banyak bahan uji)              # 4 halaman x 100 market
)
API = 'https://gamma-api.polymarket.com'
# WATCHLIST: market di luar kolam yang ingin kita pantau (ambil dari 'DI LUAR KOLAM' di Whale Radar).
# Isi dengan potongan terakhir link event polymarket.com/event/<INI>
EXTRA_EVENTS = ['another-fed-rate-hike-in-2026']

# ---------- VERSI STRATEGI ----------
# Setiap aturan masuk diberi ID otomatis dari parameternya. Ubah parameter -> ID baru -> statistik versi baru
# dihitung terpisah dan tidak tercampur dengan versi lama (hasil lama tetap tersimpan sebagai arsip).
def rule_id(prefix, keys):
    blob = json.dumps({k: CFG[k] for k in keys}, sort_keys=True)
    return prefix + '-' + hashlib.sha1(blob.encode()).hexdigest()[:6]
FADE_ID = rule_id('F', ['FADE_MIN', 'FADE_MAX', 'FADE_MIN_DAYS', 'FADE_MAX_DAYS', 'FADE_MIN_LIQ', 'FADE_SLIPPAGE'])
YP_ID = rule_id('Y', ['YP_MIN', 'YP_MAX', 'YP_MIN_DAYS', 'YP_MAX_DAYS', 'YP_MIN_LIQ', 'YP_SLIPPAGE'])
AI_VER, BASE_VER = 'A-1', 'B-1'
FADE_LEGACY_CUTOFF = '2026-10-10'     # posisi fade sebelum tanggal ini memakai aturan lama (favorit 79-90%, biaya 1 sen)
RULE_TEXT = {
    FADE_ID: f"lawan favorit {CFG['FADE_MIN']*100:.0f}-{CFG['FADE_MAX']*100:.0f}%, sisa {CFG['FADE_MIN_DAYS']}-{CFG['FADE_MAX_DAYS']} hari, likuiditas >= ${CFG['FADE_MIN_LIQ']:,}, biaya {CFG['FADE_SLIPPAGE']*100:.0f}c",
    YP_ID: f"beli No saat Yes {CFG['YP_MIN']*100:.0f}-{CFG['YP_MAX']*100:.0f}%, sisa {CFG['YP_MIN_DAYS']}-{CFG['YP_MAX_DAYS']} hari, likuiditas >= ${CFG['YP_MIN_LIQ']:,}, biaya {CFG['YP_SLIPPAGE']*100:.0f}c",
    'F-lama': 'lawan favorit 79-90% (aturan lama, sebelum 10 Okt), biaya 1c',
    AI_VER: 'pilihan analisis Claude (keyakinan sedang ke atas)',
    BASE_VER: 'patokan: ikut favorit',
}
SETTLED_NOW = []                       # posisi yang selesai pada run ini (dipakai notifikasi)

def migrate_versions(pos):
    for t in pos:
        if t.get('ver'): continue
        s_ = t.get('strategy', 'baseline-favorit')
        if s_ == 'fade-favorit':
            t['ver'] = 'F-lama' if str(t.get('opened', ''))[:10] < FADE_LEGACY_CUTOFF else FADE_ID
        elif s_ == 'claude-A': t['ver'] = AI_VER
        elif s_ == 'yes-premium': t['ver'] = YP_ID
        else: t['ver'] = BASE_VER
    return pos
NOW = dt.datetime.now(dt.timezone.utc)

# ---------- 3. PEMBURU: ambil & saring market ----------
def jl(x):
    if isinstance(x, str):
        try: return json.loads(x)
        except Exception: return []
    return x or []

def num(x, d=0.0):
    try: return float(x)
    except Exception: return d

def fetch_markets():
    out = []
    for p in range(CFG['PAGES']):
        r = requests.get(f'{API}/markets', params=dict(
            active='true', closed='false', limit=100, offset=p*100,
            order='volumeNum', ascending='false'), timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch: break
        out += batch
    return out

def parse(m):
    outs, pr = jl(m.get('outcomes')), [num(x) for x in jl(m.get('outcomePrices'))]
    if len(outs) != 2 or len(pr) != 2: return None
    end = None
    if m.get('endDate'):
        try: end = dt.datetime.fromisoformat(m['endDate'].replace('Z', '+00:00'))
        except Exception: pass
    if end is None: return None
    if end.tzinfo is None: end = end.replace(tzinfo=dt.timezone.utc)
    ev = (m.get('events') or [{}])[0].get('slug') or m.get('slug', '')
    return dict(id=str(m.get('id')), ev=ev, toks=jl(m.get('clobTokenIds')), q=m.get('question', '?'), o=outs, p=pr,
                liq=num(m.get('liquidityNum', m.get('liquidity'))),
                vol24=num(m.get('volume24hr')), vol=num(m.get('volumeNum', m.get('volume'))),
                spread=num(m.get('spread'), 0.05), end=end,
                days=(end - NOW).total_seconds() / 86400,
                url=f'https://polymarket.com/event/{ev}')

def fetch_extra():
    out = []
    for slug in EXTRA_EVENTS:
        try:
            r = requests.get(f'{API}/events', params=dict(slug=slug), timeout=30)
            r.raise_for_status()
            evs = r.json()
            evs = evs if isinstance(evs, list) else [evs]
            for ev in evs:
                for m in ev.get('markets', []):
                    m = dict(m); m.setdefault('events', [{'slug': slug}])
                    x = parse(m)
                    if x: x['extra'] = True; out.append(x)
        except Exception as e:
            print('watchlist gagal', slug, e)
    return out

def score(m):
    s_liq = min(math.log10(max(m['liq'], 1)) / 5, 1)            # 100k = penuh
    s_vol = min(math.log10(max(m['vol24'], 1)) / 5, 1)
    s_spr = max(0, 1 - m['spread'] / 0.05)                      # spread kecil = bagus
    s_time = 1 if 3 <= m['days'] <= 30 else 0.5
    return round(100 * (0.35*s_liq + 0.30*s_vol + 0.25*s_spr + 0.10*s_time))

PARSED_ALL = []
def scan():
    global PARSED_ALL
    raw = fetch_markets()
    parsed = [x for x in (parse(m) for m in raw) if x]
    PARSED_ALL = parsed
    keep = []
    for m in parsed:
        fav = max(m['p'])
        if (m['liq'] >= CFG['MIN_LIQUIDITY'] and m['vol24'] >= CFG['MIN_VOLUME24H']
                and CFG['MIN_DAYS'] <= m['days'] <= CFG['MAX_DAYS']
                and CFG['PRICE_MIN'] <= fav <= CFG['PRICE_MAX']):
            m['score'] = score(m)
            m['anomaly'] = abs(sum(m['p']) - 1) > 0.02
            keep.append(m)
    keep.sort(key=lambda x: -x['score'])
    have = {m['id'] for m in keep}
    for m in fetch_extra():
        if m['id'] in have: continue
        m['score'] = score(m); m['anomaly'] = abs(sum(m['p']) - 1) > 0.02
        keep.append(m)
    return len(raw), len(parsed), keep

# ---------- 4. PAPER TRADING (uang virtual) ----------
def load_pos():
    if os.path.exists(POS_FILE):
        return json.load(open(POS_FILE))
    return []

def save_pos(pos):
    json.dump(pos, open(POS_FILE, 'w'), indent=1)

def refresh_positions(pos):
    for t in pos:
        if t['status'] != 'open': continue
        try:
            r = requests.get(f"{API}/markets/{t['id']}", timeout=30)
            r.raise_for_status()
            m = r.json()
            pr = [num(x) for x in jl(m.get('outcomePrices'))]
            if len(pr) != 2: continue
            cur = pr[t['side_idx']]
            t['cur'] = cur
            if m.get('closed') and (max(pr) >= 0.99):
                t['status'] = 'won' if cur >= 0.99 else 'lost'
                t['closed_at'] = NOW.isoformat()
                SETTLED_NOW.append(t)
        except Exception as e:
            print('gagal update', t['q'][:40], e)
    return pos

def open_baseline(pos, cands):
    """BASELINE pembanding: 'ikut favorit' pada kandidat teratas. Bukan strategi untung,
    hanya patokan. Nanti AI harus mengalahkan patokan ini."""
    open_ids = {t['id'] for t in pos if t['status'] == 'open' and t.get('strategy') == 'baseline-favorit'}
    n_open = len(open_ids)
    known = {t['id'] for t in pos if t.get('strategy') == 'baseline-favorit'}
    used_ev = {t.get('ev') for t in pos if t['status'] == 'open' and t.get('ev') and t.get('strategy') == 'baseline-favorit'}
    for m in cands:
        if n_open >= CFG['MAX_OPEN']: break
        if m.get('extra') or m['id'] in known or m['ev'] in used_ev: continue
        used_ev.add(m['ev'])
        i = 0 if m['p'][0] >= m['p'][1] else 1
        entry = min(m['p'][i] + CFG['SLIPPAGE'], 0.99)
        pos.append(dict(id=m['id'], ev=m['ev'], q=m['q'], side=m['o'][i], side_idx=i, entry=entry,
                        cur=m['p'][i], stake=CFG['PAPER_STAKE'],
                        shares=CFG['PAPER_STAKE'] / entry, opened=NOW.isoformat(),
                        status='open', url=m['url'], strategy='baseline-favorit', ver=BASE_VER))
        n_open += 1
    return pos


# ---------- PILIHAN CLAUDE (Jalur A) : diisi dari hasil analisis di chat ----------
# match = potongan teks pertanyaan, side = sisi yang dibeli, p_yes = estimasi Claude utk YES
AI_PICKS = [
    dict(match="US announces end of Iranian blockade", side="No", p_yes=0.14, conf="sedang",
         why="Perundingan macet; Trump tolak proposal; delegasi Iran diusir; tempo 'setelah midterm'."),
    dict(match="no change in Fed interest rates after the October 2026", side="No", p_yes=0.76, conf="rendah",
         why="Futures CME menyiratkan hold 62-75% vs pasar 84%; tipis, hanya untuk dicatat."),
    dict(match="Billionaire one-time wealth tax passes", side="Yes", p_yes=0.37, conf="rendah",
         why="Polling Yes unggul (48-38 NBC, 52-46 PPIC) tapi di bawah/dekat 50%; dukungan inisiatif biasanya turun; lawan didanai besar. Keyakinan rendah."),
    dict(match="Another Fed rate hike in 2026", side="No", p_yes=0.70, conf="rendah",
         why="Futures CME ~67% dan rata2 Kalshi/Polymarket ~72% utk hike lagi s/d akhir 2026 vs harga ini ~78%. Tipis, keyakinan rendah."),
]

CLOB = 'https://clob.polymarket.com'
def book_fill(token, stake):
    """Harga isi NYATA utk membeli senilai `stake` dolar menurut order book publik (tanpa akun)."""
    try:
        r = requests.get(f'{CLOB}/book', params=dict(token_id=token), timeout=20)
        r.raise_for_status(); b = r.json()
        asks = sorted((num(a.get('price')), num(a.get('size'))) for a in b.get('asks', []))
        bids = sorted(((num(x.get('price')), num(x.get('size'))) for x in b.get('bids', [])), reverse=True)
    except Exception:
        return None
    if not asks: return None
    need, shares, cost = stake, 0.0, 0.0
    for p, sz in asks:
        if p <= 0: continue
        usd = min(need, p * sz)
        shares += usd / p; cost += usd; need -= usd
        if need <= 1e-9: break
    return dict(best_ask=asks[0][0], best_bid=(bids[0][0] if bids else None),
                vwap=(cost / shares if shares else None), full=(need <= 1e-6))

def real_info(m, i):
    """(lewati?, kolom tambahan). Lewati kalau order book terlalu tipis utk modal simulasi kita."""
    toks = m.get('toks') or []
    if len(toks) != 2: return False, {}
    f = book_fill(toks[i], CFG['PAPER_STAKE'])
    if f is None: return False, {}
    if not f['full'] or not f['vwap']: return True, {}
    return False, dict(real_entry=f['vwap'], best_ask=f['best_ask'], best_bid=f['best_bid'])

def open_ai_picks(pos, cands):
    have = {(t['id'], t.get('strategy')) for t in pos}
    for pk in AI_PICKS:
        m = next((c for c in cands if pk['match'].lower() in c['q'].lower()), None)
        if pk.get('conf') == 'rendah' or not m or (m['id'], 'claude-A') in have: continue
        i = 0 if m['o'][0].lower() == pk['side'].lower() else 1
        entry = min(m['p'][i] + CFG['SLIPPAGE'], 0.99)
        _, rf = real_info(m, i)
        pos.append(dict(id=m['id'], ev=m['ev'], q=m['q'], side=m['o'][i], side_idx=i, entry=entry,
                        cur=m['p'][i], stake=CFG['PAPER_STAKE'], shares=CFG['PAPER_STAKE']/entry,
                        opened=NOW.isoformat(), status='open', url=m['url'], strategy='claude-A', ver=AI_VER,
                        p_yes=pk['p_yes'], conf=pk['conf'], why=pk['why'], **rf))
    return pos


# Kelompok tema: taruhan dalam satu tema saling berkorelasi (kalah bersama), jadi dibatasi.
CLUSTERS = {
    'timteng': ['iran', 'israel', 'hormuz', 'nato', 'russia', 'ukraine', 'gaza', 'hamas', 'hezbollah', 'syria', 'houthi', 'ceasefire', 'blockade', 'netanyahu', 'bennett', 'eizenkot'],
    'pemilu-as': ['house', 'senate', 'midterm', 'balance of power', 'democrat', 'republican', 'governor', 'gop', 'trump'],
    'kripto': ['bitcoin', 'btc', 'ethereum', 'solana', 'xrp', 'crypto', 'dip to', 'reach $'],
    'fed': ['fed ', 'interest rate', 'rate hike', 'rate cut', 'fomc'],
    'brasil': ['brazil', 'lula', 'bolsonaro'],
    'ai': ['ai model', 'openai', 'google', 'gemini', 'grok', 'xai'],
}
EXCLUDE_WORDS = ['anthropic', 'claude']   # benturan kepentingan: tidak diperdagangkan oleh mesin otomatis

def cluster_of(q, ev=''):
    ql = (q or '').lower() + ' '
    for name, words in CLUSTERS.items():
        if any(w in ql for w in words): return name
    return 'lain:' + str(ev)

def open_fade(pos, cands):
    """STRATEGI UJI #3: lawan favorit 80-90% (hipotesis dari backtest: favorit menang lebih jarang dari harganya).
    Murni uji ke depan dengan uang virtual. Satu posisi per event."""
    mine = [t for t in pos if t.get('strategy') == 'fade-favorit']
    n_open = sum(1 for t in mine if t['status'] == 'open')
    known = {t['id'] for t in mine}
    used_ev = {t.get('ev') for t in mine if t['status'] == 'open'}
    used_ev |= {t.get('ev') for t in pos if t['status'] == 'open' and t.get('strategy') == 'claude-A'}  # hindari taruhan kembar dgn Claude-A
    cnt = {}
    for t in mine:
        if t['status'] == 'open':
            c0 = cluster_of(t['q'], t.get('ev')); cnt[c0] = cnt.get(c0, 0) + 1
    for m in sorted(PARSED_ALL or cands, key=lambda c: c['days']):
        if n_open >= CFG['FADE_MAX_OPEN']: break
        if m.get('extra') or m['id'] in known or m['ev'] in used_ev: continue
        if any(w in m['q'].lower() for w in EXCLUDE_WORDS): continue
        cl_ = cluster_of(m['q'], m['ev'])
        if cnt.get(cl_, 0) >= CFG['FADE_MAX_PER_CLUSTER']: continue
        if [str(o).lower() for o in m['o']] != ['yes', 'no']: continue   # backtest hanya mencakup Yes/No
        fav = max(m['p'])
        if not (CFG['FADE_MIN'] <= fav <= CFG['FADE_MAX']): continue
        if not (CFG['FADE_MIN_DAYS'] <= m['days'] <= CFG['FADE_MAX_DAYS']): continue
        if m['liq'] < CFG['FADE_MIN_LIQ']: continue
        i = 1 if m['p'][0] >= m['p'][1] else 0          # sisi UNDERDOG
        entry = min(m['p'][i] + CFG['FADE_SLIPPAGE'], 0.99)
        skip, rf = real_info(m, i)
        if skip: continue                                  # order book terlalu tipis utk modal ini
        pos.append(dict(id=m['id'], ev=m['ev'], q=m['q'], side=m['o'][i], side_idx=i, entry=entry,
                        cur=m['p'][i], stake=CFG['PAPER_STAKE'], shares=CFG['PAPER_STAKE'] / entry,
                        opened=NOW.isoformat(), status='open', url=m['url'], strategy='fade-favorit', ver=FADE_ID, cluster=cl_, **rf,
                        why='Lawan favorit %.0f%%: backtest menunjukkan favorit 70-95%% menang lebih jarang dari harganya (belum terbukti ke depan).' % (fav*100)))
        used_ev.add(m['ev']); n_open += 1; cnt[cl_] = cnt.get(cl_, 0) + 1
    return pos

def open_yes_premium(pos, cands):
    """STRATEGI BAYANGAN (tanpa peran keputusan). Hipotesis dari oracle3 (klaim pengembangnya, belum kita verifikasi):
    kontrak Yes cenderung terlalu mahal -> beli No pada market Yes 30-70%. Uji ke depan murni, uang virtual."""
    mine = [t for t in pos if t.get('strategy') == 'yes-premium']
    n_open = sum(1 for t in mine if t['status'] == 'open')
    known = {t['id'] for t in mine}
    used_ev = {t.get('ev') for t in mine if t['status'] == 'open'}
    cnt = {}
    for t in mine:
        if t['status'] == 'open':
            c0 = cluster_of(t['q'], t.get('ev')); cnt[c0] = cnt.get(c0, 0) + 1
    for m in sorted(cands, key=lambda c: c['days']):
        if n_open >= CFG['YP_MAX_OPEN']: break
        if m.get('extra') or m['id'] in known or m['ev'] in used_ev: continue
        if any(w in m['q'].lower() for w in EXCLUDE_WORDS): continue
        if [str(o).lower() for o in m['o']] != ['yes', 'no']: continue
        if not (CFG['YP_MIN'] <= m['p'][0] <= CFG['YP_MAX']): continue
        if not (CFG['YP_MIN_DAYS'] <= m['days'] <= CFG['YP_MAX_DAYS']): continue
        if m['liq'] < CFG['YP_MIN_LIQ']: continue
        cl_ = cluster_of(m['q'], m['ev'])
        if cnt.get(cl_, 0) >= CFG['FADE_MAX_PER_CLUSTER']: continue
        entry = min(m['p'][1] + CFG['YP_SLIPPAGE'], 0.99)
        skip, rf = real_info(m, 1)
        if skip: continue
        pos.append(dict(id=m['id'], ev=m['ev'], q=m['q'], side=m['o'][1], side_idx=1, entry=entry,
                        cur=m['p'][1], stake=CFG['PAPER_STAKE'], shares=CFG['PAPER_STAKE'] / entry,
                        opened=NOW.isoformat(), status='open', url=m['url'], strategy='yes-premium',
                        ver=YP_ID, shadow=True, cluster=cl_, **rf,
                        why='BAYANGAN: hipotesis "Yes terlalu mahal" (klaim oracle3, belum terbukti). Beli No saat Yes %.0f%%.' % (m['p'][0]*100)))
        used_ev.add(m['ev']); n_open += 1; cnt[cl_] = cnt.get(cl_, 0) + 1
    return pos

def pnl_real(t):
    if not t.get('real_entry'): return None
    sh = t['stake'] / t['real_entry']
    if t['status'] == 'won': return sh - t['stake']
    if t['status'] == 'lost': return -t['stake']
    return sh * t['cur'] - t['stake']

def boot_roi(items, n=1000):
    import random
    random.seed(1)
    groups = {}
    for c, p, st in items: groups.setdefault(c, []).append((p, st))
    keys = list(groups)
    if len(keys) < 3: return None
    res = []
    for _ in range(n):
        pick = [random.choice(keys) for _ in keys]
        P = sum(p for k in pick for p, st in groups[k]); S = sum(st for k in pick for p, st in groups[k])
        res.append(P / S if S else 0.0)
    res.sort()
    return res[int(0.05 * n)], res[int(0.95 * n)]

def stat_pack(done, slip):
    n = len(done)
    wins = sum(1 for t in done if t['status'] == 'won')
    stake = sum(t['stake'] for t in done)
    tot = sum(pnl(t) for t in done)
    roi = tot / stake
    mkt = sum(t['entry'] - slip for t in done) / n
    top = max(done, key=pnl)
    roi_ex = (tot - pnl(top)) / (stake - top['stake']) if n > 1 else 0.0
    rr = [(pnl_real(t), t['stake']) for t in done if pnl_real(t) is not None]
    roi_real = (sum(p for p, _ in rr) / sum(x for _, x in rr)) if rr else None
    ci = boot_roi([(t.get('cluster') or t.get('ev') or t['id'], pnl(t), t['stake']) for t in done])
    n_cl = len({(t.get('cluster') or t.get('ev') or t['id']) for t in done})
    return dict(n=n, wins=wins, roi=roi, mkt=mkt, gap=wins / n - mkt, roi_ex=roi_ex, roi_real=roi_real, n_rr=len(rr), ci=ci, n_cl=n_cl)

def gate_status(pos):
    done = [t for t in pos if t.get('strategy') == 'fade-favorit' and t.get('ver') == FADE_ID and t['status'] in ('won', 'lost')]
    if not done: return dict(n=0, ok=False, light='merah', roi=0.0, gap=0.0, roi_ex=0.0, n_cl=0, ci5=None)
    g = stat_pack(done, CFG['FADE_SLIPPAGE'])
    g['ci5'] = g['ci'][0] if g['ci'] else None
    g['yellow'] = bool(g['n'] >= 40 and g['roi'] >= 0 and g['gap'] >= 0.03 and g['roi_ex'] > 0)
    g['green'] = bool(g['n'] >= 120 and g['ci5'] is not None and g['ci5'] > 0 and g['gap'] >= 0.03 and g['roi_ex'] > 0)
    g['light'] = 'hijau' if g['green'] else ('kuning' if g['yellow'] else 'merah')
    g['ok'] = g['yellow']
    return g

def scorecard_html(pos):
    def ck(ok): return '<span class="up">&#10003;</span>' if ok else '<span class="dn">&#10007;</span>'
    g = gate_status(pos)
    ci5 = g.get('ci5')
    if g['n'] == 0:
        gate = (f"<div class='card' style='margin-bottom:16px;font-size:13px;line-height:1.7'><strong>Gerbang bertahap &mdash; fade-favorit, aturan {FADE_ID}:</strong> lampu MERAH (belum ada posisi versi ini yang selesai).<br>"
                "KUNING = izin uji eksekusi uang asli SANGAT kecil (maks $100 total): 40 posisi selesai, ROI &ge; 0%, selisih &ge; +3 poin, positif tanpa kemenangan terbesar.<br>"
                "HIJAU = baru boleh mempertimbangkan menaikkan modal bertahap: 120 posisi selesai dan batas bawah ROI (90%, per tema) &gt; 0.<br>"
                f"<small style='color:#8fa0bd'>Aturan: {RULE_TEXT[FADE_ID]}</small></div>")
    else:
        ci_txt = '-' if ci5 is None else f"{ci5*100:+.0f}%"
        gate = (f"<div class='card' style='margin-bottom:16px;font-size:13px;line-height:1.7'><strong>Gerbang bertahap &mdash; fade-favorit, aturan {FADE_ID}</strong> &nbsp; lampu: <strong>{g['light'].upper()}</strong><br>"
                "<u>KUNING &mdash; izin uji eksekusi uang asli SANGAT kecil (maks $100 total), bukan bukti untung</u><br>"
                f"{ck(g['n']>=40)} Posisi selesai: {g['n']} / 40 &nbsp; (tema independen: {g['n_cl']})<br>"
                f"{ck(g['roi']>=0)} ROI setelah biaya: {g['roi']*100:+.0f}% (target &ge; 0%)<br>"
                f"{ck(g['gap']>=0.03)} Underdog menang lebih sering dari harga: {g['gap']*100:+.0f} poin (target &ge; +3)<br>"
                f"{ck(g['roi_ex']>0)} ROI tanpa kemenangan terbesar: {g['roi_ex']*100:+.0f}% (target &gt; 0)<br>"
                "<u>HIJAU &mdash; baru boleh mempertimbangkan menaikkan modal bertahap</u><br>"
                f"{ck(g['n']>=120)} Posisi selesai: {g['n']} / 120<br>"
                f"{ck(ci5 is not None and ci5>0)} Batas bawah ROI (90%, per tema): {ci_txt} (target &gt; 0)<br>"
                f"<small style='color:#8fa0bd'>Aturan: {RULE_TEXT[FADE_ID]}. Versi aturan lain dihitung terpisah (arsip). Dengan edge setengah dari backtest pun, 40 posisi sering belum cukup untuk membedakan dari keberuntungan.</small></div>")
    order = {'fade-favorit': 0, 'yes-premium': 1, 'claude-A': 2, 'baseline-favorit': 3}
    keys = sorted({(t.get('strategy', 'baseline-favorit'), t.get('ver', '?')) for t in pos},
                  key=lambda k: (order.get(k[0], 9), k[1] != FADE_ID, k[1]))
    rows = ''
    for strat, ver in keys:
        grp = [t for t in pos if t.get('strategy', 'baseline-favorit') == strat and t.get('ver', '?') == ver]
        done = [t for t in grp if t['status'] in ('won', 'lost')]
        if strat == 'fade-favorit': role = 'UJI UTAMA' if ver == FADE_ID else 'arsip'
        elif strat == 'yes-premium': role = 'bayangan (dihentikan)'
        elif strat == 'claude-A': role = 'analisis Claude'
        else: role = 'patokan'
        head = f"<td>{strat}</td><td>{ver}</td><td>{role}</td><td>{len(grp)}</td>"
        if not done:
            rows += f"<tr>{head}<td colspan=8>belum ada yang selesai</td></tr>"; continue
        slip = CFG['FADE_SLIPPAGE'] if strat == 'fade-favorit' else (CFG['YP_SLIPPAGE'] if strat == 'yes-premium' else CFG['SLIPPAGE'])
        p = stat_pack(done, slip)
        ci_txt = f"{p['ci'][0]*100:+.0f}% s/d {p['ci'][1]*100:+.0f}%" if p['ci'] else "-"
        rr_txt = '-' if p['roi_real'] is None else f"{p['roi_real']*100:+.0f}% (n={p['n_rr']})"
        rows += (f"<tr>{head}<td>{p['n']}</td><td>{p['wins']}</td>"
                 f"<td class='{'up' if p['gap']>=0 else 'dn'}'>{p['gap']*100:+.0f} poin</td>"
                 f"<td class='{'up' if p['roi']>=0 else 'dn'}'>{p['roi']*100:+.0f}%</td>"
                 f"<td>{rr_txt}</td><td>{p['roi_ex']*100:+.0f}%</td><td>{ci_txt}</td></tr>")
        if strat == 'fade-favorit' and ver == FADE_ID:
            for lab_side, sd in (('&nbsp;&nbsp;&#8627; underdog Yes (favorit No)', 'Yes'), ('&nbsp;&nbsp;&#8627; underdog No (favorit Yes)', 'No')):
                allp = [t for t in grp if t['side'] == sd]
                sub_ = [t for t in done if t['side'] == sd]
                if not sub_:
                    rows += f"<tr><td colspan=3>{lab_side}</td><td>{len(allp)}</td><td colspan=8>belum ada yang selesai</td></tr>"; continue
                ps = stat_pack(sub_, slip)
                ci2 = f"{ps['ci'][0]*100:+.0f}% s/d {ps['ci'][1]*100:+.0f}%" if ps['ci'] else '-'
                rr2 = '-' if ps['roi_real'] is None else f"{ps['roi_real']*100:+.0f}% (n={ps['n_rr']})"
                rows += (f"<tr><td colspan=3>{lab_side}</td><td>{len(allp)}</td><td>{ps['n']}</td><td>{ps['wins']}</td>"
                         f"<td class='{'up' if ps['gap']>=0 else 'dn'}'>{ps['gap']*100:+.0f} poin</td>"
                         f"<td class='{'up' if ps['roi']>=0 else 'dn'}'>{ps['roi']*100:+.0f}%</td><td>{rr2}</td><td>{ps['roi_ex']*100:+.0f}%</td><td>{ci2}</td></tr>")
    notes = ''.join(f"<div><b>{k}</b>: {html.escape(v)}</div>" for k, v in RULE_TEXT.items() if any(t.get('ver') == k for t in pos))
    return (f"<h2>Papan keputusan</h2>{gate}<div class='wrap'><table style='min-width:1000px'><tr><th>Strategi</th><th>Versi</th><th>Peran</th><th>Posisi</th>"
            f"<th>Selesai</th><th>Menang</th><th>Selisih vs harga</th><th>ROI (sim)</th><th>ROI (order book nyata)</th><th>ROI tanpa menang terbesar</th><th>Rentang 90% (per tema)</th></tr>{rows}</table></div>"
            f"<div class='note'>{notes}</div>")

def pnl(t):
    if t['status'] == 'won': return t['shares'] - t['stake']
    if t['status'] == 'lost': return -t['stake']
    return t['shares'] * t['cur'] - t['stake']

# ---------- 5. DASHBOARD ----------
def wib(iso):
    try:
        d = dt.datetime.fromisoformat(iso).astimezone(dt.timezone(dt.timedelta(hours=7)))
        return d.strftime('%d %b %H:%M')
    except Exception:
        return '-'

def money(x): return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"

def build_html(total, parsed, cands, pos):
    e = html.escape
    openp = [t for t in pos if t['status'] == 'open']
    done = [t for t in pos if t['status'] in ('won', 'lost')]
    wins = sum(1 for t in done if t['status'] == 'won')
    tot_pnl = sum(pnl(t) for t in pos)
    pb = sum(pnl(t) for t in pos if t.get('strategy','baseline-favorit')=='baseline-favorit')
    pc = sum(pnl(t) for t in pos if t.get('strategy')=='claude-A')
    pf = sum(pnl(t) for t in pos if t.get('strategy')=='fade-favorit')
    fd = [t for t in pos if t.get('strategy')=='fade-favorit' and t['status'] in ('won','lost')]
    fd_txt = f"{sum(1 for t in fd if t['status']=='won')}/{len(fd)}"
    wr = f'{100*wins/len(done):.0f}%' if done else '-'
    rows = ''
    for m in cands[:CFG['TOP_N']]:
        i = 0 if m['p'][0] >= m['p'][1] else 1
        flag = ' <span class="tag warn">harga janggal</span>' if m['anomaly'] else ''
        rows += (f"<tr><td><a href='{e(m['url'])}' target='_blank'>{e(m['q'])}</a>{flag}</td>"
                 f"<td>{e(m['o'][i])} {m['p'][i]*100:.0f}%</td><td>${m['liq']:,.0f}</td>"
                 f"<td>${m['vol24']:,.0f}</td><td>{m['days']:.0f} hari</td>"
                 f"<td><div class='bar'><i style='width:{m['score']}%'></i></div>{m['score']}</td></tr>")
    prow = ''
    for t in sorted(pos, key=lambda x: (x['status'] != 'open', x.get('strategy') != 'claude-A', x['opened']))[:70]:
        p = pnl(t); cls = 'up' if p >= 0 else 'dn'
        why = f"<br><small style='color:#8fa0bd'>{e(t.get('why',''))}</small>" if t.get('why') else ''
        real = f"<br><small style='color:#8fa0bd'>nyata {t['real_entry']*100:.1f}c</small>" if t.get('real_entry') else ''
        prow += (f"<tr><td><a href='{e(t['url'])}' target='_blank'>{e(t['q'])}</a>{why}</td>"
                 f"<td><b>{e(t['side'])}</b></td><td>{wib(t['opened'])}</td><td>{t['entry']*100:.1f}c{real}</td>"
                 f"<td>{t['shares']:.1f}</td><td>${t['stake']:.0f}</td><td>{t['cur']*100:.1f}c</td>"
                 f"<td class='{cls}'>{money(p)}</td><td><span class='tag {t['status']}'>{t['status']}</span></td>"
                 f"<td>{e(t.get('strategy','baseline-favorit'))}<br><small style='color:#8fa0bd'>{e(t.get('ver',''))}</small></td></tr>")
    if not prow: prow = "<tr><td colspan=10>Belum ada posisi.</td></tr>"
    css = """
.pmx{--bg:#0b1220;--card:#131c2e;--line:#22304a;--tx:#e6edf7;--mu:#8fa0bd;--ac:#38e8a0;--rd:#ff6b6b;--yl:#ffc857;
background:var(--bg);color:var(--tx);font-family:system-ui,sans-serif;padding:16px;border-radius:12px;max-width:1100px;margin:auto}
.pmx h1{font-size:20px;margin:0 0 4px}.pmx .sub{color:var(--mu);font-size:12px;margin-bottom:14px}
.pmx .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.pmx .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.pmx .card b{display:block;font-size:22px;margin-top:4px}.pmx .card span{color:var(--mu);font-size:12px}
.pmx h2{font-size:15px;margin:18px 0 8px}.pmx .wrap{overflow-x:auto}
.pmx table{border-collapse:collapse;width:100%;font-size:13px;min-width:900px}
.pmx th{color:var(--mu);text-align:left;font-weight:500;padding:8px;border-bottom:1px solid var(--line)}
.pmx td{padding:8px;border-bottom:1px solid var(--line);vertical-align:middle}
.pmx a{color:var(--tx);text-decoration:none}.pmx a:hover{color:var(--ac)}
.pmx .up{color:var(--ac)}.pmx .dn{color:var(--rd)}
.pmx .bar{display:inline-block;width:60px;height:6px;background:var(--line);border-radius:4px;margin-right:6px;vertical-align:middle}
.pmx .bar i{display:block;height:100%;background:var(--ac);border-radius:4px}
.pmx .tag{font-size:11px;padding:2px 7px;border-radius:20px;background:var(--line)}
.pmx .tag.warn{background:#4a3b12;color:var(--yl)}.pmx .tag.won{background:#0f3d2e;color:var(--ac)}
.pmx .tag.lost{background:#4a1d1d;color:var(--rd)}.pmx .note{color:var(--mu);font-size:12px;margin-top:14px;line-height:1.5}
"""
    body = f"""<style>{css}</style><div class="pmx"><h1>Polymarket Mesin v1</h1>
<div class="sub">Update: {NOW.strftime('%d %b %Y %H:%M')} UTC &middot; MODE SIMULASI (uang virtual) &middot; diperbarui otomatis tiap hari</div>
<div class="grid">
<div class="card"><span>Market dipindai</span><b>{total}</b></div>
<div class="card"><span>Lolos saringan</span><b>{len(cands)}</b></div>
<div class="card"><span>Posisi terbuka</span><b>{len(openp)}</b></div>
<div class="card"><span>Win rate (selesai)</span><b>{wr}</b></div>
<div class="card"><span>P/L baseline</span><b class="{'up' if pb>=0 else 'dn'}">{money(pb)}</b></div>
<div class="card"><span>P/L Claude-A</span><b class="{'up' if pc>=0 else 'dn'}">{money(pc)}</b></div>
<div class="card"><span>Fade menang/selesai</span><b>{fd_txt}</b></div>
<div class="card"><span>P/L fade-favorit</span><b class="{'up' if pf>=0 else 'dn'}">{money(pf)}</b></div>
<div class="card"><span>Total P/L virtual</span><b class="{'up' if tot_pnl>=0 else 'dn'}">{money(tot_pnl)}</b></div></div>
{scorecard_html(pos)}
<h2>Kandidat teratas hari ini</h2><div class="wrap"><table>
<tr><th>Pertanyaan</th><th>Favorit</th><th>Likuiditas</th><th>Vol 24j</th><th>Tutup</th><th>Skor</th></tr>{rows}</table></div>
<h2>Buku posisi simulasi (posisi Claude-A di atas)</h2><div class="wrap"><table>
<tr><th>Pertanyaan (pair)</th><th>Sisi dibeli</th><th>Waktu masuk (WIB)</th><th>Harga masuk</th><th>Lembar</th><th>Modal virtual</th><th>Harga kini</th><th>P/L</th><th>Status</th><th>Strategi</th></tr>{prow}</table></div>
<div class="note">Skor = kelayakan diperdagangkan (likuiditas, volume, spread), BUKAN prediksi menang.
Posisi 'baseline-favorit' hanya patokan pembanding; AI di fase 2 harus mengalahkannya.</div></div>"""
    return body

# ---------- NOTIFIKASI TELEGRAM (opsional; hanya aktif kalau TELEGRAM_TOKEN & TELEGRAM_CHAT_ID diisi di GitHub Secrets) ----------
def tg_ready():
    return bool(os.environ.get('TELEGRAM_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'))

def notify(text):
    if not tg_ready(): return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
                          json=dict(chat_id=os.environ['TELEGRAM_CHAT_ID'], text=text[:3900], disable_web_page_preview=True), timeout=20)
        return r.ok
    except Exception:
        print('(notifikasi Telegram gagal dikirim)')     # sengaja tanpa detail agar token tidak bocor ke log
        return False

STATE_FILE = f'{BASE}/notify_state.json'
def send_notifications(pos):
    if not tg_ready(): return
    try: st = json.load(open(STATE_FILE))
    except Exception: st = {}
    msgs = []
    if SETTLED_NOW:
        ln = ['🏁 Posisi selesai:']
        for t in SETTLED_NOW[:15]:
            ln.append(f"{'✅' if t['status'] == 'won' else '❌'} [{t.get('strategy')}/{t.get('ver')}] {t['q'][:70]} | beli {t['side']} @ {t['entry']*100:.0f}c | "
                      f"{'MENANG' if t['status'] == 'won' else 'KALAH'} ({money(pnl(t))})")
        msgs.append('\n'.join(ln))
    g = gate_status(pos)
    prev = st.get('light', 'merah')
    if g['light'] != prev:
        if g['light'] == 'kuning':
            msgs.append(f"🟡 Lampu KUNING (aturan {FADE_ID}): {g['n']} posisi selesai, ROI {g['roi']*100:+.0f}%, selisih {g['gap']*100:+.0f} poin. "
                        "Ini hanya izin untuk uji eksekusi uang asli SANGAT kecil (maks $100 total, taruhan $2-5), BUKAN bukti untung. Buka chat dengan Claude dan minta 'cek' dulu.")
        elif g['light'] == 'hijau':
            msgs.append(f"🟢 Lampu HIJAU (aturan {FADE_ID}): {g['n']} posisi selesai, batas bawah ROI {g['ci5']*100:+.0f}%. "
                        "Minta 'cek' ke Claude untuk meninjau bersama sebelum menaikkan modal apa pun.")
        else:
            msgs.append(f"🔴 Lampu kembali MERAH (aturan {FADE_ID}): hasil terbaru menurunkan angkanya.")
        st['light'] = g['light']
    today = NOW.strftime('%Y-%m-%d')
    if NOW.weekday() == 0 and st.get('last_weekly') != today:        # tiap Senin
        n_open = sum(1 for t in pos if t['status'] == 'open')
        n_done = sum(1 for t in pos if t['status'] in ('won', 'lost'))
        msgs.append(f"📊 Ringkasan mingguan: {n_open} posisi terbuka, {n_done} selesai. lampu {g['light']} (fade {FADE_ID}): {g['n']} selesai"
                    + (f", ROI {g['roi']*100:+.0f}%." if g['n'] else '.'))
        st['last_weekly'] = today
    if os.environ.get('TG_PING') == 'true':
        msgs.append('✅ Tes notifikasi berhasil. Mesin Polymarket terhubung ke Telegram.')
    for m_ in msgs: notify(m_)
    json.dump(st, open(STATE_FILE, 'w'))

# ---------- 6. JALANKAN ----------
total, n_parsed, cands = scan()
pos = migrate_versions(refresh_positions(load_pos()))
pos = open_baseline(pos, cands)
pos = open_ai_picks(pos, cands)
pos = open_fade(pos, cands)
pos = open_yes_premium(pos, cands)
save_pos(pos)
send_notifications(pos)
body = build_html(total, n_parsed, cands, pos)
full = f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Polymarket Mesin v1</title><style>body{{margin:0;background:#050912}}</style></head><body>{body}</body></html>"
open(f'{BASE}/dashboard.html', 'w', encoding='utf-8').write(full)
print(f'OK: {total} market dipindai, {len(cands)} lolos saringan. File: {BASE}/dashboard.html')
try:
    from IPython.display import HTML, display
    display(HTML('<iframe srcdoc="%s" style="width:100%%;height:1100px;border:0;border-radius:12px"></iframe>' % html.escape(full, quote=True)))
    if IN_COLAB:
        from google.colab import files
        files.download(f'{BASE}/dashboard.html')
except Exception:
    pass

# ---------- 7. BRIEF UNTUK CLAUDE (analisis semi-manual) ----------
lines = ['Analisis kandidat Polymarket berikut. Untuk tiap market beri estimasi probabilitas',
         'kamu, selisih dengan harga pasar, keyakinan (rendah/sedang/tinggi), dan alasan singkat.', '']
seen_ev = set()
for m in [c for c in cands if c.get('extra')] + [c for c in cands if not c.get('extra')]:
    if m['ev'] in seen_ev: continue
    seen_ev.add(m['ev'])
    lines.append(f"- {m['q']} | harga: {m['o'][0]} {m['p'][0]*100:.0f}% / {m['o'][1]} {m['p'][1]*100:.0f}% | tutup {m['days']:.0f} hari | likuiditas ${m['liq']:,.0f} | {m['url']}")
    if len(seen_ev) >= 10: break
brief = '\n'.join(lines)
open(f'{BASE}/brief_untuk_claude.txt', 'w', encoding='utf-8').write(brief)
print('\n===== COPY TEKS DI BAWAH INI KE CLAUDE =====\n' + brief)

# ---------- 8. LEDGER POSISI (untuk dikirim ke Claude) ----------
led = ['', '===== POSISI SIMULASI SAAT INI (kirim juga ini ke Claude) =====']
for t in pos:
    if t['status'] != 'open': continue
    led.append(f"- [{t.get('strategy','baseline-favorit')}/{t.get('ver','')}] {t['q']} | beli {t['side']} @ {t['entry']*100:.1f}c | masuk {wib(t['opened'])} WIB | kini {t['cur']*100:.1f}c | P/L {money(pnl(t))}")
settled = [t for t in pos if t['status'] in ('won','lost')]
led.append(f"Selesai: {len(settled)} posisi, menang {sum(1 for t in settled if t['status']=='won')}.")
print('\n'.join(led))
open(f'{BASE}/brief_untuk_claude.txt', 'w', encoding='utf-8').write(brief + '\n' + '\n'.join(led))
