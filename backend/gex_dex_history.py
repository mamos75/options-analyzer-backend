"""
gex_dex_history.py — Historique GEX/DEX pour le widget GEX & DEX Évolution.

Fournit les séries temporelles GEX[], DEX[], timestamps[] et btc_price[]
sans aucune dépendance sur le module MOPI.

Remplace /api/mopi_vs_btc comme source de données pour gex_dex.js (F15.1).
"""

import os
import sqlite3
import time
from typing import Dict, List, Optional

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

RESOLUTIONS_S = {"30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
PERIODS_D     = {"7d": 7, "14d": 14, "30d": 30, "90d": 90}


def _load_rows(from_ts: int) -> List[Dict]:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, gex, dex, btc_price FROM metrics_history"
            " WHERE ts >= ? ORDER BY ts ASC",
            (from_ts,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _resample(rows: List[Dict], resolution_s: int) -> List[Dict]:
    """Resampling temporel : moyenne GEX/DEX, OHLC BTC."""
    buckets: Dict[int, List[Dict]] = {}
    for r in rows:
        bt = (r["ts"] // resolution_s) * resolution_s
        buckets.setdefault(bt, []).append(r)

    result = []
    for ts in sorted(buckets.keys()):
        grp = sorted(buckets[ts], key=lambda x: x["ts"])
        gex_v = [float(r["gex"]) for r in grp if r.get("gex") is not None]
        dex_v = [float(r["dex"]) for r in grp if r.get("dex") is not None]
        btc_v = [float(r["btc_price"]) for r in grp
                 if r.get("btc_price") and float(r["btc_price"]) > 0]
        if not btc_v:
            continue
        result.append({
            "ts":        ts,
            "gex":       sum(gex_v) / len(gex_v) if gex_v else 0.0,
            "dex":       sum(dex_v) / len(dex_v) if dex_v else 0.0,
            "btc_price": sum(btc_v) / len(btc_v),
        })
    return result


def compute_gex_dex_history(period: str = "7d", resolution: str = "1h") -> Dict:
    """
    Retourne les séries GEX[], DEX[], timestamps[], btc_price[] sur la période demandée.

    Structure de réponse :
    {
      "status":     "OK" | "NO_DATA",
      "period":     str,
      "resolution": str,
      "n_points":   int,
      "timestamps": [int, ...],
      "gex":        [float, ...],
      "dex":        [float, ...],
      "btc_price":  [float, ...],
    }
    """
    days  = PERIODS_D.get(period, 7)
    res_s = RESOLUTIONS_S.get(resolution, 3600)

    from_ts = int(time.time()) - days * 86400
    raw = _load_rows(from_ts)

    if not raw:
        return {
            "status":     "NO_DATA",
            "message":    "Aucune donnée disponible — accumulation en cours.",
            "period":     period,
            "resolution": resolution,
            "n_points":   0,
            "timestamps": [],
            "gex":        [],
            "dex":        [],
            "btc_price":  [],
        }

    resampled = _resample(raw, res_s)

    return {
        "status":     "OK",
        "period":     period,
        "resolution": resolution,
        "n_points":   len(resampled),
        "timestamps": [r["ts"]                   for r in resampled],
        "gex":        [round(r["gex"], 0)         for r in resampled],
        "dex":        [round(r["dex"], 1)         for r in resampled],
        "btc_price":  [round(r["btc_price"], 0)   for r in resampled],
    }
