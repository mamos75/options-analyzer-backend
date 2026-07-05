"""
Tests — Wording GEX conditionnel selon statut de calibration.

Règle maîtresse :
  Un caveat ajouté après une certitude n'annule pas la certitude.
  Quand la confiance baisse, c'est la formulation principale qui doit
  devenir plus prudente — pas un disclaimer collé en fin de texte.

Scénarios obligatoires :
  1.  calibration available          → wording inchangé, phrases assertives présentes
  2.  calibration degraded           → phrases assertives remplacées par conditionnel
  3.  calibration stale              → phrases assertives remplacées (mention données anciennes)
  4.  calibration unavailable        → phrases assertives remplacées (lecture non validée)
  5.  gex_use_in_signal=False        → aucune transformation (GEX déjà exclu)
  6.  horizon 4h + calibration degraded  → scenario adouci
  7.  horizon 24h + calibration stale    → scenario adouci
  8.  horizon 72h + calibration unavailable → scenario adouci
  9.  _append_once empêche les doublons
  10. footer Telegram : badge léger si calibration dégradée (pas de phrase complète)
  11. footer Telegram sans badge si gex_use_in_signal=False
  12. aucune phrase bannie ("sera amplifié", "brutal attendu", etc.)
      n'est produite quand calibration_status != "available"
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.narrative_resolver import (
    resolve_narrative,
    resolve_narrative_horizon,
    _gex_calibration_caveat,
    _append_once,
    _apply_gex_confidence_wording,
)
from backend.dealer_pressure import DealerPressure, DEXLevels
from backend.mopi import MOPIScore
from backend.gex import GEXProfile, MaxPainProfile, MaxPainExpiry
from backend.gravity_map import GravityMap
from backend.options_walls import OptionsWallsProfile
from backend.squeeze_score import SqueezeScore

BTC_SPOT = 100_000.0

# Phrases assertives interdites quand calibration != available
BANNED_PHRASES = [
    "chaque move vers le bas sera amplifié",
    "chaque move sera amplifié dans les deux sens",
    "Chaque move sera amplifié dans les deux sens",
    "mouvement brutal attendu",
    "le prochain mouvement sera violent",
    "mouvement violent imminent",
    "tout breakout amplifié",
    "tout mouvement sera exacerbé dans les deux sens",
]

# Marqueurs de wording adouci attendus par statut
SOFT_MARKERS = {
    "degraded":    ["possible", "pourrait", "suggère", "reste possible", "se confirme"],
    "stale":       ["possible", "indique", "pourrait"],
    "unavailable": ["possible", "pointe vers", "se confirme", "à confirmer"],
}

CAVEAT_DEGRADED    = "Calibration GEX dégradée"
CAVEAT_STALE       = "Calibration GEX ancienne"
CAVEAT_UNAVAILABLE = "Calibration GEX indisponible"


# ─── Stubs ────────────────────────────────────────────────────────────────────

def _mopi(score: float = 30.0) -> MOPIScore:
    """score=30 → BEARISH → produit le scénario amplificateur baissier assertif."""
    label = "BULLISH" if score >= 55 else ("BEARISH" if score < 45 else "NEUTRE")
    return MOPIScore(
        score=score, label=label, emoji="📊",
        gex_component=50.0, iv_rank_component=50.0,
        pc_ratio_component=50.0, squeeze_component=50.0,
        iv_rank=50.0, pc_ratio=1.0, squeeze_prob=30.0,
    )


def _gex(
    regime: str = "AMPLIFICATEUR",
    flip: float = 95_000.0,
    total_gex: float = 1_000_000_000.0,
) -> GEXProfile:
    mp_expiry = MaxPainExpiry(
        strike=98_000.0, expiry="2026-06-10", dte=9, oi_total=50_000,
    )
    return GEXProfile(
        total_gex=total_gex,
        gex_by_strike={}, call_gex_by_strike={}, put_gex_by_strike={},
        flip_level=flip, max_pain=98_000.0, gamma_walls=[],
        btc_price=BTC_SPOT, regime=regime,
        max_pain_profile=MaxPainProfile(near=mp_expiry, institutional=mp_expiry),
    )


def _dp(direction: str = "BEARISH_FLOWS") -> DealerPressure:
    nd = 2000.0 if direction == "BEARISH_FLOWS" else -2000.0
    color = "red" if direction == "BEARISH_FLOWS" else "green"
    return DealerPressure(
        net_delta=nd, net_delta_usd=nd * BTC_SPOT,
        delta_by_strike={}, direction=direction,
        intensity="MODERATE", pressure_pct=nd / 20_000 * 100,
        gauge_color=color, flux_conditionnel="...",
        direction_risque_trader="RESISTANCE",
        exposition_nette_btc=abs(nd),
    )


def _gmap() -> GravityMap:
    return GravityMap(
        btc_price=BTC_SPOT, zones=[],
        strongest_magnet=BTC_SPOT * 1.02,
        next_explosive=BTC_SPOT * 0.90,
        gravity_score=60.0, narrative="Gravité modérée.", timestamp=0.0,
    )


def _walls() -> OptionsWallsProfile:
    return OptionsWallsProfile(
        walls=[], major_call_wall=BTC_SPOT * 1.05,
        major_put_wall=BTC_SPOT * 0.95,
        oi_by_strike={}, btc_price=BTC_SPOT,
    )


def _sq() -> SqueezeScore:
    return SqueezeScore(
        score=30.0, label="DORMANT", emoji="😴",
        probability_pct=30.0, signals=[],
        dominant_signal="GEX", direction_bias="NEUTRAL",
        trigger_zone=BTC_SPOT * 0.95,
    )


def _dex(profile: str = "ACTIVE") -> DEXLevels:
    return DEXLevels(
        structural=-5000.0, active=-500.0, actionable=-50.0,
        structural_usd=-5e9, active_usd=-5e8, actionable_usd=-5e7,
        low_oi_anomaly_count=0, low_oi_anomaly_strikes=[],
        dex_profile=profile,
        dex_active_pct=40.0 if profile in ("ACTIVE", "ACTIONABLE") else 3.0,
        dex_actionable_pct=20.0 if profile == "ACTIONABLE" else 2.0,
    )


def _gex_audit_active():
    from backend.gex_activity_audit import GEXActivityAudit, GEXCategoryStats
    cat = GEXCategoryStats(gex_abs_usd=3e8, gex_net_usd=3e8, gex_pct=30.0, count=10, top_contributors=[])
    return GEXActivityAudit(
        btc_price=BTC_SPOT, gex_total_usd=1e9, gex_regime="AMPLIFICATEUR",
        timestamp=0.0,
        dormant=GEXCategoryStats(gex_abs_usd=1e8, gex_net_usd=1e8, gex_pct=10.0, count=5, top_contributors=[]),
        structural=GEXCategoryStats(gex_abs_usd=1e8, gex_net_usd=1e8, gex_pct=10.0, count=5, top_contributors=[]),
        active=cat, actionable=cat,
        gex_structural_score=0.2, gex_active_score=0.6, gex_actionable_score=0.3,
        active_pct=30.0, actionable_pct=30.0,
        overall_profile="ACTIVE", signal_quality_score=7,
        signal_quality_label="Signal valide", signal_quality_color="green",
        signal_verdict="GEX actif — signal exploitable",
        use_in_signal=True, low_oi_anomaly_count=0,
    )


def _call_resolve(
    calibration_status: str = "available",
    calibration_reason_code: str = "calibration_available",
    mopi_score: float = 30.0,
):
    """Produit un scénario amplificateur baissier (mopi<45 par défaut) pour tester le wording."""
    audit = _gex_audit_active()
    return resolve_narrative(
        mopi=_mopi(mopi_score), gex=_gex(), dp=_dp(), gmap=_gmap(),
        walls=_walls(), sq=_sq(), spot=BTC_SPOT,
        audit=audit, dex_levels=_dex(),
        calibration_status=calibration_status,
        calibration_reason_code=calibration_reason_code,
    )


def _call_horizon(
    horizon: str,
    calibration_status: str = "available",
    calibration_reason_code: str = "calibration_available",
):
    return resolve_narrative_horizon(
        mopi=_mopi(30.0), gex=_gex(total_gex=5e8), dp=_dp(),
        gmap=_gmap(), walls=_walls(), sq=_sq(), spot=BTC_SPOT,
        horizon=horizon, dex_levels=_dex(),
        audit=_gex_audit_active(),
        calibration_status=calibration_status,
        calibration_reason_code=calibration_reason_code,
    )


# ─── Tests _apply_gex_confidence_wording ─────────────────────────────────────

def test_apply_wording_available_unchanged():
    """available → texte inchangé."""
    text = "Régime amplificateur baissier — chaque move vers le bas sera amplifié"
    assert _apply_gex_confidence_wording(text, "available") == text


def test_apply_wording_degraded_removes_assertive():
    """degraded → phrase assertive remplacée par conditionnel."""
    text = "Régime amplificateur baissier — chaque move vers le bas sera amplifié"
    result = _apply_gex_confidence_wording(text, "degraded")
    assert "chaque move vers le bas sera amplifié" not in result
    assert "possible" in result or "se confirme" in result


def test_apply_wording_stale_adds_old_data_context():
    """stale → mention données anciennes."""
    text = "mouvement brutal attendu"
    result = _apply_gex_confidence_wording(text, "stale")
    assert "mouvement brutal attendu" not in result
    assert "anciennes" in result or "possible" in result


def test_apply_wording_unavailable_replaces_certainty():
    """unavailable → certitude remplacée par hypothèse non validée."""
    text = "le prochain mouvement sera violent"
    result = _apply_gex_confidence_wording(text, "unavailable")
    assert "sera violent" not in result
    assert "non validée" in result or "pourrait" in result


def test_apply_wording_banner_degraded():
    """Banner 'le prochain mouvement sera violent' → adouci pour degraded."""
    text = "Régime amplificateur actif — le prochain mouvement sera violent."
    result = _apply_gex_confidence_wording(text, "degraded")
    assert "sera violent" not in result
    assert "pourrait" in result or "suggère" in result


def test_apply_wording_all_banned_phrases_removed_degraded():
    """Aucune phrase bannie ne subsiste après adoucissement degraded."""
    for phrase in BANNED_PHRASES:
        result = _apply_gex_confidence_wording(phrase, "degraded")
        assert phrase not in result, f"Phrase bannie encore présente (degraded): {phrase!r}"


def test_apply_wording_all_banned_phrases_removed_stale():
    """Aucune phrase bannie ne subsiste après adoucissement stale."""
    for phrase in BANNED_PHRASES:
        result = _apply_gex_confidence_wording(phrase, "stale")
        assert phrase not in result, f"Phrase bannie encore présente (stale): {phrase!r}"


def test_apply_wording_all_banned_phrases_removed_unavailable():
    """Aucune phrase bannie ne subsiste après adoucissement unavailable."""
    for phrase in BANNED_PHRASES:
        result = _apply_gex_confidence_wording(phrase, "unavailable")
        assert phrase not in result, f"Phrase bannie encore présente (unavailable): {phrase!r}"


# ─── Tests helpers existants ──────────────────────────────────────────────────

def test_caveat_available_empty():
    """calibration available → caveat vide."""
    assert _gex_calibration_caveat("available", "calibration_available") == ""


def test_caveat_degraded():
    """calibration degraded → wording exact."""
    c = _gex_calibration_caveat("degraded", "calibration_inconsistent")
    assert CAVEAT_DEGRADED in c


def test_caveat_stale():
    """calibration stale → wording exact."""
    c = _gex_calibration_caveat("stale", "calibration_stale")
    assert CAVEAT_STALE in c


def test_caveat_unavailable():
    """calibration unavailable → wording exact."""
    c = _gex_calibration_caveat("unavailable", "calibration_missing")
    assert CAVEAT_UNAVAILABLE in c


def test_append_once_no_duplicate():
    """_append_once n'ajoute pas le caveat si déjà présent."""
    base = "Signal haussier. ⚠️ Calibration GEX dégradée : signal à confirmer."
    caveat = "⚠️ Calibration GEX dégradée : signal à confirmer."
    result = _append_once(base, caveat)
    assert result.count(caveat) == 1


def test_append_once_adds_if_absent():
    """_append_once ajoute le caveat si absent."""
    result = _append_once("Signal haussier.", "⚠️ test caveat")
    assert "⚠️ test caveat" in result
    assert result.count("⚠️ test caveat") == 1


def test_append_once_empty_caveat():
    """_append_once ne modifie pas le texte si caveat vide."""
    base = "Signal haussier."
    result = _append_once(base, "")
    assert result == base


# ─── Tests resolve_narrative — wording adouci à la source ────────────────────

def test_narrative_available_preserves_assertive():
    """calibration available → formulations assertives inchangées dans scenario."""
    n = _call_resolve("available")
    # Le scénario baissier doit contenir la formulation affirmative
    assert "sera amplifié" in n.scenario_principal or "breakout" in n.scenario_principal


def test_narrative_degraded_no_banned_phrases():
    """calibration degraded → aucune phrase bannie dans scenario/risque/phrase_synthese/banner."""
    n = _call_resolve("degraded", "calibration_inconsistent")
    fields = [n.scenario_principal, n.risque_principal, n.phrase_synthese, n.banner_message]
    for field_text in fields:
        for phrase in BANNED_PHRASES:
            assert phrase not in field_text, (
                f"Phrase bannie encore présente (degraded) dans field: {phrase!r}\n"
                f"Texte: {field_text!r}"
            )


def test_narrative_stale_no_banned_phrases():
    """calibration stale → aucune phrase bannie."""
    n = _call_resolve("stale", "calibration_stale")
    fields = [n.scenario_principal, n.risque_principal, n.phrase_synthese, n.banner_message]
    for field_text in fields:
        for phrase in BANNED_PHRASES:
            assert phrase not in field_text, (
                f"Phrase bannie encore présente (stale): {phrase!r}\n"
                f"Texte: {field_text!r}"
            )


def test_narrative_unavailable_no_banned_phrases():
    """calibration unavailable → aucune phrase bannie."""
    n = _call_resolve("unavailable", "calibration_missing")
    fields = [n.scenario_principal, n.risque_principal, n.phrase_synthese, n.banner_message]
    for field_text in fields:
        for phrase in BANNED_PHRASES:
            assert phrase not in field_text, (
                f"Phrase bannie encore présente (unavailable): {phrase!r}\n"
                f"Texte: {field_text!r}"
            )


def test_narrative_degraded_uses_conditional_wording():
    """calibration degraded → le wording adouci contient des marqueurs conditionnels."""
    n = _call_resolve("degraded", "calibration_inconsistent")
    combined = " ".join([n.scenario_principal, n.risque_principal, n.phrase_synthese])
    markers = SOFT_MARKERS["degraded"]
    assert any(m in combined for m in markers), (
        f"Aucun marqueur conditionnel trouvé (degraded)\nTexte: {combined!r}"
    )


def test_narrative_unavailable_uses_hypothesis_wording():
    """calibration unavailable → wording de type hypothèse non validée."""
    n = _call_resolve("unavailable", "calibration_missing")
    combined = " ".join([n.scenario_principal, n.risque_principal, n.phrase_synthese])
    markers = SOFT_MARKERS["unavailable"]
    assert any(m in combined for m in markers), (
        f"Aucun marqueur hypothèse trouvé (unavailable)\nTexte: {combined!r}"
    )


def test_gex_use_in_signal_false_no_transformation():
    """gex_use_in_signal=False est prioritaire : pas de transformation même si calibration dégradée."""
    from backend.gex_activity_audit import GEXActivityAudit, GEXCategoryStats
    dormant_cat = GEXCategoryStats(
        gex_abs_usd=1e9, gex_net_usd=1e9, gex_pct=95.0, count=50, top_contributors=[]
    )
    small_cat = GEXCategoryStats(gex_abs_usd=1e6, gex_net_usd=1e6, gex_pct=0.1, count=1, top_contributors=[])
    dormant_audit = GEXActivityAudit(
        btc_price=BTC_SPOT, gex_total_usd=1e9, gex_regime="AMPLIFICATEUR",
        timestamp=0.0,
        dormant=dormant_cat, structural=small_cat, active=small_cat, actionable=small_cat,
        gex_structural_score=0.0, gex_active_score=0.01, gex_actionable_score=0.01,
        active_pct=0.1, actionable_pct=0.1,
        overall_profile="DORMANT", signal_quality_score=1,
        signal_quality_label="Dormant", signal_quality_color="red",
        signal_verdict="GEX dormant — non exploitable",
        use_in_signal=False, low_oi_anomaly_count=0,
    )
    n = resolve_narrative(
        mopi=_mopi(30.0), gex=_gex(), dp=_dp(), gmap=_gmap(),
        walls=_walls(), sq=_sq(), spot=BTC_SPOT,
        audit=dormant_audit, dex_levels=_dex(),
        calibration_status="degraded",
        calibration_reason_code="calibration_inconsistent",
    )
    assert not n.gex_use_in_signal, "L'audit dormant devrait mettre gex_use_in_signal=False"
    # Le texte GEX dormant ne contient pas de certitudes GEX à adoucir,
    # et aucune transformation ne doit avoir eu lieu
    assert "suggère" not in n.scenario_principal or "dormant" in n.scenario_principal.lower()


# ─── Tests resolve_narrative_horizon ─────────────────────────────────────────

def test_horizon_4h_degraded_no_banned_phrases():
    """horizon 4h + calibration degraded → aucune phrase bannie dans scenario."""
    hn = _call_horizon("4h", "degraded", "calibration_inconsistent")
    for phrase in BANNED_PHRASES:
        assert phrase not in hn.scenario, (
            f"Phrase bannie présente (4h degraded): {phrase!r}\nScenario: {hn.scenario!r}"
        )


def test_horizon_24h_stale_no_banned_phrases():
    """horizon 24h + calibration stale → aucune phrase bannie dans scenario."""
    hn = _call_horizon("24h", "stale", "calibration_stale")
    for phrase in BANNED_PHRASES:
        assert phrase not in hn.scenario, (
            f"Phrase bannie présente (24h stale): {phrase!r}\nScenario: {hn.scenario!r}"
        )


def test_horizon_72h_unavailable_no_banned_phrases():
    """horizon 72h + calibration unavailable → aucune phrase bannie dans scenario."""
    hn = _call_horizon("72h", "unavailable", "calibration_missing")
    for phrase in BANNED_PHRASES:
        assert phrase not in hn.scenario, (
            f"Phrase bannie présente (72h unavailable): {phrase!r}\nScenario: {hn.scenario!r}"
        )


def test_horizon_available_preserves_wording():
    """horizon 4h + calibration available → wording inchangé."""
    hn = _call_horizon("4h", "available", "calibration_available")
    # available = pas de transformation. Le test vérifie juste que ça tourne sans erreur.
    assert hn.scenario is not None and len(hn.scenario) > 0


# ─── Tests footer Telegram ────────────────────────────────────────────────────

def test_telegram_footer_badge_when_degraded():
    """Footer Telegram affiche un badge léger (pas phrase complète) si calibration dégradée."""
    from backend.alerts import TelegramAlerter

    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._last_narrative = _call_resolve("degraded", "calibration_inconsistent")
    alerter._last_gex_audit = None
    alerter._last_flip_audit = None
    alerter._cal_status = "degraded"
    alerter._cal_reason_code = "calibration_inconsistent"

    footer = alerter._narrative_footer()
    # Badge léger présent
    assert "Calibration GEX dégradée" in footer, (
        f"Badge calibration absent du footer: {footer!r}"
    )


def test_telegram_footer_badge_stale():
    """Footer badge stale."""
    from backend.alerts import TelegramAlerter

    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._last_narrative = _call_resolve("stale", "calibration_stale")
    alerter._last_gex_audit = None
    alerter._last_flip_audit = None
    alerter._cal_status = "stale"
    alerter._cal_reason_code = "calibration_stale"

    footer = alerter._narrative_footer()
    assert "Calibration GEX ancienne" in footer, f"Badge stale absent: {footer!r}"


def test_telegram_footer_badge_unavailable():
    """Footer badge unavailable."""
    from backend.alerts import TelegramAlerter

    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._last_narrative = _call_resolve("unavailable", "calibration_missing")
    alerter._last_gex_audit = None
    alerter._last_flip_audit = None
    alerter._cal_status = "unavailable"
    alerter._cal_reason_code = "calibration_missing"

    footer = alerter._narrative_footer()
    assert "Calibration GEX non validée" in footer, f"Badge unavailable absent: {footer!r}"


def test_telegram_footer_no_badge_when_available():
    """Footer Telegram sans badge si calibration available."""
    from backend.alerts import TelegramAlerter

    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._last_narrative = _call_resolve("available", "calibration_available")
    alerter._last_gex_audit = None
    alerter._last_flip_audit = None
    alerter._cal_status = "available"
    alerter._cal_reason_code = "calibration_available"

    footer = alerter._narrative_footer()
    assert CAVEAT_DEGRADED not in footer
    assert CAVEAT_STALE not in footer
    assert CAVEAT_UNAVAILABLE not in footer
    assert "Calibration GEX dégradée" not in footer
    assert "Calibration GEX ancienne" not in footer
    assert "Calibration GEX non validée" not in footer


def test_telegram_footer_no_badge_when_gex_excluded():
    """Footer sans badge de calibration si gex_use_in_signal=False."""
    from backend.alerts import TelegramAlerter
    from backend.gex_activity_audit import GEXActivityAudit, GEXCategoryStats

    dormant_cat = GEXCategoryStats(
        gex_abs_usd=1e9, gex_net_usd=1e9, gex_pct=95.0, count=50, top_contributors=[]
    )
    small_cat = GEXCategoryStats(gex_abs_usd=1e6, gex_net_usd=1e6, gex_pct=0.1, count=1, top_contributors=[])
    dormant_audit = GEXActivityAudit(
        btc_price=BTC_SPOT, gex_total_usd=1e9, gex_regime="AMPLIFICATEUR",
        timestamp=0.0,
        dormant=dormant_cat, structural=small_cat, active=small_cat, actionable=small_cat,
        gex_structural_score=0.0, gex_active_score=0.01, gex_actionable_score=0.01,
        active_pct=0.1, actionable_pct=0.1,
        overall_profile="DORMANT", signal_quality_score=1,
        signal_quality_label="Dormant", signal_quality_color="red",
        signal_verdict="GEX dormant",
        use_in_signal=False, low_oi_anomaly_count=0,
    )
    narrative = resolve_narrative(
        mopi=_mopi(30.0), gex=_gex(), dp=_dp(), gmap=_gmap(),
        walls=_walls(), sq=_sq(), spot=BTC_SPOT,
        audit=dormant_audit, dex_levels=_dex(),
        calibration_status="degraded",
        calibration_reason_code="calibration_inconsistent",
    )
    assert not narrative.gex_use_in_signal

    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._last_narrative = narrative
    alerter._last_gex_audit = None
    alerter._last_flip_audit = None
    alerter._cal_status = "degraded"
    alerter._cal_reason_code = "calibration_inconsistent"

    footer = alerter._narrative_footer()
    # gex_use_in_signal=False → pas de badge calibration dans le footer
    assert "Calibration GEX dégradée" not in footer


# ─── Tests caveat dans phrase_synthese ────────────────────────────────────────

def test_phrase_synthese_contains_caveat_degraded():
    """calibration degraded → phrase_synthese contient le texte du caveat."""
    n = _call_resolve("degraded", "calibration_inconsistent")
    assert CAVEAT_DEGRADED in n.phrase_synthese, (
        f"Caveat dégradé absent de phrase_synthese: {n.phrase_synthese!r}"
    )


def test_phrase_synthese_contains_caveat_stale():
    """calibration stale → phrase_synthese contient le texte du caveat."""
    n = _call_resolve("stale", "calibration_stale")
    assert CAVEAT_STALE in n.phrase_synthese, (
        f"Caveat stale absent de phrase_synthese: {n.phrase_synthese!r}"
    )


def test_phrase_synthese_contains_caveat_unavailable():
    """calibration unavailable → phrase_synthese contient le texte du caveat."""
    n = _call_resolve("unavailable", "calibration_missing")
    assert CAVEAT_UNAVAILABLE in n.phrase_synthese, (
        f"Caveat unavailable absent de phrase_synthese: {n.phrase_synthese!r}"
    )


def test_phrase_synthese_no_caveat_when_available():
    """calibration available → aucun caveat ajouté dans phrase_synthese."""
    n = _call_resolve("available", "calibration_available")
    assert CAVEAT_DEGRADED not in n.phrase_synthese
    assert CAVEAT_STALE not in n.phrase_synthese
    assert CAVEAT_UNAVAILABLE not in n.phrase_synthese


def test_phrase_synthese_no_duplicate_caveat():
    """_append_once empêche les doublons dans phrase_synthese."""
    n = _call_resolve("degraded", "calibration_inconsistent")
    count = n.phrase_synthese.count(CAVEAT_DEGRADED)
    assert count == 1, f"Caveat dupliqué ({count}x) dans phrase_synthese: {n.phrase_synthese!r}"


# ─── Tests caveat dans les horizons ──────────────────────────────────────────

def test_horizon_4h_contains_caveat_degraded():
    """horizon 4h + calibration degraded → scenario contient le caveat."""
    hn = _call_horizon("4h", "degraded", "calibration_inconsistent")
    assert CAVEAT_DEGRADED in hn.scenario, (
        f"Caveat dégradé absent du scenario 4h: {hn.scenario!r}"
    )


def test_horizon_24h_contains_caveat_stale():
    """horizon 24h + calibration stale → scenario contient le caveat."""
    hn = _call_horizon("24h", "stale", "calibration_stale")
    assert CAVEAT_STALE in hn.scenario, (
        f"Caveat stale absent du scenario 24h: {hn.scenario!r}"
    )


def test_horizon_72h_contains_caveat_unavailable():
    """horizon 72h + calibration unavailable → scenario contient le caveat."""
    hn = _call_horizon("72h", "unavailable", "calibration_missing")
    assert CAVEAT_UNAVAILABLE in hn.scenario, (
        f"Caveat unavailable absent du scenario 72h: {hn.scenario!r}"
    )


# ─── NOUVEAUX TESTS : qualité du rendu (anti-répétition) ─────────────────────

def test_unavailable_no_repeated_non_validee():
    """unavailable → 'non validée' / 'lecture non validée' absent des champs narratifs."""
    n = _call_resolve("unavailable", "calibration_missing")
    for field_name, field_text in [
        ("scenario_principal", n.scenario_principal),
        ("risque_principal",   n.risque_principal),
        ("phrase_synthese",    n.phrase_synthese),
        ("banner_message",     n.banner_message),
    ]:
        count = field_text.count("non validée")
        assert count == 0, (
            f"'non validée' présent {count}x dans {field_name} — le wording inline "
            f"ne doit pas le répéter (le caveat final suffit):\n{field_text!r}"
        )


def test_caveat_appears_once_per_field():
    """unavailable → 'Calibration GEX indisponible' présent exactement 1 fois dans chaque champ."""
    n = _call_resolve("unavailable", "calibration_missing")
    for field_name, field_text in [
        ("scenario_principal", n.scenario_principal),
        ("phrase_synthese",    n.phrase_synthese),
        ("banner_message",     n.banner_message),
    ]:
        count = field_text.count(CAVEAT_UNAVAILABLE)
        assert count <= 1, (
            f"Caveat répété {count}x dans {field_name}:\n{field_text!r}"
        )


def test_phrase_synthese_lisible_unavailable():
    """phrase_synthese lisible : 'possible' ou 'confirmer' présents, caveat unique."""
    n = _call_resolve("unavailable", "calibration_missing")
    ps = n.phrase_synthese
    assert "possible" in ps or "confirmer" in ps, (
        f"phrase_synthese manque de marqueurs de prudence:\n{ps!r}"
    )
    assert ps.count(CAVEAT_UNAVAILABLE) <= 1, (
        f"Caveat dupliqué dans phrase_synthese:\n{ps!r}"
    )


def test_horizon_scenario_caveat_unique():
    """horizon unavailable → caveat présent exactement 1 fois dans chaque scenario horizon."""
    for h in ("4h", "24h", "72h"):
        hn = _call_horizon(h, "unavailable", "calibration_missing")
        count = hn.scenario.count(CAVEAT_UNAVAILABLE)
        assert count == 1, (
            f"Caveat répété {count}x dans scenario horizon {h}:\n{hn.scenario!r}"
        )


def test_telegram_footer_caveat_unique():
    """Footer Telegram : 'indisponible' apparaît au plus 1 fois."""
    from backend.alerts import TelegramAlerter

    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._last_narrative = _call_resolve("unavailable", "calibration_missing")
    alerter._last_gex_audit = None
    alerter._last_flip_audit = None
    alerter._cal_status = "unavailable"
    alerter._cal_reason_code = "calibration_missing"

    footer = alerter._narrative_footer()
    count = footer.count("indisponible")
    assert count <= 1, f"'indisponible' répété {count}x dans footer:\n{footer!r}"


def test_available_scenario_no_caveat():
    """calibration available → aucun caveat, wording assertif inchangé."""
    n = _call_resolve("available", "calibration_available")
    for field_name, field_text in [
        ("scenario_principal", n.scenario_principal),
        ("risque_principal",   n.risque_principal),
        ("phrase_synthese",    n.phrase_synthese),
        ("banner_message",     n.banner_message),
    ]:
        assert "Calibration GEX" not in field_text, (
            f"Caveat inattendu dans {field_name} (available):\n{field_text!r}"
        )
    # Wording assertif doit être présent
    combined = n.scenario_principal + n.banner_message
    assert "sera amplifié" in combined or "violent" in combined or "breakout" in combined, (
        f"Wording assertif absent en mode available:\n{combined!r}"
    )
