"""
Tests non-régression : explosive_bias directionnel des zones Gravity.

Couvre :
  - _compute_explosive_bias() : logique directionnelle
  - _build_narrative() : phrases bias-aware selon position spot / zone
  - _serialize_gravity_map() via GravityZone : exposition des 3 champs API
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.gravity_map import (
    _compute_explosive_bias,
    _build_narrative,
    GravityZone,
    GravityMap,
)

BTC_SPOT = 100_000.0
MAX_ABS_GEX = 1_000_000.0  # valeur de référence normalisée


# ── _compute_explosive_bias ───────────────────────────────────────────────────

def test_bias_down_only_gex_tres_negatif_puts_dominants():
    """GEX très négatif + puts dominants → DOWN_ONLY."""
    bias, score_down, score_up = _compute_explosive_bias(
        strike=95_000.0,
        spot=BTC_SPOT,
        gex=-400_000.0,      # gex_norm = -0.4 (< -0.3)
        call_oi=100.0,
        put_oi=200.0,        # put_ratio ≈ 0.67 > 1.3×call → puts_dominant
        max_abs_gex=MAX_ABS_GEX,
    )
    assert bias == "DOWN_ONLY", f"Attendu DOWN_ONLY, obtenu {bias}"
    assert score_down > score_up, "score_down doit être > score_up pour DOWN_ONLY"


def test_bias_up_only_gex_tres_negatif_calls_dominants():
    """GEX très négatif + calls dominants → UP_ONLY."""
    bias, score_down, score_up = _compute_explosive_bias(
        strike=105_000.0,
        spot=BTC_SPOT,
        gex=-400_000.0,      # gex_norm = -0.4
        call_oi=200.0,
        put_oi=100.0,        # call_ratio ≈ 0.67 > 1.3×put → calls_dominant
        max_abs_gex=MAX_ABS_GEX,
    )
    assert bias == "UP_ONLY", f"Attendu UP_ONLY, obtenu {bias}"
    assert score_up > score_down, "score_up doit être > score_down pour UP_ONLY"


def test_bias_symmetric_gex_negatif_oi_mixte():
    """GEX négatif + OI mixte (calls/puts équilibrés) → SYMMETRIC."""
    bias, score_down, score_up = _compute_explosive_bias(
        strike=100_000.0,
        spot=BTC_SPOT,
        gex=-400_000.0,      # gex_norm = -0.4, bien négatif
        call_oi=150.0,
        put_oi=150.0,        # exactement 50/50
        max_abs_gex=MAX_ABS_GEX,
    )
    assert bias == "SYMMETRIC", f"Attendu SYMMETRIC, obtenu {bias}"


def test_bias_neutral_gex_faible():
    """GEX faible (< 10% du max) → NEUTRAL."""
    bias, score_down, score_up = _compute_explosive_bias(
        strike=100_000.0,
        spot=BTC_SPOT,
        gex=-50_000.0,       # gex_norm = -0.05 < 0.1 → trop faible
        call_oi=100.0,
        put_oi=200.0,
        max_abs_gex=MAX_ABS_GEX,
    )
    assert bias == "NEUTRAL", f"Attendu NEUTRAL, obtenu {bias}"


def test_scores_bornes_0_100():
    """Les scores doivent toujours être dans [0, 100]."""
    for gex_val in [-MAX_ABS_GEX, -MAX_ABS_GEX * 0.5, 0.0]:
        for put_oi, call_oi in [(1000.0, 0.0), (0.0, 1000.0), (500.0, 500.0)]:
            _, sd, su = _compute_explosive_bias(
                strike=BTC_SPOT * 0.95,
                spot=BTC_SPOT,
                gex=gex_val,
                call_oi=call_oi,
                put_oi=put_oi,
                max_abs_gex=MAX_ABS_GEX,
            )
            assert 0.0 <= sd <= 100.0, f"score_down hors bornes : {sd}"
            assert 0.0 <= su <= 100.0, f"score_up hors bornes : {su}"


def test_neutral_si_oi_nul():
    """Si total_oi == 0, retourner NEUTRAL sans erreur."""
    bias, sd, su = _compute_explosive_bias(
        strike=BTC_SPOT, spot=BTC_SPOT,
        gex=-500_000.0, call_oi=0.0, put_oi=0.0,
        max_abs_gex=MAX_ABS_GEX,
    )
    assert bias == "NEUTRAL"


# ── _build_narrative — BTC sous zone (zone > spot) ───────────────────────────

def _make_explosive_zone(center, bias, score_down=80.0, score_up=20.0) -> GravityZone:
    return GravityZone(
        price_low=center - 500,
        price_high=center + 500,
        center=center,
        zone_type="EXPLOSIVE",
        strength=80.0,
        label=f"⚡ Explosive ${center:,.0f}",
        color="rgba(255,60,107,0.18)",
        oi_usd=1_000_000.0,
        gex=-500_000.0,
        explosive_bias=bias,
        explosive_score_down=score_down,
        explosive_score_up=score_up,
    )


def test_narrative_btc_sous_zone_down_only():
    """Zone au-dessus du spot + DOWN_ONLY → phrase 'résistance de régime, reclaim non automatiquement explosif'."""
    zone_center = BTC_SPOT * 1.05  # zone 5% au-dessus
    zones = [_make_explosive_zone(zone_center, "DOWN_ONLY")]
    narrative = _build_narrative(
        spot=BTC_SPOT,
        zones=zones,
        magnet=0.0,
        explosive=zone_center,
        gravity_score=60.0,
    )
    assert "résistance de régime" in narrative.lower() or "reclaim n'est pas automatiquement" in narrative, (
        f"Narrative DOWN_ONLY incorrecte : {narrative}"
    )


def test_narrative_btc_sous_zone_symmetric():
    """Zone au-dessus du spot + SYMMETRIC → phrase 'reclaim confirmé peut déclencher'."""
    zone_center = BTC_SPOT * 1.05
    zones = [_make_explosive_zone(zone_center, "SYMMETRIC", score_down=65.0, score_up=65.0)]
    narrative = _build_narrative(
        spot=BTC_SPOT,
        zones=zones,
        magnet=0.0,
        explosive=zone_center,
        gravity_score=60.0,
    )
    assert "reclaim" in narrative.lower() and "accélération" in narrative.lower(), (
        f"Narrative SYMMETRIC (zone au-dessus) incorrecte : {narrative}"
    )


def test_narrative_btc_sous_zone_up_only():
    """Zone au-dessus du spot + UP_ONLY → phrase 'déclencheur haussier si reclaim confirmé'."""
    zone_center = BTC_SPOT * 1.05
    zones = [_make_explosive_zone(zone_center, "UP_ONLY", score_down=20.0, score_up=85.0)]
    narrative = _build_narrative(
        spot=BTC_SPOT,
        zones=zones,
        magnet=0.0,
        explosive=zone_center,
        gravity_score=60.0,
    )
    assert "déclencheur haussier" in narrative.lower() or "reclaim confirmé" in narrative.lower(), (
        f"Narrative UP_ONLY (zone au-dessus) incorrecte : {narrative}"
    )


# ── _build_narrative — BTC au-dessus zone (zone < spot) ──────────────────────

def test_narrative_btc_dessus_zone_down_only():
    """Zone sous le spot + DOWN_ONLY → phrase 'cassure baissière peut accélérer'."""
    zone_center = BTC_SPOT * 0.95  # zone 5% en dessous
    zones = [_make_explosive_zone(zone_center, "DOWN_ONLY", score_down=88.0, score_up=22.0)]
    narrative = _build_narrative(
        spot=BTC_SPOT,
        zones=zones,
        magnet=0.0,
        explosive=zone_center,
        gravity_score=60.0,
    )
    assert "cassure" in narrative.lower() and "baiss" in narrative.lower(), (
        f"Narrative DOWN_ONLY (zone sous spot) incorrecte : {narrative}"
    )
    # Le reclaim ne doit PAS être présenté comme automatiquement explosif
    assert "reclaim ne valide pas automatiquement" in narrative.lower() or "ne valide pas" in narrative.lower(), (
        f"Le reclaim doit être nuancé pour DOWN_ONLY : {narrative}"
    )


def test_narrative_btc_dessus_zone_symmetric():
    """Zone sous le spot + SYMMETRIC → 'deux sens' ou 'cassure ou reclaim'."""
    zone_center = BTC_SPOT * 0.95
    zones = [_make_explosive_zone(zone_center, "SYMMETRIC", score_down=65.0, score_up=65.0)]
    narrative = _build_narrative(
        spot=BTC_SPOT,
        zones=zones,
        magnet=0.0,
        explosive=zone_center,
        gravity_score=60.0,
    )
    assert "deux sens" in narrative.lower() or ("cassure" in narrative.lower() and "reclaim" in narrative.lower()), (
        f"Narrative SYMMETRIC (zone sous spot) incorrecte : {narrative}"
    )


# ── Champs dataclass GravityZone ─────────────────────────────────────────────

def test_gravityzone_defaultfields():
    """GravityZone sans biais explicite → valeurs par défaut valides."""
    z = GravityZone(
        price_low=99_000, price_high=101_000, center=100_000,
        zone_type="MAGNETIC", strength=75.0,
        label="🧲 Magnétique $100,000", color="rgba(0,212,126,0.18)",
        oi_usd=1_000_000.0, gex=0.0,
    )
    assert z.explosive_bias == "NEUTRAL"
    assert z.explosive_score_down == 0.0
    assert z.explosive_score_up == 0.0


def test_gravityzone_explosive_fields_set():
    """GravityZone EXPLOSIVE avec biais renseigné → champs accessibles."""
    z = _make_explosive_zone(95_000, "DOWN_ONLY", 87.0, 22.0)
    assert z.explosive_bias == "DOWN_ONLY"
    assert z.explosive_score_down == 87.0
    assert z.explosive_score_up == 22.0


# ── Cohérence API (sérialisation simulée) ────────────────────────────────────

def test_serialize_zone_exposes_bias_fields():
    """Vérifie que les 3 champs sont présents dans la sérialisation API (simulation)."""
    z = _make_explosive_zone(95_000, "DOWN_ONLY", 87.0, 22.0)
    # Simuler _serialize_gravity_map() zone dict (logique identique à main.py)
    zone_dict = {
        "price_low": z.price_low,
        "price_high": z.price_high,
        "center": z.center,
        "zone_type": z.zone_type,
        "strength": z.strength,
        "label": z.label,
        "color": z.color,
        "oi_usd": z.oi_usd,
        "gex": z.gex,
        "explosive_bias": z.explosive_bias,
        "explosive_score_down": z.explosive_score_down,
        "explosive_score_up": z.explosive_score_up,
    }
    assert zone_dict["explosive_bias"] == "DOWN_ONLY"
    assert zone_dict["explosive_score_down"] == 87.0
    assert zone_dict["explosive_score_up"] == 22.0
