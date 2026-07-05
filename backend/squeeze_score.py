"""
Squeeze Score Avancé — Probabilité de mouvement violent (squeeze / breakout).

5 signaux combinés :
  1. GEX polarity   — GEX négatif amplifie les mouvements
  2. IV compression — IV rank faible = ressort comprimé
  3. OI concentration — OI concentré sur peu de strikes = tension binaire
  4. Gamma wall proximity — prix proche d'un wall = squeeze imminent
  5. Dealer convergence — dealers et GEX pointent même direction = cascade

Score 0-100 : 0=calme, 100=squeeze imminent.
"""

from dataclasses import dataclass
from typing import List

from .deribit_client import MarketSnapshot
from .gex import GEXProfile, _compute_dte
from .dealer_pressure import DealerPressure
from .options_activity_engine import compute_dte_urgency


@dataclass
class SqueezeSignal:
    name: str
    score: float       # contribution 0-100
    weight: float
    description: str


@dataclass
class SqueezeScore:
    score: float                     # 0-100
    label: str                       # DORMANT / BUILDING / IMMINENT / CRITICAL
    emoji: str
    probability_pct: float           # =score, alias lisible
    signals: List[SqueezeSignal]
    dominant_signal: str
    direction_bias: str              # "UP" | "DOWN" | "NEUTRAL"
    trigger_zone: float              # prix déclencheur estimé
    global_risk_label: str = ""      # "risque global : faible | modéré | élevé | critique"
    local_risk_label: str = ""       # "risque local sous $X : élevé" (si flip proche)
    local_risk_level: float = 0.0    # niveau du risque local (0 si absent)


def compute_squeeze_score(
    snapshot: MarketSnapshot,
    gex_profile: GEXProfile,
    dealer_pressure: DealerPressure,
    iv_rank: float,                  # 0-100
) -> SqueezeScore:
    spot = snapshot.btc_price
    signals: List[SqueezeSignal] = []

    # ── Signal 1 : GEX Polarity ─────────────────────────────────────────────
    # Near-term gamma effectif uniquement (DTE ≤ 14, pondéré distance × delta × activité).
    # Seuils ~10x plus petits que pour total_gex — magnitude near-term réduite.
    gex_near = gex_profile.gex_near
    if gex_near <= -300_000_000:
        s1 = 95.0; desc1 = "Market makers sans filet — chaque mouvement est amplifié"
    elif gex_near <= -50_000_000:
        s1 = 80.0; desc1 = "Les market makers amplifient les mouvements"
    elif gex_near <= -10_000_000:
        s1 = 65.0; desc1 = "Légère amplification en cours"
    elif gex_near < 10_000_000:
        s1 = 50.0; desc1 = "Les market makers sans biais net"
    elif gex_near < 50_000_000:
        s1 = 30.0; desc1 = "Les market makers freinent les mouvements"
    else:
        s1 = 10.0; desc1 = "Les market makers compriment fortement la volatilité"
    signals.append(SqueezeSignal("Effet Market Makers", s1, 0.30, desc1))

    # ── Signal 2 : IV Compression ─────────────────────────────────────────
    # IV rank < 20% = volatilité comprimée historiquement = ressort = squeeze probable
    if iv_rank < 10:
        s2 = 95.0; desc2 = "Volatilité au plus bas des 90 derniers jours — ressort maximum"
    elif iv_rank < 20:
        s2 = 80.0; desc2 = "Volatilité très comprimée — ressort prêt à se détendre"
    elif iv_rank < 30:
        s2 = 65.0; desc2 = "Volatilité sous sa moyenne — compression en cours"
    elif iv_rank < 50:
        s2 = 45.0; desc2 = "Volatilité dans sa zone normale"
    elif iv_rank < 70:
        s2 = 30.0; desc2 = "Volatilité élevée — un mouvement est déjà intégré"
    else:
        s2 = 15.0; desc2 = "Volatilité extrême — le marché est déjà en alerte"
    signals.append(SqueezeSignal("Compression Volatilité", s2, 0.25, desc2))

    # ── Signal 3 : OI Concentration ───────────────────────────────────────
    # Si >50% de l'OI effectif est sur 3 strikes ou moins → tension binaire near-term.
    # Pondéré par DTE urgency — far-term OI compte moins (impact hedging différé).
    oi_by_strike: dict[float, float] = {}
    for opt in snapshot.options:
        dte_eff = _compute_dte(opt.expiry)
        urg = compute_dte_urgency(dte_eff)
        oi_by_strike[opt.strike] = oi_by_strike.get(opt.strike, 0) + opt.oi * urg
    total_oi = sum(oi_by_strike.values()) or 1.0
    top3_oi = sum(sorted(oi_by_strike.values(), reverse=True)[:3])
    concentration = top3_oi / total_oi
    if concentration > 0.65:
        s3 = 90.0; desc3 = f"OI concentré à {concentration*100:.0f}% sur 3 strikes — tension binaire"
    elif concentration > 0.50:
        s3 = 70.0; desc3 = f"OI concentré à {concentration*100:.0f}% — pression focalisée"
    elif concentration > 0.35:
        s3 = 45.0; desc3 = f"OI modérément concentré ({concentration*100:.0f}%)"
    else:
        s3 = 20.0; desc3 = "OI dispersé — tension diffuse"
    signals.append(SqueezeSignal("OI Concentration", s3, 0.20, desc3))

    # ── Signal 4 : Gamma Wall Proximity ──────────────────────────────────
    # Prix < 2% d'un gamma wall → squeeze au franchissement
    walls = gex_profile.gamma_walls
    if walls:
        nearest_wall = min(walls, key=lambda w: abs(w - spot))
        dist_pct = abs(nearest_wall - spot) / spot * 100
        if dist_pct < 1.0:
            s4 = 95.0; desc4 = f"À {dist_pct:.1f}% du gamma wall ${nearest_wall:,.0f} — déclenchement imminent"
        elif dist_pct < 2.0:
            s4 = 80.0; desc4 = f"Wall ${nearest_wall:,.0f} à {dist_pct:.1f}% — tension maximale"
        elif dist_pct < 4.0:
            s4 = 60.0; desc4 = f"Wall ${nearest_wall:,.0f} à {dist_pct:.1f}% — surveillance"
        elif dist_pct < 7.0:
            s4 = 35.0; desc4 = f"Wall distant ({dist_pct:.1f}%)"
        else:
            s4 = 15.0; desc4 = "Aucun wall proche"
        trigger_zone = nearest_wall
    else:
        s4 = 30.0; desc4 = "Pas de gamma wall identifié"
        trigger_zone = spot
    signals.append(SqueezeSignal("Wall Proximity", s4, 0.15, desc4))

    # ── Signal 5 : Dealer Convergence ────────────────────────────────────
    # Dealers et GEX pointent même direction = cascade de hedging
    dealer_dir = dealer_pressure.direction
    gex_regime = gex_profile.regime
    if gex_regime == "AMPLIFICATEUR" and dealer_dir in ("BULLISH_FLOWS", "BEARISH_FLOWS"):
        s5 = 85.0
        desc5 = "Market makers amplifient + pression directionnelle — cascade probable"
    elif gex_regime == "AMPLIFICATEUR":
        s5 = 65.0; desc5 = "Market makers en mode amplification, pression neutre"
    elif dealer_dir in ("BULLISH_FLOWS", "BEARISH_FLOWS") and gex_regime == "NEUTRE":
        s5 = 50.0; desc5 = "Pression directionnelle sans amplification"
    else:
        s5 = 25.0; desc5 = "Pas de convergence directionnelle"
    signals.append(SqueezeSignal("Convergence Directionnelle", s5, 0.10, desc5))

    # ── Score final ───────────────────────────────────────────────────────
    score = sum(sig.score * sig.weight for sig in signals)
    score = round(max(0, min(100, score)), 1)

    # ── Direction bias ────────────────────────────────────────────────────
    _flip = gex_profile.flip_level
    if dealer_pressure.direction == "BULLISH_FLOWS" or (gex_near < 0 and _flip is not None and _flip < spot):
        direction_bias = "UP"
    elif dealer_pressure.direction == "BEARISH_FLOWS" or (gex_near < 0 and _flip is not None and _flip > spot):
        direction_bias = "DOWN"
    else:
        direction_bias = "NEUTRAL"

    # ── Dominant signal ───────────────────────────────────────────────────
    dominant = max(signals, key=lambda s: s.score * s.weight)

    label, emoji = _classify_squeeze(score)

    # Labels global/local pour éviter le double discours
    global_risk_label = f"risque global : {_score_to_risk_label(score)}"
    local_risk_label, local_risk_level = _compute_local_risk(
        gex_profile.flip_level, spot, gex_profile.regime
    )

    return SqueezeScore(
        score=score,
        label=label,
        emoji=emoji,
        probability_pct=score,
        signals=signals,
        dominant_signal=dominant.name,
        direction_bias=direction_bias,
        trigger_zone=trigger_zone,
        global_risk_label=global_risk_label,
        local_risk_label=local_risk_label,
        local_risk_level=local_risk_level,
    )


def _score_to_risk_label(score: float) -> str:
    if score >= 80: return "critique"
    if score >= 60: return "élevé"
    if score >= 40: return "modéré"
    return "faible"


def _compute_local_risk(flip_level, spot: float, regime: str) -> tuple:
    """Risque local : flip level proche = danger asymétrique concentré."""
    if flip_level is None or flip_level <= 0:
        return "", 0.0
    dist_pct = abs(flip_level - spot) / spot * 100
    if dist_pct > 8:
        return "", 0.0
    side = "sous" if flip_level < spot else "au-dessus de"
    if dist_pct < 2:
        intensity = "critique"
    elif dist_pct < 3.5:
        intensity = "élevé"
    elif dist_pct < 5:
        intensity = "modéré"
    else:
        intensity = "faible"
    return f"risque local {side} ${flip_level:,.0f} : {intensity}", flip_level


def _classify_squeeze(score: float):
    if score >= 80:
        return "EXPLOSION IMMINENTE", "🚨"
    elif score >= 60:
        return "MOUVEMENT IMMINENT", "⚡"
    elif score >= 40:
        return "TENSION CROISSANTE", "🔶"
    return "DORMANT", "😴"


def squeeze_summary(sq: SqueezeScore) -> str:
    bar = "█" * int(sq.score / 10) + "░" * (10 - int(sq.score / 10))
    bias_arrow = {"UP": "⬆️", "DOWN": "⬇️", "NEUTRAL": "➡️"}.get(sq.direction_bias, "")
    lines = [
        f"{sq.emoji} **Squeeze Score {sq.score:.0f}/100 — {sq.label}**",
        f"`{bar}` {bias_arrow}",
        f"Signal dominant: {sq.dominant_signal}",
        f"Zone déclencheur: ${sq.trigger_zone:,.0f}",
    ]
    for sig in sorted(sq.signals, key=lambda s: s.score, reverse=True)[:3]:
        lines.append(f"  • {sig.name}: {sig.description}")
    return "\n".join(lines)
