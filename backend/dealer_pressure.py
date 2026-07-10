"""
Dealer Hedging Pressure — delta net position des market makers.

Les dealers sont la contrepartie de chaque trade :
- Traders achètent calls → dealers short calls → delta dealer = -OI × delta_call
- Traders achètent puts  → dealers short puts  → delta dealer = -OI × delta_put

delta_call > 0, delta_put < 0

delta_net > 0 → dealers long BTC → vendent si prix monte → résistance haussière
delta_net < 0 → dealers short BTC → achètent si prix monte → soutien dynamique

Règle fondamentale :
  Gamma mesure l'impact mécanique  → GEX = gamma × OI × spot²
  Delta mesure l'exposition directionnelle → DEX = delta × OI × contract_size
  Ne jamais mélanger gamma et delta.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .deribit_client import MarketSnapshot, OptionData
from .options_activity_engine import (
    compute_flow_ratio as _compute_flow_ratio,
    compute_proximity_score as _proximity,
    compute_dte_urgency as _dte_urgency,
    compute_structural_active_actionable,
    CONTRACT_SIZE,
    MIN_OI_FLOW   as _MIN_OI_FLOW,
    FLOW_RATIO_CAP as _FLOW_RATIO_CAP,
    LOW_OI_ANOMALY_OI as _LOW_OI_ANOMALY_OI,
)


@dataclass
class DealerPressure:
    net_delta: float                          # en BTC (signé)
    net_delta_usd: float                      # en USD
    delta_by_strike: Dict[float, float]       # strike → delta net dealer
    direction: str                            # "BULLISH_FLOWS" | "BEARISH_FLOWS" | "NEUTRAL"
    intensity: str                            # "EXTREME" | "HIGH" | "MODERATE" | "LOW"
    pressure_pct: float                       # -100 à +100 (normalisé)
    gauge_color: str                          # "green" | "red" | "yellow" — couleur CORRECTE pour la jauge
    flux_conditionnel: str                    # action conditionnelle des dealers (soutien/résistance)
    direction_risque_trader: str              # "SOUTIEN" | "RESISTANCE" | "NEUTRE"
    exposition_nette_btc: float               # abs(net_delta) — exposition brute


def compute_dealer_pressure(snapshot: MarketSnapshot) -> DealerPressure:
    spot = snapshot.btc_price
    net_delta = 0.0
    delta_by_strike: Dict[float, float] = {}

    for opt in snapshot.options:
        # Dealers short l'option → leur delta = -OI × delta_instrument
        dealer_delta = -opt.oi * opt.delta
        net_delta += dealer_delta
        delta_by_strike[opt.strike] = delta_by_strike.get(opt.strike, 0) + dealer_delta

    net_delta_usd = net_delta * spot

    direction = _classify_direction(net_delta)
    intensity = _classify_intensity(abs(net_delta))
    pressure_pct = _normalize(net_delta)
    gauge_color = _gauge_color(direction)
    flux_conditionnel = _flux_conditionnel(net_delta, direction)
    direction_risque_trader = _direction_risque(direction)

    return DealerPressure(
        net_delta=round(net_delta, 2),
        net_delta_usd=round(net_delta_usd, 0),
        delta_by_strike={k: round(v, 4) for k, v in sorted(delta_by_strike.items())},
        direction=direction,
        intensity=intensity,
        pressure_pct=round(pressure_pct, 1),
        gauge_color=gauge_color,
        flux_conditionnel=flux_conditionnel,
        direction_risque_trader=direction_risque_trader,
        exposition_nette_btc=round(abs(net_delta), 2),
    )


def _gauge_color(direction: str) -> str:
    """Couleur jauge DEX — BULLISH_FLOWS = vert (soutien), BEARISH_FLOWS = rouge (résistance)."""
    if direction == "BULLISH_FLOWS":
        return "green"
    if direction == "BEARISH_FLOWS":
        return "red"
    return "yellow"


def _flux_conditionnel(net_delta: float, direction: str) -> str:
    """Ce que les dealers FERONT conditionnellement (hedging delta)."""
    abs_delta = abs(net_delta)
    if direction == "BULLISH_FLOWS":
        return f"Les dealers doivent acheter ~{abs_delta:,.0f} BTC si BTC monte — re-hedging short delta (soutien dynamique)"
    if direction == "BEARISH_FLOWS":
        return f"Les dealers doivent vendre ~{abs_delta:,.0f} BTC si BTC monte — re-hedging long delta (résistance dynamique)"
    return "Pas de biais directionnel notable des dealers"


def _direction_risque(direction: str) -> str:
    if direction == "BULLISH_FLOWS":
        return "SOUTIEN"
    if direction == "BEARISH_FLOWS":
        return "RESISTANCE"
    return "NEUTRE"


def _classify_direction(delta: float) -> str:
    if delta > 500:
        return "BEARISH_FLOWS"   # dealers long → hedging vend la hausse
    elif delta < -500:
        return "BULLISH_FLOWS"   # dealers short → hedging achète la hausse
    return "NEUTRAL"


def _classify_intensity(abs_delta: float) -> str:
    if abs_delta > 10_000:
        return "EXTREME"
    elif abs_delta > 5_000:
        return "HIGH"
    elif abs_delta > 1_000:
        return "MODERATE"
    return "LOW"


def _normalize(delta: float, cap: float = 20_000) -> float:
    return max(-100, min(100, delta / cap * 100))


@dataclass
class DEXLevels:
    """3 niveaux d'exposition delta dealers — Structural / Active / Actionable.

    Structural = Σ(delta × OI × contract_size)  — stock delta total (tout le carnet)
    Active     = Σ(delta × OI × flow_ratio)      — pondéré par activité récente
    Actionable = Σ(delta × OI × flow_ratio × proximity × dte_urgency)  — impactant BTC maintenant
    """
    structural: float         # en BTC (signé)
    active: float             # en BTC (signé)
    actionable: float         # en BTC (signé)
    structural_usd: float     # en USD
    active_usd: float         # en USD
    actionable_usd: float     # en USD
    low_oi_anomaly_count: int         # nb de strikes taggés "low OI anomaly"
    low_oi_anomaly_strikes: List[float]  # strikes concernés
    # Profil d'activité (depuis options_activity_engine)
    dex_profile: str = "STRUCTURAL"    # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    dex_active_pct: float = 0.0        # |active| / |structural| × 100
    dex_actionable_pct: float = 0.0    # |actionable| / |active| × 100


def compute_dex_levels(snapshot: MarketSnapshot) -> DEXLevels:
    """Calcule les 3 niveaux DEX (Structural / Active / Actionable).

    DEX = delta × OI × contract_size  (≠ GEX = gamma × OI × spot²)
    Les dealers sont short les options → delta dealer = -delta_instrument.
    Délègue au moteur central options_activity_engine.
    """
    spot = snapshot.btc_price
    scores = compute_structural_active_actionable(
        snapshot.options, spot, use_dealer_delta=True
    )

    # Tracking détaillé des strikes anomalies (non exposé par le moteur)
    anomaly_strikes: List[float] = []
    for opt in snapshot.options:
        _, is_anomaly = _compute_flow_ratio(opt)
        if is_anomaly and opt.strike not in anomaly_strikes:
            anomaly_strikes.append(opt.strike)

    return DEXLevels(
        structural=round(scores.structural, 2),
        active=round(scores.active, 2),
        actionable=round(scores.actionable, 2),
        structural_usd=round(scores.structural * spot, 0),
        active_usd=round(scores.active * spot, 0),
        actionable_usd=round(scores.actionable * spot, 0),
        low_oi_anomaly_count=scores.low_oi_anomaly_count,
        low_oi_anomaly_strikes=sorted(anomaly_strikes),
        dex_profile=scores.profile,
        dex_active_pct=scores.active_pct,
        dex_actionable_pct=scores.actionable_pct,
    )


def dealer_summary(dp: DealerPressure) -> str:
    arrow = "⬇️" if dp.direction == "BEARISH_FLOWS" else "⬆️" if dp.direction == "BULLISH_FLOWS" else "➡️"
    return (
        f"{arrow} **Dealer Pressure: {dp.direction}** ({dp.intensity})\n"
        f"Delta Net: {dp.net_delta:+,.0f} BTC (${dp.net_delta_usd:+,.0f})\n"
        f"Pression: {dp.pressure_pct:+.1f}%"
    )


def dex_narrative(dp: DealerPressure) -> str:
    """Titre headline — toujours cohérent avec gauge_color."""
    if dp.direction == "BEARISH_FLOWS":
        return "Résistance dealers : les dealers vendent BTC si le prix monte (re-hedging)."
    if dp.direction == "BULLISH_FLOWS":
        return "Soutien dealers : les dealers achètent BTC si le prix monte (re-hedging)."
    return "Pression dealers neutre."


def dex_subtitle(dp: DealerPressure) -> str:
    """Détail chiffré — exposition nette."""
    if dp.direction == "BEARISH_FLOWS":
        return f"Exposition nette : {dp.net_delta:+,.0f} BTC ({dp.net_delta_usd/1e6:+.1f}M$) — résistance {dp.intensity.lower()}"
    if dp.direction == "BULLISH_FLOWS":
        return f"Exposition nette : {dp.net_delta:+,.0f} BTC ({dp.net_delta_usd/1e6:+.1f}M$) — soutien {dp.intensity.lower()}"
    return f"Exposition nette : {dp.net_delta:+,.0f} BTC — neutre"
