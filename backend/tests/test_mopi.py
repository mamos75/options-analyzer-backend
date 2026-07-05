"""
Tests de non-régression pour le module MOPI.
Protège contre le bug cap GEX trop bas (50M → transformait GEX en interrupteur binaire).
Cap corrigé : 5_000_000_000 (ordre de grandeur réel BTC).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.mopi import _normalize_gex, _normalize_gex_near, compute_mopi, _estimate_squeeze_prob, _compute_iv_rank
from backend.deribit_client import OptionData, MarketSnapshot
from backend.gex import GEXProfile

BTC_SPOT = 70_000.0


def _make_option(strike, opt_type, gamma=0.0001, oi=100.0):
    return OptionData(
        instrument=f"BTC-30MAY26-{int(strike)}-{'C' if opt_type == 'call' else 'P'}",
        strike=strike,
        expiry="30MAY26",
        option_type=opt_type,
        oi=oi,
        volume=0.0,
        gamma=gamma,
        delta=0.5,
        iv=60.0,
        mark_price=0.05,
        bid=0.04,
        ask=0.06,
    )


def _neutral_snapshot():
    opts = [
        _make_option(BTC_SPOT, "call", oi=100),
        _make_option(BTC_SPOT, "put", oi=100),
    ]
    return MarketSnapshot(btc_price=BTC_SPOT, options=opts, timestamp=0.0)


def _neutral_gex_profile(gex_value: float, gex_near: float = None) -> GEXProfile:
    """Crée un GEXProfile de test.
    gex_near — si non spécifié, = gex_value (simule near-term = totalité du GEX).
    """
    if gex_value > 5_000_000:
        regime = "STABILISANT"
    elif gex_value < -5_000_000:
        regime = "AMPLIFICATEUR"
    else:
        regime = "NEUTRE"
    near = gex_value if gex_near is None else gex_near
    return GEXProfile(
        total_gex=gex_value,
        gex_by_strike={},
        call_gex_by_strike={},
        put_gex_by_strike={},
        flip_level=BTC_SPOT * 0.95,
        max_pain=BTC_SPOT,
        gamma_walls=[],
        btc_price=BTC_SPOT,
        regime=regime,
        gex_near=near,
    )


# ─── Tests _normalize_gex ────────────────────────────────────────────────────

def test_gex_cap_5b():
    """GEX +3B doit donner ~80, pas 100 (bug interrupteur binaire)."""
    score = _normalize_gex(3_000_000_000)
    assert 78 <= score <= 82, f"GEX +3B attendu ~80, got {score:.1f}"


def test_gex_zero_gives_fifty():
    """GEX 0 = équilibre parfait = score 50."""
    score = _normalize_gex(0)
    assert score == 50.0, f"GEX 0 attendu 50.0, got {score}"


def test_gex_negative_3b_gives_twenty():
    """GEX -3B doit donner ~20 (symétrique de +3B)."""
    score = _normalize_gex(-3_000_000_000)
    assert 18 <= score <= 22, f"GEX -3B attendu ~20, got {score:.1f}"


def test_gex_plus_5b_gives_hundred():
    """GEX +5B (cap max) = score 100."""
    score = _normalize_gex(5_000_000_000)
    assert score == 100.0, f"GEX +5B attendu 100.0, got {score}"


def test_gex_minus_5b_gives_zero():
    """GEX -5B (cap min) = score 0."""
    score = _normalize_gex(-5_000_000_000)
    assert score == 0.0, f"GEX -5B attendu 0.0, got {score}"


def test_gex_capped_not_saturated_at_3b():
    """Régression principale : GEX +3B ne doit plus retourner 100 (cap 50M era)."""
    score = _normalize_gex(3_000_000_000)
    assert score < 100.0, f"GEX +3B ne doit pas être saturé à 100 — got {score}"
    assert score > 0.0, "GEX +3B doit être > 0"


def test_gex_is_continuous_not_binary():
    """Le score GEX doit être continu entre 0 et 100, pas binaire 0/100."""
    scores = [_normalize_gex(g) for g in [-5e9, -3e9, -1e9, 0, 1e9, 3e9, 5e9]]
    for i in range(len(scores) - 1):
        assert scores[i] < scores[i + 1], f"Score GEX non monotone : {scores}"
    # Doit couvrir toute la plage 0-100 entre -5B et +5B
    assert scores[0] == 0.0
    assert scores[-1] == 100.0
    # Les valeurs intermédiaires doivent être réellement intermédiaires
    assert 10 < scores[1] < 30, f"GEX -3B attendu ~20, got {scores[1]}"
    assert 40 < scores[3] < 60, f"GEX 0 attendu ~50, got {scores[3]}"
    assert 70 < scores[5] < 90, f"GEX +3B attendu ~80, got {scores[5]}"


# ─── Tests compute_mopi (score global) ──────────────────────────────────────

def test_mopi_bounded_0_100():
    """MOPI doit toujours rester entre 0 et 100 quelle que soit l'entrée."""
    snapshot = _neutral_snapshot()
    for gex_val in [-5e9, -1e9, 0, 1e9, 3e9, 5e9]:
        gex = _neutral_gex_profile(gex_val)
        mopi = compute_mopi(snapshot, gex, iv_history_90d=[60.0] * 90)
        assert 0 <= mopi.score <= 100, f"MOPI hors bornes pour GEX={gex_val}: {mopi.score}"


def test_mopi_gex_3b_not_low_with_neutral_others():
    """
    Post-audit gamma effectif : MOPI gex_component basé sur gex_near (cap 500M).
    gex_near=300M → gex_score ~80 (non saturé) → MOPI > 50 avec composantes neutres.
    300M = valeur near-term réaliste forte (équivalent ancien 3B total pour l'impact signal).
    """
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(3_000_000_000, gex_near=300_000_000)
    # Historique IV neutre (IV rank ~50%)
    iv_history = [50.0] * 45 + [70.0] * 45  # current=60 → rank ~50%
    mopi = compute_mopi(snapshot, gex, iv_history_90d=iv_history)
    assert mopi.score > 50, (
        f"MOPI avec gex_near=300M doit être > 50 si autres composantes neutres, got {mopi.score:.1f}"
    )
    assert mopi.gex_component < 100, (
        f"gex_component doit être < 100 (non saturé), got {mopi.gex_component}"
    )
    assert 78 <= mopi.gex_component <= 82, (
        f"gex_component attendu ~80 pour gex_near=300M, got {mopi.gex_component}"
    )


def test_mopi_gex_0_gives_neutral_gex_component():
    """GEX 0 → gex_component = 50 (neutre exact)."""
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(0)
    mopi = compute_mopi(snapshot, gex, iv_history_90d=[60.0] * 90)
    assert mopi.gex_component == 50.0, f"gex_component GEX=0 attendu 50.0, got {mopi.gex_component}"


def test_mopi_gex_negative_3b_gives_low_gex_component():
    """Post-audit : gex_near=-300M → gex_component ~20 (zone baissière).
    300M négatif near-term = pression sell-side effective forte."""
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(-3_000_000_000, gex_near=-300_000_000)
    mopi = compute_mopi(snapshot, gex, iv_history_90d=[60.0] * 90)
    assert 18 <= mopi.gex_component <= 22, (
        f"gex_component gex_near=-300M attendu ~20, got {mopi.gex_component}"
    )


# ─── Tests _compute_iv_rank (non-régression bug >100%) ───────────────────────

def test_iv_rank_clamped_when_iv_exceeds_history_max():
    """Bug corrigé : si IV actuelle > max historique, IV Rank était > 100%.
    Doit maintenant être clampé à 100.0.
    """
    history = [50.0, 60.0, 70.0, 80.0]  # max historique = 80
    rank = _compute_iv_rank(current_iv=90.0, history=history)  # IV actuelle > max → anciennement 125%
    assert rank == 100.0, f"IV Rank doit être clampé à 100.0 quand IV > max historique, got {rank}"


def test_iv_rank_clamped_when_iv_below_history_min():
    """Si IV actuelle < min historique, IV Rank ne doit pas être négatif."""
    history = [50.0, 60.0, 70.0, 80.0]  # min historique = 50
    rank = _compute_iv_rank(current_iv=30.0, history=history)
    assert rank == 0.0, f"IV Rank doit être clampé à 0.0 quand IV < min historique, got {rank}"


def test_iv_rank_normal_case():
    """IV dans la plage historique → calcul correct."""
    history = [40.0, 60.0, 80.0, 100.0]
    rank = _compute_iv_rank(current_iv=70.0, history=history)
    assert abs(rank - 50.0) < 0.1, f"IV Rank attendu ~50%, got {rank}"


def test_mopi_iv_rank_always_in_0_100():
    """iv_rank dans MOPIScore doit toujours être dans [0, 100] même si IV dépasse l'historique."""
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(0)
    # Historique avec max = 60, IV actuelle = 90 → anciennement iv_rank = 150%
    iv_history = [30.0] * 90  # historique homogène = toujours 50 (high==low fallback)
    mopi = compute_mopi(snapshot, gex, iv_history_90d=iv_history)
    assert 0.0 <= mopi.iv_rank <= 100.0, f"iv_rank hors bornes: {mopi.iv_rank}"


# ─── Tests _estimate_squeeze_prob ───────────────────────────────────────────

def test_squeeze_prob_positive_gex_3b_not_min():
    """Post-audit : gex_near=200M positif → squeeze_prob entre 30 et 50.
    200M near-term réaliste : prob -= min(20, 200M/25M) = min(20, 8) = 8 → prob = 42.
    Vérifie que la calibration est continue (pas un interrupteur 30/50).
    """
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(3_000_000_000, gex_near=200_000_000)
    prob = _estimate_squeeze_prob(gex, snapshot)
    assert 30 < prob < 50, (
        f"Squeeze prob gex_near=200M attendu dans ]30, 50[, got {prob:.1f}"
    )


def test_squeeze_prob_negative_gex_3b_not_max():
    """Post-audit : gex_near=-100M → squeeze_prob dans une plage raisonnable (non saturé).
    gex_near=-100M : prob += min(30, 100M/16.7M) = min(30, 5.99) ≈ 55.99.
    Vérifie que la calibration near-term est raisonnable (pas plafonnée instantanément).
    """
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(-3_000_000_000, gex_near=-100_000_000)
    prob = _estimate_squeeze_prob(gex, snapshot)
    assert 50 < prob < 80, (
        f"Squeeze prob gex_near=-100M attendu dans ]50, 80[, got {prob:.1f}"
    )


def test_squeeze_prob_bounded():
    """squeeze_prob toujours entre 0 et 100."""
    snapshot = _neutral_snapshot()
    for gex_val in [-10e9, -3e9, 0, 3e9, 10e9]:
        gex = _neutral_gex_profile(gex_val)
        prob = _estimate_squeeze_prob(gex, snapshot)
        assert 0 <= prob <= 100, f"squeeze_prob hors bornes pour GEX={gex_val}: {prob}"


# ─── Tests cap dynamique + observabilité ────────────────────────────────────

def test_normalize_gex_near_static_cap():
    """Cap statique 500M : GEX 0 → 50, GEX +500M → 100, GEX -500M → 0."""
    assert _normalize_gex_near(0) == 50.0
    assert _normalize_gex_near(500_000_000) == 100.0
    assert _normalize_gex_near(-500_000_000) == 0.0


def test_normalize_gex_near_dynamic_cap_smaller():
    """Cap dynamique réduit (200M) : GEX +200M → 100, GEX +100M → ~75."""
    assert _normalize_gex_near(200_000_000, cap=200_000_000) == 100.0
    score = _normalize_gex_near(100_000_000, cap=200_000_000)
    assert 73 <= score <= 77, f"GEX +100M cap=200M attendu ~75, got {score:.1f}"


def test_normalize_gex_near_dynamic_cap_larger():
    """Cap dynamique élevé (2B) : GEX +500M → score réduit (non saturé)."""
    score = _normalize_gex_near(500_000_000, cap=2_000_000_000)
    assert 55 <= score <= 65, f"GEX +500M cap=2B attendu ~62.5, got {score:.1f}"


def test_normalize_gex_near_always_bounded():
    """_normalize_gex_near toujours dans [0, 100] avec n'importe quel cap."""
    for gex in [-10e9, -500e6, 0, 500e6, 10e9]:
        for cap in [100e6, 500e6, 2e9]:
            score = _normalize_gex_near(gex, cap=cap)
            assert 0 <= score <= 100, f"Score hors bornes: gex={gex} cap={cap} → {score}"


def test_compute_mopi_uses_dynamic_cap():
    """compute_mopi avec cap dynamique 200M : gex_near=200M → gex_component=100."""
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(3_000_000_000, gex_near=200_000_000)
    mopi = compute_mopi(
        snapshot, gex, iv_history_90d=[60.0] * 90,
        gex_near_cap=200_000_000,
        cap_mode="dynamic/rolling_7d",
        saturation_rate_7d=0.25,
    )
    assert mopi.gex_component == 100.0, f"gex_component attendu 100.0 avec gex_near=cap, got {mopi.gex_component}"
    assert mopi.gex_near_cap == 200_000_000
    assert mopi.cap_mode == "dynamic/rolling_7d"
    assert mopi.saturation_rate_7d == 0.25


def test_compute_mopi_observability_fields_in_mopiscore():
    """MOPIScore expose toujours les 3 champs d'observabilité."""
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(0)
    mopi = compute_mopi(snapshot, gex, iv_history_90d=[60.0] * 90)
    assert hasattr(mopi, "gex_near_cap")
    assert hasattr(mopi, "cap_mode")
    assert hasattr(mopi, "saturation_rate_7d")
    assert mopi.cap_mode == "static/bootstrap"
    assert mopi.saturation_rate_7d is None


def test_compute_mopi_bootstrap_cap_unchanged():
    """Sans cap dynamique (bootstrap), le comportement reste identique à avant — non-régression."""
    snapshot = _neutral_snapshot()
    gex = _neutral_gex_profile(3_000_000_000, gex_near=300_000_000)
    iv_history = [50.0] * 45 + [70.0] * 45
    mopi_old = compute_mopi(snapshot, gex, iv_history_90d=iv_history)
    mopi_new = compute_mopi(
        snapshot, gex, iv_history_90d=iv_history,
        gex_near_cap=500_000_000,
        cap_mode="static/bootstrap",
    )
    assert mopi_old.score == mopi_new.score, "Score ne doit pas changer avec cap statique identique"
    assert mopi_old.gex_component == mopi_new.gex_component


# ─── Runner standalone ────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    if failed:
        sys.exit(1)
