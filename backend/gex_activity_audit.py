"""
GEX Activity Audit — qualité réelle du signal GEX pour Signal Mamos.

Mesure la part du GEX provenant des positions :
  DORMANT    : OI inactif (flux nul) — gonfle le GEX sans signifier quoi que ce soit
  STRUCTURAL : gros OI, flux faible, loin du spot ou long-daté
  ACTIVE     : OI + flux récent — position en mouvement
  ACTIONABLE : flux + proximité spot + DTE court — impact BTC immédiat

Un GEX de $2B dominé à 80% par des puts deep OTM à 180j = signal peu fiable.
Un GEX de $500M dominé à 60% par des options ATM DTE ≤ 14j = signal fort.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .deribit_client import OptionData, MarketSnapshot
from .gex import CONTRACT_SIZE, GEX_NEUTRAL_THRESHOLD, _classify_regime, _compute_dte
from .options_activity_engine import (
    MIN_OI_FLOW,
    ActivityTag,
    TAG_ACTIONABLE,
    TAG_ACTIVE,
    TAG_DORMANT,
    TAG_STRUCTURAL,
    classify_activity_profile,
    compute_flow_ratio,
    compute_proximity_score,
    compute_dte_urgency,
)

# Seuils pour tagger chaque option individuellement (prox × urgency)
_ACTIONABLE_THRESHOLD = 0.25
_ACTIVE_THRESHOLD = 0.05


@dataclass
class GEXCategoryStats:
    gex_abs_usd: float           # Σ|GEX| des options de cette catégorie
    gex_net_usd: float           # GEX net signé (calls positifs, puts négatifs)
    gex_pct: float               # % du total Σ|GEX| du snapshot
    count: int                   # nombre d'instruments dans cette catégorie
    top_contributors: List[dict] = field(default_factory=list)  # top 5 par |GEX|


@dataclass
class GEXActivityAudit:
    btc_price: float
    gex_total_usd: float
    gex_regime: str
    timestamp: float

    # Distribution par catégorie
    dormant: GEXCategoryStats
    structural: GEXCategoryStats
    active: GEXCategoryStats
    actionable: GEXCategoryStats

    # Scores agrégés en mode GEX (poids = gamma × OI × spot²)
    gex_structural_score: float   # = total_gex (GEX brut non filtré)
    gex_active_score: float       # Σ(gex × flow_ratio)
    gex_actionable_score: float   # Σ(gex × flow × prox × urgency)
    active_pct: float             # |gex_active| / |gex_structural| × 100
    actionable_pct: float         # |gex_actionable| / |gex_active| × 100
    overall_profile: ActivityTag  # classification globale du basket

    # Verdict Signal Mamos
    signal_quality_score: int
    signal_quality_label: str
    signal_quality_color: str
    signal_verdict: str
    use_in_signal: bool

    low_oi_anomaly_count: int


def _tag_option(flow: float, prox: float, urgency: float) -> ActivityTag:
    """Classifie une option individuellement pour le GEX audit."""
    if flow == 0.0:
        return TAG_DORMANT
    product = prox * urgency
    if product >= _ACTIONABLE_THRESHOLD:
        return TAG_ACTIONABLE
    if product >= _ACTIVE_THRESHOLD:
        return TAG_ACTIVE
    return TAG_STRUCTURAL


def _compute_signal_quality(
    dormant_pct: float,
    active_and_actionable_pct: float,
    actionable_only_pct: float,
    anomaly_count: int,
) -> Tuple[int, str, str, str, bool]:
    """
    Score 0-10 de qualité du signal GEX pour Signal Mamos.
    Retourne (score, label, color, verdict, use_in_signal).
    """
    score = 5

    if actionable_only_pct >= 30:
        score += 2
    elif actionable_only_pct >= 15:
        score += 1

    if active_and_actionable_pct >= 40:
        score += 1

    if dormant_pct >= 60:
        score -= 2
    elif dormant_pct >= 40:
        score -= 1

    if anomaly_count >= 5:
        score -= 1

    score = max(0, min(10, score))

    if score >= 8:
        # P0.7 — "GEX mécaniquement actif" ≠ edge statistique validé.
        # Séparer activité mécanique (ce label) de signal directionnel confirmé (backtest requis).
        label, color, use = "GEX mécaniquement actif", "green", True
    elif score >= 6:
        label, color, use = "Signal fiable", "green", True
    elif score >= 5:
        label, color, use = "Signal modéré", "yellow", True
    elif score >= 3:
        label, color, use = "Signal faible", "orange", False
    else:
        label, color, use = "Signal peu fiable", "red", False

    if dormant_pct >= 60:
        verdict = (
            f"{dormant_pct:.0f}% du GEX vient de positions inactives (OI sans flux). "
            f"Le signal est structurellement gonflé — pondérer avant usage dans Signal Mamos."
        )
    elif actionable_only_pct >= 25:
        verdict = (
            f"{actionable_only_pct:.0f}% du GEX est actionnable (ATM + DTE court). "
            f"Le signal reflète une activité de marché réelle — fiable pour Signal Mamos."
        )
    elif active_and_actionable_pct >= 35:
        verdict = (
            f"{active_and_actionable_pct:.0f}% du GEX est actif ou actionnable. "
            f"Bonne qualité — catalyseur immédiat limité mais signal valide."
        )
    else:
        verdict = (
            f"GEX structurel dominant ({100 - active_and_actionable_pct:.0f}% structural + dormant). "
            f"Signal de contexte longue durée — moins prédictif à 24h."
        )

    return score, label, color, verdict, use


@dataclass
class FlipActivityAudit:
    """Audit qualité du flip level GEX.

    Règle : un niveau proche du spot n'est pas automatiquement un déclencheur.
    Il devient déclencheur seulement s'il est confirmé par activité récente,
    DTE court et contribution GEX exploitable dans la fenêtre ±10%.
    """
    flip_level: float
    flip_activity_tag: ActivityTag       # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    flip_signal_quality: int             # 0-10
    flip_signal_label: str               # "Signal fort" | "Signal fiable" | etc.
    flip_use_in_signal: bool             # False si DORMANT ou STRUCTURAL
    window_gex_total: float              # Σ|GEX| dans la fenêtre ±10%
    window_dormant_pct: float
    window_structural_pct: float
    window_active_pct: float
    window_actionable_pct: float
    top_contributors: List[dict] = field(default_factory=list)


def compute_flip_activity_audit(snapshot: MarketSnapshot, flip_level: Optional[float]) -> "FlipActivityAudit":
    """Audit qualité du flip level GEX.

    Analyse les options dans ±10% autour du flip_level pour déterminer
    si le niveau est soutenu par une activité récente exploitable.
    flip_level=None = pas de crossing identifié — retourne DORMANT automatiquement.
    """
    spot = snapshot.btc_price

    if flip_level is None:
        return FlipActivityAudit(
            flip_level=flip_level,
            flip_activity_tag=TAG_DORMANT,
            flip_signal_quality=0,
            flip_signal_label="Signal peu fiable",
            flip_use_in_signal=False,
            window_gex_total=0.0,
            window_dormant_pct=100.0,
            window_structural_pct=0.0,
            window_active_pct=0.0,
            window_actionable_pct=0.0,
            top_contributors=[],
        )

    window_lo = flip_level * 0.90
    window_hi = flip_level * 1.10

    buckets: Dict[ActivityTag, List[dict]] = {
        TAG_DORMANT:    [],
        TAG_STRUCTURAL: [],
        TAG_ACTIVE:     [],
        TAG_ACTIONABLE: [],
    }

    for opt in snapshot.options:
        if not (window_lo <= opt.strike <= window_hi):
            continue
        dte = _compute_dte(opt.expiry)
        if dte <= 0:
            continue

        contribution = abs(opt.gamma * opt.oi * CONTRACT_SIZE * (spot ** 2))

        flow, _ = compute_flow_ratio(opt)
        prox = compute_proximity_score(opt.strike, flip_level)
        urgency = compute_dte_urgency(dte)

        tag = _tag_option(flow, prox, urgency)
        buckets[tag].append({
            "instrument": opt.instrument,
            "strike": opt.strike,
            "expiry": opt.expiry,
            "type": opt.option_type,
            "dte": dte,
            "oi": round(opt.oi, 1),
            "flow_ratio": round(flow, 3),
            "prox": round(prox, 3),
            "urgency": round(urgency, 3),
            "gex_abs_usd": round(contribution, 0),
            "activity_tag": tag,
        })

    all_items = [o for opts in buckets.values() for o in opts]
    total_abs = sum(o["gex_abs_usd"] for o in all_items) or 1.0

    def _pct(t: ActivityTag) -> float:
        s = sum(o["gex_abs_usd"] for o in buckets[t])
        return round(s / total_abs * 100, 1)

    dormant_pct    = _pct(TAG_DORMANT)
    structural_pct = _pct(TAG_STRUCTURAL)
    active_pct     = _pct(TAG_ACTIVE)
    actionable_pct = _pct(TAG_ACTIONABLE)

    active_total = active_pct + actionable_pct
    actionable_of_active = (
        actionable_pct / active_total * 100 if active_total > 1e-6 else 0.0
    )
    base_tag = classify_activity_profile(active_total, actionable_of_active)

    # Règles de surcharge spec
    if dormant_pct > 60:
        final_tag = TAG_DORMANT
    elif actionable_pct > 35:
        final_tag = TAG_ACTIONABLE
    elif actionable_pct > 20 and base_tag in (TAG_DORMANT, TAG_STRUCTURAL):
        final_tag = TAG_ACTIVE
    else:
        final_tag = base_tag

    flip_use_in_signal = final_tag in (TAG_ACTIVE, TAG_ACTIONABLE)

    sq_score, sq_label, _, _, _ = _compute_signal_quality(
        dormant_pct=dormant_pct,
        active_and_actionable_pct=active_total,
        actionable_only_pct=actionable_pct,
        anomaly_count=0,
    )

    top5 = sorted(all_items, key=lambda x: x["gex_abs_usd"], reverse=True)[:5]

    return FlipActivityAudit(
        flip_level=flip_level,
        flip_activity_tag=final_tag,
        flip_signal_quality=sq_score,
        flip_signal_label=sq_label,
        flip_use_in_signal=flip_use_in_signal,
        window_gex_total=round(total_abs if all_items else 0.0, 0),
        window_dormant_pct=dormant_pct,
        window_structural_pct=structural_pct,
        window_active_pct=active_pct,
        window_actionable_pct=actionable_pct,
        top_contributors=top5,
    )


def compute_gex_activity_audit(snapshot: MarketSnapshot) -> GEXActivityAudit:
    spot = snapshot.btc_price

    buckets: Dict[ActivityTag, List[dict]] = {
        TAG_DORMANT: [],
        TAG_STRUCTURAL: [],
        TAG_ACTIVE: [],
        TAG_ACTIONABLE: [],
    }

    gex_structural = 0.0
    gex_active = 0.0
    gex_actionable = 0.0
    anomaly_count = 0

    for opt in snapshot.options:
        dte = _compute_dte(opt.expiry)
        if dte <= 0:
            continue

        # GEX contribution brute (formule identique à compute_gex)
        gex_raw = opt.gamma * opt.oi * CONTRACT_SIZE * (spot ** 2)
        gex_signed = gex_raw if opt.option_type == "call" else -gex_raw

        flow, is_anomaly = compute_flow_ratio(opt)
        prox = compute_proximity_score(opt.strike, spot)
        urgency = compute_dte_urgency(dte)

        if is_anomaly:
            anomaly_count += 1

        tag = _tag_option(flow, prox, urgency)
        buckets[tag].append({
            "instrument": opt.instrument,
            "strike": opt.strike,
            "expiry": opt.expiry,
            "type": opt.option_type,
            "dte": dte,
            "oi": round(opt.oi, 1),
            "flow_ratio": round(flow, 3),
            "prox": round(prox, 3),
            "urgency": round(urgency, 3),
            "gex_usd": round(gex_signed, 0),
            "gex_abs_usd": round(abs(gex_signed), 0),
        })

        gex_structural += gex_signed
        gex_active += gex_signed * flow
        gex_actionable += gex_signed * flow * prox * urgency

    total_abs_gex = sum(
        abs(o["gex_usd"]) for opts in buckets.values() for o in opts
    ) or 1.0

    def _make_stats(tag: ActivityTag) -> GEXCategoryStats:
        opts = buckets[tag]
        abs_sum = sum(abs(o["gex_usd"]) for o in opts)
        net_sum = sum(o["gex_usd"] for o in opts)
        top5 = sorted(opts, key=lambda x: x["gex_abs_usd"], reverse=True)[:5]
        return GEXCategoryStats(
            gex_abs_usd=round(abs_sum, 0),
            gex_net_usd=round(net_sum, 0),
            gex_pct=round(abs_sum / total_abs_gex * 100, 1),
            count=len(opts),
            top_contributors=top5,
        )

    dormant_stats = _make_stats(TAG_DORMANT)
    structural_stats = _make_stats(TAG_STRUCTURAL)
    active_stats = _make_stats(TAG_ACTIVE)
    actionable_stats = _make_stats(TAG_ACTIONABLE)

    # Profil global via l'activity engine (basé sur les scores pondérés)
    abs_structural = abs(gex_structural)
    active_engine_pct = (abs(gex_active) / abs_structural * 100) if abs_structural > 1e-9 else 0.0
    abs_active_engine = abs(gex_active)
    actionable_engine_pct = (abs(gex_actionable) / abs_active_engine * 100) if abs_active_engine > 1e-9 else 0.0
    overall_profile = classify_activity_profile(active_engine_pct, actionable_engine_pct)

    # Percentages basés sur les buckets (pour le verdict qualité)
    dormant_pct = dormant_stats.gex_pct
    active_and_actionable_pct = active_stats.gex_pct + actionable_stats.gex_pct
    actionable_only_pct = actionable_stats.gex_pct

    sq_score, sq_label, sq_color, sq_verdict, use_in_signal = _compute_signal_quality(
        dormant_pct=dormant_pct,
        active_and_actionable_pct=active_and_actionable_pct,
        actionable_only_pct=actionable_only_pct,
        anomaly_count=anomaly_count,
    )

    return GEXActivityAudit(
        btc_price=spot,
        gex_total_usd=round(gex_structural, 0),
        gex_regime=_classify_regime(gex_structural),
        timestamp=snapshot.timestamp,
        dormant=dormant_stats,
        structural=structural_stats,
        active=active_stats,
        actionable=actionable_stats,
        gex_structural_score=round(gex_structural, 0),
        gex_active_score=round(gex_active, 0),
        gex_actionable_score=round(gex_actionable, 0),
        active_pct=round(active_engine_pct, 1),
        actionable_pct=round(actionable_engine_pct, 1),
        overall_profile=overall_profile,
        signal_quality_score=sq_score,
        signal_quality_label=sq_label,
        signal_quality_color=sq_color,
        signal_verdict=sq_verdict,
        use_in_signal=use_in_signal,
        low_oi_anomaly_count=anomaly_count,
    )
