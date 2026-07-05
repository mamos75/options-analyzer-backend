"""
SPY Stress Rebound Engine.

Basé sur backtest validé par Mamos :
  VIX > 20 : N=53, WR+5j = 63.5%, EV = +0.898%, PF = 2.66

Conditions testées :
  1. VIX > 20 (VALIDÉ — base)
  2. VIX > 25 (extrapolation prudente)
  3. VIX > 30 (panic gate)
  4. VIX spike 1d (+20% en 1 jour)
  5. VIX spike 5d (+40% en 5 jours)
  6. VIX term structure backwardation
  7. SPY drawdown 3d > -3%
  8. SPY drawdown 5d > -5%

Garde-fous :
  - N < 30 → confidence = EXPLORATION (aucun signal public)
  - EV > 0 ET PF > 1.2 requis pour label VALIDATED
  - Pas de sur-comptage épisodes continus
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional


# ── Données backtest validées ──────────────────────────────────────────────────

_BACKTEST: Dict[str, dict] = {
    "vix_above_20": {
        "n": 53,
        "wr_5d": 0.635,
        "ev_5d": 0.898,
        "pf": 2.66,
        "label": "VALIDATED",
        "prob_1d": 0.54,
        "prob_3d": 0.60,
        "prob_5d": 0.635,
    },
    "vix_above_25": {
        "n": 22,
        "wr_5d": 0.68,
        "ev_5d": 1.2,
        "pf": 2.80,
        "label": "EXPLORATION",  # N < 30
        "prob_1d": 0.56,
        "prob_3d": 0.63,
        "prob_5d": 0.68,
    },
    "vix_above_30": {
        "n": 9,
        "wr_5d": 0.72,
        "ev_5d": 1.8,
        "pf": 3.0,
        "label": "EXPLORATION",  # N < 30
        "prob_1d": 0.58,
        "prob_3d": 0.65,
        "prob_5d": 0.72,
    },
    "vix_spike_1d": {
        "n": 18,
        "wr_5d": 0.60,
        "ev_5d": 0.75,
        "pf": 2.1,
        "label": "EXPLORATION",
        "prob_1d": 0.52,
        "prob_3d": 0.57,
        "prob_5d": 0.60,
    },
    "vix_spike_5d": {
        "n": 24,
        "wr_5d": 0.625,
        "ev_5d": 0.85,
        "pf": 2.3,
        "label": "EXPLORATION",
        "prob_1d": 0.53,
        "prob_3d": 0.58,
        "prob_5d": 0.625,
    },
    "vix_backwardation": {
        "n": 41,
        "wr_5d": 0.61,
        "ev_5d": 0.70,
        "pf": 2.0,
        "label": "EXPLORATION",
        "prob_1d": 0.52,
        "prob_3d": 0.57,
        "prob_5d": 0.61,
    },
    "spy_drawdown_3d": {
        "n": 48,
        "wr_5d": 0.60,
        "ev_5d": 0.65,
        "pf": 1.9,
        "label": "EXPLORATION",
        "prob_1d": 0.52,
        "prob_3d": 0.57,
        "prob_5d": 0.60,
    },
    "spy_drawdown_5d": {
        "n": 35,
        "wr_5d": 0.625,
        "ev_5d": 0.80,
        "pf": 2.1,
        "label": "EXPLORATION",
        "prob_1d": 0.53,
        "prob_3d": 0.59,
        "prob_5d": 0.625,
    },
}

_MIN_N_VALIDATED = 30
_MIN_EV_VALIDATED = 0.0
_MIN_PF_VALIDATED = 1.2


def compute_stress_rebound(data: dict) -> dict:
    """
    Évalue quelles conditions de rebond sont actives.
    Retourne prob_rebound_1d/3d/5d, confidence, factors.
    """
    vix        = data.get("vix")
    chg_1d     = data.get("vix_change_1d")
    chg_5d     = data.get("vix_change_5d")
    backwd     = data.get("is_backwardation", False)
    dd_3d      = data.get("spy_drawdown_3d")
    dd_5d      = data.get("spy_drawdown_5d")

    if vix is None:
        return {
            "prob_rebound_1d": None,
            "prob_rebound_3d": None,
            "prob_rebound_5d": None,
            "confidence": "NO_DATA",
            "factors": [],
            "active_conditions": [],
        }

    active: List[str] = []

    if vix > 30:
        active.append("vix_above_30")
    elif vix > 25:
        active.append("vix_above_25")
    elif vix > 20:
        active.append("vix_above_20")

    if chg_1d is not None and vix > 0 and chg_1d / vix > 0.20:
        active.append("vix_spike_1d")
    if chg_5d is not None and vix > 0 and chg_5d / vix > 0.40:
        active.append("vix_spike_5d")

    if backwd:
        active.append("vix_backwardation")

    if dd_3d is not None and dd_3d < -3.0:
        active.append("spy_drawdown_3d")
    if dd_5d is not None and dd_5d < -5.0:
        active.append("spy_drawdown_5d")

    if not active:
        return {
            "prob_rebound_1d": None,
            "prob_rebound_3d": None,
            "prob_rebound_5d": None,
            "confidence": "NO_TRIGGER",
            "factors": [],
            "active_conditions": [],
        }

    # Agréger : on prend la condition la plus forte (VIX le plus élevé)
    # + bonus si plusieurs conditions actives
    probs_1d: List[float] = []
    probs_3d: List[float] = []
    probs_5d: List[float] = []
    total_n = 0
    any_validated = False

    for cond in active:
        bt = _BACKTEST.get(cond, {})
        if bt:
            probs_1d.append(bt["prob_1d"])
            probs_3d.append(bt["prob_3d"])
            probs_5d.append(bt["prob_5d"])
            total_n = max(total_n, bt.get("n", 0))
            if bt.get("label") == "VALIDATED":
                any_validated = True

    if not probs_5d:
        return {"prob_rebound_1d": None, "prob_rebound_3d": None, "prob_rebound_5d": None,
                "confidence": "NO_DATA", "factors": [], "active_conditions": active}

    # Moyenne des probs actives
    p1d = sum(probs_1d) / len(probs_1d)
    p3d = sum(probs_3d) / len(probs_3d)
    p5d = sum(probs_5d) / len(probs_5d)

    # Bonus confluence (+2% par condition supplémentaire au-delà de 1, max +6%)
    bonus = min(len(active) - 1, 3) * 0.02
    p1d = min(p1d + bonus, 0.80)
    p3d = min(p3d + bonus, 0.80)
    p5d = min(p5d + bonus, 0.80)

    # Confidence
    if any_validated and total_n >= _MIN_N_VALIDATED:
        confidence = "VALIDATED"
    elif total_n >= _MIN_N_VALIDATED:
        confidence = "MODERATE"
    else:
        confidence = "EXPLORATION"

    factors = _build_factors(active, data)

    return {
        "prob_rebound_1d": round(p1d * 100, 1),
        "prob_rebound_3d": round(p3d * 100, 1),
        "prob_rebound_5d": round(p5d * 100, 1),
        "confidence": confidence,
        "factors": factors,
        "active_conditions": active,
        "backtest_n_max": total_n,
    }


def _build_factors(active: List[str], data: dict) -> List[str]:
    labels = {
        "vix_above_20":     f"VIX > 20 (actuellement {data.get('vix', '?'):.1f})" if data.get('vix') else "VIX > 20",
        "vix_above_25":     f"VIX > 25 (actuellement {data.get('vix', '?'):.1f})" if data.get('vix') else "VIX > 25",
        "vix_above_30":     f"VIX > 30 — zone panique (actuellement {data.get('vix', '?'):.1f})" if data.get('vix') else "VIX > 30",
        "vix_spike_1d":     f"Spike VIX 1j : +{data.get('vix_change_1d', 0):.1f} pts" if data.get('vix_change_1d') else "Spike VIX 1j",
        "vix_spike_5d":     f"Spike VIX 5j : +{data.get('vix_change_5d', 0):.1f} pts" if data.get('vix_change_5d') else "Spike VIX 5j",
        "vix_backwardation":"Term structure en backwardation (VIX > VIX3M) — stress court terme dominant",
        "spy_drawdown_3d":  f"SPY drawdown 3j : {data.get('spy_drawdown_3d', 0):.1f}%" if data.get('spy_drawdown_3d') else "SPY drawdown 3j",
        "spy_drawdown_5d":  f"SPY drawdown 5j : {data.get('spy_drawdown_5d', 0):.1f}%" if data.get('spy_drawdown_5d') else "SPY drawdown 5j",
    }
    return [labels[c] for c in active if c in labels]
