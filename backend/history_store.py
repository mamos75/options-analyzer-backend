"""
Stockage SQLite de l'historique des métriques options.
Enregistrement automatique toutes les 6h via la task background dans main.py.
"""

import sqlite3
import time
import os
import json
from typing import List, Dict, Optional
from pathlib import Path

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


_MIN_BOOTSTRAP_POINTS = 48   # ~24h à 30min/point — en-dessous → mode static
_STATIC_NEAR_GEX_CAP = 500_000_000


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS metrics_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                mopi        REAL,
                gex         REAL,
                dex         REAL,
                iv_rank     REAL,
                pc_ratio    REAL,
                max_pain    REAL,
                flip_level  REAL,
                btc_price   REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON metrics_history(ts)")
        for col in ("pc_ratio_near", "gex_near"):
            try:
                c.execute(f"ALTER TABLE metrics_history ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass  # colonne déjà présente

        # B7 — vex/cex columns (migration safe)
        for col in ("vex REAL DEFAULT 0.0", "cex REAL DEFAULT 0.0"):
            try:
                c.execute(f"ALTER TABLE metrics_history ADD COLUMN {col}")
            except Exception:
                pass  # already exists
        # V4 — convention versioning (1=CP-skew obsolete, 2=short-all)
        try:
            c.execute("ALTER TABLE metrics_history ADD COLUMN vex_convention INTEGER DEFAULT 1")
        except Exception:
            pass  # already exists
        # V5 — regime_id + verdict_arbiter journaling (auto-validation)
        for col_def in (
            "regime_id TEXT",
            "verdict_arbiter TEXT",
        ):
            try:
                c.execute(f"ALTER TABLE metrics_history ADD COLUMN {col_def}")
            except Exception:
                pass  # already exists

        # Table historique Probability Engine — un snapshot complet par intervalle
        c.execute("""
            CREATE TABLE IF NOT EXISTS pe_snapshots (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                    INTEGER NOT NULL,
                spot                  REAL,
                dominant_scenario     TEXT,
                dominant_probability  REAL,
                dominant_confidence   REAL,
                bear_4h_prob          REAL,
                bear_4h_conf          REAL,
                bear_4h_coverage      REAL,
                bull_4h_prob          REAL,
                bull_4h_conf          REAL,
                bear_24h_prob         REAL,
                bear_24h_conf         REAL,
                bear_24h_coverage     REAL,
                bull_24h_prob         REAL,
                bull_24h_conf         REAL,
                bear_72h_prob         REAL,
                bear_72h_conf         REAL,
                bear_72h_coverage     REAL,
                bull_72h_prob         REAL,
                bull_72h_conf         REAL,
                gex_near              REAL,
                dex_direction         TEXT,
                flip_level            REAL,
                gex_momentum          TEXT,
                rules_unavailable_min INTEGER,
                snapshot_json         TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pe_ts ON pe_snapshots(ts)")
        c.commit()


def save_snapshot(
    mopi: float, gex: float, dex: float,
    iv_rank: float, pc_ratio: float,
    max_pain: float, flip_level: float,
    btc_price: float,
    pc_ratio_near: float = 0.0,
    gex_near: float = 0.0,
    vex: float = 0.0,
    cex: float = 0.0,
    # V5 — regime journaling
    regime_id: Optional[str] = None,
    verdict_arbiter: Optional[str] = None,
):
    ts = int(time.time())
    with _conn() as c:
        c.execute(
            "INSERT INTO metrics_history"
            "(ts,mopi,gex,dex,iv_rank,pc_ratio,max_pain,flip_level,btc_price,pc_ratio_near,gex_near,vex,cex,vex_convention,regime_id,verdict_arbiter)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, mopi, gex, dex, iv_rank, pc_ratio, max_pain, flip_level, btc_price, pc_ratio_near, gex_near, vex, cex, 2, regime_id, verdict_arbiter),
        )
        c.commit()


def compute_dynamic_gex_cap(
    days: int = 7,
    percentile: int = 90,
    min_cap: float = 100_000_000,
    max_cap: float = 2_000_000_000,
) -> dict:
    """Calcule le cap near GEX dynamique via percentile rolling.

    Retourne cap_value, cap_mode, saturation_rate_7d, neutralization_rate_7d, n_points.
    En mode bootstrap (< 48 points), retourne le cap statique provisoire.
    """
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT gex_near FROM metrics_history WHERE ts >= ? AND gex_near IS NOT NULL",
            (cutoff,),
        ).fetchall()
    values = [abs(float(r["gex_near"])) for r in rows]
    n = len(values)

    if n < _MIN_BOOTSTRAP_POINTS:
        return {
            "cap_value": _STATIC_NEAR_GEX_CAP,
            "cap_mode": "static/bootstrap",
            "saturation_rate_7d": None,
            "neutralization_rate_7d": None,
            "n_points": n,
        }

    sorted_vals = sorted(values)
    idx = min(int(n * percentile / 100), n - 1)
    raw_cap = sorted_vals[idx]
    cap_value = max(min_cap, min(max_cap, raw_cap))

    # saturation : fraction où le signal serait clampé (0 ou 100 — trop proche du cap)
    saturation_rate = sum(1 for v in values if v >= cap_value * 0.95) / n

    # neutralisation : fraction où le signal serait coincé à ~50 (cap beaucoup trop élevé)
    neutral_threshold = cap_value * 0.05
    neutralization_rate = sum(1 for v in values if v < neutral_threshold) / n

    return {
        "cap_value": cap_value,
        "cap_mode": "dynamic/rolling_7d",
        "saturation_rate_7d": round(saturation_rate, 3),
        "neutralization_rate_7d": round(neutralization_rate, 3),
        "n_points": n,
    }


def get_last_ts() -> int:
    with _conn() as c:
        row = c.execute("SELECT MAX(ts) as ts FROM metrics_history").fetchone()
        return row["ts"] or 0


def _filter_gex_outliers(rows: List[Dict], k: float = 3.0) -> List[Dict]:
    """Retire les spikes GEX aberrants via IQR×k — garde la lisibilité du graphique."""
    values = [r["gex"] for r in rows if r["gex"] is not None]
    if len(values) < 8:
        return rows
    sv = sorted(values)
    n = len(sv)
    q1, q3 = sv[n // 4], sv[3 * n // 4]
    iqr = q3 - q1
    if iqr == 0:
        return rows
    lo, hi = q1 - k * iqr, q3 + k * iqr
    return [r for r in rows if r["gex"] is None or lo <= r["gex"] <= hi]


def get_last_n_snapshots(n: int = 2) -> List[Dict]:
    """Retourne les n derniers snapshots par timestamp décroissant.

    Utilisé par le Probability Engine pour détecter le GEX momentum.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, gex_near, btc_price, mopi FROM metrics_history "
            "ORDER BY ts DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_pe_snapshot(
    output_dict: dict,
    gex_near: float = 0.0,
    dex_direction: str = "",
    flip_level: float = 0.0,
    gex_momentum: Optional[str] = None,
) -> None:
    """Stocke un snapshot complet du Probability Engine horodaté."""
    ts = int(time.time())

    def _sc(key: str) -> dict:
        return output_dict.get(key) or {}

    def _unavailable_count(sc: dict) -> int:
        rules = (sc.get("positive_rules") or []) + (sc.get("penalty_rules") or [])
        return sum(1 for r in rules if r.get("data_quality") == "unavailable")

    unavailable_counts = [
        _unavailable_count(_sc(k))
        for k in ("bear_4h", "bull_4h", "bear_24h", "bull_24h", "bear_72h", "bull_72h")
    ]
    rules_unavail_min = min(unavailable_counts) if unavailable_counts else 0

    with _conn() as c:
        c.execute(
            """INSERT INTO pe_snapshots
            (ts, spot, dominant_scenario, dominant_probability, dominant_confidence,
             bear_4h_prob, bear_4h_conf, bear_4h_coverage,
             bull_4h_prob, bull_4h_conf,
             bear_24h_prob, bear_24h_conf, bear_24h_coverage,
             bull_24h_prob, bull_24h_conf,
             bear_72h_prob, bear_72h_conf, bear_72h_coverage,
             bull_72h_prob, bull_72h_conf,
             gex_near, dex_direction, flip_level, gex_momentum,
             rules_unavailable_min, snapshot_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts,
                output_dict.get("spot"),
                output_dict.get("dominant_scenario"),
                output_dict.get("dominant_probability"),
                output_dict.get("dominant_confidence"),
                _sc("bear_4h").get("probability"),  _sc("bear_4h").get("confidence"),  _sc("bear_4h").get("data_coverage_pct"),
                _sc("bull_4h").get("probability"),  _sc("bull_4h").get("confidence"),
                _sc("bear_24h").get("probability"), _sc("bear_24h").get("confidence"), _sc("bear_24h").get("data_coverage_pct"),
                _sc("bull_24h").get("probability"), _sc("bull_24h").get("confidence"),
                _sc("bear_72h").get("probability"), _sc("bear_72h").get("confidence"), _sc("bear_72h").get("data_coverage_pct"),
                _sc("bull_72h").get("probability"), _sc("bull_72h").get("confidence"),
                gex_near,
                dex_direction,
                flip_level,
                gex_momentum,
                rules_unavail_min,
                json.dumps(output_dict, ensure_ascii=False),
            ),
        )
        c.commit()


def get_pe_history(hours: int = 24) -> List[Dict]:
    """Retourne les snapshots PE des dernières `hours` heures, du plus ancien au plus récent."""
    cutoff = int(time.time()) - hours * 3600
    with _conn() as c:
        rows = c.execute(
            """SELECT ts, spot, dominant_scenario, dominant_probability, dominant_confidence,
                      bear_4h_prob, bear_4h_conf, bear_4h_coverage,
                      bull_4h_prob, bull_4h_conf,
                      bear_24h_prob, bear_24h_conf, bear_24h_coverage,
                      bull_24h_prob, bull_24h_conf,
                      bear_72h_prob, bear_72h_conf, bear_72h_coverage,
                      bull_72h_prob, bull_72h_conf,
                      gex_near, dex_direction, flip_level, gex_momentum,
                      rules_unavailable_min
               FROM pe_snapshots WHERE ts >= ? ORDER BY ts ASC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pe_snapshot_count() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) as n FROM pe_snapshots").fetchone()
        return row["n"] if row else 0


def get_history(days: int, filter_outliers: bool = True) -> List[Dict]:
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT ts,mopi,gex,dex,iv_rank,pc_ratio,pc_ratio_near,max_pain,flip_level,btc_price"
            " FROM metrics_history WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    data = []
    for r in rows:
        row = dict(r)
        # pc_ratio_near NULL sur anciennes lignes → fallback pc_ratio global
        if row.get("pc_ratio_near") is None:
            row["pc_ratio_near"] = row.get("pc_ratio")
        data.append(row)
    if filter_outliers:
        data = _filter_gex_outliers(data)
    return data


def get_vex_cex_history(days: int = 7) -> List[Dict]:
    """Historique VEX/CEX par periode — filtre sur convention v2 (short-all) uniquement.

    Les lignes v1 (CP-skew, avant 2026-07-06) sont exclues pour eviter les trends
    calcules a cheval sur deux conventions incompatibles.
    """
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, vex, cex, vex_convention FROM metrics_history "
            "WHERE ts >= ? AND vex IS NOT NULL AND vex_convention = 2 ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    return [{"ts": r[0], "vex": r[1] or 0.0, "cex": r[2] or 0.0} for r in rows]
