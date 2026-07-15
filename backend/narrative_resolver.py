"""
Narrative Resolver — Cohérence narrative globale du dashboard.

Règle maîtresse : un dashboard doit raconter UNE seule histoire.
Deux widgets qui se contredisent = produit cassé, pas de la nuance.

Ce module prend tous les indicateurs et produit :
  - scenario_principal  : ce qui se passe
  - risque_principal    : le danger immédiat
  - niveau_haut         : attraction / résistance haut
  - niveau_bas          : ligne rouge / support bas
  - invalidation        : le niveau qui changerait tout
  - phrase_synthese     : 1 phrase de décision
  - banner_message      : texte du bandeau Range Mode
  - gex_activity_label  : badge qualité GEX (🪨/⚡/🔥/💀)
  - gex_activity_context: phrase explicative qualité GEX
  - gex_use_in_signal   : False si GEX trop dormant pour Signal Mamos
  - dex_activity_label  : badge qualité DEX (🪨/⚡/🔥/💀)
  - dex_activity_context: phrase explicative qualité DEX
  - dex_use_in_signal   : False si DEX dormant/structurel (stock, pas flux exploitable)

Règle DEX : DEX brut = stock de delta. DEX actif = flux exploitable.
Ne jamais transformer un stock dormant en signal directionnel.
"""

from dataclasses import dataclass, field
from typing import Optional

from .gex import GEXProfile
from .mopi import MOPIScore
from .dealer_pressure import DealerPressure, DEXLevels
from .gravity_map import GravityMap
from .options_walls import OptionsWallsProfile
from .squeeze_score import SqueezeScore
from .gex_activity_audit import GEXActivityAudit, FlipActivityAudit
from .gravity_activity_audit import GravityActivityAudit
from .options_activity_engine import TAG_DORMANT
from .directional_bias import DirectionalBias, compute_directional_bias
from .options_walls import OptionsWall as _OptionsWall


# ── Config convergence ──────────────────────────────────────────────────────────
CONVERGENCE_TOLERANCE_PCT: float = 0.015   # ±1.5% — même seuil que frontend CFG.LEVELS_NEAR_THRESH


@dataclass
class ConvergenceResult:
    """Résultat de detect_convergence() — niveaux convergents avec leurs types réels."""
    converging: bool                 # True si >=2 niveaux convergent
    count: int                       # nombre de niveaux convergents
    center: float | None             # prix de référence (max_pain prioritaire)
    types: list                      # types réels : ["flip", "max_pain", "call_wall", "put_wall", "atm"]
    labels: list                     # labels humains correspondants
    tolerance_pct: float             # tolérance utilisée


def detect_convergence(
    levels: dict,
    tolerance_pct: float = CONVERGENCE_TOLERANCE_PCT,
) -> ConvergenceResult:
    """Détecte les niveaux convergents avec leur type réel.

    Args:
        levels: dict {type_cle -> prix} — types reconnus :
            "flip", "max_pain", "call_wall", "put_wall", "atm"
        tolerance_pct: tolérance relative (défaut = CONVERGENCE_TOLERANCE_PCT)

    Returns:
        ConvergenceResult avec les niveaux convergents et leurs types réels.
    """
    # Ordre de priorité pour le centre de référence
    PRIORITY = ["max_pain", "flip", "call_wall", "put_wall", "atm"]
    LABELS = {
        "flip":      "Gamma Flip",
        "max_pain":  "Max Pain",
        "call_wall": "Call wall",
        "put_wall":  "Put wall",
        "atm":       "ATM",
    }

    # Filtrer les niveaux définis
    defined = {k: v for k, v in levels.items() if v is not None and v > 0}
    if len(defined) < 2:
        return ConvergenceResult(
            converging=False, count=len(defined),
            center=None, types=[], labels=[],
            tolerance_pct=tolerance_pct,
        )

    # Choisir le centre de référence par priorité
    center = None
    for key in PRIORITY:
        if key in defined:
            center = defined[key]
            break
    if center is None:
        center = list(defined.values())[0]

    # Trouver tous les niveaux dans la tolérance du centre
    converging_types = []
    converging_labels = []
    for key in PRIORITY:  # ordre fixe pour la cohérence des labels
        if key not in defined:
            continue
        price = defined[key]
        if abs(price - center) / center <= tolerance_pct:
            converging_types.append(key)
            converging_labels.append(LABELS.get(key, key))

    return ConvergenceResult(
        converging=len(converging_types) >= 2,
        count=len(converging_types),
        center=center,
        types=converging_types,
        labels=converging_labels,
        tolerance_pct=tolerance_pct,
    )



def select_levels(walls_global, spot: float) -> dict:
    """Sélecteur de niveaux unifié — F11.1.

    Retourne resistance et support depuis les murs options,
    en excluant les zones DORMANT. Fallback sur pool global si near vide.
    """
    walls = walls_global.walls if walls_global else []
    active = [w for w in walls if getattr(w, "tag", "DORMANT") != "DORMANT"]
    pool = active if active else walls

    above = [w for w in pool if w.strike > spot * 1.002]
    below = [w for w in pool if w.strike < spot * 0.998]

    resistance = min(above, key=lambda w: w.strike) if above else None
    support    = max(below, key=lambda w: w.strike) if below else None

    post_exp = bool(not active and walls)
    return {
        "resistance": resistance,
        "support": support,
        "fallback": resistance is None or support is None,
        "source": "post_expiration" if post_exp else "normal",
    }


def _wall_label(wall) -> str:
    """Génère un label typé depuis les champs réels du mur — F11.2."""
    if wall is None:
        return "niveau estimé"
    wt = getattr(wall, "wall_type", "") or ""
    if "CALL" in wt.upper():
        kind = "calls"
    elif "PUT" in wt.upper():
        kind = "puts"
    else:
        kind = "options"
    oi = f"{wall.total_oi:,.0f} BTC"
    tag = getattr(wall, "tag", "")
    tag_str = tag.lower() if tag and hasattr(tag, "lower") else str(tag).lower()
    suffix = f" ({tag_str})" if tag_str and tag_str not in ("active", "actionable", "") else ""
    return f"mur {kind}{suffix} ${wall.strike:,.0f} ({oi} OI)"



@dataclass
class NarrativeResolved:
    scenario_principal: str
    risque_principal: str
    niveau_haut: float
    niveau_haut_label: str
    niveau_bas: float
    niveau_bas_label: str
    invalidation: float
    phrase_synthese: str
    banner_message: str
    range_mode: bool
    asymmetric_side: str          # "DOWN" | "UP" | "BALANCED" | "NEUTRAL"
    max_pain_display: dict        # {"strike", "expiry", "dte", "label"}
    dex_coherent: bool            # True si DEX cohérent avec biais global (ou dormant/structurel → ignoré)
    contradictions: list          # liste des incohérences détectées et résolues
    gex_activity_label: str       # "🔥 GEX actionnable" | "⚡ GEX actif" | "🪨 GEX structurel" | "💀 GEX dormant"
    gex_activity_context: str     # phrase humaine expliquant la qualité du GEX
    gex_use_in_signal: bool       # False si GEX trop dormant/structurel pour Signal Mamos
    dex_activity_label: str       # "🔥 DEX actionnable" | "⚡ DEX actif" | "🪨 DEX structurel" | "💀 DEX dormant"
    dex_activity_context: str     # phrase humaine expliquant la qualité du DEX
    dex_use_in_signal: bool       # False si DEX dormant/structurel (positions passives, pas flux exploitable)
    gravity_target: Optional[float] = None       # cible gravity ACTIVE/ACTIONABLE uniquement (None si DORMANT)
    gravity_zone: Optional[str] = None           # label de la zone gravity cible
    gravity_tag: Optional[str] = None            # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    flip_activity_tag: Optional[str] = None      # DORMANT | STRUCTURAL | ACTIVE | ACTIONABLE
    flip_signal_quality: Optional[int] = None    # 0-10
    flip_use_in_signal: Optional[bool] = None    # False si flip dormant/structurel
    flip_activity_context: Optional[str] = None  # phrase humaine expliquant la qualité du flip
    flip_top_contributors: list = field(default_factory=list)
    directional_bias: Optional[DirectionalBias] = None
    # Point 8 — Risk Matrix (remplace "Aucun risque immédiat identifié")
    risk_matrix: Optional[dict] = None
    # F8.4 — Echelle des niveaux (ladders)
    upside_ladder: list = field(default_factory=list)   # [{price, types, oi, dist_pct, tag}]
    downside_ladder: list = field(default_factory=list)
    # Phase 2 Sprint 3 — types réels des niveaux
    niveau_haut_type: str = ""         # flip | call_wall | put_wall | atm | gravity | fallback
    niveau_bas_type: str = ""          # flip | call_wall | put_wall | atm | gravity | fallback
    convergence: object = None          # ConvergenceResult depuis detect_convergence()



def _build_ladders(spot: float, flip: float | None, mp_strike: float, walls, max_pain_dte: int) -> tuple:
    """F8.4 — Construit les echelles upside/downside de niveaux options.

    Chaque entree : {price, types, oi, dist_pct, tag}
    - upside_ladder : niveaux au-dessus du spot (croissant, plus proche en tete)
    - downside_ladder : niveaux en-dessous du spot (decroissant, plus proche en tete)

    Regles :
    - Prix exact pour flip et max_pain (pas d'arrondi)
    - OI réel depuis wall.total_oi (BTC)
    - Déduplication à 0.5% : si flip et wall au même strike → une entrée multi-types
    - Walls non-DORMANT prioritaires ; fallback sur tous si aucun actif
    """
    # Clé = prix exact (pas arrondi) pour préserver flip et max_pain précis
    candidates: dict[float, dict] = {}

    def _add(price: float, typ: str, oi: float | None = None, tag: str = "WALL"):
        if not price or price <= 0:
            return
        # Chercher si un candidat existe déjà à moins de 0.5% de ce prix
        for existing_price in list(candidates.keys()):
            if abs(existing_price - price) / spot < 0.005:
                entry = candidates[existing_price]
                if typ not in entry["types"]:
                    entry["types"].append(typ)
                # Conserver OI le plus élevé
                if oi and (entry["oi"] is None or oi > entry["oi"]):
                    entry["oi"] = oi
                # Promouvoir le tag si plus important (FLIP > WALL > MAX_PAIN)
                _priority = {"FLIP": 3, "WALL": 2, "MAX_PAIN": 1}
                if _priority.get(tag, 0) > _priority.get(entry["tag"], 0):
                    entry["tag"] = tag
                return
        # Nouveau candidat
        candidates[price] = {"price": float(price), "types": [typ], "oi": oi, "tag": tag}

    # 1. Flip (prix exact, priorité maximale)
    if flip is not None and flip > 0:
        _add(flip, "FLIP", tag="FLIP")

    # 2. Max Pain si expiry proche (≤ 7 jours)
    if mp_strike > 0 and max_pain_dte <= 7:
        _add(mp_strike, "MAX_PAIN", tag="MAX_PAIN")

    # 3. Walls — préférer non-DORMANT, fallback sur tous
    if walls and hasattr(walls, "walls") and walls.walls:
        active_walls = [
            w for w in walls.walls
            if getattr(w, "tag", None) not in ("DORMANT",)
            and str(getattr(w, "tag", "")).upper() != "DORMANT"
        ]
        # Fallback : si aucun actif, prendre tous (max 8)
        pool = active_walls if active_walls else list(walls.walls)[:8]
        for w in pool[:10]:
            strike = getattr(w, "strike", None)
            # OI réel : total_oi en BTC (champ correct de OptionsWall)
            oi = getattr(w, "total_oi", None) or getattr(w, "oi", None)
            if strike:
                _add(float(strike), "WALL", oi=float(oi) if oi else None)
                # F11.3 dissolution flag
                delta = getattr(w, "oi_delta_24h", 0.0) or 0.0
                oi_val = float(oi) if oi else 0.0
                if oi_val > 0 and delta < 0 and abs(delta) / oi_val > 0.20:
                    entry = candidates.get(float(strike))
                    if entry:
                        entry["dissolution"] = True
                        pct = round(abs(delta) / oi_val * 100, 0)
                        entry["dissolution_note"] = f"OI -{pct:.0f}% en 24h"
    else:
        # Fallback profil simplifié
        if hasattr(walls, "major_call_wall") and walls.major_call_wall:
            _add(float(walls.major_call_wall), "WALL")
        if hasattr(walls, "major_put_wall") and walls.major_put_wall:
            _add(float(walls.major_put_wall), "WALL")

    # 4. Split upside / downside, calcul dist_pct
    upside = []
    downside = []
    for price, entry in candidates.items():
        dist_pct = (price - spot) / spot * 100
        entry["dist_pct"] = round(dist_pct, 2)
        if price > spot * 1.002:
            upside.append(entry)
        elif price < spot * 0.998:
            downside.append(entry)

    # 5. Tri final
    upside.sort(key=lambda x: x["price"])
    downside.sort(key=lambda x: x["price"], reverse=True)

    return upside[:5], downside[:5]


def _compute_risk_matrix(
    gex: "GEXProfile",
    dp: "DealerPressure",
    mopi: "MOPIScore",
    sq: "SqueezeScore",
    spot: float,
    flip_level: Optional[float],
    flip_use_in_signal: bool,
    iv_rank: float = 50.0,
) -> dict:
    """Point 8 — Classification fragmentée du risque.

    Remplace le fallback binaire "Aucun risque immédiat identifié".
    Le risque n'est jamais absent — il est fragmenté selon les couches.

    Retourne un dict avec 5 dimensions :
      directionnel     : modéré/élevé/faible + bearish/bullish/neutre
      volatilite       : faible/modéré/élevé
      squeeze          : dormant/croissant/actif
      faux_breakout    : faible/modéré/élevé
      rupture_regime   : dormant/actif + niveau
    """
    gex_regime = gex.regime if gex else "NEUTRE"
    squeeze_prob = sq.probability_pct if sq else 0.0
    iv_r = getattr(mopi, "iv_rank", iv_rank) if mopi else iv_rank

    # ── Risque directionnel ─────────────────────────────────────────────────
    if gex_regime == "AMPLIFICATEUR":
        if dp.direction == "BEARISH_FLOWS":
            dir_niveau, dir_sens, dir_color = "élevé", "bearish", "red"
        elif dp.direction == "BULLISH_FLOWS":
            dir_niveau, dir_sens, dir_color = "élevé", "bullish", "red"
        else:
            dir_niveau, dir_sens, dir_color = "modéré", "neutre (direction non confirmée)", "orange"
    elif gex_regime == "STABILISANT":
        dir_niveau, dir_sens, dir_color = "faible", "compressé (dealers freinent)", "green"
    else:
        dir_niveau, dir_sens, dir_color = "faible", "neutre", "green"

    # ── Risque de volatilité ────────────────────────────────────────────────
    if iv_r > 70 or squeeze_prob > 60:
        vol_niveau, vol_color = "élevé", "red"
        vol_detail = f"IV rank {iv_r:.0f}% — options historiquement très chères" if iv_r > 70 else "Squeeze élevé"
    elif iv_r > 50 or squeeze_prob > 30:
        vol_niveau, vol_color = "modéré", "orange"
        vol_detail = f"IV rank {iv_r:.0f}% — compression en cours"
    else:
        vol_niveau, vol_color = "faible", "green"
        vol_detail = f"IV rank {iv_r:.0f}% — volatilité normale"

    # ── Risque de squeeze ───────────────────────────────────────────────────
    if squeeze_prob > 60:
        sq_niveau, sq_color = "actif", "red"
    elif squeeze_prob > 30:
        sq_niveau, sq_color = "croissant", "orange"
    else:
        sq_niveau, sq_color = "dormant", "green"

    # ── Risque de faux breakout ─────────────────────────────────────────────
    # Élevé si GEX AMPLIFICATEUR + flip proche + IV élevée
    flip_ok = flip_level is not None and flip_use_in_signal
    flip_dist_pct = abs(flip_level - spot) / spot * 100 if flip_ok else 999
    if gex_regime == "AMPLIFICATEUR" and flip_dist_pct < 5 and iv_r > 60:
        fb_niveau, fb_color = "élevé", "red"
        fb_detail = f"Régime amplificateur + flip à {flip_dist_pct:.1f}% + IV élevée"
    elif gex_regime == "AMPLIFICATEUR" and flip_dist_pct < 10:
        fb_niveau, fb_color = "modéré", "orange"
        fb_detail = f"Régime amplificateur, flip à {flip_dist_pct:.1f}%"
    elif gex_regime == "STABILISANT" and iv_r > 70:
        fb_niveau, fb_color = "modéré", "orange"
        fb_detail = "Compression + IV haute — cassure possible si catalyseur"
    else:
        fb_niveau, fb_color = "faible", "green"
        fb_detail = "Conditions sans amplification de breakout"

    # ── Risque de rupture de régime ─────────────────────────────────────────
    if flip_ok and flip_dist_pct <= 3:
        if flip_level > spot:
            rr_niveau = "actif"
            rr_detail = f"Rupture haussière de régime si ${flip_level:,.0f} cède ({flip_dist_pct:.1f}%)"
            rr_color = "red"
        else:
            rr_niveau = "actif"
            rr_detail = f"Rupture baissière de régime si ${flip_level:,.0f} cède ({flip_dist_pct:.1f}%)"
            rr_color = "red"
    elif flip_ok and flip_dist_pct <= 8:
        rr_niveau = "surveillance"
        rr_detail = f"Flip ${flip_level:,.0f} à {flip_dist_pct:.1f}% — à surveiller"
        rr_color = "orange"
    else:
        rr_niveau = "dormant"
        rr_detail = "Aucun niveau de rupture immédiat identifié" if not flip_ok else f"Flip ${flip_level:,.0f} loin ({flip_dist_pct:.1f}%)"
        rr_color = "green"

    return {
        "directionnel":   {"niveau": dir_niveau, "sens": dir_sens, "color": dir_color},
        "volatilite":     {"niveau": vol_niveau, "detail": vol_detail, "color": vol_color},
        "squeeze":        {"niveau": sq_niveau, "prob_pct": round(squeeze_prob, 1), "color": sq_color},
        "faux_breakout":  {"niveau": fb_niveau, "detail": fb_detail, "color": fb_color},
        "rupture_regime": {"niveau": rr_niveau, "detail": rr_detail, "color": rr_color},
    }


def _gex_calibration_caveat(status: str, reason_code: str) -> str:
    """Retourne le caveat calibration GEX selon le statut.
    available → "" (aucun caveat)
    degraded  → avertissement signal à confirmer
    stale     → avertissement données anciennes
    unavailable → avertissement lecture à confirmer
    """
    if status == "available":
        return ""
    if status == "degraded":
        return "⚠️ Calibration GEX dégradée : signal à confirmer."
    if status == "stale":
        return "⚠️ Calibration GEX ancienne : prudence sur l'intensité."
    return "⚠️ Calibration GEX indisponible : lecture à confirmer."


def _append_once(text: str, caveat: str) -> str:
    """Ajoute caveat à text uniquement s'il n'y est pas déjà (exact ou sémantique).

    Dedup sémantique : extrait le marqueur principal du caveat (partie avant ':')
    et vérifie s'il est déjà présent dans le texte, pour éviter les doublons
    même si la formulation exacte diffère.
    """
    if not caveat or caveat in text:
        return text
    # Marqueur principal = "Calibration GEX indisponible", "Calibration GEX dégradée", etc.
    caveat_marker = caveat.lstrip("⚠️ ").split(":")[0].strip()
    if caveat_marker and caveat_marker in text:
        return text
    return f"{text} {caveat}" if text else caveat


# Formulations assertives GEX → remplacements conditionnels par statut de calibration.
# Ordre : du plus spécifique au plus général pour éviter les substitutions partielles.
_GEX_WORDING_SUBS: list = [
    ("chaque move vers le bas sera amplifié", {
        "degraded":    "une amplification baissière reste possible si le flux actuel se confirme",
        "stale":       "amplification baissière possible",
        "unavailable": "amplification baissière possible si le flux se confirme",
    }),
    ("chaque move sera amplifié dans les deux sens", {
        "degraded":    "une amplification reste possible dans les deux sens si le flux se confirme",
        "stale":       "amplification possible dans les deux sens",
        "unavailable": "amplification possible dans les deux sens si le flux se confirme",
    }),
    ("Chaque move sera amplifié dans les deux sens", {
        "degraded":    "Une amplification reste possible dans les deux sens si le flux se confirme",
        "stale":       "Amplification possible dans les deux sens",
        "unavailable": "Amplification possible dans les deux sens si le flux se confirme",
    }),
    ("tout mouvement sera exacerbé dans les deux sens", {
        "degraded":    "tout mouvement pourrait être exacerbé si le flux se confirme",
        "stale":       "tout mouvement pourrait être exacerbé",
        "unavailable": "tout mouvement pourrait être exacerbé si le flux se confirme",
    }),
    ("mouvement brutal attendu", {
        "degraded":    "mouvement brutal possible si la pression se confirme",
        "stale":       "mouvement brutal possible",
        "unavailable": "mouvement brutal possible si le flux se confirme",
    }),
    ("le prochain mouvement sera violent", {
        "degraded":    "le prochain mouvement pourrait être violent si le flux se confirme",
        "stale":       "le prochain mouvement pourrait être violent",
        "unavailable": "le prochain mouvement pourrait être violent si le flux se confirme",
    }),
    ("mouvement violent imminent", {
        "degraded":    "mouvement violent possible si un catalyseur se confirme",
        "stale":       "mouvement violent possible",
        "unavailable": "mouvement violent possible si un catalyseur se confirme",
    }),
    ("tout breakout amplifié", {
        "degraded":    "breakout potentiellement amplifié si le flux se confirme",
        "stale":       "breakout potentiellement amplifié",
        "unavailable": "breakout potentiellement amplifié si le flux se confirme",
    }),
    ("breakout amplifié", {
        "degraded":    "breakout potentiellement amplifié si le flux se confirme",
        "stale":       "breakout potentiellement amplifié",
        "unavailable": "breakout potentiellement amplifié si le flux se confirme",
    }),
    ("Régime amplificateur baissier", {
        "degraded":    "Le GEX suggère un régime amplificateur baissier",
        "stale":       "GEX indique un régime amplificateur baissier",
        "unavailable": "Le GEX pointe vers un régime amplificateur baissier",
    }),
    ("Régime amplificateur haussier", {
        "degraded":    "Le GEX suggère un régime amplificateur haussier",
        "stale":       "GEX indique un régime amplificateur haussier",
        "unavailable": "Le GEX pointe vers un régime amplificateur haussier",
    }),
    ("Régime amplificateur neutre", {
        "degraded":    "Le GEX suggère un régime amplificateur sans direction confirmée",
        "stale":       "GEX indique un régime amplificateur sans direction confirmée",
        "unavailable": "Le GEX pointe vers un régime amplificateur sans direction confirmée",
    }),
    ("Régime amplificateur actif", {
        "degraded":    "Le GEX suggère un régime amplificateur",
        "stale":       "GEX indique un régime amplificateur",
        "unavailable": "Le GEX pointe vers un régime amplificateur",
    }),
]


def _apply_gex_confidence_wording(text: str, calibration_status: str) -> str:
    """Adoucit les formulations GEX assertives selon le statut de calibration.

    available   → texte inchangé
    degraded    → affirmations → hypothèses conditionnelles
    stale       → ajoute notion de données anciennes
    unavailable → certitudes → hypothèses non validées

    Règle : n'appeler que si gex_use_in_signal=True.
    Si GEX déjà exclu, le texte ne contient pas de certitudes GEX à adoucir.
    """
    if calibration_status == "available":
        return text
    for original, replacements in _GEX_WORDING_SUBS:
        replacement = replacements.get(calibration_status)
        if replacement and original in text:
            text = text.replace(original, replacement)
    return text


def resolve_narrative(
    mopi: MOPIScore,
    gex: GEXProfile,
    dp: DealerPressure,
    gmap: GravityMap,
    walls: OptionsWallsProfile,
    sq: SqueezeScore,
    spot: float,
    audit: Optional[GEXActivityAudit] = None,
    dex_levels: Optional[DEXLevels] = None,
    gravity_audit: Optional[GravityActivityAudit] = None,
    flip_audit: Optional[FlipActivityAudit] = None,
    calibration_status: str = "available",
    calibration_reason_code: str = "calibration_available",
) -> NarrativeResolved:
    contradictions = []

    # ── Gravity activity — strikes DORMANT exclus des cibles ──────────────
    dormant_strikes: set[float] = set()
    if gravity_audit:
        dormant_strikes = {
            z.strike for z in gravity_audit.zones
            if not z.use_in_signal
        }

    # ── Max Pain unifié ────────────────────────────────────────────────────
    mp_near = gex.max_pain_profile.near if gex.max_pain_profile else None
    mp_inst = gex.max_pain_profile.institutional if gex.max_pain_profile else None
    if mp_near:
        max_pain_display = {
            "strike": round(mp_near.strike, 0),
            "expiry": mp_near.expiry,
            "dte": mp_near.dte,
            "label": f"Cible {mp_near.expiry} (J-{mp_near.dte}) : ${mp_near.strike:,.0f}",
        }
        # Note: near != institutional = deux horizons normaux, pas une contradiction
    else:
        mp_strike_fallback = round(gex.max_pain, 0)
        max_pain_display = {
            "strike": mp_strike_fallback,
            "expiry": "?",
            "dte": 0,
            "label": f"${mp_strike_fallback:,.0f}",
        }

    # ── Range mode ────────────────────────────────────────────────────────
    range_mode = gex.regime == "STABILISANT" and gex.regime_meca != "ZONE_DE_FLIP"
    mp_strike = max_pain_display["strike"]
    near_max_pain = abs(spot - mp_strike) / spot < 0.04  # old: range scenario trigger
    # F8.5 — PIN priority: spot colle au max_pain (<=0.5%) ET expiry aujourd'hui/demain
    pin_active = (
        mp_strike > 0
        and abs(spot - mp_strike) / spot < 0.005
        and max_pain_display.get('dte', 99) <= 1
    )

    # ── Asymétrie du risque ───────────────────────────────────────────────
    flip = gex.flip_level
    flip_use_in_signal_val = flip_audit.flip_use_in_signal if flip_audit is not None else True
    asymmetric_side = "NEUTRAL"
    if flip is not None and flip_use_in_signal_val:
        dist_pct = abs(flip - spot) / spot * 100
        if dist_pct <= 8:
            asymmetric_side = "DOWN" if flip < spot else "UP"

    # ── Qualité DEX (conditionne AVANT le check de cohérence) ───────────────
    dex_label, dex_context, dex_use_in_signal = _build_dex_activity(dex_levels)

    # ── Vérification cohérence DEX ────────────────────────────────────────
    # Règle : DEX dormant/structurel = stock de delta, pas un flux exploitable.
    # Ne jamais transformer un stock dormant en signal directionnel ou contradiction.
    dex_coherent = True
    # Si dex_use_in_signal=False : DEX structurel de fond, aucune contradiction émise.

    # ── Vérification cohérence Gravity ───────────────────────────────────
    # Si gravity_score bas mais zone explosive forte sous le spot
    if gmap.gravity_score < 40 and gmap.asymmetric_risk and gmap.asymmetric_risk.get("strength", 0) > 70:
        ar = gmap.asymmetric_risk
        contradictions.append({
            "widget": "Gravity Score vs Zone Explosive",
            "detail": (
                f"Score global {gmap.gravity_score:.0f}/100 ('gravité faible') "
                f"mais zone {ar['zone_type']} ${ar['level']:,.0f} = force {ar['strength']:.0f}%"
            ),
            "resolution": f"Gravité globale faible, mais risque local élevé sous ${ar['level']:,.0f}",
        })

    # ── Qualité GEX (conditionne le wording du scénario) ────────────────────
    gex_label, gex_context, gex_use_in_signal = _build_gex_activity(audit, gex)

    # ── Scénario principal ────────────────────────────────────────────────
    # F8.5 — PIN prioritaire : spot collé au max_pain (≤0.5%) ET expiry J0/J1
    if pin_active:
        _pin_dist = abs(spot - mp_strike) / spot * 100
        scenario = (
            f"PIN d'expiration actif — Spot collé au Max Pain ${mp_strike:,.0f} "
            f"(écart {_pin_dist:.1f}%) — Mouvement directionnel improbable avant le fixing "
            f"({max_pain_display.get('expiry', '?')}, J-{max_pain_display.get('dte', 0)})"
        )
    elif range_mode and near_max_pain:
        scenario = (
            f"Range compressé — BTC attiré vers ${mp_strike:,.0f} "
            f"({max_pain_display['expiry']}, J-{max_pain_display['dte']})"
        )
    elif range_mode:
        scenario = (
            f"Range compressé — Cible expiration ${mp_strike:,.0f} "
            f"({max_pain_display['expiry']}), spot ${spot:,.0f}"
        )
    elif gex.regime_meca == "ZONE_DE_FLIP":
        flip_val = gex.flip_level
        if flip_val:
            scenario = (
                f"SUR la ligne de bascule (Gamma Flip ${flip_val:,.0f}) — "
                "clôture au-dessus = régime stabilisant, en-dessous = amplificateur. "
                "Pas de biais directionnel exploitable dans cet état."
            )
        else:
            scenario = (
                "SUR la ligne de bascule — régime indéterminé. "
                "Clôture au-dessus = stabilisant, en-dessous = amplificateur."
            )
    elif gex.regime == "AMPLIFICATEUR":
        if not gex_use_in_signal:
            # GEX dormant ou structurel : vocabulaire conditionnel, jamais affirmatif
            audit_profile = audit.overall_profile if audit else "STRUCTURAL"
            if audit_profile == "DORMANT":
                scenario = (
                    "💀 GEX dormant — le régime brut suggère une amplification, "
                    "mais le signal vient majoritairement de positions inactives. "
                    "Contexte de fond, pas signal court terme exploitable"
                )
            else:
                scenario = (
                    "🪨 GEX structurel — compression de fond détectée, "
                    "peu d'activité récente. "
                    "Signal GEX à confirmer par volume et activité marché"
                )
        elif dp.direction == "BEARISH_FLOWS":
            scenario = "Régime amplificateur baissier — chaque move vers le bas sera amplifié"
        elif dp.direction == "BULLISH_FLOWS":
            scenario = "Régime amplificateur haussier — breakout possible si résistances cèdent"
        else:
            scenario = "Régime amplificateur neutre — direction incertaine, mouvement violent imminent"
    else:
        scenario = "Phase de transition — aucun signal directionnel dominant"

    # ── Risque principal ──────────────────────────────────────────────────
    risque = _compute_main_risk(
        flip, spot, gex.regime, sq, gmap,
        use_in_signal=gex_use_in_signal,
        flip_use_in_signal=flip_use_in_signal_val,
    )

    # ── Niveaux haut / bas (excluant zones DORMANT) ───────────────────────
    niveau_haut, niveau_haut_label = _compute_niveau_haut(spot, gmap, walls, dormant_strikes)
    niveau_bas, niveau_bas_label = _compute_niveau_bas(
        spot, flip, gmap, walls, dormant_strikes,
        flip_use_in_signal=flip_use_in_signal_val,
    )

    # ── Types réels des niveaux + detect_convergence ─────────────────────
    niveau_haut_type = _infer_niveau_type(niveau_haut_label, niveau_haut, flip, spot)
    niveau_bas_type  = _infer_niveau_type(niveau_bas_label, niveau_bas, flip, spot)
    _cw_price = walls.major_call_wall if (walls and walls.major_call_wall and walls.major_call_wall > spot * 1.002) else None
    _pw_price = walls.major_put_wall  if (walls and walls.major_put_wall  and walls.major_put_wall  < spot * 0.998) else None
    _conv_result = detect_convergence({
        "flip":      flip if flip else None,
        "max_pain":  mp_strike if mp_strike else None,
        "call_wall": _cw_price,
        "put_wall":  _pw_price,
    })

    # ── F8.4 — Ladders (upside / downside levels) ─────────────────────────
    _mp_dte = max_pain_display.get('dte', 99) if max_pain_display else 99
    upside_ladder, downside_ladder = _build_ladders(spot, flip, mp_strike, walls, _mp_dte)

    # ── Invalidation ──────────────────────────────────────────────────────
    if flip is not None and flip_use_in_signal_val:
        invalidation = flip
    else:
        invalidation = niveau_bas

    # ── Wording GEX — adoucissement à la source si calibration dégradée ──
    # Règle priorité : gex_use_in_signal=False → GEX déjà exclu → pas de transformation.
    # Un caveat ajouté après une certitude n'annule pas la certitude :
    # c'est la formulation principale qui doit devenir conditionnelle.
    caveat = _gex_calibration_caveat(calibration_status, calibration_reason_code)
    if gex_use_in_signal and calibration_status != "available":
        scenario = _apply_gex_confidence_wording(scenario, calibration_status)
        risque   = _apply_gex_confidence_wording(risque, calibration_status)

    # ── Phrase synthèse ───────────────────────────────────────────────────
    phrase_synthese = _build_synthese(
        scenario, risque, niveau_haut, niveau_bas,
        range_mode, asymmetric_side, mp_strike, flip, spot,
        flip_use_in_signal=flip_use_in_signal_val,
        upside_ladder=upside_ladder,
        downside_ladder=downside_ladder,
    )
    # F8.5 — PIN override : si pin actif, la synthèse principale = message pin
    if pin_active:
        _pin_dist = abs(spot - mp_strike) / spot * 100
        phrase_synthese = (
            f"Spot collé au Max Pain ${mp_strike:,.0f} (écart {_pin_dist:.1f}%) — "
            f"pin d'expiration en cours. Mouvement directionnel improbable avant le fixing."
        )
    if gex_use_in_signal and calibration_status != "available":
        phrase_synthese = _apply_gex_confidence_wording(phrase_synthese, calibration_status)

    # ── Banner message ────────────────────────────────────────────────────
    banner_message = _build_banner(range_mode, asymmetric_side, flip, spot, gex.regime, gex_use_in_signal)
    if gex_use_in_signal and calibration_status != "available":
        banner_message = _apply_gex_confidence_wording(banner_message, calibration_status)

    # ── Append caveat à tous les champs narratifs (après softening) ───────
    # _append_once évite les doublons si le wording substitution l'a déjà intégré.
    if gex_use_in_signal and caveat:
        scenario        = _append_once(scenario, caveat)
        risque          = _append_once(risque, caveat)
        phrase_synthese = _append_once(phrase_synthese, caveat)
        banner_message  = _append_once(banner_message, caveat)

    # ── Gravity target — zone ACTIVE/ACTIONABLE la plus proche ───────────
    gravity_target, gravity_zone, gravity_tag = _compute_gravity_target(spot, gravity_audit)
    gravity_targets_list = _compute_gravity_targets_list(spot, gravity_audit)

    # ── Directional Bias Score ───────────────────────────────────────────
    mp_near = gex.max_pain_profile.near if gex.max_pain_profile else None
    db = compute_directional_bias(
        pc_ratio_weighted=mopi.pc_ratio_weighted,
        gex_use_in_signal=gex_use_in_signal,
        dex_use_in_signal=dex_use_in_signal,
        dex_actionable_btc=dex_levels.actionable if dex_levels else 0.0,
        asymmetric_side=asymmetric_side,
        spot=spot,
        flip_level=gex.flip_level,
        niveau_haut=niveau_haut,
        niveau_haut_label=niveau_haut_label,
        niveau_bas=niveau_bas,
        niveau_bas_label=niveau_bas_label,
        max_pain_near_strike=mp_near.strike if mp_near else None,
        gravity_target=gravity_target,
        gravity_targets_list=gravity_targets_list,
    )

    # Point 8 — Risk Matrix (classification fragmentée)
    iv_rank_val = getattr(mopi, "iv_rank", 50.0)
    risk_matrix = _compute_risk_matrix(
        gex=gex, dp=dp, mopi=mopi, sq=sq, spot=spot,
        flip_level=flip, flip_use_in_signal=flip_use_in_signal_val,
        iv_rank=iv_rank_val,
    )

    flip_ctx = _build_flip_activity_context(flip_audit)
    return NarrativeResolved(
        scenario_principal=scenario,
        risque_principal=risque,
        niveau_haut=niveau_haut,
        niveau_haut_label=niveau_haut_label,
        niveau_bas=niveau_bas,
        niveau_bas_label=niveau_bas_label,
        invalidation=invalidation,
        phrase_synthese=phrase_synthese,
        banner_message=banner_message,
        range_mode=range_mode,
        asymmetric_side=asymmetric_side,
        max_pain_display=max_pain_display,
        dex_coherent=dex_coherent,
        contradictions=contradictions,
        gex_activity_label=gex_label,
        gex_activity_context=gex_context,
        gex_use_in_signal=gex_use_in_signal,
        dex_activity_label=dex_label,
        dex_activity_context=dex_context,
        dex_use_in_signal=dex_use_in_signal,
        gravity_target=gravity_target,
        gravity_zone=gravity_zone,
        gravity_tag=gravity_tag,
        flip_activity_tag=flip_audit.flip_activity_tag if flip_audit else None,
        flip_signal_quality=flip_audit.flip_signal_quality if flip_audit else None,
        flip_use_in_signal=flip_use_in_signal_val,
        flip_activity_context=flip_ctx,
        flip_top_contributors=flip_audit.top_contributors if flip_audit else [],
        directional_bias=db,
        risk_matrix=risk_matrix,
        upside_ladder=upside_ladder,
        downside_ladder=downside_ladder,
        niveau_haut_type=niveau_haut_type,
        niveau_bas_type=niveau_bas_type,
        convergence=_conv_result,
    )


def _compute_main_risk(
    flip: float, spot: float, regime: str, sq: SqueezeScore, gmap: GravityMap,
    use_in_signal: bool = True,
    flip_use_in_signal: bool = True,
) -> str:
    """Risque principal = le danger le plus immédiat et exploitable."""
    risks = []

    if flip is not None and flip_use_in_signal:
        dist_pct = abs(flip - spot) / spot * 100
        if dist_pct <= 8:
            side = "sous" if flip < spot else "au-dessus de"
            intensity = "violente" if dist_pct < 3 else "significative"
            direction = "baissière" if flip < spot else "haussière de résistance"
            risks.append((dist_pct, f"Rupture {direction} {intensity} si ${flip:,.0f} cède ({dist_pct:.1f}% du spot)"))

    # Zones explosives depuis gravity map
    explosives_near = [
        z for z in gmap.zones
        if z.zone_type == "EXPLOSIVE" and abs(z.center - spot) / spot < 0.10
    ]
    for z in explosives_near:
        dist = abs(z.center - spot) / spot * 100
        side = "sous" if z.center < spot else "au-dessus de"
        risks.append((dist, f"Zone explosive ${z.center:,.0f} — si atteinte, mouvement brutal attendu"))

    if not risks:
        if regime == "AMPLIFICATEUR":
            if use_in_signal:
                return "Régime amplificateur actif — tout mouvement sera exacerbé dans les deux sens"
            return "Le GEX brut suggère un régime amplificateur — à confirmer par volume et activité récente"
        # Point 8 — plus de "Aucun risque immédiat" — le risque est toujours fragmenté
        return "Aucun déclencheur immédiat identifié — risques fragmentés selon les couches (voir risk_matrix)"

    risks.sort(key=lambda r: r[0])
    return risks[0][1]


def _is_dormant(center: float, dormant_strikes: set) -> bool:
    return any(abs(center - s) < 1 for s in dormant_strikes)


def _infer_niveau_type(label: str, price: float, flip, spot: float) -> str:
    """Infère le type réel d'un niveau depuis son label et sa position relative.

    Types retournés : "flip" | "call_wall" | "put_wall" | "atm" | "gravity" | "fallback"
    """
    lbl = label.lower() if label else ""
    if "flip" in lbl or "ligne rouge" in lbl or "régime bascule" in lbl or "bascule si" in lbl:
        return "flip"
    if flip is not None and abs(price - flip) / max(flip, 1) < 0.002:
        return "flip"
    if "call" in lbl:
        return "call_wall"
    if "put" in lbl:
        return "put_wall"
    if "atm" in lbl or "at_money" in lbl:
        return "atm"
    if "attraction" in lbl or "cible d'attraction" in lbl:
        return "gravity"
    # fallback : aucun tag exploitable
    return "fallback"


def _compute_niveau_haut(
    spot: float, gmap: GravityMap, walls: OptionsWallsProfile,
    dormant_strikes: set | None = None,
) -> tuple:
    """Niveau haut = cible/résistance principale au-dessus du spot. Exclut les zones DORMANT."""
    ds = dormant_strikes or set()
    magnets_above = [
        z for z in gmap.zones
        if z.zone_type == "MAGNETIC" and z.center > spot * 1.005 and not _is_dormant(z.center, ds)
    ]
    resistance_above = [
        z for z in gmap.zones
        if z.zone_type == "RESISTANCE" and z.center > spot * 1.005 and not _is_dormant(z.center, ds)
    ]

    # F11.1 — sélecteur unifié en premier
    lvl = select_levels(walls, spot)
    if lvl["resistance"] is not None:
        wall = lvl["resistance"]
        label = _wall_label(wall)
        if lvl["source"] == "post_expiration":
            label += " — structure post-expiration"
        return wall.strike, label

    niveau = walls.major_call_wall if walls.major_call_wall and walls.major_call_wall > spot * 1.005 else 0.0

    if magnets_above:
        magnet = min(magnets_above, key=lambda z: abs(z.center - spot))
        niveau = magnet.center
        # Est-ce AUSSI une résistance (dual nature) ?
        is_also_resistance = (
            any(abs(r.center - niveau) / niveau < 0.015 for r in resistance_above) or
            (walls.major_call_wall and walls.major_call_wall > spot and abs(walls.major_call_wall - niveau) / niveau < 0.015)
        )
        if is_also_resistance:
            return niveau, f"${niveau:,.0f} attire le prix, mais peut aussi freiner la cassure"
        return niveau, f"${niveau:,.0f} — cible d'attraction principale"

    if resistance_above:
        res = min(resistance_above, key=lambda z: abs(z.center - spot))
        return res.center, f"${res.center:,.0f} — résistance principale"

    if niveau > spot * 1.005:
        return niveau, f"${niveau:,.0f} — call wall principal"

    return spot * 1.05, f"${spot * 1.05:,.0f} — estimation spot+5% (pas de wall identifié)"


def _compute_niveau_bas(
    spot: float, flip: float, gmap: GravityMap, walls: OptionsWallsProfile,
    dormant_strikes: set | None = None,
    flip_use_in_signal: bool = True,
) -> tuple:
    """Niveau bas = ligne rouge / support principal sous le spot. Exclut les zones DORMANT."""
    ds = dormant_strikes or set()
    if flip is not None and flip < spot and flip_use_in_signal:
        dist_pct = (spot - flip) / spot * 100
        if dist_pct <= 10:
            return flip, f"${flip:,.0f} — ligne rouge (flip GEX, régime bascule si cassé)"

    explosives_below = [
        z for z in gmap.zones
        if z.zone_type == "EXPLOSIVE" and z.center < spot * 0.995 and not _is_dormant(z.center, ds)
    ]
    if explosives_below:
        exp = max(explosives_below, key=lambda z: z.center)
        return exp.center, f"${exp.center:,.0f} — zone explosive (cassure = accélération)"

    supports_below = [
        z for z in gmap.zones
        if z.zone_type == "SUPPORT" and z.center < spot * 0.995 and not _is_dormant(z.center, ds)
    ]
    if supports_below:
        sup = max(supports_below, key=lambda z: z.center)
        return sup.center, f"${sup.center:,.0f} — support principal"

    # F9.3 — Premier support = max(strike) parmi les murs strictement sous le spot
    # major_put_wall peut être None ou AU-DESSUS du spot → jamais retourner un niveau > spot
    if hasattr(walls, "walls") and walls.walls:
        puts_below = [
            w for w in walls.walls
            if w.strike < spot * 0.998
        ]
        if puts_below:
            best_put = max(puts_below, key=lambda w: w.strike)
            return best_put.strike, f"${best_put.strike:,.0f} — put wall principal"
    elif walls.major_put_wall and walls.major_put_wall < spot * 0.998:
        return walls.major_put_wall, f"${walls.major_put_wall:,.0f} — put wall principal"

    # F11.1 — fallback select_levels pour support
    lvl = select_levels(walls, spot)
    if lvl["support"] is not None:
        wall = lvl["support"]
        label = _wall_label(wall)
        if lvl["source"] == "post_expiration":
            label += " — structure post-expiration"
        return wall.strike, label

    return spot * 0.95, f"${spot * 0.95:,.0f} — estimation spot-5% (pas de support identifié)"


def _compute_gravity_target(
    spot: float, gravity_audit: Optional[GravityActivityAudit]
) -> tuple:
    """Retourne (target_price, zone_label, activity_tag) pour la zone gravity ACTIVE/ACTIONABLE la plus proche.
    Retourne (None, None, None) si toutes les zones sont DORMANT/STRUCTURAL."""
    if not gravity_audit:
        return None, None, None
    eligible = [
        z for z in gravity_audit.zones
        if z.activity_tag in ("ACTIVE", "ACTIONABLE") and z.use_in_signal
    ]
    if not eligible:
        return None, None, None
    best = min(eligible, key=lambda z: abs(z.strike - spot))
    label = f"${best.strike:,.0f} — {best.activity_label}"
    return best.strike, label, best.activity_tag


def _compute_gravity_targets_list(
    spot: float, gravity_audit: Optional[GravityActivityAudit]
) -> list[tuple[float, str]]:
    """Retourne toutes les zones gravity ACTIVE/ACTIONABLE triées par distance au spot."""
    if not gravity_audit:
        return []
    eligible = [
        z for z in gravity_audit.zones
        if z.activity_tag in ("ACTIVE", "ACTIONABLE") and z.use_in_signal
    ]
    eligible_sorted = sorted(eligible, key=lambda z: abs(z.strike - spot))
    return [(z.strike, f"${z.strike:,.0f} — {z.activity_label}") for z in eligible_sorted]


def _build_synthese(
    scenario: str,
    risque: str,
    niveau_haut: float,
    niveau_bas: float,
    range_mode: bool,
    asymmetric_side: str,
    mp_strike: float,
    flip: float,
    spot: float,
    flip_use_in_signal: bool = True,
    upside_ladder: list | None = None,
    downside_ladder: list | None = None,
) -> str:
    # F8.4 — Helper : trouve le premier obstacle entre spot et flip dans le ladder
    def _first_obstacle_up(ladder: list | None, flip_price: float | None) -> dict | None:
        """Premier niveau upside ENTRE le spot et le flip (exclu), avec OI si dispo."""
        if not ladder or not flip_price:
            return None
        for entry in ladder:  # déjà trié croissant
            p = entry["price"]
            if p < flip_price * 0.999:  # strictement sous le flip
                return entry
        return None

    def _first_obstacle_dn(ladder: list | None, flip_price: float | None) -> dict | None:
        """Premier niveau downside ENTRE le spot et le flip (exclu), avec OI si dispo."""
        if not ladder or not flip_price:
            return None
        for entry in ladder:  # déjà trié décroissant
            p = entry["price"]
            if p > flip_price * 1.001:  # strictement au-dessus du flip
                return entry
        return None

    def _fmt_obstacle(obs: dict) -> str:
        """Format : '$64,000 (4 047 BTC)' ou '$64,000' si pas d'OI."""
        p = obs["price"]
        oi = obs.get("oi")
        if oi and oi > 0:
            return f"${p:,.0f} ({oi:,.0f} BTC OI)"
        return f"${p:,.0f}"
    # ── Range mode ────────────────────────────────────────────────────────────
    if range_mode and asymmetric_side == "DOWN" and flip is not None:
        return (
            f"BTC reste compressé autour de l'expiration (${mp_strike:,.0f}), "
            f"mais le range est asymétrique : le vrai danger est sous ${flip:,.0f}."
        )
    if range_mode and asymmetric_side == "UP" and flip is not None:
        return (
            f"BTC reste compressé autour de l'expiration (${mp_strike:,.0f}), "
            f"mais un franchissement de ${flip:,.0f} pourrait déclencher une accélération haussière."
        )
    if range_mode:
        return (
            f"BTC en range vers ${mp_strike:,.0f}. "
            f"Surveillance : ${niveau_bas:,.0f} en bas, ${niveau_haut:,.0f} en haut."
        )

    # ── Non-range + flip + asymétrie UP → synthèse 4 parties ─────────────────
    # (flip ≥ spot : si BTC monte au-dessus du flip → accélération haussière)
    if flip is not None and asymmetric_side == "UP":
        dist_pct = abs(flip - spot) / spot * 100
        if dist_pct < 0.5:
            situation = f"BTC est au seuil GEX (${flip:,.0f})"
        elif dist_pct < 3:
            situation = f"BTC teste le seuil GEX (${flip:,.0f})"
        else:
            situation = f"BTC vise le seuil GEX (${flip:,.0f})"

        if "baissier" in scenario:
            biais = "biais options baissier"
            invalidation = f"recassure au-dessus de ${flip:,.0f} qui invalide la pression baissière"
            opp = f"si la pression baissière cède, accélération haussière violente vers ${niveau_haut:,.0f}"
        elif "haussier" in scenario:
            biais = "biais options haussier"
            invalidation = f"retour sous ${flip:,.0f}"
            opp = "si retour sous ce niveau, régime change — pression baissière s'active"
        else:
            # ZONE_DE_FLIP neutre côté UP : même logique que DOWN — phrase 2-côtés.
            _nxt_up = next((e["price"] for e in (upside_ladder or []) if e["price"] > flip * 1.001), None)
            _nxt_dn = niveau_bas if niveau_bas and niveau_bas < flip else None
            _above_target = f"${_nxt_up:,.0f}" if _nxt_up else "les résistances"
            _below_target = f"${_nxt_dn:,.0f}" if _nxt_dn else "les supports"
            return (
                f"{situation} — sans biais directionnel. "
                f"Au-dessus : franchise de ${flip:,.0f} active l'amplification haussière (accélération vers {_above_target}). "
                f"En-dessous : retour sous ${flip:,.0f} renforce la pression baissière vers {_below_target}."
            )

        return (
            f"{situation} — {biais}. "
            f"Invalidation : {invalidation}. "
            f"Scénario opposé : {opp}."
        )

    # ── Non-range + flip + asymétrie DOWN → synthèse 4 parties ───────────────
    # (flip < spot : si BTC recasse sous le flip → accélération baissière)
    if flip is not None and asymmetric_side == "DOWN":
        dist_pct = (spot - flip) / spot * 100
        if dist_pct < 3:
            situation = f"BTC tient juste au-dessus du seuil GEX (${flip:,.0f})"
        else:
            situation = f"BTC reste au-dessus du seuil GEX (${flip:,.0f})"

        if "haussier" in scenario:
            biais = "biais options haussier"
            invalidation = f"cassure sous ${flip:,.0f}"
            # Trouver le vrai niveau sous le flip, pas le flip lui-même
            _nxt_dn = next((e["price"] for e in (downside_ladder or []) if e["price"] < flip * 0.999), None)
            _opp_target = f"${_nxt_dn:,.0f}" if _nxt_dn else "les niveaux inférieurs"
            opp = f"si ce niveau cède, amplification baissière violente vers {_opp_target}"
        elif "baissier" in scenario:
            biais = "biais options baissier"
            invalidation = f"retour au-dessus de ${flip:,.0f}"
            opp = "si retour haussier, régime change — compression probable"
        else:
            # ZONE_DE_FLIP neutre : pas de biais directionnel exploitable.
            # La phrase 4-parties (situation — biais — invalidation — opp) est inadaptée :
            # il n'y a pas de "biais" ni d'"invalidation" au sens directionnel.
            # On produit une phrase 2-côtés : ce qui se passe au-dessus vs en-dessous.
            _nxt_dn = next((e["price"] for e in (downside_ladder or []) if e["price"] < flip * 0.999), None)
            _nxt_up = niveau_haut if niveau_haut and niveau_haut > flip else None
            _below_target = f"${_nxt_dn:,.0f}" if _nxt_dn else "les niveaux inférieurs"
            _above_target = f"${_nxt_up:,.0f}" if _nxt_up else "les résistances"
            return (
                f"{situation} — sans biais directionnel. "
                f"Au-dessus : clôture sur ${flip:,.0f} active le régime stabilisant (compression vers {_above_target}). "
                f"En-dessous : cassure sous ${flip:,.0f} active l'amplification baissière (accélération vers {_below_target})."
            )

        return (
            f"{situation} — {biais}. "
            f"Invalidation : {invalidation}. "
            f"Scénario opposé : {opp}."
        )

    # ── Flip proche mais dormant/structurel — note cautious ──────────────────
    if flip is not None and not flip_use_in_signal:
        dist_pct = abs(flip - spot) / spot * 100
        if dist_pct <= 8:
            return (
                f"{scenario}. "
                f"Seuil GEX ${flip:,.0f} proche ({dist_pct:.1f}%) mais "
                f"structurel/dormant — non confirmé par l'activité récente."
            )

    # ── Fallback : pas de flip ou asymétrie neutre ────────────────────────────
    return f"{scenario}. {risque}."


def _build_banner(
    range_mode: bool, asymmetric_side: str, flip: float, spot: float, regime: str,
    use_in_signal: bool = True,
) -> str:
    if range_mode and asymmetric_side == "DOWN" and flip is not None:
        return f"BTC reste compressé, mais le risque est asymétrique : ${flip:,.0f} est la ligne rouge."
    if range_mode and asymmetric_side == "UP" and flip is not None:
        return f"BTC reste compressé — une cassure de ${flip:,.0f} pourrait tout changer."
    if range_mode:
        return "BTC en range compressé — aucun catalyseur directionnel pour l'instant."
    if regime == "AMPLIFICATEUR":
        if use_in_signal:
            return "Régime amplificateur actif — le prochain mouvement sera violent."
        return "GEX brut amplificateur — signal de fond, à confirmer par volume et activité récente."
    return "Signaux mixtes — attends une confirmation de direction avant d'entrer."


def _build_flip_activity_context(flip_audit: Optional[FlipActivityAudit]) -> Optional[str]:
    """Phrase humaine expliquant la qualité du flip level."""
    if flip_audit is None:
        return None
    tag = flip_audit.flip_activity_tag
    if tag == "ACTIONABLE":
        return (
            f"🔥 Flip actionnable ({flip_audit.window_actionable_pct:.0f}% ATM + DTE court) — "
            "déclencheur confirmé par l'activité récente."
        )
    if tag == "ACTIVE":
        return (
            f"⚡ Flip actif ({flip_audit.window_active_pct:.0f}% actif) — "
            "niveau à surveiller, activité récente présente."
        )
    if tag == "STRUCTURAL":
        return (
            f"🪨 Flip structurel ({flip_audit.window_structural_pct:.0f}% structural) — "
            "niveau de fond, peu de flux récent. Non confirmé comme déclencheur."
        )
    return (
        f"💀 Flip dormant ({flip_audit.window_dormant_pct:.0f}% dormant) — "
        "non confirmé par l'activité récente. Niveau structurel de référence uniquement."
    )


def _build_gex_activity(
    audit: Optional[GEXActivityAudit],
    gex: GEXProfile,
) -> tuple:
    """Retourne (label, context, use_in_signal) pour la qualité du signal GEX."""
    if audit is None:
        if gex.regime == "AMPLIFICATEUR":
            return "⚡ GEX actif", "GEX amplificateur — qualité non auditée.", True
        return "🪨 GEX structurel", "GEX sans audit de qualité disponible.", True

    profile = audit.overall_profile  # "DORMANT" | "STRUCTURAL" | "ACTIVE" | "ACTIONABLE"
    dormant_pct = audit.dormant.gex_pct
    actionable_pct = audit.actionable.gex_pct

    if profile == "ACTIONABLE":
        # Point 1 — GEX mécaniquement actif ≠ signal directionnel validé
        label = "🔥 GEX mécaniquement actif"
        context = (
            f"GEX actionnable ({actionable_pct:.0f}% ATM + DTE court) — "
            "influence dealer probable sur BTC, mais direction non validée seule. "
            "Signal exploitable uniquement en confluence avec DEX + MOPI + Gravity."
        )
    elif profile == "ACTIVE":
        label = "⚡ GEX actif"
        context = (
            "GEX actif — positions en mouvement, impact BTC probable à court terme."
        )
    elif profile == "STRUCTURAL":
        label = "🪨 GEX structurel"
        context = (
            f"GEX surtout structurel ({dormant_pct:.0f}% dormant) — "
            "compression de fond, peu de signal court terme."
        )
    else:  # DORMANT
        label = "💀 GEX dormant"
        context = (
            f"GEX majoritairement inactif ({dormant_pct:.0f}% dormant) — "
            "gonflement artificiel, pas de signal exploitable."
        )

    return label, context, audit.use_in_signal


def _build_dex_activity(
    dex_levels: Optional[DEXLevels],
) -> tuple:
    """Retourne (label, context, use_in_signal) pour la qualité du signal DEX.

    DORMANT    → use_in_signal=False : stock inactif, pas de flux exploitable
    STRUCTURAL → use_in_signal=False : pression de fond, confirmer par volume récent
    ACTIVE     → use_in_signal=True  : flux delta récent, impact court terme probable
    ACTIONABLE → use_in_signal=True  : flux + ATM + DTE court — signal exploitable maintenant
    """
    if dex_levels is None:
        return "⚡ DEX actif", "DEX sans audit de qualité disponible.", True

    profile = dex_levels.dex_profile  # "DORMANT" | "STRUCTURAL" | "ACTIVE" | "ACTIONABLE"
    active_pct = dex_levels.dex_active_pct
    actionable_pct = dex_levels.dex_actionable_pct

    if profile == "ACTIONABLE":
        label = "🔥 DEX actionnable"
        context = (
            f"DEX actionnable ({actionable_pct:.0f}% ATM + DTE court) — "
            "flux delta exploitable maintenant."
        )
        use_in_signal = True
    elif profile == "ACTIVE":
        label = "⚡ DEX actif"
        context = "DEX actif — flux delta récent, impact BTC probable à court terme."
        use_in_signal = True
    elif profile == "STRUCTURAL":
        label = "🪨 DEX structurel"
        context = (
            f"DEX structurel de fond ({active_pct:.0f}% actif) — "
            "pression delta à confirmer par volume récent. "
            "Pas de signal court terme exploitable."
        )
        use_in_signal = False
    else:  # DORMANT
        label = "💀 DEX dormant"
        context = (
            "DEX majoritairement inactif — "
            "stock delta sans flux exploitable. "
            "Pas de signal court terme exploitable."
        )
        use_in_signal = False

    return label, context, use_in_signal


# ═══════════════════════════════════════════════════════════════════════════════
# NARRATIVE HORIZON — pondération par fenêtre temporelle (V1 Hypothèse)
# ═══════════════════════════════════════════════════════════════════════════════

_GEX_NEUTRAL_THRESHOLD_USD = 5_000_000  # |GEX| < $5M → neutre, sort du calcul

HORIZON_HOURS = {"4h": 4, "24h": 24, "72h": 72}

# Poids nominaux par horizon (avant veto)
_HORIZON_WEIGHTS: dict = {
    "4h":  {"DEX": 1.0, "GEX": 0.5, "WALLS": 0.3, "MAX_PAIN": 0.1, "GRAVITY": 0.1},
    "24h": {"GEX": 1.0, "DEX": 0.5, "WALLS": 0.3, "GRAVITY": 0.3, "MAX_PAIN": 0.1},
    "72h": {"MAX_PAIN": 1.0, "WALLS": 0.8, "GRAVITY": 0.8, "GEX": 0.2, "DEX": 0.2},
}

# DTE max (jours) pour que Max Pain reste "principal" sur cet horizon
_HORIZON_DTE_THRESHOLD: dict = {"4h": 0, "24h": 1, "72h": 3}

_HYPOTHESIS_DISCLAIMER = (
    "Cette hiérarchie est une hypothèse V1. "
    "Elle doit être validée par le backtest EV dès que l'historique est suffisant."
)


@dataclass
class HorizonNarrative:
    horizon: str
    force_dominante: str
    scenario: str
    niveau_haut: float
    niveau_haut_label: str
    niveau_bas: float
    niveau_bas_label: str
    forces_haussieres: list
    forces_baissieres: list
    forces_neutres: list
    confidence: int
    hypothesis_version: str
    hypothesis_disclaimer: str
    vetoed_forces: list
    flip_activity_tag: Optional[str] = None
    flip_use_in_signal: Optional[bool] = None
    flip_activity_context: Optional[str] = None


def resolve_narrative_horizon(
    mopi: MOPIScore,
    gex: GEXProfile,
    dp: DealerPressure,
    gmap: GravityMap,
    walls: OptionsWallsProfile,
    sq: SqueezeScore,
    spot: float,
    horizon: str,
    audit: Optional[GEXActivityAudit] = None,
    dex_levels: Optional[DEXLevels] = None,
    gravity_audit: Optional[GravityActivityAudit] = None,
    flip_audit: Optional[FlipActivityAudit] = None,
    calibration_status: str = "available",
    calibration_reason_code: str = "calibration_available",
) -> HorizonNarrative:
    """Narrative pondérée par horizon temporel (4h / 24h / 72h).

    Hiérarchie V1 (hypothèse à backtester) :
      4h  → DEX principal, GEX = amplitude
      24h → GEX principal, DEX = confirmation
      72h → Max Pain principal si DTE ≤ 3j, sinon Walls + Gravity
    """
    if horizon not in HORIZON_HOURS:
        raise ValueError(f"horizon doit être parmi {list(HORIZON_HOURS.keys())}, reçu '{horizon}'")

    weights = _HORIZON_WEIGHTS[horizon]
    dte_threshold = _HORIZON_DTE_THRESHOLD[horizon]

    # ── Qualité DEX / GEX / Flip ─────────────────────────────────────────────
    _, _, dex_use_in_signal = _build_dex_activity(dex_levels)
    _, _, gex_use_in_signal = _build_gex_activity(audit, gex)
    flip_use_in_signal_h = flip_audit.flip_use_in_signal if flip_audit is not None else True

    # ── Gravity : au moins une zone active/actionnable ? ─────────────────────
    gravity_has_active = bool(
        gravity_audit and any(z.use_in_signal for z in gravity_audit.zones)
    )

    # ── Walls : au moins un mur non-dormant ? ─────────────────────────────────
    walls_has_active = any(w.tag != TAG_DORMANT for w in walls.walls) if walls.walls else False

    # ── Max Pain — DTE et strike ──────────────────────────────────────────────
    mp_near = gex.max_pain_profile.near if gex.max_pain_profile else None
    mp_dte = mp_near.dte if mp_near else 99
    mp_strike = mp_near.strike if mp_near else round(gex.max_pain, 0)
    mp_expiry = mp_near.expiry if mp_near else "?"
    max_pain_is_context = mp_dte > dte_threshold  # True → contexte seulement

    # ── Règles de veto ────────────────────────────────────────────────────────
    gex_vetoed = (
        gex.regime == "NEUTRE"
        or abs(gex.total_gex) < _GEX_NEUTRAL_THRESHOLD_USD
        or not gex_use_in_signal
    )
    dex_vetoed = not dex_use_in_signal
    gravity_vetoed = not gravity_has_active
    walls_vetoed = not walls_has_active

    vetoed_forces: list = []
    if gex_vetoed:
        if gex.regime == "NEUTRE" or abs(gex.total_gex) < _GEX_NEUTRAL_THRESHOLD_USD:
            raison = f"GEX neutre (|GEX|=${abs(gex.total_gex)/1e6:.1f}M < $5M ou régime NEUTRE)"
        else:
            raison = "GEX dormant (use_in_signal=False)"
        vetoed_forces.append({"force": "GEX", "raison": raison})
    if dex_vetoed:
        profile = dex_levels.dex_profile if dex_levels else "N/A"
        vetoed_forces.append({
            "force": "DEX",
            "raison": f"DEX {profile} (use_in_signal=False) — stock delta inactif",
        })
    if gravity_vetoed:
        vetoed_forces.append({"force": "GRAVITY", "raison": "Toutes zones Gravity dormantes"})
    if walls_vetoed:
        vetoed_forces.append({"force": "WALLS", "raison": "Tous murs d'options dormants"})
    if max_pain_is_context and horizon != "72h":
        vetoed_forces.append({
            "force": "MAX_PAIN",
            "raison": f"DTE {mp_dte}j > horizon {horizon} — contexte seulement",
        })

    # ── Poids effectifs après veto ────────────────────────────────────────────
    def _eff(name: str, vetoed: bool) -> float:
        if vetoed:
            return 0.0
        w = weights.get(name, 0.0)
        # Max Pain DTE > threshold → poids réduit à 0.1 maximum
        if name == "MAX_PAIN" and max_pain_is_context:
            return min(w, 0.1)
        return w

    eff: dict = {
        "GEX":      _eff("GEX", gex_vetoed),
        "DEX":      _eff("DEX", dex_vetoed),
        "WALLS":    _eff("WALLS", walls_vetoed),
        "GRAVITY":  _eff("GRAVITY", gravity_vetoed),
        "MAX_PAIN": _eff("MAX_PAIN", max_pain_is_context and horizon != "72h"),
    }
    # 72h spécial : si DTE > threshold, Max Pain perd sa position dominante
    # et Walls + Gravity reprennent leur poids plein
    if horizon == "72h" and max_pain_is_context:
        eff["MAX_PAIN"] = 0.1
        if not walls_vetoed:
            eff["WALLS"] = 0.8
        if not gravity_vetoed:
            eff["GRAVITY"] = 0.8

    # ── Directions ───────────────────────────────────────────────────────────
    # Calculées AVANT force_dominante : une force neutre ne peut jamais être
    # dominante, peu importe son poids nominal.
    dirs = {
        "GEX":      _dir_gex(gex, mopi, gex_vetoed),
        "DEX":      _dir_dex(dp, dex_vetoed),
        "WALLS":    _dir_walls(walls, spot, walls_vetoed),
        "GRAVITY":  _dir_gravity(gmap, spot, gravity_vetoed),
        "MAX_PAIN": _dir_max_pain(mp_strike, spot, max_pain_is_context and horizon != "72h"),
    }

    # ── Force dominante — parmi les forces directionnelles uniquement ─────────
    # Règle : une force neutre (dirs == NEUTRE) ou veto (eff == 0) ne peut
    # jamais être force_dominante. Si aucune force directionnelle → "AUCUNE".
    directional_eff = {
        name: w for name, w in eff.items()
        if w > 0.0 and dirs[name] in ("HAUSSIER", "BAISSIER")
    }
    force_dominante = (
        max(directional_eff, key=lambda k: directional_eff[k])
        if directional_eff else "AUCUNE"
    )

    # ── Détails humains ───────────────────────────────────────────────────────
    details = {
        "GEX":      _det_gex(gex, mopi, gex_vetoed, audit, flip_use_in_signal=flip_use_in_signal_h),
        "DEX":      _det_dex(dp, dex_levels, dex_vetoed),
        "WALLS":    _det_walls(walls, spot, walls_vetoed),
        "GRAVITY":  _det_gravity(gmap, spot, gravity_vetoed, gravity_audit),
        "MAX_PAIN": _det_max_pain(mp_strike, mp_expiry, mp_dte, spot, max_pain_is_context, horizon),
    }

    # ── Classification forces haussières / baissieres / neutres ───────────────
    forces_haussieres: list = []
    forces_baissieres: list = []
    forces_neutres: list = []

    for name in ("GEX", "DEX", "WALLS", "GRAVITY", "MAX_PAIN"):
        w = eff[name]
        if w == 0.0:
            continue  # veto → dans vetoed_forces
        entry = {"name": name, "weight": round(w, 2), "detail": details[name]}
        d = dirs[name]
        if d == "HAUSSIER":
            forces_haussieres.append(entry)
        elif d == "BAISSIER":
            forces_baissieres.append(entry)
        else:
            forces_neutres.append(entry)

    for lst in (forces_haussieres, forces_baissieres, forces_neutres):
        lst.sort(key=lambda x: x["weight"], reverse=True)

    # ── Niveaux haut / bas ────────────────────────────────────────────────────
    dormant_strikes: set = set()
    if gravity_audit:
        dormant_strikes = {z.strike for z in gravity_audit.zones if not z.use_in_signal}
    niveau_haut, niveau_haut_label = _compute_niveau_haut(spot, gmap, walls, dormant_strikes)
    niveau_bas, niveau_bas_label = _compute_niveau_bas(
        spot, gex.flip_level, gmap, walls, dormant_strikes,
        flip_use_in_signal=flip_use_in_signal_h,
    )

    # ── Scénario narratif ────────────────────────────────────────────────────
    scenario = _build_horizon_scenario(
        horizon=horizon,
        force_dominante=force_dominante,
        forces_haussieres=forces_haussieres,
        forces_baissieres=forces_baissieres,
        spot=spot,
        niveau_haut=niveau_haut,
        niveau_bas=niveau_bas,
        gex=gex,
        dp=dp,
        mp_strike=mp_strike,
        mp_expiry=mp_expiry,
        mp_dte=mp_dte,
        max_pain_is_context=max_pain_is_context,
        mopi=mopi,
    )

    # ── Wording GEX horizon — adoucissement + caveat si calibration dégradée ──
    # gex_use_in_signal=False → GEX déjà exclu → pas de transformation
    caveat_h = _gex_calibration_caveat(calibration_status, calibration_reason_code)
    if gex_use_in_signal and calibration_status != "available":
        scenario = _apply_gex_confidence_wording(scenario, calibration_status)
        scenario = _append_once(scenario, caveat_h)

    # ── Confidence 0-100 ─────────────────────────────────────────────────────
    confidence = _compute_horizon_confidence(
        force_dominante=force_dominante,
        forces_haussieres=forces_haussieres,
        forces_baissieres=forces_baissieres,
        forces_neutres=forces_neutres,
        vetoed_forces=vetoed_forces,
        audit=audit,
        dex_levels=dex_levels,
        gex_use_in_signal=gex_use_in_signal,
        dex_use_in_signal=dex_use_in_signal,
    )

    flip_ctx_h = _build_flip_activity_context(flip_audit)
    return HorizonNarrative(
        horizon=horizon,
        force_dominante=force_dominante,
        scenario=scenario,
        niveau_haut=niveau_haut,
        niveau_haut_label=niveau_haut_label,
        niveau_bas=niveau_bas,
        niveau_bas_label=niveau_bas_label,
        forces_haussieres=forces_haussieres,
        forces_baissieres=forces_baissieres,
        forces_neutres=forces_neutres,
        confidence=confidence,
        hypothesis_version="V1",
        hypothesis_disclaimer=_HYPOTHESIS_DISCLAIMER,
        vetoed_forces=vetoed_forces,
        flip_activity_tag=flip_audit.flip_activity_tag if flip_audit else None,
        flip_use_in_signal=flip_use_in_signal_h,
        flip_activity_context=flip_ctx_h,
    )


# ── Direction helpers ─────────────────────────────────────────────────────────

def _dir_gex(gex: GEXProfile, mopi: "MOPIScore", vetoed: bool) -> str:
    if vetoed or gex.regime == "STABILISANT":
        return "NEUTRE"
    # Direction from GEX regime and flip level sign (no MOPI dependency)
    if gex.regime == "AMPLIFICATEUR":
        return "INCONNU"  # amplificateur sans biais directionnel propre
    return "NEUTRE"


def _dir_dex(dp: DealerPressure, vetoed: bool) -> str:
    if vetoed:
        return "NEUTRE"
    if dp.direction == "BULLISH_FLOWS":
        return "HAUSSIER"
    elif dp.direction == "BEARISH_FLOWS":
        return "BAISSIER"
    return "NEUTRE"


def _dir_max_pain(mp_strike: float, spot: float, vetoed: bool) -> str:
    if vetoed:
        return "NEUTRE"
    if mp_strike > spot * 1.005:
        return "HAUSSIER"
    elif mp_strike < spot * 0.995:
        return "BAISSIER"
    return "NEUTRE"


def _dir_gravity(gmap: GravityMap, spot: float, vetoed: bool) -> str:
    if vetoed:
        return "NEUTRE"
    above = [z for z in gmap.zones if z.zone_type == "MAGNETIC" and z.center > spot]
    below = [z for z in gmap.zones if z.zone_type == "MAGNETIC" and z.center < spot]
    if above and not below:
        return "HAUSSIER"
    if below and not above:
        return "BAISSIER"
    return "NEUTRE"


def _dir_walls(walls: OptionsWallsProfile, spot: float, vetoed: bool) -> str:
    if vetoed:
        return "NEUTRE"
    call_ok = walls.major_call_wall > spot * 1.005
    put_ok = walls.major_put_wall < spot * 0.995 and walls.major_put_wall > 0
    if not call_ok and not put_ok:
        return "NEUTRE"
    call_dist = abs(walls.major_call_wall - spot) / spot if call_ok else 1.0
    put_dist = abs(walls.major_put_wall - spot) / spot if put_ok else 1.0
    # Call wall plus proche → résistance forte → bearish
    # Put wall plus proche → support fort → bullish
    if call_dist < put_dist * 0.7:
        return "BAISSIER"
    if put_dist < call_dist * 0.7:
        return "HAUSSIER"
    return "NEUTRE"


# ── Detail helpers ────────────────────────────────────────────────────────────

def _det_gex(
    gex: GEXProfile, mopi: "MOPIScore", vetoed: bool,
    audit: Optional[GEXActivityAudit],
    flip_use_in_signal: bool = True,
) -> str:
    if vetoed:
        if gex.regime == "NEUTRE" or abs(gex.total_gex) < _GEX_NEUTRAL_THRESHOLD_USD:
            return f"GEX neutre (${abs(gex.total_gex)/1e6:.1f}M) — exclu du calcul"
        return "GEX dormant (use_in_signal=False) — exclu"
    flip = gex.flip_level
    flip_suffix = f" Flip ${flip:,.0f}" if flip is not None and flip_use_in_signal else ""
    if gex.regime == "STABILISANT":
        return f"Régime STABILISANT — dealers hedgent, BTC compressé.{flip_suffix}"
    return f"Régime AMPLIFICATEUR — breakout amplifié.{flip_suffix}"


def _det_dex(dp: DealerPressure, dex_levels: Optional[DEXLevels], vetoed: bool) -> str:
    if vetoed:
        profile = dex_levels.dex_profile if dex_levels else "N/A"
        return f"DEX {profile} — stock delta passif, pas de flux exploitable"
    side = "soutien" if dp.direction == "BULLISH_FLOWS" else (
        "résistance" if dp.direction == "BEARISH_FLOWS" else "neutre"
    )
    return f"Dealers : {side} — {dp.net_delta:+,.0f} BTC net delta"


def _det_walls(walls: OptionsWallsProfile, spot: float, vetoed: bool) -> str:
    if vetoed:
        return "Murs dormants — OI sans flux récent, aucun impact attendu"
    call_dist = abs(walls.major_call_wall - spot) / spot * 100 if walls.major_call_wall > spot else 0.0
    put_dist = abs(walls.major_put_wall - spot) / spot * 100 if 0 < walls.major_put_wall < spot else 0.0
    return (
        f"Call wall ${walls.major_call_wall:,.0f} (+{call_dist:.1f}%) | "
        f"Put wall ${walls.major_put_wall:,.0f} (-{put_dist:.1f}%)"
    )


def _det_gravity(
    gmap: GravityMap, spot: float, vetoed: bool,
    gravity_audit: Optional[GravityActivityAudit],
) -> str:
    if vetoed:
        return "Toutes zones Gravity dormantes — aucune cible exploitable"
    target, _, tag = _compute_gravity_target(spot, gravity_audit) if gravity_audit else (None, None, None)
    if target:
        dist = (target - spot) / spot * 100
        return f"Zone gravity {tag} ${target:,.0f} ({dist:+.1f}%)"
    if gmap.strongest_magnet:
        dist = (gmap.strongest_magnet - spot) / spot * 100
        return f"Aimant principal ${gmap.strongest_magnet:,.0f} ({dist:+.1f}%)"
    return "Gravity active, cible non précisée"


def _det_max_pain(
    mp_strike: float, mp_expiry: str, mp_dte: int,
    spot: float, is_context: bool, horizon: str,
) -> str:
    dist = (mp_strike - spot) / spot * 100
    suffix = f" [contexte — DTE {mp_dte}j > horizon {horizon}]" if is_context else ""
    return f"Max Pain ${mp_strike:,.0f} ({mp_expiry}, J-{mp_dte}){suffix} — attraction {dist:+.1f}%"


# ── Scenario builder ─────────────────────────────────────────────────────────

def _build_horizon_scenario(
    horizon: str,
    force_dominante: str,
    forces_haussieres: list,
    forces_baissieres: list,
    spot: float,
    niveau_haut: float,
    niveau_bas: float,
    gex: GEXProfile,
    dp: DealerPressure,
    mp_strike: float,
    mp_expiry: str,
    mp_dte: int,
    max_pain_is_context: bool,
    mopi: MOPIScore,
) -> str:
    if force_dominante == "AUCUNE":
        return (
            f"{horizon} — Aucun signal dominant (toutes forces veto ou neutres). "
            "Attendre un catalyseur avant d'entrer."
        )

    hausse_names = " + ".join(f["name"] for f in forces_haussieres) or "aucune"
    baisse_names = " + ".join(f["name"] for f in forces_baissieres) or "aucune"

    if horizon == "4h":
        if force_dominante == "DEX":
            if dp.direction == "BULLISH_FLOWS":
                return (
                    f"4h — Soutien dealers dominant : ${spot:,.0f} → cible ${niveau_haut:,.0f}. "
                    f"DEX donne la direction. Ligne rouge : ${niveau_bas:,.0f}."
                )
            if dp.direction == "BEARISH_FLOWS":
                return (
                    f"4h — Résistance dealers dominant : pression vers ${niveau_bas:,.0f}. "
                    f"DEX bloque le rebond. Ligne rouge : ${niveau_bas:,.0f}."
                )
            return (
                f"4h — DEX neutre. Range ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
            )
        if force_dominante == "GEX":
            return (
                f"4h — GEX {gex.regime} confirme l'amplitude. "
                f"Support ${niveau_bas:,.0f}, résistance ${niveau_haut:,.0f}."
            )
        return (
            f"4h — {force_dominante} dominant. "
            f"Haussier : {hausse_names}. Baissier : {baisse_names}. "
            f"Range ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
        )

    if horizon == "24h":
        if force_dominante == "GEX":
            bias = "haussier" if dp.direction == "BULLISH_FLOWS" else ("baissier" if dp.direction == "BEARISH_FLOWS" else "neutre")
            if gex.regime == "STABILISANT":
                return (
                    f"24h — GEX STABILISANT : BTC compressé, dealers hedgent des deux côtés. "
                    f"Attraction Max Pain ${mp_strike:,.0f}. "
                    f"Range ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
                )
            dex_conf = "confirmé par DEX" if dp.direction != "NEUTRAL" else "DEX en attente"
            return (
                f"24h — GEX AMPLIFICATEUR {bias} dominant : tout breakout amplifié. "
                f"{dex_conf.capitalize()}. "
                f"Niveaux clés : ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
            )
        if force_dominante == "DEX":
            side = "soutien" if dp.direction == "BULLISH_FLOWS" else "résistance"
            return (
                f"24h — GEX peu actif, DEX prend le relais ({side} dealers). "
                f"Niveaux : ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
            )
        return (
            f"24h — {force_dominante} dominant. "
            f"Haussier : {hausse_names}. Baissier : {baisse_names}. "
            f"Range ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
        )

    if horizon == "72h":
        if force_dominante == "MAX_PAIN":
            return (
                f"72h — Expiration {mp_expiry} (J-{mp_dte}) : "
                f"BTC attiré vers Max Pain ${mp_strike:,.0f}. "
                f"Force principale sur cette fenêtre. "
                f"Support ${niveau_bas:,.0f}, résistance ${niveau_haut:,.0f}."
            )
        if force_dominante in ("WALLS", "GRAVITY"):
            return (
                f"72h — Pas d'expiry proche (DTE {mp_dte}j), structures de fond dominent. "
                f"Murs d'options + zones gravity guident la direction. "
                f"Niveaux clés : ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
            )
        return (
            f"72h — {force_dominante} dominant. "
            f"Haussier : {hausse_names}. Baissier : {baisse_names}. "
            f"Range ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
        )

    # Fallback
    return (
        f"{horizon} — {force_dominante} dominant. "
        f"Haussier : {hausse_names}. Baissier : {baisse_names}. "
        f"Range ${niveau_bas:,.0f} — ${niveau_haut:,.0f}."
    )


# ── Confidence calculator ────────────────────────────────────────────────────

def _compute_horizon_confidence(
    force_dominante: str,
    forces_haussieres: list,
    forces_baissieres: list,
    forces_neutres: list,
    vetoed_forces: list,
    audit: Optional[GEXActivityAudit],
    dex_levels: Optional[DEXLevels],
    gex_use_in_signal: bool,
    dex_use_in_signal: bool,
) -> int:
    if force_dominante == "AUCUNE":
        return 10

    base = 40

    # Consensus directionnel
    hausse_w = sum(f["weight"] for f in forces_haussieres)
    baisse_w = sum(f["weight"] for f in forces_baissieres)
    total_w = hausse_w + baisse_w + sum(f["weight"] for f in forces_neutres)
    if total_w > 0:
        dominant_w = max(hausse_w, baisse_w)
        base += int(dominant_w / total_w * 30)  # +0 à +30

    # Qualité de la force dominante
    if force_dominante == "GEX" and audit:
        bonuses = {"ACTIONABLE": 15, "ACTIVE": 10, "STRUCTURAL": 5, "DORMANT": -5}
        base += bonuses.get(audit.overall_profile, 0)
    elif force_dominante == "DEX" and dex_levels:
        bonuses = {"ACTIONABLE": 15, "ACTIVE": 10, "STRUCTURAL": -5, "DORMANT": -10}
        base += bonuses.get(dex_levels.dex_profile, 0)

    # Pénalité par force veto
    base -= len(vetoed_forces) * 3

    return max(10, min(100, base))
