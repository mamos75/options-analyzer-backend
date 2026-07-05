"""
SPY Worker — Data Worker 100% gratuit.

Collecte toutes les 30min :
  - VIX / VIX9D / VIX3M / VIX6M / VVIX (Yahoo Finance)
  - SPY price / volume / drawdowns
  - SPY options PCR (volume + OI) via yfinance
  - CBOE equity PCR quotidien (best effort via aiohttp)
  - IV Rank calculé via range VIX 1an

Architecture Worker-Cache : UN seul fetch → cache → tous les endpoints lisent le cache.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

log = logging.getLogger(__name__)

SPY_WORKER_INTERVAL = int(os.environ.get("SPY_WORKER_INTERVAL", 1800))
DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

_CBOE_PCR_URL = "https://cdn.cboe.com/api/global/us_options_volume/options-volume.csv"
_TIMEOUT = aiohttp.ClientTimeout(total=20)

_cache: dict = {
    "spy_price": None,
    "spy_change_1d": None,
    "spy_drawdown_3d": None,
    "spy_drawdown_5d": None,
    "spy_dist_52w_high": None,
    "spy_volume": None,
    "vix": None,
    "vix9d": None,
    "vix3m": None,
    "vix6m": None,
    "vvix": None,
    "vix_change_1d": None,
    "vix_change_5d": None,
    "vix_percentile": None,
    "vix9d_vix_spread": None,
    "vix_vix3m_spread": None,
    "contango": None,
    "vol_of_vol": None,
    "pcr_equity": None,
    "pcr_index": None,
    "pcr_spy_volume": None,
    "pcr_spy_oi": None,
    "pcr_spy_near": None,
    "iv_rank": None,
    "iv_current": None,
    "vix_regime": None,
    "us_mopi": None,
    "us_mopi_label": None,
    "spy_regime": None,
    "prob_rebound_1d": None,
    "prob_rebound_3d": None,
    "prob_rebound_5d": None,
    "rebound_confidence": None,
    "rebound_factors": None,
    "last_updated": None,
    "error": None,
}


def get_cache() -> dict:
    return dict(_cache)


def is_stale(max_age_seconds: int = 3600) -> bool:
    lu = _cache.get("last_updated")
    return lu is None or (time.time() - lu) > max_age_seconds


# ─────────────────────────── DB ──────────────────────────────────────────────

def init_spy_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS spy_snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                INTEGER NOT NULL,
                spy_price         REAL,
                spy_change_1d     REAL,
                spy_drawdown_3d   REAL,
                spy_drawdown_5d   REAL,
                spy_dist_52w_high REAL,
                spy_volume        REAL,
                vix               REAL,
                vix9d             REAL,
                vix3m             REAL,
                vix6m             REAL,
                vvix              REAL,
                vix_change_1d     REAL,
                vix_change_5d     REAL,
                vix_percentile    REAL,
                vix9d_vix_spread  REAL,
                vix_vix3m_spread  REAL,
                contango          INTEGER,
                vol_of_vol        REAL,
                pcr_equity        REAL,
                pcr_index         REAL,
                pcr_spy_volume    REAL,
                pcr_spy_oi        REAL,
                pcr_spy_near      REAL,
                iv_rank           REAL,
                iv_current        REAL,
                vix_regime        TEXT,
                us_mopi           REAL,
                us_mopi_label     TEXT,
                spy_regime        TEXT,
                prob_rebound_1d   REAL,
                prob_rebound_3d   REAL,
                prob_rebound_5d   REAL,
                rebound_confidence TEXT,
                rebound_factors   TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_spy_ts ON spy_snapshots(ts)")


def save_spy_snapshot(data: dict) -> None:
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("""
                INSERT INTO spy_snapshots (
                    ts, spy_price, spy_change_1d, spy_drawdown_3d, spy_drawdown_5d,
                    spy_dist_52w_high, spy_volume,
                    vix, vix9d, vix3m, vix6m, vvix,
                    vix_change_1d, vix_change_5d, vix_percentile,
                    vix9d_vix_spread, vix_vix3m_spread, contango, vol_of_vol,
                    pcr_equity, pcr_index, pcr_spy_volume, pcr_spy_oi, pcr_spy_near,
                    iv_rank, iv_current,
                    vix_regime, us_mopi, us_mopi_label, spy_regime,
                    prob_rebound_1d, prob_rebound_3d, prob_rebound_5d,
                    rebound_confidence, rebound_factors
                ) VALUES (
                    :ts, :spy_price, :spy_change_1d, :spy_drawdown_3d, :spy_drawdown_5d,
                    :spy_dist_52w_high, :spy_volume,
                    :vix, :vix9d, :vix3m, :vix6m, :vvix,
                    :vix_change_1d, :vix_change_5d, :vix_percentile,
                    :vix9d_vix_spread, :vix_vix3m_spread, :contango, :vol_of_vol,
                    :pcr_equity, :pcr_index, :pcr_spy_volume, :pcr_spy_oi, :pcr_spy_near,
                    :iv_rank, :iv_current,
                    :vix_regime, :us_mopi, :us_mopi_label, :spy_regime,
                    :prob_rebound_1d, :prob_rebound_3d, :prob_rebound_5d,
                    :rebound_confidence, :rebound_factors
                )
            """, {k: data.get(k) for k in [
                "ts", "spy_price", "spy_change_1d", "spy_drawdown_3d", "spy_drawdown_5d",
                "spy_dist_52w_high", "spy_volume",
                "vix", "vix9d", "vix3m", "vix6m", "vvix",
                "vix_change_1d", "vix_change_5d", "vix_percentile",
                "vix9d_vix_spread", "vix_vix3m_spread", "contango", "vol_of_vol",
                "pcr_equity", "pcr_index", "pcr_spy_volume", "pcr_spy_oi", "pcr_spy_near",
                "iv_rank", "iv_current",
                "vix_regime", "us_mopi", "us_mopi_label", "spy_regime",
                "prob_rebound_1d", "prob_rebound_3d", "prob_rebound_5d",
                "rebound_confidence", "rebound_factors",
            ]})
    except Exception as e:
        log.error(f"[spy_worker] DB save error: {e}")


def get_spy_history(limit: int = 200) -> list:
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM spy_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# ─────────────────────────── yfinance fetch (sync → executor) ────────────────

def _fetch_yfinance_sync() -> Dict[str, Any]:
    try:
        import yfinance as yf
        import numpy as np
    except ImportError:
        log.error("[spy_worker] yfinance not installed")
        return {}

    result: Dict[str, Any] = {}

    # VIX série complète
    vix_tickers = {
        "vix": "^VIX",
        "vix9d": "^VIX9D",
        "vix3m": "^VIX3M",
        "vix6m": "^VIX6M",
        "vvix": "^VVIX",
    }
    for key, ticker in vix_tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period="6mo")
            if not hist.empty:
                result[key] = float(hist["Close"].iloc[-1])
                if len(hist) >= 2:
                    result[f"_{key}_prev1d"] = float(hist["Close"].iloc[-2])
                if len(hist) >= 6:
                    result[f"_{key}_prev5d"] = float(hist["Close"].iloc[-6])
        except Exception as e:
            log.warning(f"[spy_worker] {ticker}: {e}")
            result[key] = None

    # VIX percentile 1an
    try:
        vix_1y = yf.Ticker("^VIX").history(period="1y")["Close"]
        if len(vix_1y) > 50 and result.get("vix"):
            pct = float(np.mean(vix_1y <= result["vix"]) * 100)
            result["vix_percentile"] = round(pct, 1)
            result["iv_rank"] = round(
                (result["vix"] - float(vix_1y.min())) /
                max(float(vix_1y.max()) - float(vix_1y.min()), 0.01) * 100, 1
            )
    except Exception as e:
        log.warning(f"[spy_worker] VIX 1y: {e}")

    # SPY price + drawdowns
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="1y")
        if not hist.empty:
            closes = hist["Close"]
            result["spy_price"] = float(closes.iloc[-1])
            result["spy_volume"] = float(hist["Volume"].iloc[-1])
            if len(closes) >= 2:
                result["spy_change_1d"] = round(
                    (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100, 3
                )
            if len(closes) >= 4:
                result["spy_drawdown_3d"] = round(
                    (closes.iloc[-1] - closes.iloc[-4]) / closes.iloc[-4] * 100, 3
                )
            if len(closes) >= 6:
                result["spy_drawdown_5d"] = round(
                    (closes.iloc[-1] - closes.iloc[-6]) / closes.iloc[-6] * 100, 3
                )
            h52 = float(hist["High"].max())
            l52 = float(hist["Low"].min())
            result["spy_high_52w"] = h52
            result["spy_low_52w"] = l52
            if h52 > 0:
                result["spy_dist_52w_high"] = round(
                    (closes.iloc[-1] - h52) / h52 * 100, 2
                )

        # SPY options PCR — toutes expiries disponibles (max 8)
        exps = list(spy.options or [])[:8]
        today_dt = date.today()
        total_put_vol = total_call_vol = 0.0
        total_put_oi = total_call_oi = 0.0
        near_put_vol = near_call_vol = 0.0

        for exp in exps:
            try:
                chain = spy.option_chain(exp)
                exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = max(0, (exp_dt - today_dt).days)

                pv = float(chain.puts["volume"].fillna(0).sum())
                cv = float(chain.calls["volume"].fillna(0).sum())
                poi = float(chain.puts["openInterest"].fillna(0).sum())
                coi = float(chain.calls["openInterest"].fillna(0).sum())

                total_put_vol += pv
                total_call_vol += cv
                total_put_oi += poi
                total_call_oi += coi

                if dte <= 14:
                    near_put_vol += pv
                    near_call_vol += cv
            except Exception:
                pass

        if total_call_vol > 0:
            result["pcr_spy_volume"] = round(total_put_vol / total_call_vol, 3)
        if total_call_oi > 0:
            result["pcr_spy_oi"] = round(total_put_oi / total_call_oi, 3)
        if near_call_vol > 0:
            result["pcr_spy_near"] = round(near_put_vol / near_call_vol, 3)

    except Exception as e:
        log.warning(f"[spy_worker] SPY: {e}")

    return result


async def _fetch_cboe_pcr(session: aiohttp.ClientSession) -> dict:
    """CBOE equity + index PCR quotidien (CSV gratuit)."""
    try:
        async with session.get(_CBOE_PCR_URL, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return {}
            text = await r.text()
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # Header: DATE,CALL_VOL,PUT_VOL,TOTAL_VOL,PC_RATIO,...
            # On cherche la dernière ligne de données
            for line in reversed(lines[1:]):
                parts = line.split(",")
                if len(parts) >= 5:
                    try:
                        equity_pcr = float(parts[4]) if parts[4] else None
                        index_pcr = float(parts[9]) if len(parts) > 9 and parts[9] else None
                        return {"pcr_equity": equity_pcr, "pcr_index": index_pcr}
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        log.warning(f"[spy_worker] CBOE PCR: {e}")
    return {}


# ─────────────────────────── Derived metrics ─────────────────────────────────

def _compute_derived(raw: dict) -> dict:
    from .spy_vix_engine import compute_vix_regime, compute_vix_features
    from .spy_stress_rebound import compute_stress_rebound
    from .spy_us_mopi import compute_us_mopi
    from .spy_regime_engine import compute_spy_regime

    vix = raw.get("vix")
    vix9d = raw.get("vix9d")
    vix3m = raw.get("vix3m")
    vix6m = raw.get("vix6m")
    vvix = raw.get("vvix")
    vix_prev1d = raw.get("_vix_prev1d")
    vix_prev5d = raw.get("_vix_prev5d")

    derived: dict = {}

    # VIX spreads
    if vix is not None:
        derived["iv_current"] = vix
        if vix9d is not None:
            derived["vix9d_vix_spread"] = round(vix9d - vix, 3)
        if vix3m is not None:
            derived["vix_vix3m_spread"] = round(vix - vix3m, 3)
            derived["contango"] = int(vix < vix3m)
        if vvix is not None:
            derived["vol_of_vol"] = vvix
        if vix_prev1d is not None:
            derived["vix_change_1d"] = round(vix - vix_prev1d, 3)
        if vix_prev5d is not None:
            derived["vix_change_5d"] = round(vix - vix_prev5d, 3)

    # VIX features + regime
    try:
        feats = compute_vix_features({**raw, **derived})
        regime = compute_vix_regime(feats)
        derived["vix_regime"] = regime
    except Exception as e:
        log.warning(f"[spy_worker] vix_engine: {e}")
        derived["vix_regime"] = "UNKNOWN"

    # Stress Rebound Engine
    try:
        rebound = compute_stress_rebound({**raw, **derived})
        derived["prob_rebound_1d"] = rebound.get("prob_rebound_1d")
        derived["prob_rebound_3d"] = rebound.get("prob_rebound_3d")
        derived["prob_rebound_5d"] = rebound.get("prob_rebound_5d")
        derived["rebound_confidence"] = rebound.get("confidence")
        derived["rebound_factors"] = json.dumps(rebound.get("factors", []))
    except Exception as e:
        log.warning(f"[spy_worker] stress_rebound: {e}")

    # US-MOPI
    try:
        mopi = compute_us_mopi({**raw, **derived})
        derived["us_mopi"] = mopi.get("score")
        derived["us_mopi_label"] = mopi.get("label")
    except Exception as e:
        log.warning(f"[spy_worker] us_mopi: {e}")

    # SPY Regime
    try:
        spy_reg = compute_spy_regime({**raw, **derived})
        derived["spy_regime"] = spy_reg
    except Exception as e:
        log.warning(f"[spy_worker] spy_regime: {e}")

    return derived


# ─────────────────────────── Main async worker ───────────────────────────────

async def _collect_once() -> None:
    loop = asyncio.get_event_loop()

    # yfinance (bloquant) → executor
    raw = await loop.run_in_executor(None, _fetch_yfinance_sync)

    # CBOE PCR (async)
    async with aiohttp.ClientSession() as session:
        cboe = await _fetch_cboe_pcr(session)

    raw.update(cboe)
    derived = _compute_derived(raw)
    merged = {**raw, **derived}

    ts = int(time.time())
    merged["ts"] = ts

    # Nettoyage clés internes (préfixe _)
    clean = {k: v for k, v in merged.items() if not k.startswith("_")}

    # Mise à jour cache
    for k in _cache:
        if k in clean:
            _cache[k] = clean[k]
    _cache["last_updated"] = ts
    _cache["error"] = None

    save_spy_snapshot(clean)
    vix_val = clean.get("vix", "?")
    regime = clean.get("vix_regime", "?")
    mopi = clean.get("us_mopi", "?")
    log.info(f"[spy_worker] VIX={vix_val} regime={regime} US-MOPI={mopi} ts={ts}")


async def run_worker() -> None:
    init_spy_db()
    await asyncio.sleep(15)  # laisse le backend démarrer
    while True:
        try:
            await _collect_once()
        except Exception as e:
            _cache["error"] = str(e)
            log.error(f"[spy_worker] error: {e}")
        await asyncio.sleep(SPY_WORKER_INTERVAL)
