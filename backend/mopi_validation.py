"""
mopi_validation.py — Self-validation du signal MOPI sur données historiques réelles.

F12.3 — Calcule le WR conditionnel des signaux MOPI >HIGH et <LOW à +4h/+24h,
avec borne Wilson LB (95% IC unilatéral) et n par bucket.

Résultats F12 (1901 snapshots, mai–juillet 2026) :
  MOPI>70 @4h  : WR=44.6%  Wilson LB=0.375  → PAS d'edge (sous hasard)
  MOPI>70 @24h : WR=79.2%  Wilson LB=0.725  → Edge fort et significatif
  MOPI<30 @4h  : WR=62.0%  Wilson LB=0.482  → Marginalement non-significatif
  MOPI<30 @24h : WR=89.4%  Wilson LB=0.774  → Edge très fort
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Optional

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

# Horizons en secondes (tolérance ±10%)
_HORIZON_4H_MIN  = 13200   # 3h40
_HORIZON_4H_MAX  = 16800   # 4h40
_HORIZON_24H_MIN = 82800   # 23h
_HORIZON_24H_MAX = 97200   # 27h


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _wilson_lower(wr: float, n: int, z: float = 1.645) -> float:
    """Borne inférieure Wilson à z sigma (z=1.645 → 95% unilatéral)."""
    if n <= 0:
        return 0.0
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (wr + z2 / (2 * n)) / denom
    margin = z * math.sqrt(wr * (1 - wr) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - margin)


def _compute_bucket(
    conn: sqlite3.Connection,
    threshold: float,
    direction: str,           # "UP" | "DOWN"
    horizon_min: int,
    horizon_max: int,
) -> dict:
    """Calcule WR, Wilson LB, n pour un bucket MOPI/horizon donné."""
    if direction == "UP":
        cond = f"h.mopi > {threshold}"
    else:
        cond = f"h.mopi < {threshold}"

    rows = conn.execute(f"""
        SELECT h.mopi, h.btc_price AS entry_price,
               (SELECT btc_price FROM metrics_history
                WHERE ts BETWEEN h.ts + {horizon_min} AND h.ts + {horizon_max}
                ORDER BY ts LIMIT 1) AS future_price
        FROM metrics_history h
        WHERE {cond} AND h.btc_price IS NOT NULL AND h.btc_price > 0
    """).fetchall()

    valid = [r for r in rows if r["future_price"] is not None]
    n = len(valid)
    if n == 0:
        return {"n": 0, "wr": None, "wilson_lb": None, "avg_ret_pct": None,
                "has_edge": False, "threshold": threshold, "direction": direction}

    if direction == "UP":
        wins = sum(1 for r in valid if r["future_price"] > r["entry_price"])
    else:
        wins = sum(1 for r in valid if r["future_price"] < r["entry_price"])

    wr = wins / n
    avg_ret = sum(
        (r["future_price"] - r["entry_price"]) / r["entry_price"] * 100
        for r in valid
    ) / n
    wilson_lb = _wilson_lower(wr, n)
    has_edge = n >= 30 and wilson_lb > 0.50

    return {
        "n": n,
        "wins": wins,
        "wr": round(wr, 3),
        "wilson_lb": round(wilson_lb, 3),
        "avg_ret_pct": round(avg_ret, 3),
        "has_edge": has_edge,
        "threshold": threshold,
        "direction": direction,
    }


def compute_mopi_validation(
    high_threshold: float = 70.0,
    low_threshold: float = 30.0,
) -> dict:
    """
    Retourne le rapport de validation MOPI complet.

    Structure :
    {
      "generated_at": int,
      "n_snapshots_total": int,
      "thresholds": {"high": 70, "low": 30},
      "percentiles": {"p10": ..., "p20": ..., ...},
      "results": {
        "high_4h":  {n, wr, wilson_lb, avg_ret_pct, has_edge},
        "high_24h": {...},
        "low_4h":   {...},
        "low_24h":  {...},
      },
      "verdict": {
        "high_has_4h_edge": bool,
        "high_has_24h_edge": bool,
        "low_has_4h_edge": bool,
        "low_has_24h_edge": bool,
        "recommended_horizon": "24h" | "4h" | "none",
        "signal_status": "validé_24h" | "recalibrer" | "pas_d_edge",
      }
    }
    """
    conn = _conn()
    try:
        # Total snapshots
        n_total = conn.execute(
            "SELECT COUNT(*) FROM metrics_history WHERE mopi IS NOT NULL"
        ).fetchone()[0]

        # Percentiles
        mopi_vals = [
            r[0] for r in conn.execute(
                "SELECT mopi FROM metrics_history WHERE mopi IS NOT NULL ORDER BY mopi"
            ).fetchall()
        ]
        n = len(mopi_vals)
        percentiles = {}
        for p in [10, 20, 30, 70, 80, 90]:
            percentiles[f"p{p}"] = round(mopi_vals[int(n * p / 100)], 1) if n > 0 else None

        # Validation buckets
        results = {
            "high_4h":  _compute_bucket(conn, high_threshold, "UP",   _HORIZON_4H_MIN,  _HORIZON_4H_MAX),
            "high_24h": _compute_bucket(conn, high_threshold, "UP",   _HORIZON_24H_MIN, _HORIZON_24H_MAX),
            "low_4h":   _compute_bucket(conn, low_threshold,  "DOWN", _HORIZON_4H_MIN,  _HORIZON_4H_MAX),
            "low_24h":  _compute_bucket(conn, low_threshold,  "DOWN", _HORIZON_24H_MIN, _HORIZON_24H_MAX),
        }

        # Verdict global
        h4  = results["high_4h"]["has_edge"]
        h24 = results["high_24h"]["has_edge"]
        l4  = results["low_4h"]["has_edge"]
        l24 = results["low_24h"]["has_edge"]

        if h24 and l24:
            recommended_horizon = "24h"
            signal_status = "validé_24h"
        elif h4 and l4:
            recommended_horizon = "4h"
            signal_status = "validé_4h"
        elif h24 or l24:
            recommended_horizon = "24h"
            signal_status = "partiel_24h"
        else:
            recommended_horizon = "none"
            signal_status = "pas_d_edge"

        return {
            "generated_at": int(time.time()),
            "n_snapshots_total": n_total,
            "thresholds": {"high": high_threshold, "low": low_threshold},
            "percentiles": percentiles,
            "results": results,
            "verdict": {
                "high_has_4h_edge":     h4,
                "high_has_24h_edge":    h24,
                "low_has_4h_edge":      l4,
                "low_has_24h_edge":     l24,
                "recommended_horizon":  recommended_horizon,
                "signal_status":        signal_status,
            },
        }
    finally:
        conn.close()
