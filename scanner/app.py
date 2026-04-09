from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import urllib.request
import json
import threading
import time

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

# UT Bot timeframes to check
TIMEFRAMES = ['15m', '1h', '2h', '4h']

scan_cache = {
    'last_scan': None,
    'next_scan': None,
    'results': None,
    'status': 'idle'
}
scan_lock = threading.Lock()


def fetch_klines(symbol, interval, limit=60):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except:
        return None


def ut_bot(symbol, interval, atr_period=10, mult=1.5):
    """
    UT Bot Alert — ATR tabanlı trailing stop.
    Fiyat trailing stop'u yukarı kırarsa BUY,
    aşağı kırarsa SELL, değişim yoksa NEUTRAL döner.
    """
    data = fetch_klines(symbol, interval, limit=60)
    if not data or len(data) < atr_period + 3:
        return 'NO_DATA', None

    closes = [float(k[4]) for k in data]
    highs  = [float(k[2]) for k in data]
    lows   = [float(k[3]) for k in data]

    # True Range
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1])
        ))

    # Smoothed ATR (RMA)
    atr_val = sum(tr[:atr_period]) / atr_period
    atr_vals = [atr_val]
    for i in range(atr_period, len(tr)):
        atr_val = (atr_val * (atr_period - 1) + tr[i]) / atr_period
        atr_vals.append(atr_val)

    # Trailing stop + direction
    c = closes[atr_period:]
    if len(c) < 2:
        return 'NO_DATA', None

    stop      = [c[0] - mult * atr_vals[0]]
    direction = [1]

    for i in range(1, len(c)):
        prev_stop = stop[-1]
        prev_dir  = direction[-1]
        atr       = atr_vals[i]

        if prev_dir == 1:
            new_stop = max(c[i] - mult * atr, prev_stop)
        else:
            new_stop = min(c[i] + mult * atr, prev_stop)

        if c[i] > new_stop:
            new_dir = 1
        else:
            new_dir = -1

        # Direction changed → reset stop
        if new_dir != prev_dir:
            new_stop = c[i] - mult * atr if new_dir == 1 else c[i] + mult * atr

        stop.append(new_stop)
        direction.append(new_dir)

    prev_dir = direction[-2]
    curr_dir = direction[-1]
    curr_stop = stop[-1]
    curr_price = c[-1]

    if prev_dir == -1 and curr_dir == 1:
        signal = 'BUY'
    elif prev_dir == 1 and curr_dir == -1:
        signal = 'SELL'
    else:
        signal = 'BUY_HOLD' if curr_dir == 1 else 'SELL_HOLD'

    return signal, round(curr_stop, 8), round(curr_price, 8)


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
        tf_results = {}
        for tf in TIMEFRAMES:
            res = ut_bot(coin, tf)
            if res[0] == 'NO_DATA':
                tf_results[tf] = {'signal': 'NO_DATA', 'stop': None, 'price': None}
            else:
                signal, stop, price = res
                tf_results[tf] = {
                    'signal': signal,
                    'stop': stop,
                    'stop_fmt': fmt_price(stop),
                    'price': price,
                    'price_fmt': fmt_price(price)
                }

        # Current price from shortest available timeframe
        current_price = None
        for tf in TIMEFRAMES:
            if tf_results[tf].get('price'):
                current_price = tf_results[tf]['price']
                break

        # Score: count timeframes with fresh BUY signal
        buy_count  = sum(1 for tf in TIMEFRAMES if tf_results[tf]['signal'] == 'BUY')
        hold_count = sum(1 for tf in TIMEFRAMES if tf_results[tf]['signal'] == 'BUY_HOLD')

        results.append({
            'coin': coin,
            'price': current_price,
            'price_fmt': fmt_price(current_price),
            'timeframes': tf_results,
            'buy_count': buy_count,
            'hold_count': hold_count,
            'error': False
        })

    # Sort: fresh BUY count desc, then hold count desc
    results.sort(key=lambda x: (-x['buy_count'], -x['hold_count'], x['coin']))
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
                scan_cache['next_scan'] = now + 900
                scan_cache['status'] = 'done'
        except Exception as e:
            with scan_lock:
                scan_cache['status'] = f'error: {e}'

        time.sleep(900)


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
    print("Kripto UT Bot Tarayici: http://localhost:5050")
    app.run(host='0.0.0.0', port=5050, debug=False)
