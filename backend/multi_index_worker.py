"""
Multi-Index Worker — Phase 8.

Collecte toutes les 30min via yfinance :
  QQQ (Nasdaq 100), IWM (Russell 2000), DIA (Dow Jones),
  GLD (Gold), TLT (Long bonds)

Calcule la largeur du marché (market breadth) et la performance relative vs SPY.
Architecture Worker-Cache : UN seul fetch → cache → endpoint.
"""

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

MULTI_INDEX_INTERVAL = int(os.environ.get("MULTI_INDEX_INTERVAL", 1800))
DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

TICKERS = {
    "qqq":  "QQQ",   # Nasdaq 100 — tech
    "iwm":  "IWM",   # Russell 2000 — small caps / risk
    "dia":  "DIA",   # Dow Jones — industrials
    "gld":  "GLD",   # Gold — safe haven
    "tlt":  "TLT",   # Long bonds — risk-off gauge
}

_cache: dict = {t: None for t in TICKERS}
_cache["breadth"] = None
_cache["last_updated"] = None
_cache["error"] = None


def get_cache() -> dict:
    return dict(_cache)


def is_stale(max_age_seconds: int = 3600) -> bool:
    lu = _cache.get("last_updated")
    return lu is None or (time.time() - lu) > max_age_seconds


# ─────────────────────────── DB ──────────────────────────────────────────────

def init_multi_index_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS multi_index_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                ticker      TEXT    NOT NULL,
                price       REAL,
                change_1d   REAL,
                change_5d   REAL,
                change_1mo  REAL,
                dist_52w_high REAL,
                dist_52w_low  REAL,
                volume      REAL,
                rel_spy_1d  REAL,
                rel_spy_5d  REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_midx_ts ON multi_index_snapshots(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_midx_ticker ON multi_index_snapshots(ticker)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS multi_index_breadth (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                breadth_score   REAL,
                breadth_label   TEXT,
                n_up_1d         INTEGER,
                n_down_1d       INTEGER,
                strongest_1d    TEXT,
                weakest_1d      TEXT,
                risk_on_score   REAL,
                interpretation  TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_breadth_ts ON multi_index_breadth(ts)")


def _save_snapshots(ts: int, tickers_data: dict, breadth: dict) -> None:
    try:
        with sqlite3.connect(DB_PATH) as c:
            for ticker, data in tickers_data.items():
                if data is None:
                    continue
                c.execute("""
                    INSERT INTO multi_index_snapshots
                    (ts, ticker, price, change_1d, change_5d, change_1mo,
                     dist_52w_high, dist_52w_low, volume, rel_spy_1d, rel_spy_5d)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ts, ticker,
                    data.get("price"), data.get("change_1d"), data.get("change_5d"),
                    data.get("change_1mo"), data.get("dist_52w_high"), data.get("dist_52w_low"),
                    data.get("volume"), data.get("rel_spy_1d"), data.get("rel_spy_5d"),
                ))
            if breadth:
                c.execute("""
                    INSERT INTO multi_index_breadth
                    (ts, breadth_score, breadth_label, n_up_1d, n_down_1d,
                     strongest_1d, weakest_1d, risk_on_score, interpretation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ts,
                    breadth.get("score"), breadth.get("label"),
                    breadth.get("n_up_1d"), breadth.get("n_down_1d"),
                    breadth.get("strongest_1d"), breadth.get("weakest_1d"),
                    breadth.get("risk_on_score"), breadth.get("interpretation"),
                ))
    except Exception as e:
        log.error(f"[multi_index] DB save error: {e}")


def get_multi_index_history(ticker: str = "qqq", limit: int = 100) -> list:
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM multi_index_snapshots WHERE ticker=? ORDER BY ts DESC LIMIT ?",
                (ticker, limit)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# ─────────────────────────── yfinance fetch ──────────────────────────────────

def _fetch_yfinance_sync(spy_change_1d: Optional[float], spy_change_5d: Optional[float]) -> Dict[str, Any]:
    try:
        import yfinance as yf
        import numpy as np
    except ImportError:
        log.error("[multi_index] yfinance not installed")
        return {}

    results = {}
    for key, symbol in TICKERS.items():
        try:
            hist = yf.Ticker(symbol).history(period="1y")
            if hist.empty:
                results[key] = None
                continue

            closes = hist["Close"]
            price = float(closes.iloc[-1])
            h52 = float(hist["High"].max())
            l52 = float(hist["Low"].min())

            data: Dict[str, Any] = {
                "price": round(price, 2),
                "volume": float(hist["Volume"].iloc[-1]),
                "dist_52w_high": round((price - h52) / h52 * 100, 2) if h52 > 0 else None,
                "dist_52w_low":  round((price - l52) / l52 * 100, 2) if l52 > 0 else None,
            }

            if len(closes) >= 2:
                prev1 = float(closes.iloc[-2])
                data["change_1d"] = round((price - prev1) / prev1 * 100, 3)
            if len(closes) >= 6:
                prev5 = float(closes.iloc[-6])
                data["change_5d"] = round((price - prev5) / prev5 * 100, 3)
            if len(closes) >= 22:
                prev1mo = float(closes.iloc[-22])
                data["change_1mo"] = round((price - prev1mo) / prev1mo * 100, 3)

            # Performance relative vs SPY
            if spy_change_1d is not None and data.get("change_1d") is not None:
                data["rel_spy_1d"] = round(data["change_1d"] - spy_change_1d, 3)
            if spy_change_5d is not None and data.get("change_5d") is not None:
                data["rel_spy_5d"] = round(data["change_5d"] - spy_change_5d, 3)

            results[key] = data
        except Exception as e:
            log.warning(f"[multi_index] {symbol}: {e}")
            results[key] = None

    return results


def _compute_breadth(tickers_data: dict) -> dict:
    """Largeur du marché et score risk-on."""
    changes_1d = {k: v["change_1d"] for k, v in tickers_data.items()
                  if v and v.get("change_1d") is not None}
    if not changes_1d:
        return {}

    # Nombre de tickers en hausse/baisse sur 1j (hors GLD/TLT pour breadth actions)
    equity_tickers = {k: v for k, v in changes_1d.items() if k in ("qqq", "iwm", "dia")}
    n_up = sum(1 for v in equity_tickers.values() if v > 0)
    n_down = sum(1 for v in equity_tickers.values() if v < 0)
    n_equity = len(equity_tickers)

    if n_equity == 0:
        breadth_score = 50.0
        breadth_label = "NO_DATA"
    else:
        breadth_score = round(n_up / n_equity * 100, 1)
        if breadth_score >= 100:
            breadth_label = "BROAD_RALLY"
        elif breadth_score >= 67:
            breadth_label = "WIDE"
        elif breadth_score >= 34:
            breadth_label = "MIXED"
        elif breadth_score > 0:
            breadth_label = "NARROW"
        else:
            breadth_label = "BROAD_SELLOFF"

    # Strongest / weakest sur 1j (tous tickers confondus)
    if changes_1d:
        strongest = max(changes_1d, key=lambda k: changes_1d[k])
        weakest   = min(changes_1d, key=lambda k: changes_1d[k])
    else:
        strongest = weakest = None

    # Risk-on score : QQQ et IWM montent → risk on ; TLT monte, GLD monte → risk off
    risk_on = 50.0
    signals = []
    if "qqq" in changes_1d:
        signals.append(("qqq",  changes_1d["qqq"], +1.5))   # tech = risk on pondéré
    if "iwm" in changes_1d:
        signals.append(("iwm",  changes_1d["iwm"], +1.2))   # small caps = risk on
    if "tlt" in changes_1d:
        signals.append(("tlt", -changes_1d["tlt"], +1.0))   # bonds montent → risk off
    if "gld" in changes_1d:
        signals.append(("gld", -changes_1d["gld"], +0.5))   # gold monte → risk off

    if signals:
        total_w = sum(w for _, _, w in signals)
        # Normalise autour de 50 : chaque pct contribue ±w/total_w points
        delta = sum(chg * w for _, chg, w in signals) / total_w
        risk_on = max(0.0, min(100.0, round(50.0 + delta * 5, 1)))

    if risk_on >= 70:
        risk_label = "RISK_ON"
    elif risk_on >= 55:
        risk_label = "MILD_RISK_ON"
    elif risk_on >= 45:
        risk_label = "NEUTRAL"
    elif risk_on >= 30:
        risk_label = "MILD_RISK_OFF"
    else:
        risk_label = "RISK_OFF"

    # Phrase interprétation
    interp = _breadth_interpretation(breadth_label, risk_label, n_up, n_equity,
                                     equity_tickers.get("qqq"), equity_tickers.get("iwm"))

    return {
        "score": breadth_score,
        "label": breadth_label,
        "n_up_1d": n_up,
        "n_down_1d": n_down,
        "strongest_1d": strongest,
        "weakest_1d": weakest,
        "risk_on_score": risk_on,
        "risk_on_label": risk_label,
        "interpretation": interp,
    }


def _breadth_interpretation(breadth_label: str, risk_label: str,
                             n_up: int, n_equity: int,
                             qqq_chg: Optional[float], iwm_chg: Optional[float]) -> str:
    if breadth_label == "BROAD_RALLY":
        base = f"Hausse généralisée ({n_up}/{n_equity} indices actions en vert)."
    elif breadth_label == "WIDE":
        base = f"Majorité des indices en hausse ({n_up}/{n_equity})."
    elif breadth_label == "MIXED":
        base = f"Marché partagé — aucune direction dominante ({n_up}/{n_equity} en hausse)."
    elif breadth_label == "NARROW":
        base = f"Hausse très étroite ({n_up}/{n_equity}) — méfiance."
    elif breadth_label == "BROAD_SELLOFF":
        base = f"Baisse généralisée — tous les indices actions en rouge."
    else:
        return "Données insuffisantes."

    if risk_label in ("RISK_ON", "MILD_RISK_ON"):
        ext = " Le flux est orienté actions risquées."
    elif risk_label in ("RISK_OFF", "MILD_RISK_OFF"):
        ext = " Les investisseurs fuient vers les refuges (bonds/gold)."
    else:
        ext = ""

    # Divergence QQQ vs IWM (tech vs small caps)
    div = ""
    if qqq_chg is not None and iwm_chg is not None:
        diff = qqq_chg - iwm_chg
        if diff > 1.0:
            div = " Divergence tech vs small caps : la tech surperforme — attention aux fondamentaux."
        elif diff < -1.0:
            div = " Rotation : les small caps surperforment la tech — signal risk-on."

    return base + ext + div


# ─────────────────────────── Main async worker ───────────────────────────────

async def _collect_once(spy_cache: Optional[dict] = None) -> None:
    loop = asyncio.get_event_loop()

    spy_1d = spy_cache.get("spy_change_1d") if spy_cache else None
    # 5d SPY pas dans spy_worker cache, on approx via drawdown_5d
    spy_5d = spy_cache.get("spy_drawdown_5d") if spy_cache else None

    tickers_data = await loop.run_in_executor(
        None, lambda: _fetch_yfinance_sync(spy_1d, spy_5d)
    )

    breadth = _compute_breadth(tickers_data)

    ts = int(time.time())
    for key in TICKERS:
        _cache[key] = tickers_data.get(key)
    _cache["breadth"] = breadth
    _cache["last_updated"] = ts
    _cache["error"] = None

    _save_snapshots(ts, tickers_data, breadth)

    n_up = breadth.get("n_up_1d", "?")
    label = breadth.get("label", "?")
    risk = breadth.get("risk_on_label", "?")
    log.info(f"[multi_index] breadth={label} n_up={n_up} risk={risk} ts={ts}")


async def run_worker(spy_cache_fn=None) -> None:
    init_multi_index_db()
    await asyncio.sleep(45)  # décalé par rapport au spy_worker (15s)
    while True:
        try:
            spy_cache = spy_cache_fn() if spy_cache_fn else None
            await _collect_once(spy_cache)
        except Exception as e:
            _cache["error"] = str(e)
            log.error(f"[multi_index] error: {e}")
        await asyncio.sleep(MULTI_INDEX_INTERVAL)
