"""
BME Binance Enrichment — Données OHLCV 90j pour enrichir l'entraînement du BTCMomentumEngine.

Problème résolu :
  Les données internes (model_predictions) couvrent ~19j bullish uniquement.
  Le BME ne voit jamais de bear market → biais UP extrême.

Solution :
  Fetch klines 1h BTC/USDT depuis Binance public API (90j = ~2160 candles).
  Calcul forward-looking des labels UP/DOWN/RANGE.
  Stockage dans bme_price_training (table SQLite séparée).
  Le BME combine les deux sources à l'entraînement.

Seuils de labeling (calibrés sur distributions réelles) :
  4h  : ±0.5%  → UP / DOWN
  24h : ±1.5%  → UP / DOWN
  72h : ±3.0%  → UP / DOWN
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")
_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_SYMBOL    = "BTCUSDT"
_INTERVAL  = "1h"          # 1 candle = 1h
_DAYS_BACK = 90
_CANDLES   = _DAYS_BACK * 24   # 2160

# Seuils de labeling par horizon
_LABEL_THRESHOLDS = {
    "4h":  0.005,   # ±0.5%
    "24h": 0.015,   # ±1.5%
    "72h": 0.015,   # ±1.5% (réduit de 3%→1.5% pour équilibrer UP/DOWN sur données 90j)
}
# Candles à l'avance par horizon
_HORIZON_CANDLES = {"4h": 4, "24h": 24, "72h": 72}

_LABEL_IDX = {"UP": 0, "DOWN": 1, "RANGE": 2}


# ─── Fetch klines ──────────────────────────────────────────────────────────────

def fetch_binance_klines(days_back: int = _DAYS_BACK) -> List[dict]:
    """Fetch klines 1h BTCUSDT depuis l'API publique Binance. Retourne liste de dicts."""
    try:
        import urllib.request
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - days_back * 24 * 3600 * 1000
        limit    = 1000   # max par requête Binance

        candles = []
        fetch_start = start_ms
        while fetch_start < end_ms:
            url = (
                f"{_BINANCE_KLINES_URL}?symbol={_SYMBOL}&interval={_INTERVAL}"
                f"&startTime={fetch_start}&limit={limit}"
            )
            with urllib.request.urlopen(url, timeout=15) as resp:
                batch = json.loads(resp.read())
            if not batch:
                break
            candles.extend(batch)
            last_ts = batch[-1][0]
            fetch_start = last_ts + 3600 * 1000  # +1h
            if len(batch) < limit:
                break

        result = []
        for c in candles:
            result.append({
                "ts":    int(c[0]) // 1000,   # open_time → secondes
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "vol":   float(c[5]),          # base volume BTC
            })
        log.info(f"[bme_enrich] Binance klines fetched: {len(result)} candles")
        return result
    except Exception as e:
        log.error(f"[bme_enrich] fetch_binance_klines error: {e}")
        return []


# ─── Feature extraction (prix uniquement) ─────────────────────────────────────

def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    returns = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(0, r) for r in returns[-period:]]
    losses = [max(0, -r) for r in returns[-period:]]
    avg_g  = sum(gains)  / period
    avg_l  = sum(losses) / period
    if avg_l < 1e-8:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def build_price_features(closes: List[float], idx: int) -> Optional[np.ndarray]:
    """
    Construit le vecteur 22-dim à partir de candles closes.
    Features options = valeurs neutres (0 / baseline).
    idx = index courant dans closes.
    """
    if idx < 48:  # besoin de 48h d'historique
        return None
    spot = closes[idx]

    def ret(back: int) -> float:
        i = idx - back
        if i < 0 or closes[i] <= 0:
            return 0.0
        return (spot - closes[i]) / closes[i]

    mom_4h  = ret(4)
    mom_8h  = ret(8)
    mom_24h = ret(24)
    mom_48h = ret(48)

    # Momentum accélération
    mom_acc = mom_4h - mom_8h / 2.0 if mom_8h != 0 else 0.0

    # Volatilité réalisée 24h (std des retours horaires sur 24h)
    window_24h = closes[max(0, idx-24): idx+1]
    if len(window_24h) >= 4:
        rets_r = [(window_24h[i] - window_24h[i-1]) / window_24h[i-1]
                  for i in range(1, len(window_24h)) if window_24h[i-1] > 0]
        vol_24h = float(np.std(rets_r)) if rets_r else 0.005
    else:
        vol_24h = 0.005

    # RSI-14 (fenêtre 8h = 8 candles)
    rsi_window = closes[max(0, idx-22): idx+1]
    rsi = _compute_rsi(rsi_window, 14)
    rsi = rsi if rsi is not None else 50.0

    # Bollinger Band position (24h)
    if len(window_24h) >= 8:
        ma    = np.mean(window_24h)
        std_b = np.std(window_24h)
        bb_pct = float((spot - ma) / (2.0 * std_b)) if std_b > 0 else 0.0
    else:
        bb_pct = 0.0

    # Normalisation identique à btc_momentum_engine.build_feature_vector
    v_mom_4h  = float(np.clip(mom_4h  / 0.05, -3.0, 3.0))
    v_mom_8h  = float(np.clip(mom_8h  / 0.08, -3.0, 3.0))
    v_mom_24h = float(np.clip(mom_24h / 0.12, -3.0, 3.0))
    v_mom_48h = float(np.clip(mom_48h / 0.15, -3.0, 3.0))
    v_acc     = float(np.clip(mom_acc / 0.03, -3.0, 3.0))
    v_vol     = float(np.clip(vol_24h / 0.01, 0.0, 5.0))
    v_rsi     = float((rsi - 50.0) / 50.0)
    v_bb      = float(np.clip(bb_pct, -2.0, 2.0))

    # Features options = valeurs neutres
    v_gex     = 0.0   # gex_near = 0
    v_dex     = 0.0   # direction neutre
    v_iv      = 0.0   # iv_rank = 50
    v_pcr     = 0.0   # pcr = 1.0 (neutre)
    v_mopi    = 0.0   # mopi = 50
    v_flip    = 0.0   # flip_dist = 0
    v_fund    = 0.0   # funding = 0
    v_oi      = 0.0   # oi neutre
    v_vol_s   = 0.0   # spot volume neutre
    v_mp_dist = 0.0   # max pain distance = 0
    v_div_t   = 0.0   # mopi_div_type neutre
    v_div_s   = 0.0   # mopi_div_strength = 0

    # Composites
    v_mom_regime = 0.0           # mom × regime (regime neutre = 0)
    v_iv_vol     = v_iv * v_vol / 5.0   # = 0 (iv neutre)

    vec = np.array([
        v_mom_4h, v_mom_8h, v_mom_24h, v_mom_48h, v_acc,
        v_vol, v_rsi, v_bb,
        v_gex, v_dex, v_iv, v_pcr, v_mopi, v_flip,
        v_fund, v_oi, v_vol_s, v_mp_dist,
        v_div_t, v_div_s,
        v_mom_regime, v_iv_vol,
    ], dtype=np.float64)
    return np.nan_to_num(vec, nan=0.0, posinf=3.0, neginf=-3.0)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS bme_price_training (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                horizon     TEXT NOT NULL,
                spot        REAL NOT NULL,
                features    BLOB NOT NULL,
                label       TEXT NOT NULL,
                return_pct  REAL,
                created_at  INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bpt_horizon ON bme_price_training(horizon, ts)")
        c.commit()


def _already_loaded(n_expected: int = 500) -> bool:
    """True si on a déjà suffisamment de données Binance en cache."""
    try:
        with _conn() as c:
            row = c.execute("SELECT COUNT(*) as n FROM bme_price_training").fetchone()
            return row["n"] >= n_expected
    except Exception:
        return False


def _last_ts(horizon: str = "") -> int:
    """Retourne le timestamp du dernier sample Binance injecté (par horizon)."""
    try:
        with _conn() as c:
            if horizon:
                row = c.execute(
                    "SELECT MAX(ts) as m FROM bme_price_training WHERE horizon = ?", (horizon,)
                ).fetchone()
            else:
                row = c.execute("SELECT MAX(ts) as m FROM bme_price_training").fetchone()
            return int(row["m"] or 0)
    except Exception:
        return 0


# ─── Build & inject dataset ───────────────────────────────────────────────────

def build_and_store(force: bool = False) -> Dict[str, int]:
    """
    Fetch les klines Binance, génère les samples, insère dans bme_price_training.
    Retourne {horizon: n_inserted} par horizon.
    """
    _ensure_table()

    # Vérification globale : si les données 4h ET 24h sont récentes, skip le fetch
    last_global = _last_ts()
    if not force and last_global > 0 and (time.time() - last_global) < 6 * 3600:
        # Vérifier si 72h est aussi à jour
        last_72h = _last_ts("72h")
        if last_72h > 0 and (time.time() - last_72h) < 6 * 3600:
            log.info(f"[bme_enrich] cache Binance récent — skip fetch")
            return {"4h": 0, "24h": 0, "72h": 0}

    candles = fetch_binance_klines(_DAYS_BACK)
    if len(candles) < 100:
        log.warning("[bme_enrich] Pas assez de candles Binance — abort")
        return {"4h": 0, "24h": 0, "72h": 0}

    closes = [c["close"] for c in candles]
    tss    = [c["ts"]    for c in candles]

    inserted: Dict[str, int] = {"4h": 0, "24h": 0, "72h": 0}

    for horizon, fwd in _HORIZON_CANDLES.items():
        thresh = _LABEL_THRESHOLDS[horizon]
        last_h = _last_ts(horizon)   # last ts par horizon (évite les skips cross-horizon)
        rows_to_insert = []

        for idx in range(48, len(candles) - fwd):
            ts   = tss[idx]
            spot = closes[idx]

            # Déjà présent pour cet horizon ?
            if last_h > 0 and ts <= last_h:
                continue

            fwd_price  = closes[idx + fwd]
            ret        = (fwd_price - spot) / spot
            if ret > thresh:
                label = "UP"
            elif ret < -thresh:
                label = "DOWN"
            else:
                label = "RANGE"

            vec = build_price_features(closes, idx)
            if vec is None:
                continue

            rows_to_insert.append((
                ts, horizon, spot, vec.tobytes(), label, round(ret, 5)
            ))

        if rows_to_insert:
            with _conn() as c:
                c.executemany(
                    """INSERT OR IGNORE INTO bme_price_training
                       (ts, horizon, spot, features, label, return_pct)
                       VALUES (?,?,?,?,?,?)""",
                    rows_to_insert,
                )
                c.commit()
            inserted[horizon] = len(rows_to_insert)
            log.info(f"[bme_enrich] {horizon}: {len(rows_to_insert)} samples insérés")

    return inserted


# ─── API pour le BME ──────────────────────────────────────────────────────────

def get_training_data(horizon: str, max_n: int = 600) -> List[Tuple[np.ndarray, int]]:
    """
    Retourne [(feature_vector, label_idx)] depuis bme_price_training.
    Appelé par BTCMomentumEngine._get_training_data() pour enrichir le training set.
    """
    _label_map = {"UP": 0, "DOWN": 1, "RANGE": 2}
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT features, label FROM bme_price_training
                   WHERE horizon = ?
                   ORDER BY ts DESC LIMIT ?""",
                (horizon, max_n),
            ).fetchall()
        result = []
        for r in rows:
            try:
                vec = np.frombuffer(r["features"], dtype=np.float64).copy()
                if len(vec) != 22:
                    continue
                lbl = _label_map.get(r["label"])
                if lbl is None:
                    continue
                result.append((vec, lbl))
            except Exception:
                pass
        return result
    except Exception as e:
        log.warning(f"[bme_enrich] get_training_data error: {e}")
        return []


def get_stats() -> dict:
    """Statistiques du dataset Binance."""
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT horizon, label, COUNT(*) as n
                   FROM bme_price_training
                   GROUP BY horizon, label
                   ORDER BY horizon, label"""
            ).fetchall()
            last = c.execute(
                "SELECT MAX(ts) as m, MIN(ts) as mn FROM bme_price_training"
            ).fetchone()
        dist: dict = {}
        for r in rows:
            if r["horizon"] not in dist:
                dist[r["horizon"]] = {}
            dist[r["horizon"]][r["label"]] = r["n"]
        return {
            "distribution": dist,
            "last_ts":  int(last["m"] or 0),
            "first_ts": int(last["mn"] or 0),
        }
    except Exception as e:
        return {"error": str(e)}
