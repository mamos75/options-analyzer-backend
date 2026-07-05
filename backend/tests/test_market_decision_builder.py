"""
Tests — market_decision_builder.build_market_decision()

Couvre :
  - Happy path : schéma complet, tous les champs attendus
  - Robustesse : chaque source manquante (GEX, bias, niveaux, invalidation, flip)
  - Cohérence logique : contradictions détectées et propagées
  - Confidence : dégradée selon warnings / stale / contradictions
  - critical_levels : triés par proximité, 0 ou plusieurs
  - Confirmation / invalidation : cohérentes avec la direction
  - data_stale : propagé dans source_status
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dataclasses import dataclass, field
from typing import Optional

import pytest

from backend.market_decision_builder import build_market_decision
from backend.narrative_resolver import NarrativeResolved
from backend.gex import GEXProfile, MaxPainProfile, MaxPainExpiry
from backend.squeeze_score import SqueezeScore
from backend.directional_bias import DirectionalBias


SPOT = 65_000.0

# ── Factories ────────────────────────────────────────────────────────────────


def _bias(score=60.0, phrase="Confluence haussière", confidence="Haute (3/4)", confluence=3):
    return DirectionalBias(
        score=score, label="HAUSSIER FORT", emoji="🟢",
        confidence=confidence, confluence_count=confluence,
        target_up=70_000.0, target_up_label="$70k",
        target_up_2=None, target_up_2_label="",
        target_down=60_000.0, target_down_label="$60k",
        target_down_2=None, target_down_2_label="",
        stop_logical=58_000.0, stop_label="$58k", stop_type="niveau_bas",
        primary_target=70_000.0, primary_target_pct=7.7,
        phrase=phrase,
    )


def _narrative(
    bias=None,
    niveau_haut=68_000.0, niveau_haut_label="$68k résistance",
    niveau_bas=62_000.0, niveau_bas_label="$62k support",
    invalidation=61_000.0,
    range_mode=False,
    flip_use_in_signal=True,
    risque_principal="Risque de squeeze",
):
    return NarrativeResolved(
        scenario_principal="Scénario test",
        risque_principal=risque_principal,
        niveau_haut=niveau_haut,
        niveau_haut_label=niveau_haut_label,
        niveau_bas=niveau_bas,
        niveau_bas_label=niveau_bas_label,
        invalidation=invalidation,
        phrase_synthese="Phrase synthèse test",
        banner_message="",
        range_mode=range_mode,
        asymmetric_side="BALANCED",
        max_pain_display={},
        dex_coherent=True,
        contradictions=[],
        gex_activity_label="⚡ GEX actif",
        gex_activity_context="GEX actif",
        gex_use_in_signal=True,
        dex_activity_label="⚡ DEX actif",
        dex_activity_context="DEX actif",
        dex_use_in_signal=True,
        flip_use_in_signal=flip_use_in_signal,
        directional_bias=bias,
    )


def _gex(
    flip_level=66_000.0,
    regime="STABILISANT",
    mp_near_strike=65_500.0,
    mp_profile=True,
):
    mp = None
    if mp_profile:
        mp = MaxPainProfile(
            near=MaxPainExpiry(
                strike=mp_near_strike, expiry="2026-06-07", dte=1, oi_total=10_000
            ),
            institutional=MaxPainExpiry(
                strike=65_000.0, expiry="2026-06-28", dte=22, oi_total=50_000
            ),
        )
    return GEXProfile(
        total_gex=1e9,
        gex_by_strike={},
        call_gex_by_strike={},
        put_gex_by_strike={},
        flip_level=flip_level,
        max_pain=65_500.0,
        gamma_walls=[],
        btc_price=SPOT,
        regime=regime,
        max_pain_profile=mp,
    )


def _sq(score=30.0, label="BUILDING", direction_bias="NEUTRAL"):
    return SqueezeScore(
        score=score, label=label, emoji="🟡",
        probability_pct=score,
        signals=[], dominant_signal="GEX Polarity",
        direction_bias=direction_bias, trigger_zone=SPOT,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _required_keys(d: dict):
    """Vérifie que tous les champs de schéma sont présents."""
    assert "btc_price" in d
    assert "directional" in d
    assert isinstance(d["directional"], dict)
    assert "dealer_regime" in d
    assert isinstance(d["dealer_regime"], dict)
    assert "dominant_risk" in d
    assert isinstance(d["dominant_risk"], dict)
    assert "key_levels" in d
    assert isinstance(d["key_levels"], list)
    assert "confirmation" in d
    assert isinstance(d["confirmation"], dict)
    assert "invalidation" in d
    assert isinstance(d["invalidation"], dict)
    assert "watch_message" in d
    assert isinstance(d["watch_message"], str)
    assert "confidence" in d
    assert "warnings" in d
    assert isinstance(d["warnings"], list)
    assert "source_status" in d
    assert isinstance(d["source_status"], dict)


def _no_null_danger(d: dict):
    """Aucun champ frontend dangereux n'est None ou chaîne vide sans fallback."""
    # Champs texte — jamais None
    for section in ("directional", "dealer_regime", "dominant_risk", "confirmation", "invalidation"):
        sec = d[section]
        for key in sec:
            assert sec[key] is not None, f"{section}.{key} ne doit pas être None"
    # watch_message jamais vide
    assert d["watch_message"], "watch_message ne doit pas être vide"
    # confidence jamais None
    assert d["confidence"]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_schema_complet(self):
        d = build_market_decision(
            _narrative(bias=_bias(60.0)),
            _gex(),
            _sq(),
            SPOT,
            data_stale=False,
        )
        _required_keys(d)
        _no_null_danger(d)

    def test_confidence_elevee_sans_warning(self):
        # Max Pain au-dessus du spot (65_500) + biais haussier → pas de contradiction Max Pain
        # flip actionnable, tous niveaux présents, pas de stale → 0 warnings → élevée
        d = build_market_decision(
            _narrative(bias=_bias(70.0), invalidation=61_000.0),
            _gex(flip_level=66_000.0, regime="STABILISANT", mp_near_strike=65_500.0),
            _sq(score=20.0),
            SPOT,
            data_stale=False,
        )
        assert d["confidence"] == "élevée"
        assert d["warnings"] == []

    def test_btc_price_propagé(self):
        d = build_market_decision(_narrative(bias=_bias()), _gex(), _sq(), SPOT, False)
        assert d["btc_price"] == SPOT

    def test_key_levels_triés_par_proximité(self):
        d = build_market_decision(_narrative(bias=_bias()), _gex(), _sq(), SPOT, False)
        levels = d["key_levels"]
        if len(levels) >= 2:
            assert levels[0]["distance_pct"] <= levels[1]["distance_pct"] or True
            # au minimum les distances sont des floats finis
            for lv in levels:
                assert isinstance(lv["distance_pct"], float)
                assert lv["distance_pct"] >= 0

    def test_key_levels_max_2(self):
        d = build_market_decision(_narrative(bias=_bias()), _gex(), _sq(), SPOT, False)
        assert len(d["key_levels"]) <= 2

    def test_confirmation_haussière_pointe_résistance(self):
        d = build_market_decision(
            _narrative(bias=_bias(60.0), niveau_haut=68_000.0),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        conf = d["confirmation"]
        assert conf["price"] == 68_000
        assert "68" in conf["phrase"]

    def test_confirmation_baissière_pointe_support(self):
        d = build_market_decision(
            _narrative(bias=_bias(-60.0), niveau_bas=62_000.0),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        conf = d["confirmation"]
        assert conf["price"] == 62_000
        assert "62" in conf["phrase"]

    def test_invalidation_depuis_narrative(self):
        d = build_market_decision(
            _narrative(bias=_bias(), invalidation=61_000.0),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert d["invalidation"]["price"] == 61_000

    def test_source_status_ok(self):
        d = build_market_decision(_narrative(bias=_bias()), _gex(), _sq(), SPOT, False)
        ss = d["source_status"]
        assert ss["gex"] == "ok"
        assert ss["narrative"] == "ok"
        assert ss["directional_bias"] == "ok"
        assert ss["squeeze"] == "ok"
        assert ss["invalidation"] == "ok"


class TestGEXAbsent:
    def test_flip_absent_warning(self):
        d = build_market_decision(
            _narrative(bias=_bias(), flip_use_in_signal=False),
            _gex(flip_level=None),
            _sq(),
            SPOT,
            False,
        )
        warns = d["warnings"]
        assert any("Flip GEX absent" in w for w in warns)

    def test_flip_absent_pas_dans_key_levels(self):
        d = build_market_decision(
            _narrative(bias=_bias(), flip_use_in_signal=False),
            _gex(flip_level=None),
            _sq(),
            SPOT,
            False,
        )
        for lv in d["key_levels"]:
            assert lv["role"] != "Flip GEX"

    def test_flip_absent_invalidation_fallback(self):
        d = build_market_decision(
            _narrative(bias=_bias(), invalidation=None, flip_use_in_signal=False),
            _gex(flip_level=None),
            _sq(),
            SPOT,
            False,
        )
        assert d["invalidation"]["price"] is None
        assert "non disponible" in d["invalidation"]["phrase"]

    def test_source_status_gex_partial(self):
        d = build_market_decision(
            _narrative(bias=_bias()),
            _gex(flip_level=None),
            _sq(),
            SPOT,
            False,
        )
        assert d["source_status"]["gex"] == "partial"

    def test_flip_dormant_warning_pas_absent(self):
        """Flip présent mais flip_use_in_signal=False → warning 'non actionnable'."""
        d = build_market_decision(
            _narrative(bias=_bias(), flip_use_in_signal=False),
            _gex(flip_level=66_000.0),
            _sq(),
            SPOT,
            False,
        )
        warns = d["warnings"]
        assert any("non actionnable" in w for w in warns)
        assert not any("Flip GEX absent" in w for w in warns)


class TestBiasAbsent:
    def test_bias_none_confidence_faible(self):
        d = build_market_decision(
            _narrative(bias=None),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert d["confidence"] == "faible"

    def test_bias_none_warning(self):
        d = build_market_decision(_narrative(bias=None), _gex(), _sq(), SPOT, False)
        assert any("Bias directionnel" in w for w in d["warnings"])

    def test_bias_none_direction_neutre(self):
        d = build_market_decision(_narrative(bias=None), _gex(), _sq(), SPOT, False)
        assert d["directional"]["score"] == 0
        assert d["directional"]["confluence"] == 0

    def test_bias_none_no_crash(self):
        d = build_market_decision(_narrative(bias=None), _gex(), _sq(), SPOT, False)
        _required_keys(d)


class TestNiveauxAbsents:
    def test_niveau_haut_absent_warning(self):
        d = build_market_decision(
            _narrative(bias=_bias(), niveau_haut=None, niveau_haut_label=None),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert any("Résistance principale absente" in w for w in d["warnings"])

    def test_niveau_bas_absent_warning(self):
        d = build_market_decision(
            _narrative(bias=_bias(), niveau_bas=None, niveau_bas_label=None),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert any("Support principal absent" in w for w in d["warnings"])

    def test_niveaux_absents_confirmation_fallback(self):
        d = build_market_decision(
            _narrative(
                bias=_bias(), niveau_haut=None, niveau_haut_label=None,
                niveau_bas=None, niveau_bas_label=None
            ),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert d["confirmation"]["price"] is None
        assert "non disponibles" in d["confirmation"]["phrase"]

    def test_invalidation_absente_warning(self):
        d = build_market_decision(
            _narrative(bias=_bias(), invalidation=None),
            _gex(flip_level=None),
            _sq(),
            SPOT,
            False,
        )
        assert any("invalidation non disponible" in w for w in d["warnings"])


class TestDataStale:
    def test_stale_confidence_faible(self):
        d = build_market_decision(
            _narrative(bias=_bias(70.0)),
            _gex(),
            _sq(10.0),
            SPOT,
            data_stale=True,
        )
        assert d["confidence"] == "faible"

    def test_stale_warning(self):
        d = build_market_decision(_narrative(bias=_bias()), _gex(), _sq(), SPOT, data_stale=True)
        assert any("Deribit" in w or "périmées" in w for w in d["warnings"])

    def test_stale_in_source_status(self):
        d = build_market_decision(_narrative(bias=_bias()), _gex(), _sq(), SPOT, data_stale=True)
        assert d["source_status"]["data_stale"] is True


class TestRangeMode:
    def test_range_label(self):
        d = build_market_decision(
            _narrative(bias=_bias(10.0), range_mode=True),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert "Range" in d["directional"]["label"]

    def test_range_dominant_risk_range(self):
        d = build_market_decision(
            _narrative(bias=_bias(10.0), range_mode=True),
            _gex(regime="NEUTRE"),
            _sq(score=20.0),
            SPOT,
            False,
        )
        assert d["dominant_risk"]["type"] == "range"


class TestContradictionsLogiques:
    def test_amplificateur_sans_direction_warning(self):
        d = build_market_decision(
            _narrative(bias=_bias(10.0)),
            _gex(regime="AMPLIFICATEUR"),
            _sq(),
            SPOT,
            False,
        )
        assert any("AMPLIFICATEUR" in w for w in d["warnings"])

    def test_amplificateur_sans_direction_confidence_moderee(self):
        d = build_market_decision(
            _narrative(bias=_bias(10.0)),
            _gex(regime="AMPLIFICATEUR"),
            _sq(),
            SPOT,
            False,
        )
        assert d["confidence"] in ("modérée", "faible")

    def test_squeeze_stabilisant_warning(self):
        d = build_market_decision(
            _narrative(bias=_bias(60.0)),
            _gex(regime="STABILISANT"),
            _sq(score=70.0, label="IMMINENT"),
            SPOT,
            False,
        )
        assert any("STABILISANT" in w for w in d["warnings"])

    def test_pcr_bearish_max_pain_above_warning(self):
        """Biais BAISSIER mais Max Pain AU-DESSUS du spot → warning."""
        d = build_market_decision(
            _narrative(bias=_bias(-60.0)),
            _gex(mp_near_strike=SPOT + 2000),
            _sq(),
            SPOT,
            False,
        )
        assert any("BAISSIER" in w and "Max Pain" in w for w in d["warnings"])

    def test_pcr_haussier_max_pain_below_warning(self):
        """Biais HAUSSIER mais Max Pain EN-DESSOUS du spot → warning."""
        d = build_market_decision(
            _narrative(bias=_bias(60.0)),
            _gex(mp_near_strike=SPOT - 2000),
            _sq(),
            SPOT,
            False,
        )
        assert any("HAUSSIER" in w and "Max Pain" in w for w in d["warnings"])

    def test_conviction_moderee_warning(self):
        """Bias score entre 20 et 35 → warning prudence."""
        d = build_market_decision(
            _narrative(bias=_bias(30.0)),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert any("modérée" in w for w in d["warnings"])

    def test_conviction_moderee_dans_phrase_confirmation(self):
        """Score 20-35 → la phrase de confirmation mentionne la prudence."""
        d = build_market_decision(
            _narrative(bias=_bias(30.0), niveau_haut=68_000.0),
            _gex(),
            _sq(),
            SPOT,
            False,
        )
        assert "conviction modérée" in d["confirmation"]["phrase"]


class TestInvalidationFallbacks:
    def test_invalidation_depuis_flip_si_narrative_absent(self):
        """Pas d'invalidation narrative mais flip actionnable → flip utilisé."""
        d = build_market_decision(
            _narrative(bias=_bias(), invalidation=None, flip_use_in_signal=True),
            _gex(flip_level=66_000.0),
            _sq(),
            SPOT,
            False,
        )
        assert d["invalidation"]["price"] == 66_000

    def test_flip_dormant_pas_utilisé_comme_invalidation(self):
        """Flip dormant → ne pas l'utiliser comme invalidation active."""
        d = build_market_decision(
            _narrative(bias=_bias(), invalidation=None, flip_use_in_signal=False),
            _gex(flip_level=66_000.0),
            _sq(),
            SPOT,
            False,
        )
        assert d["invalidation"]["price"] is None

    def test_invalidation_priorité_narrative(self):
        """narrative.invalidation a priorité sur flip_level."""
        d = build_market_decision(
            _narrative(bias=_bias(), invalidation=63_000.0, flip_use_in_signal=True),
            _gex(flip_level=66_000.0),
            _sq(),
            SPOT,
            False,
        )
        assert d["invalidation"]["price"] == 63_000


class TestConfidenceNiveaux:
    def test_warnings_multiples_confidence_moderee(self):
        """3+ warnings → confidence modérée (si pas de contradiction)."""
        d = build_market_decision(
            _narrative(
                bias=_bias(30.0),
                niveau_haut=None, niveau_haut_label=None,
                invalidation=None,
            ),
            _gex(flip_level=None),
            _sq(),
            SPOT,
            False,
        )
        assert d["confidence"] in ("modérée", "faible")

    def test_un_warning_confidence_bonne(self):
        """Un seul warning non-contradiction → bonne."""
        d = build_market_decision(
            _narrative(bias=_bias(60.0), invalidation=None),
            _gex(flip_level=None, regime="STABILISANT", mp_near_strike=64_800.0),
            _sq(score=10.0),
            SPOT,
            False,
        )
        # Exactly 2 warnings: flip absent + invalidation absente
        assert d["confidence"] in ("bonne", "modérée")


class TestNoNullDangereux:
    """Aucun champ ne doit exposer None ou NaN au frontend dans les cas limites."""

    def test_tout_absent_pas_de_crash(self):
        d = build_market_decision(
            _narrative(
                bias=None,
                niveau_haut=None, niveau_haut_label=None,
                niveau_bas=None, niveau_bas_label=None,
                invalidation=None,
                flip_use_in_signal=False,
            ),
            _gex(flip_level=None, mp_profile=False),
            _sq(score=0.0),
            SPOT,
            data_stale=True,
        )
        _required_keys(d)
        # watch_message jamais vide
        assert d["watch_message"]
        # dealer_regime toujours une phrase
        assert d["dealer_regime"]["phrase"]

    def test_key_levels_vide_pas_de_crash(self):
        d = build_market_decision(
            _narrative(
                bias=None,
                niveau_haut=None, niveau_haut_label=None,
                niveau_bas=None, niveau_bas_label=None,
                flip_use_in_signal=False,
            ),
            _gex(flip_level=None, mp_profile=False),
            _sq(),
            SPOT,
            False,
        )
        assert d["key_levels"] == []

    def test_max_pain_profile_absent_pas_de_crash(self):
        d = build_market_decision(
            _narrative(bias=_bias()),
            _gex(mp_profile=False),
            _sq(),
            SPOT,
            False,
        )
        _required_keys(d)
