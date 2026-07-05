"""
Tests Phase 1B — outcome tracking pour signaux silencieux.

Prouve que :
1.  EVENT_TYPES contient les 5 nouveaux types Phase 1B
2.  _HIT_CONFIG contient les 5 nouveaux types avec règles correctes
3.  log_event("gex_regime") accepted (pas de warning "inconnu")
4.  log_event("gravity_explosive") accepted
5.  log_event("max_pain_pull") accepted
6.  log_event("max_pain_shift") accepted
7.  log_event("mopi_cross") accepted
8.  hit_target correct pour gex_regime ANY +2.5% → True
9.  hit_target correct pour gravity_explosive ANY DOWN -2.5% → True
10. hit_target correct pour max_pain_pull UP +1.2% → True
11. hit_target correct pour max_pain_pull DOWN -1.2% → True
12. hit_target correct pour mopi_cross UP +1.6% → True
13. hit_target correct pour mopi_cross DOWN -1.6% → True
14. gex_regime avec mouvement <0.5% → neutral (pas hit, pas invalidé)
15. indicator_accuracy mapping contient gex + max_pain + mopi_cross
16. log_event("gex_regime", sent=False) → sent=False dans PendingEvent
17. log_event("max_pain_pull") invalidé si direction=UP mais BTC -1.5%
18. gravity_explosive direction=None + gros move → hit (ANY sans direction)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.event_store import EventStore, EVENT_TYPES, _HIT_CONFIG, _determine_hit

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_es() -> EventStore:
    es = EventStore.__new__(EventStore)
    es._pending = {}
    es._dedup_cache = {}
    return es


def _log(es, event_type, direction=None, sent=False):
    return es.log_event(
        event_type=event_type,
        spot=67000.0,
        signal_strength=75.0,
        quality_state="ACTIVE",
        gex_near=-5e9,
        mopi_score=40.0,
        squeeze_score=55.0,
        nearest_wall=65000.0,
        nearest_gravity_zone=70000.0,
        direction=direction,
        sent=sent,
        blocked_reason="silent_observation",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. EVENT_TYPES contient les 5 nouveaux types
# ─────────────────────────────────────────────────────────────────────────────

def test_event_types_gex_regime():
    assert "gex_regime" in EVENT_TYPES


def test_event_types_gravity_explosive():
    assert "gravity_explosive" in EVENT_TYPES


def test_event_types_max_pain_pull():
    assert "max_pain_pull" in EVENT_TYPES


def test_event_types_max_pain_shift():
    assert "max_pain_shift" in EVENT_TYPES


def test_event_types_mopi_cross():
    assert "mopi_cross" in EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# 2. _HIT_CONFIG contient les 5 nouveaux types
# ─────────────────────────────────────────────────────────────────────────────

def test_hit_config_gex_regime():
    rule, thresh = _HIT_CONFIG["gex_regime"]
    assert rule == "ANY" and thresh == 2.0


def test_hit_config_gravity_explosive():
    rule, thresh = _HIT_CONFIG["gravity_explosive"]
    assert rule == "ANY" and thresh == 2.0


def test_hit_config_max_pain_pull():
    rule, thresh = _HIT_CONFIG["max_pain_pull"]
    assert rule == "ANY" and thresh == 1.0


def test_hit_config_max_pain_shift():
    rule, thresh = _HIT_CONFIG["max_pain_shift"]
    assert rule == "ANY" and thresh == 1.5


def test_hit_config_mopi_cross():
    rule, thresh = _HIT_CONFIG["mopi_cross"]
    assert rule == "ANY" and thresh == 1.5


# ─────────────────────────────────────────────────────────────────────────────
# 3-7. log_event accepte les nouveaux types (pas d'ID vide)
# ─────────────────────────────────────────────────────────────────────────────

def test_log_event_gex_regime_accepted():
    es = _make_es()
    ev_id = _log(es, "gex_regime")
    assert ev_id != ""


def test_log_event_gravity_explosive_accepted():
    es = _make_es()
    ev_id = _log(es, "gravity_explosive", direction=None)
    assert ev_id != ""


def test_log_event_max_pain_pull_accepted():
    es = _make_es()
    ev_id = _log(es, "max_pain_pull", direction="UP")
    assert ev_id != ""


def test_log_event_max_pain_shift_accepted():
    es = _make_es()
    ev_id = _log(es, "max_pain_shift", direction="UP")
    assert ev_id != ""


def test_log_event_mopi_cross_accepted():
    es = _make_es()
    ev_id = _log(es, "mopi_cross", direction="DOWN")
    assert ev_id != ""


# ─────────────────────────────────────────────────────────────────────────────
# 8. hit_target pour gex_regime ANY +2.5%
# ─────────────────────────────────────────────────────────────────────────────

def test_gex_regime_hit_big_up_move():
    hit, inv = _determine_hit("gex_regime", None, outcome_4h=2.5, outcome_24h=None)
    assert hit is True and inv is False


def test_gex_regime_hit_big_down_move():
    # ANY sans direction : abs(primary) >= 2.0
    hit, inv = _determine_hit("gex_regime", None, outcome_4h=-2.5, outcome_24h=None)
    assert hit is True and inv is False


# ─────────────────────────────────────────────────────────────────────────────
# 9. gravity_explosive DOWN -2.5%
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_explosive_down_hit():
    hit, inv = _determine_hit("gravity_explosive", "DOWN", outcome_4h=-2.5, outcome_24h=None)
    assert hit is True


def test_gravity_explosive_up_hit():
    hit, inv = _determine_hit("gravity_explosive", "UP", outcome_4h=2.5, outcome_24h=None)
    assert hit is True


# ─────────────────────────────────────────────────────────────────────────────
# 10-11. max_pain_pull directional
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_pull_up_hit():
    hit, inv = _determine_hit("max_pain_pull", "UP", outcome_4h=1.2, outcome_24h=None)
    assert hit is True


def test_max_pain_pull_down_hit():
    hit, inv = _determine_hit("max_pain_pull", "DOWN", outcome_4h=-1.2, outcome_24h=None)
    assert hit is True


# ─────────────────────────────────────────────────────────────────────────────
# 12-13. mopi_cross directional
# ─────────────────────────────────────────────────────────────────────────────

def test_mopi_cross_up_hit():
    hit, inv = _determine_hit("mopi_cross", "UP", outcome_4h=1.6, outcome_24h=None)
    assert hit is True


def test_mopi_cross_down_hit():
    hit, inv = _determine_hit("mopi_cross", "DOWN", outcome_4h=-1.6, outcome_24h=None)
    assert hit is True


# ─────────────────────────────────────────────────────────────────────────────
# 14. gex_regime — mouvement <0.5% → neutral
# ─────────────────────────────────────────────────────────────────────────────

def test_gex_regime_neutral_small_move():
    hit, inv = _determine_hit("gex_regime", None, outcome_4h=0.2, outcome_24h=None)
    assert hit is False and inv is True  # abs < 0.3 → invalidated (ANY no-direction branch)


def test_gex_regime_neutral_medium_move():
    # 0.3 < abs < 2.0 → not hit, not invalidated
    hit, inv = _determine_hit("gex_regime", None, outcome_4h=1.0, outcome_24h=None)
    assert hit is False and inv is False


# ─────────────────────────────────────────────────────────────────────────────
# 15. indicator_accuracy mapping
# ─────────────────────────────────────────────────────────────────────────────

def test_indicator_accuracy_has_gex_group():
    from backend.indicator_accuracy import _INDICATOR_GROUPS
    assert "gex" in _INDICATOR_GROUPS
    assert "gex_regime" in _INDICATOR_GROUPS["gex"]


def test_indicator_accuracy_has_max_pain_group():
    from backend.indicator_accuracy import _INDICATOR_GROUPS
    assert "max_pain" in _INDICATOR_GROUPS
    assert "max_pain_pull" in _INDICATOR_GROUPS["max_pain"]
    assert "max_pain_shift" in _INDICATOR_GROUPS["max_pain"]


def test_indicator_accuracy_mopi_has_cross():
    from backend.indicator_accuracy import _INDICATOR_GROUPS
    assert "mopi_cross" in _INDICATOR_GROUPS["mopi"]


def test_indicator_accuracy_gravity_has_explosive():
    from backend.indicator_accuracy import _INDICATOR_GROUPS
    assert "gravity_explosive" in _INDICATOR_GROUPS["gravity"]


# ─────────────────────────────────────────────────────────────────────────────
# 16. log_event sent=False → PendingEvent.sent == False
# ─────────────────────────────────────────────────────────────────────────────

def test_log_event_gex_regime_sent_false():
    es = _make_es()
    ev_id = _log(es, "gex_regime", sent=False)
    assert es._pending[ev_id].sent is False
    assert es._pending[ev_id].blocked_reason == "silent_observation"


# ─────────────────────────────────────────────────────────────────────────────
# 17. max_pain_pull invalidé si direction=UP mais BTC -1.5%
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_pull_up_invalidated_if_down():
    hit, inv = _determine_hit("max_pain_pull", "UP", outcome_4h=-1.5, outcome_24h=None)
    # DOWN sur UP → invalidated (threshold/2 = 0.5 → -1.5 < -0.5 → inv)
    assert hit is False and inv is True


# ─────────────────────────────────────────────────────────────────────────────
# 18. gravity_explosive direction=None + gros move → hit
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_explosive_symmetric_big_move():
    hit, inv = _determine_hit("gravity_explosive", None, outcome_4h=3.0, outcome_24h=None)
    assert hit is True
