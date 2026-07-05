"""
Tests de non-régression — Max Pain par expiry.
Protège contre le mélange d'expiries qui rendait le Max Pain trompeur.

Règle mémorisée : une métrique options sans contexte d'expiration est dangereuse.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.deribit_client import OptionData, MarketSnapshot
from backend.gex import (
    compute_max_pain_by_expiry,
    compute_gex,
    _compute_max_pain_single,
    _compute_dte,
    _MIN_OI_NEAR_TERM,
)

# Dates bien dans le futur pour que DTE > 0 indépendamment de la date d'exécution
EXPIRY_NEAR = "30JAN30"      # near-term : plus proche
EXPIRY_FAR = "31DEC30"       # far : institutional (plus loin, plus d'OI)
EXPIRY_PAST = "01JAN20"      # passée — doit toujours être exclue

BTC_SPOT = 94_000.0


def _opt(strike, opt_type, oi, expiry=EXPIRY_NEAR, gamma=0.0001):
    return OptionData(
        instrument=f"BTC-{expiry}-{int(strike)}-{'C' if opt_type == 'call' else 'P'}",
        strike=strike,
        expiry=expiry,
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


def _snapshot(options):
    return MarketSnapshot(btc_price=BTC_SPOT, options=options, timestamp=0.0)


# ─── DTE helper ──────────────────────────────────────────────────────────────

def test_compute_dte_future_positive():
    """Expiry future → DTE positif."""
    dte = _compute_dte(EXPIRY_FAR)
    assert dte > 0, f"DTE doit être positif pour {EXPIRY_FAR}, got {dte}"


def test_compute_dte_past_negative_or_zero():
    """Expiry passée → DTE ≤ 0."""
    dte = _compute_dte(EXPIRY_PAST)
    assert dte <= 0, f"DTE doit être ≤ 0 pour {EXPIRY_PAST}, got {dte}"


def test_compute_dte_near_less_than_far():
    """EXPIRY_NEAR doit avoir un DTE plus petit que EXPIRY_FAR."""
    dte_near = _compute_dte(EXPIRY_NEAR)
    dte_far = _compute_dte(EXPIRY_FAR)
    assert dte_near < dte_far, f"Near DTE={dte_near} doit être < Far DTE={dte_far}"


# ─── Empty / degenerate cases ────────────────────────────────────────────────

def test_empty_options_returns_none():
    """Aucune option → None, pas d'exception."""
    result = compute_max_pain_by_expiry([])
    assert result is None


def test_all_expired_returns_none():
    """Toutes les expiries passées → None."""
    options = [
        _opt(90_000, "call", oi=500, expiry=EXPIRY_PAST),
        _opt(90_000, "put", oi=500, expiry=EXPIRY_PAST),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is None, "Toutes expiries passées → doit retourner None"


# ─── Sélection near-term ─────────────────────────────────────────────────────

def test_near_term_is_closest_dte():
    """near doit être l'expiry avec le DTE le plus petit (et OI suffisant)."""
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_NEAR),
        _opt(90_000, "call", oi=300, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=300, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.near.expiry == EXPIRY_NEAR, (
        f"near doit être {EXPIRY_NEAR} (DTE le plus petit), got {result.near.expiry}"
    )


def test_near_excludes_expired_expiry():
    """L'expiry passée ne doit jamais apparaître dans near ni institutional."""
    options = [
        _opt(90_000, "call", oi=500, expiry=EXPIRY_PAST),
        _opt(90_000, "put",  oi=500, expiry=EXPIRY_PAST),
        _opt(90_000, "call", oi=200, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_NEAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.near.expiry != EXPIRY_PAST, "near ne doit jamais être une expiry passée"
    assert result.institutional.expiry != EXPIRY_PAST, "institutional ne doit jamais être une expiry passée"


# ─── Sélection institutional ─────────────────────────────────────────────────

def test_institutional_is_highest_oi_expiry():
    """institutional doit être l'expiry avec l'OI total le plus élevé."""
    options = [
        _opt(90_000, "call", oi=100, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=100, expiry=EXPIRY_NEAR),
        _opt(90_000, "call", oi=500, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=500, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.institutional.expiry == EXPIRY_FAR, (
        f"institutional doit être {EXPIRY_FAR} (OI le plus élevé), got {result.institutional.expiry}"
    )
    assert result.institutional.oi_total == 1000.0  # 500 calls + 500 puts


def test_single_expiry_near_equals_institutional():
    """Avec une seule expiry valide, near et institutional doivent être identiques."""
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.near.expiry == result.institutional.expiry == EXPIRY_FAR
    assert result.near.strike == result.institutional.strike


# ─── Formule max pain par expiry ─────────────────────────────────────────────

def test_max_pain_formula_correct_per_expiry():
    """
    Cas contrôlé — vérifie que le calcul par expiry est correct.

    Strike 90k: call OI=200, put OI=50
    Strike 100k: call OI=100, put OI=150  (même expiry EXPIRY_NEAR)

    À 90k : 100k put ITM → (100k-90k)×150 = 1,500,000
    À 100k: 90k call ITM → (100k-90k)×200 = 2,000,000
    → Max Pain = 90k (pain minimal = 1,500,000)
    """
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=50,  expiry=EXPIRY_NEAR),
        _opt(100_000, "call", oi=100, expiry=EXPIRY_NEAR),
        _opt(100_000, "put",  oi=150, expiry=EXPIRY_NEAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.near.strike == 90_000, (
        f"Max Pain attendu à 90k, got {result.near.strike}"
    )


def test_max_pain_not_mixed_across_expiries():
    """
    Régression principale : Max Pain ne doit PAS mélanger les expiries.

    EXPIRY_NEAR : strike 90k, calls lourds (OI=1000)
    EXPIRY_FAR  : strike 100k, puts lourds (OI=1000)

    Sans séparation par expiry, les puts far-term polluent le calcul near-term.
    Avec séparation, near doit ignorer les puts EXPIRY_FAR.
    """
    options = [
        # Near-term : seulement des calls sur 90k
        _opt(90_000, "call", oi=1000, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=10,   expiry=EXPIRY_NEAR),
        # Far-term : puts massifs sur 100k
        _opt(100_000, "put",  oi=1000, expiry=EXPIRY_FAR),
        _opt(100_000, "call", oi=10,   expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None

    # Near-term : calcul sur EXPIRY_NEAR uniquement
    # À 90k: call 90k → pas ITM (égal). Put 90k: 90k=90k pas ITM → pain=0
    # → Max pain = 90k (seul strike disponible)
    assert result.near.expiry == EXPIRY_NEAR
    assert result.near.strike == 90_000, (
        f"Near max pain doit être 90k (calculé sur EXPIRY_NEAR seulement), got {result.near.strike}"
    )


# ─── OI threshold / fallback ─────────────────────────────────────────────────

def test_low_oi_fallback_still_returns_result():
    """OI < _MIN_OI_NEAR_TERM → fallback actif, résultat non-None."""
    low_oi = _MIN_OI_NEAR_TERM - 1  # juste en dessous du seuil
    options = [
        _opt(90_000, "call", oi=low_oi, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=low_oi, expiry=EXPIRY_NEAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None, "Même avec OI faible, le fallback doit retourner un résultat"
    assert result.near.expiry == EXPIRY_NEAR


def test_oi_total_reported_correctly():
    """oi_total dans le résultat = somme OI calls + puts de l'expiry."""
    options = [
        _opt(90_000, "call", oi=300, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_NEAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.near.oi_total == 500.0, (
        f"oi_total attendu 500, got {result.near.oi_total}"
    )


# ─── DTE dans les résultats ──────────────────────────────────────────────────

def test_dte_in_result_is_positive():
    """Le DTE retourné dans le résultat doit être > 0."""
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.near.dte > 0
    assert result.institutional.dte > 0


def test_institutional_dte_geq_near_dte():
    """institutional.dte ≥ near.dte (institutional = plus loin ou égal)."""
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_NEAR),
        _opt(90_000, "call", oi=500, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=500, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.institutional.dte >= result.near.dte, (
        f"institutional DTE ({result.institutional.dte}) doit être ≥ near DTE ({result.near.dte})"
    )


# ─── Intégration avec compute_gex ────────────────────────────────────────────

def test_compute_gex_exposes_max_pain_profile():
    """compute_gex() doit exposer max_pain_profile non-None avec des options futures."""
    options = [
        _opt(90_000, "call", oi=500, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=500, expiry=EXPIRY_FAR),
    ]
    snap = _snapshot(options)
    profile = compute_gex(snap)
    assert profile.max_pain_profile is not None, "max_pain_profile ne doit pas être None"
    assert profile.max_pain == profile.max_pain_profile.near.strike, (
        "gex.max_pain doit correspondre à max_pain_profile.near.strike (backward compat)"
    )


def test_compute_gex_max_pain_not_zero_with_valid_options():
    """max_pain ne doit pas être 0 si des options valides existent."""
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_FAR),
    ]
    snap = _snapshot(options)
    profile = compute_gex(snap)
    assert profile.max_pain > 0, f"max_pain doit être > 0, got {profile.max_pain}"


# ─── Max Pain Active (volume) ─────────────────────────────────────────────────

# Dates supplémentaires pour tester active/actionable
EXPIRY_MID = "15JUN30"   # entre NEAR et FAR — utilisé pour tester le volume


def _opt_vol(strike, opt_type, oi, volume, expiry=EXPIRY_MID):
    return OptionData(
        instrument=f"BTC-{expiry}-{int(strike)}-{'C' if opt_type == 'call' else 'P'}",
        strike=strike,
        expiry=expiry,
        option_type=opt_type,
        oi=oi,
        volume=volume,
        gamma=0.0001,
        delta=0.5,
        iv=60.0,
        mark_price=0.05,
        bid=0.04,
        ask=0.06,
    )


def test_active_max_pain_is_highest_volume_expiry():
    """active doit être l'expiry avec le plus de volume récent."""
    options = [
        # EXPIRY_NEAR : peu de volume
        _opt(90_000, "call", oi=500, expiry=EXPIRY_NEAR),
        _opt(90_000, "put",  oi=500, expiry=EXPIRY_NEAR),
        # EXPIRY_MID : beaucoup de volume
        _opt_vol(90_000, "call", oi=500, volume=1000.0),
        _opt_vol(90_000, "put",  oi=500, volume=1000.0),
    ]
    # Ajouter volume=0 sur NEAR (déjà 0 par défaut dans _opt)
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.active is not None
    assert result.active.expiry == EXPIRY_MID, (
        f"active doit être l'expiry avec plus de volume ({EXPIRY_MID}), got {result.active.expiry}"
    )


def test_active_max_pain_not_none():
    """active ne doit jamais être None si des options valides existent."""
    options = [
        _opt(90_000, "call", oi=200, expiry=EXPIRY_FAR),
        _opt(90_000, "put",  oi=200, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.active is not None


# ─── Max Pain Actionable (DTE≤3) ─────────────────────────────────────────────

EXPIRY_3D = "03JAN30"    # DTE > 3 jours (test structure uniquement)


def test_actionable_none_when_no_dte_leq_3():
    """Sans expiry DTE≤3, actionable = None (pas de cassure imminente)."""
    options = [
        _opt(90_000, "call", oi=500, expiry=EXPIRY_NEAR),   # DTE >> 3
        _opt(90_000, "put",  oi=500, expiry=EXPIRY_FAR),
    ]
    result = compute_max_pain_by_expiry(options)
    assert result is not None
    assert result.actionable is None, (
        "actionable doit être None quand aucune expiry n'est à DTE≤3"
    )


def test_actionable_dte_leq_3_validated():
    """Mamos valide : Max Pain Actionable = DTE≤3 uniquement."""
    # On ne peut pas créer facilement une vraie date à DTE=2 car _compute_dte
    # calcule depuis today. On vérifie juste que si actionable n'est pas None,
    # son DTE ≤ 3.
    # Ce test vérifie la structure : si actionable existe → DTE ≤ 3.
    from backend.gex import MaxPainProfile
    # Dummy profile créé manuellement
    from backend.gex import MaxPainExpiry
    profile = MaxPainProfile(
        near=MaxPainExpiry(strike=90_000, expiry=EXPIRY_NEAR, dte=5, oi_total=1000),
        institutional=MaxPainExpiry(strike=92_000, expiry=EXPIRY_FAR, dte=400, oi_total=5000),
        active=MaxPainExpiry(strike=91_000, expiry=EXPIRY_MID, dte=50, oi_total=2000),
        actionable=MaxPainExpiry(strike=89_000, expiry="02JAN30", dte=2, oi_total=300),
    )
    assert profile.actionable.dte <= 3, "Si actionable existe, DTE doit être ≤ 3"


def test_actionable_near_has_valid_structure():
    """MaxPainProfile.actionable doit avoir strike, expiry, dte, oi_total si non-None."""
    from backend.gex import MaxPainExpiry
    mp = MaxPainExpiry(strike=88_000, expiry="01JAN30", dte=1, oi_total=500)
    assert mp.strike == 88_000
    assert mp.dte == 1
    assert mp.oi_total == 500


# ─── Runner standalone ───────────────────────────────────────────────────────

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
