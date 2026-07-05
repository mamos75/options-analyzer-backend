"""
Indicator Accuracy — Score 0-100 par indicateur sur les N derniers jours.

Mappe les event_types de l'event_store vers les indicateurs du dashboard :
  gex      ← pas d'event_type direct (proxy via conviction_score / signal)
  dex      ← dealer_buy_pressure + dealer_sell_pressure
  gravity  ← gravity_magnet
  walls    ← wall_breakout + wall_rejection
  max_pain ← pas d'event_type direct (proxy via backtest)
  squeeze  ← squeeze_bullish + squeeze_bearish
  mopi     ← mopi_bullish + mopi_bearish

Le score = winrate_pct pondéré (hit/total) × 100.
Si données insuffisantes (N < 5) → None.
"""

from typing import Dict, Optional
from .event_store import get_event_store

_MIN_N = 5  # minimum de signaux pour afficher un score

_INDICATOR_GROUPS: Dict[str, list] = {
    "dex":      ["dealer_buy_pressure", "dealer_sell_pressure", "dex_bearish", "dex_bullish"],
    "gravity":  ["gravity_magnet", "gravity_explosive"],
    "walls":    ["wall_breakout", "wall_rejection"],
    "squeeze":  ["squeeze_bullish", "squeeze_bearish"],
    "mopi":     ["mopi_bullish", "mopi_bearish", "mopi_cross"],
    "gex":      ["gex_regime"],
    "max_pain": ["max_pain_pull", "max_pain_shift"],
}


def _merge_groups(raw_stats: dict, group_event_types: list) -> Optional[dict]:
    """Agrège plusieurs event_types en un score combiné."""
    total = 0
    hit   = 0
    inv   = 0
    o4    = []
    o24   = []
    o72   = []

    for etype in group_event_types:
        s = raw_stats.get(etype)
        if not s:
            continue
        total += s.get("total", 0)
        hit   += s.get("hit",   0)
        inv   += s.get("invalidated", 0)
        if s.get("avg_outcome_4h") is not None:
            o4.append(s["avg_outcome_4h"])
        if s.get("avg_outcome_24h") is not None:
            o24.append(s["avg_outcome_24h"])
        if s.get("avg_outcome_72h") is not None:
            o72.append(s["avg_outcome_72h"])

    if total < _MIN_N:
        return None

    return {
        "total":          total,
        "hit":            hit,
        "invalidated":    inv,
        "winrate_pct":    round(hit / total * 100, 1),
        "score":          round(hit / total * 100),
        "avg_outcome_4h":  round(sum(o4)  / len(o4),  2) if o4  else None,
        "avg_outcome_24h": round(sum(o24) / len(o24), 2) if o24 else None,
        "avg_outcome_72h": round(sum(o72) / len(o72), 2) if o72 else None,
    }


def compute_indicator_accuracy(days: int = 30) -> dict:
    """
    Retourne un dict structuré :
    {
      "days": 30,
      "scores": {
        "squeeze": 81,
        "dex":     74,
        "mopi":    68,
        "walls":   37,
        "gravity": 41,
        ...
      },
      "details": { ... stats complètes par indicateur ... },
      "signal_moyen": 72,
      "pending": 3,
    }
    Scores None si données insuffisantes.
    """
    es         = get_event_store()
    raw_stats  = es.get_accuracy_by_event_type(days=days)
    pending    = es.get_pending_count()

    details: Dict[str, Optional[dict]] = {}
    scores:  Dict[str, Optional[int]]  = {}

    for indicator, etypes in _INDICATOR_GROUPS.items():
        merged = _merge_groups(raw_stats, etypes)
        details[indicator] = merged
        scores[indicator]  = merged["score"] if merged else None

    valid_scores = [s for s in scores.values() if s is not None]
    signal_moyen = round(sum(valid_scores) / len(valid_scores)) if valid_scores else None

    # ── Scores preview 4h (pending events avec checked_4h) ───────────────────
    raw_4h = es.get_intermediate_accuracy_4h(min_n=5)
    scores_4h: Dict[str, Optional[dict]] = {}
    for indicator, etypes in _INDICATOR_GROUPS.items():
        merged_4h = None
        total_4h, hits_4h, avgs_4h = 0, 0, []
        for etype in etypes:
            s = raw_4h.get(etype)
            if not s:
                continue
            total_4h += s["n"]
            hits_4h  += s["hit"]
            avgs_4h.append(s["avg_outcome_4h"])
        if total_4h >= 5:
            wr = round(hits_4h / total_4h * 100, 1)
            merged_4h = {
                "n": total_4h,
                "winrate_4h_pct": wr,
                "score_4h": round(wr),
                "avg_outcome_4h": round(sum(avgs_4h) / len(avgs_4h), 2) if avgs_4h else None,
                "preview": True,
            }
        scores_4h[indicator] = merged_4h

    return {
        "days":         days,
        "scores":       scores,
        "scores_4h":    scores_4h,
        "details":      details,
        "signal_moyen": signal_moyen,
        "pending":      pending,
        "note": (
            "Données insuffisantes — les scores arrivent après 72h par signal."
            if not valid_scores else None
        ),
    }
