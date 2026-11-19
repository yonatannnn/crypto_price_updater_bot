"""Paper-trading simulator: maths, storage and formatting (no Telegram code here)."""
from datetime import datetime

# Maintenance margin rate used for the liquidation estimate (Binance-like for small positions)
MMR = 0.005

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,              -- 'long' | 'short'
    entry REAL NOT NULL,
    margin REAL NOT NULL,            -- USDT you put in
    leverage REAL NOT NULL DEFAULT 1,
    qty REAL NOT NULL,               -- coins = margin * leverage / entry
    tp REAL,
    sl REAL,
    liq REAL,
    target_profit REAL,              -- USDT
    target_price REAL,               -- price where target_profit is reached
    target_notified INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    close_price REAL,
    close_reason TEXT,               -- 'tp' | 'sl' | 'liq' | 'manual'
    pnl REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades (status, user_id);
"""

def init_db(db):
    db.executescript(SCHEMA)
    db.commit()

# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------
def calc_qty(entry, margin, leverage):
    return margin * leverage / entry

def calc_pnl(side, entry, qty, price):
    return qty * (price - entry) if side == "long" else qty * (entry - price)

def calc_liq(side, entry, leverage):
    """Isolated-margin liquidation estimate. None for 1x (can't be liquidated)."""
    if leverage <= 1:
        return None
    if side == "long":
        return entry * (1 - 1 / leverage + MMR)
    return entry * (1 + 1 / leverage - MMR)

def price_for_profit(side, entry, qty, profit):
    return entry + profit / qty if side == "long" else entry - profit / qty

def parse_target(text, margin):
    """'50' -> 50 USDT, '20%' -> 20% of margin. Returns USDT or raises ValueError."""
    t = text.strip().replace("$", "")
    value = margin * _to_float(t[:-1]) / 100 if t.endswith("%") else _to_float(t)
    if value <= 0:
        raise ValueError
    return value

def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        raise ValueError() from None

def parse_number(text):
    v = _to_float(text.strip().replace(",", "").replace("$", ""))
    if v <= 0:
        raise ValueError
    return v

def parse_leverage(text):
    v = _to_float(text.strip().lower().rstrip("x"))
    if not 1 <= v <= 125:
        raise ValueError
    return v

# ---------------------------------------------------------------------------
# Command parsing: /trade BTC long 100 10x entry 65000 tp 70000 sl 62000 target 50
# ---------------------------------------------------------------------------
SIDES = {"long": "long", "buy": "long", "l": "long", "short": "short", "sell": "short", "s": "short"}
KEYS = {"entry": "entry", "e": "entry", "at": "entry",
        "tp": "tp", "sl": "sl",
        "target": "target", "t": "target", "profit": "target",
        "margin": "margin", "m": "margin", "size": "margin",
        "lev": "leverage", "leverage": "leverage", "x": "leverage"}

def parse_trade_command(tokens):
    """tokens after '/trade'. Returns (raw_symbol, draft_dict) or raises ValueError(message)."""
    flat = []
    for tok in tokens:
        flat.extend(p for p in tok.replace("=", " ").split() if p)
    if len(flat) < 3:
        raise ValueError("Not enough details.")
    raw_symbol = flat[0]
    side = SIDES.get(flat[1].lower())
    if not side:
        raise ValueError("Side must be <b>long</b> or <b>short</b>.")
    draft = {"side": side, "leverage": 1.0}
    rest = flat[2:]
    i = 0
    try:
        while i < len(rest):
            tok = rest[i].lower()
            if tok in KEYS:
                key = KEYS[tok]
                if i + 1 >= len(rest):
                    raise ValueError(f"Missing value after <b>{tok}</b>.")
                val = rest[i + 1]
                if key == "leverage":
                    draft["leverage"] = parse_leverage(val)
                elif key == "target":
                    draft["target_raw"] = val
                else:
                    draft[key] = parse_number(val)
                i += 2
            elif tok.endswith("x") and tok[:-1].replace(".", "", 1).isdigit():
                draft["leverage"] = parse_leverage(tok)
                i += 1
            elif "margin" not in draft:
                draft["margin"] = parse_number(tok)
                i += 1
            else:
                raise ValueError(f"Don't understand <b>{rest[i]}</b>.")
    except ValueError as e:
        if str(e):
            raise
        raise ValueError(f"Invalid number near <b>{rest[i]}</b>.")
    if "margin" not in draft:
        raise ValueError("Please include the margin (USDT), e.g. <code>100</code>.")
    return raw_symbol, draft

# ---------------------------------------------------------------------------
# Validation and creation
# ---------------------------------------------------------------------------
def finalize_draft(draft):
    """Fill computed fields and return (errors, warnings). draft must have symbol/side/entry/margin/leverage."""
    errors, warnings = [], []
    side, entry, lev = draft["side"], draft["entry"], draft["leverage"]
    draft["qty"] = calc_qty(entry, draft["margin"], lev)
    draft["liq"] = calc_liq(side, entry, lev)
    tp, sl, liq = draft.get("tp"), draft.get("sl"), draft["liq"]

    if tp is not None:
        if side == "long" and tp <= entry:
            errors.append("For a LONG, TP must be above the entry.")
        if side == "short" and tp >= entry:
            errors.append("For a SHORT, TP must be below the entry.")
    if sl is not None:
        if side == "long" and sl >= entry:
            errors.append("For a LONG, SL must be below the entry.")
        if side == "short" and sl <= entry:
            errors.append("For a SHORT, SL must be above the entry.")
        if liq is not None and ((side == "long" and sl <= liq) or (side == "short" and sl >= liq)):
            warnings.append("⚠️ Your SL is beyond the liquidation price — you'd be liquidated first.")

    if draft.get("target_raw"):
        try:
            draft["target_profit"] = parse_target(draft["target_raw"], draft["margin"])
        except ValueError:
            errors.append("Profit target must be a number like <code>50</code> or <code>20%</code>.")
    if draft.get("target_profit"):
        tprice = price_for_profit(side, entry, draft["qty"], draft["target_profit"])
        if tprice <= 0:
            errors.append("That profit target is impossible for a SHORT (price would go below 0).")
        else:
            draft["target_price"] = tprice
    return errors, warnings

def insert_trade(db, user_id, d):
    cur = db.execute(
        "INSERT INTO trades (user_id, symbol, side, entry, margin, leverage, qty, tp, sl, liq, "
        "target_profit, target_price, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, d["symbol"], d["side"], d["entry"], d["margin"], d["leverage"], d["qty"],
         d.get("tp"), d.get("sl"), d.get("liq"), d.get("target_profit"), d.get("target_price"),
         datetime.utcnow().isoformat()),
    )
    db.commit()
    return cur.lastrowid

def close_trade(db, trade, price, reason):
    pnl = calc_pnl(trade["side"], trade["entry"], trade["qty"], price)
    if reason == "liq":
        pnl = -trade["margin"]   # isolated margin: you lose the whole margin
    db.execute(
        "UPDATE trades SET status='closed', close_price=?, close_reason=?, pnl=?, closed_at=? WHERE id=? AND status='open'",
        (price, reason, pnl, datetime.utcnow().isoformat(), trade["id"]),
    )
    db.commit()
    return pnl

def check_trade(trade, price):
    """What happens to an open trade at this price.
    Returns ('liq'|'sl'|'tp', level) to close, ('target', level) to notify, or None."""
    side = trade["side"]
    hit_down = lambda level: level is not None and price <= level
    hit_up = lambda level: level is not None and price >= level
    if side == "long":
        if hit_down(trade["liq"]):
            return ("liq", trade["liq"])
        if hit_down(trade["sl"]):
            return ("sl", trade["sl"])
        if hit_up(trade["tp"]):
            return ("tp", trade["tp"])
        if not trade["target_notified"] and hit_up(trade["target_price"]):
            return ("target", trade["target_price"])
    else:
        if hit_up(trade["liq"]):
            return ("liq", trade["liq"])
        if hit_up(trade["sl"]):
            return ("sl", trade["sl"])
        if hit_down(trade["tp"]):
            return ("tp", trade["tp"])
        if not trade["target_notified"] and hit_down(trade["target_price"]):
            return ("target", trade["target_price"])
    return None

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.10f}".rstrip("0").rstrip(".")

def fmt_usd(v):
    v = round(v, 2) + 0.0   # avoid "-$0.00"
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"

def fmt_lev(lev):
    return f"{lev:g}x"

def side_label(side):
    return "🟢 LONG" if side == "long" else "🔴 SHORT"

def trade_card(t, price=None, title=None):
    """HTML description of a trade (dict or sqlite Row). price = current market price for live PnL."""
    g = (lambda k: t[k]) if not isinstance(t, dict) else t.get
    lines = []
    if title:
        lines.append(title)
    head = f"<b>{g('symbol')}</b> {side_label(g('side'))} {fmt_lev(g('leverage'))}"
    if not isinstance(t, dict) or g("id"):
        head = f"#{g('id')} " + head
    lines.append(head)
    lines.append(f"Entry: <code>${fmt_price(g('entry'))}</code> · Margin: <code>${g('margin'):,.2f}</code>")
    lines.append(f"Position: <code>{g('qty'):.6g}</code> coins (<code>${g('margin') * g('leverage'):,.2f}</code>)")
    if g("tp"):
        tp_pnl = calc_pnl(g("side"), g("entry"), g("qty"), g("tp"))
        lines.append(f"🎯 TP: <code>${fmt_price(g('tp'))}</code> ({fmt_usd(tp_pnl)})")
    if g("sl"):
        sl_pnl = calc_pnl(g("side"), g("entry"), g("qty"), g("sl"))
        lines.append(f"🛑 SL: <code>${fmt_price(g('sl'))}</code> ({fmt_usd(sl_pnl)})")
    if g("target_profit"):
        lines.append(f"💰 Target {fmt_usd(g('target_profit'))} at <code>${fmt_price(g('target_price'))}</code>")
    if g("liq"):
        lines.append(f"💀 Liq. (est.): <code>${fmt_price(g('liq'))}</code>")
    if price is not None:
        pnl = calc_pnl(g("side"), g("entry"), g("qty"), price)
        roe = pnl / g("margin") * 100
        icon = "📈" if pnl >= 0 else "📉"
        lines.append(f"{icon} Now <code>${fmt_price(price)}</code> → PnL <b>{fmt_usd(pnl)}</b> ({roe:+.1f}%)")
    return "\n".join(lines)

CLOSE_TITLES = {
    "tp": "🎯 <b>Take profit hit!</b>",
    "sl": "🛑 <b>Stop loss hit</b>",
    "liq": "💀 <b>Liquidated</b>",
    "manual": "✋ <b>Trade closed</b>",
}

def close_message(trade, close_price, reason, pnl):
    roe = pnl / trade["margin"] * 100
    return (
        f"{CLOSE_TITLES[reason]}\n"
        f"#{trade['id']} <b>{trade['symbol']}</b> {side_label(trade['side'])} {fmt_lev(trade['leverage'])}\n"
        f"Entry <code>${fmt_price(trade['entry'])}</code> → Exit <code>${fmt_price(close_price)}</code>\n"
        f"PnL: <b>{fmt_usd(pnl)}</b> ({roe:+.1f}%)"
    )

def target_message(trade, price):
    pnl = calc_pnl(trade["side"], trade["entry"], trade["qty"], price)
    return (
        f"💰 <b>Profit target reached!</b>\n"
        f"#{trade['id']} <b>{trade['symbol']}</b> {side_label(trade['side'])} {fmt_lev(trade['leverage'])}\n"
        f"Price <code>${fmt_price(price)}</code> → PnL <b>{fmt_usd(pnl)}</b>\n"
        f"The trade is still open — close it from 📒 My trades if you want to take profit."
    )

def history_text(db, user_id, limit=10):
    rows = db.execute(
        "SELECT * FROM trades WHERE user_id=? AND status='closed' ORDER BY closed_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    stats = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) total, COALESCE(SUM(pnl>0),0) wins "
        "FROM trades WHERE user_id=? AND status='closed'", (user_id,),
    ).fetchone()
    if not rows:
        return "📜 No closed trades yet."
    icons = {"tp": "🎯", "sl": "🛑", "liq": "💀", "manual": "✋"}
    lines = ["📜 <b>Trade history</b> (last 10)\n"]
    for r in rows:
        lines.append(
            f"{icons.get(r['close_reason'], '•')} #{r['id']} {r['symbol']} {r['side'].upper()} "
            f"{fmt_lev(r['leverage'])}: <b>{fmt_usd(r['pnl'])}</b>"
        )
    win_rate = stats["wins"] / stats["n"] * 100
    lines.append(f"\nTrades: {stats['n']} · Win rate: {win_rate:.0f}% · Total PnL: <b>{fmt_usd(stats['total'])}</b>")
    return "\n".join(lines)
