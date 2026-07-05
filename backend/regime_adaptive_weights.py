"""
Regime Adaptive Weights — Conviction Score V2 (observe mode).

Lit les performances par régime (regime_segmentation.py) et propose
des poids ajustés par moteur selon le régime de marché actuel.

Mode actuel : observe (calcule les poids proposés, ne les applique pas).

Modes disponibles :
  observe  → calcule les poids proposés, ne touche pas au moteur principal
  shadow   → applique à un moteur challenger uniquement (futur)
  active   → applique au moteur principal après validation (futur)

Caps d'ajustement :
  N < 5        → ajustement minimal max ±2% (signal très bruité)
  N < 100      → ajustement léger max ±5%
  N 100+       → ajustement possible max ±15%
  EV négatif   → réduire poids
  EV positif   → augmenter poids
  PF < 1       → réduire poids
  PF > 1.2     → augmenter poids
  N = 0        → bloqué (aucun événement finalisé)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .regime_segmentation import (
    ENGINE_FAMILIES,
    REGIMES,
    _classify_regimes,
    compute_regime_performance,
)

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

AdaptiveMode = str  # "observe" | "shadow" | "active"
ADAPTIVE_WEIGHTS_MODE: AdaptiveMode = "observe"

# Poids de base pour chaque moteur (contribution unitaire au conviction score)
BASE_WEIGHTS: Dict[str, float] = {
    "squeeze":  1.0,
    "walls":    1.0,
    "gravity":  1.0,
    "dealer":   1.0,
    "mopi":     1.0,
    "gex":      1.0,
    "max_pain": 1.0,
}

# Caps d'ajustement
_N_MINIMAL_THRESHOLD = 5   # N < 5  → max ±2% (signal très bruité)
_N_LIGHT_THRESHOLD   = 100 # N < 100 → max ±5%
_MAX_DELTA_MINIMAL = 0.02  # ±2%
_MAX_DELTA_LIGHT   = 0.05  # ±5%
_MAX_DELTA_FULL    = 0.15  # ±15% pour N ≥ 100

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class WeightProposal:
    engine:             str
    regime:             str
    base_weight:        float
    proposed_weight:    float
    delta:              float            # proposed - base (0.0 si blocked)
    theoretical_delta:  float            # delta théorique si N suffisant (pour observabilité)
    n:                  int
    ev:                 Optional[float]
    winrate:            Optional[float]
    profit_factor:      Optional[float]
    applied:            bool             # True seulement si mode "active"
    blocked:            bool             # True si N insuffisant ou EV indisponible
    block_reason:       Optional[str]
    explanation:        str              # phrase humaine lisible


@dataclass
class AdaptiveWeightsReport:
    mode:                AdaptiveMode
    current_regime:      List[str]
    proposals:           List[WeightProposal]
    summary:             Dict[str, float]   # engine → poids proposé pour régime actuel
    regime_distribution: Dict
    meta:                Dict


# ── Lecture état de marché actuel ────────────────────────────────────────────

def _get_current_market_state() -> Dict:
    """Lit l'état de marché le plus récent depuis options_history.db."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT gex, iv_rank, dex FROM metrics_history ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return {
                "gex":     float(row["gex"] or 0),
                "iv_rank": float(row["iv_rank"]) if row["iv_rank"] is not None else None,
                "dex":     float(row["dex"])     if row["dex"]     is not None else None,
            }
    except Exception as e:
        log.warning(f"[adaptive_weights] get_current_state: {e}")
    return {"gex": 0, "iv_rank": None, "dex": None}


# ── Calcul de l'ajustement ────────────────────────────────────────────────────

def _compute_weight_adjustment(
    n: int,
    ev: Optional[float],
    profit_factor: Optional[float],
) -> Tuple[float, float, bool, Optional[str]]:
    """
    Retourne (delta, theoretical_delta, blocked, block_reason).
    delta             = ajustement appliqué (0.0 si EV absent).
    theoretical_delta = identique à delta (conservé pour compatibilité API).
    """
    if ev is None:
        return 0.0, 0.0, True, "EV indisponible"

    if n < _N_MINIMAL_THRESHOLD:
        max_delta = _MAX_DELTA_MINIMAL
    elif n < _N_LIGHT_THRESHOLD:
        max_delta = _MAX_DELTA_LIGHT
    else:
        max_delta = _MAX_DELTA_FULL

    ev_signal = max(-1.0, min(1.0, ev / 4.0))

    pf_signal = 0.0
    if profit_factor is not None:
        pf_signal = max(-1.0, min(1.0, (profit_factor - 1.0) / 0.5))

    combined = 0.7 * ev_signal + 0.3 * pf_signal
    delta = round(max(-max_delta, min(max_delta, combined * max_delta)), 4)

    return delta, delta, False, None


def _build_explanation(
    engine:        str,
    regime:        str,
    n:             int,
    ev:            Optional[float],
    winrate:       Optional[float],
    profit_factor: Optional[float],
    delta:         float,
    blocked:       bool,
    block_reason:  Optional[str],
    proposed:      float,
) -> str:
    if blocked:
        return f"{engine} | {regime} → BLOQUÉ ({block_reason})"

    if delta > 0:
        direction = f"↑ augmenté {delta:+.1%}"
    elif delta < 0:
        direction = f"↓ réduit {delta:+.1%}"
    else:
        direction = "→ inchangé"

    ev_str = f"EV={ev:+.2f}%" if ev is not None else "EV=n/a"
    wr_str = f"WR={winrate:.0f}%" if winrate is not None else "WR=n/a"
    pf_str = f"PF={profit_factor:.2f}" if profit_factor is not None else "PF=n/a"

    return (
        f"{engine} | {regime} → poids {proposed:.3f} ({direction}) "
        f"| {ev_str} | {wr_str} | {pf_str} | N={n}"
    )


# ── Moteur principal ──────────────────────────────────────────────────────────

def compute_adaptive_weights(days: int = 30) -> AdaptiveWeightsReport:
    """
    Calcule les poids adaptatifs par régime pour chaque moteur.

    En mode "observe" : calcule et retourne les propositions sans modifier
    le moteur de conviction principal.
    """
    # 1. Régime actuel
    state = _get_current_market_state()
    current_regimes = _classify_regimes(
        state["gex"], state["iv_rank"], state["dex"]
    )

    # 2. Performances par régime
    perf_data = compute_regime_performance(days=days)
    engine_matrix = perf_data.get("engine_matrix", {})
    regime_distribution = perf_data.get("regime_distribution", {})

    proposals: List[WeightProposal] = []

    for engine, base_weight in BASE_WEIGHTS.items():
        regime_data = engine_matrix.get(engine, {})

        for regime in REGIMES:
            stats = regime_data.get(regime)

            if not stats:
                proposals.append(WeightProposal(
                    engine=engine,
                    regime=regime,
                    base_weight=base_weight,
                    proposed_weight=base_weight,
                    delta=0.0,
                    theoretical_delta=0.0,
                    n=0,
                    ev=None,
                    winrate=None,
                    profit_factor=None,
                    applied=False,
                    blocked=True,
                    block_reason="Aucune donnée pour ce couple moteur × régime",
                    explanation=f"{engine} | {regime} → BLOQUÉ (aucune donnée)",
                ))
                continue

            n  = stats.get("n", 0)
            ev = stats.get("ev")
            wr = stats.get("winrate")
            pf = stats.get("profit_factor")

            delta, theoretical_delta, blocked, block_reason = _compute_weight_adjustment(n, ev, pf)
            proposed = round(base_weight + delta, 4) if not blocked else base_weight

            explanation = _build_explanation(
                engine, regime, n, ev, wr, pf,
                delta, blocked, block_reason, proposed,
            )

            proposals.append(WeightProposal(
                engine=engine,
                regime=regime,
                base_weight=base_weight,
                proposed_weight=proposed,
                delta=delta,
                theoretical_delta=theoretical_delta,
                n=n,
                ev=ev,
                winrate=wr,
                profit_factor=pf,
                # "active" uniquement en mode active et si non bloqué
                applied=(ADAPTIVE_WEIGHTS_MODE == "active" and not blocked),
                blocked=blocked,
                block_reason=block_reason,
                explanation=explanation,
            ))

    # 3. Résumé : poids proposés pour les régimes ACTUELS uniquement
    summary: Dict[str, float] = {}
    for p in proposals:
        if p.regime in current_regimes and p.engine not in summary:
            summary[p.engine] = p.proposed_weight if not p.blocked else p.base_weight

    return AdaptiveWeightsReport(
        mode=ADAPTIVE_WEIGHTS_MODE,
        current_regime=current_regimes,
        proposals=proposals,
        summary=summary,
        regime_distribution=regime_distribution,
        meta={
            "days":        days,
            "n_events":    perf_data.get("meta", {}).get("n_events", 0),
            "perf_status": perf_data.get("status", "UNKNOWN"),
            "state":       state,
        },
    )


# ── Formatage de la réponse API ───────────────────────────────────────────────

def format_adaptive_weights_report(report: AdaptiveWeightsReport) -> Dict:
    """
    Formate le rapport AdaptiveWeightsReport pour l'endpoint
    /api/regime_adaptive_weights.

    Retourne :
      mode             — observe / shadow / active
      current_regime   — régimes détectés sur l'état actuel
      summary          — poids proposés pour les régimes actuels (par moteur)
      weight_table     — tableau complet {régime: {moteur: {...}}}
      stats            — nombre de propositions bloquées / augmentées / réduites
      regime_distribution
      meta
    """
    # Tableau indexé par régime puis par moteur
    table: Dict[str, Dict] = {}
    for p in report.proposals:
        if p.regime not in table:
            table[p.regime] = {}
        table[p.regime][p.engine] = {
            "base_weight":        p.base_weight,
            "proposed_weight":    p.proposed_weight if not p.blocked else p.base_weight,
            "delta":              p.delta if not p.blocked else 0.0,
            "theoretical_delta":  p.theoretical_delta,   # visible même si bloqué
            "n":                  p.n,
            "ev":                 p.ev,
            "winrate":            p.winrate,
            "profit_factor":      p.profit_factor,
            "blocked":            p.blocked,
            "block_reason":       p.block_reason,
            "explanation":        p.explanation,
            "applied":            p.applied,
        }

    n_blocked   = sum(1 for p in report.proposals if p.blocked)
    n_increased = sum(1 for p in report.proposals if not p.blocked and p.delta > 0)
    n_decreased = sum(1 for p in report.proposals if not p.blocked and p.delta < 0)
    n_unchanged = len(report.proposals) - n_blocked - n_increased - n_decreased

    return {
        "mode":           report.mode,
        "current_regime": report.current_regime,
        "summary":        report.summary,
        "weight_table":   table,
        "stats": {
            "total_proposals": len(report.proposals),
            "blocked":         n_blocked,
            "increased":       n_increased,
            "decreased":       n_decreased,
            "unchanged":       n_unchanged,
        },
        "regime_distribution": report.regime_distribution,
        "meta":           report.meta,
    }
