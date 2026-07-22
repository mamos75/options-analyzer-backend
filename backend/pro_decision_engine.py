"""
pro_decision_engine.py — Moteur de décision "trader pro options"

Philosophie :
  Un trader pro options ne prédit pas la direction — il lit les FORCES MÉCANIQUES
  imposées par la structure du marché options (GEX, DEX, OI, walls, vol) et en
  déduit le chemin de moindre résistance, les niveaux d'invalidation précis,
  et la structure de trade la plus efficiente pour ce contexte.

Logique :
  1. Lire le RÉGIME (GEX : stabilisant / amplificateur / flip)
  2. Lire la PRESSION DEALER (DEX : qui doit hedger quoi)
  3. Lire la STRUCTURE OI (walls, gravity, max pain)
  4. Lire la VOLATILITÉ (IV rank, term structure, backwardation/contango)
  5. Lire LES PROBABILITÉS (moteur de règles 4h/24h/72h)
  6. Synthétiser : scénario, conviction, trade suggéré
  7. Sortir une DÉCISION CLAIRE avec raisonnement structuré
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import math

def _bias_label(score: float) -> str:
    """Retourne un label qualitatif pour le biais directionnel (-100..+100)."""
    if score >= 60:  return "Biais options : HAUSSIER FORT"
    if score >= 25:  return "Biais options : HAUSSIER MODERE"
    if score > -25:  return "Biais options : NEUTRE"
    if score > -60:  return "Biais options : BAISSIER MODERE"
    return "Biais options : BAISSIER FORT"



# ─────────────────────────────────────────────────────────────────────────────
# Structures de sortie
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeStructure:
    """Structure de trade suggérée par le moteur."""
    type: str                     # "CALL" | "PUT" | "CALL_SPREAD" | "PUT_SPREAD" | "STRADDLE" | "STRANGLE" | "WAIT"
    action: str                   # texte court : "Acheter PUT 63000 17JUL26"
    rationale: str                # pourquoi ce trade
    expiry_suggested: str         # "17JUL26"
    dte_suggested: int            # jours
    strike_primary: Optional[float] = None
    strike_secondary: Optional[float] = None  # pour spreads
    max_iv_acceptable: Optional[float] = None # IV max pour entrer (trop cher = ne pas acheter prime)
    sizing_pct: float = 0.0       # % du capital risqué suggéré (0-100)
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    risk_reward: Optional[float] = None


@dataclass
class RegimeReading:
    """Lecture du régime GEX."""
    regime: str                   # STABILISANT | AMPLIFICATEUR | ZONE_DE_FLIP | NEUTRE
    flip_level: Optional[float]
    flip_proximity_pct: float     # distance spot→flip en %
    flip_is_actionable: bool      # flip ACTIONABLE selon audit
    gex_total_b: float            # GEX en milliards $
    implication: str              # texte explicatif pro


@dataclass
class DealerReading:
    """Lecture de la pression dealer (DEX)."""
    direction: str                # BULLISH_FLOWS | BEARISH_FLOWS | NEUTRAL
    net_delta_btc: float
    actionable_btc: float
    is_actionable: bool           # DEX exploitable selon audit
    intensity: str                # EXTREME | FORT | MODERE | FAIBLE
    mechanic: str                 # ce que les dealers DOIVENT faire mécaniquement
    trader_implication: str       # ce que ça signifie pour le trader


@dataclass
class VolReading:
    """Lecture de la structure de volatilité."""
    iv_rank: float                # 0-100
    iv_regime: str                # COMPRIMEE | NORMALE | ELEVEE | EXTREME
    term_structure: str           # BACKWARDATION | FLAT | CONTANGO
    front_iv: float               # IV expiry la plus proche à fort OI
    back_iv: float                # IV expiry lointaine
    iv_spread: float              # back - front
    trade_implication: str        # acheter/vendre prime ?
    best_dte_range: str           # fourchette DTE recommandée


@dataclass
class ProbabilityReading:
    """Lecture du moteur de probabilités."""
    dominant_direction: str       # BULL | BEAR | NEUTRE
    dominant_horizon: str         # 4h | 24h | 72h
    dominant_prob: float          # probabilité dominante
    verdict_24h: str              # BIAIS_HAUSSIER | BIAIS_BAISSIER | EQUILIBRE
    verdict_72h: str
    confidence_rules: float       # confiance règles [0-100]
    edge_quality: str             # FORT | MODERE | FAIBLE | INEXISTANT
    summary: str


@dataclass
class KeyLevels:
    """Niveaux clés de la structure options."""
    spot: float
    flip_level: Optional[float]
    max_pain: Optional[float]
    call_wall: Optional[float]
    put_wall: Optional[float]
    gravity_magnet: Optional[float]
    nearest_resistance: Optional[float]
    nearest_support: Optional[float]
    upside_ladder: list
    downside_ladder: list


@dataclass
class RiskWarning:
    """Avertissement de risque."""
    level: str                    # CRITIQUE | ELEVE | MODERE | INFO
    category: str                 # VOL | REGIME | TIMING | LIQUIDITE | CONTRARIAN
    message: str


@dataclass
class ProDecision:
    """Décision complète du moteur pro."""
    # Synthèse principale
    verdict: str                  # "BEAR STRUCTUREL" | "BULL MECANIQUE" | "RANGE" | "ATTENDRE"
    verdict_emoji: str
    conviction: int               # 0-10
    conviction_label: str         # "Inexistante" | "Faible" | "Modérée" | "Forte" | "Très forte"
    action_label: str             # "ENTRER SHORT" | "ENTRER LONG" | "COUVRIR" | "ATTENDRE"
    action_color: str             # red | green | yellow | gray

    # Raisonnement structuré
    primary_thesis: str           # thèse principale (1 phrase pro)
    supporting_forces: list       # forces qui confirment
    opposing_forces: list         # forces qui s'opposent
    key_risk: str                 # risque principal

    # Niveaux
    levels: KeyLevels

    # Lectures détaillées
    regime: RegimeReading
    dealer: DealerReading
    vol: VolReading
    probability: ProbabilityReading

    # Trade suggéré
    trade: TradeStructure

    # Risques
    warnings: list

    # Méta
    scenario_type: str            # DIRECTIONNEL | VOLATILITE | NEUTRE | BINAIRE
    invalidation_price: Optional[float]
    invalidation_reason: str
    timestamp: float
    spot: float
    # P2/P3 — Arbiter souverain : conviction bridée + échelle unifiée
    arbiter_confidence_pct: Optional[int] = None   # confiance Arbiter 0-100 (source de vérité)
    global_confidence: Optional[int] = None        # P3 — alias public de arbiter_confidence_pct
    # P4 — TTL / pré-expiration
    pre_expiration_warning: Optional[str] = None   # phrase d'alerte si signal dominé par contrat expirant
    dominant_signal_dte: Optional[int] = None      # DTE du contrat signal dominant (pour cap trade DTE)


# ─────────────────────────────────────────────────────────────────────────────
# Fonctions d'analyse
# ─────────────────────────────────────────────────────────────────────────────

def _read_regime(snap: dict, narrative: dict) -> RegimeReading:
    db = snap.get("dashboard", {})
    regime = db.get("gex_regime", "NEUTRE")
    flip_level = db.get("flip_level")
    spot = snap.get("spot", db.get("btc_price", 0))
    gex_total = db.get("gex_total", 0) or 0
    flip_tag = narrative.get("flip_activity_tag", "DORMANT")

    flip_prox = 0.0
    if flip_level and spot:
        flip_prox = (flip_level - spot) / spot * 100

    # Implication pro selon régime
    implications = {
        "STABILISANT": (
            "Les dealers sont LONGS gamma → ils vendent la hausse et achètent la baisse. "
            "Effet pin : le spot est aspiré vers le flip/max pain. "
            "Les moves directionnels sont amortis. Éviter les achats de premium directionnel."
        ),
        "AMPLIFICATEUR": (
            "Les dealers sont COURTS gamma → ils achètent la hausse et vendent la baisse. "
            "Chaque move est AMPLIFIÉ mécaniquement. "
            "C'est le régime idéal pour acheter des options directionnelles ou des straddles."
        ),
        "ZONE_DE_FLIP": (
            f"BTC est SUR la ligne de bascule ${flip_level:,.0f}. "
            "Un close au-dessus → régime STABILISANT (dealers absorbent). "
            "Un close en-dessous → régime AMPLIFICATEUR (dealers amplifient). "
            "Zone binaire à haut risque de faux breakout."
        ),
        "NEUTRE": (
            "GEX faible ou neutre — les dealers ont peu d'influence mécanique. "
            "Le marché suit la liquidité et le sentiment directement."
        ),
    }
    implication = implications.get(regime, "Régime indéterminé.")

    return RegimeReading(
        regime=regime,
        flip_level=flip_level,
        flip_proximity_pct=flip_prox,
        flip_is_actionable=(flip_tag == "ACTIONABLE"),
        gex_total_b=gex_total / 1e9,
        implication=implication,
    )


def _read_dealer(snap: dict, narrative: dict) -> DealerReading:
    dealer = snap.get("dealer", {})
    direction = dealer.get("direction", "NEUTRAL")
    net_delta = dealer.get("net_delta", dealer.get("structural", 0)) or 0
    actionable = dealer.get("actionable", 0) or 0
    intensity = dealer.get("intensity", "FAIBLE")
    dex_use = narrative.get("dex_use_in_signal", False)

    # Mécanique : ce que les dealers DOIVENT faire
    if direction == "BULLISH_FLOWS":
        mechanic = (
            f"Dealers SHORT delta ({abs(net_delta):,.0f} BTC net). "
            "Doivent ACHETER du BTC si le prix monte (re-hedging) → soutien mécanique à la hausse."
        )
        trader_impl = (
            "Dealers SHORT delta : doivent ACHETER du BTC si le prix monte (re-hedging). "
            "Les dips sont mécaniquement achetés — soutien structurel à la hausse."
        )
    elif direction == "BEARISH_FLOWS":
        mechanic = (
            f"Dealers LONG delta ({abs(net_delta):,.0f} BTC net). "
            "Doivent VENDRE du BTC si le prix monte (re-hedging) → résistance mécanique à la hausse."
        )
        trader_impl = (
            "Dealers LONG delta : doivent VENDRE du BTC si le prix monte (re-hedging). "
            "Les rallies sont mécaniquement vendus — résistance structurelle à la hausse."
        )
    else:
        mechanic = "Pression dealer neutre — pas de hedging directionnel dominant."
        trader_impl = "Pas d'implication mécanique forte du côté dealer."

    return DealerReading(
        direction=direction,
        net_delta_btc=net_delta,
        actionable_btc=actionable,
        is_actionable=dex_use,
        intensity=intensity,
        mechanic=mechanic,
        trader_implication=trader_impl,
    )


def _read_vol(vol_structure: list, db: dict) -> VolReading:
    iv_rank = db.get("iv_rank", 50) or 50

    if iv_rank >= 80:
        iv_regime = "EXTREME"
    elif iv_rank >= 60:
        iv_regime = "ELEVEE"
    elif iv_rank >= 35:
        iv_regime = "NORMALE"
    else:
        iv_regime = "COMPRIMEE"

    # Analyser la term structure
    front_iv, back_iv = 0.0, 0.0
    term_structure = "FLAT"

    if vol_structure and len(vol_structure) >= 2:
        # Front = premier expiry avec OI significatif (>500 BTC)
        front_entries = [x for x in vol_structure if x.get("oi_pct", 0) > 1 and x.get("dte", 999) <= 20]
        back_entries  = [x for x in vol_structure if x.get("dte", 0) >= 60]

        if front_entries:
            front_iv = front_entries[0].get("iv", 0)
        if back_entries:
            back_iv = back_entries[-1].get("iv", 0)

        if front_iv and back_iv:
            iv_spread = back_iv - front_iv
            if iv_spread > 3:
                term_structure = "CONTANGO"     # back > front (normal)
            elif iv_spread < -3:
                term_structure = "BACKWARDATION" # front > back (stress)
            else:
                term_structure = "FLAT"
        else:
            iv_spread = 0.0
    else:
        iv_spread = 0.0

    # Implications trading
    if iv_regime in ("EXTREME", "ELEVEE"):
        trade_impl = (
            f"IV rank {iv_rank:.0f}% — premium OPTIONS très cher. "
            "VENDRE du premium est statistiquement favorable. "
            "Éviter d'acheter des options nues (theta élevé). "
            "Préférer les spreads pour limiter le coût."
        )
        if term_structure == "BACKWARDATION":
            trade_impl += " Backwardation = stress imminent possible — straddle court terme."
        best_dte = "7-21 jours (vente premium, theta élevé)"
    elif iv_regime == "COMPRIMEE":
        trade_impl = (
            f"IV rank {iv_rank:.0f}% — premium OPTIONS bon marché. "
            "ACHETER du premium est statistiquement favorable. "
            "Les breakouts depuis la compression sont souvent violents. "
            "Straddle ou options directionnelles ATM recommandées."
        )
        best_dte = "21-45 jours (achat premium, gamma play)"
    else:
        trade_impl = f"IV rank {iv_rank:.0f}% — volatilité dans les normes. Stratégie neutre."
        best_dte = "14-30 jours"

    return VolReading(
        iv_rank=iv_rank,
        iv_regime=iv_regime,
        term_structure=term_structure,
        front_iv=front_iv,
        back_iv=back_iv,
        iv_spread=iv_spread,
        trade_implication=trade_impl,
        best_dte_range=best_dte,
    )


def _read_probability(pe: dict) -> ProbabilityReading:
    dominant_scenario = pe.get("dominant_scenario", "")
    dominant_prob     = pe.get("dominant_probability", 50) or 50
    verdict_24h       = pe.get("horizon_verdict_24h", "EQUILIBRE")
    verdict_72h       = pe.get("horizon_verdict_72h", "EQUILIBRE")

    bull_24h = pe.get("bull_24h", {}).get("probability", 50) or 50
    bear_24h = pe.get("bear_24h", {}).get("probability", 50) or 50
    bull_72h = pe.get("bull_72h", {}).get("probability", 50) or 50
    bear_72h = pe.get("bear_72h", {}).get("probability", 50) or 50

    # Confiance moyenne
    conf_24h = (
        (pe.get("bull_24h", {}).get("confidence", 50) or 50) +
        (pe.get("bear_24h", {}).get("confidence", 50) or 50)
    ) / 2

    # Direction dominante
    if "BULL" in dominant_scenario:
        dominant_direction = "BULL"
    elif "BEAR" in dominant_scenario:
        dominant_direction = "BEAR"
    else:
        dominant_direction = "NEUTRE"

    # Horizon dominant
    if "4H" in dominant_scenario:
        dominant_horizon = "4h"
    elif "24H" in dominant_scenario:
        dominant_horizon = "24h"
    else:
        dominant_horizon = "72h"

    # Edge quality
    edge = abs(dominant_prob - 50)  # 0 = pile/face, 45 = fort edge
    if edge >= 20 and conf_24h >= 70:
        edge_quality = "FORT"
    elif edge >= 12 and conf_24h >= 50:
        edge_quality = "MODERE"
    elif edge >= 6:
        edge_quality = "FAIBLE"
    else:
        edge_quality = "INEXISTANT"

    # Résumé
    summary = (
        f"Dominant : {dominant_direction} {dominant_horizon} à {dominant_prob:.0f}%. "
        f"24h : Bull {bull_24h:.0f}% vs Bear {bear_24h:.0f}%. "
        f"72h : Bull {bull_72h:.0f}% vs Bear {bear_72h:.0f}%. "
        f"Edge : {edge_quality}."
    )

    return ProbabilityReading(
        dominant_direction=dominant_direction,
        dominant_horizon=dominant_horizon,
        dominant_prob=dominant_prob,
        verdict_24h=verdict_24h,
        verdict_72h=verdict_72h,
        confidence_rules=conf_24h,
        edge_quality=edge_quality,
        summary=summary,
    )


def _build_key_levels(snap: dict, narrative: dict, walls: dict, gravity: dict) -> KeyLevels:
    db     = snap.get("dashboard", {})
    spot   = snap.get("spot", db.get("btc_price", 0))

    flip   = db.get("flip_level")
    mp_near = db.get("max_pain_near", {})
    max_pain = mp_near.get("strike") if isinstance(mp_near, dict) else None

    major_call = walls.get("major_call_wall")
    major_put  = walls.get("major_put_wall")
    magnet     = gravity.get("strongest_magnet")

    upside   = narrative.get("upside_ladder", [])
    downside = narrative.get("downside_ladder", [])

    # Résistance et support les plus proches
    nearest_res = None
    nearest_sup = None
    if upside:
        nearest_res = upside[0].get("price") if upside else None
    if downside:
        nearest_sup = downside[0].get("price") if downside else None

    return KeyLevels(
        spot=spot,
        flip_level=flip,
        max_pain=max_pain,
        call_wall=major_call,
        put_wall=major_put,
        gravity_magnet=magnet,
        nearest_resistance=nearest_res,
        nearest_support=nearest_sup,
        upside_ladder=upside,
        downside_ladder=downside,
    )


def _compute_conviction(regime: RegimeReading, dealer: DealerReading,
                        prob: ProbabilityReading, narrative: dict,
                        directional_bias: dict) -> int:
    """
    Score de conviction 0-10 basé sur la confluence des forces.
    Un pro n'entre que quand plusieurs moteurs pointent dans le même sens.
    """
    score = 0

    # 1. Edge probabiliste
    edge_pts = {"FORT": 3, "MODERE": 2, "FAIBLE": 1, "INEXISTANT": 0}
    score += edge_pts.get(prob.edge_quality, 0)

    # 2. Dealer actionnable
    if dealer.is_actionable:
        score += 2
    elif dealer.net_delta_btc != 0:
        score += 1

    # 3. GEX regime clair et actionnable
    if regime.regime in ("AMPLIFICATEUR", "STABILISANT") and regime.flip_is_actionable:
        score += 2
    elif regime.regime in ("AMPLIFICATEUR", "STABILISANT"):
        score += 1

    # 4. Directional bias confluence
    bias_score = abs(directional_bias.get("score", 0))
    if bias_score >= 70:
        score += 2
    elif bias_score >= 40:
        score += 1

    # 5. Contradictions = malus
    contradictions = narrative.get("contradictions", [])
    score -= len(contradictions)

    # 6. Zone de flip = incertitude = malus
    if regime.regime == "ZONE_DE_FLIP":
        score -= 2

    return max(0, min(10, score))


def _suggest_trade(
    verdict: str,
    regime: RegimeReading,
    vol: VolReading,
    levels: KeyLevels,
    prob: ProbabilityReading,
    conviction: int,
    snap: dict,
    max_dte: Optional[int] = None,  # P4 — DTE max du contrat dominant (cap trade DTE)
) -> TradeStructure:
    """
    Suggère la structure de trade optimale selon les conditions de marché.
    Un pro adapte la structure à l'IV et au régime, pas seulement à la direction.
    P4 — max_dte : si le signal est piloté par un contrat J-N, ne pas recommander
    une expiry au-delà de J+2 après l'expiration du contrat dominant.
    """
    spot = levels.spot
    iv_rank = vol.iv_rank
    iv_regime = vol.iv_regime
    db = snap.get("dashboard", {})

    # Si conviction trop faible → attendre
    if conviction <= 2 or verdict == "ATTENDRE":
        return TradeStructure(
            type="WAIT",
            action="Aucun trade — attendre confirmation",
            rationale=(
                "Conviction insuffisante. "
                "Les signaux sont contradictoires ou manquent de confluence. "
                "Un pro attend un setup de qualité, pas n'importe quel trade."
            ),
            expiry_suggested="—",
            dte_suggested=0,
            sizing_pct=0.0,
        )

    # Choisir expiry recommandée selon DTE optimal et vol structure
    # Front expiry avec OI significatif proche de la zone d'action
    if "7-21" in vol.best_dte_range:
        dte_target = 14
    elif "21-45" in vol.best_dte_range:
        dte_target = 30
    else:
        dte_target = 21

    # P4 — Si signal piloté par un contrat pré-expiration, cap DTE à max_dte+2
    # (ne jamais recommander une thèse 21 jours sur un signal J-1)
    if max_dte is not None and max_dte <= 3:
        dte_target = min(dte_target, max(max_dte + 2, 3))

    # Sizing selon conviction (% du capital)
    sizing = {
        10: 5.0, 9: 4.0, 8: 3.5, 7: 3.0, 6: 2.5,
        5: 2.0, 4: 1.5, 3: 1.0, 2: 0.5, 1: 0.25, 0: 0.0
    }
    sizing_pct = sizing.get(conviction, 1.0)

    if verdict == "ZONE_DE_FLIP_BINAIRE":
        # Cas spécial : zone de flip → straddle ou strangle
        strike_atm = round(spot / 1000) * 1000
        if iv_regime in ("COMPRIMEE", "NORMALE"):
            return TradeStructure(
                type="STRADDLE",
                action=f"Acheter STRADDLE ATM ${strike_atm:,.0f} ~{dte_target}j",
                rationale=(
                    f"Zone de flip GEX ${levels.flip_level:,.0f} — mouvement violent probable dans un sens ou l'autre. "
                    f"IV rank {iv_rank:.0f}% acceptable pour acheter un straddle. "
                    "Le straddle capte le move quelle que soit la direction."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=strike_atm,
                max_iv_acceptable=55.0,
                sizing_pct=sizing_pct * 0.7,  # réduction car 2 legs
                stop_price=spot * 0.985,  # stop si range serré = theta killer
                target_price=levels.nearest_resistance or spot * 1.03,
            )
        else:
            # IV trop chère → strangle OTM pour moins payer
            wing_pct = 0.03
            call_strike = round(spot * (1 + wing_pct) / 500) * 500
            put_strike  = round(spot * (1 - wing_pct) / 500) * 500
            return TradeStructure(
                type="STRANGLE",
                action=f"Acheter STRANGLE PUT ${put_strike:,.0f} / CALL ${call_strike:,.0f} ~{dte_target}j",
                rationale=(
                    f"Zone flip binaire mais IV rank {iv_rank:.0f}% élevée → strangle OTM ±3% moins coûteux. "
                    "Nécessite un move plus fort pour être profitable mais coût initial réduit."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=put_strike,
                strike_secondary=call_strike,
                max_iv_acceptable=70.0,
                sizing_pct=sizing_pct * 0.6,
            )

    elif verdict in ("BEAR_FORT", "BEAR_MODERE"):
        stop = levels.nearest_resistance or (spot * 1.015)
        target = levels.nearest_support or levels.max_pain or (spot * 0.97)


        if regime.regime == "STABILISANT" and iv_regime in ("NORMALE", "COMPRIMEE"):
            # STABILISANT = dealers amortissent les moves baissiers → PUT nu sous-optimal
            # Bear call spread sur resistance : collecte theta, profite du pin
            call_short = levels.nearest_resistance or round(spot * 1.01 / 500) * 500
            call_long  = round((call_short or spot) * 1.025 / 500) * 500
            return TradeStructure(
                type="CALL_SPREAD",
                action=(
                    f"Bear Call Spread : Vendre CALL ${call_short:,.0f} / Acheter CALL ${call_long:,.0f} ~{dte_target}j"
                ),
                rationale=(
                    f"Biais baissier mais GEX STABILISANT : les dealers amortissent les moves dans les deux sens. "
                    f"Acheter un PUT nu est sous-optimal (move lent, theta défavorable). "
                    f"Bear call spread sur resistance ${call_short:,.0f} : collecte premium + profite de l'effet pin. "
                    f"Profit max si BTC reste sous ${call_short:,.0f}."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=call_short,
                strike_secondary=call_long,
                sizing_pct=sizing_pct,
                stop_price=None,   # credit spread = risque défini par la structure, pas un stop
                target_price=target,
                risk_reward=None,  # dépend du premium collecté (non disponible sans prix live)
            )

        if iv_regime in ("ELEVEE", "EXTREME"):
            # IV chère → vendre des calls ou bear spread plutôt qu'acheter des puts
            call_short = levels.nearest_resistance or round(spot * 1.01 / 500) * 500
            call_long  = round((call_short or spot) * 1.03 / 500) * 500
            spread_width = (call_long - call_short) if (call_long and call_short) else 0
            return TradeStructure(
                type="CALL_SPREAD",
                action=(
                    f"Bear Call Spread : Vendre CALL ${call_short:,.0f} / Acheter CALL ${call_long:,.0f} ~{dte_target}j"
                ),
                rationale=(
                    f"Biais baissier clair mais IV rank {iv_rank:.0f}% — acheter des puts coûte trop cher. "
                    f"Bear call spread : collecte premium sur résistance ${call_short:,.0f}. "
                    f"Risque max = largeur spread (${spread_width:,.0f}), profit = premium collecté. "
                    f"Profit max si BTC reste sous ${call_short:,.0f} à expiration."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=call_short,
                strike_secondary=call_long,
                sizing_pct=sizing_pct,
                stop_price=None,   # credit spread = risque défini par la structure, pas un stop
                target_price=target,
                risk_reward=None,  # dépend du premium collecté (non disponible sans prix live)
            )
        else:
            # IV normale → acheter des puts directement
            put_strike = levels.nearest_support or round(spot * 0.985 / 500) * 500
            return TradeStructure(
                type="PUT",
                action=f"Acheter PUT ${put_strike:,.0f} ~{dte_target}j",
                rationale=(
                    f"Biais baissier avec IV rank {iv_rank:.0f}% encore abordable. "
                    f"Put ${put_strike:,.0f} capture le move vers target ${target:,.0f}. "
                    f"Stop : close au-dessus de ${stop:,.0f}."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=put_strike,
                max_iv_acceptable=50.0,
                sizing_pct=sizing_pct,
                stop_price=stop,
                target_price=target,
                risk_reward=round((spot - target) / (spot * 0.02), 1),
            )

    elif verdict in ("BULL_FORT", "BULL_MODERE"):
        stop = levels.nearest_support or (spot * 0.985)
        target = levels.nearest_resistance or levels.call_wall or (spot * 1.03)


        if regime.regime == "STABILISANT" and iv_regime in ("NORMALE", "COMPRIMEE"):
            # STABILISANT = dealers amortissent les moves haussiers → CALL nu sous-optimal
            # Bull put spread sur support : collecte theta, profite du pin
            put_short = levels.nearest_support or round(spot * 0.99 / 500) * 500
            put_long  = round((put_short or spot) * 0.975 / 500) * 500
            return TradeStructure(
                type="PUT_SPREAD",
                action=(
                    f"Bull Put Spread : Vendre PUT ${put_short:,.0f} / Acheter PUT ${put_long:,.0f} ~{dte_target}j"
                ),
                rationale=(
                    f"Biais haussier mais GEX STABILISANT : les dealers amortissent les moves dans les deux sens. "
                    f"Acheter un CALL nu est sous-optimal (move lent, theta défavorable). "
                    f"Bull put spread sur support ${put_short:,.0f} : collecte premium + profite de l'effet pin. "
                    f"Profit max si BTC reste au-dessus de ${put_short:,.0f}."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=put_short,
                strike_secondary=put_long,
                sizing_pct=sizing_pct,
                stop_price=None,   # credit spread = risque défini par la structure
                target_price=target,
                risk_reward=None,  # dépend du premium collecté (non disponible sans prix live)
            )

        if iv_regime in ("ELEVEE", "EXTREME"):
            # IV chère → bull put spread
            put_short = levels.nearest_support or round(spot * 0.985 / 500) * 500
            put_long  = round((put_short or spot) * 0.97 / 500) * 500
            spread_width = (put_short - put_long) if (put_short and put_long) else 0
            return TradeStructure(
                type="PUT_SPREAD",
                action=(
                    f"Bull Put Spread : Vendre PUT ${put_short:,.0f} / Acheter PUT ${put_long:,.0f} ~{dte_target}j"
                ),
                rationale=(
                    f"Biais haussier mais IV rank {iv_rank:.0f}% — vente de puts sur support mécanique. "
                    f"Risque max = largeur spread (${spread_width:,.0f}), profit = premium collecté. "
                    f"Profit max si BTC reste au-dessus de ${put_short:,.0f} à expiration."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=put_short,
                strike_secondary=put_long,
                sizing_pct=sizing_pct,
                stop_price=None,   # credit spread = risque défini par la structure
                target_price=target,
                risk_reward=None,  # dépend du premium collecté (non disponible sans prix live)
            )
        else:
            call_strike = round(spot / 500) * 500
            return TradeStructure(
                type="CALL",
                action=f"Acheter CALL ${call_strike:,.0f} ~{dte_target}j",
                rationale=(
                    f"Biais haussier avec IV rank {iv_rank:.0f}% encore abordable. "
                    f"Call ATM ${call_strike:,.0f} capture le move vers target ${target:,.0f}. "
                    f"Stop : close en-dessous de ${stop:,.0f}."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=call_strike,
                max_iv_acceptable=50.0,
                sizing_pct=sizing_pct,
                stop_price=stop,
                target_price=target,
                risk_reward=round((target - spot) / (spot * 0.015), 1),
            )

    else:  # RANGE / NEUTRE
        # Vendre du premium si IV élevée, sinon attendre
        if iv_regime in ("ELEVEE", "EXTREME") and conviction >= 5:
            call_short = levels.nearest_resistance or round(spot * 1.02 / 500) * 500
            put_short  = levels.nearest_support    or round(spot * 0.98 / 500) * 500
            return TradeStructure(
                type="STRANGLE",
                action=f"Vendre STRANGLE PUT ${put_short:,.0f} / CALL ${call_short:,.0f} ~{dte_target}j",
                rationale=(
                    f"Marché en range avec IV rank {iv_rank:.0f}% élevée — vente de premium optimal. "
                    f"Strangle court : collect theta, profit si BTC reste entre ${put_short:,.0f} et ${call_short:,.0f}. "
                    "Risque : sortie violente du range (couvrir si squeeze score monte > 60)."
                ),
                expiry_suggested=f"~{dte_target}j",
                dte_suggested=dte_target,
                strike_primary=put_short,
                strike_secondary=call_short,
                sizing_pct=sizing_pct * 0.5,  # demi-taille car risque illimité
                stop_price=call_short or spot * 1.025,
                target_price=spot,
            )
        else:
            return TradeStructure(
                type="WAIT",
                action="Attendre — range sans edge premium",
                rationale=(
                    "Marché sans direction claire et IV insuffisante pour vendre du premium. "
                    "Un pro ne trade pas pour trader — il attend un setup avec edge défini."
                ),
                expiry_suggested="—",
                dte_suggested=0,
                sizing_pct=0.0,
            )


def _build_warnings(regime: RegimeReading, vol: VolReading,
                    squeeze: dict, narrative: dict, conviction: int) -> list:
    warnings = []
    sq_score = squeeze.get("score", 0) or 0
    risk_matrix = narrative.get("risk_matrix", {})

    if regime.regime == "ZONE_DE_FLIP":
        warnings.append(RiskWarning(
            level="CRITIQUE",
            category="REGIME",
            message=f"Zone de Flip GEX ${regime.flip_level:,.0f} — attendre un close de confirmation avant d'entrer directionnel.",
        ))

    if vol.iv_rank >= 85:
        warnings.append(RiskWarning(
            level="ELEVE",
            category="VOL",
            message=f"IV rank {vol.iv_rank:.0f}% — acheter du premium est dangereux. Préférer la vente ou les spreads.",
        ))

    if vol.term_structure == "BACKWARDATION":
        warnings.append(RiskWarning(
            level="ELEVE",
            category="VOL",
            message="Backwardation : IV front > back — stress de court terme détecté. Risque d'événement violent imminent.",
        ))

    if sq_score >= 60:
        warnings.append(RiskWarning(
            level="ELEVE",
            category="TIMING",
            message=f"Squeeze Score {sq_score:.0f}/100 — breakout violent possible. Éviter les shorts de volatilité.",
        ))

    rupture = risk_matrix.get("rupture_regime", {})
    if rupture.get("niveau") == "actif":
        warnings.append(RiskWarning(
            level="ELEVE",
            category="REGIME",
            message=f"Risque de rupture de régime actif : {rupture.get('detail', '')}",
        ))

    faux_bo = risk_matrix.get("faux_breakout", {})
    if faux_bo.get("niveau") in ("modéré", "élevé"):
        warnings.append(RiskWarning(
            level="MODERE",
            category="TIMING",
            message=f"Risque de faux breakout {faux_bo.get('niveau', '')} : {faux_bo.get('detail', '')}",
        ))

    if conviction <= 3:
        warnings.append(RiskWarning(
            level="MODERE",
            category="CONTRARIAN",
            message="Conviction faible — réduire la taille ou abstenir.",
        ))

    contradictions = narrative.get("contradictions", [])
    for c in contradictions[:2]:
        warnings.append(RiskWarning(
            level="MODERE",
            category="CONTRARIAN",
            message=f"Signal contradictoire ({c.get('widget', '')}): {c.get('detail', '')}",
        ))

    return warnings


def compute_pro_decision(
    snap: dict,
    narrative: dict,
    vol_structure_data: list,
    pe: dict,
    squeeze: dict,
    directional_bias: dict,
    gravity: dict,
    walls: dict,
    arbiter_confidence_pct: Optional[int] = None,  # P2 — cap conviction
    pre_expiration_warning: Optional[str] = None,  # P4 — alerte TTL pré-expiration
    dominant_signal_dte: Optional[int] = None,     # P4 — DTE du contrat dominant (cap trade DTE)
) -> ProDecision:
    """
    Point d'entrée principal du moteur de décision pro.
    """
    import time

    db   = snap.get("dashboard", {})
    spot = snap.get("spot", db.get("btc_price", 0))

    # ── Lectures individuelles ──────────────────────────────────────────────
    regime  = _read_regime(snap, narrative)
    dealer  = _read_dealer(snap, narrative)
    vol     = _read_vol(vol_structure_data, db)
    prob    = _read_probability(pe)
    levels  = _build_key_levels(snap, narrative, walls, gravity)

    # ── Conviction ──────────────────────────────────────────────────────────
    conviction = _compute_conviction(regime, dealer, prob, narrative, directional_bias)

    # P2 — Cap conviction par la confiance Arbiter (source de vérité souveraine)
    # Arbiter 15% → conviction max = floor(15/10) = 1 → jamais 7/10 avec 15% Arbiter
    if arbiter_confidence_pct is not None:
        conviction_cap = arbiter_confidence_pct // 10
        conviction = min(conviction, conviction_cap)

    # ── Déterminer verdict ──────────────────────────────────────────────────
    # Logique de synthèse : régime + dealer + probabilité
    bias_score   = directional_bias.get("score", 0) or 0
    asym_side    = narrative.get("asymmetric_side", "NEUTRAL")
    verdict_24h  = prob.verdict_24h
    regime_name  = regime.regime

    # ── Phase 4 — Machine à états SUPPRESSED ──────────────────────────────
    # L'Arbiter dit confiance < 30% → état SUPPRESSED.
    # Aucun verdict directionnel, aucun résidu stop/target/cible.
    # Seule lecture factuelle des signaux.
    _arb_suppressed = (
        arbiter_confidence_pct is not None and arbiter_confidence_pct < 30
    )

    # Labels conviction (needed in both paths)
    conv_labels = {
        10: "Maximale", 9: "Très forte", 8: "Forte",
        7: "Forte", 6: "Bonne", 5: "Modérée",
        4: "Modérée", 3: "Faible", 2: "Faible",
        1: "Très faible", 0: "Inexistante",
    }

    if _arb_suppressed:
        verdict       = "ATTENDRE"
        verdict_emoji = "⊘"
        action_label  = "Structure détectée — non actionnable"
        action_color  = "gray"

        # Thèse factuelle sans conclusion directionnelle
        _sig_parts = []
        if abs(bias_score) >= 40:
            _sig_parts.append(_bias_label(bias_score).lower())
        if dealer.is_actionable:
            _dex_lbl = "haussier" if dealer.direction == "BULLISH_FLOWS" else "baissier"
            _sig_parts.append(f"DEX {_dex_lbl}")
        _sig_parts.append(f"régime {regime_name.lower()}")
        _arb_pct_label = arbiter_confidence_pct if arbiter_confidence_pct is not None else "?"
        _sig_parts.append(f"Arbiter {_arb_pct_label}% (seuil 30%)")
        thesis = "Structure détectée : " + ", ".join(_sig_parts) + ". Pas d'edge actionnable."

        # Signaux en présence (liste neutre — pas de classification favorable/adverse)
        _neutral_signals = []
        if abs(bias_score) >= 40:
            _dir = "BEAR" if bias_score < 0 else "BULL"
            _neutral_signals.append(_bias_label(bias_score))
        if dealer.is_actionable:
            _neutral_signals.append(f"DEX {dealer.direction} ({dealer.intensity})")
        if prob.edge_quality in ("FORT", "MODERE"):
            _neutral_signals.append(
                f"Score règles {prob.dominant_prob:.0f}/100 {prob.dominant_direction} {prob.dominant_horizon}"
            )
        _neutral_signals.append(f"Régime GEX : {regime_name}")
        supporting_forces = _neutral_signals
        opposing_forces   = []

        # Trade WAIT — zéro résidu de cible/stop/sizing
        trade_suppressed = TradeStructure(
            type="WAIT",
            action="Aucun trade — attendre confirmation",
            rationale=f"Arbiter {_arb_pct_label}% (seuil 30%). Pas de setup actionnable.",
            expiry_suggested="—",
            dte_suggested=0,
        )

        key_risk_suppressed = (
            "Conditions insuffisantes pour un trade de qualité — attendre convergence des signaux."
        )

        warnings_suppressed = _build_warnings(regime, vol, squeeze, narrative, conviction)
        if pre_expiration_warning:
            warnings_suppressed.insert(0, RiskWarning(
                level="ELEVE", category="PRE_EXPIRATION", message=pre_expiration_warning,
            ))

        return ProDecision(
            verdict=verdict,
            verdict_emoji=verdict_emoji,
            conviction=conviction,
            conviction_label=conv_labels.get(conviction, f"Niveau {conviction}"),
            action_label=action_label,
            action_color=action_color,
            primary_thesis=thesis,
            supporting_forces=supporting_forces,
            opposing_forces=opposing_forces,
            key_risk=key_risk_suppressed,
            levels=levels,
            regime=regime,
            dealer=dealer,
            vol=vol,
            probability=prob,
            trade=trade_suppressed,
            warnings=[asdict(w) for w in warnings_suppressed],
            scenario_type="NEUTRE",
            invalidation_price=None,
            invalidation_reason="",
            timestamp=time.time(),
            spot=spot,
            arbiter_confidence_pct=arbiter_confidence_pct,
            global_confidence=arbiter_confidence_pct,
            pre_expiration_warning=pre_expiration_warning,
            dominant_signal_dte=dominant_signal_dte,
        )
    # ── Fin état SUPPRESSED ─────────────────────────────────────────────────

    # Zone de flip = verdict spécial (priorité maximale si vraiment sur la ligne)
    if regime_name == "ZONE_DE_FLIP" and abs(regime.flip_proximity_pct) < 1.0:
        verdict = "ZONE_DE_FLIP_BINAIRE"
        verdict_emoji = "⚖️"
        action_label = "ATTENDRE CONFIRMATION"
        action_color = "yellow"
        thesis = (
            f"BTC SUR la zone de bascule GEX ${regime.flip_level:,.0f} (à {abs(regime.flip_proximity_pct):.1f}%). "
            "La mécanique options est binaire : close au-dessus = régime stabilisant, en-dessous = amplificateur. "
            "Aucun biais directionnel exploitable sans confirmation de close."
        )
    elif bias_score <= -70:
        # Biais très fort → BEAR même sans confluence dealer parfaite
        is_full_confluence = dealer.direction == "BEARISH_FLOWS"
        verdict = "BEAR_FORT" if is_full_confluence else "BEAR_MODERE"
        verdict_emoji = "🔴" if is_full_confluence else "🟠"
        action_label = "ENTRER BAISSIER" if is_full_confluence else "BIAIS BAISSIER — TAILLE RÉDUITE"
        action_color = "red" if is_full_confluence else "orange"
        dealer_note = "" if is_full_confluence else f" (ATTENTION : DEX {dealer.direction} diverge — taille réduite)"
        thesis = (
            f"{_bias_label(bias_score)} — structure baissière dominante.{dealer_note} "
            + (f"Cible mécanique : ${levels.nearest_support:,.0f}. Stop : ${levels.nearest_resistance:,.0f}."
               if levels.nearest_support and levels.nearest_resistance else "")
        )
    elif bias_score <= -40 and (dealer.direction == "BEARISH_FLOWS" or verdict_24h == "BIAIS_BAISSIER"):
        verdict = "BEAR_MODERE"
        verdict_emoji = "🟠"
        action_label = "BIAIS BAISSIER — TAILLE RÉDUITE"
        action_color = "orange"
        thesis = (
            f"{_bias_label(bias_score)}. "
            "Probabilités court terme légèrement baissières. "
            "Confluence incomplète — taille réduite."
        )
    elif bias_score >= 70:
        # Biais très fort → BULL même sans confluence dealer parfaite
        is_full_confluence = dealer.direction == "BULLISH_FLOWS"
        verdict = "BULL_FORT" if is_full_confluence else "BULL_MODERE"
        verdict_emoji = "🟢" if is_full_confluence else "🟡"
        action_label = "ENTRER HAUSSIER" if is_full_confluence else "BIAIS HAUSSIER — TAILLE RÉDUITE"
        action_color = "green" if is_full_confluence else "yellow"
        dealer_note = "" if is_full_confluence else f" (ATTENTION : DEX {dealer.direction} diverge — taille réduite)"
        thesis = (
            f"{_bias_label(bias_score)} — structure haussière dominante.{dealer_note} "
            + (f"Cible : ${levels.nearest_resistance:,.0f}. Stop : ${levels.nearest_support:,.0f}."
               if levels.nearest_resistance and levels.nearest_support else "")
        )
    elif bias_score >= 40 and (dealer.direction == "BULLISH_FLOWS" or verdict_24h == "BIAIS_HAUSSIER"):
        verdict = "BULL_MODERE"
        verdict_emoji = "🟡"
        action_label = "BIAIS HAUSSIER — TAILLE RÉDUITE"
        action_color = "yellow"
        thesis = (
            f"{_bias_label(bias_score)}. "
            "Confluence incomplète — taille réduite recommandée."
        )
    elif conviction <= 2:
        verdict = "ATTENDRE"
        verdict_emoji = "⏸️"
        action_label = "PAS DE TRADE"
        action_color = "gray"
        thesis = "Aucune confluence suffisante. Le marché ne présente pas de setup avec edge défini."
    elif vol.iv_rank >= 65 and abs(bias_score) < 40:
        verdict = "NEUTRE_RANGE"
        verdict_emoji = "↔️"
        action_label = "VENTE PREMIUM — RANGE"
        action_color = "blue"
        thesis = (
            f"Marché sans direction claire ({_bias_label(bias_score)}) mais IV rank {vol.iv_rank:.0f}% élevée. "
            "Opportunité de vente de premium sur les extrêmes de range."
        )
    else:
        verdict = "ATTENDRE"
        verdict_emoji = "⏸️"
        action_label = "PAS DE TRADE CLAIR"
        action_color = "gray"
        thesis = (
            f"Signaux mixtes ({_bias_label(bias_score)}, IV {vol.iv_rank:.0f}%). "
            "Pas de setup avec edge défini — un pro attend la convergence des signaux."
        )

    # ── Forces pour/contre ──────────────────────────────────────────────────
    supporting = []
    opposing   = []

    # GEX
    # MECANIQUE : STABILISANT = dealers LONGS gamma → vendent hausse ET achetent baisse
    # → amortissent les moves dans LES DEUX SENS → toujours adverse pour premium directionnel
    if regime_name == "AMPLIFICATEUR":
        supporting.append(f"GEX AMPLIFICATEUR ({regime.gex_total_b:.1f}B$) — dealers COURTS gamma, moves amplifiés")
    elif regime_name == "STABILISANT":
        opposing.append(
            f"GEX STABILISANT — dealers LONGS gamma : vendent la hausse ET achètent la baisse. "
            f"Les moves directionnels sont amortis dans les deux sens — premium directionnel nu défavorisé"
        )
    elif regime_name == "ZONE_DE_FLIP":
        opposing.append(f"Zone de Flip GEX ${regime.flip_level:,.0f} — régime mécanique incertain")

    # DEX
    if dealer.is_actionable:
        if (dealer.direction == "BEARISH_FLOWS" and "BEAR" in verdict) or \
           (dealer.direction == "BULLISH_FLOWS" and "BULL" in verdict):
            supporting.append(f"DEX {dealer.direction} actionnable ({abs(dealer.actionable_btc):,.0f} BTC) — dealers en renfort")
        else:
            opposing.append(f"DEX {dealer.direction} actionnable — dealers en opposition au biais")

    # Vol
    if vol.iv_rank >= 75:
        if "PUT" in (verdict or "") or "CALL" in (verdict or ""):
            opposing.append(f"IV rank {vol.iv_rank:.0f}% élevée — premium cher, theta défavorable")
        else:
            supporting.append(f"IV rank {vol.iv_rank:.0f}% — favorable à la vente de premium")

    # Probabilités
    if prob.edge_quality in ("FORT", "MODERE"):
        supporting.append(f"Edge probabiliste {prob.edge_quality} ({prob.dominant_prob:.0f}% {prob.dominant_direction} {prob.dominant_horizon})")
    else:
        opposing.append(f"Edge probabiliste {prob.edge_quality} — signal règles faible")

    # Gravity
    if levels.gravity_magnet and spot:
        dist = (levels.gravity_magnet - spot) / spot * 100
        if abs(dist) < 3:
            supporting.append(f"Gravity magnet ${levels.gravity_magnet:,.0f} ({dist:+.1f}%) — attraction forte")

    # Max Pain
    if levels.max_pain and spot:
        mp_dist = (levels.max_pain - spot) / spot * 100
        if abs(mp_dist) < 4:
            if mp_dist < 0 and "BEAR" in verdict:
                supporting.append(f"Max Pain ${levels.max_pain:,.0f} ({mp_dist:+.1f}%) — gravité expiration baissière")
            elif mp_dist > 0 and "BULL" in verdict:
                supporting.append(f"Max Pain ${levels.max_pain:,.0f} ({mp_dist:+.1f}%) — gravité expiration haussière")

    # ── Risque principal ────────────────────────────────────────────────────
    key_risk = narrative.get("risque_principal", "")
    if not key_risk:
        if regime_name == "ZONE_DE_FLIP":
            key_risk = f"Cassure violente dans un sens ou l'autre depuis la zone flip ${regime.flip_level:,.0f}"
        elif verdict in ("BEAR_FORT", "BEAR_MODERE"):
            key_risk = f"Retournement haussier si close au-dessus de ${levels.nearest_resistance:,.0f}" if levels.nearest_resistance else "Retournement haussier inattendu"
        elif verdict in ("BULL_FORT", "BULL_MODERE"):
            key_risk = f"Breakdown si close en-dessous de ${levels.nearest_support:,.0f}" if levels.nearest_support else "Breakdown baissier inattendu"
        else:
            key_risk = "Sortie violente du range (squeeze score à surveiller)"

    # conv_labels défini plus haut (avant état SUPPRESSED)

    # ── Trade suggéré ───────────────────────────────────────────────────────
    # P4 — Passer le DTE dominant pour que _suggest_trade ne recommande pas de DTE > expiration
    trade = _suggest_trade(verdict, regime, vol, levels, prob, conviction, snap,
                           max_dte=dominant_signal_dte)

    # ── Warnings ────────────────────────────────────────────────────────────
    warnings = _build_warnings(regime, vol, squeeze, narrative, conviction)

    # P4 — Ajouter warning pré-expiration si applicable
    if pre_expiration_warning:
        warnings.insert(0, RiskWarning(
            level="ELEVE",
            category="PRE_EXPIRATION",
            message=pre_expiration_warning,
        ))

    # ── Invalidation ────────────────────────────────────────────────────────
    inv_price = directional_bias.get("stop_logical") or narrative.get("invalidation")
    inv_reason = directional_bias.get("stop_label", "Niveau d'invalidation technique")

    # ── Scenario type ────────────────────────────────────────────────────────
    if "BINAIRE" in verdict or "FLIP" in verdict:
        scenario_type = "BINAIRE"
    elif vol.iv_rank >= 70 and abs(bias_score) < 40:
        scenario_type = "VOLATILITE"
    elif abs(bias_score) >= 50:
        scenario_type = "DIRECTIONNEL"
    else:
        scenario_type = "NEUTRE"

    return ProDecision(
        verdict=verdict,
        verdict_emoji=verdict_emoji,
        conviction=conviction,
        conviction_label=conv_labels.get(conviction, f"Niveau {conviction}"),
        action_label=action_label,
        action_color=action_color,
        primary_thesis=thesis,
        supporting_forces=supporting,
        opposing_forces=opposing,
        key_risk=key_risk,
        levels=levels,
        regime=regime,
        dealer=dealer,
        vol=vol,
        probability=prob,
        trade=trade,
        warnings=[asdict(w) for w in warnings],
        scenario_type=scenario_type,
        invalidation_price=inv_price,
        invalidation_reason=inv_reason,
        timestamp=time.time(),
        spot=spot,
        arbiter_confidence_pct=arbiter_confidence_pct,
        global_confidence=arbiter_confidence_pct,  # P3 — alias source de vérité unique
        pre_expiration_warning=pre_expiration_warning,  # P4
        dominant_signal_dte=dominant_signal_dte,        # P4
    )
