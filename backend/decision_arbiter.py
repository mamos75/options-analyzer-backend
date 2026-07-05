"""
Decision Arbiter — Moteur supérieur qui tranche entre tous les signaux.

Rôle : produire UNE seule conclusion actionnelle et expliquer les signaux ignorés.

Pipeline :
  1. Vérifier la fraîcheur des données (data_health)
  2. Vérifier les contradictions (narrative)
  3. Vérifier l'edge statistique (arena stats)
  4. Dégrader les moteurs faibles
  5. Produire la décision finale
  6. Expliquer les exclusions
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ArbiteredDecision:
    verdict: str              # "NO_TRADE" | "OBSERVE" | "SIGNAL_UP" | "SIGNAL_DOWN"
    confidence: str           # "FAIBLE" | "MODÉRÉ" | "ÉLEVÉ"
    confidence_pct: int       # 0-100
    phrase: str               # phrase de décision lisible
    signals_used: list        # signaux actifs ayant contribué
    signals_ignored: list     # signaux exclus + raison
    contradictions: list      # contradictions détectées
    data_quality: str         # "OK" | "DEGRADED" | "INSUFFICIENT"
    arena_status: str         # "NO_EDGE" | "COLLECTING" | "LEADER_CLEAR"
    arena_leader: Optional[str]
    arena_leader_wr: Optional[float]
    arena_leader_ev: Optional[float]
    generated_at: str
    # P0.5 — Statut global lisible par le dashboard (un seul mot, une seule couleur).
    # TRADEABLE   : signal convergent + confiance ≥ 60 + données OK
    # OBSERVE     : signal présent mais non convergent ou confiance faible
    # CONFLICT    : contradictions non résolues entre indicateurs
    # DEGRADED    : données stale ou calibration dégradée
    # OFFLINE     : données insuffisantes ou erreur collecte
    system_status: str        # "TRADEABLE" | "OBSERVE" | "CONFLICT" | "DEGRADED" | "OFFLINE"


def compute_decision(
    narrative_data: dict,
    arena_data: Optional[dict] = None,
    health_data: Optional[dict] = None,
    mopi_score: float = 50.0,
    mopi_n_outcomes: int = 0,
    flip_use_in_signal: bool = True,
    gex_use_in_signal: bool = True,
    dex_use_in_signal: bool = True,
) -> ArbiteredDecision:
    """
    Arbitrage final entre tous les signaux disponibles.

    Règles de dégradation :
    - MOPI N < 30 → contexte seulement, ne contribue pas à la décision
    - Neural WR < 45% → ignoré (LAB ONLY)
    - flip_use_in_signal=False → aucune conclusion basée sur flip
    - Contradictions non résolues → cap confiance à 40%
    - Data stale/insufficient → NO_TRADE automatique

    Règle de décision :
    - 0 signal actif → NO_TRADE
    - 1 signal actif → OBSERVE
    - 2+ signaux convergents sans contradiction → SIGNAL_UP ou SIGNAL_DOWN
    - 2+ signaux divergents → NO_TRADE
    """
    signals_used = []
    signals_ignored = []
    contradictions = narrative_data.get("contradictions", [])

    # ── 1. Qualité des données ──────────────────────────────────────────────
    data_quality = "OK"
    data_stale = narrative_data.get("data_stale", False)
    if data_stale:
        data_quality = "DEGRADED"

    if health_data:
        live_outcomes = health_data.get("live_outcomes", 0)
        if live_outcomes < 10:
            data_quality = "INSUFFICIENT"

    # ── 2. Collecter les signaux actifs et les raisons d'exclusion ─────────
    range_mode = narrative_data.get("range_mode", False)
    asymmetric_side = narrative_data.get("asymmetric_side", "NEUTRAL")

    # GEX
    if gex_use_in_signal:
        gex_regime = narrative_data.get("gex_regime", "NEUTRE")
        if gex_regime == "AMPLIFICATEUR":
            signals_used.append({
                "name": "GEX",
                "direction": "AMPLIFICATEUR",
                "detail": "Régime amplificateur actif",
                "weight": "élevé",
            })
        elif gex_regime == "STABILISANT":
            signals_used.append({
                "name": "GEX",
                "direction": "RANGE",
                "detail": "Régime stabilisant — compression",
                "weight": "élevé",
            })
    else:
        signals_ignored.append({
            "name": "GEX",
            "reason": "GEX dormant ou structurel — stock gamma inactif",
        })

    # DEX
    if dex_use_in_signal:
        dex_dir = narrative_data.get("dex_direction", "NEUTRAL")
        if dex_dir in ("BULLISH_FLOWS", "BEARISH_FLOWS"):
            signals_used.append({
                "name": "DEX",
                "direction": "UP" if dex_dir == "BULLISH_FLOWS" else "DOWN",
                "detail": f"Flux dealers : {dex_dir}",
                "weight": "élevé",
            })
    else:
        signals_ignored.append({
            "name": "DEX",
            "reason": "DEX structurel ou dormant — pas de flux exploitable",
        })

    # MOPI
    if mopi_n_outcomes >= 30 and (mopi_score > 70 or mopi_score < 30):
        direction = "UP" if mopi_score > 70 else "DOWN"
        signals_used.append({
            "name": "MOPI",
            "direction": direction,
            "detail": f"MOPI {mopi_score:.0f}/100 — signal extrême (N={mopi_n_outcomes})",
            "weight": "modéré",
        })
    else:
        reason = f"MOPI {mopi_score:.0f}/100 — non extrême ou N insuffisant ({mopi_n_outcomes} outcomes)"
        signals_ignored.append({"name": "MOPI", "reason": reason})

    # Flip
    if not flip_use_in_signal:
        signals_ignored.append({
            "name": "Flip Level",
            "reason": "flip_level=None (all_gamma_negative) ou niveau dormant — non confirmé comme déclencheur",
        })

    # Neural engines — toujours ignorés tant que non validés
    signals_ignored.append({
        "name": "Neural (MLP + GRU)",
        "reason": "LAB ONLY — validation insuffisante. Non utilisés dans la décision.",
    })

    # ── 3. Edge statistique Arena ───────────────────────────────────────────
    arena_status = "COLLECTING"
    arena_leader = None
    arena_leader_wr = None
    arena_leader_ev = None

    if arena_data:
        health = arena_data.get("arena_health") or arena_data
        live_oc = health.get("live_outcomes", 0) or 0
        lb = health.get("leaderboard_detail", {}) or {}

        if live_oc >= 100:
            best_mn, best_wr = None, 0.0
            for mn, info in lb.items():
                if mn in ("neural_tabular_engine", "temporal_neural_engine"):
                    continue
                wr = info.get("winrate") or 0.0
                n = info.get("live_outcomes", 0) or 0
                if n >= 30 and wr > best_wr:
                    best_wr = wr
                    best_mn = mn
                    arena_leader_ev = info.get("ev")

            if best_mn and best_wr > 0.52:
                arena_status = "LEADER_CLEAR"
                arena_leader = best_mn
                arena_leader_wr = round(best_wr, 3)
            else:
                arena_status = "NO_EDGE"
        else:
            arena_status = "COLLECTING"

    # ── 4. Décision finale ──────────────────────────────────────────────────
    n_signals = len(signals_used)
    up_signals   = [s for s in signals_used if s.get("direction") in ("UP", "AMPLIFICATEUR")]
    down_signals = [s for s in signals_used if s.get("direction") in ("DOWN", "AMPLIFICATEUR")]
    range_signals = [s for s in signals_used if s.get("direction") == "RANGE"]

    # Contradictions non résolues = confiance plafonnée
    has_contradiction = len(contradictions) > 0

    if data_quality == "INSUFFICIENT":
        verdict = "NO_TRADE"
        phrase = "Données insuffisantes — attendre la collecte d'un historique exploitable."
        confidence_pct = 0
    elif n_signals == 0:
        verdict = "NO_TRADE"
        phrase = "Aucun signal actif — tous les indicateurs sont dormants ou non extrêmes."
        confidence_pct = 5
    elif range_signals and not up_signals and not down_signals:
        verdict = "OBSERVE"
        phrase = f"Régime stabilisant — BTC en range. Attendre une cassure confirmée."
        confidence_pct = 40
    elif up_signals and not down_signals and not has_contradiction:
        verdict = "SIGNAL_UP"
        names = " + ".join(s["name"] for s in up_signals)
        phrase = f"Signal haussier convergent ({names}). Entrée si confirmation volume."
        confidence_pct = min(70, 40 + len(up_signals) * 15)
    elif down_signals and not up_signals and not has_contradiction:
        verdict = "SIGNAL_DOWN"
        names = " + ".join(s["name"] for s in down_signals)
        phrase = f"Signal baissier convergent ({names}). Entrée si confirmation volume."
        confidence_pct = min(70, 40 + len(down_signals) * 15)
    elif has_contradiction:
        verdict = "OBSERVE"
        phrase = f"Signaux contradictoires ({len(contradictions)} contradiction(s)) — attendre résolution."
        confidence_pct = 20
    else:
        verdict = "OBSERVE"
        phrase = "Signaux mixtes ou insuffisants — pas d'edge directionnel clair."
        confidence_pct = 15

    # Arena dégrade si NO_EDGE
    if arena_status == "NO_EDGE":
        confidence_pct = min(confidence_pct, 30)
    elif arena_status == "LEADER_CLEAR" and arena_leader_wr and arena_leader_wr > 0.55:
        confidence_pct = min(100, confidence_pct + 10)

    if confidence_pct >= 60:
        confidence = "ÉLEVÉ"
    elif confidence_pct >= 35:
        confidence = "MODÉRÉ"
    else:
        confidence = "FAIBLE"

    # ── P0.5 — Calcul system_status ────────────────────────────────────────
    if data_quality == "INSUFFICIENT":
        system_status = "OFFLINE"
    elif data_quality == "DEGRADED":
        system_status = "DEGRADED"
    elif len(contradictions) > 0 and verdict == "OBSERVE":
        system_status = "CONFLICT"
    elif verdict in ("SIGNAL_UP", "SIGNAL_DOWN") and confidence_pct >= 60:
        system_status = "TRADEABLE"
    else:
        system_status = "OBSERVE"

    return ArbiteredDecision(
        verdict=verdict,
        confidence=confidence,
        confidence_pct=confidence_pct,
        phrase=phrase,
        signals_used=signals_used,
        signals_ignored=signals_ignored,
        contradictions=contradictions,
        data_quality=data_quality,
        arena_status=arena_status,
        arena_leader=arena_leader,
        arena_leader_wr=arena_leader_wr,
        arena_leader_ev=arena_leader_ev,
        generated_at=datetime.now(timezone.utc).isoformat(),
        system_status=system_status,
    )
