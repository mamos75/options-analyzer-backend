"""
Decision Arbiter — Moteur supérieur qui tranche entre tous les signaux.

Rôle : produire UNE seule conclusion actionnelle et expliquer les signaux ignorés.

Pipeline :
  1. Vérifier la fraîcheur des données (data_health)
  2. Vérifier les contradictions (narrative)
  3. Vérifier l'edge statistique (arena stats)
  4. Intégrer le régime VEX/CEX (V5)
  5. Dégrader les moteurs faibles
  6. Produire la décision finale
  7. Expliquer les exclusions

V5 — Fusion VEX/CEX Regime :
  Le régime VEX/CEX (regime_id depuis regime_vexcex_engine) est un nouvel input.
  La table de verdicts est indexée par regime_id + contexte (phase, urgency, arena).
  Règles de dégradation spécifiques au régime :
    - NEU-* : confiance plafonnée à 20%
    - COMP-0 CRITIQUE : confiance dégradée si PE "edge insuffisant"
    - EXP-*-1 + arena LEADER_CLEAR : boost de confiance +15
    - FL-0 CRITIQUE : verdict forcé OBSERVE (flip zone = pas directionnel)
"""

from __future__ import annotations

# ── MOPI seuils centralisés ───────────────────────────────────────────────────
MOPI_SIGNAL_HIGH = 70   # score > MOPI_SIGNAL_HIGH → signal UP
MOPI_SIGNAL_LOW  = 30   # score < MOPI_SIGNAL_LOW  → signal DOWN
# Note : backtest.py utilise 65/35 — logique séparée, ne pas modifier ici.

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
    # V5 — régime VEX/CEX
    vexcex_regime_id: Optional[str] = None   # ex. "EXP-UP-1", "NEU-0", "COMP-0"
    vexcex_phase: Optional[str] = None       # ex. "EXP", "NEU", "COMP"
    vexcex_urgency: Optional[str] = None     # ex. "CRITIQUE", "ÉLEVÉE", "NEUTRE"
    vexcex_label: Optional[str] = None       # ex. "EXPANSION HAUSSIÈRE"
    vexcex_contribution: Optional[str] = None  # "BOOST" | "DEGRADED" | "NEUTRAL" | "BLOCKED"
    # F8.3 — Vocabulaire desk premium
    state: str = "RAS"    # "RAS" | "TENSION" | "CONFLIT" | "ZONE_CRITIQUE"
    action: str = "OBSERVER"  # "OBSERVER" | "PRÉPARER" | "AGIR_LONG" | "AGIR_SHORT"


# ── Table de verdicts VEX/CEX par regime_id ───────────────────────────────
#
# Format : regime_id → dict de modificateurs :
#   confidence_delta  : ajout/retrait de confiance_pct (+15, -10, etc.)
#   confidence_cap    : plafond confiance_pct (None = pas de cap)
#   force_verdict     : forcer un verdict précis (None = pas de forçage)
#   direction_hint    : "UP" | "DOWN" | "RANGE" | None (indice directionnel)
#   contribution      : "BOOST" | "DEGRADED" | "NEUTRAL" | "BLOCKED"
#
# Priorités :
#   1. force_verdict (absolu)
#   2. confidence_cap
#   3. confidence_delta
#   4. direction_hint (influence signal selection)

_REGIME_VERDICT_TABLE: dict = {
    # ── NEU : zone morte — dégradation systématique
    "NEU-0": {
        "confidence_cap": 20,
        "confidence_delta": -10,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "DEGRADED",
    },
    "NEU-1": {
        "confidence_cap": 25,
        "confidence_delta": -5,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "DEGRADED",
    },
    # ── FL : zone de flip — observation requise
    "FL-0": {
        "confidence_cap": 30,
        "confidence_delta": 0,
        "force_verdict": "OBSERVE",   # flip critique → jamais directionnel
        "direction_hint": None,
        "contribution": "BLOCKED",
    },
    "FL-1": {
        "confidence_cap": 40,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    },
    "COMP-6": {
        "confidence_cap": 35,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    },
    # ── EXP : expansion directionnelle
    "EXP-UP-1": {
        "confidence_cap": None,
        "confidence_delta": +15,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "EXP-DOWN-1": {
        "confidence_cap": None,
        "confidence_delta": +15,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    "EXP-UP-0": {
        "confidence_cap": None,
        "confidence_delta": +8,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "EXP-DOWN-0": {
        "confidence_cap": None,
        "confidence_delta": +8,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    # ── FB : feedback loop
    "FB-UP": {
        "confidence_cap": None,
        "confidence_delta": +10,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "FB-DOWN": {
        "confidence_cap": None,
        "confidence_delta": +10,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    # ── COMP : compression / squeeze
    "COMP-0": {
        "confidence_cap": 40,
        "confidence_delta": -5,
        "force_verdict": None,
        "direction_hint": None,       # pas de direction — attendre résolution
        "contribution": "DEGRADED",
    },
    # ── DIV : divergence
    "DIV-0": {
        "confidence_cap": 35,
        "confidence_delta": -10,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "DEGRADED",
    },
    # ── MOD : modéré
    "MOD-UP": {
        "confidence_cap": None,
        "confidence_delta": +5,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "MOD-DOWN": {
        "confidence_cap": None,
        "confidence_delta": +5,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    "MOD-MIX": {
        "confidence_cap": 45,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    },
}

# Règle : si regime urgency = CRITIQUE et phase = COMP → dégradation supplémentaire
_COMP_CRITIQUE_EXTRA_CAP = 30


def _get_regime_modifiers(
    regime_id: Optional[str],
    urgency: Optional[str],
    phase: Optional[str],
) -> dict:
    """Retourne les modificateurs pour le regime_id donné."""
    if not regime_id:
        return {
            "confidence_cap": None,
            "confidence_delta": 0,
            "force_verdict": None,
            "direction_hint": None,
            "contribution": "NEUTRAL",
        }
    mods = dict(_REGIME_VERDICT_TABLE.get(regime_id, {
        "confidence_cap": None,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    }))
    # Dégradation supplémentaire : COMP CRITIQUE
    if phase == "COMP" and urgency == "CRITIQUE":
        mods["confidence_cap"] = min(
            mods.get("confidence_cap") or 100,
            _COMP_CRITIQUE_EXTRA_CAP,
        )
    return mods


def compute_decision(
    narrative_data: dict,
    arena_data: Optional[dict] = None,
    health_data: Optional[dict] = None,
    mopi_score: float = 50.0,
    mopi_n_outcomes: int = 0,
    flip_use_in_signal: bool = True,
    gex_use_in_signal: bool = True,
    dex_use_in_signal: bool = True,
    # V5 — régime VEX/CEX
    vexcex_regime_id: Optional[str] = None,
    vexcex_phase: Optional[str] = None,
    vexcex_urgency: Optional[str] = None,
    vexcex_label: Optional[str] = None,
) -> ArbiteredDecision:
    """
    Arbitrage final entre tous les signaux disponibles.

    Règles de dégradation :
    - MOPI N < 30 → contexte seulement, ne contribue pas à la décision
    - Neural WR < 45% → ignoré (LAB ONLY)
    - flip_use_in_signal=False → aucune conclusion basée sur flip
    - Contradictions non résolues → cap confiance à 40%
    - Data stale/insufficient → NO_TRADE automatique
    - NEU-* VEX/CEX → confiance plafonnée à 20% (V5)
    - FL-0 → verdict forcé OBSERVE (V5)
    - EXP-*-1 → boost +15 confiance (V5)

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
            # F8.8 — Enrichir le detail avec dex_activity_label si dispo
            _dex_lbl = narrative_data.get("dex_activity_label", "")
            _dex_ctx_used = narrative_data.get("dex_activity_context", "")
            _dex_detail = f"Flux dealers : {dex_dir}"
            if _dex_lbl:
                _dex_detail = f"Flux dealers : {dex_dir} ({_dex_lbl})"
            signals_used.append({
                "name": "DEX",
                "direction": "UP" if dex_dir == "BULLISH_FLOWS" else "DOWN",
                "detail": _dex_detail,
                "weight": "élevé",
            })
    else:
        # F8.8 — Raison explicite depuis dex_activity_context (narrative_resolver)
        _dex_ctx = narrative_data.get("dex_activity_context")
        signals_ignored.append({
            "name": "DEX",
            "reason": _dex_ctx or "DEX structurel ou dormant — pas de flux exploitable",
        })

    # MOPI — F12 validation : edge 24h (WR 79% high, 89% low, Wilson LB >0.72)
    #         Pas d'edge 4h (WR 44% sur N=177, Wilson LB 0.375 < 0.50)
    #         → signal qualifié horizon 24h uniquement
    if mopi_n_outcomes >= 30 and (mopi_score > MOPI_SIGNAL_HIGH or mopi_score < MOPI_SIGNAL_LOW):
        direction = "UP" if mopi_score > MOPI_SIGNAL_HIGH else "DOWN"
        signals_used.append({
            "name": "MOPI",
            "direction": direction,
            "detail": f"MOPI {mopi_score:.0f}/100 — signal extrême horizon 24h (WR validé 79%+, N={mopi_n_outcomes})",
            "weight": "modéré",
            "horizon": "24h",
        })
    else:
        # Deux cas distincts pour ne pas mentir sur la raison réelle
        if mopi_n_outcomes < 30:
            reason = f"MOPI {mopi_score:.0f}/100 — historique insuffisant ({mopi_n_outcomes} snapshots < 30 requis)"
        else:
            reason = f"MOPI {mopi_score:.0f}/100 — dans la zone neutre (signal extrême requis : >{MOPI_SIGNAL_HIGH} ou <{MOPI_SIGNAL_LOW})"
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

    # V5 — VEX/CEX regime signal
    regime_mods = _get_regime_modifiers(vexcex_regime_id, vexcex_urgency, vexcex_phase)
    if vexcex_regime_id and vexcex_phase not in ("NEU",):
        dir_hint = regime_mods.get("direction_hint")
        contribution = regime_mods.get("contribution", "NEUTRAL")
        signals_used.append({
            "name": f"VEX/CEX [{vexcex_regime_id}]",
            "direction": dir_hint or "RANGE",
            "detail": vexcex_label or vexcex_regime_id,
            "weight": "modéré" if contribution == "BOOST" else "faible",
        })
    elif vexcex_regime_id:
        signals_ignored.append({
            "name": f"VEX/CEX [{vexcex_regime_id}]",
            "reason": f"Régime {vexcex_phase} — zone morte ou signaux faibles. Non contributif.",
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

    # V5 — force_verdict prend priorité absolue
    forced_verdict = regime_mods.get("force_verdict")

    if data_quality == "INSUFFICIENT":
        verdict = "NO_TRADE"
        phrase = "Données insuffisantes — attendre la collecte d'un historique exploitable."
        confidence_pct = 0
    elif forced_verdict:
        verdict = forced_verdict
        _display_verdict = "OBSERVER" if forced_verdict == "OBSERVE" else forced_verdict
        phrase = f"Régime VEX/CEX {vexcex_regime_id} ({vexcex_label}) — {_display_verdict} imposé."
        confidence_pct = 30
    elif n_signals == 0:
        verdict = "NO_TRADE"
        phrase = "Aucun signal actif — tous les indicateurs sont dormants ou non extrêmes."
        confidence_pct = 5
    elif range_signals and not up_signals and not down_signals:
        verdict = "OBSERVE"
        phrase = "Régime stabilisant — BTC en range. Attendre une cassure confirmée."
        confidence_pct = 40
    elif up_signals and not down_signals and not has_contradiction:
        verdict = "SIGNAL_UP"
        names = " + ".join(s["name"] for s in up_signals)
        # F9.5 — trigger réel : cassure du premier niveau de l'échelle
        phrase = f"Signal haussier convergent ({names}). Attendre cassure confirmée du premier niveau upside."
        confidence_pct = min(70, 40 + len(up_signals) * 15)
    elif down_signals and not up_signals and not has_contradiction:
        verdict = "SIGNAL_DOWN"
        names = " + ".join(s["name"] for s in down_signals)
        # F9.5 — trigger réel
        phrase = f"Signal baissier convergent ({names}). Attendre cassure confirmée du premier niveau downside."
        confidence_pct = min(70, 40 + len(down_signals) * 15)
    elif has_contradiction:
        verdict = "OBSERVE"
        phrase = f"Contradiction : {contradictions[0]['detail']}" if contradictions else "Signaux contradictoires — attendre résolution."  # F8.3
        confidence_pct = 20
    else:
        verdict = "OBSERVE"
        phrase = "Signaux mixtes ou insuffisants — pas d'edge directionnel clair."
        confidence_pct = 15

    # ── 5. Modifieurs de confiance ─────────────────────────────────────────
    # Arena
    if arena_status == "NO_EDGE":
        confidence_pct = min(confidence_pct, 30)
    elif arena_status == "LEADER_CLEAR" and arena_leader_wr and arena_leader_wr > 0.55:
        confidence_pct = min(100, confidence_pct + 10)

    # V5 — VEX/CEX regime modifiers (seulement si pas forced_verdict déjà appliqué)
    if not forced_verdict:
        delta = regime_mods.get("confidence_delta", 0)
        cap = regime_mods.get("confidence_cap")
        confidence_pct = confidence_pct + delta
        if cap is not None:
            confidence_pct = min(confidence_pct, cap)

    confidence_pct = max(0, min(100, confidence_pct))

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

    # F8.3 — Calcul state + action depuis verdict + contradictions + confidence
    if data_quality == "INSUFFICIENT":
        state = "RAS"
    elif verdict == "NO_TRADE":
        state = "RAS"
    elif verdict == "OBSERVE" and len(contradictions) > 0:
        state = "CONFLIT"
    elif verdict == "OBSERVE":
        state = "TENSION"
    elif confidence_pct < 40:
        state = "ZONE_CRITIQUE"
    else:
        state = "TENSION"

    # F9.5 — AGIR_* exige ≥2 sources directionnelles convergentes ; 1 source = PRÉPARER + raison
    if verdict in ("SIGNAL_UP", "SIGNAL_DOWN"):
        _dir_signals = up_signals if verdict == "SIGNAL_UP" else down_signals
        _n_dir = len(_dir_signals)
        if confidence_pct >= 60 and _n_dir >= 2:
            action = "AGIR_LONG" if verdict == "SIGNAL_UP" else "AGIR_SHORT"
        elif _n_dir == 1:
            action = "PRÉPARER"
            _solo_name = _dir_signals[0]["name"]
            phrase = phrase + f" ({_solo_name} seul — attendre 2e confirmation)"
        else:
            action = "PRÉPARER"
    else:
        action = "OBSERVER"

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
        vexcex_regime_id=vexcex_regime_id,
        vexcex_phase=vexcex_phase,
        vexcex_urgency=vexcex_urgency,
        vexcex_label=vexcex_label,
        vexcex_contribution=regime_mods.get("contribution"),
        state=state,
        action=action,
    )
