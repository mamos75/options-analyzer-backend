"""
Probability Engine Phase A — Moteur probabiliste par règles expertes pondérées.

Architecture:
  • Chaque règle : poids | justification | qualité_données | pénalité si contradiction
  • Base = 50% pour chaque scénario
  • Probabilité clampée [5%, 95%]
  • Confiance = métrique SÉPARÉE de la probabilité

Séparation fondamentale:
  Probabilité = scénario le plus probable (directionnelle)
  Confiance   = qualité du signal (couverture données + consensus)

  Exemple : Baisse 24h 64% / Confiance 52%
  → le scénario baisse domine, mais le signal reste moyen.

Seuils confiance (affichage dashboard):
  < 40%    → EDGE INSUFFISANT / NEUTRE
  40-60%   → SIGNAL FAIBLE / À SURVEILLER
  60-75%   → SIGNAL VALIDE
  ≥ 75%    → SIGNAL FORT

Phase B (après historique propre) : validation statistique par règle.
Phase C (après validation) : ML = couche de calibration, pas boîte noire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

_BASE_PROBABILITY = 50
_PROB_MIN = 5
_PROB_MAX = 95
_BULL_PROB_CAP_CRASH_REGIME = 55.0  # V2: BULL capped à 55% en crash régime

_CONF_INSUFFICIENT = 40
_CONF_WEAK = 60
_CONF_VALID = 75

_ENGINE_VERSION = "Phase-A/rules-expert-v2"
_DISCLAIMER = (
    "Moteur Phase A — règles expertes pondérées. "
    "Probabilités = estimations conditionnelles, pas prédictions certaines. "
    "Phase B (validation statistique historique) requise pour calibration réelle. "
    "Confiance = complétude des données, PAS une probabilité de succès. "
    "Signal fort ≠ edge validé — toujours vérifier Validation historique."
)

# ══════════════════════════════════════════════════════════════════════════════
# MODULE RELIABILITY — pondération par backtest historique
# ══════════════════════════════════════════════════════════════════════════════

# Préfixes de rule_id → module accuracy (depuis indicator_accuracy.py)
_RULE_ID_PREFIXES: list = [
    ("dex_",             "dex"),
    ("gex_near_",        "gex"),
    ("gex_amplifier_",   "gex"),
    ("gex_stabilisant_", "gex"),
    ("gex_momentum_",    "gex"),
    ("spot_below_flip_", "gex"),
    ("spot_above_flip_", "gex"),
    ("spot_far_",        "gex"),
    ("flip_",            "gex"),
    ("max_pain_",        "max_pain"),
    ("put_wall_",        "walls"),
    ("call_wall_",       "walls"),

    ("iv_",              "iv_rank"),
    ("pcr_near_",        "pcr"),
    ("puts_skew_",       "pcr"),
    ("calls_skew_",      "pcr"),
    ("funding_",         "dex"),
    ("futures_",         "dex"),
]


def _module_for_rule(rule_id: str) -> str:
    for prefix, module in _RULE_ID_PREFIXES:
        if rule_id.startswith(prefix):
            return module
    return "unknown"


def _reliability_factor_from_score(accuracy_score: Optional[float]) -> float:
    """module_weight_effective = weight_base × reliability_factor.

    None (N < min_n) → 1.0  Phase A : pas encore de données, confiance totale
    Grade A/B (≥60)  → 1.0
    Grade C  (≥40)   → 0.75
    Grade D  (≥20)   → 0.50
    Grade F  (<20)   → 0.25
    """
    if accuracy_score is None:
        return 1.0
    if accuracy_score >= 60:
        return 1.0
    if accuracy_score >= 40:
        return 0.75
    if accuracy_score >= 20:
        return 0.50
    return 0.25


def _grade_from_score(accuracy_score: Optional[float]) -> str:
    if accuracy_score is None:
        return "N/A"
    if accuracy_score >= 80:
        return "A"
    if accuracy_score >= 60:
        return "B"
    if accuracy_score >= 40:
        return "C"
    if accuracy_score >= 20:
        return "D"
    return "F"


def _historical_validation_label(accuracy_score: Optional[float]) -> str:
    """Niveau de validation historique lisible — pour affichage dashboard."""
    if accuracy_score is None:
        return "en accumulation"
    if accuracy_score >= 60:
        return "forte"
    if accuracy_score >= 40:
        return "moyenne"
    return "faible"


def _scale_rules_by_reliability(
    rules: List[ProbabilityRule],
    module_factors: dict,
) -> List[ProbabilityRule]:
    """Applique module_weight_effective = weight_base × reliability_factor.

    Si factor == 1.0 → règle inchangée.
    Si factor < 1.0  → weight et pts_applied réduits proportionnellement.
    """
    scaled = []
    for r in rules:
        module = _module_for_rule(r.id)
        factor = module_factors.get(module, 1.0)
        if factor == 1.0:
            scaled.append(r)
            continue
        new_weight = max(0, round(r.weight * factor))
        if r.data_quality == "unavailable":
            new_pts = 0
        elif r.triggered:
            sign = 1 if r.pts_applied >= 0 else -1
            new_pts = new_weight * sign
        else:
            new_pts = 0
        grade = _grade_from_score(None)  # label générique — score non disponible ici
        scaled.append(ProbabilityRule(
            id=r.id,
            description=r.description,
            justification=r.justification,
            weight=new_weight,
            pts_applied=new_pts,
            triggered=r.triggered,
            data_quality=r.data_quality,
            condition_detail=f"{r.condition_detail} [fiabilité ×{factor:.2f}]",
        ))
    return scaled


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ProbabilityRule:
    id: str
    description: str        # phrase courte — dashboard
    justification: str      # pourquoi ce facteur
    weight: int             # poids nominal ≥ 0 (pts potentiels)
    pts_applied: int        # pts réels (+bear/-penalty, 0 si non triggered ou unavailable)
    triggered: bool
    data_quality: str       # "high" | "medium" | "low" | "unavailable"
    condition_detail: str   # valeur observée (ex: "GEX near = -$450M")


@dataclass
class ScenarioProbability:
    scenario: str           # "BEAR_4H" | "BULL_4H" | "BEAR_24H" | "BULL_24H" | "BEAR_72H" | "BULL_72H"
    horizon: str
    direction: str          # "BEAR" | "BULL"
    probability: float      # % clampé [5, 95]
    confidence: float       # % 0-100 — complétude données + consensus (PAS une proba de succès)
    confidence_label: str   # "Signal règles fort/valide/faible/données insuffisantes"
    edge_label: str         # "EDGE INSUFFISANT" | "SIGNAL RÈGLES FAIBLE" | "SIGNAL RÈGLES VALIDE" | "SIGNAL RÈGLES FORT"
    signal_label: str       # ex: "Baisse 24h : 64%  |  Complétude : 52%  |  Validation : faible"
    base_probability: int   # toujours 50
    raw_pts: int            # pts nets avant clamp
    positive_rules: List[ProbabilityRule]
    penalty_rules: List[ProbabilityRule]
    rules_triggered_count: int
    rules_unavailable_count: int
    top_contributors: List[str]     # top 3 règles les plus impactantes
    data_coverage_pct: float        # % du poids couvert par données disponibles
    # V2 — Crash Regime Gate
    crash_regime_active: bool = False
    crash_regime_warning: str = ""
    max_pain_weight_applied: int = -1  # -1 = non applicable, 0 = ignoré, >0 = poids effectif
    # V2 — Terminologie rigoureuse (point 6)
    data_completeness: float = -1.0         # alias de confidence (complétude données)
    historical_validation: str = "en accumulation"  # "en accumulation" | "faible" | "moyenne" | "forte"
    edge_reel: str = "non confirmé"          # "non confirmé" | "partiellement confirmé" | "confirmé"
    conclusion_line: str = ""                # phrase de décision courte
    reliability_factors_applied: bool = False  # True si des facteurs < 1.0 ont été appliqués


@dataclass
class ProbabilityEngineOutput:
    spot: float
    timestamp: str
    bear_4h: ScenarioProbability
    bull_4h: ScenarioProbability
    bear_24h: ScenarioProbability
    bull_24h: ScenarioProbability
    bear_72h: ScenarioProbability
    bull_72h: ScenarioProbability
    dominant_scenario: str      # scénario avec proba la plus éloignée de 50
    dominant_probability: float
    dominant_confidence: float
    interpretation: str         # phrase synthèse humaine
    engine_version: str
    disclaimer: str
    # V2 — Crash Regime Gate
    crash_regime_active: bool = False
    crash_regime_warning: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _rule(
    rule_id: str,
    description: str,
    justification: str,
    weight: int,
    triggered: bool,
    data_quality: str,
    condition_detail: str,
    pts_sign: int = 1,          # +1 → ajoute si triggered | -1 → soustrait si triggered
) -> ProbabilityRule:
    if data_quality == "unavailable":
        pts = 0
    elif triggered:
        pts = weight * pts_sign
    else:
        pts = 0
    return ProbabilityRule(
        id=rule_id,
        description=description,
        justification=justification,
        weight=weight,
        pts_applied=pts,
        triggered=triggered,
        data_quality=data_quality,
        condition_detail=condition_detail,
    )


def _compute_confidence(
    positive_rules: List[ProbabilityRule],
    penalty_rules: List[ProbabilityRule],
    direction: str,     # "BEAR" | "BULL"
) -> float:
    """Confiance = f(couverture données × consensus directionnel).

    Deux composantes distinctes:
      data_coverage  : % du poids total couvert par données disponibles (high/medium/low)
      signal_consensus: % des règles disponibles alignées avec le scénario dominant

    confidence = 0.65 × data_coverage + 0.35 × signal_consensus

    Qualité pondérée: high=1.0, medium=0.75, low=0.5, unavailable=0.0
    """
    all_rules = positive_rules + penalty_rules
    if not all_rules:
        return 0.0

    quality_weights = {"high": 1.0, "medium": 0.75, "low": 0.5, "unavailable": 0.0}

    total_nominal_weight = sum(r.weight for r in all_rules)
    if total_nominal_weight == 0:
        return 0.0

    # Data coverage: somme pondérée par qualité / total nominal
    covered_weight = sum(
        r.weight * quality_weights[r.data_quality]
        for r in all_rules
    )
    data_coverage = covered_weight / total_nominal_weight

    # Signal consensus: parmi les règles déclenchées avec données disponibles,
    # quelle fraction va dans le sens de la proba finale?
    triggered_available = [
        r for r in all_rules
        if r.triggered and r.data_quality != "unavailable"
    ]
    if not triggered_available:
        signal_consensus = 0.5  # ni pour ni contre
    else:
        # Règles positives déclenche = alignées sur le scénario (BEAR ou BULL)
        # Pénalités déclenchées = contre le scénario
        aligned = [r for r in triggered_available if r.pts_applied > 0]
        against = [r for r in triggered_available if r.pts_applied < 0]
        total_ta = len(triggered_available)
        signal_consensus = len(aligned) / total_ta if total_ta > 0 else 0.5

    raw = 0.65 * data_coverage + 0.35 * signal_consensus
    return round(min(100.0, raw * 100), 1)


def _data_coverage_pct(all_rules: List[ProbabilityRule]) -> float:
    """% du poids nominal couvert par données de qualité haute ou moyenne."""
    total = sum(r.weight for r in all_rules)
    if not total:
        return 0.0
    available = sum(
        r.weight for r in all_rules
        if r.data_quality in ("high", "medium", "low")
    )
    return round(available / total * 100, 1)


def _confidence_label(conf: float) -> str:
    """Label de complétude données (anciennement 'Signal fort').

    Point 6 : "Confiance" rebaptisé "Complétude données".
    Ce score mesure la disponibilité des données + le consensus des règles.
    CE N'EST PAS une probabilité de succès du trade.
    """
    if conf < _CONF_INSUFFICIENT:
        return "Données insuffisantes"
    if conf < _CONF_WEAK:
        return "Signal règles faible"
    if conf < _CONF_VALID:
        return "Signal règles valide"
    return "Signal règles fort"


def _edge_label(conf: float) -> str:
    if conf < _CONF_INSUFFICIENT:
        return "EDGE INSUFFISANT"
    if conf < _CONF_WEAK:
        return "SIGNAL RÈGLES FAIBLE"
    if conf < _CONF_VALID:
        return "SIGNAL RÈGLES VALIDE"
    return "SIGNAL RÈGLES FORT"


def _top_contributors(
    positive_rules: List[ProbabilityRule],
    penalty_rules: List[ProbabilityRule],
    n: int = 3,
) -> List[str]:
    """Top N règles par |pts_applied| décroissant, avec signe."""
    all_rules = positive_rules + penalty_rules
    active = sorted(
        [r for r in all_rules if r.pts_applied != 0],
        key=lambda r: abs(r.pts_applied),
        reverse=True,
    )
    result = []
    for r in active[:n]:
        sign = "+" if r.pts_applied > 0 else ""
        result.append(f"{sign}{r.pts_applied}pts — {r.description}")
    return result


def _build_scenario(
    scenario: str,
    horizon: str,
    direction: str,
    positive_rules: List[ProbabilityRule],
    penalty_rules: List[ProbabilityRule],
    crash_regime: bool = False,
    max_pain_weight_applied: int = -1,
    historical_validation: str = "en accumulation",
    reliability_factors_applied: bool = False,
) -> ScenarioProbability:
    raw_pts = sum(r.pts_applied for r in positive_rules + penalty_rules)
    raw_prob = _BASE_PROBABILITY + raw_pts

    # V2 — Cap BULL à 55% en crash régime (flux dealers baissiers dominants)
    crash_regime_active = False
    crash_regime_warning = ""
    if direction == "BULL" and crash_regime:
        probability = float(min(_BULL_PROB_CAP_CRASH_REGIME, max(_PROB_MIN, raw_prob)))
        crash_regime_active = True
        crash_regime_warning = (
            "CRASH REGIME GATE actif — Probabilité BULL plafonnée à 55%. "
            "Max Pain au-dessus = attraction résiduelle non confirmée (ignorée). "
            "Flux dealers baissiers dominants + GEX AMPLIFICATEUR : "
            "Buy The Dip interdit, tout signal haussier = rebond technique possible uniquement."
        )
    else:
        probability = float(max(_PROB_MIN, min(_PROB_MAX, raw_prob)))

    conf = _compute_confidence(positive_rules, penalty_rules, direction)
    all_rules = positive_rules + penalty_rules
    coverage = _data_coverage_pct(all_rules)
    triggered = sum(1 for r in all_rules if r.triggered and r.data_quality != "unavailable")
    unavailable = sum(1 for r in all_rules if r.data_quality == "unavailable")

    dir_label = "Baisse" if direction == "BEAR" else "Hausse"
    conf_lbl = _confidence_label(conf)
    edge_lbl = _edge_label(conf)
    contributors = _top_contributors(positive_rules, penalty_rules)

    # Point 6 — nouvelle terminologie rigoureuse
    # "Confiance" = complétude données, PAS probabilité de succès
    signal_label = (
        f"Direction règles : {dir_label} {probability:.0f}%  |  "
        f"Complétude : {conf:.0f}%  |  "
        f"Validation : {historical_validation}"
    )

    # Edge réel : déterminé par validation historique (PAS par complétude données)
    edge_reel_map = {
        "en accumulation": "non confirmé (données en cours d'accumulation)",
        "faible":          "non confirmé — performance historique faible",
        "moyenne":         "partiellement confirmé",
        "forte":           "confirmé par backtest",
    }
    edge_reel = edge_reel_map.get(historical_validation, "non confirmé")

    # Conclusion — 1 phrase décision
    if edge_lbl == "EDGE INSUFFISANT":
        conclusion = "Signal trop faible — ne pas exploiter seul"
    elif historical_validation in ("faible", "en accumulation"):
        conclusion = (
            f"Biais {dir_label.lower()} par les règles — "
            "validation historique insuffisante, exploitable uniquement en confluence"
        )
    elif historical_validation == "moyenne":
        conclusion = f"Biais {dir_label.lower()} partiellement validé — surveillance requise"
    else:
        conclusion = f"Biais {dir_label.lower()} confirmé par les règles et le backtest"

    return ScenarioProbability(
        scenario=scenario,
        horizon=horizon,
        direction=direction,
        probability=probability,
        confidence=conf,
        confidence_label=conf_lbl,
        edge_label=edge_lbl,
        signal_label=signal_label,
        base_probability=_BASE_PROBABILITY,
        raw_pts=raw_pts,
        positive_rules=positive_rules,
        penalty_rules=penalty_rules,
        rules_triggered_count=triggered,
        rules_unavailable_count=unavailable,
        top_contributors=contributors,
        data_coverage_pct=coverage,
        crash_regime_active=crash_regime_active,
        crash_regime_warning=crash_regime_warning,
        max_pain_weight_applied=max_pain_weight_applied,
        data_completeness=conf,
        historical_validation=historical_validation,
        edge_reel=edge_reel,
        conclusion_line=conclusion,
        reliability_factors_applied=reliability_factors_applied,
    )


# ══════════════════════════════════════════════════════════════════════════════
# V2 — CRASH REGIME GATE
# ══════════════════════════════════════════════════════════════════════════════

def _crash_regime_active(
    dex_score: float,
    dex_direction: str,
    gex_regime: str,
) -> bool:
    """Gate crash régime — signal BULL fort interdit si flux dealers extrêmement baissiers.

    Cas réel 30 mai 2025 : 30 signaux HAUSSIER autour de $73,500 → BTC chute à $59,441.
    Cause : Max Pain $78k et Flip $71k ont surpondéré le scénario haussier
    alors que DEX = +9,147 BTC (score 27%) — flux dealers vendeurs.

    Règle : en régime AMPLIFICATEUR + DEX extrême baissier,
    Max Pain / Gravity sont des aimants conditionnels, jamais directionnels dominants.
    """
    return (
        dex_score <= 35.0
        and dex_direction == "BEARISH_FLOWS"
        and gex_regime == "AMPLIFICATEUR"
    )


def _max_pain_bull_weight(
    max_pain_dte: int,
    gex_regime: str,
    dex_direction: str,
    iv_rank: float,
    crash_regime: bool,
) -> int:
    """Poids effectif Max Pain dans les signaux BULL (V2 hiérarchie).

    Max Pain ne contribue FORTEMENT que si toutes les conditions alignées :
      1. DTE ≤ 7 jours (expiry proche — l'aimant est réel)
      2. GEX STABILISANT (dealers absorbent, pas amplifient)
      3. DEX neutre ou haussier (flux dealers pas vendeurs)
      4. IV non stressée rank < 60 (marché calme)

    En crash régime : poids = 0 (aimant résiduel ignoré).
    """
    if crash_regime:
        return 0
    dte_ok = max_pain_dte <= 7
    gex_ok = gex_regime == "STABILISANT"
    dex_ok = dex_direction in ("BULLISH_FLOWS", "NEUTRAL_FLOWS", "NEUTRAL")
    iv_ok = iv_rank < 60

    if dte_ok and gex_ok and dex_ok and iv_ok:
        return 15  # poids nominal
    elif dte_ok and (gex_ok or dex_ok) and iv_ok:
        return 6   # poids réduit — une condition manquante
    else:
        return 0   # contexte contradictoire — Max Pain ignoré


# ══════════════════════════════════════════════════════════════════════════════
# RÈGLES PAR HORIZON
# ══════════════════════════════════════════════════════════════════════════════

def _rules_bear_4h(
    gex_near: float,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    dex_actionable: float,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    call_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
    gex_near_prev: Optional[float],
) -> tuple[list, list]:
    """Règles 4h — DEX dominant, GEX = amplitude."""

    positives = [
        _rule(
            "dex_bearish_4h",
            "Résistance dealer active (BEARISH_FLOWS)",
            "Dealers short BTC pour hedger → pression mécanique baissière immédiate (4h clé)",
            weight=18,
            triggered=(dex_direction == "BEARISH_FLOWS"),
            data_quality="high",
            condition_detail=f"DEX = {dex_direction}",
        ),
        _rule(
            "gex_near_negative_4h",
            "GEX near négatif — régime amplificateur actif",
            "Dealers en position courte gamma → amplifient chaque move bas",
            weight=10,
            triggered=(gex_near < 0),
            data_quality="high",
            condition_detail=f"GEX near = {gex_near/1e6:.0f}M$",
        ),
        _rule(
            "spot_below_flip_4h",
            "Spot sous le gamma flip — régime amplificateur baissier confirmé",
            "En dessous du flip, les dealers amplifient les baisses mécaniquement",
            weight=8,
            triggered=(
                flip_level is not None
                and flip_use_in_signal
                and spot < flip_level
            ),
            data_quality="high" if (flip_level is not None and flip_use_in_signal) else "low",
            condition_detail=(
                f"Spot ${spot:,.0f} vs Flip ${flip_level:,.0f}"
                if flip_level else "Flip non disponible"
            ),
        ),
        _rule(
            "iv_spike_4h",
            "IV court terme élevée — demande de protection immédiate",
            "IV rank élevé = marché paie cher les puts → signal de peur à court terme",
            weight=5,
            triggered=(iv_rank > 70),
            data_quality="medium",
            condition_detail=f"IV rank = {iv_rank:.0f}%",
        ),
        _rule(
            "pcr_near_bearish_4h",
            "Put/Call ratio court terme élevé — skew puts",
            "Ratio puts/calls near-term > 1.5 = demande de protection forte immédiate",
            weight=4,
            triggered=(pc_ratio_near > 1.5),
            data_quality="medium",
            condition_detail=f"PCR near = {pc_ratio_near:.2f}",
        ),
        _rule(
            "gex_momentum_4h",
            "GEX near en contraction — pression amplificatrice croissante",
            "GEX qui descend = dealers short davantage → amplification baissière s'accélère",
            weight=3,
            triggered=(
                gex_near_prev is not None and gex_near < gex_near_prev
            ),
            data_quality="medium" if gex_near_prev is not None else "unavailable",
            condition_detail=(
                f"GEX near {gex_near/1e6:.0f}M$ vs précédent {gex_near_prev/1e6:.0f}M$"
                if gex_near_prev is not None else "Historique GEX non disponible"
            ),
        ),
    ]

    penalties = [
        _rule(
            "put_wall_near_support_4h",
            "Put wall support très proche (< 2%)",
            "Gros mur de puts sous le spot = dealers achètent pour hedger → freine la baisse",
            weight=10,
            triggered=(
                put_wall > 0
                and 0 < (spot - put_wall) / spot < 0.02
            ),
            data_quality="high",
            condition_detail=(
                f"Put wall ${put_wall:,.0f} ({(spot-put_wall)/spot*100:.1f}% sous spot)"
                if put_wall > 0 else "Put wall non identifié"
            ),
            pts_sign=-1,
        ),
        _rule(
            "max_pain_below_4h",
            "Max Pain sous le spot — attraction haussière 4h",
            "Max Pain < spot avec DTE ≤ 1j = attraction vers le haut imminente",
            weight=5,
            triggered=(max_pain_strike < spot * 0.995 and max_pain_dte <= 1),
            data_quality="high",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte})",
            pts_sign=-1,
        ),
        _rule(
            "dex_actionable_bullish_4h",
            "Pression DEX actionnable haussière contrarie le scénario baissier",
            "Dealers long BTC avec gros delta actionnable = soutien mécanique immédiat",
            weight=6,
            triggered=(dex_direction == "BULLISH_FLOWS" and abs(dex_actionable) > 500),
            data_quality="high",
            condition_detail=f"DEX BULLISH actionnable {dex_actionable:.0f} BTC",
            pts_sign=-1,
        ),
    ]

    return positives, penalties


def _rules_bull_4h(
    gex_near: float,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    dex_actionable: float,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    call_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
    gex_near_prev: Optional[float],
) -> tuple[list, list]:
    """Règles 4h BULL — miroir symétrique DEX-focused."""

    positives = [
        _rule(
            "dex_bullish_4h",
            "Soutien dealer actif (BULLISH_FLOWS)",
            "Dealers long BTC pour hedger → soutien mécanique haussier immédiat",
            weight=18,
            triggered=(dex_direction == "BULLISH_FLOWS"),
            data_quality="high",
            condition_detail=f"DEX = {dex_direction}",
        ),
        _rule(
            "gex_near_positive_4h",
            "GEX near positif — régime stabilisant (absorption des baisses)",
            "Dealers long gamma → absorbent les ventes, stabilisent le prix vers le haut",
            weight=8,
            triggered=(gex_near > 0),
            data_quality="high",
            condition_detail=f"GEX near = {gex_near/1e6:.0f}M$",
        ),
        _rule(
            "spot_above_flip_4h",
            "Spot au-dessus du gamma flip — régime stabilisant actif",
            "Au-dessus du flip, les dealers absorbent les ventes mécaniquement",
            weight=8,
            triggered=(
                flip_level is not None
                and flip_use_in_signal
                and spot > flip_level
            ),
            data_quality="high" if (flip_level is not None and flip_use_in_signal) else "low",
            condition_detail=(
                f"Spot ${spot:,.0f} vs Flip ${flip_level:,.0f}"
                if flip_level else "Flip non disponible"
            ),
        ),
        _rule(
            "iv_low_calm_4h",
            "IV court terme basse — marché calme, tendance haussière non contrariée",
            "IV rank faible = pas de demande de protection = biais haussier de fond",
            weight=4,
            triggered=(iv_rank < 30),
            data_quality="medium",
            condition_detail=f"IV rank = {iv_rank:.0f}%",
        ),
        _rule(
            "pcr_near_bullish_4h",
            "Put/Call ratio court terme bas — pression calls",
            "PCR near < 0.70 = demande de calls forte > puts → sentiment haussier immédiat",
            weight=4,
            triggered=(pc_ratio_near < 0.70),
            data_quality="medium",
            condition_detail=f"PCR near = {pc_ratio_near:.2f}",
        ),
        _rule(
            "gex_momentum_expansion_4h",
            "GEX near en expansion — régime stabilisant se renforce",
            "GEX qui monte (plus positif) = dealers absorbent davantage → soutien croissant",
            weight=3,
            triggered=(
                gex_near_prev is not None and gex_near > gex_near_prev and gex_near > 0
            ),
            data_quality="medium" if gex_near_prev is not None else "unavailable",
            condition_detail=(
                f"GEX near {gex_near/1e6:.0f}M$ vs précédent {gex_near_prev/1e6:.0f}M$"
                if gex_near_prev is not None else "Historique GEX non disponible"
            ),
        ),
    ]

    penalties = [
        _rule(
            "call_wall_near_resistance_4h",
            "Call wall résistance très proche (< 2%)",
            "Gros mur de calls au-dessus = dealers vendent pour hedger → freine la hausse",
            weight=10,
            triggered=(
                call_wall > spot
                and 0 < (call_wall - spot) / spot < 0.02
            ),
            data_quality="high",
            condition_detail=(
                f"Call wall ${call_wall:,.0f} ({(call_wall-spot)/spot*100:.1f}% au-dessus)"
                if call_wall > spot else "Call wall non identifié au-dessus"
            ),
            pts_sign=-1,
        ),
        _rule(
            "max_pain_above_4h",
            "Max Pain au-dessus du spot — attraction baissière 4h",
            "Max Pain > spot avec DTE ≤ 1j = attraction vers le bas imminente",
            weight=5,
            triggered=(max_pain_strike > spot * 1.005 and max_pain_dte <= 1),
            data_quality="high",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte})",
            pts_sign=-1,
        ),
        _rule(
            "dex_actionable_bearish_4h",
            "Pression DEX actionnable baissière contrarie le scénario haussier",
            "Dealers short BTC avec gros delta actionnable = résistance mécanique immédiate",
            weight=6,
            triggered=(dex_direction == "BEARISH_FLOWS" and abs(dex_actionable) > 500),
            data_quality="high",
            condition_detail=f"DEX BEARISH actionnable {dex_actionable:.0f} BTC",
            pts_sign=-1,
        ),
    ]

    return positives, penalties


def _rules_bear_24h(
    gex_near: float,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
    gex_near_prev: Optional[float],
    funding_rate: Optional[float] = None,
    futures_oi: Optional[float] = None,
    futures_oi_prev: Optional[float] = None,
    spot_volume_24h: Optional[float] = None,
    spot_volume_7d_avg: Optional[float] = None,
    spot_prev: Optional[float] = None,
) -> tuple[list, list]:
    """Règles 24h BEAR — GEX dominant, DEX = confirmation.

    Règles exactes spécifiées par Mamos :
      +12 si GEX near négatif
      +10 si spot sous gamma flip confirmé
      +8  si dealer pressure négatif
      +6  si GEX momentum en contraction
      +6  si IV short-term monte
      +5  si skew puts augmente
      +4  si futures OI monte avec prix qui baisse (unavailable)
      -8  si gros wall puts support proche
      -6  si Max Pain au-dessus avec DTE proche
      -5  si funding pas encore négatif (unavailable)
      -5  si volume spot ne confirme pas (unavailable)
      -6  si spot trop éloigné du flip après flush
    """

    positives = [
        _rule(
            "gex_near_negative_24h",
            "GEX near négatif — régime amplificateur baissier",
            "Dealers short gamma near-term → amplifient chaque move baissier. "
            "Signal le plus fiable sur 24h.",
            weight=12,
            triggered=(gex_near < 0),
            data_quality="high",
            condition_detail=f"GEX near = {gex_near/1e6:.1f}M$",
        ),
        _rule(
            "spot_below_flip_24h",
            "Spot sous le gamma flip confirmé",
            "En dessous du flip, dealers amplifient mécaniquement la baisse sur 24h",
            weight=10,
            triggered=(
                flip_level is not None
                and flip_use_in_signal
                and spot < flip_level
            ),
            data_quality="high" if (flip_level is not None and flip_use_in_signal) else "low",
            condition_detail=(
                f"Spot ${spot:,.0f} < Flip ${flip_level:,.0f}"
                if flip_level else "Flip non disponible"
            ),
        ),
        _rule(
            "dex_bearish_24h",
            "Pression dealer baissière (BEARISH_FLOWS)",
            "Dealers court BTC net → résistance directionnelle sur 24h",
            weight=12,
            triggered=(dex_direction == "BEARISH_FLOWS"),
            data_quality="high",
            condition_detail=f"DEX direction = {dex_direction}",
        ),
        _rule(
            "gex_momentum_contraction_24h",
            "GEX near en contraction — amplification baissière croissante",
            "GEX qui descend = pression de hedging amplificatrice s'accroît",
            weight=3,
            triggered=(gex_near_prev is not None and gex_near < gex_near_prev),
            data_quality="medium" if gex_near_prev is not None else "unavailable",
            condition_detail=(
                f"GEX near {gex_near/1e6:.1f}M$ ↓ vs {gex_near_prev/1e6:.1f}M$"
                if gex_near_prev is not None else "Historique GEX non disponible"
            ),
        ),
        _rule(
            "iv_rising_24h",
            "IV short-term élevée — peur croissante du marché",
            "IV rank > 70 = marché paie la protection → signal de pression baissière confirmée",
            weight=5,
            triggered=(iv_rank > 70),
            data_quality="medium",
            condition_detail=f"IV rank = {iv_rank:.0f}%",
        ),
        _rule(
            "puts_skew_24h",
            "Skew puts augmente — demande de protection baissière",
            "PCR near > 1.5 = signal de protection fort → pression baissière réelle",
            weight=4,
            triggered=(pc_ratio_near > 1.5),
            data_quality="medium",
            condition_detail=f"PCR near = {pc_ratio_near:.2f}",
        ),
    ]

    penalties = [
        _rule(
            "put_wall_near_support_24h",
            "Gros put wall support proche (< 3% sous spot)",
            "Mur de puts proche = dealers achètent BTC pour hedger si cassure → frein baissier",
            weight=8,
            triggered=(
                put_wall > 0
                and 0 < (spot - put_wall) / spot < 0.03
            ),
            data_quality="high",
            condition_detail=(
                f"Put wall ${put_wall:,.0f} ({(spot-put_wall)/spot*100:.1f}% sous spot)"
                if put_wall > 0 else "Put wall non identifié"
            ),
            pts_sign=-1,
        ),
        _rule(
            "max_pain_above_near_dte_24h",
            "Max Pain au-dessus du spot avec DTE proche",
            "Max Pain > spot avec expiry dans ≤ 3j = attraction mécanique vers le haut",
            weight=6,
            triggered=(max_pain_strike > spot * 1.005 and max_pain_dte <= 3),
            data_quality="high",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte})",
            pts_sign=-1,
        ),
        _rule(
            "spot_far_above_flip_24h",
            "Spot trop éloigné du flip après flush (> 5%)",
            "BTC très au-dessus du flip après chute = oversold probable, rebond mécanique attendu",
            weight=6,
            triggered=(
                flip_level is not None
                and flip_use_in_signal
                and spot > flip_level
                and (spot - flip_level) / spot > 0.05
            ),
            data_quality="medium" if (flip_level is not None and flip_use_in_signal) else "unavailable",
            condition_detail=(
                f"Spot ${spot:,.0f} = {(spot-flip_level)/spot*100:.1f}% au-dessus du flip ${flip_level:,.0f}"
                if flip_level else "Flip non disponible"
            ),
            pts_sign=-1,
        ),
    ]

    return positives, penalties


def _rules_bull_24h(
    gex_near: float,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    call_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
    gex_near_prev: Optional[float],
) -> tuple[list, list]:
    """Règles 24h BULL — miroir logique du scénario baissier."""

    positives = [
        _rule(
            "gex_near_positive_24h",
            "GEX near positif — régime stabilisant haussier",
            "Dealers long gamma → absorbent les ventes, tendance haussière soutenue",
            weight=12,
            triggered=(gex_near > 0),
            data_quality="high",
            condition_detail=f"GEX near = {gex_near/1e6:.1f}M$",
        ),
        _rule(
            "spot_above_flip_24h",
            "Spot au-dessus du gamma flip confirmé",
            "Au-dessus du flip, les dealers soutiennent mécaniquement sur 24h",
            weight=10,
            triggered=(
                flip_level is not None
                and flip_use_in_signal
                and spot > flip_level
            ),
            data_quality="high" if (flip_level is not None and flip_use_in_signal) else "low",
            condition_detail=(
                f"Spot ${spot:,.0f} > Flip ${flip_level:,.0f}"
                if flip_level else "Flip non disponible"
            ),
        ),
        _rule(
            "dex_bullish_24h",
            "Soutien dealer haussier (BULLISH_FLOWS)",
            "Dealers long BTC net → soutien directionnel sur 24h",
            weight=12,
            triggered=(dex_direction == "BULLISH_FLOWS"),
            data_quality="high",
            condition_detail=f"DEX direction = {dex_direction}",
        ),
        _rule(
            "gex_momentum_expansion_24h",
            "GEX near en expansion — soutien haussier croissant",
            "GEX qui monte = pression de hedging stabilisante s'accroît",
            weight=3,
            triggered=(
                gex_near_prev is not None
                and gex_near > gex_near_prev
                and gex_near > 0
            ),
            data_quality="medium" if gex_near_prev is not None else "unavailable",
            condition_detail=(
                f"GEX near {gex_near/1e6:.1f}M$ ↑ vs {gex_near_prev/1e6:.1f}M$"
                if gex_near_prev is not None else "Historique GEX non disponible"
            ),
        ),
        _rule(
            "iv_calm_24h",
            "IV basse — marché calme, pas de demande de protection",
            "IV rank < 30 = marché confiant, pression haussière de fond",
            weight=4,
            triggered=(iv_rank < 30),
            data_quality="medium",
            condition_detail=f"IV rank = {iv_rank:.0f}%",
        ),
        _rule(
            "calls_skew_24h",
            "Skew calls — demande de participation haussière",
            "PCR near < 0.70 = demande de calls forte → momentum haussier confirmé",
            weight=4,
            triggered=(pc_ratio_near < 0.70),
            data_quality="medium",
            condition_detail=f"PCR near = {pc_ratio_near:.2f}",
        ),
        _rule(
            "put_wall_strong_support_24h",
            "Put wall fort comme filet de sécurité (3-6% sous spot)",
            "Gros mur de puts à distance raisonnable = plancher de soutien mécanique",
            weight=4,
            triggered=(
                put_wall > 0
                and 0.03 < (spot - put_wall) / spot <= 0.06
            ),
            data_quality="high",
            condition_detail=(
                f"Put wall ${put_wall:,.0f} ({(spot-put_wall)/spot*100:.1f}% sous spot)"
                if put_wall > 0 else "Put wall non identifié"
            ),
        ),
    ]

    penalties = [
        _rule(
            "call_wall_near_resistance_24h",
            "Call wall résistance proche (< 3% au-dessus du spot)",
            "Gros mur de calls proche = dealers vendent pour hedger → frein haussier fort",
            weight=8,
            triggered=(
                call_wall > spot
                and 0 < (call_wall - spot) / spot < 0.03
            ),
            data_quality="high",
            condition_detail=(
                f"Call wall ${call_wall:,.0f} ({(call_wall-spot)/spot*100:.1f}% au-dessus)"
                if call_wall > spot else "Call wall non identifié au-dessus"
            ),
            pts_sign=-1,
        ),
        _rule(
            "max_pain_below_near_dte_24h",
            "Max Pain sous le spot avec DTE proche",
            "Max Pain < spot avec expiry dans ≤ 3j = attraction mécanique vers le bas",
            weight=6,
            triggered=(max_pain_strike < spot * 0.995 and max_pain_dte <= 3),
            data_quality="high",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte})",
            pts_sign=-1,
        ),
        _rule(
            "gex_near_negative_penalty_24h",
            "GEX near négatif contredit le scénario haussier",
            "Régime amplificateur actif → dealers amplifient les moves, pas les soutiens",
            weight=6,
            triggered=(gex_near < 0),
            data_quality="high",
            condition_detail=f"GEX near = {gex_near/1e6:.1f}M$ (négatif)",
            pts_sign=-1,
        ),
        _rule(
            "spot_far_below_flip_24h",
            "Spot très éloigné sous le flip (> 5%)",
            "BTC très loin sous le flip = régime amplificateur baissier profond actif",
            weight=6,
            triggered=(
                flip_level is not None
                and flip_use_in_signal
                and spot < flip_level
                and (flip_level - spot) / spot > 0.05
            ),
            data_quality="medium" if (flip_level is not None and flip_use_in_signal) else "unavailable",
            condition_detail=(
                f"Spot ${spot:,.0f} = {(flip_level-spot)/spot*100:.1f}% sous le flip ${flip_level:,.0f}"
                if flip_level else "Flip non disponible"
            ),
            pts_sign=-1,
        ),
    ]

    return positives, penalties


def _rules_bear_72h(
    gex_near: float,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    call_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
) -> tuple[list, list]:
    """Règles 72h BEAR — Max Pain + Walls + structure dominants."""

    positives = [
        _rule(
            "max_pain_below_72h",
            "Max Pain sous le spot — attraction baissière structurelle",
            "Max Pain < spot = dealers ont intérêt à pousser BTC vers le bas à l'expiry",
            weight=15,
            triggered=(max_pain_strike < spot * 0.995 and max_pain_dte <= 7),
            data_quality="high" if max_pain_dte <= 7 else "medium",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte}) vs spot ${spot:,.0f}",
        ),
        _rule(
            "put_wall_below_structural_72h",
            "Put wall structurel sous le spot (5-15%)",
            "Mur de puts structurel = plancher mais aussi cible de flush baissier",
            weight=8,
            triggered=(
                put_wall > 0
                and 0.05 < (spot - put_wall) / spot <= 0.15
            ),
            data_quality="high",
            condition_detail=(
                f"Put wall ${put_wall:,.0f} ({(spot-put_wall)/spot*100:.1f}% sous spot)"
                if put_wall > 0 else "Put wall non identifié"
            ),
        ),
        _rule(
            "iv_high_structural_72h",
            "IV structurellement élevée sur 72h",
            "IV rank > 70 = marché anticipe la volatilité → options chères = peur de baisse",
            weight=5,
            triggered=(iv_rank > 70),
            data_quality="medium",
            condition_detail=f"IV rank = {iv_rank:.0f}%",
        ),
    ]

    penalties = [
        _rule(
            "max_pain_above_72h",
            "Max Pain au-dessus du spot — attraction haussière structurelle",
            "Max Pain > spot avec DTE ≤ 7j = attraction mécanique vers le haut sur 72h",
            weight=15,
            triggered=(max_pain_strike > spot * 1.005 and max_pain_dte <= 7),
            data_quality="high" if max_pain_dte <= 7 else "medium",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte})",
            pts_sign=-1,
        ),
        _rule(
            "call_wall_above_strong_72h",
            "Call wall fort proche — résistance structurelle mais aussi cible haussière",
            "Gros call wall proche = cible d'attraction + résistance = signal BULL 72h",
            weight=8,
            triggered=(
                call_wall > spot
                and 0 < (call_wall - spot) / spot <= 0.08
            ),
            data_quality="high",
            condition_detail=(
                f"Call wall ${call_wall:,.0f} ({(call_wall-spot)/spot*100:.1f}% au-dessus)"
                if call_wall > spot else "Call wall non identifié"
            ),
            pts_sign=-1,
        ),
    ]

    return positives, penalties


def _rules_bull_72h(
    gex_near: float,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    call_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
    gex_regime: str = "NEUTRE",
    crash_regime: bool = False,
) -> tuple[list, list]:
    """Règles 72h BULL — miroir logique.

    V2 : Max Pain repondéré conditionnellement.
    En crash régime (DEX extrême baissier + GEX AMPLIFICATEUR), poids = 0.
    Sinon : poids plein uniquement si DTE ≤ 7 + GEX STABILISANT + DEX aligné + IV calme.
    """
    # V2 — poids effectif Max Pain selon les conditions du marché
    mp_weight = _max_pain_bull_weight(max_pain_dte, gex_regime, dex_direction, iv_rank, crash_regime)
    mp_triggered = max_pain_strike > spot * 1.005 and max_pain_dte <= 7 and mp_weight > 0

    if crash_regime:
        mp_detail = (
            f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte}) — "
            "IGNORÉ : flux dealers baissiers dominants (crash régime actif)"
        )
        mp_quality = "low"
    elif mp_weight == 0:
        mp_detail = (
            f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte}) — "
            f"ignoré : conditions non alignées (GEX {gex_regime}, DEX {dex_direction})"
        )
        mp_quality = "low"
    else:
        mp_detail = (
            f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte}) vs spot ${spot:,.0f} "
            f"[poids conditionnel {mp_weight}/15]"
        )
        mp_quality = "high" if max_pain_dte <= 7 else "medium"

    positives = [
        _rule(
            "max_pain_above_72h_bull",
            "Max Pain au-dessus — attraction haussière conditionnelle" if mp_weight > 0
                else "Max Pain au-dessus — ignoré (aimant conditionnel, conditions non alignées)",
            "Max Pain > spot : aimant conditionnel UNIQUEMENT si GEX STABILISANT + DEX aligné + DTE ≤ 7 + IV calme",
            weight=mp_weight,
            triggered=mp_triggered,
            data_quality=mp_quality,
            condition_detail=mp_detail,
        ),
        _rule(
            "call_wall_above_structural_72h",
            "Call wall fort comme cible d'attraction haussière",
            "Gros call wall proche = cible mécanique haussière pour les 72h",
            weight=8,
            triggered=(
                call_wall > spot
                and 0 < (call_wall - spot) / spot <= 0.08
            ),
            data_quality="high",
            condition_detail=(
                f"Call wall ${call_wall:,.0f} ({(call_wall-spot)/spot*100:.1f}% au-dessus)"
                if call_wall > spot else "Call wall non identifié"
            ),
        ),
        _rule(
            "iv_calm_structural_72h",
            "IV structurellement basse — tendance haussière non contrariée",
            "IV rank < 30 = marché serein, tendance de fond haussière",
            weight=5,
            triggered=(iv_rank < 30),
            data_quality="medium",
            condition_detail=f"IV rank = {iv_rank:.0f}%",
        ),
        _rule(
            "put_wall_strong_floor_72h",
            "Put wall structurel fort comme plancher",
            "Gros put wall sous le spot = frein mécanique aux baisses profondes",
            weight=6,
            triggered=(
                put_wall > 0
                and 0 < (spot - put_wall) / spot <= 0.10
            ),
            data_quality="high",
            condition_detail=(
                f"Put wall ${put_wall:,.0f} ({(spot-put_wall)/spot*100:.1f}% sous spot)"
                if put_wall > 0 else "Put wall non identifié"
            ),
        ),
    ]

    penalties = [
        _rule(
            "max_pain_below_72h_penalty",
            "Max Pain sous le spot — pression baissière (conditionnel)",
            "Max Pain < spot : aimant baissier conditionnel — poids réduit si régime contradictoire",
            weight=_max_pain_bull_weight(max_pain_dte, gex_regime, dex_direction, iv_rank, crash_regime),
            triggered=(max_pain_strike < spot * 0.995 and max_pain_dte <= 7),
            data_quality="high" if max_pain_dte <= 7 else "medium",
            condition_detail=f"Max Pain ${max_pain_strike:,.0f} (J-{max_pain_dte})",
            pts_sign=-1,
        ),
    ]

    return positives, penalties


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def compute_probability_engine(
    spot: float,
    gex_near: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    dex_direction: str,
    dex_actionable_btc: float,
    iv_rank: float,
    pc_ratio_near: float,
    put_wall: float,
    call_wall: float,
    max_pain_strike: float,
    max_pain_dte: int,
    gex_near_prev: Optional[float] = None,
    # Binance public — Étape 3
    funding_rate: Optional[float] = None,
    futures_oi: Optional[float] = None,
    futures_oi_prev: Optional[float] = None,
    spot_volume_24h: Optional[float] = None,
    spot_volume_7d_avg: Optional[float] = None,
    spot_prev: Optional[float] = None,
    # V2 — Crash Regime Gate
    gex_regime: str = "NEUTRE",
    dex_score: float = 50.0,
    # V2 — Module Reliability (point 5)
    # Dict accuracy_score 0-100 par module depuis indicator_accuracy.py
    # None → 1.0 (Phase A : données non encore disponibles)
    module_accuracy_scores: Optional[dict] = None,
) -> ProbabilityEngineOutput:
    """Calcule les probabilités directionnelles pour 3 horizons (4h / 24h / 72h).

    Paramètres:
      spot              : prix BTC actuel
      gex_near          : Gamma Exposure near-term (DTE ≤ 14j, en USD)
      flip_level        : niveau de retournement GEX (None si indisponible)
      flip_use_in_signal: True si flip validé par activité récente
      dex_direction     : "BULLISH_FLOWS" | "BEARISH_FLOWS" | "NEUTRAL"
      dex_actionable_btc: delta actionnable en BTC (abs)
      iv_rank           : IV rank 0-100
      pc_ratio_near     : Put/Call ratio DTE ≤ 14j
      put_wall          : strike put wall principal (0 si absent)
      call_wall         : strike call wall principal
      max_pain_strike   : strike max pain near-term
      max_pain_dte      : jours avant expiry max pain
      gex_near_prev     : gex_near snapshot précédent (None = unavailable)
      funding_rate      : funding rate Binance en % (None = unavailable)
      futures_oi        : futures open interest Binance en USD (None = unavailable)
      futures_oi_prev   : OI snapshot précédent en USD (None = unavailable)
      spot_volume_24h   : volume spot 24h Binance en USD (None = unavailable)
      spot_volume_7d_avg: moyenne volume spot 7j en USD (None = unavailable)
      spot_prev         : prix BTC snapshot précédent (None = unavailable)
    """

    # V2 — Crash Regime Gate : détecte si flux dealers extrêmes bloquent les signaux BULL
    crash_regime = _crash_regime_active(dex_score, dex_direction, gex_regime)
    mp_weight_72h = _max_pain_bull_weight(max_pain_dte, gex_regime, dex_direction, iv_rank, crash_regime)

    # V2 — Module Reliability Factors (point 5)
    # module_weight_effective = module_weight_base × reliability_factor
    _reliability_applied = bool(module_accuracy_scores)
    _factors: dict = {}
    if module_accuracy_scores:
        for mod, score in module_accuracy_scores.items():
            _factors[mod] = _reliability_factor_from_score(score)

    # Validation historique par horizon (module dominant)
    _val_4h  = _historical_validation_label(
        (module_accuracy_scores or {}).get("dex")
    )
    _val_24h = _historical_validation_label(
        (module_accuracy_scores or {}).get("gex")
    )
    # F15 — MOPI retiré : validation 72h = max_pain seul (dette PE self-validation future)
    _val_72h_mp = _historical_validation_label((module_accuracy_scores or {}).get("max_pain"))
    _val_order  = {"en accumulation": 0, "forte": 3, "moyenne": 2, "faible": 1}
    _val_72h    = _val_72h_mp

    # ── 4h ───────────────────────────────────────────────────────────────────
    pos_b4, pen_b4 = _rules_bear_4h(
        gex_near, spot, flip_level, flip_use_in_signal,
        dex_direction, dex_actionable_btc,
        iv_rank, pc_ratio_near, put_wall, call_wall,
        max_pain_strike, max_pain_dte, gex_near_prev,
    )
    pos_u4, pen_u4 = _rules_bull_4h(
        gex_near, spot, flip_level, flip_use_in_signal,
        dex_direction, dex_actionable_btc,
        iv_rank, pc_ratio_near, put_wall, call_wall,
        max_pain_strike, max_pain_dte, gex_near_prev,
    )

    # ── 24h ──────────────────────────────────────────────────────────────────
    pos_b24, pen_b24 = _rules_bear_24h(
        gex_near, spot, flip_level, flip_use_in_signal,
        dex_direction, iv_rank, pc_ratio_near,
        put_wall, max_pain_strike, max_pain_dte, gex_near_prev,
        funding_rate=funding_rate,
        futures_oi=futures_oi,
        futures_oi_prev=futures_oi_prev,
        spot_volume_24h=spot_volume_24h,
        spot_volume_7d_avg=spot_volume_7d_avg,
        spot_prev=spot_prev,
    )
    pos_u24, pen_u24 = _rules_bull_24h(
        gex_near, spot, flip_level, flip_use_in_signal,
        dex_direction, iv_rank, pc_ratio_near,
        put_wall, call_wall, max_pain_strike, max_pain_dte, gex_near_prev,
    )

    # ── 72h ──────────────────────────────────────────────────────────────────
    pos_b72, pen_b72 = _rules_bear_72h(
        gex_near, spot, flip_level, flip_use_in_signal,
        dex_direction, iv_rank, pc_ratio_near,
        put_wall, call_wall, max_pain_strike, max_pain_dte,
    )
    pos_u72, pen_u72 = _rules_bull_72h(
        gex_near, spot, flip_level, flip_use_in_signal,
        dex_direction, iv_rank, pc_ratio_near,
        put_wall, call_wall, max_pain_strike, max_pain_dte,
        gex_regime=gex_regime,
        crash_regime=crash_regime,
    )

    # Appliquer les reliability factors AVANT _build_scenario (point 5)
    if _reliability_applied and _factors:
        pos_b4,  pen_b4  = _scale_rules_by_reliability(pos_b4,  _factors), _scale_rules_by_reliability(pen_b4,  _factors)
        pos_u4,  pen_u4  = _scale_rules_by_reliability(pos_u4,  _factors), _scale_rules_by_reliability(pen_u4,  _factors)
        pos_b24, pen_b24 = _scale_rules_by_reliability(pos_b24, _factors), _scale_rules_by_reliability(pen_b24, _factors)
        pos_u24, pen_u24 = _scale_rules_by_reliability(pos_u24, _factors), _scale_rules_by_reliability(pen_u24, _factors)
        pos_b72, pen_b72 = _scale_rules_by_reliability(pos_b72, _factors), _scale_rules_by_reliability(pen_b72, _factors)
        pos_u72, pen_u72 = _scale_rules_by_reliability(pos_u72, _factors), _scale_rules_by_reliability(pen_u72, _factors)

    bear_4h  = _build_scenario("BEAR_4H",  "4h",  "BEAR", pos_b4,  pen_b4,
                               historical_validation=_val_4h,
                               reliability_factors_applied=_reliability_applied)
    bull_4h  = _build_scenario("BULL_4H",  "4h",  "BULL", pos_u4,  pen_u4,
                               crash_regime=crash_regime,
                               historical_validation=_val_4h,
                               reliability_factors_applied=_reliability_applied)
    bear_24h = _build_scenario("BEAR_24H", "24h", "BEAR", pos_b24, pen_b24,
                               historical_validation=_val_24h,
                               reliability_factors_applied=_reliability_applied)
    bull_24h = _build_scenario("BULL_24H", "24h", "BULL", pos_u24, pen_u24,
                               crash_regime=crash_regime,
                               historical_validation=_val_24h,
                               reliability_factors_applied=_reliability_applied)
    bear_72h = _build_scenario("BEAR_72H", "72h", "BEAR", pos_b72, pen_b72,
                               historical_validation=_val_72h,
                               reliability_factors_applied=_reliability_applied)
    bull_72h = _build_scenario("BULL_72H", "72h", "BULL", pos_u72, pen_u72,
                               crash_regime=crash_regime, max_pain_weight_applied=mp_weight_72h,
                               historical_validation=_val_72h,
                               reliability_factors_applied=_reliability_applied)

    # ── Scénario dominant ─────────────────────────────────────────────────────
    all_scenarios = [bear_4h, bull_4h, bear_24h, bull_24h, bear_72h, bull_72h]
    dominant = max(all_scenarios, key=lambda s: abs(s.probability - 50))

    # ── Interprétation globale ────────────────────────────────────────────────
    interpretation = _build_interpretation(
        bear_24h, bull_24h, dominant, spot, flip_level, flip_use_in_signal,
    )

    crash_warning = ""
    if crash_regime:
        crash_warning = (
            "CRASH REGIME GATE actif — DEX extrêmement baissier "
            f"(score {dex_score:.0f}%) + GEX AMPLIFICATEUR. "
            "Max Pain / Gravity = aimants résiduels non confirmés. "
            "Probabilités BULL plafonnées à 55%. Risk-Off / Trend Following baissier uniquement."
        )

    ts = datetime.now(timezone.utc).isoformat()
    return ProbabilityEngineOutput(
        spot=spot,
        timestamp=ts,
        bear_4h=bear_4h,
        bull_4h=bull_4h,
        bear_24h=bear_24h,
        bull_24h=bull_24h,
        bear_72h=bear_72h,
        bull_72h=bull_72h,
        dominant_scenario=dominant.scenario,
        dominant_probability=dominant.probability,
        dominant_confidence=dominant.confidence,
        interpretation=interpretation,
        engine_version=_ENGINE_VERSION,
        disclaimer=_DISCLAIMER,
        crash_regime_active=crash_regime,
        crash_regime_warning=crash_warning,
    )


def _build_interpretation(
    bear_24h: ScenarioProbability,
    bull_24h: ScenarioProbability,
    dominant: ScenarioProbability,
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
) -> str:
    """Phrase synthèse humaine — logique décision, pas description données."""
    b = bear_24h.probability
    u = bull_24h.probability
    bc = bear_24h.confidence
    uc = bull_24h.confidence
    edge_b = bear_24h.edge_label
    edge_u = bull_24h.edge_label

    # Signal insuffisant sur tous les horizons
    if bc < _CONF_INSUFFICIENT and uc < _CONF_INSUFFICIENT:
        return (
            "Signal 24h insuffisant — GEX, DEX et Flip non alignés. "
            "Pas de pression directionnelle exploitable. "
            "Attends une confluence GEX + DEX + position du spot vs Flip avant d'agir."
        )

    if b > u and b > 60:
        strength = "forte" if b > 70 else "modérée"
        conf_str = f"complétude {bc:.0f}%"
        flip_str = (
            f" Flip ${flip_level:,.0f} est la ligne rouge."
            if flip_level and flip_use_in_signal and spot < flip_level
            else ""
        )
        return (
            f"Pression baissière {strength} 24h ({b:.0f}% — {conf_str}).{flip_str}"
        )

    if u > b and u > 60:
        strength = "forte" if u > 70 else "modérée"
        conf_str = f"complétude {uc:.0f}%"
        flip_str = (
            f" Flip ${flip_level:,.0f} est le niveau clé à défendre."
            if flip_level and flip_use_in_signal and spot > flip_level
            else ""
        )
        return (
            f"Pression haussière {strength} 24h ({u:.0f}% — {conf_str}).{flip_str}"
        )

    # Signaux contradictoires ou proches de 50 — F14.3 texte différencié
    diff = abs(u - b)
    if diff <= 5:
        return (
            f"Équilibre 24h ({u:.0f}% haussier vs {b:.0f}% baissier — écart {diff:.0f} pts). "
            "Pas de pression directionnelle dominante. Attends une confirmation avant d'agir."
        )
    if u > b:
        return (
            f"Biais haussier modéré 24h ({u:.0f}% vs {b:.0f}%). "
            "Signal en émergence — attends confluence GEX + DEX pour confirmer."
        )
    return (
        f"Biais baissier modéré 24h ({b:.0f}% vs {u:.0f}%). "
        "Signal en émergence — attends confluence GEX + DEX pour confirmer."
    )


# ══════════════════════════════════════════════════════════════════════════════
# SÉRIALISATION JSON
# ══════════════════════════════════════════════════════════════════════════════

def _rule_to_dict(r: ProbabilityRule) -> dict:
    return {
        "id": r.id,
        "description": r.description,
        "justification": r.justification,
        "weight": r.weight,
        "pts_applied": r.pts_applied,
        "triggered": r.triggered,
        "data_quality": r.data_quality,
        "condition_detail": r.condition_detail,
    }


def _scenario_to_dict(s: ScenarioProbability) -> dict:
    return {
        "scenario": s.scenario,
        "horizon": s.horizon,
        "direction": s.direction,
        "probability": round(s.probability, 1),
        "confidence": round(s.confidence, 1),           # backward compat — alias data_completeness
        "confidence_label": s.confidence_label,
        "edge_label": s.edge_label,
        "signal_label": s.signal_label,
        "base_probability": s.base_probability,
        "raw_pts": s.raw_pts,
        "rules_triggered_count": s.rules_triggered_count,
        "rules_unavailable_count": s.rules_unavailable_count,
        "data_coverage_pct": s.data_coverage_pct,
        "top_contributors": s.top_contributors,
        "positive_rules": [_rule_to_dict(r) for r in s.positive_rules],
        "penalty_rules": [_rule_to_dict(r) for r in s.penalty_rules],
        # V2 — Crash Regime Gate
        "crash_regime_active": s.crash_regime_active,
        "crash_regime_warning": s.crash_regime_warning,
        "max_pain_weight_applied": s.max_pain_weight_applied,
        # V2 — Terminologie rigoureuse (point 6)
        "data_completeness": round(s.data_completeness, 1),
        "historical_validation": s.historical_validation,
        "edge_reel": s.edge_reel,
        "conclusion_line": s.conclusion_line,
        "reliability_factors_applied": s.reliability_factors_applied,
    }


def _horizon_verdict(bull_prob: float, bear_prob: float) -> str:
    """F8.2 — Verdict équilibré avec frontière incluse (delta <= 5)."""
    diff = abs(bull_prob - bear_prob)
    if diff <= 5:
        return "EQUILIBRE"
    return "BIAIS_HAUSSIER" if bull_prob > bear_prob else "BIAIS_BAISSIER"


def probability_engine_to_dict(output: ProbabilityEngineOutput) -> dict:
    b24 = output.bear_24h.probability if output.bear_24h else 50.0
    u24 = output.bull_24h.probability if output.bull_24h else 50.0
    b72 = output.bear_72h.probability if output.bear_72h else 50.0
    u72 = output.bull_72h.probability if output.bull_72h else 50.0
    return {
        "spot": output.spot,
        "timestamp": output.timestamp,
        "engine_version": output.engine_version,
        "disclaimer": output.disclaimer,
        "dominant_scenario": output.dominant_scenario,
        "dominant_probability": round(output.dominant_probability, 1),
        "dominant_confidence": round(output.dominant_confidence, 1),
        "interpretation": output.interpretation,
        "bear_4h":  _scenario_to_dict(output.bear_4h),
        "bull_4h":  _scenario_to_dict(output.bull_4h),
        "bear_24h": _scenario_to_dict(output.bear_24h),
        "bull_24h": _scenario_to_dict(output.bull_24h),
        "bear_72h": _scenario_to_dict(output.bear_72h),
        "bull_72h": _scenario_to_dict(output.bull_72h),
        # F8.2 — Verdicts horizon pré-calculés (frontière incluse delta<=5)
        "horizon_verdict_24h": _horizon_verdict(u24, b24),
        "horizon_verdict_72h": _horizon_verdict(u72, b72),
        # V2 — Crash Regime Gate
        "crash_regime_active": output.crash_regime_active,
        "crash_regime_warning": output.crash_regime_warning,
    }
