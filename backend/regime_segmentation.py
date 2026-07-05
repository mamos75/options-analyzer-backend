"""
Regime Segmentation Engine — Performance conditionnelle par régime de marché.

Objectif : savoir si une feature gagne GLOBALEMENT ou seulement dans certains régimes.

Régimes taggés sur chaque outcome :
  Positive_Gamma  — GEX > 5M  (dealers long gamma, marché stabilisé)
  Negative_Gamma  — GEX < -5M (dealers short gamma, marché amplifié)
  Neutral         — GEX dans la zone morte (-5M, 5M)
  Vol_Expansion   — IV Rank > 60 (volatilité en expansion)
  Vol_Contraction — IV Rank < 40 (volatilité en contraction)
  Panic           — GEX négatif + IV > 70 + DEX extrême baissier

Pour chaque moteur et chaque feature :
  EV par régime, Winrate par régime, Profit Factor par régime.

Endpoint : GET /api/regime_performance
"""

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_DATA_DIR = Path("/data")
EVENT_LOG_PATH = (
    (_DATA_DIR / "event_store.jsonl") if _DATA_DIR.exists()
    else (Path(__file__).parent / "event_store.jsonl")
)

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")

GEX_NEUTRAL_THRESHOLD = 5_000_000  # $5M — zone morte (aligné avec gex.py)
MIN_N_DISPLAY = 5                  # N minimum pour afficher des stats

REGIMES: Dict[str, str] = {
    "Positive_Gamma":  "GEX > 5M — Dealers long gamma (STABILISANT)",
    "Negative_Gamma":  "GEX < -5M — Dealers short gamma (AMPLIFICATEUR)",
    "Neutral":         "GEX dans la zone neutre (-5M, 5M)",
    "Vol_Expansion":   "IV Rank > 60 — Volatilité en expansion",
    "Vol_Contraction": "IV Rank < 40 — Volatilité en contraction",
    "Panic":           "GEX négatif + IV > 70 + DEX extrême baissier (flux dealers crash)",
}

# Familles de moteurs — groupe les event_types pour la vue "engine"
ENGINE_FAMILIES: Dict[str, List[str]] = {
    "squeeze":   ["squeeze_bullish", "squeeze_bearish", "squeeze_violent_move_unknown_direction"],
    "walls":     ["wall_rejection", "wall_breakout", "wall_rejection_candidate", "wall_breakout_candidate"],
    "gravity":   ["gravity_magnet", "gravity_explosive"],
    "dealer":    ["dealer_buy_pressure", "dealer_sell_pressure", "dex_bullish", "dex_bearish"],
    "mopi":      ["mopi_bullish", "mopi_bearish", "mopi_cross"],
    "gex":       ["gex_regime"],
    "max_pain":  ["max_pain_pull", "max_pain_shift"],
}


def _classify_regimes(gex_near: float, iv_rank: Optional[float], dex: Optional[float]) -> List[str]:
    """Retourne toutes les étiquettes de régime applicables à cet état de marché."""
    tags = []

    # Régime gamma primaire (mutuellement exclusifs)
    if gex_near > GEX_NEUTRAL_THRESHOLD:
        tags.append("Positive_Gamma")
    elif gex_near < -GEX_NEUTRAL_THRESHOLD:
        tags.append("Negative_Gamma")
        # Sous-régime Panic : GEX négatif + IV élevée + DEX extrême baissier
        if (iv_rank is not None and iv_rank > 70
                and dex is not None and dex > 2000):
            tags.append("Panic")
    else:
        tags.append("Neutral")

    # Overlay volatilité (additif)
    if iv_rank is not None:
        if iv_rank > 60:
            tags.append("Vol_Expansion")
        elif iv_rank < 40:
            tags.append("Vol_Contraction")

    return tags


def _load_history_snapshots(db_path: str, cutoff_ts: int) -> List[Dict]:
    """Charge les snapshots depuis options_history.db pour le lookup de régime."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, iv_rank, gex, dex FROM metrics_history WHERE ts >= ? ORDER BY ts",
            (cutoff_ts,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"[regime_segmentation] load_history: {e}")
        return []


def _find_nearest_snapshot(
    snapshots: List[Dict], ts_epoch: float, max_delta: int = 7200
) -> Optional[Dict]:
    """Trouve le snapshot le plus proche d'un timestamp (tolérance ±max_delta sec)."""
    if not snapshots:
        return None
    best = min(snapshots, key=lambda s: abs(float(s["ts"]) - ts_epoch))
    if abs(float(best["ts"]) - ts_epoch) <= max_delta:
        return best
    return None


def _direction_adjusted_return(outcome: float, direction: Optional[str]) -> float:
    """Ajuste le return en fonction de la direction du signal."""
    if direction == "DOWN":
        return -outcome   # short signal : BTC baisse = gain
    if direction == "UP":
        return outcome    # long signal : BTC monte = gain
    return abs(outcome)   # directionnel inconnu : mouvement brut


def _compute_stats(returns: List[float]) -> Optional[Dict]:
    """Calcule EV, winrate, profit_factor sur une liste de returns ajustés."""
    n = len(returns)
    if n == 0:
        return None

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    winrate = round(len(wins) / n * 100, 1)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

    ev = round(
        (len(wins) / n) * avg_win - (len(losses) / n) * avg_loss, 3
    )

    total_gain = sum(wins)
    total_loss = abs(sum(losses))
    profit_factor = (
        round(total_gain / total_loss, 2) if total_loss > 0 else None
    )

    grade = _grade(ev, n)

    return {
        "n":             n,
        "ev":            ev,
        "winrate":       winrate,
        "profit_factor": profit_factor,
        "avg_win":       round(avg_win, 3),
        "avg_loss":      round(avg_loss, 3),
        "grade":         grade,
        "insufficient":  n < 30,
    }


def _grade(ev: float, n: int) -> str:
    if n < 30:
        return "INSUFFICIENT DATA"
    if ev > 4.0 and n >= 50:
        return "A"
    if ev > 2.0:
        return "B"
    if ev > 0.0:
        return "C"
    if ev > -2.0:
        return "D"
    return "F"


def _pick_outcome(record: Dict) -> Optional[float]:
    """Retourne l'outcome le plus fiable disponible (priorité 72h > 24h > 4h)."""
    for key in ("outcome_72h", "outcome_24h", "outcome_4h"):
        v = record.get(key)
        if v is not None:
            return float(v)
    return None


def compute_regime_performance(days: int = 30) -> Dict:
    """
    Moteur principal — lit event_store.jsonl, tague chaque outcome par régime,
    calcule EV/winrate/PF par (event_type × régime) et par (engine × régime).

    Retourne :
      regimes          — liste des régimes avec descriptions
      event_types      — liste des event_types présents
      matrix           — {event_type: {regime: stats}}
      engine_matrix    — {engine_family: {regime: stats}}
      insights         — découvertes clés (meilleur régime par engine)
      meta             — N événements, période, warnings
    """
    cutoff = int(time.time()) - days * 86400
    snapshots = _load_history_snapshots(DB_PATH, cutoff_ts=cutoff)

    if not EVENT_LOG_PATH.exists():
        return {
            "status": "NO_DATA",
            "message": "event_store.jsonl introuvable — le système démarre.",
            "regimes": REGIMES,
            "matrix": {},
            "engine_matrix": {},
            "insights": [],
            "meta": {"n_events": 0, "days": days, "warnings": []},
        }

    # ── 1. Chargement des events finalisés ───────────────────────────────────
    events: List[Dict] = []
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    n_skipped = 0

    try:
        with open(EVENT_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue

                # Filtre fenêtre temporelle
                try:
                    ts_dt = datetime.fromisoformat(rec.get("ts", ""))
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                    if ts_dt < cutoff_dt:
                        continue
                    ts_epoch = ts_dt.timestamp()
                except Exception:
                    n_skipped += 1
                    continue

                # Outcome requis
                outcome = _pick_outcome(rec)
                if outcome is None:
                    n_skipped += 1
                    continue

                # Régime : cherche dans snapshots DB, fallback sur gex_near de l'event
                snap = _find_nearest_snapshot(snapshots, ts_epoch)
                if snap:
                    gex_val = float(snap.get("gex") or rec.get("gex_near") or 0)
                    iv_rank = snap.get("iv_rank")
                    dex_val = snap.get("dex")
                    if iv_rank is not None:
                        iv_rank = float(iv_rank)
                    if dex_val is not None:
                        dex_val = float(dex_val)
                else:
                    # Fallback : gex_near depuis l'event lui-même, iv/dex inconnus
                    gex_val = float(rec.get("gex_near") or 0)
                    iv_rank = None
                    dex_val = None

                regime_tags = _classify_regimes(gex_val, iv_rank, dex_val)
                adjusted = _direction_adjusted_return(outcome, rec.get("direction"))

                events.append({
                    "event_type":   rec.get("event_type", "unknown"),
                    "direction":    rec.get("direction"),
                    "outcome":      outcome,
                    "adj_return":   adjusted,
                    "regime_tags":  regime_tags,
                    "hit_target":   rec.get("hit_target"),
                    "invalidated":  rec.get("invalidated"),
                    "gex_near":     gex_val,
                    "iv_rank":      iv_rank,
                })
    except Exception as e:
        log.error(f"[regime_segmentation] compute: {e}")

    n_events = len(events)
    if n_events == 0:
        return {
            "status": "ACCUMULATING",
            "message": f"Aucun event finalisé dans les {days} derniers jours. Accumulation en cours.",
            "regimes": REGIMES,
            "matrix": {},
            "engine_matrix": {},
            "insights": [],
            "meta": {
                "n_events": 0,
                "n_skipped": n_skipped,
                "days": days,
                "warnings": [f"{n_skipped} événements ignorés (outcome manquant ou trop anciens)"],
            },
        }

    # ── 2. Construction de la matrice par event_type × régime ────────────────
    # buckets[event_type][regime] = [adj_return, ...]
    from collections import defaultdict
    buckets: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    event_types_seen: set = set()
    regimes_seen: Dict[str, int] = defaultdict(int)

    for ev in events:
        etype = ev["event_type"]
        event_types_seen.add(etype)
        for regime in ev["regime_tags"]:
            buckets[etype][regime].append(ev["adj_return"])
            regimes_seen[regime] += 1

    # ── 3. Matrice event_type × régime ───────────────────────────────────────
    matrix: Dict[str, Dict] = {}
    for etype, regime_data in buckets.items():
        matrix[etype] = {}
        for regime, returns in regime_data.items():
            matrix[etype][regime] = _compute_stats(returns)

    # ── 4. Matrice engine × régime (agrégation par famille) ──────────────────
    engine_buckets: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for ev in events:
        etype = ev["event_type"]
        engine = _event_type_to_engine(etype)
        if engine is None:
            continue
        for regime in ev["regime_tags"]:
            engine_buckets[engine][regime].append(ev["adj_return"])

    engine_matrix: Dict[str, Dict] = {}
    for engine, regime_data in engine_buckets.items():
        engine_matrix[engine] = {}
        for regime, returns in regime_data.items():
            engine_matrix[engine][regime] = _compute_stats(returns)

    # ── 5. Insights — découvertes clés ───────────────────────────────────────
    insights = _build_insights(engine_matrix)

    # ── 6. Distribution des régimes ──────────────────────────────────────────
    regime_distribution = {
        r: {"count": regimes_seen.get(r, 0)}
        for r in REGIMES
    }

    warnings = []
    if n_skipped > n_events * 0.5:
        warnings.append(
            f"{n_skipped} événements ignorés (outcome manquant) — "
            "augmenter la fenêtre ou attendre 72h."
        )
    if not snapshots:
        warnings.append(
            "options_history.db introuvable — régime classifié uniquement depuis gex_near "
            "(iv_rank et dex non disponibles)."
        )

    return {
        "status": "OK",
        "regimes": REGIMES,
        "event_types": sorted(event_types_seen),
        "matrix":       matrix,
        "engine_matrix": engine_matrix,
        "insights":     insights,
        "regime_distribution": regime_distribution,
        "meta": {
            "n_events":    n_events,
            "n_skipped":   n_skipped,
            "days":        days,
            "n_snapshots_db": len(snapshots),
            "warnings":    warnings,
        },
    }


def _event_type_to_engine(event_type: str) -> Optional[str]:
    """Retourne le nom de la famille moteur pour un event_type donné."""
    for engine, etypes in ENGINE_FAMILIES.items():
        if event_type in etypes:
            return engine
    return None


def _build_insights(engine_matrix: Dict[str, Dict]) -> List[Dict]:
    """Génère les insights clés : meilleur régime par engine, régimes à éviter."""
    insights = []

    for engine, regime_data in engine_matrix.items():
        valid = {
            r: s for r, s in regime_data.items()
            if s and not s.get("insufficient") and s.get("ev") is not None
        }
        if not valid:
            continue

        best_regime = max(valid, key=lambda r: valid[r]["ev"])
        worst_regime = min(valid, key=lambda r: valid[r]["ev"])
        best = valid[best_regime]
        worst = valid[worst_regime]

        # Signal : diff EV significative entre meilleur et pire régime
        ev_spread = best["ev"] - worst["ev"]
        if ev_spread > 0.5:
            insights.append({
                "engine":       engine,
                "type":         "regime_dependency",
                "best_regime":  best_regime,
                "worst_regime": worst_regime,
                "ev_best":      best["ev"],
                "ev_worst":     worst["ev"],
                "ev_spread":    round(ev_spread, 3),
                "message": (
                    f"{engine.upper()} : EV {best['ev']:+.2f}% en {best_regime} "
                    f"vs {worst['ev']:+.2f}% en {worst_regime} "
                    f"(spread {ev_spread:.2f}%)"
                ),
            })

        # Signal Panic si la performance chute dramatiquement
        if "Panic" in valid and valid["Panic"]["ev"] < -1.0:
            insights.append({
                "engine":  engine,
                "type":    "panic_regime_warning",
                "regime":  "Panic",
                "ev":      valid["Panic"]["ev"],
                "message": (
                    f"{engine.upper()} perd de l'edge en régime Panic "
                    f"(EV {valid['Panic']['ev']:+.2f}%) — désactiver en crash."
                ),
            })

    insights.sort(key=lambda x: x.get("ev_spread", 0), reverse=True)
    return insights


def generate_regime_report(data: Dict) -> str:
    """Génère un rapport texte lisible pour Telegram/logs."""
    if data.get("status") != "OK":
        return f"📊 REGIME PERFORMANCE\n{data.get('message', 'Données insuffisantes.')}"

    lines = [
        "📊 REGIME PERFORMANCE MATRIX",
        f"N={data['meta']['n_events']} events | {data['meta']['days']}j",
        "",
    ]

    engine_matrix = data.get("engine_matrix", {})
    regimes = list(REGIMES.keys())

    for engine, regime_data in sorted(engine_matrix.items()):
        lines.append(f"── {engine.upper()} ──")
        for regime in regimes:
            s = regime_data.get(regime)
            if not s or s.get("insufficient"):
                continue
            ev = s["ev"]
            wr = s["winrate"]
            pf = s.get("profit_factor")
            n = s["n"]
            pf_str = f" | PF={pf:.2f}" if pf else ""
            grade = s.get("grade", "?")
            lines.append(
                f"  {regime:<18} [{grade}] EV {ev:+.2f}% | WR {wr:.0f}% | N={n}{pf_str}"
            )
        lines.append("")

    if data.get("insights"):
        lines.append("🔍 INSIGHTS CLÉS")
        for ins in data["insights"][:5]:
            lines.append(f"  • {ins['message']}")

    return "\n".join(lines)
