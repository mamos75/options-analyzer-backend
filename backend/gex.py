"""
Calcul GEX (Gamma Exposure) et métriques dérivées.

GEX = Σ (gamma × OI × contrat_size × spot²)
  - GEX positif → dealers long gamma → ils vendent la hausse, achètent la baisse → marché stabilisé
  - GEX négatif → dealers short gamma → ils achètent la hausse, vendent la baisse → marché amplifié

Contrat Deribit = 1 BTC. gamma exprimé en BTC/BTC².
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from .deribit_client import MarketSnapshot, OptionData

log = logging.getLogger(__name__)

CONTRACT_SIZE = 1.0  # 1 BTC par contrat Deribit

# Fichier d'alerte interne — jamais envoyé sur Telegram public
_GEX_SCALE_ALERT_FILE = "/tmp/gex_scale_alert.log"
_SUSPICIOUS_GEX_ABS_THRESHOLD = 1_000  # USD — absurde pour un marché BTC > $10k

# OI minimum (en contrats) pour qu'une expiry soit éligible near-term
_MIN_OI_NEAR_TERM = 100

# Fallback activité minimum — évite d'annuler positions sans flux récent visible
_MIN_ACTIVITY_WEIGHT = 0.1


@dataclass
class GEXHorizons:
    """Gamma Effectif segmenté par horizon — effective_gamma pondéré DTE × distance × delta × activité.

    near    = signal trading / alertes / narrative / squeeze (DTE ≤ 14)
    monthly = swing context (DTE 15-45)
    global_ = structure uniquement (DTE > 45)

    Règle : near est le SEUL horizon autorisé à piloter une alerte ou un score actionnable.
    """
    near: float
    monthly: float
    global_: float


@dataclass
class MaxPainExpiry:
    strike: float
    expiry: str
    dte: int
    oi_total: float


@dataclass
class MaxPainProfile:
    near: MaxPainExpiry                        # near-term (DTE le plus petit > 0 avec OI significatif)
    institutional: MaxPainExpiry               # structure longue (expiry avec OI total le plus élevé)
    active: Optional["MaxPainExpiry"] = field(default=None)      # expiry avec plus de volume récent
    actionable: Optional["MaxPainExpiry"] = field(default=None)  # DTE≤3 — impactant BTC maintenant


@dataclass
class GEXProfile:
    total_gex: float                       # GEX net total en USD (backward compat — structure globale)
    gex_by_strike: Dict[float, float]      # strike → GEX net
    call_gex_by_strike: Dict[float, float]
    put_gex_by_strike: Dict[float, float]
    flip_level: Optional[float]            # strike de crossing GEX (None = pas de niveau valide — jamais 0.0)
    max_pain: float                        # near-term max pain strike (backward compat)
    gamma_walls: List[float]               # strikes avec GEX absolu > 2σ (murs gamma)
    btc_price: float
    regime: str                            # "STABILISANT" | "AMPLIFICATEUR" | "NEUTRE"
    max_pain_profile: Optional[MaxPainProfile] = field(default=None)  # structuré par expiry
    flip_level_reason: str = "crossing_near"  # 'crossing_near' | 'crossing_global' | diagnostic si invalide
    flip_available: bool = False           # True ssi flip_level est un vrai niveau (pas None)
    regime_state: str = "unknown"          # 'mixed_gamma_regime' | 'negative_gamma_regime' | 'positive_gamma_regime' | 'insufficient_data'
    regime_confidence: str = "low"         # 'high' | 'medium' | 'low'
    # Gamma Effectif par horizon — seul near doit piloter les alertes/scores actionnables
    gex_near: float = 0.0     # DTE ≤ 14 — trading / alertes / narrative / squeeze
    gex_monthly: float = 0.0  # DTE 15-45 — swing context
    gex_global: float = 0.0   # DTE > 45 — structure uniquement
    # V3-bis — regime mecanique spot/flip (source unique)
    regime_meca: str = "NEUTRE"           # STABILISANT | AMPLIFICATEUR | ZONE_DE_FLIP | NEUTRE
    regime_source: str = "gex_estime"    # "flip" | "gex_estime"
    gex_intensity: float = 0.0           # |gex_total| — magnitude separee du regime
    gex_flip_incoherent: bool = False    # True si signe GEX contredit le regime mecanique


def compute_effective_gamma_horizons(snapshot: MarketSnapshot) -> GEXHorizons:
    """Gamma Effectif pondéré par DTE × distance × delta × activité — segmenté par horizon.

    effective_gamma = gamma × OI × contract_size × spot² × dte_weight × distance_weight × delta_weight × activity_weight

    Règle : near (DTE ≤ 14) = seul horizon valide pour alertes/scores actionnables.
    Jamais d'alerte basée sur gex_monthly ou gex_global.
    """
    from .options_activity_engine import compute_flow_ratio, compute_proximity_score, compute_dte_urgency

    spot = snapshot.btc_price
    near = monthly = global_ = 0.0

    for opt in snapshot.options:
        dte = _compute_dte(opt.expiry)
        if dte <= 0:
            continue

        dte_weight = compute_dte_urgency(dte)
        distance_weight = compute_proximity_score(opt.strike, spot)
        delta_weight = abs(opt.delta)   # proxy ATM/ITM — gamma ATM est le plus impactant
        flow, _ = compute_flow_ratio(opt)
        activity_weight = max(flow, _MIN_ACTIVITY_WEIGHT)  # min 10% — évite d'annuler positions structurales

        sign = 1.0 if opt.option_type == "call" else -1.0
        eg = (
            sign
            * opt.gamma * opt.oi * CONTRACT_SIZE * (spot ** 2)
            * dte_weight * distance_weight * delta_weight * activity_weight
        )

        if dte <= DTE_NEAR_MAX:
            near += eg
        elif dte <= DTE_MONTHLY_MAX:
            monthly += eg
        else:
            global_ += eg

    return GEXHorizons(near=near, monthly=monthly, global_=global_)


def compute_gex(snapshot: MarketSnapshot) -> GEXProfile:
    spot = snapshot.btc_price
    gex_by_strike: Dict[float, float] = {}
    call_gex: Dict[float, float] = {}
    put_gex: Dict[float, float] = {}

    for opt in snapshot.options:
        # GEX dealer — hypothese : clients longs options (calls ET puts)
        # → dealer short calls et short puts (hypothese simplificatrice mixte)
        # Resultat : call GEX = +gamma (hedging pro-tendance) ; put GEX = -gamma (contra-tendance)
        # NOTE B6 : hypothese differente de DEX (short-all). Voir API_CONTRACT.md "Conventions dealer".
        g = opt.gamma * opt.oi * CONTRACT_SIZE * (spot ** 2)

        if opt.option_type == "call":
            gex = g  # positif
            call_gex[opt.strike] = call_gex.get(opt.strike, 0) + gex
        else:
            gex = -g  # négatif (puts créent pression baissière sur hedging)
            put_gex[opt.strike] = put_gex.get(opt.strike, 0) + gex

        gex_by_strike[opt.strike] = gex_by_strike.get(opt.strike, 0) + gex

    total_gex = sum(gex_by_strike.values())

    _check_gex_scale(total_gex, spot, snapshot.options)

    flip_level, flip_level_reason = _find_flip_level(gex_by_strike, spot)
    flip_available, regime_state, regime_confidence = _derive_regime(flip_level_reason, flip_level)
    max_pain_profile = compute_max_pain_by_expiry(snapshot.options)
    max_pain = (
        max_pain_profile.near.strike
        if max_pain_profile
        else _compute_max_pain_single(snapshot.options)
    )
    gamma_walls = _find_gamma_walls(gex_by_strike)
    regime = _classify_regime(total_gex)
    horizons = compute_effective_gamma_horizons(snapshot)
    # V3-bis — regime mecanique (source unique)
    meca = classify_regime_spot_flip(spot, flip_level, total_gex)

    return GEXProfile(
        total_gex=total_gex,
        gex_by_strike=gex_by_strike,
        call_gex_by_strike=call_gex,
        put_gex_by_strike=put_gex,
        flip_level=flip_level,
        flip_level_reason=flip_level_reason,
        flip_available=flip_available,
        regime_state=regime_state,
        regime_confidence=regime_confidence,
        max_pain=max_pain,
        gamma_walls=gamma_walls,
        btc_price=spot,
        regime=regime,
        max_pain_profile=max_pain_profile,
        gex_near=horizons.near,
        gex_monthly=horizons.monthly,
        gex_global=horizons.global_,
        regime_meca=meca["regime_meca"],
        regime_source=meca["regime_source"],
        gex_intensity=meca["gex_intensity"],
        gex_flip_incoherent=meca["gex_flip_incoherent"],
    )


def _check_gex_scale(total_gex: float, spot: float, options: List[OptionData]) -> None:
    total_oi = sum(o.oi for o in options)
    if spot > 10_000 and total_oi > 0 and abs(total_gex) < _SUSPICIOUS_GEX_ABS_THRESHOLD:
        msg = (
            f"[GEX SCALE ALERT] suspicious: spot={spot:.0f} OI={total_oi:.0f} "
            f"abs(GEX)={abs(total_gex):.2f} < {_SUSPICIOUS_GEX_ABS_THRESHOLD} — "
            f"possible division error or bad data"
        )
        log.error(msg)
        try:
            import time
            with open(_GEX_SCALE_ALERT_FILE, "a") as f:
                f.write(f"{int(time.time())} {msg}\n")
        except Exception:
            pass


def _find_flip_level_legacy(gex_by_strike: Dict[float, float], spot: float) -> float:
    """LEGACY: premier crossing positif→négatif en partant du plus bas strike.
    Bug métier: retourne un niveau deep OTM (ex. -12%) sans lien avec le marché actuel."""
    strikes = sorted(gex_by_strike.keys())
    cumulative = 0.0
    last_strike = spot
    for strike in strikes:
        cumulative += gex_by_strike[strike]
        if cumulative < 0:
            return strike
        last_strike = strike
    return last_strike


def _find_flip_level_A(gex_by_strike: Dict[float, float], spot: float) -> float:
    """Variante A — Dernier crossing négatif→positif (retour durable en positif).
    Parcourt du plus bas au plus haut, mémorise le dernier endroit où le cumul
    passe de négatif à positif. Ce niveau marque la frontière durable où les calls
    commencent à dominer — en dessous = amplificateur, au-dessus = stabilisant."""
    strikes = sorted(gex_by_strike.keys())
    cumulative = 0.0
    last_neg_to_pos: Optional[float] = None
    for strike in strikes:
        prev = cumulative
        cumulative += gex_by_strike[strike]
        if prev < 0 and cumulative >= 0:
            last_neg_to_pos = strike
    return last_neg_to_pos if last_neg_to_pos is not None else spot


def _find_flip_level_B(gex_by_strike: Dict[float, float], spot: float) -> float:
    """Variante B — Crossing calculé uniquement sur les strikes dans ±15% du spot.
    Ignore les puts deep OTM qui polluent le cumul sans interagir avec le marché actuel.
    Si pas de crossing dans la fenêtre, retourne le bord inférieur de la fenêtre."""
    window = {k: v for k, v in gex_by_strike.items()
              if spot * 0.85 <= k <= spot * 1.15}
    if not window:
        return spot
    strikes = sorted(window.keys())
    cumulative = 0.0
    last_neg_to_pos: Optional[float] = None
    for strike in strikes:
        prev = cumulative
        cumulative += window[strike]
        if prev < 0 and cumulative >= 0:
            last_neg_to_pos = strike
    return last_neg_to_pos if last_neg_to_pos is not None else min(window.keys())


def _find_flip_level_C(gex_by_strike: Dict[float, float], spot: float) -> float:
    """Variante C — Crossing sur les strikes dont l'importance GEX dépasse 1% du total.
    Filtre le bruit des strikes marginaux (petites positions, expiries lointaines anecdotiques).
    Garde la structure dominante du carnet gamma."""
    total_abs = sum(abs(v) for v in gex_by_strike.values())
    if total_abs == 0:
        return spot
    significant = {k: v for k, v in gex_by_strike.items()
                   if abs(v) / total_abs >= 0.01}
    if not significant:
        return spot
    strikes = sorted(significant.keys())
    cumulative = 0.0
    last_neg_to_pos: Optional[float] = None
    for strike in strikes:
        prev = cumulative
        cumulative += significant[strike]
        if prev < 0 and cumulative >= 0:
            last_neg_to_pos = strike
    return last_neg_to_pos if last_neg_to_pos is not None else spot


def _find_flip_level(gex_by_strike: Dict[float, float], spot: float) -> Tuple[Optional[float], str]:
    """Flip level actif — B en premier, A comme fallback.
    Retourne (niveau, reason_code) machine-readable pour diagnostic frontend.
    Reason codes: 'crossing_near' | 'crossing_global' | 'no_near_gamma_sign_cross'
                  | 'all_gamma_negative' | 'all_gamma_positive' | 'insufficient_near_strikes'
    Règle : None = absence de niveau. 0.0 n'est jamais retourné (serait une vraie valeur)."""
    if not gex_by_strike:
        return None, "insufficient_near_strikes"

    window = {k: v for k, v in gex_by_strike.items()
              if spot * 0.85 <= k <= spot * 1.15}
    if window:
        strikes = sorted(window.keys())
        cumulative = 0.0
        last_neg_to_pos: Optional[float] = None
        for strike in strikes:
            prev = cumulative
            cumulative += window[strike]
            if prev < 0 and cumulative >= 0:
                last_neg_to_pos = strike
        if last_neg_to_pos is not None:
            return last_neg_to_pos, "crossing_near"
        # Fenêtre near existe mais pas de crossing → cherche globalement
        global_level = _find_flip_level_A(gex_by_strike, spot)
        if global_level != spot:
            return global_level, "crossing_global"
        # Aucun crossing — distinguer all_negative / all_positive / mixed sans crossing
        near_vals = list(window.values())
        if max(near_vals) <= 0:
            return None, "all_gamma_negative"
        elif min(near_vals) >= 0:
            return None, "all_gamma_positive"
        else:
            return None, "no_near_gamma_sign_cross"
    else:
        # Pas de strikes dans ±10%
        global_level = _find_flip_level_A(gex_by_strike, spot)
        if global_level != spot:
            return global_level, "crossing_global"
        all_vals = list(gex_by_strike.values())
        if max(all_vals) <= 0:
            return None, "all_gamma_negative"
        elif min(all_vals) >= 0:
            return None, "all_gamma_positive"
        else:
            return None, "no_near_gamma_sign_cross"


def _derive_regime(reason: str, flip: Optional[float]) -> Tuple[bool, str, str]:
    """Dérive (flip_available, regime_state, regime_confidence) depuis le reason code.
    flip_available=True ssi flip est un vrai niveau de crossing.
    regime_state décrit le régime gamma même quand il n'y a pas de crossing."""
    if flip is not None:
        confidence = "high" if reason == "crossing_near" else "medium"
        return True, "mixed_gamma_regime", confidence
    state_map = {
        "all_gamma_negative":        ("negative_gamma_regime", "high"),
        "all_gamma_positive":        ("positive_gamma_regime", "high"),
        "no_near_gamma_sign_cross":  ("mixed_gamma_regime",    "low"),
        "insufficient_near_strikes": ("insufficient_data",     "low"),
    }
    state, conf = state_map.get(reason, ("unknown", "low"))
    return False, state, conf


def audit_flip_variants(gex_by_strike: Dict[float, float], spot: float) -> Dict[str, float]:
    """Retourne les 4 variantes du flip level pour comparaison et audit."""
    return {
        "legacy": _find_flip_level_legacy(gex_by_strike, spot),
        "A_last_durable_crossing": _find_flip_level_A(gex_by_strike, spot),
        "B_spot_window_10pct": _find_flip_level_B(gex_by_strike, spot),
        "C_significant_strikes": _find_flip_level_C(gex_by_strike, spot),
    }


def flip_scenario_comparison() -> List[Dict]:
    """5 scénarios canoniques — avant (legacy) vs après (B+fallback A).
    Chaque scénario représente une configuration de marché réelle.
    Démontre pourquoi B est meilleur : il répond à la question trader, pas la question mathématique."""
    scenarios = [
        {
            "nom": "Puts OTM profonds (cas classique bug legacy)",
            "description": "Puts 30k-50k pèsent sur le cumul → legacy retourne 52k deep OTM",
            "spot": 105_000.0,
            "gex": {
                30000: -80_000_000, 40000: -120_000_000, 50000: -200_000_000,
                52000: -180_000_000,
                95000: 500_000_000, 100000: 400_000_000, 105000: 200_000_000,
                110000: -100_000_000, 115000: -50_000_000,
            },
        },
        {
            "nom": "Amplificateur profond — fallback A nécessaire",
            "description": "Tout négatif dans ±10% → B ne trouve pas de crossing, fallback A prend le relais",
            "spot": 95_000.0,
            "gex": {
                60000: -500_000_000, 70000: -300_000_000, 80000: -200_000_000,
                85000: -150_000_000, 90000: -100_000_000, 95000: -80_000_000,
                100000: -50_000_000, 105000: -30_000_000,
                120000: 2_000_000_000,
            },
        },
        {
            "nom": "Crossing net dans la fenêtre ±10%",
            "description": "B et legacy convergent — scénario propre sans pollution OTM",
            "spot": 100_000.0,
            "gex": {
                90000: -300_000_000, 92000: -200_000_000, 95000: -100_000_000,
                97000: 800_000_000,
                100000: 400_000_000, 103000: 200_000_000, 107000: 100_000_000,
            },
        },
        {
            "nom": "Puts ultra-profonds contaminants (spike OI expirations lointaines)",
            "description": "Options 20k-45k avec OI résiduel mais gamma faible — legacy pollué, B immunisé",
            "spot": 108_000.0,
            "gex": {
                20000: -50_000_000, 25000: -60_000_000, 35000: -80_000_000,
                45000: -90_000_000,
                98000: -200_000_000, 102000: -150_000_000,
                105000: 1_200_000_000,
                110000: 300_000_000, 115000: 100_000_000,
            },
        },
        {
            "nom": "Flip au-dessus du spot (régime AMPLIFICATEUR actif)",
            "description": "Spot sous le flip — résistance de régime au-dessus. Les deux algos concordent ici.",
            "spot": 88_000.0,
            "gex": {
                60000: -200_000_000, 70000: -300_000_000, 80000: -400_000_000,
                85000: -500_000_000, 88000: -300_000_000,
                92000: 1_500_000_000,
                95000: 400_000_000, 100000: 200_000_000,
            },
        },
    ]

    results = []
    for s in scenarios:
        spot = s["spot"]
        gex = s["gex"]
        legacy = _find_flip_level_legacy(gex, spot)
        actif, actif_reason = _find_flip_level(gex, spot)
        legacy_dist = (legacy - spot) / spot * 100
        actif_dist = (actif - spot) / spot * 100 if actif > 0 else 999.0

        def _ux_cat(dist_pct: float) -> str:
            a = abs(dist_pct)
            if a <= 1.0:
                return "déclencheur"
            if a <= 3.0:
                return "niveau actif"
            if a <= 5.0:
                return "secondaire"
            return "contexte structurel (>5%)"

        legacy_ok = abs(legacy_dist) <= 5.0
        actif_ok = abs(actif_dist) <= 5.0
        verdict = (
            "✅ les deux pertinents" if legacy_ok and actif_ok
            else "✅ actif pertinent / ❌ legacy trop loin" if actif_ok
            else "⚠️ actif fallback loin (marché extrême)" if not actif_ok
            else "✅ convergent"
        )

        results.append({
            "scenario": s["nom"],
            "description": s["description"],
            "spot": spot,
            "legacy": {
                "level": round(legacy, 0),
                "distance_pct": round(legacy_dist, 1),
                "categorie_ux": _ux_cat(legacy_dist),
                "pertinent_trader": legacy_ok,
            },
            "actif_B_fallback_A": {
                "level": round(actif, 0),
                "reason": actif_reason,
                "distance_pct": round(actif_dist, 1),
                "categorie_ux": _ux_cat(actif_dist),
                "pertinent_trader": actif_ok,
            },
            "verdict": verdict,
        })
    return results


def _compute_dte(expiry: str) -> int:
    """Calcule les jours avant expiration (DTE) depuis aujourd'hui UTC."""
    try:
        exp_date = datetime.strptime(expiry.upper(), "%d%b%y").date()
        today = datetime.now(timezone.utc).date()
        return (exp_date - today).days
    except Exception:
        return -1


# Bornes DTE par horizon — Near=trading court terme, Monthly=swing, Global=structure
DTE_NEAR_MAX = 14
DTE_MONTHLY_MIN = 15
DTE_MONTHLY_MAX = 45


def filter_options_by_dte(
    options: List[OptionData],
    dte_min: int = 0,
    dte_max: Optional[int] = None,
) -> List[OptionData]:
    """Filtre les options par DTE. Ignore les options expirées (DTE < 0)."""
    result = []
    for opt in options:
        dte = _compute_dte(opt.expiry)
        if dte < max(0, dte_min):
            continue
        if dte_max is not None and dte > dte_max:
            continue
        result.append(opt)
    return result


def _compute_max_pain_single(options: List[OptionData]) -> float:
    """Max pain sur une liste d'options — Σ(valeur intrinsèque ITM × OI) minimisée.
    mark_price exclu intentionnellement : fluctue avec BTC et rendait le calcul instable."""
    strikes = list({o.strike for o in options})
    if not strikes:
        return 0.0
    min_pain = float("inf")
    max_pain_strike = strikes[0]
    for target in strikes:
        pain = 0.0
        for opt in options:
            if opt.option_type == "call" and target > opt.strike:
                pain += (target - opt.strike) * opt.oi
            elif opt.option_type == "put" and target < opt.strike:
                pain += (opt.strike - target) * opt.oi
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = target
    return max_pain_strike


def compute_max_pain_by_expiry(options: List[OptionData]) -> Optional[MaxPainProfile]:
    """Calcule Max Pain séparé par expiry.

    near         = expiry la plus proche (DTE > 0) avec OI ≥ _MIN_OI_NEAR_TERM → actionnable maintenant
    institutional = expiry avec OI total le plus élevé (DTE > 0) → structure longue

    Retourne None si aucune expiry valide (toutes expirées ou liste vide).
    """
    if not options:
        return None

    # Grouper par expiry
    by_expiry: Dict[str, List[OptionData]] = {}
    for opt in options:
        by_expiry.setdefault(opt.expiry, []).append(opt)

    # Calculer stats par expiry, filtrer DTE <= 0 (expirées)
    expiry_stats: List[Dict] = []
    for expiry, opts in by_expiry.items():
        dte = _compute_dte(expiry)
        if dte <= 0:
            continue
        oi_total = sum(o.oi for o in opts)
        expiry_stats.append({
            "expiry": expiry,
            "dte": dte,
            "oi_total": oi_total,
            "options": opts,
        })

    if not expiry_stats:
        return None

    # Filtrer par OI significatif pour near-term (évite les minuscules hebdos)
    eligible = [s for s in expiry_stats if s["oi_total"] >= _MIN_OI_NEAR_TERM]
    if not eligible:
        eligible = expiry_stats  # fallback : toutes les expiries valides

    # Structural = toutes expiries → near (DTE le plus court) + institutional (OI le plus élevé)
    near_stat = min(eligible, key=lambda x: x["dte"])
    inst_stat = max(expiry_stats, key=lambda x: x["oi_total"])

    # Active = expiry avec le plus de volume récent (DTE > 0) — ce qui bouge
    vol_by_expiry = {
        s["expiry"]: sum(o.volume for o in s["options"])
        for s in expiry_stats
    }
    active_stat = max(expiry_stats, key=lambda x: vol_by_expiry.get(x["expiry"], 0))

    # Actionable = DTE≤3 avec OI significatif — peut impacter BTC avant expiration
    actionable_candidates = [s for s in eligible if s["dte"] <= 3]
    actionable_stat = (
        min(actionable_candidates, key=lambda x: x["dte"])
        if actionable_candidates else None
    )

    def _to_expiry(stat: dict) -> MaxPainExpiry:
        return MaxPainExpiry(
            strike=_compute_max_pain_single(stat["options"]),
            expiry=stat["expiry"],
            dte=stat["dte"],
            oi_total=stat["oi_total"],
        )

    return MaxPainProfile(
        near=_to_expiry(near_stat),
        institutional=_to_expiry(inst_stat),
        active=_to_expiry(active_stat),
        actionable=_to_expiry(actionable_stat) if actionable_stat else None,
    )


def _find_gamma_walls(gex_by_strike: Dict[float, float]) -> List[float]:
    if not gex_by_strike:
        return []
    values = list(gex_by_strike.values())
    mean = sum(abs(v) for v in values) / len(values)
    sigma = (sum((abs(v) - mean) ** 2 for v in values) / len(values)) ** 0.5
    threshold = mean + 2 * sigma
    return [s for s, g in gex_by_strike.items() if abs(g) >= threshold]


GEX_NEUTRAL_THRESHOLD = 5_000_000  # $5M — zone morte, aucune alerte en dessous
_FLIP_ZONE_PCT = 0.01  # ±1% du spot = zone de flip


def _classify_regime(total_gex: float) -> str:
    """Regime GEX base sur la magnitude totale — conserve la compatibilite backward."""
    if total_gex > GEX_NEUTRAL_THRESHOLD:
        return "STABILISANT"
    elif total_gex < -GEX_NEUTRAL_THRESHOLD:
        return "AMPLIFICATEUR"
    return "NEUTRE"


def classify_regime_spot_flip(
    spot: float,
    flip: Optional[float],
    total_gex: float,
) -> dict:
    """V3-bis — Regime mecanique unique base sur la position spot/flip.

    STABILISANT   : spot > flip (spot au-dessus du Gamma Flip)
    AMPLIFICATEUR : spot < flip (spot en-dessous du Gamma Flip)
    ZONE_DE_FLIP  : |spot - flip| / spot < FLIP_ZONE_PCT (±1%)
    NEUTRE        : flip non detecte — fallback signe GEX total

    Retourne :
      regime_meca    : str  — STABILISANT | AMPLIFICATEUR | ZONE_DE_FLIP | NEUTRE
      regime_source  : str  — "flip" | "gex_estime"
      gex_intensity  : float — |gex_total| (magnitude, separee du regime)
      gex_flip_incoherent : bool — si le signe GEX contredit le regime mecanique
    """
    gex_intensity = abs(total_gex)

    if flip is None or spot <= 0:
        # Fallback : signe GEX total
        if total_gex > GEX_NEUTRAL_THRESHOLD:
            regime_meca = "STABILISANT"
        elif total_gex < -GEX_NEUTRAL_THRESHOLD:
            regime_meca = "AMPLIFICATEUR"
        else:
            regime_meca = "NEUTRE"
        return {
            "regime_meca": regime_meca,
            "regime_source": "gex_estime",
            "gex_intensity": gex_intensity,
            "gex_flip_incoherent": False,
        }

    dist_pct = abs(spot - flip) / spot
    if dist_pct < _FLIP_ZONE_PCT:
        regime_meca = "ZONE_DE_FLIP"
    elif spot > flip:
        regime_meca = "STABILISANT"
    else:
        regime_meca = "AMPLIFICATEUR"

    # Check de coherence : signe GEX doit confirmer le regime mecanique
    # STABILISANT -> GEX devrait etre positif
    # AMPLIFICATEUR -> GEX devrait etre negatif
    incoherent = False
    if regime_meca == "STABILISANT" and total_gex < -GEX_NEUTRAL_THRESHOLD:
        incoherent = True
    elif regime_meca == "AMPLIFICATEUR" and total_gex > GEX_NEUTRAL_THRESHOLD:
        incoherent = True

    return {
        "regime_meca": regime_meca,
        "regime_source": "flip",
        "gex_intensity": gex_intensity,
        "gex_flip_incoherent": incoherent,
    }


def gex_summary(profile: GEXProfile) -> str:
    emoji = {"STABILISANT": "🟢", "AMPLIFICATEUR": "🔴", "NEUTRE": "🟡"}[profile.regime]
    spot = profile.btc_price

    # Détection asymétrie : flip_level proche sous le spot = risque baissier concentré
    flip = profile.flip_level
    flip_dist_pct = abs(flip - spot) / spot * 100 if flip is not None else 999
    asymmetric_down = flip is not None and flip < spot and flip_dist_pct < 8
    asymmetric_up = flip is not None and flip > spot and flip_dist_pct < 8

    if profile.regime == "STABILISANT":
        line1 = "Les dealers absorbent les mouvements — marché compressé."
        if asymmetric_down:
            line2 = f"BTC reste compressé, mais le risque est asymétrique : ${flip:,.0f} est la ligne rouge."
        elif asymmetric_up:
            line2 = f"BTC reste compressé — une cassure de ${flip:,.0f} pourrait déclencher une accélération."
        else:
            line2 = "BTC risque de rester en range. Pas de move violent tant que cette situation continue."
    elif profile.regime == "AMPLIFICATEUR":
        line1 = "Les dealers ont perdu le contrôle — chaque mouvement sera amplifié."
        line2 = "Le prochain move, dans un sens ou dans l'autre, sera violent. Prépare-toi."
    else:
        line1 = "Les gros acteurs ne prennent pas position pour l'instant."
        line2 = "Le marché est en phase d'attente. Aucun avantage directionnel clair."

    flip_valid = flip is not None and flip_dist_pct > 0.5
    pain_valid = profile.max_pain > 0

    parts = [f"{emoji} **Dealers BTC**", line1, line2]
    if flip_valid:
        parts.append(f"▶ Seuil de Régime : ${flip:,.0f}. Niveau où le régime dealers peut basculer (stabilisant ↔ amplificateur). ≠ support/résistance.")
    if pain_valid:
        if profile.max_pain_profile:
            near = profile.max_pain_profile.near
            parts.append(
                f"▶ Cible Expiration ({near.expiry}, J-{near.dte}) : ${profile.max_pain:,.0f}. "
                f"Si rien ne change, BTC pourrait être attiré vers cette zone avant l'expiration."
            )
        else:
            parts.append(f"▶ Cible Expiration : ${profile.max_pain:,.0f}. Si rien ne change, BTC pourrait être attiré vers cette zone.")
    return "\n".join(parts)
