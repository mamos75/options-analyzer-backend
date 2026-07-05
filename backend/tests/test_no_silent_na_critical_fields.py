"""
Gap #8/#9 — test_no_silent_na_on_critical_dashboard_fields.

Règle gravée : tout champ critique affiché au dashboard doit avoir un diagnostic
typé avec status, reason_code, value, debug.
Aucun N/A silencieux ne peut revenir en prod.

Champs couverts : flip_level, gravity_magnet, gravity_explosive,
                  wall_call, wall_put, mopi, squeeze, dealer_pressure,
                  gex_calibration.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from types import SimpleNamespace
from backend.field_diagnostic import (
    FieldDiag,
    build_all_diagnostics,
    diag_flip_level,
    diag_gravity_magnet,
    diag_gravity_explosive,
    diag_wall_call,
    diag_wall_put,
    diag_mopi,
    diag_squeeze,
    diag_dealer_pressure,
    diag_gex_calibration,
    _REASON_TO_STATUS,
    _status_for,
    _GEX_CAL_MIN_POINTS,
    _GEX_SAT_THRESHOLD,
    _GEX_NEU_THRESHOLD,
)

# ─── Constantes de référence ──────────────────────────────────────────────────

BTC_SPOT = 95_000.0

CRITICAL_FIELDS = [
    "flip_level",
    "gravity_magnet",
    "gravity_explosive",
    "wall_call",
    "wall_put",
    "mopi",
    "squeeze",
    "dealer_pressure",
    "gex_calibration",
]

REQUIRED_DIAG_KEYS = {"status", "reason_code", "value", "debug"}
VALID_STATUSES = {"available", "degraded", "unavailable", "stale"}


# ─── Builders mock ────────────────────────────────────────────────────────────

def _gex_profile(flip_reason="crossing_near", flip_level=90_000.0):
    return SimpleNamespace(
        btc_price=BTC_SPOT,
        flip_level=flip_level,
        flip_level_reason=flip_reason,
        flip_available=flip_level is not None,
        gex_by_strike={
            90_000.0: 1_000_000,
            92_000.0: -500_000,
            98_000.0: 2_000_000,
        },
        gex_near=100_000_000,
        total_gex=300_000_000,
    )


def _snapshot():
    return SimpleNamespace(btc_price=BTC_SPOT, timestamp=0.0)


def _zone(zone_type):
    return SimpleNamespace(zone_type=zone_type)


def _gmap(magnet=90_000.0, explosive=100_000.0, zones=None):
    if zones is None:
        zones = [_zone("MAGNETIC"), _zone("EXPLOSIVE")]
    return SimpleNamespace(
        btc_price=BTC_SPOT,
        strongest_magnet=magnet,
        next_explosive=explosive,
        zones=zones,
    )


def _walls(call_wall=100_000.0, put_wall=90_000.0):
    return SimpleNamespace(major_call_wall=call_wall, major_put_wall=put_wall)


def _mopi(score=65.0, label="BULLISH", iv_rank=45.0):
    return SimpleNamespace(score=score, label=label, iv_rank=iv_rank)


def _squeeze(score=30.0, label="BUILDING", direction_bias="UP"):
    return SimpleNamespace(score=score, label=label, direction_bias=direction_bias)


def _dp(pressure_pct=60.0, direction="BULLISH_FLOWS", intensity="HIGH"):
    return SimpleNamespace(pressure_pct=pressure_pct, direction=direction, intensity=intensity)


def _calibration(
    cap_value=500_000_000,
    cap_mode="dynamic/p90_7d",
    n_points=100,
    saturation_rate_7d=None,
    neutralization_rate_7d=None,
):
    return {
        "cap_value": cap_value,
        "cap_mode": cap_mode,
        "n_points": n_points,
        "saturation_rate_7d": saturation_rate_7d,
        "neutralization_rate_7d": neutralization_rate_7d,
    }


# ─── Règle structurelle centrale ──────────────────────────────────────────────

def _assert_diag_complete(diag_dict: dict, field_name: str):
    """Un diagnostic incomplet = N/A silencieux potentiel — fail immédiat."""
    missing = REQUIRED_DIAG_KEYS - set(diag_dict.keys())
    assert not missing, (
        f"{field_name}: clés manquantes {missing} — N/A silencieux détecté"
    )
    assert diag_dict["status"] in VALID_STATUSES, (
        f"{field_name}: status invalide '{diag_dict['status']}' — doit être dans {VALID_STATUSES}"
    )
    assert isinstance(diag_dict["reason_code"], str) and diag_dict["reason_code"], (
        f"{field_name}: reason_code vide ou non-string"
    )
    assert diag_dict["reason_code"] != "N/A", (
        f"{field_name}: reason_code est la string 'N/A' — N/A silencieux"
    )
    assert diag_dict["status"] != "N/A", (
        f"{field_name}: status est la string 'N/A'"
    )
    assert diag_dict["value"] != "N/A", (
        f"{field_name}: value est la string 'N/A' — doit être None ou float"
    )
    assert isinstance(diag_dict["debug"], dict), (
        f"{field_name}: debug doit être un dict, reçu {type(diag_dict['debug'])}"
    )


# ─── Tests centraux : build_all_diagnostics ───────────────────────────────────

def test_no_silent_na_on_critical_dashboard_fields_nominal():
    """Cas nominal : toutes données disponibles → tous champs diagnostiqués, aucun N/A."""
    result = build_all_diagnostics(
        gex_profile=_gex_profile(),
        snapshot=_snapshot(),
        gmap=_gmap(),
        walls_profile=_walls(),
        mopi_score=_mopi(),
        squeeze_score=_squeeze(),
        dp=_dp(),
        gex_calibration_cache=_calibration(),
    )

    for field in CRITICAL_FIELDS:
        assert field in result, f"Champ critique manquant dans build_all_diagnostics : {field}"
        _assert_diag_complete(result[field], field)


def test_no_silent_na_on_critical_dashboard_fields_all_unavailable():
    """Cas dégradé : même quand tout est unavailable, chaque champ a un diagnostic complet."""
    result = build_all_diagnostics(
        gex_profile=_gex_profile(flip_reason="no_near_gamma_sign_cross", flip_level=None),
        snapshot=_snapshot(),
        gmap=_gmap(magnet=0.0, explosive=0.0, zones=[]),
        walls_profile=_walls(call_wall=BTC_SPOT, put_wall=BTC_SPOT),
        mopi_score=_mopi(),
        squeeze_score=_squeeze(),
        dp=_dp(),
        gex_calibration_cache=_calibration(n_points=0),
    )

    for field in CRITICAL_FIELDS:
        assert field in result
        _assert_diag_complete(result[field], field)

    assert result["flip_level"]["status"] == "unavailable"
    assert result["flip_level"]["reason_code"] == "no_near_gamma_sign_cross"
    assert result["flip_level"]["value"] is None

    assert result["gravity_magnet"]["status"] == "unavailable"
    assert result["gravity_magnet"]["reason_code"] == "no_magnetic_zone"

    assert result["gravity_explosive"]["status"] == "unavailable"
    assert result["gravity_explosive"]["reason_code"] == "no_explosive_zone"

    assert result["wall_call"]["status"] == "unavailable"
    assert result["wall_call"]["reason_code"] == "no_calls_above_spot"

    assert result["wall_put"]["status"] == "unavailable"
    assert result["wall_put"]["reason_code"] == "no_puts_below_spot"

    # mopi / squeeze / dealer_pressure : toujours available même en mode dégradé
    assert result["mopi"]["status"] == "available"
    assert result["squeeze"]["status"] == "available"
    assert result["dealer_pressure"]["status"] == "available"


def test_build_all_diagnostics_covers_all_critical_fields():
    """build_all_diagnostics doit couvrir TOUS les champs critiques listés."""
    result = build_all_diagnostics(
        gex_profile=_gex_profile(),
        snapshot=_snapshot(),
        gmap=_gmap(),
        walls_profile=_walls(),
        mopi_score=_mopi(),
        squeeze_score=_squeeze(),
        dp=_dp(),
        gex_calibration_cache=_calibration(),
    )
    missing = set(CRITICAL_FIELDS) - set(result.keys())
    assert not missing, f"Champs critiques non couverts par build_all_diagnostics : {missing}"


# ─── Tests paramétrés : flip_level ───────────────────────────────────────────

@pytest.mark.parametrize("flip_reason,expected_status,flip_level", [
    ("crossing_near",             "available",   90_000.0),
    ("crossing_global",           "degraded",    88_000.0),
    ("no_near_gamma_sign_cross",  "unavailable", None),
    ("insufficient_near_strikes", "unavailable", None),
    ("all_gamma_negative",        "unavailable", None),
    ("all_gamma_positive",        "unavailable", None),
    ("quality_gate_dormant",      "unavailable", None),
    ("calculation_error",         "unavailable", None),
])
def test_diag_flip_level_no_silent_na(flip_reason, expected_status, flip_level):
    """flip_level : chaque reason_code produit un diagnostic complet, jamais N/A silencieux."""
    d = diag_flip_level(_gex_profile(flip_reason=flip_reason, flip_level=flip_level), _snapshot()).to_dict()
    _assert_diag_complete(d, "flip_level")
    assert d["status"] == expected_status
    assert d["reason_code"] == flip_reason
    if expected_status == "unavailable":
        assert d["value"] is None


# ─── Tests paramétrés : gravity ───────────────────────────────────────────────

@pytest.mark.parametrize("magnet,zones,expected_status,expected_reason", [
    (90_000.0,           [_zone("MAGNETIC")], "available",   "magnetic_zone_found"),
    (0.0,                [],                  "unavailable", "no_magnetic_zone"),
    (BTC_SPOT * 0.9995,  [_zone("MAGNETIC")], "unavailable", "no_magnetic_zone"),
])
def test_diag_gravity_magnet_no_silent_na(magnet, zones, expected_status, expected_reason):
    d = diag_gravity_magnet(_gmap(magnet=magnet, zones=zones)).to_dict()
    _assert_diag_complete(d, "gravity_magnet")
    assert d["status"] == expected_status
    assert d["reason_code"] == expected_reason


@pytest.mark.parametrize("explosive,zones,expected_status,expected_reason", [
    (100_000.0, [_zone("EXPLOSIVE")], "available",   "explosive_zone_found"),
    (0.0,       [],                   "unavailable", "no_explosive_zone"),
    (BTC_SPOT * 0.9995, [_zone("EXPLOSIVE")], "unavailable", "no_explosive_zone"),
])
def test_diag_gravity_explosive_no_silent_na(explosive, zones, expected_status, expected_reason):
    d = diag_gravity_explosive(_gmap(explosive=explosive, zones=zones)).to_dict()
    _assert_diag_complete(d, "gravity_explosive")
    assert d["status"] == expected_status
    assert d["reason_code"] == expected_reason


# ─── Tests paramétrés : walls ─────────────────────────────────────────────────

@pytest.mark.parametrize("call_wall,expected_status,expected_reason", [
    (100_000.0, "available",   "call_wall_found"),
    (BTC_SPOT,  "unavailable", "no_calls_above_spot"),
    (90_000.0,  "unavailable", "no_calls_above_spot"),
])
def test_diag_wall_call_no_silent_na(call_wall, expected_status, expected_reason):
    d = diag_wall_call(_walls(call_wall=call_wall), BTC_SPOT).to_dict()
    _assert_diag_complete(d, "wall_call")
    assert d["status"] == expected_status
    assert d["reason_code"] == expected_reason


@pytest.mark.parametrize("put_wall,expected_status,expected_reason", [
    (90_000.0,  "available",   "put_wall_found"),
    (BTC_SPOT,  "unavailable", "no_puts_below_spot"),
    (100_000.0, "unavailable", "no_puts_below_spot"),
])
def test_diag_wall_put_no_silent_na(put_wall, expected_status, expected_reason):
    d = diag_wall_put(_walls(put_wall=put_wall), BTC_SPOT).to_dict()
    _assert_diag_complete(d, "wall_put")
    assert d["status"] == expected_status
    assert d["reason_code"] == expected_reason


# ─── Tests : champs always_available ─────────────────────────────────────────

def test_diag_mopi_always_available_and_complete():
    d = diag_mopi(_mopi(score=65.0)).to_dict()
    _assert_diag_complete(d, "mopi")
    assert d["status"] == "available"
    assert d["reason_code"] == "always_available"
    assert d["value"] == 65.0
    assert "label" in d["debug"]
    assert "iv_rank" in d["debug"]


def test_diag_squeeze_always_available_and_complete():
    d = diag_squeeze(_squeeze(score=30.0)).to_dict()
    _assert_diag_complete(d, "squeeze")
    assert d["status"] == "available"
    assert d["reason_code"] == "always_available"
    assert d["value"] == 30.0
    assert "label" in d["debug"]
    assert "direction_bias" in d["debug"]


def test_diag_dealer_pressure_always_available_and_complete():
    d = diag_dealer_pressure(_dp(pressure_pct=60.0)).to_dict()
    _assert_diag_complete(d, "dealer_pressure")
    assert d["status"] == "available"
    assert d["reason_code"] == "always_available"
    assert d["value"] == 60.0
    assert "direction" in d["debug"]
    assert "intensity" in d["debug"]


# ─── Règles systémiques ───────────────────────────────────────────────────────

def test_unknown_reason_code_defaults_to_unavailable():
    """Un reason_code inconnu → unavailable, jamais None ou status invalide."""
    result = _status_for("totally_unknown_reason_xyz")
    assert result == "unavailable"
    assert result in VALID_STATUSES


def test_all_registered_reason_codes_map_to_valid_status():
    """Chaque reason_code dans _REASON_TO_STATUS produit un status valide."""
    for reason_code, status in _REASON_TO_STATUS.items():
        assert status in VALID_STATUSES, (
            f"reason_code '{reason_code}' → status '{status}' non valide"
        )


def test_field_diag_to_dict_always_has_value_key_even_when_none():
    """FieldDiag.to_dict() doit exposer 'value' même si None — jamais clé absente."""
    diag = FieldDiag(
        status="unavailable",
        reason_code="calculation_error",
        value=None,
        debug={},
    )
    d = diag.to_dict()
    assert REQUIRED_DIAG_KEYS == set(d.keys())
    assert "value" in d
    assert d["value"] is None


def test_field_diag_to_dict_never_string_na():
    """FieldDiag ne doit jamais contenir la string 'N/A' dans ses champs typés."""
    for reason, status in _REASON_TO_STATUS.items():
        diag = FieldDiag(status=status, reason_code=reason, value=None, debug={})
        d = diag.to_dict()
        assert d["status"] != "N/A"
        assert d["reason_code"] != "N/A"
        assert d["value"] != "N/A"


# ─── Guard frontend ───────────────────────────────────────────────────────────

def test_frontend_na_guard_backend_never_returns_na_string():
    """
    Règle frontend : le backend ne retourne JAMAIS la string 'N/A' dans un diagnostic.
    Le frontend doit afficher le reason_code (grisé si unavailable), jamais 'N/A' brut.
    Si ce test fail → N/A silencieux en prod.
    """
    scenarios = [
        dict(
            gex_profile=_gex_profile(flip_reason="no_near_gamma_sign_cross", flip_level=None),
            gmap=_gmap(magnet=0.0, explosive=0.0, zones=[]),
            walls_profile=_walls(call_wall=BTC_SPOT, put_wall=BTC_SPOT),
            gex_calibration_cache=_calibration(n_points=0),
        ),
        dict(
            gex_profile=_gex_profile(flip_reason="crossing_near", flip_level=90_000.0),
            gmap=_gmap(),
            walls_profile=_walls(),
            gex_calibration_cache=_calibration(),
        ),
        dict(
            gex_profile=_gex_profile(flip_reason="quality_gate_dormant", flip_level=None),
            gmap=_gmap(magnet=0.0, explosive=0.0, zones=[_zone("VOID")]),
            walls_profile=_walls(call_wall=BTC_SPOT * 1.0002, put_wall=BTC_SPOT),
            gex_calibration_cache=_calibration(saturation_rate_7d=0.45),
        ),
    ]

    for i, scenario in enumerate(scenarios):
        result = build_all_diagnostics(
            gex_profile=scenario["gex_profile"],
            snapshot=_snapshot(),
            gmap=scenario["gmap"],
            walls_profile=scenario["walls_profile"],
            mopi_score=_mopi(),
            squeeze_score=_squeeze(),
            dp=_dp(),
            gex_calibration_cache=scenario["gex_calibration_cache"],
        )
        for field, diag in result.items():
            assert diag.get("reason_code") != "N/A", (
                f"Scénario {i} / {field}: reason_code='N/A' — N/A silencieux frontend"
            )
            assert diag.get("status") != "N/A", (
                f"Scénario {i} / {field}: status='N/A'"
            )
            assert diag.get("value") != "N/A", (
                f"Scénario {i} / {field}: value='N/A' (string) — doit être None ou float"
            )


# ─── Tests : gex_calibration ─────────────────────────────────────────────────

@pytest.mark.parametrize("cal,expected_status,expected_reason", [
    # calibration disponible — dynamique, assez de points, pas d'alerte
    (_calibration(),                                                          "available",   "calibration_available"),
    # calibration absente — aucun historique (cold start)
    (_calibration(n_points=0),                                                "unavailable", "calibration_missing"),
    # calibration stale — données insuffisantes (< 48 points = 1 jour)
    (_calibration(n_points=_GEX_CAL_MIN_POINTS - 1),                         "stale",       "calibration_stale"),
    # calibration inconsistante — saturation trop haute
    (_calibration(saturation_rate_7d=_GEX_SAT_THRESHOLD + 0.01),             "degraded",    "calibration_inconsistent"),
    # calibration inconsistante — neutralisation trop haute
    (_calibration(neutralization_rate_7d=_GEX_NEU_THRESHOLD + 0.01),         "degraded",    "calibration_inconsistent"),
    # pile sur les seuils (valeurs exactes) — pas encore en alerte
    (_calibration(saturation_rate_7d=_GEX_SAT_THRESHOLD),                    "available",   "calibration_available"),
    (_calibration(neutralization_rate_7d=_GEX_NEU_THRESHOLD),                "available",   "calibration_available"),
    # dict vide — aucun historique
    ({},                                                                      "unavailable", "calibration_missing"),
])
def test_diag_gex_calibration_no_silent_na(cal, expected_status, expected_reason):
    """gex_calibration : chaque cas produit un diagnostic complet, jamais N/A silencieux."""
    d = diag_gex_calibration(cal).to_dict()
    _assert_diag_complete(d, "gex_calibration")
    assert d["status"] == expected_status, (
        f"attendu status={expected_status!r}, reçu {d['status']!r} (reason={d['reason_code']!r})"
    )
    assert d["reason_code"] == expected_reason


def test_diag_gex_calibration_available_has_cap_value():
    """En mode available, value = cap_value (float non-null)."""
    d = diag_gex_calibration(_calibration(cap_value=1_500_000_000)).to_dict()
    assert d["status"] == "available"
    assert d["value"] == 1_500_000_000.0
    assert isinstance(d["value"], float)


def test_diag_gex_calibration_missing_value_is_none():
    """En mode unavailable (calibration_missing), value doit être None."""
    d = diag_gex_calibration(_calibration(n_points=0)).to_dict()
    assert d["status"] == "unavailable"
    assert d["value"] is None


def test_diag_gex_calibration_error_fallback():
    """Un dict malformé déclenche calibration_error, jamais de crash ni de N/A."""
    bad_cal = {"cap_value": "not_a_number", "n_points": "also_bad"}
    d = diag_gex_calibration(bad_cal).to_dict()
    _assert_diag_complete(d, "gex_calibration")
    assert d["status"] == "unavailable"
    assert d["reason_code"] == "calibration_error"
    assert d["value"] is None
    assert "error" in d["debug"]


def test_diag_gex_calibration_reason_codes_in_reason_to_status():
    """Tous les reason_codes calibration sont enregistrés dans _REASON_TO_STATUS."""
    calibration_codes = [
        "calibration_available",
        "calibration_inconsistent",
        "calibration_stale",
        "calibration_missing",
        "calibration_error",
    ]
    for code in calibration_codes:
        assert code in _REASON_TO_STATUS, f"reason_code '{code}' absent de _REASON_TO_STATUS"
        assert _REASON_TO_STATUS[code] in {"available", "degraded", "unavailable", "stale"}, (
            f"reason_code '{code}' → status invalide '{_REASON_TO_STATUS[code]}'"
        )


def test_gex_calibration_always_present_in_build_all_diagnostics():
    """_fieldDiagnostics.gex_calibration doit toujours être présent — même sans cache."""
    result = build_all_diagnostics(
        gex_profile=_gex_profile(),
        snapshot=_snapshot(),
        gmap=_gmap(),
        walls_profile=_walls(),
        mopi_score=_mopi(),
        squeeze_score=_squeeze(),
        dp=_dp(),
        # pas de gex_calibration_cache → doit fallback sur calibration_missing
    )
    assert "gex_calibration" in result
    _assert_diag_complete(result["gex_calibration"], "gex_calibration")
    # sans cache → cold start → unavailable
    assert result["gex_calibration"]["status"] == "unavailable"
    assert result["gex_calibration"]["reason_code"] == "calibration_missing"
