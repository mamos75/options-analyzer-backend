"""
mopi_validation.py — Self-validation du signal MOPI sur données historiques réelles.

F13.1 — Validation par épisodes (gap >6h = nouvel épisode).
Corrige l'inflation statistique F12 due aux snapshots chevauchants (horizon 24h,
snapshot toutes les 30min → lignes corrélées comptées comme cas indépendants).

Résultats F13 (épisodes réels, mai–juillet 2026) :
  MOPI>70 : 179 lignes → 17 épisodes ; WR=68.8%, Wilson LB=0.482 → PAS d'edge
  MOPI<30 :  50 lignes →  9 épisodes ; WR=75.0%, Wilson LB=0.460 → PAS d'edge
  Baseline 24h up-rate : 0.490 (n=1844 snapshots)
  → signal conservé comme indicateur directionnel, poids réduit à "faible"
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

# Horizons en secondes (tolérance ±10%)
_HORIZON_4H_MIN  = 13200   # 3h40
_HORIZON_4H_MAX  = 16800   # 4h40
_HORIZON_24H_MIN = 82800   # 23h
_HORIZON_24H_MAX = 97200   # 27h

# Gap minimum (en secondes) pour définir un nouvel épisode
_EPISODE_GAP = 21600  # 6h


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


def _cluster_episodes(rows: list) -> list:
    """
    Regroupe les snapshots en épisodes (gap >6h = nouvel épisode).
    Retourne uniquement la première ligne de chaque épisode.
    """
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda r: r["ts"])
    episodes = [sorted_rows[0]]
    for i in range(1, len(sorted_rows)):
        if sorted_rows[i]["ts"] - sorted_rows[i - 1]["ts"] > _EPISODE_GAP:
            episodes.append(sorted_rows[i])
    return episodes


def _compute_monthly_wr(episode_rows: list, direction: str) -> dict:
    """Calcule le WR par mois pour les épisodes donnés."""
    monthly: dict = {}
    for row in episode_rows:
        if row["future_price"] is None:
            continue
        month = datetime.utcfromtimestamp(row["ts"]).strftime("%Y-%m")
        if month not in monthly:
            monthly[month] = {"n": 0, "wins": 0}
        monthly[month]["n"] += 1
        if direction == "UP":
            if row["future_price"] > row["entry_price"]:
                monthly[month]["wins"] += 1
        else:
            if row["future_price"] < row["entry_price"]:
                monthly[month]["wins"] += 1
    for m in monthly:
        monthly[m]["wr"] = round(monthly[m]["wins"] / monthly[m]["n"], 3)
    return monthly


def _worst_month(monthly_wr: dict):
    """Retourne le pire mois (WR le plus bas) parmi monthly_wr."""
    if not monthly_wr:
        return None
    worst = min(monthly_wr.items(), key=lambda x: x[1]["wr"])
    return {"month": worst[0], "wr": worst[1]["wr"], "n": worst[1]["n"]}


def _compute_bucket_episodes(
    conn: sqlite3.Connection,
    threshold: float,
    direction: str,           # "UP" | "DOWN"
    horizon_min: int,
    horizon_max: int,
    baseline_wr: float,
) -> dict:
    """Calcule WR, Wilson LB, n_episodes pour un bucket MOPI/horizon par épisodes."""
    if direction == "UP":
        cond = f"h.mopi > {threshold}"
    else:
        cond = f"h.mopi < {threshold}"

    rows = conn.execute(f"""
        SELECT h.ts, h.mopi, h.btc_price AS entry_price,
               (SELECT btc_price FROM metrics_history
                WHERE ts BETWEEN h.ts + {horizon_min} AND h.ts + {horizon_max}
                ORDER BY ts LIMIT 1) AS future_price
        FROM metrics_history h
        WHERE {cond} AND h.btc_price IS NOT NULL AND h.btc_price > 0
        ORDER BY h.ts
    """).fetchall()

    n_heures = len(rows)  # nombre de lignes brutes

    # Clustering épisodes
    episode_first_rows = _cluster_episodes(rows)
    # Garder uniquement les épisodes avec future_price disponible
    valid_episodes = [r for r in episode_first_rows if r["future_price"] is not None]
    n_episodes = len(valid_episodes)

    if n_episodes == 0:
        return {
            "n": 0, "n_episodes": 0, "n_heures": n_heures,
            "wr": None, "wilson_lb": None,
            "has_edge": False, "threshold": threshold, "direction": direction,
            "monthly_wr": {}, "worst_month": None,
        }

    if direction == "UP":
        wins = sum(1 for r in valid_episodes if r["future_price"] > r["entry_price"])
    else:
        wins = sum(1 for r in valid_episodes if r["future_price"] < r["entry_price"])

    wr = wins / n_episodes
    wilson_lb = _wilson_lower(wr, n_episodes)
    # Edge : Wilson LB doit dépasser le baseline de +10 pts
    has_edge = wilson_lb > (baseline_wr + 0.10)

    monthly_wr = _compute_monthly_wr(valid_episodes, direction)

    return {
        "n": n_episodes,           # alias rétrocompat
        "n_episodes": n_episodes,
        "n_heures": n_heures,
        "wins": wins,
        "wr": round(wr, 3),
        "wilson_lb": round(wilson_lb, 3),
        "has_edge": has_edge,
        "threshold": threshold,
        "direction": direction,
        "monthly_wr": monthly_wr,
        "worst_month": _worst_month(monthly_wr),
    }


def _compute_baseline(conn: sqlite3.Connection) -> dict:
    """Calcule le WR baseline séparé UP et DOWN (F14.1)."""
    rows = conn.execute("""
        SELECT h.btc_price,
               (SELECT btc_price FROM metrics_history
                WHERE ts BETWEEN h.ts + 82800 AND h.ts + 97200
                ORDER BY ts LIMIT 1) AS future_price
        FROM metrics_history h
        WHERE h.btc_price IS NOT NULL AND h.btc_price > 0
    """).fetchall()
    valid = [r for r in rows if r["future_price"] is not None]
    n = len(valid)
    if n == 0:
        return {"up": {"wr": 0.5, "n": 0}, "down": {"wr": 0.5, "n": 0}}
    up_wins   = sum(1 for r in valid if r["future_price"] > r["btc_price"])
    down_wins = sum(1 for r in valid if r["future_price"] < r["btc_price"])
    return {
        "up":   {"wr": round(up_wins / n, 3),   "n": n},
        "down": {"wr": round(down_wins / n, 3), "n": n},
    }


def compute_mopi_validation(
    high_threshold: float = 70.0,
    low_threshold: float = 30.0,
) -> dict:
    """
    Retourne le rapport de validation MOPI par épisodes (F13/F14).

    Structure :
    {
      "generated_at": int,
      "n_snapshots_total": int,
      "baseline": {"up": {"wr": float, "n": int}, "down": {"wr": float, "n": int}},
      "thresholds": {"high": 70, "low": 30},
      "results": {
        "high_4h":  {n, n_episodes, n_heures, wr, wilson_lb, has_edge, monthly_wr, worst_month},
        "high_24h": {...},
        "low_4h":   {...},
        "low_24h":  {...},
      },
      "verdict": {
        "signal_status": "validé_24h" | "partiel_24h" | "recalibrer" | "pas_d_edge",
        "recommended_horizon": "24h" | "4h" | "none",
        "preliminary": bool,
      }
    }
    """
    conn = _conn()
    try:
        # Total snapshots
        n_total = conn.execute(
            "SELECT COUNT(*) FROM metrics_history WHERE mopi IS NOT NULL"
        ).fetchone()[0]

        # Baseline 24h — F14.1 : baseline up/down séparés
        baseline = _compute_baseline(conn)
        baseline_up_wr   = baseline["up"]["wr"]
        baseline_down_wr = baseline["down"]["wr"]

        # Validation buckets par épisodes
        results = {
            "high_4h":  _compute_bucket_episodes(conn, high_threshold, "UP",   _HORIZON_4H_MIN,  _HORIZON_4H_MAX,  baseline_up_wr),
            "high_24h": _compute_bucket_episodes(conn, high_threshold, "UP",   _HORIZON_24H_MIN, _HORIZON_24H_MAX, baseline_up_wr),
            "low_4h":   _compute_bucket_episodes(conn, low_threshold,  "DOWN", _HORIZON_4H_MIN,  _HORIZON_4H_MAX,  baseline_down_wr),
            "low_24h":  _compute_bucket_episodes(conn, low_threshold,  "DOWN", _HORIZON_24H_MIN, _HORIZON_24H_MAX, baseline_down_wr),
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

        # F14.2 — preliminary élargi : n < 30 OU wilson_lb <= baseline correspondante
        def _bucket_is_validated(bucket: dict, bline_wr: float) -> bool:
            n_ep = bucket.get("n_episodes", 0)
            lb   = bucket.get("wilson_lb")
            return n_ep >= 30 and lb is not None and lb > bline_wr

        h24_ok = _bucket_is_validated(results["high_24h"], baseline_up_wr)
        l24_ok = _bucket_is_validated(results["low_24h"],  baseline_down_wr)
        preliminary = not (h24_ok and l24_ok)

        return {
            "generated_at": int(time.time()),
            "n_snapshots_total": n_total,
            "baseline": baseline,
            "thresholds": {"high": high_threshold, "low": low_threshold},
            "results": results,
            "verdict": {
                "signal_status":        signal_status,
                "recommended_horizon":  recommended_horizon,
                "preliminary":          preliminary,
            },
        }
    finally:
        conn.close()
