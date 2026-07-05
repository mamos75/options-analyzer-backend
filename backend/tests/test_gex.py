"""
Tests de non-régression pour le module GEX.
Protège contre la régression /1e8 qui rendait tout le dashboard faussement NEUTRE.
"""

import sys
import os

# Ajoute dashboard_options/ au path pour importer backend comme package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.deribit_client import OptionData, MarketSnapshot
from backend.gex import (
    compute_gex,
    _classify_regime,
    _find_flip_level_legacy,
    _find_flip_level_A,
    _find_flip_level_B,
    _find_flip_level_C,
    _find_flip_level,
    _derive_regime,
    flip_scenario_comparison,
    GEX_NEUTRAL_THRESHOLD,
    CONTRACT_SIZE,
)

BTC_SPOT = 67_000.0


def _make_option(strike, opt_type, gamma, oi, mark_price=0.05):
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
        mark_price=mark_price,
        bid=0.04,
        ask=0.06,
    )


def _snapshot(options):
    return MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)


# ─── Non-régression formule ──────────────────────────────────────────────────

def test_gex_formula_scale():
    """
    Régression /1e8 : gamma Deribit ~0.0001 → GEX doit être en millions USD,
    pas en quelques centaines (bug si on divisait par 1e8 par erreur).
    """
    # gamma typique Deribit ATM BTC : ~0.0001 BTC/BTC²
    # OI typique ATM : ~500 contrats
    # GEX = 0.0001 × 500 × 1 × 67000² ≈ 224M USD
    gamma = 0.0001
    oi = 500.0
    opts = [_make_option(BTC_SPOT, "call", gamma, oi)]
    profile = compute_gex(_snapshot(opts))
    expected = gamma * oi * CONTRACT_SIZE * (BTC_SPOT ** 2)
    assert abs(profile.total_gex - expected) < 1.0, (
        f"Formule GEX incorrecte: {profile.total_gex:.2f} ≠ {expected:.2f}"
    )
    assert abs(profile.total_gex) > 1_000_000, (
        f"GEX absurdement petit ({profile.total_gex:.2f}) — régression /1e8 détectée"
    )


def test_gex_formula_net_call_minus_put():
    """Calls = GEX positif, puts = GEX négatif, net correct."""
    gamma = 0.0001
    oi = 100.0
    call = _make_option(BTC_SPOT, "call", gamma, oi)
    put = _make_option(BTC_SPOT, "put", gamma, oi)
    gex_call_only = compute_gex(_snapshot([call])).total_gex
    gex_put_only = compute_gex(_snapshot([put])).total_gex
    gex_both = compute_gex(_snapshot([call, put])).total_gex
    assert gex_call_only > 0, "GEX call doit être positif"
    assert gex_put_only < 0, "GEX put doit être négatif"
    assert abs(gex_both) < 1.0, f"GEX net call+put égaux = ~0, got {gex_both:.2f}"


# ─── Régimes ─────────────────────────────────────────────────────────────────

def test_regime_stabilisant():
    gex = GEX_NEUTRAL_THRESHOLD + 1
    assert _classify_regime(gex) == "STABILISANT"


def test_regime_amplificateur():
    gex = -(GEX_NEUTRAL_THRESHOLD + 1)
    assert _classify_regime(gex) == "AMPLIFICATEUR"


def test_regime_neutre_zero():
    assert _classify_regime(0.0) == "NEUTRE"


def test_regime_neutre_juste_en_dessous():
    assert _classify_regime(GEX_NEUTRAL_THRESHOLD - 1) == "NEUTRE"
    assert _classify_regime(-(GEX_NEUTRAL_THRESHOLD - 1)) == "NEUTRE"


def test_regime_frontiere_exacte():
    # Exactement au seuil → toujours NEUTRE (pas de > strict)
    assert _classify_regime(GEX_NEUTRAL_THRESHOLD) == "NEUTRE"
    assert _classify_regime(-GEX_NEUTRAL_THRESHOLD) == "NEUTRE"


def test_regime_depuis_snapshot_gros_call():
    """Snapshot dominé par gros calls → STABILISANT."""
    gamma = 0.0005
    oi = 2000.0
    opts = [_make_option(BTC_SPOT, "call", gamma, oi)]
    profile = compute_gex(_snapshot(opts))
    assert profile.regime == "STABILISANT", (
        f"Attendu STABILISANT, GEX={profile.total_gex/1e6:.1f}M"
    )


def test_regime_depuis_snapshot_gros_put():
    """Snapshot dominé par gros puts → AMPLIFICATEUR."""
    gamma = 0.0005
    oi = 2000.0
    opts = [_make_option(BTC_SPOT, "put", gamma, oi)]
    profile = compute_gex(_snapshot(opts))
    assert profile.regime == "AMPLIFICATEUR", (
        f"Attendu AMPLIFICATEUR, GEX={profile.total_gex/1e6:.1f}M"
    )


# ─── Sanity check d'échelle ───────────────────────────────────────────────────

def test_sanity_check_suspicious_scale():
    """
    Si spot > 10000 et OI > 0 mais abs(gex_total) < 1000 → scale suspecte.
    Ce test vérifie que la formule ne produira jamais ce cas avec des données réalistes.
    """
    gamma = 0.00001  # gamma très faible mais non nul
    oi = 1.0
    opts = [_make_option(BTC_SPOT, "call", gamma, oi)]
    profile = compute_gex(_snapshot(opts))
    gex = profile.total_gex
    # Même avec gamma=0.00001 et OI=1, GEX = 0.00001×1×1×67000² ≈ 44.89 USD
    # Sous les seuils de production mais NON nul — formule correcte
    expected = gamma * oi * CONTRACT_SIZE * (BTC_SPOT ** 2)
    assert abs(gex - expected) < 0.1


def test_no_options_returns_zero_gex():
    profile = compute_gex(_snapshot([]))
    assert profile.total_gex == 0.0
    assert profile.regime == "NEUTRE"


def test_gex_regime_threshold_is_5M():
    """Le seuil doit rester à $5M — ne jamais modifier sans ce test."""
    assert GEX_NEUTRAL_THRESHOLD == 5_000_000, (
        f"Seuil GEX changé! Attendu 5_000_000, got {GEX_NEUTRAL_THRESHOLD}"
    )


# ─── Non-régression flip level (bug métier 2026-05-31) ───────────────────────
# Contexte : flip legacy retournait le premier crossing depuis le strike le plus bas
# → niveaux deep OTM inutilisables (-12% à -33% du spot).
# Les variantes A/B/C doivent toujours retourner un niveau exploitable.

_SPOT_FLIP = 75_000.0

def _gex_mamos_case():
    """Profil GEX du bug Mamos : GEX +3.9B STABILISANT, flip legacy à -12%."""
    gex = {}
    for s in [50000, 55000, 60000, 62000, 63000, 64000, 65000]:
        gex[s] = -300_000_000
    for s in [66000, 67000, 68000, 69000, 70000, 71000, 72000, 73000, 74000, 74500]:
        gex[s] = -100_000_000
    gex[75000] = 3_200_000_000
    for s in [76000, 77000, 78000, 80000, 85000, 90000]:
        gex[s] = 200_000_000
    return gex


def test_flip_legacy_bug_confirms_deep_otm():
    """Legacy retourne un niveau deep OTM (> 10% du spot) → bug confirmé."""
    gex = _gex_mamos_case()
    level = _find_flip_level_legacy(gex, _SPOT_FLIP)
    dist_pct = abs(level - _SPOT_FLIP) / _SPOT_FLIP * 100
    assert dist_pct > 10, (
        f"Bug legacy non reproductible: flip={level:,.0f}, dist={dist_pct:.1f}%"
    )


def test_flip_A_retourne_niveau_proche():
    """Variante A doit trouver le dernier crossing durable, ≤ 5% du spot."""
    gex = _gex_mamos_case()
    level = _find_flip_level_A(gex, _SPOT_FLIP)
    dist_pct = abs(level - _SPOT_FLIP) / _SPOT_FLIP * 100
    assert dist_pct <= 5.0, (
        f"Variante A trop loin: flip={level:,.0f}, dist={dist_pct:.1f}%"
    )


def test_flip_B_retourne_niveau_proche():
    """Variante B (±15%) doit retourner un niveau dans la fenêtre ou en fallback A."""
    gex = _gex_mamos_case()
    level = _find_flip_level_B(gex, _SPOT_FLIP)
    dist_pct = abs(level - _SPOT_FLIP) / _SPOT_FLIP * 100
    assert dist_pct <= 5.0, (
        f"Variante B trop loin: flip={level:,.0f}, dist={dist_pct:.1f}%"
    )


def test_flip_C_retourne_niveau_proche():
    """Variante C (strikes significatifs) doit filtrer le bruit et rester proche."""
    gex = _gex_mamos_case()
    level = _find_flip_level_C(gex, _SPOT_FLIP)
    dist_pct = abs(level - _SPOT_FLIP) / _SPOT_FLIP * 100
    assert dist_pct <= 5.0, (
        f"Variante C trop loin: flip={level:,.0f}, dist={dist_pct:.1f}%"
    )


def test_flip_actif_priorite_B_dans_fenetre():
    """Flip actif utilise B quand crossing dans ±15% — résultat identique à B, reason=crossing_near."""
    gex = _gex_mamos_case()
    level_actif, reason = _find_flip_level(gex, _SPOT_FLIP)
    level_B = _find_flip_level_B(gex, _SPOT_FLIP)
    assert level_actif == level_B, (
        f"Flip actif devrait correspondre à B: actif={level_actif:,.0f} B={level_B:,.0f}"
    )
    assert reason == "crossing_near", f"Raison attendue crossing_near, obtenu: {reason}"


def test_flip_actif_fallback_A_si_pas_crossing_dans_fenetre():
    """Si aucun crossing dans ±15%, le flip actif doit fallback sur variante A."""
    # Profil sans aucun put dans la zone ±15%: tout positif dans la fenêtre
    # fenêtre ±15% sur spot=75k → [63 750, 86 250]
    gex = {}
    for s in [50000, 55000, 60000, 63000]:
        gex[s] = -200_000_000  # hors fenêtre (> 15% sous spot)
    gex[65000] = 500_000_000   # juste dans la fenêtre ±15% (plancher = 63 750)
    for s in [68000, 70000, 72000, 75000, 78000, 80000]:
        gex[s] = 300_000_000   # tout positif dans ±15%
    spot = 75_000.0

    level_actif, reason = _find_flip_level(gex, spot)
    level_A = _find_flip_level_A(gex, spot)
    level_B = _find_flip_level_B(gex, spot)

    # B ne trouve pas de crossing dans la fenêtre (tout positif dans ±15%)
    # Actif doit fallback sur A → crossing_global
    assert level_actif == level_A, (
        f"Fallback A attendu: actif={level_actif:,.0f} A={level_A:,.0f} B={level_B:,.0f}"
    )
    assert reason == "crossing_global", f"Raison attendue crossing_global, obtenu: {reason}"


def test_flip_amplificateur_au_dessus_spot():
    """En régime AMPLIFICATEUR, le flip doit être AU-DESSUS du spot (résistance de régime)."""
    gex = {}
    for s in [60000, 62000, 65000, 70000, 72000, 74000]:
        gex[s] = -400_000_000
    gex[78000] = 1_500_000_000
    for s in [80000, 82000, 85000]:
        gex[s] = 200_000_000
    spot = 75_000.0

    level, reason = _find_flip_level(gex, spot)
    assert level > spot, (
        f"Flip AMPLIFICATEUR doit être > spot: flip={level:,.0f} spot={spot:,.0f}"
    )


def test_scenario_comparison_actif_gagne_au_moins_4_sur_5():
    """Avant/après : l'algo B+fallbackA doit être pertinent trader dans ≥4/5 scénarios.
    Valide que le nouveau flip répond à la bonne question (≤5% du spot) là où legacy échoue."""
    scenarios = flip_scenario_comparison()
    assert len(scenarios) == 5, "5 scénarios canoniques attendus"
    actif_wins = sum(1 for s in scenarios if s["actif_B_fallback_A"]["pertinent_trader"])
    legacy_wins = sum(1 for s in scenarios if s["legacy"]["pertinent_trader"])
    assert actif_wins >= 4, (
        f"B+fallbackA pertinent dans {actif_wins}/5 — attendu ≥4. "
        f"Legacy: {legacy_wins}/5. Vérifie les scénarios."
    )
    assert actif_wins > legacy_wins, (
        f"B+fallbackA ({actif_wins}/5) devrait surpasser legacy ({legacy_wins}/5)."
    )


def test_scenario_comparison_legacy_echoue_cas_profonds():
    """Legacy doit échouer sur les scénarios avec puts OTM profonds (bugs connus)."""
    scenarios = flip_scenario_comparison()
    puts_profonds = next(s for s in scenarios if "OTM profonds" in s["scenario"])
    ultra_profonds = next(s for s in scenarios if "ultra-profonds" in s["scenario"])
    assert not puts_profonds["legacy"]["pertinent_trader"], "Legacy devrait échouer sur puts OTM profonds"
    assert not ultra_profonds["legacy"]["pertinent_trader"], "Legacy devrait échouer sur puts ultra-profonds"
    assert puts_profonds["actif_B_fallback_A"]["pertinent_trader"], "B devrait être pertinent malgré les puts OTM"
    assert ultra_profonds["actif_B_fallback_A"]["pertinent_trader"], "B devrait filtrer les puts ultra-profonds"


# ─── Non-régression reason codes flip (spec Mamos 2026-06-01) ──────────────────

def test_flip_reason_all_gamma_negative():
    """GEX 100% négatif dans la fenêtre ±15% → reason_code=all_gamma_negative, flip_level=None."""
    spot = 75_000.0
    gex = {k: -200_000_000 for k in [68_000, 70_000, 72_000, 75_000, 78_000, 80_000]}
    level, reason = _find_flip_level(gex, spot)
    assert level is None, f"flip_level doit être None quand 100% négatif, got {level}"
    assert reason == "all_gamma_negative", f"Attendu all_gamma_negative, got {reason}"


def test_flip_reason_all_gamma_positive():
    """GEX 100% positif dans la fenêtre ±15% → reason_code=all_gamma_positive, flip_level=None."""
    spot = 75_000.0
    gex = {k: 200_000_000 for k in [68_000, 70_000, 72_000, 75_000, 78_000, 80_000]}
    level, reason = _find_flip_level(gex, spot)
    assert level is None, f"flip_level doit être None quand 100% positif, got {level}"
    assert reason == "all_gamma_positive", f"Attendu all_gamma_positive, got {reason}"


def test_flip_reason_no_near_gamma_sign_cross():
    """min<0 et max>0 dans la fenêtre mais cumul jamais positif → no_near_gamma_sign_cross."""
    spot = 75_000.0
    # cumul : 68k=-100M puis 80k=-50M — jamais de crossing négatif→positif
    gex = {68_000: -100_000_000, 80_000: 50_000_000}
    level, reason = _find_flip_level(gex, spot)
    assert level is None, f"flip_level doit être None sans crossing cumulé, got {level}"
    assert reason == "no_near_gamma_sign_cross", f"Attendu no_near_gamma_sign_cross, got {reason}"


def test_flip_none_gives_flip_available_false():
    """flip_level=None → _derive_regime retourne flip_available=False pour tous les reason codes."""
    for reason in (
        "no_near_gamma_sign_cross",
        "all_gamma_negative",
        "all_gamma_positive",
        "insufficient_near_strikes",
    ):
        flip_avail, _state, _conf = _derive_regime(reason, None)
        assert flip_avail is False, (
            f"flip_available doit être False pour reason={reason}, got {flip_avail}"
        )


def test_reason_code_status_unavailable_mapping():
    """reason_code=no_near_gamma_sign_cross → statut 'unavailable' (KPI carte = —)."""
    from backend.field_diagnostic import _REASON_TO_STATUS
    codes_unavailable = [
        "no_near_gamma_sign_cross",
        "all_gamma_negative",
        "all_gamma_positive",
        "insufficient_near_strikes",
        "quality_gate_dormant",
    ]
    for code in codes_unavailable:
        status = _REASON_TO_STATUS.get(code)
        assert status == "unavailable", (
            f"reason_code '{code}' doit mapper à 'unavailable', got '{status}'"
        )


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
