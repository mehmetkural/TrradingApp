from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import urllib.request
import json
import threading
import time
import os

app = Flask(__name__, static_folder='static')
CORS(app)

EXCLUDE = {
    'USDT','USDC','DAI','USDe','PYUSD','USD1','USDG','USDD','RLUSD','TUSD',
    'EURC','FDUSD','XAUt','PAXG','LEO','OKB','KCS','GT','BGB','CC','M',
    'WLFI','STABLE','SIREN','NIGHT','EDGE','VVV','ASTER','U','PUMP'
}

TOP100 = [
    'BTC','ETH','XRP','BNB','SOL','TRX','DOGE','HYPE','ADA','BCH',
    'LINK','XMR','ZEC','XLM','LTC','AVAX','HBAR','SUI','TAO','SHIB',
    'TON','CRO','DOT','UNI','NEAR','PEPE','AAVE','ICP','ETC','ONDO',
    'RENDER','ALGO','POL','ATOM','QNT','KAS','WLD','ENA','FIL','TRUMP',
    'APT','FLR','ZRO','ARB','VET','JUP','FET','BONK','CAKE','DASH',
    'VIRTUAL','PENGU','STX','CHZ','XTZ','SEI','CRV','GNO','PI','MNT',
    'MORPHO','DEXE','JST','NEXO','SUN','DCR','ETHFI','SKY','MON',
    'LINK','DOT','ATOM','ALGO','VET','ZEC'
]

COINS = list(dict.fromkeys([c for c in TOP100 if c not in EXCLUDE]))

scan_cache = {
    'last_scan': None,
    'next_scan': None,
    'results': None,
    'status': 'idle'
}
scan_lock = threading.Lock()


def fetch_klines(symbol, interval='1d', limit=35):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except:
        return None


def ema(prices, period):
    k = 2 / (period + 1)
    e = prices[0]
    result = [e]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
        result.append(e)
    return result


def calc_sma20(closes):
    if len(closes) < 20:
        return None
    return sum(closes[-20:]) / 20


def calc_macd(closes):
    if len(closes) < 26:
        return None, None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(closes))]
    signal = ema(macd_line[25:], 9)
    macd_recent = macd_line[25:]
    return macd_recent, signal


def check_macd_crossover(macd_vals, signal_vals, lookback=3):
    if len(macd_vals) < lookback + 1 or len(signal_vals) < lookback + 1:
        return False
    for i in range(-lookback, 0):
        prev_i = i - 1
        if macd_vals[prev_i] < signal_vals[prev_i] and macd_vals[i] > signal_vals[i]:
            return True
    return False


def calc_ut_bot_2h(symbol):
    data = fetch_klines(symbol, '2h', 60)
    if not data or len(data) < 25:
        return 'NO_DATA', None

    closes = [float(k[4]) for k in data]
    highs  = [float(k[2]) for k in data]
    lows   = [float(k[3]) for k in data]

    atr_period = 10
    mult = 1.5

    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        ))

    atr_val = sum(tr[:atr_period]) / atr_period
    atr_vals = [atr_val]
    for i in range(atr_period, len(tr)):
        atr_val = (atr_val * (atr_period - 1) + tr[i]) / atr_period
        atr_vals.append(atr_val)

    c = closes[atr_period:]
    if len(c) < 2:
        return 'NO_DATA', None

    stop = [c[0] - mult * atr_vals[0]]
    direction = [1]

    for i in range(1, len(c)):
        prev_stop = stop[-1]
        prev_dir = direction[-1]
        atr = atr_vals[i]

        if prev_dir == 1:
            new_stop = max(c[i] - mult * atr, prev_stop)
        else:
            new_stop = min(c[i] + mult * atr, prev_stop)

        if c[i] > new_stop:
            new_dir = 1
        else:
            new_dir = -1

        if new_dir != prev_dir:
            new_stop = c[i] - mult * atr if new_dir == 1 else c[i] + mult * atr

        stop.append(new_stop)
        direction.append(new_dir)

    prev_dir = direction[-2]
    curr_dir = direction[-1]
    curr_stop = stop[-1]

    if prev_dir == -1 and curr_dir == 1:
        return 'BUY', round(curr_stop, 8)
    elif prev_dir == 1 and curr_dir == -1:
        return 'SELL', round(curr_stop, 8)
    else:
        return 'NEUTRAL', round(curr_stop, 8)


def fmt_price(p):
    if p is None:
        return '—'
    if p >= 1000:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:.4f}"
    elif p >= 0.001:
        return f"${p:.6f}"
    else:
        return f"${p:.8f}"


def run_scan():
    results = []
    for coin in COINS:
        data = fetch_klines(coin, '1d', 35)
        if not data:
            results.append({
                'coin': coin,
                'price': None,
                'sma20': None,
                'macd_val': None,
                'signal_line': None,
                'ut_signal': 'NO_DATA',
                'ut_stop': None,
                'above_sma': False,
                'macd_crossover': False,
                'macd_bullish': False,
                'score': 0,
                'reasons': ['Binance\'de bulunamadı'],
                'error': True
            })
            continue

        closes = [float(k[4]) for k in data]
        current_price = closes[-1]
        sma20 = calc_sma20(closes)

        if sma20 is None:
            results.append({'coin': coin, 'price': current_price, 'score': 0,
                            'reasons': ['Yetersiz veri'], 'error': True})
            continue

        above_sma = current_price > sma20
        macd_vals, signal_vals = calc_macd(closes)

        if macd_vals is None:
            results.append({'coin': coin, 'price': current_price, 'score': 0,
                            'reasons': ['MACD hesaplanamadı'], 'error': True})
            continue

        crossover = check_macd_crossover(macd_vals, signal_vals)
        macd_now = macd_vals[-1]
        sig_now = signal_vals[-1]
        macd_bullish = macd_now > sig_now

        ut_signal, ut_stop = calc_ut_bot_2h(coin)
        score = sum([above_sma, crossover, ut_signal == 'BUY'])

        reasons = []
        if not above_sma:
            reasons.append(f"Fiyat < SMA20")
        if not crossover:
            reasons.append("MACD bearish" if not macd_bullish else "Crossover > 3 mum")
        if ut_signal != 'BUY':
            reasons.append(f"UT Bot = {ut_signal}")

        results.append({
            'coin': coin,
            'price': current_price,
            'price_fmt': fmt_price(current_price),
            'sma20': sma20,
            'sma20_fmt': fmt_price(sma20),
            'macd_val': round(macd_now, 6),
            'signal_line': round(sig_now, 6),
            'macd_bullish': macd_bullish,
            'ut_signal': ut_signal,
            'ut_stop': ut_stop,
            'ut_stop_fmt': fmt_price(ut_stop),
            'above_sma': above_sma,
            'macd_crossover': crossover,
            'score': score,
            'reasons': reasons,
            'error': False
        })

    results.sort(key=lambda x: (-x.get('score', 0), x['coin']))
    return results


def background_scanner():
    while True:
        with scan_lock:
            scan_cache['status'] = 'scanning'

        try:
            results = run_scan()
            now = time.time()
            with scan_lock:
                scan_cache['results'] = results
                scan_cache['last_scan'] = now
                scan_cache['next_scan'] = now + 900  # 15 min
                scan_cache['status'] = 'done'
        except Exception as e:
            with scan_lock:
                scan_cache['status'] = f'error: {e}'

        time.sleep(900)  # 15 minutes


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/scan')
def api_scan():
    with scan_lock:
        data = dict(scan_cache)
    return jsonify(data)


@app.route('/api/scan/force', methods=['POST'])
def api_force_scan():
    def do_scan():
        with scan_lock:
            scan_cache['status'] = 'scanning'
        try:
            results = run_scan()
            now = time.time()
            with scan_lock:
                scan_cache['results'] = results
                scan_cache['last_scan'] = now
                scan_cache['next_scan'] = now + 900
                scan_cache['status'] = 'done'
        except Exception as e:
            with scan_lock:
                scan_cache['status'] = f'error: {e}'

    t = threading.Thread(target=do_scan, daemon=True)
    t.start()
    return jsonify({'status': 'scan started'})


if __name__ == '__main__':
    scanner_thread = threading.Thread(target=background_scanner, daemon=True)
    scanner_thread.start()
    print("🚀 Kripto Tarayıcı başlatılıyor: http://localhost:5050")
    app.run(host='0.0.0.0', port=5050, debug=False)
