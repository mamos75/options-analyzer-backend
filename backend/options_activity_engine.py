"""
Options Activity Engine — moteur central réutilisable.

Distingue 3 niveaux d'activité pour tout ensemble d'options :

  STRUCTURAL  = gros OI, flux faible    → position longue terme
  ACTIVE      = OI + flux volume        → activité récente, surveiller
  ACTIONABLE  = flux + proximité + DTE  → impact immédiat BTC, alerte possible
  DORMANT     = OI ou flux nuls         → à ignorer, JAMAIS alerter

Règle fondamentale :
  Un niveau important n'est pas forcément actif.
  Un niveau actif n'est pas forcément actionnable.
  Le dashboard doit distinguer les trois.
"""

from dataclasses import dataclass
from typing import List, Tuple

from .deribit_client import OptionData

# ── Constantes ────────────────────────────────────────────────────────────────
CONTRACT_SIZE      = 1.0    # 1 BTC par contrat Deribit
MIN_OI_FLOW        = 50     # OI minimum requis pour un flow_ratio fiable
FLOW_RATIO_CAP     = 1.0    # Volume/OI plafonné à 1 — artifact sur petit OI
LOW_OI_ANOMALY_OI  = 200    # Seuil : petit OI + gros volume → signal non fiable

# Seuils de classification (en %)
_DORMANT_ACTIVE_MAX     = 5.0   # active/structural < 5%  → DORMANT
_STRUCTURAL_ACTION_MAX  = 15.0  # actionable/active < 15% → STRUCTURAL
_ACTIVE_ACTION_MAX      = 30.0  # actionable/active < 30% → ACTIVE (sinon ACTIONABLE)

ActivityTag = str
TAG_DORMANT    = "DORMANT"
TAG_STRUCTURAL = "STRUCTURAL"
TAG_ACTIVE     = "ACTIVE"
TAG_ACTIONABLE = "ACTIONABLE"


@dataclass
class ActivityScores:
    """Scores Structural / Active / Actionable pour un ensemble d'options."""
    structural: float        # BTC (mode DEX) ou OI brut (mode walls)
    active: float            # pondéré par flux volume/OI
    actionable: float        # flux × proximité spot × urgence DTE
    active_pct: float        # |active| / |structural| × 100
    actionable_pct: float    # |actionable| / |active| × 100
    profile: ActivityTag     # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    low_oi_anomaly_count: int


def compute_flow_ratio(opt: OptionData) -> Tuple[float, bool]:
    """Retourne (flow_ratio_cappé, is_low_oi_anomaly).

    OI < MIN_OI_FLOW → 0.0 (signal non fiable).
    Cap à 1.0 — volume/OI > 1 sur petit OI = artifact statistique.
    """
    if opt.oi < MIN_OI_FLOW:
        return 0.0, False
    raw = opt.volume / opt.oi if opt.oi > 0 else 0.0
    is_anomaly = raw > FLOW_RATIO_CAP and opt.oi < LOW_OI_ANOMALY_OI
    return min(raw, FLOW_RATIO_CAP), is_anomaly


def compute_proximity_score(strike: float, spot: float) -> float:
    """1.0 au spot, décroissance linéaire jusqu'à 0.0 à ±10% de distance."""
    dist_pct = abs(strike - spot) / spot
    return max(0.0, 1.0 - dist_pct / 0.10)


def compute_dte_urgency(dte: int) -> float:
    """Facteur d'urgence par DTE. 1.0 si DTE≤1, 0.05 au-delà de 30j."""
    if dte <= 0:
        return 0.0
    if dte <= 1:
        return 1.0
    if dte <= 3:
        return 0.8
    if dte <= 7:
        return 0.5
    if dte <= 14:
        return 0.3
    if dte <= 30:
        return 0.15
    return 0.05


def classify_activity_profile(active_pct: float, actionable_pct: float) -> ActivityTag:
    """Classifie le profil d'activité en 4 catégories.

    DORMANT     : active_pct < 5%            → pas de flux, niveau inactif
    STRUCTURAL  : active_pct ≥ 5%, action < 15%  → flux mais lointain/long-daté
    ACTIVE      : actionable_pct 15-30%      → flux sans impact immédiat fort
    ACTIONABLE  : actionable_pct ≥ 30%       → flux + proximité + DTE → impact BTC maintenant

    DORMANT ne génère jamais d'alerte Telegram.
    """
    if active_pct < _DORMANT_ACTIVE_MAX:
        return TAG_DORMANT
    if actionable_pct < _STRUCTURAL_ACTION_MAX:
        return TAG_STRUCTURAL
    if actionable_pct < _ACTIVE_ACTION_MAX:
        return TAG_ACTIVE
    return TAG_ACTIONABLE


def compute_structural_active_actionable(
    options: List[OptionData],
    spot: float,
    use_dealer_delta: bool = True,
) -> ActivityScores:
    """Calcule les 3 niveaux d'activité sur un ensemble d'options.

    use_dealer_delta=True  → mode DEX  : weight = -opt.delta × CONTRACT_SIZE
    use_dealer_delta=False → mode OI brut (walls) : weight = 1.0
    """
    from .gex import _compute_dte

    structural = 0.0
    active = 0.0
    actionable = 0.0
    anomaly_count = 0

    for opt in options:
        oi = opt.oi
        weight = (-opt.delta * CONTRACT_SIZE) if use_dealer_delta else 1.0

        structural += weight * oi

        flow, is_anomaly = compute_flow_ratio(opt)
        if is_anomaly:
            anomaly_count += 1

        active += weight * oi * flow

        dte = _compute_dte(opt.expiry)
        prox = compute_proximity_score(opt.strike, spot)
        urgency = compute_dte_urgency(dte)
        actionable += weight * oi * flow * prox * urgency

    abs_structural = abs(structural)
    active_pct = (abs(active) / abs_structural * 100) if abs_structural > 1e-9 else 0.0
    abs_active = abs(active)
    actionable_pct = (abs(actionable) / abs_active * 100) if abs_active > 1e-9 else 0.0

    return ActivityScores(
        structural=round(structural, 4),
        active=round(active, 4),
        actionable=round(actionable, 4),
        active_pct=round(active_pct, 2),
        actionable_pct=round(actionable_pct, 2),
        profile=classify_activity_profile(active_pct, actionable_pct),
        low_oi_anomaly_count=anomaly_count,
    )
