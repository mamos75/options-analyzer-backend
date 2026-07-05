"""
Field Diagnostic — Gap #6 + Gap #9 (gex_calibration).

Chaque champ critique du dashboard doit avoir un état explicite :
  available   → valeur fiable, affichable
  degraded    → valeur estimée (fallback global, données partielles)
  unavailable → aucune valeur fiable, raison expliquée
  stale       → données périmées (feed coupé)

Reason codes standardisés :
  no_near_gamma_sign_cross   — aucun crossing near dans ±10% du spot
  insufficient_near_strikes  — pas assez de strikes near-term
  all_gamma_negative         — GEX entièrement négatif, pas de crossing possible
  all_gamma_positive         — GEX entièrement positif, pas de crossing possible
  quality_gate_dormant       — quality gate bloque le champ
  field_mapping_missing      — champ backend manquant
  calculation_error          — erreur de calcul détectée
  no_magnetic_zone           — aucune zone MAGNETIC dans Gravity
  no_explosive_zone          — aucune zone EXPLOSIVE dans Gravity
  no_calls_above_spot        — aucun call wall au-dessus du spot
  no_puts_below_spot         — aucun put wall en-dessous du spot
  crossing_near              — (available) crossing trouvé dans ±10%
  crossing_global            — (degraded) crossing global, hors fenêtre near
  magnetic_zone_found        — (available) zone magnétique valide
  explosive_zone_found       — (available) zone explosive valide
  call_wall_found            — (available) call wall valide
  put_wall_found             — (available) put wall valide
  always_available           — champ toujours calculable (MOPI, squeeze, DEX)
  calibration_available      — (available) cap GEX dynamique, fraîche et cohérente
  calibration_inconsistent   — (degraded) alertes saturation/neutralisation détectées
  calibration_stale          — (stale) historique insuffisant (<48 points)
  calibration_missing        — (unavailable) aucun historique — bootstrap statique
  calibration_error          — (unavailable) erreur de calcul de calibration
"""

from dataclasses import dataclass, field as dc_field
from typing import Optional, Dict, Any


@dataclass
class FieldDiag:
    status: str                         # available | degraded | unavailable | stale
    reason_code: str
    value: Optional[float]              # None si unavailable
    debug: Dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "value": self.value,
            "debug": self.debug,
        }


# Mapping reason_code → statut canonique
_REASON_TO_STATUS = {
    "crossing_near":              "available",
    "crossing_global":            "degraded",
    "no_near_gamma_sign_cross":   "unavailable",
    "insufficient_near_strikes":  "unavailable",
    "all_gamma_negative":         "unavailable",
    "all_gamma_positive":         "unavailable",
    "quality_gate_dormant":       "unavailable",
    "field_mapping_missing":      "unavailable",
    "calculation_error":          "unavailable",
    "no_magnetic_zone":           "unavailable",
    "no_explosive_zone":          "unavailable",
    "no_calls_above_spot":        "unavailable",
    "no_puts_below_spot":         "unavailable",
    "magnetic_zone_found":        "available",
    "explosive_zone_found":       "available",
    "call_wall_found":            "available",
    "put_wall_found":             "available",
    "always_available":           "available",
    "calibration_available":      "available",
    "calibration_inconsistent":   "degraded",
    "calibration_stale":          "stale",
    "calibration_missing":        "unavailable",
    "calibration_error":          "unavailable",
}

# Seuils calibration (cohérents avec main.py)
_GEX_CAL_MIN_POINTS = 48   # 1 jour à 30min = 2 points/h × 24h
_GEX_SAT_THRESHOLD = 0.30  # > 30% → cap trop bas, signal saturé
_GEX_NEU_THRESHOLD = 0.70  # > 70% → cap trop élevé, signal collé à 50


def _status_for(reason_code: str) -> str:
    return _REASON_TO_STATUS.get(reason_code, "unavailable")


def diag_flip_level(gex_profile, snapshot) -> FieldDiag:
    """Diagnostic du flip level (Seuil de régime).

    Utilise flip_level_reason déjà calculé dans GEXProfile.
    Enrichit avec debug near-window pour observabilité.
    """
    reason = gex_profile.flip_level_reason
    status = _status_for(reason)
    value = gex_profile.flip_level

    spot = gex_profile.btc_price
    near_strikes = {
        k: v for k, v in gex_profile.gex_by_strike.items()
        if spot * 0.85 <= k <= spot * 1.15
    }

    debug: Dict[str, Any] = {
        "horizon": "near",
        "strikes_count": len(near_strikes),
        "quality_state": _infer_quality_state(gex_profile),
    }
    if near_strikes:
        debug["min_gex"] = round(min(near_strikes.values()), 0)
        debug["max_gex"] = round(max(near_strikes.values()), 0)
        nearest = min(near_strikes.keys(), key=lambda k: abs(k - spot))
        debug["nearest_strike"] = nearest
    if value:
        debug["flip_level"] = value
        debug["distance_pct"] = round(abs(value - spot) / spot * 100, 2)

    return FieldDiag(status=status, reason_code=reason, value=value, debug=debug)


def diag_gravity_magnet(gmap) -> FieldDiag:
    """Diagnostic de strongest_magnet (Zone d'Attraction)."""
    magnet = getattr(gmap, "strongest_magnet", 0.0) or 0.0
    spot = gmap.btc_price
    zones = getattr(gmap, "zones", [])

    if magnet and abs(magnet - spot) / spot > 0.005:
        dist_pct = abs(magnet - spot) / spot * 100
        return FieldDiag(
            status="available",
            reason_code="magnetic_zone_found",
            value=magnet,
            debug={
                "distance_pct": round(dist_pct, 2),
                "total_zones": len(zones),
                "magnetic_zones": sum(1 for z in zones if z.zone_type == "MAGNETIC"),
            },
        )

    magnetic_zones = [z for z in zones if z.zone_type == "MAGNETIC"]
    return FieldDiag(
        status="unavailable",
        reason_code="no_magnetic_zone",
        value=None,
        debug={
            "total_zones": len(zones),
            "magnetic_zones": len(magnetic_zones),
            "zone_types": sorted({z.zone_type for z in zones}),
        },
    )


def diag_gravity_explosive(gmap) -> FieldDiag:
    """Diagnostic de next_explosive (Zone Explosive)."""
    explosive = getattr(gmap, "next_explosive", 0.0) or 0.0
    spot = gmap.btc_price
    zones = getattr(gmap, "zones", [])

    if explosive and abs(explosive - spot) / spot > 0.005:
        dist_pct = abs(explosive - spot) / spot * 100
        return FieldDiag(
            status="available",
            reason_code="explosive_zone_found",
            value=explosive,
            debug={
                "distance_pct": round(dist_pct, 2),
                "total_zones": len(zones),
                "explosive_zones": sum(1 for z in zones if z.zone_type == "EXPLOSIVE"),
            },
        )

    explosive_zones = [z for z in zones if z.zone_type == "EXPLOSIVE"]
    return FieldDiag(
        status="unavailable",
        reason_code="no_explosive_zone",
        value=None,
        debug={
            "total_zones": len(zones),
            "explosive_zones": len(explosive_zones),
        },
    )


def diag_wall_call(walls_profile, spot: float) -> FieldDiag:
    """Diagnostic du major call wall."""
    wall = getattr(walls_profile, "major_call_wall", spot)
    if wall and abs(wall - spot) / spot > 0.005 and wall > spot:
        dist_pct = (wall - spot) / spot * 100
        return FieldDiag(
            status="available",
            reason_code="call_wall_found",
            value=wall,
            debug={"distance_pct": round(dist_pct, 2), "side": "resistance"},
        )
    return FieldDiag(
        status="unavailable",
        reason_code="no_calls_above_spot",
        value=None,
        debug={"wall_raw": wall, "spot": spot},
    )


def diag_wall_put(walls_profile, spot: float) -> FieldDiag:
    """Diagnostic du major put wall."""
    wall = getattr(walls_profile, "major_put_wall", spot)
    if wall and abs(wall - spot) / spot > 0.005 and wall < spot:
        dist_pct = (spot - wall) / spot * 100
        return FieldDiag(
            status="available",
            reason_code="put_wall_found",
            value=wall,
            debug={"distance_pct": round(dist_pct, 2), "side": "support"},
        )
    return FieldDiag(
        status="unavailable",
        reason_code="no_puts_below_spot",
        value=None,
        debug={"wall_raw": wall, "spot": spot},
    )


def diag_mopi(mopi_score) -> FieldDiag:
    """MOPI est toujours calculable — always_available."""
    return FieldDiag(
        status="available",
        reason_code="always_available",
        value=mopi_score.score,
        debug={"label": mopi_score.label, "iv_rank": mopi_score.iv_rank},
    )


def diag_squeeze(squeeze_score) -> FieldDiag:
    """Squeeze score est toujours calculable — always_available."""
    return FieldDiag(
        status="available",
        reason_code="always_available",
        value=squeeze_score.score,
        debug={"label": squeeze_score.label, "direction_bias": squeeze_score.direction_bias},
    )


def diag_dealer_pressure(dp) -> FieldDiag:
    """Dealer pressure est toujours calculable — always_available."""
    return FieldDiag(
        status="available",
        reason_code="always_available",
        value=dp.pressure_pct,
        debug={"direction": dp.direction, "intensity": dp.intensity},
    )


def diag_gex_calibration(cal: dict) -> FieldDiag:
    """Diagnostic de la calibration du cap GEX near-term.

    Évalue la fiabilité du cap dynamique utilisé pour normaliser gex_near
    dans le score MOPI.  Sans calibration fiable, le GEX ne doit pas être
    présenté comme une certitude absolue.
    """
    try:
        cap_value = cal.get("cap_value", 0) or 0
        cap_mode = cal.get("cap_mode", "static/bootstrap") or "static/bootstrap"
        n_points = int(cal.get("n_points", 0) or 0)
        sat = cal.get("saturation_rate_7d")
        neu = cal.get("neutralization_rate_7d")

        debug: Dict[str, Any] = {
            "cap_value": cap_value,
            "cap_mode": cap_mode,
            "n_points": n_points,
            "saturation_rate_7d": sat,
            "neutralization_rate_7d": neu,
        }

        if n_points == 0:
            return FieldDiag(
                status="unavailable",
                reason_code="calibration_missing",
                value=None,
                debug=debug,
            )

        if n_points < _GEX_CAL_MIN_POINTS:
            return FieldDiag(
                status="stale",
                reason_code="calibration_stale",
                value=float(cap_value),
                debug=debug,
            )

        sat_alert = sat is not None and sat > _GEX_SAT_THRESHOLD
        neu_alert = neu is not None and neu > _GEX_NEU_THRESHOLD
        if sat_alert or neu_alert:
            return FieldDiag(
                status="degraded",
                reason_code="calibration_inconsistent",
                value=float(cap_value),
                debug=debug,
            )

        return FieldDiag(
            status="available",
            reason_code="calibration_available",
            value=float(cap_value),
            debug=debug,
        )
    except Exception as exc:
        return FieldDiag(
            status="unavailable",
            reason_code="calibration_error",
            value=None,
            debug={"error": str(exc)},
        )


def build_all_diagnostics(
    gex_profile, snapshot, gmap, walls_profile,
    mopi_score, squeeze_score, dp,
    gex_calibration_cache: Optional[Dict[str, Any]] = None,
) -> dict:
    """Construit le dictionnaire complet de diagnostics pour tous les champs critiques."""
    spot = gex_profile.btc_price
    cal = gex_calibration_cache or {}
    return {
        "flip_level":        diag_flip_level(gex_profile, snapshot).to_dict(),
        "gravity_magnet":    diag_gravity_magnet(gmap).to_dict(),
        "gravity_explosive": diag_gravity_explosive(gmap).to_dict(),
        "wall_call":         diag_wall_call(walls_profile, spot).to_dict(),
        "wall_put":          diag_wall_put(walls_profile, spot).to_dict(),
        "mopi":              diag_mopi(mopi_score).to_dict(),
        "squeeze":           diag_squeeze(squeeze_score).to_dict(),
        "dealer_pressure":   diag_dealer_pressure(dp).to_dict(),
        "gex_calibration":   diag_gex_calibration(cal).to_dict(),
    }


def _infer_quality_state(gex_profile) -> str:
    """Infère l'état qualité depuis le profil GEX (sans dépendre de l'audit complet)."""
    near = abs(getattr(gex_profile, "gex_near", 0) or 0)
    total = abs(getattr(gex_profile, "total_gex", 0) or 0)
    if total < 5_000_000:
        return "dormant"
    if near < 10_000_000:
        return "structural"
    if near < 50_000_000:
        return "active"
    return "actionable"
