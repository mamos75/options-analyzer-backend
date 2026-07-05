"""
Options Gravity Map — Zones magnétiques, explosives et vides sur le chart BTC.

Modèle physique simplifié :
  - Zone magnétique : fort OI proche du spot → le prix est "attiré"
  - Zone explosive  : GEX très négatif + faible liquidité → amplification explosive
  - Zone résistance : Call wall dominant au-dessus du spot
  - Zone support    : Put wall dominant en dessous du spot
  - Zone vide       : Absence d'OI entre deux walls → voyage rapide du prix
"""

from dataclasses import dataclass, field
from typing import List
from .deribit_client import MarketSnapshot
from .gex import GEXProfile, compute_gex, filter_options_by_dte, DTE_NEAR_MAX, DTE_MONTHLY_MIN, DTE_MONTHLY_MAX
from .options_walls import compute_options_walls


@dataclass
class GravityZone:
    price_low: float          # borne inférieure de la zone
    price_high: float         # borne supérieure de la zone
    center: float             # centre de la zone (strike dominant)
    zone_type: str            # MAGNETIC | EXPLOSIVE | RESISTANCE | SUPPORT | VOID
    strength: float           # 0-100 : intensité de la zone
    label: str                # texte affiché sur le chart
    color: str                # couleur hex/rgba pour le frontend
    oi_usd: float             # OI en USD de la zone
    gex: float                # GEX net de la zone
    # Biais directionnel — uniquement renseigné pour les zones EXPLOSIVE
    explosive_bias: str = "NEUTRAL"       # DOWN_ONLY | UP_ONLY | SYMMETRIC | NEUTRAL
    explosive_score_down: float = 0.0     # 0-100 : prob. mécanique de cassure baissière accélérée
    explosive_score_up: float = 0.0       # 0-100 : prob. mécanique de reclaim haussier accéléré


@dataclass
class GravityMap:
    btc_price: float
    zones: List[GravityZone]
    strongest_magnet: float       # strike le plus magnétique
    next_explosive: float         # prochain level explosif si cassé
    gravity_score: float          # 0-100 : compression globale (100=max compression)
    narrative: str                # phrase courte pour le frontend
    timestamp: float
    asymmetric_risk: dict = None  # risque local asymétrique si fort niveau hors ±5%
    gravity_global_label: str = ""  # "Compression extrême" | "Tension modérée" | "Gravité faible"


_ZONE_COLORS = {
    "MAGNETIC":   "rgba(0,212,126,0.18)",
    "EXPLOSIVE":  "rgba(255,60,107,0.18)",
    "RESISTANCE": "rgba(255,60,107,0.10)",
    "SUPPORT":    "rgba(0,212,126,0.10)",
    "VOID":       "rgba(255,255,255,0.03)",
}


def _compute_explosive_bias(
    strike: float,
    spot: float,
    gex: float,
    call_oi: float,
    put_oi: float,
    max_abs_gex: float,
) -> tuple[str, float, float]:
    """Calcule le biais directionnel mécanique d'une zone EXPLOSIVE.

    Returns (explosive_bias, score_down, score_up) où les scores sont 0-100.
    score_down  = probabilité mécanique que la cassure vers le bas accélère.
    score_up    = probabilité mécanique qu'un reclaim / cassure vers le haut accélère.

    Logique :
      GEX très négatif + puts dominants → DOWN_ONLY
      GEX très négatif + calls dominants → UP_ONLY
      GEX négatif + OI mixte → SYMMETRIC
      GEX faible ou contradictions → NEUTRAL
    """
    total_oi = call_oi + put_oi
    if total_oi == 0 or max_abs_gex == 0:
        return "NEUTRAL", 50.0, 50.0

    put_ratio = put_oi / total_oi    # 0–1
    call_ratio = call_oi / total_oi  # 0–1

    # GEX normalisé -1..+1 (négatif = amplificateur / dealers en mode couverture)
    gex_norm = gex / max_abs_gex

    # Proximité spot (1 = ATM, 0 = très loin >10%)
    dist_pct = abs(strike - spot) / spot
    proximity = max(0.0, 1.0 - dist_pct / 0.10)

    # ── Score DOWN ─────────────────────────────────────────────────────────────
    # Mécanismes baissiers : GEX négatif (dealers vendent) + puts dominants (OI put → hedge)
    score_down = 50.0
    if gex_norm < 0:
        score_down += abs(gex_norm) * 30.0   # GEX très négatif → +30 max
    score_down += put_ratio * 25.0            # Puts dominants → +25 max
    score_down += proximity * 10.0            # Proximité → +10 max
    score_down = round(min(100.0, score_down), 1)

    # ── Score UP ──────────────────────────────────────────────────────────────
    # Mécanismes haussiers : calls dominants + GEX négatif peut aussi créer un gamma squeeze
    score_up = 50.0
    if gex_norm < 0:
        score_up += abs(gex_norm) * 15.0     # GEX négatif amplifie aussi le squeeze haussier
    score_up += call_ratio * 25.0             # Calls dominants → +25 max
    score_up += proximity * 10.0              # Proximité → +10 max
    score_up = round(min(100.0, score_up), 1)

    # ── Classifier le bias ────────────────────────────────────────────────────
    if abs(gex_norm) < 0.1:
        return "NEUTRAL", score_down, score_up

    puts_dominant = put_oi > call_oi * 1.3
    calls_dominant = call_oi > put_oi * 1.3
    gex_very_neg = gex_norm < -0.3

    if gex_very_neg and puts_dominant:
        return "DOWN_ONLY", score_down, score_up

    if gex_very_neg and calls_dominant:
        return "UP_ONLY", score_down, score_up

    if gex_norm < 0 and not puts_dominant and not calls_dominant:
        return "SYMMETRIC", score_down, score_up

    return "NEUTRAL", score_down, score_up


def compute_gravity_map(snapshot: MarketSnapshot, gex_profile: GEXProfile) -> GravityMap:
    spot = snapshot.btc_price
    walls_profile = compute_options_walls(snapshot)

    # --- Agréger OI et GEX par strike ---
    oi_by_strike: dict[float, dict] = {}
    for opt in snapshot.options:
        s = opt.strike
        if s not in oi_by_strike:
            oi_by_strike[s] = {"call_oi": 0.0, "put_oi": 0.0, "total_oi": 0.0, "gex": 0.0}
        oi_by_strike[s]["total_oi"] += opt.oi
        oi_by_strike[s]["gex"] += gex_profile.gex_by_strike.get(s, 0.0)
        if opt.option_type == "call":
            oi_by_strike[s]["call_oi"] += opt.oi
        else:
            oi_by_strike[s]["put_oi"] += opt.oi

    if not oi_by_strike:
        return GravityMap(
            btc_price=spot, zones=[], strongest_magnet=spot,
            next_explosive=spot, gravity_score=50.0,
            narrative="Données insuffisantes", timestamp=snapshot.timestamp,
        )

    strikes_sorted = sorted(oi_by_strike.keys())
    total_oi = sum(v["total_oi"] for v in oi_by_strike.values())
    max_oi = max(v["total_oi"] for v in oi_by_strike.values()) or 1.0
    max_abs_gex = max(abs(v["gex"]) for v in oi_by_strike.values()) or 1.0

    # --- Seuils de zone ---
    # Un strike est "wall" si son OI > 15% du total ou > 1.5σ de la moyenne
    mean_oi = total_oi / len(oi_by_strike)
    variance = sum((v["total_oi"] - mean_oi) ** 2 for v in oi_by_strike.values()) / len(oi_by_strike)
    sigma_oi = variance ** 0.5
    wall_threshold = max(mean_oi + 1.5 * sigma_oi, total_oi * 0.08)

    zones: List[GravityZone] = []

    # --- Construire les zones ---
    for i, strike in enumerate(strikes_sorted):
        data = oi_by_strike[strike]
        oi = data["total_oi"]
        gex = data["gex"]
        oi_usd = oi * spot

        # Largeur de la zone = demi-écart avec les voisins
        prev_gap = (strike - strikes_sorted[i - 1]) / 2 if i > 0 else 500
        next_gap = (strikes_sorted[i + 1] - strike) / 2 if i < len(strikes_sorted) - 1 else 500
        zone_low = strike - min(prev_gap, 1000)
        zone_high = strike + min(next_gap, 1000)

        strength_oi = oi / max_oi * 100
        strength_gex = abs(gex) / max_abs_gex * 100

        above_spot = strike > spot

        exp_bias, exp_score_down, exp_score_up = "NEUTRAL", 0.0, 0.0

        if oi >= wall_threshold:
            if abs(gex) > max_abs_gex * 0.3 and gex < 0:
                zone_type = "EXPLOSIVE"
                strength = (strength_oi * 0.4 + strength_gex * 0.6)
                label = f"⚡ Explosive ${strike:,.0f}"
                exp_bias, exp_score_down, exp_score_up = _compute_explosive_bias(
                    strike=strike,
                    spot=spot,
                    gex=gex,
                    call_oi=data["call_oi"],
                    put_oi=data["put_oi"],
                    max_abs_gex=max_abs_gex,
                )
            elif above_spot and data["call_oi"] > data["put_oi"] * 1.3:
                zone_type = "RESISTANCE"
                strength = strength_oi
                label = f"🔴 Résistance ${strike:,.0f}"
            elif not above_spot and data["put_oi"] > data["call_oi"] * 1.3:
                zone_type = "SUPPORT"
                strength = strength_oi
                label = f"🟢 Support ${strike:,.0f}"
            else:
                zone_type = "MAGNETIC"
                strength = (strength_oi * 0.6 + strength_gex * 0.4)
                label = f"🧲 Magnétique ${strike:,.0f}"
        elif oi < mean_oi * 0.3:
            zone_type = "VOID"
            strength = max(0, 30 - strength_oi)
            label = f"⬜ Vide ${strike:,.0f}"
        else:
            continue  # strike insignifiant, skip

        zones.append(GravityZone(
            price_low=zone_low,
            price_high=zone_high,
            center=strike,
            zone_type=zone_type,
            strength=round(strength, 1),
            label=label,
            color=_ZONE_COLORS[zone_type],
            oi_usd=round(oi_usd, 0),
            gex=round(gex, 0),
            explosive_bias=exp_bias,
            explosive_score_down=exp_score_down,
            explosive_score_up=exp_score_up,
        ))

    # --- Strongest magnet (nearest magnetic zone au spot) ---
    # 0.0 = aucune zone MAGNETIC valide (ne pas afficher spot comme aimant)
    magnets = [z for z in zones if z.zone_type == "MAGNETIC"]
    strongest_magnet = (
        min(magnets, key=lambda z: abs(z.center - spot)).center
        if magnets else 0.0
    )

    # --- Next explosive (prochaine zone explosive en direction momentum) ---
    # 0.0 = aucune zone EXPLOSIVE valide
    explosives = sorted([z for z in zones if z.zone_type == "EXPLOSIVE"], key=lambda z: abs(z.center - spot))
    next_explosive = explosives[0].center if explosives else 0.0

    # --- Gravity score (compression = prix entre walls proches) ---
    # Fenêtre élargie à 7% pour capturer les zones comme $70K (≈5.1% sous $73.7K)
    nearby_zones = [z for z in zones if abs(z.center - spot) / spot < 0.07]
    gravity_score = min(100, sum(z.strength for z in nearby_zones) / max(len(nearby_zones), 1))

    # --- Asymmetric risk — zones fortes hors fenêtre ±5% mais dans ±12% ---
    # Détecte les dangers locaux qui ne font pas monter le score global
    asymmetric_risk = _detect_asymmetric_risk(spot, zones)

    # --- Global label (AVANT narrative pour éviter "gravité faible" si risque local élevé) ---
    gravity_global_label = _classify_compression(gravity_score)

    # --- Narrative ---
    narrative = _build_narrative(spot, zones, strongest_magnet, next_explosive, gravity_score, asymmetric_risk)

    return GravityMap(
        btc_price=spot,
        zones=zones,
        strongest_magnet=strongest_magnet,
        next_explosive=next_explosive,
        gravity_score=round(gravity_score, 1),
        narrative=narrative,
        timestamp=snapshot.timestamp,
        asymmetric_risk=asymmetric_risk,
        gravity_global_label=gravity_global_label,
    )


def _detect_asymmetric_risk(spot: float, zones: List[GravityZone]) -> dict:
    """Détecte un risque local asymétrique : zone forte dans ±12% mais hors ±5%.
    Un score global "faible" peut cacher un danger local concentré."""
    danger_types = {"EXPLOSIVE", "SUPPORT", "RESISTANCE"}
    # Chercher zones dans 5-12% du spot avec force > 55%
    candidates = [
        z for z in zones
        if z.zone_type in danger_types
        and 0.05 < abs(z.center - spot) / spot <= 0.12
        and z.strength > 55
    ]
    # Également inclure les zones très fortes (>70%) dans ±5% qui sont explosives
    local_strong = [
        z for z in zones
        if z.zone_type == "EXPLOSIVE"
        and abs(z.center - spot) / spot <= 0.05
        and z.strength > 70
    ]
    candidates.extend(local_strong)

    if not candidates:
        return None

    # Séparer haut/bas du spot
    below = [z for z in candidates if z.center < spot]
    above = [z for z in candidates if z.center > spot]

    if below and not above:
        strongest = max(below, key=lambda z: z.strength)
        dist_pct = (spot - strongest.center) / spot * 100
        return {
            "side": "DOWN",
            "level": strongest.center,
            "strength": round(strongest.strength, 1),
            "dist_pct": round(dist_pct, 1),
            "zone_type": strongest.zone_type,
            "label": f"Risque local élevé sous ${strongest.center:,.0f} ({dist_pct:.1f}% du spot)",
        }
    if above and not below:
        strongest = max(above, key=lambda z: z.strength)
        dist_pct = (strongest.center - spot) / spot * 100
        return {
            "side": "UP",
            "level": strongest.center,
            "strength": round(strongest.strength, 1),
            "dist_pct": round(dist_pct, 1),
            "zone_type": strongest.zone_type,
            "label": f"Risque local élevé au-dessus de ${strongest.center:,.0f} ({dist_pct:.1f}% du spot)",
        }
    if below and above:
        # Asymétrie double — retourner le côté le plus fort
        str_b = max(z.strength for z in below)
        str_a = max(z.strength for z in above)
        if str_b >= str_a:
            strongest = max(below, key=lambda z: z.strength)
            dist_pct = (spot - strongest.center) / spot * 100
            return {
                "side": "DOWN",
                "level": strongest.center,
                "strength": round(strongest.strength, 1),
                "dist_pct": round(dist_pct, 1),
                "zone_type": strongest.zone_type,
                "label": f"Risque asymétrique baissier sous ${strongest.center:,.0f}",
            }
        strongest = max(above, key=lambda z: z.strength)
        dist_pct = (strongest.center - spot) / spot * 100
        return {
            "side": "UP",
            "level": strongest.center,
            "strength": round(strongest.strength, 1),
            "dist_pct": round(dist_pct, 1),
            "zone_type": strongest.zone_type,
            "label": f"Risque asymétrique haussier au-dessus de ${strongest.center:,.0f}",
        }
    return None


def _validate_gravity_data(
    spot: float, magnet: float, explosive: float
) -> tuple[bool, bool]:
    """Valide les niveaux avant toute génération de narrative.
    Un niveau égal ou trop proche du spot est considéré invalide."""
    TOLERANCE = spot * 0.005  # 0.5%
    magnet_valid   = magnet   > 0 and abs(magnet   - spot) > TOLERANCE
    explosive_valid = explosive > 0 and abs(explosive - spot) > TOLERANCE
    return magnet_valid, explosive_valid


def _classify_compression(gravity_score: float) -> str:
    if gravity_score > 70:
        return "Compression extrême"
    elif gravity_score > 40:
        return "Tension modérée"
    return "Gravité faible"


def _get_explosive_zone(zones: List[GravityZone], center: float) -> "GravityZone | None":
    for z in zones:
        if z.zone_type == "EXPLOSIVE" and abs(z.center - center) < 1:
            return z
    return None


def _build_narrative(
    spot: float,
    zones: List[GravityZone],
    magnet: float,
    explosive: float,
    gravity_score: float,
    asymmetric_risk: dict = None,
) -> str:
    magnet_valid, explosive_valid = _validate_gravity_data(spot, magnet, explosive)
    global_label = _classify_compression(gravity_score)

    # Règle maîtresse : ne JAMAIS afficher "gravité faible" seul si un risque local existe
    has_local_danger = (
        asymmetric_risk is not None and asymmetric_risk.get("strength", 0) > 55
    )

    if not magnet_valid and not explosive_valid:
        if has_local_danger:
            ar = asymmetric_risk
            side_word = "sous" if ar["side"] == "DOWN" else "au-dessus de"
            return (
                f"{global_label}, mais risque local élevé {side_word} ${ar['level']:,.0f} "
                f"(force {ar['strength']:.0f}%). Si ce niveau cède, le mouvement peut s'accélérer."
            )
        if gravity_score > 60:
            return (
                "BTC évolue dans une zone d'équilibre sous forte compression. "
                "Les dealers absorbent encore la volatilité. "
                "Le marché reste bloqué en range tant qu'aucune cassure majeure n'apparaît."
            )
        return (
            "BTC évolue dans une zone d'équilibre. "
            "Aucun avantage directionnel clair. "
            "Les options indiquent un marché en attente d'un catalyseur."
        )

    parts = []

    if magnet_valid:
        direction_word = "au-dessus" if magnet > spot else "en dessous"
        dist_pct = abs(magnet - spot) / spot * 100
        # Vérifier si ce niveau est AUSSI une résistance (dual nature)
        dual_resistance = any(
            z.zone_type == "RESISTANCE" and abs(z.center - magnet) / magnet < 0.01
            for z in zones
        )
        if dual_resistance and magnet > spot:
            parts.append(
                f"${magnet:,.0f} attire le prix ({dist_pct:.1f}% au-dessus), "
                f"mais peut aussi freiner la cassure — aimant et plafond à la fois."
            )
        else:
            parts.append(
                f"Le niveau qui attire le plus le prix se situe vers ${magnet:,.0f} "
                f"({dist_pct:.1f}% {direction_word}). "
                f"Si rien ne change dans les prochains jours, BTC pourrait progressivement être attiré vers cette zone."
            )

    if explosive_valid:
        dist_pct = abs(explosive - spot) / spot * 100
        ez = _get_explosive_zone(zones, explosive)
        bias = ez.explosive_bias if ez else "NEUTRAL"

        if explosive > spot:
            # Zone AU-DESSUS du spot → BTC a déjà cassé en dessous ou n'y est pas encore monté
            if bias == "DOWN_ONLY":
                parts.append(
                    f"${explosive:,.0f} a été cassé à la baisse ({dist_pct:.1f}%). "
                    f"La zone reste une résistance de régime, mais le reclaim n'est pas automatiquement explosif."
                )
            elif bias == "SYMMETRIC":
                parts.append(
                    f"${explosive:,.0f} a été cassé à la baisse ({dist_pct:.1f}%). "
                    f"Un reclaim confirmé au-dessus peut déclencher une accélération haussière."
                )
            elif bias == "UP_ONLY":
                parts.append(
                    f"${explosive:,.0f} est au-dessus du spot ({dist_pct:.1f}%) et peut agir comme "
                    f"déclencheur haussier si reclaim confirmé."
                )
            else:  # NEUTRAL
                parts.append(
                    f"${explosive:,.0f} est une zone sensible au-dessus ({dist_pct:.1f}%). "
                    f"Tant que BTC reste sous ce niveau, le régime baissier reste actif."
                )
        else:
            # Zone EN DESSOUS du spot → danger de cassure baissière
            if bias == "DOWN_ONLY":
                parts.append(
                    f"Zone explosive baissière vers ${explosive:,.0f} ({dist_pct:.1f}%). "
                    f"Une cassure sous ce niveau peut accélérer la baisse. "
                    f"Un reclaim au-dessus ne valide pas automatiquement un squeeze haussier."
                )
            elif bias == "UP_ONLY":
                parts.append(
                    f"Zone explosive haussière vers ${explosive:,.0f} ({dist_pct:.1f}%). "
                    f"Une reprise au-dessus peut accélérer la hausse. "
                    f"Une cassure vers le bas ne valide pas automatiquement une cascade baissière."
                )
            elif bias == "SYMMETRIC":
                parts.append(
                    f"Zone explosive dans les deux sens vers ${explosive:,.0f} ({dist_pct:.1f}%). "
                    f"Cassure sous ce niveau ou reclaim peuvent déclencher une accélération."
                )
            else:  # NEUTRAL
                parts.append(
                    f"Zone sensible vers ${explosive:,.0f} ({dist_pct:.1f}%), direction explosive non confirmée."
                )

    if gravity_score > 70:
        parts.append("BTC est sous forte compression — une cassure peut survenir rapidement.")
    elif gravity_score > 40:
        parts.append("Tension modérée dans les options. Surveille les niveaux clés.")

    # Toujours mentionner le risque local asymétrique s'il est élevé
    if has_local_danger:
        ar = asymmetric_risk
        side_word = "sous" if ar["side"] == "DOWN" else "au-dessus de"
        parts.append(
            f"Risque local élevé {side_word} ${ar['level']:,.0f} (force {ar['strength']:.0f}%). "
            f"Une cassure de ce niveau peut déclencher une accélération."
        )

    return " ".join(parts) if parts else (
        "BTC évolue dans une zone d'équilibre. "
        "Les options indiquent un marché en attente d'un catalyseur."
    )


def gravity_map_summary(gmap: GravityMap) -> str:
    return f"🗺️ **Carte Gravité BTC**\n{gmap.narrative}"


def _make_filtered_snapshot(snapshot: MarketSnapshot, dte_min: int, dte_max) -> MarketSnapshot:
    from dataclasses import replace
    filtered = filter_options_by_dte(snapshot.options, dte_min=dte_min, dte_max=dte_max)
    return MarketSnapshot(btc_price=snapshot.btc_price, options=filtered, timestamp=snapshot.timestamp)


def compute_gravity_map_horizons(snapshot: MarketSnapshot) -> dict:
    """
    Retourne 3 GravityMaps DTE-aware :
      near    → 0-14j  : signal trading court terme
      monthly → 15-45j : contexte swing
      global  → tout   : structure de marché
    """
    snap_near = _make_filtered_snapshot(snapshot, 0, DTE_NEAR_MAX)
    snap_monthly = _make_filtered_snapshot(snapshot, DTE_MONTHLY_MIN, DTE_MONTHLY_MAX)

    gex_near = compute_gex(snap_near)
    gex_monthly = compute_gex(snap_monthly)
    gex_global = compute_gex(snapshot)

    return {
        "near": compute_gravity_map(snap_near, gex_near),
        "monthly": compute_gravity_map(snap_monthly, gex_monthly),
        "global": compute_gravity_map(snapshot, gex_global),
    }
