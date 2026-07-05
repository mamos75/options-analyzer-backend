"""
Gravity Activity Audit — Qualité réelle des zones Gravity.

Pour chaque zone Gravity, mesure si l'OI qui la constitue est :
  DORMANT    : OI inactif (flux nul) — zone fantôme, LEAPS dormants, ne pas cibler
  STRUCTURAL : gros OI longue date, peu de flux récent — contexte de fond
  ACTIVE     : OI + flux récent — zone surveillée par le marché
  ACTIONABLE : flux + proximité spot + DTE court — impact BTC immédiat

Verdict par zone :
  💀 Gravity Dormant      → OI sans flux, zone construite sur de l'OI inactif
  🪨 Gravity Structurelle → contexte de fond longue durée
  ⚡ Gravity Active       → zone surveillée, flux détecté
  🔥 Gravity Actionnable  → zone susceptible d'influencer BTC maintenant

Signal Quality — même modèle que GEX :
  score 0-10 + use_in_signal bool par zone et global
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .deribit_client import OptionData, MarketSnapshot
from .gravity_map import compute_gravity_map
from .gex import compute_gex, _compute_dte
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

_ACTIONABLE_THRESHOLD = 0.25
_ACTIVE_THRESHOLD = 0.05


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class GravityZoneCategoryBreakdown:
    oi_usd: float        # OI USD dans ce bucket
    oi_pct: float        # % du total OI de la zone
    count: int           # nombre d'options dans ce bucket


@dataclass
class GravityZoneAudit:
    strike: float
    zone_type: str                         # MAGNETIC | EXPLOSIVE | RESISTANCE | SUPPORT
    strength: float                        # 0-100 (depuis gravity_map)
    oi_usd_total: float                    # OI total USD du strike (options non-expirées)
    contribution_pct: float                # % de l'OI total de toutes les zones actives

    # Scores pondérés par activité (poids = OI USD)
    structural_score: float                # Σ(OI × spot) = OI brut non filtré
    active_score: float                    # Σ(OI × spot × flow_ratio)
    actionable_score: float                # Σ(OI × spot × flow × prox × urgency)

    # Répartition par bucket
    dormant: GravityZoneCategoryBreakdown
    structural: GravityZoneCategoryBreakdown
    active: GravityZoneCategoryBreakdown
    actionable: GravityZoneCategoryBreakdown

    # Verdict zone
    activity_tag: ActivityTag              # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    activity_label: str                    # Emoji + texte court (💀 / 🪨 / ⚡ / 🔥)
    activity_verdict: str                  # Phrase décisionnelle
    use_in_signal: bool

    # Signal quality
    signal_quality_score: int              # 0-10
    signal_quality_label: str
    signal_quality_color: str

    # Top 20 contributeurs OI de la zone
    top_contributors: List[dict] = field(default_factory=list)


@dataclass
class GravityActivityAudit:
    btc_price: float
    timestamp: float
    total_gravity_oi_usd: float            # Σ OI USD toutes zones actives (non-VOID)

    # Répartition globale (sur toutes les zones actives)
    global_dormant_pct: float
    global_structural_pct: float
    global_active_pct: float
    global_actionable_pct: float

    # Scores pondérés globaux
    global_structural_score: float         # OI brut total en USD
    global_active_score: float             # Σ(OI × flow) USD
    global_actionable_score: float         # Σ(OI × flow × prox × urgency) USD
    global_active_engine_pct: float        # |active| / |structural| × 100
    global_actionable_engine_pct: float    # |actionable| / |active| × 100

    # Verdict global
    overall_tag: ActivityTag
    overall_label: str
    overall_verdict: str

    # Signal quality global
    signal_quality_score: int
    signal_quality_label: str
    signal_quality_color: str
    use_in_signal: bool

    # Zones triées par OI desc
    zones: List[GravityZoneAudit] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tag_option(flow: float, prox: float, urgency: float) -> ActivityTag:
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
) -> Tuple[int, str, str, bool]:
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
        label, color, use = "Signal fort", "green", True
    elif score >= 6:
        label, color, use = "Signal fiable", "green", True
    elif score >= 5:
        label, color, use = "Signal modéré", "yellow", True
    elif score >= 3:
        label, color, use = "Signal faible", "orange", False
    else:
        label, color, use = "Signal peu fiable", "red", False

    return score, label, color, use


def _activity_label_verdict(
    tag: ActivityTag,
    dormant_pct: float,
    actionable_pct: float,
) -> Tuple[str, str]:
    if tag == TAG_DORMANT or dormant_pct >= 60:
        return (
            "💀 Gravity Dormant",
            f"La zone est construite majoritairement sur de l'OI inactif ({dormant_pct:.0f}%). "
            "Ne pas utiliser comme cible court terme.",
        )
    if tag == TAG_STRUCTURAL:
        return (
            "🪨 Gravity Structurelle",
            "OI longue date dominant. Contexte de fond — surveille les flux avant d'agir.",
        )
    if tag == TAG_ACTIVE:
        return (
            "⚡ Gravity Active",
            "Zone surveillée par le marché. Flux récent détecté — pertinence à moyen terme.",
        )
    return (
        "🔥 Gravity Actionnable",
        f"Zone susceptible d'influencer BTC maintenant ({actionable_pct:.0f}% actionnable). "
        "Signal exploitable à court terme.",
    )


# ── Main computation ──────────────────────────────────────────────────────────

def compute_gravity_activity_audit(snapshot: MarketSnapshot) -> GravityActivityAudit:
    spot = snapshot.btc_price

    # Calcul des zones Gravity (sans modifier le moteur)
    gex = compute_gex(snapshot)
    gmap = compute_gravity_map(snapshot, gex)

    # Index options par strike exact
    options_by_strike: Dict[float, List[OptionData]] = {}
    for opt in snapshot.options:
        options_by_strike.setdefault(opt.strike, []).append(opt)

    # OI total de référence (zones actives uniquement, excl. VOID)
    active_zones = [z for z in gmap.zones if z.zone_type != "VOID"]
    total_gravity_oi_usd = sum(z.oi_usd for z in active_zones) or 1.0

    zone_audits: List[GravityZoneAudit] = []

    # Accumulateurs globaux (scores pondérés)
    g_structural = 0.0
    g_active = 0.0
    g_actionable = 0.0
    g_dormant_oi = 0.0
    g_structural_oi = 0.0
    g_active_oi = 0.0
    g_actionable_oi = 0.0

    for zone in active_zones:
        strike = zone.center
        opts = options_by_strike.get(strike, [])

        buckets: Dict[ActivityTag, dict] = {
            TAG_DORMANT:    {"oi_usd": 0.0, "count": 0, "contributors": []},
            TAG_STRUCTURAL: {"oi_usd": 0.0, "count": 0, "contributors": []},
            TAG_ACTIVE:     {"oi_usd": 0.0, "count": 0, "contributors": []},
            TAG_ACTIONABLE: {"oi_usd": 0.0, "count": 0, "contributors": []},
        }

        zone_structural = 0.0
        zone_active = 0.0
        zone_actionable = 0.0
        zone_anomaly_count = 0

        for opt in opts:
            dte = _compute_dte(opt.expiry)
            if dte <= 0:
                continue

            oi_usd = opt.oi * spot
            flow, is_anomaly = compute_flow_ratio(opt)
            prox = compute_proximity_score(opt.strike, spot)
            urgency = compute_dte_urgency(dte)

            if is_anomaly:
                zone_anomaly_count += 1

            tag = _tag_option(flow, prox, urgency)
            b = buckets[tag]
            b["oi_usd"] += oi_usd
            b["count"] += 1
            b["contributors"].append({
                "instrument": opt.instrument,
                "expiry": opt.expiry,
                "dte": dte,
                "option_type": opt.option_type,
                "oi": round(opt.oi, 1),
                "volume": round(opt.volume, 1),
                "oi_usd": round(oi_usd, 0),
                "flow_ratio": round(flow, 3),
                "prox": round(prox, 3),
                "urgency": round(urgency, 3),
                "activity_tag": tag,
            })

            zone_structural += oi_usd
            zone_active += oi_usd * flow
            zone_actionable += oi_usd * flow * prox * urgency

        total_zone_oi = zone_structural or 1.0

        # Pourcentages pour signal quality
        dormant_pct = buckets[TAG_DORMANT]["oi_usd"] / total_zone_oi * 100
        active_and_actionable_pct = (
            buckets[TAG_ACTIVE]["oi_usd"] + buckets[TAG_ACTIONABLE]["oi_usd"]
        ) / total_zone_oi * 100
        actionable_only_pct = buckets[TAG_ACTIONABLE]["oi_usd"] / total_zone_oi * 100

        # Profil d'activité via le moteur central (flow ratios pondérés)
        abs_zone_structural = abs(zone_structural)
        active_engine_pct = (
            abs(zone_active) / abs_zone_structural * 100
            if abs_zone_structural > 1e-9 else 0.0
        )
        abs_zone_active = abs(zone_active)
        actionable_engine_pct = (
            abs(zone_actionable) / abs_zone_active * 100
            if abs_zone_active > 1e-9 else 0.0
        )
        activity_tag = classify_activity_profile(active_engine_pct, actionable_engine_pct)
        if dormant_pct >= 60:
            activity_tag = TAG_DORMANT

        sq_score, sq_label, sq_color, use_in_signal = _compute_signal_quality(
            dormant_pct, active_and_actionable_pct, actionable_only_pct, zone_anomaly_count
        )
        # DORMANT via active_engine_pct < 5% peut coexister avec dormant_pct bas → use_in_signal
        # resterait True sans cette correction (bug : circuits indépendants).
        if activity_tag == TAG_DORMANT:
            use_in_signal = False
        activity_label, activity_verdict = _activity_label_verdict(
            activity_tag, dormant_pct, actionable_only_pct
        )

        # Top 20 contributeurs (tous buckets, triés par OI USD desc)
        all_contributors = [
            c for b in buckets.values() for c in b["contributors"]
        ]
        for c in all_contributors:
            c["contribution_pct"] = round(c["oi_usd"] / total_zone_oi * 100, 2)
        top_20 = sorted(all_contributors, key=lambda x: x["oi_usd"], reverse=True)[:20]

        def _breakdown(tag: str) -> GravityZoneCategoryBreakdown:
            b = buckets[tag]
            return GravityZoneCategoryBreakdown(
                oi_usd=round(b["oi_usd"], 0),
                oi_pct=round(b["oi_usd"] / total_zone_oi * 100, 1),
                count=b["count"],
            )

        # Contribution_pct = part de cette zone dans le total Gravity OI
        contribution_pct = round(zone.oi_usd / total_gravity_oi_usd * 100, 1)

        zone_audits.append(GravityZoneAudit(
            strike=strike,
            zone_type=zone.zone_type,
            strength=zone.strength,
            oi_usd_total=round(total_zone_oi, 0),
            contribution_pct=contribution_pct,
            structural_score=round(zone_structural, 0),
            active_score=round(zone_active, 0),
            actionable_score=round(zone_actionable, 0),
            dormant=_breakdown(TAG_DORMANT),
            structural=_breakdown(TAG_STRUCTURAL),
            active=_breakdown(TAG_ACTIVE),
            actionable=_breakdown(TAG_ACTIONABLE),
            activity_tag=activity_tag,
            activity_label=activity_label,
            activity_verdict=activity_verdict,
            use_in_signal=use_in_signal,
            signal_quality_score=sq_score,
            signal_quality_label=sq_label,
            signal_quality_color=sq_color,
            top_contributors=top_20,
        ))

        # Agrégation globale
        g_structural += zone_structural
        g_active += zone_active
        g_actionable += zone_actionable
        g_dormant_oi += buckets[TAG_DORMANT]["oi_usd"]
        g_structural_oi += buckets[TAG_STRUCTURAL]["oi_usd"]
        g_active_oi += buckets[TAG_ACTIVE]["oi_usd"]
        g_actionable_oi += buckets[TAG_ACTIONABLE]["oi_usd"]

    # Trier par OI total desc
    zone_audits.sort(key=lambda z: z.oi_usd_total, reverse=True)

    # Verdict global
    total_global_oi = g_structural or 1.0
    g_dormant_pct = g_dormant_oi / total_global_oi * 100
    g_structural_pct = g_structural_oi / total_global_oi * 100
    g_active_pct = g_active_oi / total_global_oi * 100
    g_actionable_pct = g_actionable_oi / total_global_oi * 100
    g_active_and_actionable_pct = g_active_pct + g_actionable_pct

    abs_g_structural = abs(g_structural)
    g_active_engine_pct = (
        abs(g_active) / abs_g_structural * 100 if abs_g_structural > 1e-9 else 0.0
    )
    abs_g_active = abs(g_active)
    g_actionable_engine_pct = (
        abs(g_actionable) / abs_g_active * 100 if abs_g_active > 1e-9 else 0.0
    )

    overall_tag = classify_activity_profile(g_active_engine_pct, g_actionable_engine_pct)
    if g_dormant_pct >= 60:
        overall_tag = TAG_DORMANT

    g_sq_score, g_sq_label, g_sq_color, g_use_in_signal = _compute_signal_quality(
        g_dormant_pct, g_active_and_actionable_pct, g_actionable_pct, 0
    )
    if overall_tag == TAG_DORMANT:
        g_use_in_signal = False
    overall_label, overall_verdict = _activity_label_verdict(
        overall_tag, g_dormant_pct, g_actionable_pct
    )

    return GravityActivityAudit(
        btc_price=spot,
        timestamp=snapshot.timestamp,
        total_gravity_oi_usd=round(total_gravity_oi_usd, 0),
        global_dormant_pct=round(g_dormant_pct, 1),
        global_structural_pct=round(g_structural_pct, 1),
        global_active_pct=round(g_active_pct, 1),
        global_actionable_pct=round(g_actionable_pct, 1),
        global_structural_score=round(g_structural, 0),
        global_active_score=round(g_active, 0),
        global_actionable_score=round(g_actionable, 0),
        global_active_engine_pct=round(g_active_engine_pct, 1),
        global_actionable_engine_pct=round(g_actionable_engine_pct, 1),
        overall_tag=overall_tag,
        overall_label=overall_label,
        overall_verdict=overall_verdict,
        signal_quality_score=g_sq_score,
        signal_quality_label=g_sq_label,
        signal_quality_color=g_sq_color,
        use_in_signal=g_use_in_signal,
        zones=zone_audits,
    )
