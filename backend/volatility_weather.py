"""
Volatility Weather — 4 états visuels brandables pour le dashboard.

☀️  CALM      → IV basse, GEX positif, marché sous contrôle
⛅  TRANSITION → Signaux mixtes, direction incertaine
⛈️  EXPLOSIVE  → IV élevée, GEX négatif, moves violents attendus
🌪️  CHAOS      → Marché désordonné, liquidations possibles
"""

from dataclasses import dataclass
from .gex import GEXProfile
from .mopi import MOPIScore


WEATHER_STATES = {
    "CALM": {"emoji": "☀️", "color": "#22c55e", "label": "CALME"},
    "TRANSITION": {"emoji": "⛅", "color": "#eab308", "label": "TRANSITION"},
    "EXPLOSIVE": {"emoji": "⛈️", "color": "#f97316", "label": "EXPLOSIF"},
    "CHAOS": {"emoji": "🌪️", "color": "#ef4444", "label": "CHAOS"},
}


@dataclass
class WeatherReport:
    state: str
    emoji: str
    color: str
    label: str
    description: str
    iv_rank: float
    gex_regime: str
    mopi_score: float


def compute_weather(gex: GEXProfile, mopi: MOPIScore) -> WeatherReport:
    iv_rank = mopi.iv_rank
    gex_regime = gex.regime

    if iv_rank < 30 and gex_regime == "STABILISANT":
        state = "CALM"
        description = "Marché stable. Les variations restent contenues. Pas de mouvement brutal attendu."
    elif iv_rank > 70 and gex_regime == "AMPLIFICATEUR":
        if iv_rank > 85:
            state = "CHAOS"
            description = "Conditions extrêmes. Liquidations en cascade possibles. Capital à risque."
        else:
            state = "EXPLOSIVE"
            description = "La volatilité monte et les prochains mouvements peuvent être amplifiés. Reste prudent dans les deux sens."
    else:
        state = "TRANSITION"
        description = "Signaux mixtes. Direction incertaine. Attends confirmation avant d'entrer."

    meta = WEATHER_STATES[state]
    return WeatherReport(
        state=state,
        emoji=meta["emoji"],
        color=meta["color"],
        label=meta["label"],
        description=description,
        iv_rank=iv_rank,
        gex_regime=gex_regime,
        mopi_score=mopi.score,
    )


def weather_telegram_msg(w: WeatherReport) -> str:
    actions = {
        "CALM":       "Pas d'urgence — surveille un changement de volatilité avant d'agir.",
        "TRANSITION": "Attends une confirmation de direction avant d'entrer sur une position.",
        "EXPLOSIVE":  "Reste prudent — le prochain mouvement peut surprendre dans les deux sens.",
        "CHAOS":      "Réduis l'exposition. Les conditions sont dangereuses.",
    }
    action = actions.get(w.state, "Surveille l'évolution du marché.")
    return (
        f"{w.emoji} **Météo Marché : {w.label}**\n"
        f"{w.description}\n"
        f"▶ {action}"
    )
