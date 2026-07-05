"""
SPY Arena — Model Arena pour marchés US (SPY).

Moteurs (Phase 6) :
  naive_spy       — baseline 50/50
  expert_v1_spy   — VIX régime + SPY drawdown (rules)
  expert_v2_spy   — expert_v1 + US-MOPI + term structure
  autocal_spy     — calibration historique par condition VIX
  ml_research_spy — régression logistique pure numpy sur features SPY

Architecture :
  - Outcomes +1h / +4h / +24h (SPY price move > +0.5% = UP, < -0.5% = DOWN)
  - WR / EV / PF par moteur
  - Même structure que model_arena.py (BTC) — réutilisation directe

Garde-fous :
  - N < 20 → WARMING UP (aucune prédiction publique)
  - prob cap 65% max
  - EV > 0 + PF > 1.1 pour promotion ACTIVE
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

DB_PATH = ""  # set at init


# ─────────────────────────── DB Setup ────────────────────────────────────────

def init_spy_arena_db(db_path: str) -> None:
    global DB_PATH
    DB_PATH = db_path
    with sqlite3.connect(db_path) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS spy_arena_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_open      INTEGER NOT NULL,
                spy_open     REAL,
                features     TEXT,
                predictions  TEXT,
                outcome_1h   TEXT,
                outcome_4h   TEXT,
                outcome_24h  TEXT,
                spy_close_1h  REAL,
                spy_close_4h  REAL,
                spy_close_24h REAL,
                ts_close_1h   INTEGER,
                ts_close_4h   INTEGER,
                ts_close_24h  INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_spa_ts ON spy_arena_events(ts_open)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS spy_arena_outcomes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   INTEGER NOT NULL,
                horizon    TEXT NOT NULL,
                engine     TEXT NOT NULL,
                predicted  TEXT,
                prob       REAL,
                outcome    TEXT,
                correct    INTEGER,
                return_pct REAL,
                ts         INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_spo_engine ON spy_arena_outcomes(engine)")


# ─────────────────────────── Feature Extractor ───────────────────────────────

def extract_spy_features(cache: dict) -> Dict[str, Optional[float]]:
    """Extrait les features SPY depuis le cache du worker."""
    vix = cache.get("vix")
    vix_regime = cache.get("vix_regime", "UNKNOWN")
    us_mopi = cache.get("us_mopi")
    spy_chg = cache.get("spy_change_1d")
    spy_dd3 = cache.get("spy_drawdown_3d")
    spy_dd5 = cache.get("spy_drawdown_5d")
    spy_dist = cache.get("spy_dist_52w_high")
    iv_rank = cache.get("iv_rank")
    pcr_near = cache.get("pcr_spy_near")
    pcr_vol = cache.get("pcr_spy_volume")
    pcr_equity = cache.get("pcr_equity")
    vix_chg1d = cache.get("vix_change_1d")
    vix_chg5d = cache.get("vix_change_5d")
    contango = cache.get("contango")
    vix_spread = cache.get("vix_vix3m_spread")
    vvix = cache.get("vvix")
    spy_regime = cache.get("spy_regime", "NEUTRAL")
    rebound_conf = cache.get("rebound_confidence", "NO_TRIGGER")

    # Encodage régimes en numérique
    _vix_regime_map = {
        "NORMAL": 0.1, "ELEVATED": 0.3, "VOL_CRUSH": 0.15,
        "STRESS": 0.6, "PANIC": 0.8, "PANIC_EXTREME": 0.95,
        "RELIEF_RALLY": 0.5, "UNKNOWN": 0.5,
    }
    _spy_regime_map = {
        "RISK_ON_TREND": 0.1, "COMPLACENCY": 0.2, "NEUTRAL": 0.5,
        "VOL_CONTRACTION": 0.3, "VOL_EXPANSION": 0.65,
        "RISK_OFF_STRESS": 0.75, "PANIC_REBOUND": 0.6,
    }
    _rebound_map = {
        "NO_TRIGGER": 0.0, "NO_DATA": 0.0, "EXPLORATION": 0.4,
        "MODERATE": 0.6, "VALIDATED": 0.8,
    }

    return {
        "vix": vix,
        "vix_regime_num": _vix_regime_map.get(vix_regime, 0.5),
        "spy_regime_num": _spy_regime_map.get(spy_regime, 0.5),
        "us_mopi": us_mopi,
        "spy_chg_1d": spy_chg,
        "spy_dd3": spy_dd3,
        "spy_dd5": spy_dd5,
        "spy_dist_ath": spy_dist,
        "iv_rank": iv_rank,
        "pcr_near": pcr_near,
        "pcr_vol": pcr_vol,
        "pcr_equity": pcr_equity,
        "vix_chg_1d": vix_chg1d,
        "vix_chg_5d": vix_chg5d,
        "contango": float(contango) if contango is not None else None,
        "vix_vix3m_spread": vix_spread,
        "vvix": vvix,
        "rebound_signal": _rebound_map.get(rebound_conf, 0.0),
    }


# ─────────────────────────── Expert Engines ──────────────────────────────────

def _cap(p: float, lo: float = 0.35, hi: float = 0.65) -> float:
    return max(lo, min(hi, p))


def engine_naive_spy(features: dict) -> dict:
    return {
        "engine": "naive_spy",
        "prob_up": 0.50, "prob_down": 0.50, "direction": "NEUTRAL",
        "confidence": "baseline", "status": "active",
    }


def engine_expert_v1_spy(features: dict) -> dict:
    """VIX régime + SPY drawdown → signal rebond contrarian."""
    vix = features.get("vix")
    vix_reg = features.get("vix_regime_num", 0.5)
    dd5 = features.get("spy_dd5")
    rebound = features.get("rebound_signal", 0.0)

    if vix is None:
        return {"engine": "expert_v1_spy", "prob_up": 0.50, "prob_down": 0.50,
                "direction": "NEUTRAL", "confidence": "no_data", "status": "active"}

    # Signal de base : VIX élevé + drawdown = rebond probable
    base = 0.50

    # Contribution VIX régime (PANIC → +rebond)
    if vix > 30:
        base += 0.10
    elif vix > 25:
        base += 0.07
    elif vix > 20:
        base += 0.05
    elif vix < 13:
        base -= 0.05  # complacence

    # Contribution drawdown
    if dd5 is not None:
        if dd5 < -5:
            base += 0.06
        elif dd5 < -3:
            base += 0.03

    # Contribution signal rebound validé
    base += rebound * 0.05

    p_up = _cap(base)
    direction = "UP" if p_up > 0.52 else "DOWN" if p_up < 0.48 else "NEUTRAL"
    return {
        "engine": "expert_v1_spy", "prob_up": round(p_up, 3), "prob_down": round(1 - p_up, 3),
        "direction": direction, "confidence": "rules_v1", "status": "active",
    }


def engine_expert_v2_spy(features: dict) -> dict:
    """Expert V1 + US-MOPI + term structure."""
    base_result = engine_expert_v1_spy(features)
    p_up = base_result["prob_up"]

    us_mopi = features.get("us_mopi")
    vix_spread = features.get("vix_vix3m_spread")
    contango = features.get("contango")

    # Contribution US-MOPI contrarian
    if us_mopi is not None:
        if us_mopi <= 20:
            p_up += 0.05  # peur extrême = rebond contrarian
        elif us_mopi >= 80:
            p_up -= 0.05  # complacence extrême = risque

    # Contribution term structure
    if vix_spread is not None and vix_spread > 2:
        p_up += 0.03  # backwardation forte = stress → rebond
    if contango == 1:
        p_up -= 0.01  # contango = normalité

    p_up = _cap(p_up)
    direction = "UP" if p_up > 0.52 else "DOWN" if p_up < 0.48 else "NEUTRAL"
    return {
        "engine": "expert_v2_spy", "prob_up": round(p_up, 3), "prob_down": round(1 - p_up, 3),
        "direction": direction, "confidence": "rules_v2", "status": "active",
    }


def engine_autocal_spy(features: dict, history: list) -> dict:
    """Calibration historique : P(UP | VIX_bucket × spy_regime)."""
    vix = features.get("vix")
    spy_reg = features.get("spy_regime_num", 0.5)

    if not history or vix is None:
        return {"engine": "autocal_spy", "prob_up": 0.50, "prob_down": 0.50,
                "direction": "NEUTRAL", "confidence": "warming_up", "status": "warming_up"}

    # Buckets VIX
    def _vix_bucket(v):
        if v is None:
            return "unknown"
        if v > 30:
            return "panic"
        if v > 20:
            return "stress"
        if v > 15:
            return "elevated"
        return "normal"

    current_bucket = _vix_bucket(vix)

    matching = []
    for row in history:
        row_features = json.loads(row.get("features", "{}") or "{}")
        row_vix = row_features.get("vix")
        row_bucket = _vix_bucket(row_vix)
        if row_bucket == current_bucket:
            for hz in ["outcome_1h", "outcome_4h", "outcome_24h"]:
                outcome = row.get(hz)
                if outcome in ("UP", "DOWN"):
                    matching.append(outcome)

    n = len(matching)
    if n < 20:
        return {"engine": "autocal_spy", "prob_up": 0.50, "prob_down": 0.50,
                "direction": "NEUTRAL", "confidence": f"warming_up_{n}/{20}", "status": "warming_up",
                "n": n}

    wr = matching.count("UP") / n
    p_up = _cap(wr)
    direction = "UP" if p_up > 0.52 else "DOWN" if p_up < 0.48 else "NEUTRAL"
    return {
        "engine": "autocal_spy", "prob_up": round(p_up, 3), "prob_down": round(1 - p_up, 3),
        "direction": direction, "confidence": f"historical_n{n}", "status": "active",
        "n": n, "raw_wr": round(wr, 3), "bucket": current_bucket,
    }


def engine_ml_research_spy(features: dict, history: list) -> dict:
    """Régression logistique pure numpy sur features SPY."""
    _FEATURE_KEYS = [
        "vix", "vix_regime_num", "spy_regime_num", "us_mopi",
        "spy_chg_1d", "spy_dd3", "spy_dd5", "iv_rank",
        "pcr_near", "vix_chg_1d", "vix_chg_5d", "contango",
        "vix_vix3m_spread", "rebound_signal",
    ]
    _MIN_N = 30
    _EPOCHS = 200
    _LR = 0.05

    if not history or len(history) < _MIN_N:
        n = len(history) if history else 0
        return {"engine": "ml_research_spy", "prob_up": 0.50, "prob_down": 0.50,
                "direction": "NEUTRAL", "confidence": f"warming_up_{n}/{_MIN_N}",
                "status": "warming_up", "n": n}

    # Build dataset
    X_rows, y_rows = [], []
    for row in history:
        feats = json.loads(row.get("features", "{}") or "{}")
        for hz in ["outcome_24h", "outcome_4h", "outcome_1h"]:
            outcome = row.get(hz)
            if outcome not in ("UP", "DOWN"):
                continue
            x = [feats.get(k) for k in _FEATURE_KEYS]
            if any(v is None for v in x):
                continue
            X_rows.append(x)
            y_rows.append(1 if outcome == "UP" else 0)
            break

    n = len(X_rows)
    if n < _MIN_N:
        return {"engine": "ml_research_spy", "prob_up": 0.50, "prob_down": 0.50,
                "direction": "NEUTRAL", "confidence": f"warming_up_{n}/{_MIN_N}",
                "status": "warming_up", "n": n}

    X = np.array(X_rows, dtype=float)
    y = np.array(y_rows, dtype=float)

    # Normalisation min-max par feature
    X_min, X_max = X.min(axis=0), X.max(axis=0)
    denom = np.where(X_max - X_min > 1e-8, X_max - X_min, 1.0)
    X_norm = (X - X_min) / denom

    # Logistic regression
    w = np.zeros(X_norm.shape[1])
    b = 0.0

    def _sigmoid(z):
        return 1 / (1 + np.exp(-np.clip(z, -20, 20)))

    for _ in range(_EPOCHS):
        z = X_norm @ w + b
        pred = _sigmoid(z)
        err = pred - y
        w -= _LR / n * (X_norm.T @ err)
        b -= _LR / n * err.sum()

    # Predict current
    x_cur = [features.get(k) for k in _FEATURE_KEYS]
    if any(v is None for v in x_cur):
        return {"engine": "ml_research_spy", "prob_up": 0.50, "prob_down": 0.50,
                "direction": "NEUTRAL", "confidence": "missing_features",
                "status": "active", "n": n}

    x_cur_arr = np.array(x_cur, dtype=float)
    x_cur_norm = (x_cur_arr - X_min) / denom
    raw_prob = float(_sigmoid(x_cur_norm @ w + b))
    p_up = _cap(raw_prob)
    direction = "UP" if p_up > 0.52 else "DOWN" if p_up < 0.48 else "NEUTRAL"

    return {
        "engine": "ml_research_spy", "prob_up": round(p_up, 3), "prob_down": round(1 - p_up, 3),
        "direction": direction, "confidence": f"logistic_n{n}", "status": "active",
        "n": n, "raw_prob": round(raw_prob, 3),
    }


# ─────────────────────────── Main API ────────────────────────────────────────

def get_spy_arena_snapshot(cache: dict) -> dict:
    """Calcule et retourne les prédictions de tous les moteurs SPY."""
    features = extract_spy_features(cache)
    history = _get_history(200)

    results = {
        "naive_spy":       engine_naive_spy(features),
        "expert_v1_spy":   engine_expert_v1_spy(features),
        "expert_v2_spy":   engine_expert_v2_spy(features),
        "autocal_spy":     engine_autocal_spy(features, history),
        "ml_research_spy": engine_ml_research_spy(features, history),
    }

    # Agrégat consensus
    active_probs = [
        r["prob_up"] for r in results.values()
        if r.get("status") == "active" and r["engine"] != "naive_spy"
    ]
    consensus_up = round(sum(active_probs) / len(active_probs), 3) if active_probs else 0.50
    consensus_dir = "UP" if consensus_up > 0.52 else "DOWN" if consensus_up < 0.48 else "NEUTRAL"

    return {
        "ts": int(time.time()),
        "features": features,
        "engines": results,
        "consensus": {
            "prob_up": consensus_up,
            "prob_down": round(1 - consensus_up, 3),
            "direction": consensus_dir,
            "n_active": len(active_probs),
        },
        "leaderboard": _build_leaderboard(history),
    }


def _build_leaderboard(history: list) -> list:
    """WR / EV / PF par moteur sur outcomes finalisés.

    Deux métriques :
    - winrate : sur tous les outcomes (y compris RANGE), métrique trading complète
    - dir_winrate : précision directionnelle (predicted!=NEUTRAL ET outcome!=RANGE)
    """
    engines = ["naive_spy", "expert_v1_spy", "expert_v2_spy", "autocal_spy", "ml_research_spy"]
    if not DB_PATH:
        return []
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT engine, predicted, outcome, correct, return_pct FROM spy_arena_outcomes WHERE outcome IS NOT NULL"
            ).fetchall()
    except Exception:
        return []

    stats: Dict[str, Dict] = {e: {
        "n": 0, "correct": 0, "wins": [], "losses": [],
        "dir_n": 0, "dir_correct": 0,
    } for e in engines}

    for row in rows:
        eng = row["engine"]
        if eng not in stats:
            continue
        predicted = row["predicted"] or "NEUTRAL"
        outcome = row["outcome"] or "RANGE"
        stats[eng]["n"] += 1
        if row["correct"] == 1:
            stats[eng]["correct"] += 1
            stats[eng]["wins"].append(row["return_pct"] or 0)
        else:
            stats[eng]["losses"].append(abs(row["return_pct"] or 0))
        # Directional precision: exclude NEUTRAL predictions and RANGE outcomes
        if predicted != "NEUTRAL" and outcome != "RANGE":
            stats[eng]["dir_n"] += 1
            if predicted == outcome:
                stats[eng]["dir_correct"] += 1

    leaderboard = []
    for eng, s in stats.items():
        n = s["n"]
        if n == 0:
            leaderboard.append({"engine": eng, "n": 0, "status": "warming_up"})
            continue
        wr = s["correct"] / n
        avg_win = sum(s["wins"]) / len(s["wins"]) if s["wins"] else 0
        avg_loss = sum(s["losses"]) / len(s["losses"]) if s["losses"] else 0
        ev = wr * avg_win - (1 - wr) * avg_loss
        pf = (wr * avg_win) / max((1 - wr) * avg_loss, 1e-6)
        dir_n = s["dir_n"]
        dir_wr = round(s["dir_correct"] / dir_n, 3) if dir_n >= 10 else None
        leaderboard.append({
            "engine": eng, "n": n,
            "winrate": round(wr, 3),
            "dir_winrate": dir_wr,
            "dir_n": dir_n,
            "ev": round(ev, 3),
            "profit_factor": round(pf, 2),
            "avg_win": round(avg_win, 3),
            "avg_loss": round(avg_loss, 3),
            "status": "active" if dir_n >= 30 and dir_wr is not None and dir_wr > 0.52 else "exploring",
        })

    leaderboard.sort(key=lambda x: x.get("dir_winrate") or 0, reverse=True)
    return leaderboard


def _get_history(limit: int = 200) -> list:
    if not DB_PATH:
        return []
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM spy_arena_events ORDER BY ts_open DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def record_spy_event(cache: dict) -> int:
    """Enregistre un snapshot SPY pour tracking futur."""
    if not DB_PATH:
        return -1
    features = extract_spy_features(cache)
    results = {
        "naive_spy":       engine_naive_spy(features),
        "expert_v1_spy":   engine_expert_v1_spy(features),
        "expert_v2_spy":   engine_expert_v2_spy(features),
        "autocal_spy":     engine_autocal_spy(features, _get_history(200)),
        "ml_research_spy": engine_ml_research_spy(features, _get_history(200)),
    }
    spy_price = cache.get("spy_price")
    try:
        with sqlite3.connect(DB_PATH) as c:
            cur = c.execute("""
                INSERT INTO spy_arena_events (ts_open, spy_open, features, predictions)
                VALUES (?, ?, ?, ?)
            """, (int(time.time()), spy_price,
                  json.dumps(features), json.dumps(results)))
            return cur.lastrowid
    except Exception as e:
        log.error(f"[spy_arena] record error: {e}")
        return -1


def validate_spy_outcomes(cache: dict) -> int:
    """Vérifie les événements en attente et calcule les outcomes."""
    if not DB_PATH:
        return 0
    current_spy = cache.get("spy_price")
    if current_spy is None:
        return 0

    now = int(time.time())
    validated = 0

    try:
        with sqlite3.connect(DB_PATH) as c:
            pending = c.execute("""
                SELECT id, ts_open, spy_open, predictions
                FROM spy_arena_events
                WHERE spy_open IS NOT NULL
                  AND (outcome_1h IS NULL OR outcome_4h IS NULL OR outcome_24h IS NULL)
            """).fetchall()

        for row in pending:
            event_id, ts_open, spy_open, predictions_json = row
            elapsed_h = (now - ts_open) / 3600
            predictions = json.loads(predictions_json or "{}")

            updates = {}
            new_outcomes = []

            for label, min_h, col_outcome, col_close, col_ts in [
                ("1h",  1,   "outcome_1h",  "spy_close_1h",  "ts_close_1h"),
                ("4h",  4,   "outcome_4h",  "spy_close_4h",  "ts_close_4h"),
                ("24h", 24,  "outcome_24h", "spy_close_24h", "ts_close_24h"),
            ]:
                if elapsed_h < min_h:
                    continue
                # Check if already set
                with sqlite3.connect(DB_PATH) as c:
                    existing = c.execute(
                        f"SELECT {col_outcome} FROM spy_arena_events WHERE id=?", (event_id,)
                    ).fetchone()
                    if existing and existing[0] is not None:
                        continue

                ret_pct = (current_spy - spy_open) / spy_open * 100
                if ret_pct > 0.5:
                    outcome = "UP"
                elif ret_pct < -0.5:
                    outcome = "DOWN"
                else:
                    outcome = "RANGE"

                updates[col_outcome] = outcome
                updates[col_close] = current_spy
                updates[col_ts] = now

                # Per-engine outcomes
                for eng, pred in predictions.items():
                    predicted_dir = pred.get("direction", "NEUTRAL")
                    correct = 1 if predicted_dir == outcome else 0
                    new_outcomes.append((
                        event_id, label, eng,
                        predicted_dir, pred.get("prob_up"),
                        outcome, correct, ret_pct, now,
                    ))

            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                vals = list(updates.values()) + [event_id]
                with sqlite3.connect(DB_PATH) as c:
                    c.execute(f"UPDATE spy_arena_events SET {set_clause} WHERE id=?", vals)
                    if new_outcomes:
                        c.executemany("""
                            INSERT INTO spy_arena_outcomes
                            (event_id, horizon, engine, predicted, prob, outcome, correct, return_pct, ts)
                            VALUES (?,?,?,?,?,?,?,?,?)
                        """, new_outcomes)
                validated += 1

    except Exception as e:
        log.error(f"[spy_arena] validate error: {e}")

    return validated
