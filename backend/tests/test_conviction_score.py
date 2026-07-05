"""
Tests non-régression Conviction Score + Wall Lifecycle distance guard.

Règle clé : distance >5% du spot = JAMAIS un déclencheur, même ACTIONABLE + 3 confluences.
S'applique aux events individuels ET aux résumés lifecycle.
"""
import pytest
from datetime import datetime, timezone
from backend.conviction_score import compute_conviction_score, MIN_SCORE_TO_SEND
from backend.wall_lifecycle import WallLifecycleState


# ── Règle absolue distance >5% ────────────────────────────────────────────────

def test_distance_over5_blocks_actionable_no_confluence():
    r = compute_conviction_score("ACTIONABLE", 5.1, False, False, False)
    assert r.send is False, "ACTIONABLE à 5.1% doit être bloqué"


def test_distance_over5_blocks_actionable_triple_confluence():
    """Cas qui passait avant le fix : 2+0+3=5 ≥ MIN_SCORE_TO_SEND."""
    r = compute_conviction_score("ACTIONABLE", 6.0, True, True, True)
    assert r.send is False, "ACTIONABLE 6% + 3 confluences doit être bloqué (règle >5%)"


def test_distance_over5_blocks_active_with_dex():
    r = compute_conviction_score("ACTIVE", 7.5, True, False, False)
    assert r.send is False, "ACTIVE 7.5% + DEX doit être bloqué"


def test_distance_exactly5_not_blocked():
    """Exactement 5% = dans la zone secondaire → score doit décider."""
    r = compute_conviction_score("ACTIONABLE", 5.0, True, True, False)
    # score = 2 + 1 + 2 = 5 → doit passer
    assert r.send is True, "ACTIONABLE exactement 5% + 2 confluences doit passer"


def test_distance_4pct_active_no_confluence():
    """4% ACTIVE sans confluence : score = 1+1+0 = 2 → bloqué par score, pas par distance."""
    r = compute_conviction_score("ACTIVE", 4.0, False, False, False)
    assert r.send is False, "ACTIVE 4% sans confluence : score insuffisant"


# ── Cas qui doivent passer (smoke test) ──────────────────────────────────────

def test_actionable_close_passes():
    r = compute_conviction_score("ACTIONABLE", 1.0, True, False, False)
    assert r.send is True, "ACTIONABLE 1% + DEX doit passer"


def test_active_close_with_confluence_passes():
    r = compute_conviction_score("ACTIVE", 1.5, True, False, False)
    # score = 1 + 2 + 1 = 4 → bloqué (min=5)
    assert r.send is False, "ACTIVE 1.5% + 1 conf = score 4, insuffisant"
    r2 = compute_conviction_score("ACTIVE", 1.5, True, True, False)
    # score = 1 + 2 + 2 = 5 → passe
    assert r2.send is True, "ACTIVE 1.5% + 2 conf = score 5, doit passer"


# ── Score report ─────────────────────────────────────────────────────────────

def test_reason_mentions_distance_over5():
    r = compute_conviction_score("ACTIONABLE", 8.0, True, True, True)
    assert "8.0%" in r.reason or "hors portée" in r.reason


def test_breakdown_always_present():
    r = compute_conviction_score("DORMANT", 10.0)
    assert "tag" in r.breakdown
    assert "distance" in r.breakdown
    assert "confluence" in r.breakdown


# ── Lifecycle summary — hard block distance >5% ───────────────────────────────

def _make_state(strike: float, net: int = 4) -> WallLifecycleState:
    """State avec net_reinforcement > _NET_DELTA_THRESHOLD pour que should_send_summary soit True sans filtre distance."""
    now = datetime.now(timezone.utc)
    s = WallLifecycleState(strike=strike, first_seen=now, last_seen=now, current_tag="ACTIONABLE")
    s.reinforced_count = net
    s.last_reported_net = 0
    # pas de last_reported_at → cooldown_elapsed() = True
    return s


def test_lifecycle_summary_blocked_when_strike_over5pct_from_spot():
    """Mur à $78k avec BTC à $67k → 16% distance → résumé lifecycle bloqué."""
    state = _make_state(strike=78_000.0)
    # Sans filtre distance, should_send_summary() retournerait True (delta=4 ≥ 3)
    assert state.should_send_summary(spot=0.0) is True, "sanity check sans spot"
    # Avec spot $67k → distance ~16% → hard block
    assert state.should_send_summary(spot=67_000.0) is False, "distance >5% doit bloquer"


def test_lifecycle_summary_blocked_at_80k_spot_67k():
    """Scénario exact remonté par Mamos : mur $80k / BTC $67k."""
    state = _make_state(strike=80_000.0)
    assert state.should_send_summary(spot=67_000.0) is False


def test_lifecycle_summary_passes_when_close():
    """Mur à $68k avec BTC à $67k → ~1.5% → résumé autorisé si delta suffisant."""
    state = _make_state(strike=68_000.0)
    assert state.should_send_summary(spot=67_000.0) is True


def test_lifecycle_summary_passes_exactly_5pct():
    """Exactement 5% → limite haute autorisée (cohérent avec conviction_score)."""
    spot = 67_000.0
    strike = spot * 1.05   # exactement 5%
    state = _make_state(strike=strike)
    assert state.should_send_summary(spot=spot) is True


def test_lifecycle_summary_no_spot_defaults_to_no_block():
    """Appel sans spot (legacy) : pas de distance check, comportement inchangé."""
    state = _make_state(strike=80_000.0)
    assert state.should_send_summary() is True
