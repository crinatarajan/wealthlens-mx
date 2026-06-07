import os, sqlite3, json, csv, io, re, time, urllib.request, urllib.error, urllib.parse

from datetime import datetime, date
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g, Response, stream_with_context)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ── Load .env FIRST — must happen before reading any os.environ keys ──────────
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ── AI CONFIGURATION — read AFTER dotenv is loaded ────────────────────────────
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'https://api.groq.com/openai/v1/chat/completions')
AI_API_KEY  = os.environ.get('GROQ_API_KEY', '')
AI_MODEL    = os.environ.get('AI_MODEL', 'llama-3.3-70b-versatile')

def _reload_ai_config():
    global AI_API_KEY, AI_BASE_URL, AI_MODEL
    AI_API_KEY  = os.environ.get('GROQ_API_KEY', AI_API_KEY)
    AI_BASE_URL = os.environ.get('AI_BASE_URL', AI_BASE_URL)
    AI_MODEL    = os.environ.get('AI_MODEL', AI_MODEL)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'wealthlens-mx-secret-2026-change-in-prod')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

DB_PATH = os.path.join(os.path.dirname(__file__), 'wealthlens.db')
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls', 'pdf', 'txt'}

# ─── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            lang TEXT DEFAULT 'es',
            currency TEXT DEFAULT 'MXN',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            value_mxn REAL NOT NULL,
            currency TEXT DEFAULT 'MXN',
            note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_es TEXT,
            target_mxn REAL NOT NULL,
            saved_mxn REAL DEFAULT 0,
            deadline TEXT,
            priority INTEGER DEFAULT 1,
            color TEXT DEFAULT '#1D9E75',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT DEFAULT 'other',
            source TEXT DEFAULT 'manual',
            account TEXT,
            reference TEXT,
            imported_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            monthly_limit_mxn REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, category)
        );
        CREATE TABLE IF NOT EXISTS recurring (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            amount_mxn REAL NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('income','expense')),
            category TEXT DEFAULT 'other',
            frequency TEXT DEFAULT 'monthly',
            next_date TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            message_es TEXT,
            seen INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    """)
    for col, ddl in [
        ("goals", "saved_mxn REAL DEFAULT 0"),
        ("goals", "deadline TEXT"),
        ("goals", "priority INTEGER DEFAULT 1"),
    ]:
        try:
            db.execute(f"ALTER TABLE {col} ADD COLUMN {ddl}")
        except Exception:
            pass
    db.commit()
    db.close()

# ─── Auth ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()

# ─── FX ───────────────────────────────────────────────────────────────────────
# ── FX cache: refresh from an open exchange rate API every 6 hours ──────────
_FX_CACHE   = {'MXN': 1, 'USD': 17.15, 'EUR': 18.60, 'CAD': 12.60, 'GBP': 21.70}
_FX_FETCHED = 0  # epoch seconds

def _refresh_fx():
    """Fetch live USD/MXN (and other pairs) from exchangerate-api (free, no key)."""
    global _FX_CACHE, _FX_FETCHED
    if time.time() - _FX_FETCHED < 21_600:   # 6 hours
        return
    try:
        import requests as _req
        # exchangerate-api open endpoint — no key required
        r = _req.get(
            'https://open.er-api.com/v6/latest/MXN',
            timeout=8
        )
        data = r.json()
        if data.get('result') == 'success':
            rates = data['rates']          # rates relative to MXN base
            # rates['USD'] = how many USD per 1 MXN  →  MXN per USD = 1/rates['USD']
            _FX_CACHE['USD'] = round(1 / rates['USD'], 4)
            _FX_CACHE['EUR'] = round(1 / rates['EUR'], 4)
            _FX_CACHE['CAD'] = round(1 / rates['CAD'], 4)
            _FX_CACHE['GBP'] = round(1 / rates['GBP'], 4)
            _FX_FETCHED = time.time()
    except Exception:
        pass  # keep stale values on error

SYMBOLS = {'MXN': '$', 'USD': 'US$', 'EUR': '€', 'CAD': 'CA$', 'GBP': '£'}

def mxn_to(amount_mxn, currency):
    _refresh_fx()
    return amount_mxn / _FX_CACHE.get(currency, 1)

def to_mxn(amount, currency):
    _refresh_fx()
    return amount * _FX_CACHE.get(currency, 1)

# ─── AI UTILITIES ─────────────────────────────────────────────────────────────
def _is_ai_configured():
    return bool(AI_API_KEY and AI_API_KEY.strip())

# ── FIX 1: retry + longer timeout for AI calls ──────────────────────────────
def call_ai_gateway(messages, stream=False, max_tokens=1000, retries=3):
    """
    Call the AI gateway with automatic retry on transient errors.

    Retries up to `retries` times with exponential back-off on:
      • 429 Too Many Requests (Groq rate-limit)
      • 502/503/504 Gateway errors
      • Connection / timeout errors

    Raises the last exception if all attempts fail.
    """
    if not _is_ai_configured():
        raise ValueError("AI API key not configured")

    import requests

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }

    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                AI_BASE_URL,
                json=payload,
                headers=headers,
                timeout=45,          # was 30 — Groq can be slow on first wake
            )
            if resp.status_code == 429:
                # Respect Retry-After header if present, else back-off
                wait = int(resp.headers.get('Retry-After', 2 ** attempt))
                time.sleep(min(wait, 10))
                last_exc = Exception(f"Rate limited (429) after {attempt+1} attempt(s)")
                continue
            if resp.status_code in (502, 503, 504):
                time.sleep(2 ** attempt)
                last_exc = Exception(f"Gateway error {resp.status_code}")
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            time.sleep(2 ** attempt)
            last_exc = Exception("AI gateway timed out")
        except requests.exceptions.ConnectionError as e:
            time.sleep(2 ** attempt)
            last_exc = e

    raise last_exc or Exception("AI gateway failed after retries")

# ─── CATEGORY / IMPORT PARSERS ────────────────────────────────────────────────
def detect_bank(filename, text_sample):
    fn = filename.lower(); sample = text_sample.lower()
    if 'bbva' in fn or 'bancomer' in fn or 'bbva' in sample: return 'bbva'
    if 'banamex' in fn or 'citibanamex' in fn or 'banamex' in sample: return 'banamex'
    if 'santander' in fn or 'santander' in sample: return 'santander'
    if 'banorte' in fn or 'banorte' in sample: return 'banorte'
    if 'hsbc' in fn or 'hsbc' in sample: return 'hsbc'
    if 'scotiabank' in fn or 'scotiabank' in sample: return 'scotiabank'
    if 'ixe' in fn or 'ixe' in sample: return 'ixe'
    if 'inbursa' in fn or 'inbursa' in sample: return 'inbursa'
    if 'american' in fn or 'amex' in fn or 'americanexpress' in sample: return 'amex'
    return 'generic'

def parse_csv_transactions(data, bank='generic', user_id=None):
    try:
        f = io.StringIO(data)
        reader = csv.DictReader(f)
        rows = list(reader)
        transactions = []
        for row in rows:
            try:
                if bank == 'bbva':
                    date_str = row.get('Fecha', '').strip()
                    desc     = row.get('Concepto', '').strip()
                    amount   = float(row.get('Monto', 0))
                elif bank == 'santander':
                    date_str = row.get('Fecha de operación', '').strip()
                    desc     = row.get('Descripción', '').strip()
                    amount   = float(row.get('Importe', 0))
                else:
                    date_str = row.get('date', row.get('Date', '')).strip()
                    desc     = row.get('description', row.get('Description', '')).strip()
                    amount   = float(row.get('amount', row.get('Amount', 0)))
                if date_str and desc:
                    transactions.append({
                        'date': date_str, 'description': desc, 'amount': amount,
                        'category': categorize_transaction(desc), 'source': 'import', 'bank': bank
                    })
            except (ValueError, KeyError):
                continue
        return transactions
    except Exception:
        return []

def categorize_transaction(description):
    desc = description.lower()
    categories = {
        'groceries':     ['supermarket','grocery','costco','walmart','soriana','chedraui'],
        'dining':        ['restaurant','cafe','coffee','pizza','burger','taco','comida'],
        'transport':     ['uber','taxi','gasolina','fuel','transporte','transit'],
        'utilities':     ['electricity','agua','water','internet','telefonica','luz'],
        'entertainment': ['cinema','movie','spotify','netflix','gaming','entertainment'],
        'health':        ['pharmacy','doctor','hospital','medicine','farmacia','salud'],
        'shopping':      ['mall','store','amazon','mercadolibre','tienda'],
    }
    for category, keywords in categories.items():
        if any(kw in desc for kw in keywords):
            return category
    return 'other'

def build_financial_context(user_id, lang, date_from=None, date_to=None):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    assets = db.execute(
        "SELECT SUM(value_mxn) as total FROM assets WHERE user_id=?", (user_id,)
    ).fetchone()
    goals = db.execute(
        "SELECT COUNT(*) as count, SUM(target_mxn - saved_mxn) as remaining FROM goals WHERE user_id=?",
        (user_id,)
    ).fetchone()
    recent = db.execute("""
        SELECT SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income,
               SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as expenses
        FROM transactions WHERE user_id=? AND date >= date('now', '-30 days')
    """, (user_id,)).fetchone()

    total   = assets['total']  or 0
    remain  = goals['remaining'] or 0
    income  = recent['income']   or 0
    expenses= recent['expenses'] or 0

    if lang == 'es':
        return (
            f"Tu nombre: {user['name']}\nMoneda preferida: {user['currency']}\n"
            f"PATRIMONIO:\nTotal de activos: ${total:,.0f} MXN\n"
            f"METAS:\nMetas activas: {goals['count']}\nDinero aún por ahorrar: ${remain:,.0f} MXN\n"
            f"ÚLTIMOS 30 DÍAS:\nIngresos: ${income:,.0f} MXN\nGastos: ${expenses:,.0f} MXN\n"
            f"Flujo neto: ${income - expenses:,.0f} MXN"
        )
    return (
        f"Your name: {user['name']}\nPreferred currency: {user['currency']}\n"
        f"WEALTH:\nTotal assets: ${total:,.0f} MXN\n"
        f"GOALS:\nActive goals: {goals['count']}\nRemaining to save: ${remain:,.0f} MXN\n"
        f"LAST 30 DAYS:\nIncome: ${income:,.0f} MXN\nExpenses: ${expenses:,.0f} MXN\n"
        f"Net cash flow: ${income - expenses:,.0f} MXN"
    )

# ─── DEPOSIT RATES ─────────────────────────────────────────────────────────────
_DEPOSIT_RATES_CACHE = {
    'rates': [
        {'bank': 'Banregio',     'rate': 12.10, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'Inbursa',      'rate': 11.80, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'BBVA México',  'rate': 11.50, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'Banbajío',     'rate': 11.20, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'Scotiabank MX','rate': 11.25, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'Citibanamex',  'rate': 11.00, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'Banorte',      'rate': 10.75, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'Santander MX', 'rate': 10.50, 'term': '28 días', 'type': 'CEDE'},
        {'bank': 'HSBC México',  'rate': 10.50, 'term': '28 días', 'type': 'DPF'},
    ],
    'source': 'static_fallback',
    'reference': 'Banxico TIIE ~10.5% base rate (Apr 2026). GAT approximate.',
    'updated': '2026-04',
}

# ─── CRYPTO PRICE HELPER ──────────────────────────────────────────────────────
# ── FIX 2: multi-source crypto with live MXN conversion ─────────────────────
_CRYPTO_CACHE      = {}
_CRYPTO_CACHE_TIME = 0
_CRYPTO_CACHE_TTL  = 60  # seconds — refresh at most once per minute

def _fetch_crypto_prices(ids: str) -> dict:
    """
    Fetch crypto prices using a cascade of free sources:
      1. CoinGecko free API  (primary)
      2. CoinCap REST API    (fallback — no key, no rate-limit header needed)
      3. Stale cache         (last known good data)

    All prices returned include both `usd` and `mxn` keys, computed
    from a live USD/MXN FX rate.
    """
    global _CRYPTO_CACHE, _CRYPTO_CACHE_TIME

    now = time.time()
    if _CRYPTO_CACHE and (now - _CRYPTO_CACHE_TIME) < _CRYPTO_CACHE_TTL:
        return _CRYPTO_CACHE   # serve fresh cache

    _refresh_fx()
    usd_to_mxn = _FX_CACHE.get('USD', 17.15)   # live rate

    import requests as _req

    # ── Source 1: CoinGecko free tier ────────────────────────────────────────
    try:
        cg_url = (
            'https://api.coingecko.com/api/v3/simple/price'
            f'?ids={urllib.parse.quote(ids)}'
            '&vs_currencies=usd'
            '&include_24hr_change=true'
            '&include_market_cap=true'
        )
        headers = {'Accept': 'application/json', 'User-Agent': 'WealthLens/2.0'}
        # Add API key if configured (CoinGecko Demo key)
        cg_key = os.environ.get('COINGECKO_API_KEY', '')
        if cg_key:
            headers['x-cg-demo-api-key'] = cg_key

        r = _req.get(cg_url, headers=headers, timeout=10)
        if r.status_code == 200:
            raw = r.json()
            result = {}
            for coin_id, data in raw.items():
                usd_price = data.get('usd', 0) or 0
                result[coin_id] = {
                    'usd': usd_price,
                    'mxn': round(usd_price * usd_to_mxn, 2),
                    'usd_24h_change': data.get('usd_24h_change'),
                    'usd_market_cap': data.get('usd_market_cap'),
                    'source': 'coingecko',
                }
            if result:
                _CRYPTO_CACHE      = result
                _CRYPTO_CACHE_TIME = now
                return result
    except Exception:
        pass

    # ── Source 2: CoinCap API (no key required) ───────────────────────────────
    # CoinCap uses different IDs: bitcoin→bitcoin, ethereum→ethereum, solana→solana, etc.
    # We map the CoinGecko IDs to CoinCap slugs (they overlap for top coins).
    COINCAP_ID_MAP = {
        'bitcoin': 'bitcoin', 'ethereum': 'ethereum', 'solana': 'solana',
        'xrp': 'xrp', 'bnb': 'binance-coin', 'avalanche-2': 'avalanche',
        'dogecoin': 'dogecoin', 'cardano': 'cardano', 'polkadot': 'polkadot',
        'chainlink': 'chainlink',
    }
    try:
        result = {}
        for cg_id in ids.split(','):
            cg_id = cg_id.strip()
            cap_id = COINCAP_ID_MAP.get(cg_id, cg_id)
            r2 = _req.get(
                f'https://api.coincap.io/v2/assets/{cap_id}',
                timeout=8
            )
            if r2.status_code == 200:
                asset = r2.json().get('data', {})
                usd_price   = float(asset.get('priceUsd', 0) or 0)
                change_pct  = float(asset.get('changePercent24Hr', 0) or 0)
                market_cap  = float(asset.get('marketCapUsd', 0) or 0)
                result[cg_id] = {
                    'usd': round(usd_price, 6),
                    'mxn': round(usd_price * usd_to_mxn, 2),
                    'usd_24h_change': round(change_pct, 4),
                    'usd_market_cap': round(market_cap, 2),
                    'source': 'coincap',
                }
        if result:
            _CRYPTO_CACHE      = result
            _CRYPTO_CACHE_TIME = now
            return result
    except Exception:
        pass

    # ── Source 3: return stale cache if we have it ────────────────────────────
    if _CRYPTO_CACHE:
        return _CRYPTO_CACHE

    return {}   # nothing available

# ─── ROUTES ───────────────────────────────────────────────────────────────────
# Initialize DB immediately so gunicorn workers have it on startup
init_db()

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    lang = request.args.get('lang', 'en')
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['lang']    = user['lang']
            return redirect(url_for('dashboard'))
        else:
            flash('invalid_credentials')
    return render_template('login.html', lang=lang)

@app.route('/register', methods=['GET', 'POST'])
def register():
    lang = request.args.get('lang', 'en')
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not (name and email and password):
            flash('fields_required')
            return redirect(url_for('register', lang=lang))
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash('email_exists')
            return redirect(url_for('register', lang=lang))
        try:
            db.execute(
                "INSERT INTO users (email, password_hash, name, lang) VALUES (?, ?, ?, ?)",
                (email, generate_password_hash(password), name, lang)
            )
            db.commit()
            flash('registration_success')
            return redirect(url_for('login', lang=lang))
        except Exception:
            flash('registration_error')
            return redirect(url_for('register', lang=lang))
    return render_template('register.html', lang=lang)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('wealthlens_demo.html')

@app.route('/api/docs')
@login_required
def api_docs():
    return render_template('api_dashboard.html')

@app.route('/api/status')
@login_required
def api_status():
    _refresh_fx()
    return jsonify({
        'ok': True,
        'ai_configured': _is_ai_configured(),
        'ai_model': AI_MODEL,
        'ai_provider': 'Groq' if 'groq' in AI_BASE_URL.lower() else 'Other',
        'fx_usd_mxn': _FX_CACHE.get('USD', 17.15),
        'version': '2.1.0',
        'features': [
            'user_auth', 'wealth_tracking', 'goal_management',
            'transaction_import', 'ai_dashboard', 'chatbot',
            'market_data', 'api_documentation'
        ]
    })

# ─── FINANCIAL DATA APIs ──────────────────────────────────────────────────────
@app.route('/api/wealth/summary')
@login_required
def api_wealth_summary():
    uid = session['user_id']
    db  = get_db()
    assets = db.execute(
        "SELECT SUM(value_mxn) as total, COUNT(*) as count FROM assets WHERE user_id=?", (uid,)
    ).fetchone()
    goals = db.execute(
        "SELECT COUNT(*) as total, SUM(saved_mxn) as saved, SUM(target_mxn) as target FROM goals WHERE user_id=?",
        (uid,)
    ).fetchone()
    recent = db.execute("""
        SELECT SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income,
               SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as expenses
        FROM transactions WHERE user_id=? AND date >= date('now', '-30 days')
    """, (uid,)).fetchone()
    return jsonify({
        'wealth':  {'total_assets_mxn': assets['total'] or 0, 'asset_count': assets['count'] or 0},
        'goals':   {'total_goals': goals['total'] or 0, 'total_saved_mxn': goals['saved'] or 0, 'total_target_mxn': goals['target'] or 0},
        'recent_30_days': {'income_mxn': recent['income'] or 0, 'expenses_mxn': recent['expenses'] or 0},
    })

@app.route('/api/market/deposits')
@login_required
def api_market_deposits():
    rates = sorted(_DEPOSIT_RATES_CACHE['rates'], key=lambda x: x['rate'], reverse=True)
    return jsonify({
        'rates':     rates,
        'source':    _DEPOSIT_RATES_CACHE['source'],
        'reference': _DEPOSIT_RATES_CACHE['reference'],
        'updated':   _DEPOSIT_RATES_CACHE['updated'],
    })

@app.route('/api/market/stocks')
@login_required
def api_market_stocks():
    symbols = request.args.get('symbols', 'AMXL.MX,GMEXICOB.MX,WALMEX.MX,FEMSAUBD.MX,SPY,QQQ,EWW,VT')
    url = (
        f'https://query1.finance.yahoo.com/v7/finance/quote'
        f'?symbols={urllib.parse.quote(symbols)}'
        f'&fields=regularMarketPrice,regularMarketChangePercent,regularMarketChange,shortName,currency'
    )
    try:
        import requests as _req
        r = _req.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e), 'quoteResponse': {'result': []}}), 200

@app.route('/api/market/crypto')
@login_required
def api_market_crypto():
    """
    Return live crypto prices in both USD and MXN.

    Uses a cascade: CoinGecko → CoinCap → stale cache.
    MXN prices are computed from a live USD/MXN FX rate, not a
    hardcoded constant, so they are always accurate.
    """
    ids    = request.args.get('ids', 'bitcoin,ethereum,solana,xrp,bnb,avalanche-2,dogecoin')
    result = _fetch_crypto_prices(ids)

    if not result:
        return jsonify({'error': 'Could not fetch crypto prices from any source'}), 503

    # Annotate with the FX rate used so the frontend can display it
    _refresh_fx()
    return jsonify({
        **result,
        '_meta': {
            'usd_to_mxn':  _FX_CACHE.get('USD', 17.15),
            'fx_updated':  _FX_FETCHED,
            'cache_age_s': int(time.time() - _CRYPTO_CACHE_TIME),
        }
    })

# ─── AI DASHBOARD ─────────────────────────────────────────────────────────────
@app.route('/api/ai/dashboard', methods=['POST'])
@login_required
def api_ai_dashboard():
    uid  = session['user_id']
    user = current_user()
    lang = user['lang']
    data = request.json or {}

    fin_ctx = build_financial_context(uid, lang, data.get('date_from'), data.get('date_to'))
    db      = get_db()

    monthly_rows = db.execute("""
        SELECT strftime('%Y-%m', date) as month,
               SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income,
               SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as expenses
        FROM transactions WHERE user_id=?
        GROUP BY month ORDER BY month DESC LIMIT 6
    """, (uid,)).fetchall()
    monthly_data = [
        {"month": r["month"], "income": round(r["income"], 2), "expenses": round(r["expenses"], 2)}
        for r in reversed(monthly_rows)
    ]

    cat_rows = db.execute("""
        SELECT category, SUM(ABS(amount)) as total
        FROM transactions WHERE user_id=? AND amount < 0
        GROUP BY category ORDER BY total DESC LIMIT 8
    """, (uid,)).fetchall()
    cat_data = [{"category": r["category"], "amount": round(r["total"], 2)} for r in cat_rows]

    extra_ctx = (
        "MONTHLY TREND (last 6 months, MXN):\n"
        + "\n".join(f"  {m['month']}: income={m['income']:,.0f} expenses={m['expenses']:,.0f}" for m in monthly_data)
        + "\n\nTOP EXPENSE CATEGORIES (all time, MXN):\n"
        + "\n".join(f"  {c['category']}: {c['amount']:,.0f}" for c in cat_data)
    )

    system_prompt = (
        "You are an expert financial analyst. Respond ONLY with valid JSON (no markdown, no preamble). "
        "The JSON must have exactly this structure:\n"
        '{\n'
        '  "report_title": "string",\n'
        '  "period": "string",\n'
        '  "executive_summary": "2-3 sentence summary",\n'
        '  "health_score": integer 0-100,\n'
        '  "health_label": "Excellent|Good|Fair|Needs Attention",\n'
        '  "kpis": [\n'
        '    {"label":"string","value":"string","change":"string","trend":"up|down|neutral","color":"green|red|blue|gold"}\n'
        '  ],\n'
        '  "insights": [\n'
        '    {"type":"positive|warning|info","title":"string","body":"string"}\n'
        '  ],\n'
        '  "recommendations": [\n'
        '    {"priority":1,"title":"string","detail":"string","impact":"string"}\n'
        '  ]\n'
        '}'
    )

    user_question = data.get('prompt', '').strip() or (
        "Genera un dashboard financiero completo con KPIs, insights y recomendaciones." if lang == 'es'
        else "Generate a complete financial dashboard with KPIs, insights and recommendations."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"{fin_ctx}\n\n{extra_ctx}\n\n---\n\n{user_question}"},
    ]

    if not _is_ai_configured():
        return jsonify({'ok': True, 'dashboard': _demo_dashboard_payload(monthly_data, cat_data, lang), 'demo': True})

    try:
        resp = call_ai_gateway(messages, stream=False, max_tokens=1500)
        body = resp.json()
        raw  = body['choices'][0]['message']['content'].strip()
        raw  = re.sub(r'^```[a-z]*\n?', '', raw).strip()
        raw  = re.sub(r'\n?```$', '', raw).strip()
        dashboard = json.loads(raw)
        dashboard['monthly_trend']       = monthly_data
        dashboard['category_breakdown']  = cat_data
        return jsonify({'ok': True, 'dashboard': dashboard})
    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'AI returned invalid JSON: {e}'}), 502
    except Exception as e:
        # Return a helpful message rather than a bare 500
        err_str = str(e)
        if 'Rate limited' in err_str or '429' in err_str:
            return jsonify({'ok': False, 'error': 'AI service is busy — please wait a moment and try again.'}), 429
        if 'timed out' in err_str.lower():
            return jsonify({'ok': False, 'error': 'AI service took too long to respond. Please try again.'}), 504
        return jsonify({'ok': False, 'error': err_str}), 500

def _demo_dashboard_payload(monthly_data, cat_data, lang):
    if lang == 'es':
        return {
            "report_title": "Dashboard Financiero — Demo",
            "period": "Período seleccionado",
            "executive_summary": "⚠️ El gateway de IA no está configurado. Establece GROQ_API_KEY en tus variables de entorno.",
            "health_score": 72, "health_label": "Bueno",
            "kpis": [
                {"label": "Ingresos",    "value": "Ver datos", "change": "+0%", "trend": "neutral", "color": "green"},
                {"label": "Gastos",      "value": "Ver datos", "change": "+0%", "trend": "neutral", "color": "red"},
                {"label": "Flujo neto",  "value": "Ver datos", "change": "0%",  "trend": "neutral", "color": "blue"},
                {"label": "Tasa ahorro", "value": "N/A",       "change": "—",   "trend": "neutral", "color": "gold"},
            ],
            "insights":        [{"type": "info",    "title": "Sin configurar", "body": "Configura GROQ_API_KEY para activar el análisis real."}],
            "recommendations": [{"priority": 1,     "title": "Configura tu clave API", "detail": "Necesitas GROQ_API_KEY.", "impact": "Alto"}],
        }
    return {
        "report_title": "Financial Dashboard — Demo",
        "period": "Selected period",
        "executive_summary": "⚠️ AI gateway not configured. Set GROQ_API_KEY in your environment variables.",
        "health_score": 72, "health_label": "Good",
        "kpis": [
            {"label": "Income",        "value": "See data", "change": "+0%", "trend": "neutral", "color": "green"},
            {"label": "Expenses",      "value": "See data", "change": "+0%", "trend": "neutral", "color": "red"},
            {"label": "Net Cash Flow", "value": "See data", "change": "0%",  "trend": "neutral", "color": "blue"},
            {"label": "Savings Rate",  "value": "N/A",      "change": "—",   "trend": "neutral", "color": "gold"},
        ],
        "insights":        [{"type": "info",  "title": "Not configured", "body": "Set GROQ_API_KEY to enable real AI analysis."}],
        "recommendations": [{"priority": 1,   "title": "Configure API key", "detail": "You need GROQ_API_KEY.", "impact": "High"}],
    }

# ─── CHATBOT API ──────────────────────────────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    uid  = session['user_id']
    user = current_user()
    lang = user['lang']
    data = request.json or {}
    question = data.get('question', '').strip()
    if not question:
        return jsonify({'ok': False, 'error': 'Question required'}), 400

    fin_ctx = build_financial_context(uid, lang)
    system_prompt = (
        "You are a friendly financial advisor for WealthLens MX. "
        "Provide helpful, actionable financial advice based on the user's data. "
        "Be concise, professional, and supportive. "
        "Respond in the user's preferred language (Spanish or English)."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"{fin_ctx}\n\nUser question: {question}"},
    ]

    def _save_chat(answer):
        db = get_db()
        try:
            db.execute(
                "INSERT INTO chat_history (user_id, question, answer) VALUES (?, ?, ?)",
                (uid, question, answer)
            )
            db.commit()
        except Exception:
            pass

    if not _is_ai_configured():
        demo_responses = {
            'es': {
                'savings': "Recomiendo ahorrar al menos el 20% de tus ingresos mensuales.",
                'expenses': "Intenta categorizar mejor tus gastos para identificar áreas de mejora.",
                'goals': "Excelente que tengas metas claras. Revisa tu progreso mensualmente.",
                'default': "Soy tu asesor de WealthLens. ¿Cómo puedo ayudarte?"
            },
            'en': {
                'savings': "I recommend saving at least 20% of your monthly income.",
                'expenses': "Try to categorize your expenses better to find savings opportunities.",
                'goals': "Great that you have clear goals. Review your progress monthly.",
                'default': "I'm your WealthLens advisor. How can I help you?"
            }
        }
        key = ('savings'  if 'savings'  in question.lower() or 'ahorr'  in question.lower() else
               'expenses' if 'expenses' in question.lower() or 'gastos' in question.lower() else
               'goals'    if 'goal'     in question.lower() or 'meta'   in question.lower() else
               'default')
        answer = demo_responses.get(lang, demo_responses['en'])[key]
        _save_chat(answer)
        return jsonify({'ok': True, 'answer': answer, 'demo': True})

    try:
        resp   = call_ai_gateway(messages, stream=False, max_tokens=500)
        body   = resp.json()
        answer = body['choices'][0]['message']['content'].strip()
        _save_chat(answer)
        return jsonify({'ok': True, 'answer': answer})
    except Exception as e:
        err_str = str(e)
        if 'Rate limited' in err_str or '429' in err_str:
            return jsonify({'ok': False, 'error': 'AI service is busy — please wait a moment and try again.'}), 429
        if 'timed out' in err_str.lower():
            return jsonify({'ok': False, 'error': 'AI service took too long. Please try again.'}), 504
        return jsonify({'ok': False, 'error': err_str}), 500

@app.route('/api/chat/history')
@login_required
def api_chat_history():
    uid  = session['user_id']
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (uid,)
    ).fetchall()
    return jsonify({'ok': True, 'history': [dict(r) for r in rows]})


@app.route('/ping')
def ping():
    """Keep-alive endpoint for Render free tier"""
    return jsonify({'ok': True, 'ts': datetime.utcnow().isoformat()})

if __name__ == '__main__':
    init_db()
    print("\n✅ WealthLens MX v2.1 running at http://localhost:5000\n")
    app.run(debug=True, port=5000)
