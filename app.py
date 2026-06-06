import os, sqlite3, json, csv, io, re, urllib.request, urllib.error, urllib.parse
from datetime import datetime, date
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g, Response, stream_with_context)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ── Load .env FIRST ───────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ── AI CONFIGURATION ──────────────────────────────────────────────────────────
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'https://api.groq.com/openai/v1/chat/completions')
AI_API_KEY  = os.environ.get('GROQ_API_KEY', '')
AI_MODEL    = os.environ.get('AI_MODEL', 'llama-3.3-70b-versatile')

app = Flask(__name__)
# SECRET_KEY: use env var, fall back to a default only for local dev
app.secret_key = os.environ.get('SECRET_KEY', 'wealthlens-local-dev-secret-change-in-prod')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

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

# ── Run init_db at startup — works with gunicorn AND python app.py ────────────
init_db()

# ─── Auth ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            # API routes get JSON, page routes get redirect
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Not authenticated', 'redirect': '/login'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()

def get_current_user_safe():
    """Returns user or clears bad session and returns None."""
    user = current_user()
    if user is None and 'user_id' in session:
        session.clear()
    return user

# ─── FX ───────────────────────────────────────────────────────────────────────
FX      = {'MXN': 1, 'USD': 17.15, 'EUR': 18.60, 'CAD': 12.60, 'GBP': 21.70}
SYMBOLS = {'MXN': '$', 'USD': 'US$', 'EUR': '€', 'CAD': 'CA$', 'GBP': '£'}

def mxn_to(amount_mxn, currency):
    return amount_mxn / FX.get(currency, 1)

def to_mxn(amount, currency):
    return amount * FX.get(currency, 1)

# ─── AI UTILITIES ─────────────────────────────────────────────────────────────
def _is_ai_configured():
    return bool(AI_API_KEY and AI_API_KEY.strip())

def call_ai_gateway(messages, stream=False, max_tokens=1000):
    if not _is_ai_configured():
        raise ValueError("AI API key not configured. Set GROQ_API_KEY in your Render environment variables.")
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
    resp = requests.post(AI_BASE_URL, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp

# ─── CATEGORY / IMPORT PARSERS ────────────────────────────────────────────────
def detect_bank(filename, text_sample):
    fn = filename.lower(); sample = text_sample.lower()
    if 'bbva' in fn or 'bancomer' in fn or 'bbva' in sample:      return 'bbva'
    if 'banamex' in fn or 'citibanamex' in fn:                     return 'banamex'
    if 'santander' in fn or 'santander' in sample:                 return 'santander'
    if 'banorte' in fn or 'banorte' in sample:                     return 'banorte'
    if 'hsbc' in fn or 'hsbc' in sample:                           return 'hsbc'
    if 'scotiabank' in fn or 'scotiabank' in sample:               return 'scotiabank'
    if 'inbursa' in fn or 'inbursa' in sample:                     return 'inbursa'
    if 'american' in fn or 'amex' in fn:                           return 'amex'
    return 'generic'

def parse_csv_transactions(data, bank='generic', user_id=None):
    try:
        f = io.StringIO(data)
        reader = csv.DictReader(f)
        transactions = []
        for row in reader:
            try:
                if bank == 'bbva':
                    date_str = row.get('Fecha', '').strip()
                    desc     = row.get('Concepto', '').strip()
                    amount   = float(row.get('Monto', 0))
                elif bank == 'santander':
                    date_str = row.get('Fecha de operacion', '').strip()
                    desc     = row.get('Descripcion', '').strip()
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
        'groceries':    ['supermarket','grocery','costco','walmart','soriana','chedraui'],
        'dining':       ['restaurant','cafe','coffee','pizza','burger','taco','comida'],
        'transport':    ['uber','taxi','gasolina','fuel','transporte','transit'],
        'utilities':    ['electricity','agua','water','internet','telefonica','luz'],
        'entertainment':['cinema','movie','spotify','netflix','gaming','entertainment'],
        'health':       ['pharmacy','doctor','hospital','medicine','farmacia','salud'],
        'shopping':     ['mall','store','amazon','mercadolibre','tienda'],
    }
    for category, keywords in categories.items():
        if any(kw in desc for kw in keywords):
            return category
    return 'other'

def build_financial_context(user_id, lang, date_from=None, date_to=None):
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return "No user data available."

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

    total_assets   = assets['total'] or 0
    goals_count    = goals['count'] or 0
    goals_remaining= goals['remaining'] or 0
    income         = recent['income'] or 0
    expenses       = recent['expenses'] or 0

    if lang == 'es':
        return f"""
Tu nombre: {user['name']}
Moneda preferida: {user['currency']}

PATRIMONIO:
Total de activos: ${total_assets:,.0f} MXN

METAS:
Metas activas: {goals_count}
Dinero aun por ahorrar: ${goals_remaining:,.0f} MXN

ULTIMOS 30 DIAS:
Ingresos: ${income:,.0f} MXN
Gastos: ${expenses:,.0f} MXN
Flujo neto: ${(income - expenses):,.0f} MXN
"""
    return f"""
Your name: {user['name']}
Preferred currency: {user['currency']}

WEALTH:
Total assets: ${total_assets:,.0f} MXN

GOALS:
Active goals: {goals_count}
Remaining to save: ${goals_remaining:,.0f} MXN

LAST 30 DAYS:
Income: ${income:,.0f} MXN
Expenses: ${expenses:,.0f} MXN
Net cash flow: ${(income - expenses):,.0f} MXN
"""

# ─── CEDE RATES (static — updated periodically) ───────────────────────────────
_DEPOSIT_RATES_CACHE = {
    'rates': [
        {'bank': 'Banregio',      'rate': 12.10, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'Inbursa',       'rate': 11.80, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'BBVA Mexico',   'rate': 11.50, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'Scotiabank MX', 'rate': 11.25, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'Banbajio',      'rate': 11.20, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'Citibanamex',   'rate': 11.00, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'Banorte',       'rate': 10.75, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'Santander MX',  'rate': 10.50, 'term': '28 dias', 'type': 'CEDE'},
        {'bank': 'HSBC Mexico',   'rate': 10.50, 'term': '28 dias', 'type': 'DPF'},
    ],
    'source':    'static_fallback',
    'reference': 'Banxico TIIE ~10.5% base rate (Apr 2026). GAT approximate.',
    'updated':   '2026-04',
}

# ─── CRYPTO FALLBACK (used when CoinGecko is unavailable) ────────────────────
_CRYPTO_FALLBACK = {
    "bitcoin":     {"usd": 105000, "mxn": 1800750, "usd_24h_change": 0.0, "_fallback": True},
    "ethereum":    {"usd": 3800,   "mxn": 65170,   "usd_24h_change": 0.0, "_fallback": True},
    "solana":      {"usd": 185,    "mxn": 3173,    "usd_24h_change": 0.0, "_fallback": True},
    "xrp":         {"usd": 0.60,   "mxn": 10.29,   "usd_24h_change": 0.0, "_fallback": True},
    "bnb":         {"usd": 650,    "mxn": 11148,   "usd_24h_change": 0.0, "_fallback": True},
    "avalanche-2": {"usd": 40,     "mxn": 686,     "usd_24h_change": 0.0, "_fallback": True},
    "dogecoin":    {"usd": 0.18,   "mxn": 3.09,    "usd_24h_change": 0.0, "_fallback": True},
}

# ─── ROUTES ───────────────────────────────────────────────────────────────────
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
        db       = get_db()
        existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
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

# ─── DEMO ROUTE — auto-login with seeded data (great for portfolio showcase) ──
@app.route('/demo')
def demo_login():
    """One-click demo: creates a demo account + seeds data, then logs you in."""
    DEMO_EMAIL    = 'demo@wealthlens.mx'
    DEMO_PASSWORD = 'WealthLens2026!'
    DEMO_NAME     = 'Demo Investor'

    db       = get_db()
    existing = db.execute("SELECT * FROM users WHERE email=?", (DEMO_EMAIL,)).fetchone()
    if not existing:
        db.execute(
            "INSERT INTO users (email, password_hash, name, lang, currency) VALUES (?,?,?,?,?)",
            (DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD), DEMO_NAME, 'en', 'MXN')
        )
        db.commit()
        existing = db.execute("SELECT * FROM users WHERE email=?", (DEMO_EMAIL,)).fetchone()

    asset_count = db.execute(
        "SELECT COUNT(*) as c FROM assets WHERE user_id=?", (existing['id'],)
    ).fetchone()['c']

    if asset_count == 0:
        demo_assets = [
            (existing['id'], 'CEDE Banregio 28d',        'cede',   150000, 'MXN', '12.10% GAT'),
            (existing['id'], 'AMXL.MX — America Movil',  'stock',   85000, 'MXN', '340 acciones'),
            (existing['id'], 'Bitcoin (BTC)',             'crypto',  62000, 'MXN', '0.035 BTC'),
            (existing['id'], 'Ethereum (ETH)',            'crypto',  28000, 'MXN', '0.5 ETH'),
            (existing['id'], 'FIBRA UNO (FUNO11)',        'fibra',   45000, 'MXN', '1,500 certificados'),
            (existing['id'], 'Fondo de Emergencia',       'cash',    30000, 'MXN', '3 meses gastos'),
        ]
        db.executemany(
            "INSERT INTO assets (user_id,name,type,value_mxn,currency,note) VALUES (?,?,?,?,?,?)",
            demo_assets
        )
        demo_goals = [
            (existing['id'], 'Retirement Fund',    'Fondo de Retiro', 2000000, 420000, '2045-12-31', 1, '#1D9E75'),
            (existing['id'], 'Trip to Japan',      'Viaje a Japon',    80000,  35000, '2026-12-31', 2, '#378ADD'),
            (existing['id'], 'House Down Payment', 'Enganche Casa',   500000, 150000, '2028-06-30', 1, '#EF9F27'),
        ]
        db.executemany(
            "INSERT INTO goals (user_id,name,name_es,target_mxn,saved_mxn,deadline,priority,color) VALUES (?,?,?,?,?,?,?,?)",
            demo_goals
        )
        categories    = ['groceries','dining','transport','utilities','entertainment','health','shopping']
        amounts_exp   = [-3200,-1800,-950,-2100,-650,-480,-1200,-890,-2400,-760]
        amounts_inc   = [28000,28000,5000,28000,28000,3500]
        demo_txns     = []
        for i in range(6):
            demo_txns.append((existing['id'], f'2026-0{i+1}-05', 'Monthly salary',
                              amounts_inc[i], 'income', 'manual', 'BBVA'))
        for i in range(20):
            month = (i % 6) + 1
            demo_txns.append((existing['id'], f'2026-0{month}-{10+(i%15)}',
                              f'{categories[i % len(categories)].title()} expense',
                              amounts_exp[i % len(amounts_exp)],
                              categories[i % len(categories)], 'manual', 'BBVA'))
        db.executemany(
            "INSERT INTO transactions (user_id,date,description,amount,category,source,account) VALUES (?,?,?,?,?,?,?)",
            demo_txns
        )
        db.commit()

    session['user_id'] = existing['id']
    session['lang']    = 'en'
    return redirect(url_for('dashboard'))

# ─── API STATUS ───────────────────────────────────────────────────────────────
@app.route('/api/status')
@login_required
def api_status():
    return jsonify({
        'ok': True,
        'ai_configured': _is_ai_configured(),
        'ai_model':      AI_MODEL,
        'ai_provider':   'Groq' if 'groq' in AI_BASE_URL.lower() else 'Other',
        'version':       '2.1.0',
        'features': [
            'user_auth', 'wealth_tracking', 'goal_management',
            'transaction_import', 'ai_dashboard', 'chatbot',
            'market_data', 'api_documentation', 'demo_route'
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
    goals  = db.execute(
        "SELECT COUNT(*) as total, SUM(saved_mxn) as saved, SUM(target_mxn) as target FROM goals WHERE user_id=?", (uid,)
    ).fetchone()
    recent = db.execute("""
        SELECT SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income,
               SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as expenses
        FROM transactions WHERE user_id=? AND date >= date('now', '-30 days')
    """, (uid,)).fetchone()

    return jsonify({
        'wealth': {
            'total_assets_mxn': assets['total'] or 0,
            'asset_count':      assets['count'] or 0
        },
        'goals': {
            'total_goals':      goals['total'] or 0,
            'total_saved_mxn':  goals['saved']  or 0,
            'total_target_mxn': goals['target'] or 0
        },
        'recent_30_days': {
            'income_mxn':   recent['income']   or 0,
            'expenses_mxn': recent['expenses'] or 0
        }
    })

@app.route('/api/market/deposits', methods=['GET'])
@login_required
def api_market_deposits():
    rates = sorted(_DEPOSIT_RATES_CACHE['rates'], key=lambda x: x['rate'], reverse=True)
    return jsonify({
        'rates':     rates,
        'source':    _DEPOSIT_RATES_CACHE['source'],
        'reference': _DEPOSIT_RATES_CACHE['reference'],
        'updated':   _DEPOSIT_RATES_CACHE['updated'],
    })

@app.route('/api/market/stocks', methods=['GET'])
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
        r = _req.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }, timeout=12)
        data = r.json()
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e), 'quoteResponse': {'result': []}}), 200

@app.route('/api/market/crypto', methods=['GET'])
@login_required
def api_market_crypto():
    """
    Fetch live crypto prices from CoinGecko.
    Falls back to static prices if the API is unavailable or rate-limited.
    Set COINGECKO_API_KEY in Render env vars for higher rate limits (free at coingecko.com/api).
    """
    ids = request.args.get('ids', 'bitcoin,ethereum,solana,xrp,bnb,avalanche-2,dogecoin')
    url = (
        f'https://api.coingecko.com/api/v3/simple/price'
        f'?ids={urllib.parse.quote(ids)}'
        f'&vs_currencies=usd,mxn'
        f'&include_24hr_change=true'
        f'&include_market_cap=true'
    )
    try:
        import requests as _req
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept':     'application/json',
        }
        cg_key = os.environ.get('COINGECKO_API_KEY', '').strip()
        if cg_key:
            headers['x-cg-demo-api-key'] = cg_key

        r = _req.get(url, headers=headers, timeout=15)

        if r.status_code == 429:
            # Rate-limited — return fallback with a flag so the UI can show a notice
            return jsonify({**_CRYPTO_FALLBACK, '_status': 'rate_limited'}), 200

        if r.status_code != 200:
            raise ValueError(f"CoinGecko returned HTTP {r.status_code}")

        data = r.json()
        if not data:
            raise ValueError("Empty response from CoinGecko")

        return jsonify(data)

    except Exception as e:
        # Return fallback static prices — the UI will still render, just with stale data
        return jsonify({**_CRYPTO_FALLBACK, '_status': 'fallback', '_error': str(e)}), 200

# ─── AI DASHBOARD ─────────────────────────────────────────────────────────────
@app.route('/api/ai/dashboard', methods=['POST'])
@login_required
def api_ai_dashboard():
    uid  = session['user_id']
    user = get_current_user_safe()
    if user is None:
        return jsonify({'ok': False, 'error': 'Session expired. Please log in again.', 'redirect': '/login'}), 401

    lang     = user['lang']
    data     = request.json or {}
    date_from= data.get('date_from')
    date_to  = data.get('date_to')
    prompt   = data.get('prompt', '').strip()

    fin_ctx = build_financial_context(uid, lang, date_from, date_to)
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
        "MONTHLY TREND (last 6 months, MXN):\n" +
        "\n".join(f"  {m['month']}: income={m['income']:,.0f}  expenses={m['expenses']:,.0f}" for m in monthly_data) +
        "\n\nTOP EXPENSE CATEGORIES (all time, MXN):\n" +
        "\n".join(f"  {c['category']}: {c['amount']:,.0f}" for c in cat_data)
    )

    system_prompt = (
        "You are an expert financial analyst. Respond ONLY with valid JSON (no markdown, no preamble). "
        "The JSON must have exactly this structure:\n"
        "{\n"
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
        "}"
    )

    user_question = prompt or (
        "Genera un dashboard financiero completo con KPIs, insights y recomendaciones." if lang == 'es'
        else "Generate a complete financial dashboard with KPIs, insights and recommendations."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"{fin_ctx}\n\n{extra_ctx}\n\n---\n\n{user_question}"},
    ]

    if not _is_ai_configured():
        demo_payload = _demo_dashboard_payload(monthly_data, cat_data, lang)
        return jsonify({'ok': True, 'dashboard': demo_payload, 'demo': True})

    try:
        resp = call_ai_gateway(messages, stream=False, max_tokens=1500)
        body = resp.json()
        raw  = body['choices'][0]['message']['content'].strip()
        raw  = re.sub(r'^```[a-z]*\n?', '', raw).strip()
        raw  = re.sub(r'\n?```$', '', raw).strip()
        dashboard = json.loads(raw)
        dashboard['monthly_trend']      = monthly_data
        dashboard['category_breakdown'] = cat_data
        return jsonify({'ok': True, 'dashboard': dashboard})
    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'AI returned invalid JSON: {e}'}), 502
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

def _demo_dashboard_payload(monthly_data, cat_data, lang):
    if lang == 'es':
        return {
            "report_title": "Dashboard Financiero — Demo",
            "period": "Periodo seleccionado",
            "executive_summary": "⚠️ El gateway de IA no esta configurado. Establece GROQ_API_KEY en las variables de entorno de Render para obtener analisis real.",
            "health_score": 72, "health_label": "Bueno",
            "kpis": [
                {"label": "Ingresos",     "value": "Ver datos", "change": "+0%", "trend": "neutral", "color": "green"},
                {"label": "Gastos",       "value": "Ver datos", "change": "+0%", "trend": "neutral", "color": "red"},
                {"label": "Flujo neto",   "value": "Ver datos", "change":  "0%", "trend": "neutral", "color": "blue"},
                {"label": "Tasa ahorro",  "value": "N/A",       "change":  "—",  "trend": "neutral", "color": "gold"},
            ],
            "insights": [{"type": "info", "title": "Sin configurar",
                          "body": "Abre Render > Environment y agrega GROQ_API_KEY para activar el analisis real."}],
            "recommendations": [{"priority": 1, "title": "Configura tu clave API",
                                  "detail": "Ve a console.groq.com, crea una clave gratuita y agregala en Render.",
                                  "impact": "Alto"}],
            "monthly_trend": monthly_data, "category_breakdown": cat_data,
        }
    return {
        "report_title": "Financial Dashboard — Demo Mode",
        "period": "Selected period",
        "executive_summary": "⚠️ AI gateway not configured. Set GROQ_API_KEY in Render environment variables to get real AI analysis.",
        "health_score": 72, "health_label": "Good",
        "kpis": [
            {"label": "Income",        "value": "See data", "change": "+0%", "trend": "neutral", "color": "green"},
            {"label": "Expenses",      "value": "See data", "change": "+0%", "trend": "neutral", "color": "red"},
            {"label": "Net Cash Flow", "value": "See data", "change":  "0%", "trend": "neutral", "color": "blue"},
            {"label": "Savings Rate",  "value": "N/A",      "change":  "—",  "trend": "neutral", "color": "gold"},
        ],
        "insights": [{"type": "info", "title": "Not configured",
                      "body": "Go to Render > Environment and add GROQ_API_KEY to enable real AI analysis."}],
        "recommendations": [{"priority": 1, "title": "Configure your API key",
                              "detail": "Visit console.groq.com, grab a free key, add it to Render env vars.",
                              "impact": "High"}],
        "monthly_trend": monthly_data, "category_breakdown": cat_data,
    }

# ─── CHAT API ─────────────────────────────────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    uid  = session['user_id']
    user = get_current_user_safe()

    # FIX: guard against stale session (user wiped by Render redeploy)
    if user is None:
        return jsonify({'ok': False, 'error': 'Session expired. Please log in again.', 'redirect': '/login'}), 401

    lang     = user['lang']
    data     = request.json or {}
    question = data.get('question', '').strip()

    if not question:
        return jsonify({'ok': False, 'error': 'Question required'}), 400

    fin_ctx = build_financial_context(uid, lang)

    system_prompt = (
        "You are a friendly, expert financial advisor for WealthLens MX — a personal finance app for Mexican investors. "
        "You have access to the user's real portfolio data. "
        "Give specific, actionable advice based on their actual numbers. "
        "Be concise (3-5 sentences max unless more detail is needed), professional, and supportive. "
        "Always respond in the user's preferred language (Spanish or English). "
        "When discussing Mexican finance, mention relevant products: CEDEs, Cetes, FIBRAs, SIC, SAT, IMSS/AFORE."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"{fin_ctx}\n\nUser question: {question}"},
    ]

    if not _is_ai_configured():
        # Smart demo responses — at least give useful canned advice
        q = question.lower()
        responses_en = {
            'savings':    "Aim to save at least 20% of your monthly income. With Mexican CEDEs currently offering ~12% GAT, even a 28-day CEDE at Banregio beats inflation. Your emergency fund should cover 3-6 months of expenses before investing further.",
            'crypto':     "Crypto is volatile — limit it to 5-10% of your total portfolio. Your BTC and ETH holdings look reasonable. Always keep the majority in stable assets like CEDEs or broad market ETFs.",
            'cede':       "CEDEs (Certificados de Deposito) are fixed-term deposits offered by Mexican banks. Banregio currently offers ~12.10% GAT. They're IPAB-insured up to 400,000 UDIs per bank, making them very safe for short-term savings.",
            'goal':       "You're making good progress on your goals! Review them monthly. For long-term goals like retirement, consider a mix of SIC-listed ETFs (SPY, QQQ via GBM or Bursanet) plus Afore top-up contributions.",
            'invest':     "For Mexican investors: (1) Max out CEDEs for cash you need in <1 year, (2) Use Cetes Directo for 28-91 day treasuries, (3) Invest long-term savings in diversified ETFs via a casa de bolsa. Diversify across asset classes.",
            'default':    "I'm your WealthLens AI advisor. I can help with savings strategies, CEDE comparisons, crypto allocation, goal planning, and Mexican tax-efficient investing. What would you like to explore?",
        }
        responses_es = {
            'savings':    "Busca ahorrar al menos el 20% de tus ingresos. Los CEDEs de Banregio al 12.10% GAT superan la inflacion actual. Asegurate de tener un fondo de emergencia de 3-6 meses antes de invertir mas.",
            'crypto':     "Las criptomonedas son volatiles — limitaas al 5-10% de tu portafolio. Tu BTC y ETH se ven razonables. Mantén la mayoria en activos estables como CEDEs o ETFs diversificados.",
            'cede':       "Los CEDEs son depositos a plazo fijo de bancos mexicanos. Banregio ofrece ~12.10% GAT. Estan protegidos por el IPAB hasta 400,000 UDIs por banco — muy seguros para ahorro de corto plazo.",
            'goal':       "Buen avance en tus metas! Revisalas mensualmente. Para metas de largo plazo como el retiro, considera ETFs en el SIC (SPY, QQQ via GBM) y aportaciones voluntarias al Afore.",
            'invest':     "Para inversionistas mexicanos: (1) CEDEs para dinero que necesitas en <1 ano, (2) Cetes Directo para plazos de 28-91 dias, (3) ETFs diversificados via casa de bolsa para largo plazo.",
            'default':    "Soy tu asesor de WealthLens. Puedo ayudarte con estrategias de ahorro, comparativas de CEDEs, asignacion de cripto, planeacion de metas e inversion eficiente en Mexico. ¿Que deseas explorar?",
        }

        resp_map = responses_en if lang != 'es' else responses_es
        if any(w in q for w in ['saving','ahorr','save']):            answer = resp_map['savings']
        elif any(w in q for w in ['crypto','bitcoin','eth','btc']):   answer = resp_map['crypto']
        elif any(w in q for w in ['cede','deposito','deposit']):      answer = resp_map['cede']
        elif any(w in q for w in ['goal','meta','retire','retiro']):   answer = resp_map['goal']
        elif any(w in q for w in ['invest','portafolio','portfolio']): answer = resp_map['invest']
        else:                                                          answer = resp_map['default']

        db = get_db()
        try:
            db.execute("INSERT INTO chat_history (user_id,question,answer) VALUES (?,?,?)", (uid,question,answer))
            db.commit()
        except Exception:
            pass
        return jsonify({'ok': True, 'answer': answer, 'demo': True})

    try:
        resp   = call_ai_gateway(messages, stream=False, max_tokens=600)
        body   = resp.json()
        answer = body['choices'][0]['message']['content'].strip()

        db = get_db()
        try:
            db.execute("INSERT INTO chat_history (user_id,question,answer) VALUES (?,?,?)", (uid,question,answer))
            db.commit()
        except Exception:
            pass

        return jsonify({'ok': True, 'answer': answer})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/chat/history')
@login_required
def api_chat_history():
    uid  = session['user_id']
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (uid,)
    ).fetchall()
    return jsonify({'ok': True, 'history': [dict(r) for r in rows]})

# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
@app.route('/ping')
def ping():
    return 'pong', 200

if __name__ == '__main__':
    print("\n✅ WealthLens MX running at http://localhost:5000\n")
    app.run(debug=True, port=5000)
