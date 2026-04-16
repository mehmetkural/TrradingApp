from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import urllib.request
import json
import threading
import time
from datetime import datetime, timezone

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
    'INJ','SUPER','ARKM','PYTH','ENS','RSR','GALA','PIXEL'
]

COINS = list(dict.fromkeys([c for c in TOP100 if c not in EXCLUDE]))
TIMEFRAMES = ['15m', '1h', '2h', '4h']

# Binance'da farklı sembol adı olan coinler
SYMBOL_OVERRIDE = {'BABYDOGE': '1000BABYDOGE'}

def binance_sym(symbol):
    return (SYMBOL_OVERRIDE.get(symbol) or symbol) + 'USDT'

MEME_COINS = [
    'DOGE','SHIB','PEPE','BONK','FLOKI','WIF','MEME','BRETT',
    'TURBO','BOME','POPCAT','NEIRO','PNUT','ACT','GOAT','DOGS',
    'PENGU','TRUMP','BABYDOGE','GROK',
]

# ─── Scanner state ────────────────────────────────────────────
scan_cache = {'last_scan': None, 'next_scan': None, 'results': None, 'status': 'idle'}
scan_lock  = threading.Lock()

# ─── Simulation state (normal) ────────────────────────────────
SIM_DEFAULT_BUDGET    = 1000.0  # USD toplam bütçe
SIM_DEFAULT_TRADE_PCT = 0.10    # işlem başına bütçenin %10'u

sim_state = {
    'running':        False,
    'started_at':     None,
    'budget':         SIM_DEFAULT_BUDGET,   # canlı cüzdan (P&L ile değişir)
    'initial_budget': SIM_DEFAULT_BUDGET,   # başlangıç referansı
    'trade_pct':      SIM_DEFAULT_TRADE_PCT,
    'positions':      {},   # coin -> {entry_price, entry_time, qty, cost}
    'trades':         [],   # list of closed trades
    'signals':        {},   # coin -> last seen signal
    'last_check':     None,
    'next_check':     None,
    'total_realized_pnl': 0.0,
}
sim_lock = threading.Lock()

# ─── Simulation state (MEME) ──────────────────────────────────
meme_sim_state = {
    'running':        False,
    'started_at':     None,
    'budget':         SIM_DEFAULT_BUDGET,
    'initial_budget': SIM_DEFAULT_BUDGET,
    'trade_pct':      SIM_DEFAULT_TRADE_PCT,
    'positions':      {},
    'trades':         [],
    'signals':        {},
    'last_check':     None,
    'next_check':     None,
    'total_realized_pnl': 0.0,
}
meme_sim_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# Binance helpers
# ═══════════════════════════════════════════════════════════════

def fetch_klines(symbol, interval, limit=60):
    url = f"https://api.binance.com/api/v3/klines?symbol={binance_sym(symbol)}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except:
        return None


def fetch_price(symbol):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_sym(symbol)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            return float(json.loads(r.read())['price'])
    except:
        return None


# ═══════════════════════════════════════════════════════════════
# UT Bot core
# ═══════════════════════════════════════════════════════════════

def ut_bot(symbol, interval, atr_period=10, mult=1.5):
    data = fetch_klines(symbol, interval, limit=60)
    if not data or len(data) < atr_period + 3:
        return 'NO_DATA', None, None

    closes = [float(k[4]) for k in data]
    highs  = [float(k[2]) for k in data]
    lows   = [float(k[3]) for k in data]

    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        ))

    atr_val = sum(tr[:atr_period]) / atr_period
    atr_vals = [atr_val]
    for i in range(atr_period, len(tr)):
        atr_val = (atr_val * (atr_period - 1) + tr[i]) / atr_period
        atr_vals.append(atr_val)

    c = closes[atr_period:]
    if len(c) < 2:
        return 'NO_DATA', None, None

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

        new_dir = 1 if c[i] > new_stop else -1
        if new_dir != prev_dir:
            new_stop = c[i] - mult * atr if new_dir == 1 else c[i] + mult * atr

        stop.append(new_stop)
        direction.append(new_dir)

    prev_dir  = direction[-2]
    curr_dir  = direction[-1]
    curr_stop = stop[-1]
    curr_price = c[-1]

    if prev_dir == -1 and curr_dir == 1:
        signal = 'BUY'
    elif prev_dir == 1 and curr_dir == -1:
        signal = 'SELL'
    else:
        signal = 'BUY_HOLD' if curr_dir == 1 else 'SELL_HOLD'

    return signal, round(curr_stop, 8), round(curr_price, 8)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def fmt_price(p):
    if p is None: return '—'
    if p >= 1000:       return f"${p:,.2f}"
    elif p >= 1:        return f"${p:.4f}"
    elif p >= 0.001:    return f"${p:.6f}"
    else:               return f"${p:.8f}"


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def now_ts():
    return time.time()


# ═══════════════════════════════════════════════════════════════
# Scanner loop
# ═══════════════════════════════════════════════════════════════

def run_scan():
    results = []
    for coin in COINS:
        tf_results = {}
        for tf in TIMEFRAMES:
            sig, stop, price = ut_bot(coin, tf)
            tf_results[tf] = {
                'signal':    sig,
                'stop':      stop,
                'stop_fmt':  fmt_price(stop),
                'price':     price,
                'price_fmt': fmt_price(price)
            }

        current_price = next(
            (tf_results[tf]['price'] for tf in TIMEFRAMES if tf_results[tf]['price']),
            None
        )
        buy_count  = sum(1 for tf in TIMEFRAMES if tf_results[tf]['signal'] == 'BUY')
        hold_count = sum(1 for tf in TIMEFRAMES if tf_results[tf]['signal'] == 'BUY_HOLD')

        results.append({
            'coin': coin,
            'price': current_price,
            'price_fmt': fmt_price(current_price),
            'timeframes': tf_results,
            'buy_count':  buy_count,
            'hold_count': hold_count,
            'error': False
        })

    results.sort(key=lambda x: (-x['buy_count'], -x['hold_count'], x['coin']))
    return results


def background_scanner():
    while True:
        with scan_lock:
            scan_cache['status'] = 'scanning'
        try:
            results = run_scan()
            now = now_ts()
            with scan_lock:
                scan_cache['results'] = results
                scan_cache['last_scan'] = now
                scan_cache['next_scan'] = now + 900
                scan_cache['status'] = 'done'
            # Scan bitince meme sim de tetikle
            with meme_sim_lock:
                meme_running = meme_sim_state['running']
            if meme_running:
                try:
                    meme_sim_tick()
                except Exception as e:
                    print(f"[MEME SIM SCAN TRIGGER] {e}")
        except Exception as e:
            with scan_lock:
                scan_cache['status'] = f'error: {e}'
        time.sleep(900)


# ═══════════════════════════════════════════════════════════════
# Simulation loop
# ═══════════════════════════════════════════════════════════════

SIM_STOP_LOSS_PCT = 0.08   # %8 stop-loss

def sim_tick():
    """
    Check 1h UT Bot signal (mult=2.0) for every coin.
    BUY       → open position using trade_pct of total budget
    SELL      → close position (real reversal only, not SELL_HOLD)
    STOP_LOSS → close if price drops -%8 from entry
    P&L updates the total budget dynamically.
    """
    with sim_lock:
        if not sim_state['running']:
            return

    for coin in COINS:
        # 1h timeframe, ATR mult=2.0 — daha az gürültü
        sig, stop, price = ut_bot(coin, '1h', mult=2.0)
        if price is None:
            continue

        with sim_lock:
            budget    = sim_state['budget']
            trade_pct = sim_state['trade_pct']
            locked    = sum(p['cost'] for p in sim_state['positions'].values())
            free      = budget - locked
            in_pos    = coin in sim_state['positions']
            pos       = sim_state['positions'].get(coin)

        trade_cost = round(budget * trade_pct, 4)

        # ── Stop-loss kontrolü ────────────────────────────────
        sl_triggered = (
            in_pos and
            price <= pos['entry_price'] * (1 - SIM_STOP_LOSS_PCT)
        )

        # ── Çıkış: sadece gerçek SELL dönüşü veya stop-loss ──
        should_exit = in_pos and (sig == 'SELL' or sl_triggered)

        # ── Open long ──────────────────────────────────────────
        if sig == 'BUY' and not in_pos and free >= trade_cost and trade_cost > 0:
            qty   = trade_cost / price
            entry = {
                'entry_price':     price,
                'entry_price_fmt': fmt_price(price),
                'entry_time':      now_iso(),
                'entry_ts':        now_ts(),
                'qty':             qty,
                'cost':            trade_cost,
                'stop':            stop,
                'stop_fmt':        fmt_price(stop),
            }
            with sim_lock:
                sim_state['positions'][coin] = entry

        # ── Close long ─────────────────────────────────────────
        elif should_exit:
            with sim_lock:
                pos = sim_state['positions'].pop(coin)

            revenue    = pos['qty'] * price
            pnl        = revenue - pos['cost']
            pnl_pct    = (pnl / pos['cost']) * 100
            duration_s = now_ts() - pos['entry_ts']
            hours = int(duration_s // 3600)
            mins  = int((duration_s % 3600) // 60)
            exit_reason = 'STOP_LOSS' if sl_triggered else sig

            trade = {
                'coin':            coin,
                'entry_price':     pos['entry_price'],
                'entry_price_fmt': pos['entry_price_fmt'],
                'exit_price':      price,
                'exit_price_fmt':  fmt_price(price),
                'entry_time':      pos['entry_time'],
                'exit_time':       now_iso(),
                'qty':             pos['qty'],
                'cost':            pos['cost'],
                'revenue':         revenue,
                'pnl':             round(pnl, 4),
                'pnl_pct':         round(pnl_pct, 2),
                'pnl_fmt':         f"{'+'if pnl>=0 else ''}{pnl:.2f}$",
                'duration':        f"{hours}s {mins}dk",
                'win':             pnl >= 0,
                'trigger_sig':     exit_reason,
            }

            with sim_lock:
                sim_state['trades'].insert(0, trade)
                sim_state['total_realized_pnl'] = round(
                    sim_state['total_realized_pnl'] + pnl, 4
                )
                sim_state['budget'] = round(sim_state['budget'] + pnl, 4)

        with sim_lock:
            sim_state['signals'][coin] = sig

    with sim_lock:
        sim_state['last_check'] = now_ts()
        sim_state['next_check'] = now_ts() + 900


def simulation_loop():
    while True:
        with sim_lock:
            running = sim_state['running']
        if running:
            try:
                sim_tick()
            except Exception as e:
                print(f"[SIM ERROR] {e}")
        time.sleep(900)   # every 15 minutes


# ═══════════════════════════════════════════════════════════════
# MEME Simulation loop
# ═══════════════════════════════════════════════════════════════

MEME_STOP_LOSS_PCT = 0.08   # %8 stop-loss

def meme_sim_tick():
    with meme_sim_lock:
        if not meme_sim_state['running']:
            return

    for coin in MEME_COINS:
        # 1h timeframe, ATR mult=2.0 — daha az gürültü
        sig, stop, price = ut_bot(coin, '1h', mult=2.0)
        if price is None:
            continue

        with meme_sim_lock:
            budget    = meme_sim_state['budget']
            trade_pct = meme_sim_state['trade_pct']
            locked    = sum(p['cost'] for p in meme_sim_state['positions'].values())
            free      = budget - locked
            in_pos    = coin in meme_sim_state['positions']
            pos       = meme_sim_state['positions'].get(coin)

        trade_cost = round(budget * trade_pct, 4)

        # ── Stop-loss kontrolü ────────────────────────────────
        sl_triggered = (
            in_pos and
            price <= pos['entry_price'] * (1 - MEME_STOP_LOSS_PCT)
        )

        # ── Çıkış: sadece gerçek SELL dönüşü veya stop-loss ──
        should_exit = in_pos and (sig == 'SELL' or sl_triggered)

        if sig == 'BUY' and not in_pos and free >= trade_cost and trade_cost > 0:
            qty   = trade_cost / price
            entry = {
                'entry_price':     price,
                'entry_price_fmt': fmt_price(price),
                'entry_time':      now_iso(),
                'entry_ts':        now_ts(),
                'qty':             qty,
                'cost':            trade_cost,
                'stop':            stop,
                'stop_fmt':        fmt_price(stop),
            }
            with meme_sim_lock:
                meme_sim_state['positions'][coin] = entry

        elif should_exit:
            with meme_sim_lock:
                pos = meme_sim_state['positions'].pop(coin)

            revenue    = pos['qty'] * price
            pnl        = revenue - pos['cost']
            pnl_pct    = (pnl / pos['cost']) * 100
            duration_s = now_ts() - pos['entry_ts']
            hours = int(duration_s // 3600)
            mins  = int((duration_s % 3600) // 60)
            exit_reason = 'STOP_LOSS' if sl_triggered else sig

            trade = {
                'coin':            coin,
                'entry_price':     pos['entry_price'],
                'entry_price_fmt': pos['entry_price_fmt'],
                'exit_price':      price,
                'exit_price_fmt':  fmt_price(price),
                'entry_time':      pos['entry_time'],
                'exit_time':       now_iso(),
                'qty':             pos['qty'],
                'cost':            pos['cost'],
                'revenue':         revenue,
                'pnl':             round(pnl, 4),
                'pnl_pct':         round(pnl_pct, 2),
                'pnl_fmt':         f"{'+'if pnl>=0 else ''}{pnl:.2f}$",
                'duration':        f"{hours}s {mins}dk",
                'win':             pnl >= 0,
                'trigger_sig':     exit_reason,
            }

            with meme_sim_lock:
                meme_sim_state['trades'].insert(0, trade)
                meme_sim_state['total_realized_pnl'] = round(
                    meme_sim_state['total_realized_pnl'] + pnl, 4
                )
                meme_sim_state['budget'] = round(meme_sim_state['budget'] + pnl, 4)

        with meme_sim_lock:
            meme_sim_state['signals'][coin] = sig

    with meme_sim_lock:
        meme_sim_state['last_check'] = now_ts()
        meme_sim_state['next_check'] = now_ts() + 900


def meme_simulation_loop():
    while True:
        with meme_sim_lock:
            running = meme_sim_state['running']
        if running:
            try:
                meme_sim_tick()
            except Exception as e:
                print(f"[MEME SIM ERROR] {e}")
        time.sleep(900)


# ═══════════════════════════════════════════════════════════════
# Flask routes — Scanner
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/scan')
def api_scan():
    with scan_lock:
        data = dict(scan_cache)
    return jsonify(data)


@app.route('/api/scan/force', methods=['POST'])
def api_scan_force():
    def do():
        with scan_lock:
            scan_cache['status'] = 'scanning'
        try:
            results = run_scan()
            now = now_ts()
            with scan_lock:
                scan_cache['results'] = results
                scan_cache['last_scan'] = now
                scan_cache['next_scan'] = now + 900
                scan_cache['status'] = 'done'
        except Exception as e:
            with scan_lock:
                scan_cache['status'] = f'error: {e}'
    threading.Thread(target=do, daemon=True).start()
    return jsonify({'status': 'scan started'})


# ═══════════════════════════════════════════════════════════════
# Flask routes — Simulation
# ═══════════════════════════════════════════════════════════════

@app.route('/api/sim/state')
def api_sim_state():
    with sim_lock:
        state = dict(sim_state)

    # Enrich open positions with current price + unrealized PnL
    enriched_positions = {}
    for coin, pos in state['positions'].items():
        cur = fetch_price(coin)
        if cur:
            unreal = (cur - pos['entry_price']) * pos['qty']
            unreal_pct = ((cur - pos['entry_price']) / pos['entry_price']) * 100
        else:
            cur, unreal, unreal_pct = pos['entry_price'], 0, 0

        enriched_positions[coin] = {
            **pos,
            'current_price':     cur,
            'current_price_fmt': fmt_price(cur),
            'unrealized_pnl':    round(unreal, 4),
            'unrealized_pnl_pct':round(unreal_pct, 2),
            'unrealized_fmt':    f"{'+'if unreal>=0 else ''}{unreal:.2f}$",
        }

    total_unrealized = sum(p['unrealized_pnl'] for p in enriched_positions.values())
    trades = state['trades']
    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]

    locked_budget = sum(p['cost'] for p in enriched_positions.values())
    free_budget   = round(state['budget'] - locked_budget, 4)
    trade_amount  = round(state['budget'] * state['trade_pct'], 4)
    budget_change = round(state['budget'] - state['initial_budget'], 4)

    return jsonify({
        'running':        state['running'],
        'started_at':     state['started_at'],
        'budget':         state['budget'],
        'initial_budget': state['initial_budget'],
        'trade_pct':      state['trade_pct'],
        'trade_amount':   trade_amount,
        'free_budget':    free_budget,
        'locked_budget':  locked_budget,
        'budget_change':  budget_change,
        'budget_change_fmt': f"{'+'if budget_change>=0 else ''}{budget_change:.2f}$",
        'last_check':     state['last_check'],
        'next_check':     state['next_check'],
        'positions':      enriched_positions,
        'trades':         trades,
        'signals':        state['signals'],
        'stats': {
            'total_trades':       len(trades),
            'open_positions':     len(enriched_positions),
            'wins':               len(wins),
            'losses':             len(losses),
            'win_rate':           round(len(wins)/len(trades)*100, 1) if trades else 0,
            'total_realized_pnl': state['total_realized_pnl'],
            'total_realized_fmt': f"{'+'if state['total_realized_pnl']>=0 else ''}{state['total_realized_pnl']:.2f}$",
            'total_unrealized_pnl': round(total_unrealized, 4),
            'total_unrealized_fmt': f"{'+'if total_unrealized>=0 else ''}{total_unrealized:.2f}$",
            'total_pnl':          round(state['total_realized_pnl'] + total_unrealized, 4),
            'total_pnl_fmt':      f"{'+'if (state['total_realized_pnl']+total_unrealized)>=0 else ''}{(state['total_realized_pnl']+total_unrealized):.2f}$",
            'best_trade':         max(trades, key=lambda t: t['pnl'])['coin'] if trades else '—',
            'best_pnl':           max(trades, key=lambda t: t['pnl'])['pnl'] if trades else 0,
            'worst_trade':        min(trades, key=lambda t: t['pnl'])['coin'] if trades else '—',
            'worst_pnl':          min(trades, key=lambda t: t['pnl'])['pnl'] if trades else 0,
        }
    })


@app.route('/api/sim/start', methods=['POST'])
def api_sim_start():
    body      = request.get_json(silent=True) or {}
    budget    = float(body.get('budget', SIM_DEFAULT_BUDGET))
    trade_pct = float(body.get('trade_pct', SIM_DEFAULT_TRADE_PCT))
    trade_pct = max(0.01, min(trade_pct, 1.0))  # 1% - 100% arası sınırla

    with sim_lock:
        if sim_state['running']:
            return jsonify({'status': 'already running'})
        sim_state['running']        = True
        sim_state['started_at']     = now_iso()
        sim_state['budget']         = budget
        sim_state['initial_budget'] = budget
        sim_state['trade_pct']      = trade_pct

    # First tick immediately in background
    def first_tick():
        try:
            sim_tick()
        except Exception as e:
            print(f"[SIM FIRST TICK] {e}")
    threading.Thread(target=first_tick, daemon=True).start()
    return jsonify({'status': 'started', 'budget': budget, 'trade_pct': trade_pct,
                    'trade_amount': round(budget * trade_pct, 2)})


@app.route('/api/sim/stop', methods=['POST'])
def api_sim_stop():
    with sim_lock:
        sim_state['running'] = False
    return jsonify({'status': 'stopped'})


@app.route('/api/sim/reset', methods=['POST'])
def api_sim_reset():
    with sim_lock:
        sim_state['running']             = False
        sim_state['started_at']          = None
        sim_state['positions']           = {}
        sim_state['trades']              = []
        sim_state['signals']             = {}
        sim_state['last_check']          = None
        sim_state['next_check']          = None
        sim_state['total_realized_pnl']  = 0.0
        sim_state['budget']              = SIM_DEFAULT_BUDGET
        sim_state['initial_budget']      = SIM_DEFAULT_BUDGET
        sim_state['trade_pct']           = SIM_DEFAULT_TRADE_PCT
    return jsonify({'status': 'reset'})


# ═══════════════════════════════════════════════════════════════
# Flask routes — MEME Simulation
# ═══════════════════════════════════════════════════════════════

@app.route('/api/meme/state')
def api_meme_state():
    with meme_sim_lock:
        state = dict(meme_sim_state)

    enriched_positions = {}
    for coin, pos in state['positions'].items():
        cur = fetch_price(coin)
        if cur:
            unreal     = (cur - pos['entry_price']) * pos['qty']
            unreal_pct = ((cur - pos['entry_price']) / pos['entry_price']) * 100
        else:
            cur, unreal, unreal_pct = pos['entry_price'], 0, 0

        enriched_positions[coin] = {
            **pos,
            'current_price':      cur,
            'current_price_fmt':  fmt_price(cur),
            'unrealized_pnl':     round(unreal, 4),
            'unrealized_pnl_pct': round(unreal_pct, 2),
            'unrealized_fmt':     f"{'+'if unreal>=0 else ''}{unreal:.2f}$",
        }

    total_unrealized = sum(p['unrealized_pnl'] for p in enriched_positions.values())
    trades = state['trades']
    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]

    locked_budget = sum(p['cost'] for p in enriched_positions.values())
    free_budget   = round(state['budget'] - locked_budget, 4)
    trade_amount  = round(state['budget'] * state['trade_pct'], 4)
    budget_change = round(state['budget'] - state['initial_budget'], 4)

    return jsonify({
        'running':        state['running'],
        'started_at':     state['started_at'],
        'budget':         state['budget'],
        'initial_budget': state['initial_budget'],
        'trade_pct':      state['trade_pct'],
        'trade_amount':   trade_amount,
        'free_budget':    free_budget,
        'locked_budget':  locked_budget,
        'budget_change':  budget_change,
        'budget_change_fmt': f"{'+'if budget_change>=0 else ''}{budget_change:.2f}$",
        'last_check':     state['last_check'],
        'next_check':     state['next_check'],
        'positions':      enriched_positions,
        'trades':         trades,
        'signals':        state['signals'],
        'stats': {
            'total_trades':         len(trades),
            'open_positions':       len(enriched_positions),
            'wins':                 len(wins),
            'losses':               len(losses),
            'win_rate':             round(len(wins)/len(trades)*100, 1) if trades else 0,
            'total_realized_pnl':   state['total_realized_pnl'],
            'total_realized_fmt':   f"{'+'if state['total_realized_pnl']>=0 else ''}{state['total_realized_pnl']:.2f}$",
            'total_unrealized_pnl': round(total_unrealized, 4),
            'total_unrealized_fmt': f"{'+'if total_unrealized>=0 else ''}{total_unrealized:.2f}$",
            'total_pnl':            round(state['total_realized_pnl'] + total_unrealized, 4),
            'total_pnl_fmt':        f"{'+'if (state['total_realized_pnl']+total_unrealized)>=0 else ''}{(state['total_realized_pnl']+total_unrealized):.2f}$",
            'best_trade':           max(trades, key=lambda t: t['pnl'])['coin'] if trades else '—',
            'best_pnl':             max(trades, key=lambda t: t['pnl'])['pnl'] if trades else 0,
            'worst_trade':          min(trades, key=lambda t: t['pnl'])['coin'] if trades else '—',
            'worst_pnl':            min(trades, key=lambda t: t['pnl'])['pnl'] if trades else 0,
        }
    })


@app.route('/api/meme/start', methods=['POST'])
def api_meme_start():
    body      = request.get_json(silent=True) or {}
    budget    = float(body.get('budget', SIM_DEFAULT_BUDGET))
    trade_pct = float(body.get('trade_pct', SIM_DEFAULT_TRADE_PCT))
    trade_pct = max(0.01, min(trade_pct, 1.0))

    with meme_sim_lock:
        if meme_sim_state['running']:
            return jsonify({'status': 'already running'})
        meme_sim_state['running']        = True
        meme_sim_state['started_at']     = now_iso()
        meme_sim_state['budget']         = budget
        meme_sim_state['initial_budget'] = budget
        meme_sim_state['trade_pct']      = trade_pct

    def first_tick():
        try:
            meme_sim_tick()
        except Exception as e:
            print(f"[MEME SIM FIRST TICK] {e}")
    threading.Thread(target=first_tick, daemon=True).start()
    return jsonify({'status': 'started', 'budget': budget, 'trade_pct': trade_pct,
                    'trade_amount': round(budget * trade_pct, 2)})


@app.route('/api/meme/stop', methods=['POST'])
def api_meme_stop():
    with meme_sim_lock:
        meme_sim_state['running'] = False
    return jsonify({'status': 'stopped'})


@app.route('/api/meme/reset', methods=['POST'])
def api_meme_reset():
    with meme_sim_lock:
        meme_sim_state['running']            = False
        meme_sim_state['started_at']         = None
        meme_sim_state['positions']          = {}
        meme_sim_state['trades']             = []
        meme_sim_state['signals']            = {}
        meme_sim_state['last_check']         = None
        meme_sim_state['next_check']         = None
        meme_sim_state['total_realized_pnl'] = 0.0
        meme_sim_state['budget']             = SIM_DEFAULT_BUDGET
        meme_sim_state['initial_budget']     = SIM_DEFAULT_BUDGET
        meme_sim_state['trade_pct']          = SIM_DEFAULT_TRADE_PCT
    return jsonify({'status': 'reset'})


# ═══════════════════════════════════════════════════════════════
# Start background threads
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    threading.Thread(target=background_scanner,   daemon=True).start()
    threading.Thread(target=simulation_loop,      daemon=True).start()
    threading.Thread(target=meme_simulation_loop, daemon=True).start()
    print("Kripto UT Bot Tarayici + Simulasyon: http://localhost:5050")
    app.run(host='0.0.0.0', port=5050, debug=False)
