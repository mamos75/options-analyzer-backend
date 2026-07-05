"""
Tests Stats & Edge — page de validation réelle.

Couvre :
1.  Phase "collecting" retournée quand N faible
2.  N < 10 → score masqué (None)
3.  N 10-29 → statut préliminaire
4.  N 30-99 → statut exploitable
5.  N >= 100 → statut robuste
6.  Event types Phase 1B reconnus dans INDICATOR_TABLE
7.  Indicateur absent → "Aucune observation collectée"
8.  data_sufficient=False quand N finalisés faible
9.  Score jamais affiché avec N faible (protection 100/100)
10. Auto-refresh ne casse pas si endpoint retourne objet vide
11. recent_observations présent dans réponse
12. next_4h_minutes présent dans réponse (None si pas de pending)
13. methodology présent dans réponse avec les 3 familles
14. last_obs_minutes_ago présent dans réponse
--- Bug-fix maturité (2026-06-03) ---
15. dex_bearish N=54 pending (0 outcomes) → "Insuffisant", jamais "Exploitable"
16. Phase globale basée sur out_72h pas total_obs
17. Winrate = hit/n_finalized (pas hit/n_total)
18. Indicateur avec 30 outcomes 72h → confidence="exploitable"
19. API expose observations ET outcomes_validated séparément
--- Phase 1C (2026-06-03) ---
20. dex_bearish/bullish ont migré vers pipeline outcome via log_observation
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.stats_edge import compute_stats_edge, _confidence, INDICATOR_TABLE, _MIN_SCORE_N
from backend.event_store import EVENT_LOG_PATH, PENDING_EVENTS_PATH, SILENT_LOG_PATH


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_finalized_line(
    event_type: str,
    ts: datetime = None,
    hit: bool = True,
    setup_id: str = None,
) -> str:
    if ts is None:
        ts = datetime.now(timezone.utc) - timedelta(hours=1)
    rec = {
        "id": f"test_{event_type}_{ts.timestamp():.0f}",
        "ts": ts.isoformat(),
        "event_type": event_type,
        "spot_at_signal": 67000.0,
        "outcome_4h": 2.0 if hit else -2.0,
        "outcome_24h": 2.5 if hit else -2.5,
        "outcome_72h": 3.0 if hit else -3.0,
        "hit_target": hit,
        "invalidated": not hit,
        "sent": True,
    }
    if setup_id is not None:
        rec["setup_id"] = setup_id
    return json.dumps(rec)


def _write_finalized(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _write_pending(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps(events))


def _run_with_patches(event_log: Path, pending_path: Path, silent_path: Path = None) -> dict:
    if silent_path is None:
        with tempfile.TemporaryDirectory() as td:
            s = Path(td) / "silent.jsonl"
            with (
                patch("backend.stats_edge.EVENT_LOG_PATH", event_log),
                patch("backend.stats_edge.PENDING_EVENTS_PATH", pending_path),
                patch("backend.stats_edge.SILENT_LOG_PATH", s),
            ):
                return compute_stats_edge(days=30)
    with (
        patch("backend.stats_edge.EVENT_LOG_PATH", event_log),
        patch("backend.stats_edge.PENDING_EVENTS_PATH", pending_path),
        patch("backend.stats_edge.SILENT_LOG_PATH", silent_path),
    ):
        return compute_stats_edge(days=30)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Phase "collecting" quand N faible
# ─────────────────────────────────────────────────────────────────────────────

def test_phase_collecting_when_low_n():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(3)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["phase"] == "COLLECTE"


def test_phase_collecting_empty():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["phase"] == "COLLECTE"
        assert result["totals"]["observations"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. N < 10 → score masqué
# ─────────────────────────────────────────────────────────────────────────────

def test_score_masked_when_n_below_min():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # 5 events mopi_bullish — en dessous de _MIN_SCORE_N=10
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(5)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        mopi_ind = next((i for i in result["indicators"] if i["event_type"] == "mopi_bullish"), None)
        assert mopi_ind is not None
        assert mopi_ind["score"] is None, f"Score devrait être None sous {_MIN_SCORE_N}, got {mopi_ind['score']}"


def test_score_never_100_with_low_n():
    """Jamais score = 100 si N < _MIN_SCORE_N."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("squeeze_bullish", hit=True) for _ in range(4)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        for ind in result["indicators"]:
            assert ind["score"] != 100 or ind["n_total"] >= _MIN_SCORE_N, (
                f"{ind['event_type']} : score=100 avec N={ind['n_total']} < {_MIN_SCORE_N}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. N 10-29 → statut préliminaire
# ─────────────────────────────────────────────────────────────────────────────

def test_status_preliminary_when_n_10_to_29():
    key, label = _confidence(10)
    assert key == "préliminaire"
    assert "liminaire" in label.lower()

    key2, _ = _confidence(29)
    assert key2 == "préliminaire"


def test_indicator_status_preliminary_at_n_15():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("wall_breakout", setup_id=f"s{i}") for i in range(15)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next((i for i in result["indicators"] if i["event_type"] == "wall_breakout"), None)
        assert ind is not None
        assert ind["confidence"] == "préliminaire"


# ─────────────────────────────────────────────────────────────────────────────
# 4. N 30-99 → statut exploitable
# ─────────────────────────────────────────────────────────────────────────────

def test_status_exploitable_when_n_30_to_99():
    key, label = _confidence(30)
    assert key == "exploitable"

    key2, _ = _confidence(99)
    assert key2 == "exploitable"


# ─────────────────────────────────────────────────────────────────────────────
# 5. N >= 100 → statut robuste
# ─────────────────────────────────────────────────────────────────────────────

def test_status_robust_when_n_100_plus():
    key, label = _confidence(100)
    assert key == "robuste"

    key2, _ = _confidence(500)
    assert key2 == "robuste"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Event types Phase 1B reconnus dans INDICATOR_TABLE
# ─────────────────────────────────────────────────────────────────────────────

def test_phase1b_gex_regime_in_table():
    etypes = [e for e, _, _ in INDICATOR_TABLE]
    assert "gex_regime" in etypes


def test_phase1b_gravity_explosive_in_table():
    etypes = [e for e, _, _ in INDICATOR_TABLE]
    assert "gravity_explosive" in etypes


def test_phase1b_max_pain_pull_in_table():
    etypes = [e for e, _, _ in INDICATOR_TABLE]
    assert "max_pain_pull" in etypes


def test_phase1b_max_pain_shift_in_table():
    etypes = [e for e, _, _ in INDICATOR_TABLE]
    assert "max_pain_shift" in etypes


def test_phase1b_mopi_cross_in_table():
    etypes = [e for e, _, _ in INDICATOR_TABLE]
    assert "mopi_cross" in etypes


# ─────────────────────────────────────────────────────────────────────────────
# 7. Indicateur absent → "Aucune observation collectée"
# ─────────────────────────────────────────────────────────────────────────────

def test_absent_indicator_shows_no_observation():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        # Tous les indicateurs doivent exister même sans données
        for ind in result["indicators"]:
            assert "status" in ind
            if ind["n_total"] == 0:
                assert ind["status"] == "Aucune observation collectée", (
                    f"{ind['event_type']}: attendu 'Aucune observation collectée', got '{ind['status']}'"
                )


# ─────────────────────────────────────────────────────────────────────────────
# 8. data_sufficient=False quand N faible
# ─────────────────────────────────────────────────────────────────────────────

def test_data_sufficient_false_when_low_n():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(3)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["data_sufficient"] is False


def test_data_sufficient_true_when_enough():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(12)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["data_sufficient"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 9. Affiche "Edge en accumulation" quand data_sufficient=False
# Ce test vérifie que le champ est présent pour que le frontend l'affiche
# ─────────────────────────────────────────────────────────────────────────────

def test_edge_accumulation_flag_present():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        # Le frontend affiche "Edge en accumulation" quand data_sufficient est False
        assert "data_sufficient" in result
        assert result["data_sufficient"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 10. Auto-refresh ne casse pas si endpoint vide
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_stats_edge_empty_files_no_crash():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # Fichiers inexistants — ne doit pas lever d'exception
        result = _run_with_patches(el, pp)
        assert isinstance(result, dict)
        assert result["phase"] == "COLLECTE"
        assert result["totals"]["observations"] == 0


def test_compute_stats_edge_malformed_jsonl_no_crash():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("not json\n{bad}\n")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────────────────
# 11. recent_observations présent dans réponse
# ─────────────────────────────────────────────────────────────────────────────

def test_recent_observations_present():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("squeeze_bullish")])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert "recent_observations" in result
        assert isinstance(result["recent_observations"], list)
        assert len(result["recent_observations"]) >= 1
        obs = result["recent_observations"][0]
        assert "ts" in obs
        assert "event_type" in obs
        assert "status" in obs


def test_recent_observations_empty_when_no_data():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["recent_observations"] == []


def test_recent_observations_capped_at_10():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(20)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert len(result["recent_observations"]) <= 10


# ─────────────────────────────────────────────────────────────────────────────
# 12. next_4h_minutes présent (None si pas de pending)
# ─────────────────────────────────────────────────────────────────────────────

def test_next_4h_none_when_no_pending():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish")])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert "next_4h_minutes" in result
        assert result["next_4h_minutes"] is None


def test_next_4h_computed_for_pending():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        # Event pending créé il y a 2h → encore 2h avant +4h
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _write_pending(pp, [{"ts": ts, "event_type": "wall_breakout", "checked_4h": False}])
        result = _run_with_patches(el, pp)
        assert result["next_4h_minutes"] is not None
        # 2h restantes ≈ 120 min (±5 min de tolérance)
        assert 100 <= result["next_4h_minutes"] <= 135


# ─────────────────────────────────────────────────────────────────────────────
# 13. methodology présent avec les 3 familles
# ─────────────────────────────────────────────────────────────────────────────

def test_methodology_present():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert "methodology" in result
        m = result["methodology"]
        assert "families" in m
        assert "Direction" in m["families"]
        assert "Volatilité / Magnitude" in m["families"]
        assert "Niveaux" in m["families"]
        assert "thresholds" in m
        assert m["thresholds"]["minimum"] == _MIN_SCORE_N


# ─────────────────────────────────────────────────────────────────────────────
# 14. last_obs_minutes_ago présent
# ─────────────────────────────────────────────────────────────────────────────

def test_last_obs_minutes_ago_none_when_empty():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert "last_obs_minutes_ago" in result
        assert result["last_obs_minutes_ago"] is None


def test_last_obs_minutes_ago_computed():
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        ts = datetime.now(timezone.utc) - timedelta(minutes=30)
        line = json.dumps({
            "id": "abc", "ts": ts.isoformat(),
            "event_type": "squeeze_bullish", "spot_at_signal": 67000.0,
            "outcome_4h": 2.0, "outcome_24h": 2.5, "outcome_72h": 3.0,
            "hit_target": True, "invalidated": False, "sent": True,
        })
        el.write_text(line + "\n")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["last_obs_minutes_ago"] is not None
        assert 25 <= result["last_obs_minutes_ago"] <= 35


# ─────────────────────────────────────────────────────────────────────────────
# 15. Bug-fix maturité : 54 pending (0 outcomes) → jamais "Exploitable"
# Phase 1C : dex_bearish passe par log_observation → PendingEvent, pas silent_only
# ─────────────────────────────────────────────────────────────────────────────

def _make_silent_line(event_type: str, ts: datetime = None) -> str:
    if ts is None:
        ts = datetime.now(timezone.utc) - timedelta(hours=1)
    return json.dumps({
        "id": f"silent_{event_type}_{ts.timestamp():.0f}",
        "ts": ts.isoformat(),
        "event_type": event_type,
        "sent": False,
        "blocked_reason": "silent_observation",
    })


def _make_pending_observation(event_type: str, ts: datetime = None) -> dict:
    """PendingEvent créé par log_observation() — sans outcome (en cours de collecte)."""
    if ts is None:
        ts = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "id": f"obs_{event_type}_{ts.timestamp():.0f}",
        "ts": ts.isoformat(),
        "event_type": event_type,
        "spot_at_signal": 67000.0,
        "signal_strength": 50.0,
        "quality_state": "ACTIVE",
        "gex_near": 0.0,
        "mopi_score": 50.0,
        "squeeze_score": 0.0,
        "nearest_wall": 0.0,
        "nearest_gravity_zone": 0.0,
        "direction": "DOWN" if "bearish" in event_type else "UP",
        "sent": False,
        "blocked_reason": "silent_observation",
        "silent": True,
        "outcome_1h": None,
        "outcome_4h": None,
        "outcome_24h": None,
        "checked_1h": False,
        "checked_4h": False,
        "checked_24h": False,
        "btc_samples": [],
        "max_favorable_excursion": None,
        "max_adverse_excursion": None,
        "hit_target": None,
        "invalidated": None,
        "outcome_72h": None,
        "checked_72h": False,
    }


def test_silent_54_obs_0_outcomes_not_exploitable():
    """dex_bearish avec 54 pending events (0 outcomes) → confidence='insuffisant', jamais 'exploitable'."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        sl.write_text("")
        _write_pending(pp, [_make_pending_observation("dex_bearish") for _ in range(54)])
        result = _run_with_patches(el, pp, sl)
        ind = next((i for i in result["indicators"] if i["event_type"] == "dex_bearish"), None)
        assert ind is not None, "dex_bearish doit apparaître dans les indicateurs"
        assert ind["outcome_72h"] == 0, f"Expected 0 outcomes 72h, got {ind['outcome_72h']}"
        assert ind["confidence"] != "exploitable", (
            f"BUG: dex_bearish avec 0 outcomes ne peut pas être 'exploitable'. "
            f"Got confidence='{ind['confidence']}', status='{ind['status']}'"
        )
        assert ind["confidence"] == "insuffisant", (
            f"Expected 'insuffisant', got '{ind['confidence']}'"
        )
        assert "Collecte" in ind["status"], (
            f"Status doit mentionner 'Collecte', got '{ind['status']}'"
        )


def test_silent_54_obs_0_outcomes_shows_obs_count():
    """n_total doit refléter 54 observations pour transparence."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        sl.write_text("")
        _write_pending(pp, [_make_pending_observation("dex_bullish") for _ in range(54)])
        result = _run_with_patches(el, pp, sl)
        ind = next((i for i in result["indicators"] if i["event_type"] == "dex_bullish"), None)
        assert ind is not None
        assert ind["n_total"] == 54, f"n_total doit être 54, got {ind['n_total']}"


# ─────────────────────────────────────────────────────────────────────────────
# 16. Phase globale basée sur out_72h, pas total_obs
# ─────────────────────────────────────────────────────────────────────────────

def test_phase_collecte_when_many_obs_but_zero_outcomes():
    """50 obs silencieuses + 0 outcomes finalisés → phase='COLLECTE', pas 'EXPLOITABLE'."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        _write_pending(pp, [])
        # 50 obs silencieuses — out_72h=0
        _write_finalized(sl, [_make_silent_line("dex_bearish") for _ in range(50)])
        result = _run_with_patches(el, pp, sl)
        assert result["phase"] == "COLLECTE", (
            f"BUG: 50 obs silencieuses + 0 outcomes 72h → doit être 'COLLECTE', got '{result['phase']}'"
        )


def test_phase_exploitable_requires_30_finalized_72h():
    """30 events finalisés (avec outcome_72h) → phase='EXPLOITABLE'."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(30)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        assert result["phase"] == "EXPLOITABLE", (
            f"30 finalized (72h) → doit être 'EXPLOITABLE', got '{result['phase']}'"
        )
        assert result["outcomes"]["validated_72h"] == 30


# ─────────────────────────────────────────────────────────────────────────────
# 17. Winrate = hit / n_finalized (pas hit / n_total)
# ─────────────────────────────────────────────────────────────────────────────

def test_winrate_uses_finalized_not_total():
    """10 setups finalisés (tous hits) + 40 obs silencieuses → winrate=100%, pas 20%."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        # 10 finalized hits avec setup_id unique → 10 setups finalisés
        lines = [_make_finalized_line("squeeze_bullish", hit=True, setup_id=f"s{i}") for i in range(10)]
        _write_finalized(el, lines)
        _write_pending(pp, [])
        # 40 entrées dans silent_log — ne doivent pas entrer dans le calcul du winrate
        _write_finalized(sl, [_make_silent_line("dex_bearish") for _ in range(40)])
        result = _run_with_patches(el, pp, sl)
        ind = next((i for i in result["indicators"] if i["event_type"] == "squeeze_bullish"), None)
        assert ind is not None
        # 10 setup_hits / 10 setups_finalisés = 100%
        assert ind["winrate_pct"] == 100.0, (
            f"Winrate devrait être 100.0 (10/10 setups), got {ind['winrate_pct']}"
        )
        assert ind["score"] == 100


def test_winrate_mixed_hits_and_misses():
    """7 hits + 3 misses (setups uniques) → winrate=70.0."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        lines = (
            [_make_finalized_line("wall_breakout", hit=True,  setup_id=f"h{i}") for i in range(7)] +
            [_make_finalized_line("wall_breakout", hit=False, setup_id=f"m{i}") for i in range(3)]
        )
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next((i for i in result["indicators"] if i["event_type"] == "wall_breakout"), None)
        assert ind is not None
        assert ind["winrate_pct"] == 70.0, f"Expected 70.0, got {ind['winrate_pct']}"


# ─────────────────────────────────────────────────────────────────────────────
# 18. 30 outcomes 72h → confidence="exploitable" pour un indicateur
# ─────────────────────────────────────────────────────────────────────────────

def test_indicator_exploitable_with_30_finalized():
    """30 setups finalisés (setup_id unique + outcome_72h) → confidence='exploitable'."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("gravity_magnet", setup_id=f"s{i}") for i in range(30)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next((i for i in result["indicators"] if i["event_type"] == "gravity_magnet"), None)
        assert ind is not None
        assert ind["confidence"] == "exploitable", (
            f"Expected 'exploitable' avec 30 setups finalisés, got '{ind['confidence']}'"
        )
        assert ind["n_setups_finalized"] == 30
        assert ind["confidence_basis"] == "setup"


# ─────────────────────────────────────────────────────────────────────────────
# 19. API expose observations ET outcomes_validated séparément
# ─────────────────────────────────────────────────────────────────────────────

def test_indicator_has_observations_and_outcomes_validated():
    """Chaque indicateur doit avoir 'observations', 'outcomes_validated', 'n_setups_finalized'."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("mopi_bullish") for _ in range(5)])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        for ind in result["indicators"]:
            assert "observations" in ind,          f"{ind['event_type']} manque 'observations'"
            assert "outcomes_validated" in ind,    f"{ind['event_type']} manque 'outcomes_validated'"
            assert "confidence_basis" in ind,      f"{ind['event_type']} manque 'confidence_basis'"
            assert "n_setups_finalized" in ind,    f"{ind['event_type']} manque 'n_setups_finalized'"
            assert "n_setups" in ind,              f"{ind['event_type']} manque 'n_setups'"


def test_observations_and_outcomes_are_different_for_silent():
    """Pour dex_bearish via log_observation : observations (pending) > outcomes_validated (=0)."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        sl.write_text("")
        _write_pending(pp, [_make_pending_observation("dex_bearish") for _ in range(54)])
        result = _run_with_patches(el, pp, sl)
        ind = next((i for i in result["indicators"] if i["event_type"] == "dex_bearish"), None)
        assert ind is not None
        assert ind["observations"] == 54, f"observations devrait être 54, got {ind['observations']}"
        assert ind["outcomes_validated"] == 0, f"outcomes_validated devrait être 0, got {ind['outcomes_validated']}"
        assert ind["observations"] > ind["outcomes_validated"]
        assert ind["n_setups_finalized"] == 0, "pas de setup finalisé sans setup_id"


# ─────────────────────────────────────────────────────────────────────────────
# 20. Méthodologie setup unique : N observations ≠ N setups
# ─────────────────────────────────────────────────────────────────────────────

def test_setup_dedup_single_setup_many_obs():
    """
    55 records finalisés avec LE MÊME setup_id → n_setups_finalized=1, pas 55.
    Valide la règle : même état marché persistant = 1 setup unique.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # 55 records avec setup_id identique (état persistant)
        lines = [_make_finalized_line("gravity_explosive", hit=True, setup_id="SETUP_001")
                 for _ in range(55)]
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next((i for i in result["indicators"] if i["event_type"] == "gravity_explosive"), None)
        assert ind is not None
        assert ind["n_total"] == 55, f"n_total (observations) doit être 55, got {ind['n_total']}"
        assert ind["n_setups_finalized"] == 1, (
            f"BUG: 55 records du même setup_id → n_setups_finalized doit être 1, "
            f"got {ind['n_setups_finalized']}"
        )
        assert ind["confidence"] == "insuffisant", (
            f"1 setup finalisé → insuffisant (seuil min={_MIN_SCORE_N})"
        )


def test_setup_dedup_distinct_setups():
    """
    127 observations → 3 setups distincts (3 setup_id différents).
    N statistique = 3, pas 127.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # 3 setups avec ~42 obs chacun, tous hits
        lines = []
        for s in range(3):
            lines.extend([
                _make_finalized_line("dex_bearish", hit=True, setup_id=f"SETUP_{s:03d}")
                for _ in range(42)
            ])
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next((i for i in result["indicators"] if i["event_type"] == "dex_bearish"), None)
        assert ind is not None
        assert ind["n_total"] == 126, f"n_total doit être 126, got {ind['n_total']}"
        assert ind["n_setups_finalized"] == 3, (
            f"3 setup_id distincts → n_setups_finalized=3, got {ind['n_setups_finalized']}"
        )
        # 3 setups < _MIN_SCORE_N → score=None, confidence=insuffisant
        assert ind["score"] is None, f"3 setups < {_MIN_SCORE_N} → score doit être None"
        assert ind["confidence"] == "insuffisant"


def test_legacy_records_no_setup_id_do_not_inflate_confidence():
    """
    Records sans setup_id (legacy) → n_setups_finalized=0.
    La confiance ne doit pas être 'exploitable' avec des legacy records.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # 50 finalized records sans setup_id (legacy pré-fix)
        lines = [_make_finalized_line("dex_bullish", hit=True) for _ in range(50)]
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next((i for i in result["indicators"] if i["event_type"] == "dex_bullish"), None)
        assert ind is not None
        assert ind["n_setups_finalized"] == 0, (
            "Legacy records sans setup_id → n_setups_finalized=0"
        )
        assert ind["confidence"] == "insuffisant", (
            f"Legacy records sans setup_id → confidence='insuffisant', got '{ind['confidence']}'"
        )
        assert ind["score"] is None, "Pas de score sans setups finalisés"


# ─────────────────────────────────────────────────────────────────────────────
# 21. Ligne vide avec empty_reason explicite
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_reason_present_for_all_zero_indicators():
    """
    Tout indicateur avec n_total=0 ET n_silent_raw=0 → empty_reason doit être renseigné
    (jamais None, jamais chaîne vide) pour expliquer pourquoi la ligne est vide.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        for ind in result["indicators"]:
            if ind["n_total"] == 0 and ind.get("n_silent_raw", 0) == 0:
                assert ind.get("empty_reason"), (
                    f"{ind['event_type']}: ligne vide sans empty_reason. "
                    "Toute ligne vide doit expliquer pourquoi elle est vide."
                )
                assert len(ind["empty_reason"]) > 10, (
                    f"{ind['event_type']}: empty_reason trop court: '{ind['empty_reason']}'"
                )


def test_empty_reason_none_when_data_present():
    """
    empty_reason doit être None quand des observations existent.
    Une ligne avec données ne doit pas expliquer pourquoi elle est vide.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        _write_finalized(el, [_make_finalized_line("dex_bullish", setup_id="s1")])
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next(i for i in result["indicators"] if i["event_type"] == "dex_bullish")
        assert ind["empty_reason"] is None, (
            "empty_reason doit être None quand des données existent"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 22. Wall candidates visibles — affichent le compteur, jamais de score
# ─────────────────────────────────────────────────────────────────────────────

def test_wall_candidates_visible_with_signal_count():
    """
    wall_rejection_candidate et wall_breakout_candidate doivent apparaître dans
    les indicateurs avec n_total > 0 quand des signaux silencieux existent.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        _write_pending(pp, [])
        # 5 signaux bloqués wall_rejection_candidate dans le silent log
        lines = [_make_silent_line("wall_rejection_candidate") for _ in range(5)]
        _write_finalized(sl, lines)
        result = _run_with_patches(el, pp, sl)

        cand = next(
            (i for i in result["indicators"] if i["event_type"] == "wall_rejection_candidate"),
            None
        )
        assert cand is not None, "wall_rejection_candidate doit être dans les indicateurs"
        assert cand["n_total"] > 0 or cand["n_silent_raw"] > 0, (
            "wall_rejection_candidate doit montrer le compteur de signaux"
        )
        assert cand["score"] is None, "silent_only → aucun score (jamais de winrate affiché)"
        assert cand["silent_only"] is True


def test_wall_candidates_no_score_no_outcome():
    """wall_rejection_candidate ne doit jamais afficher un score même avec N>10."""
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        _write_pending(pp, [])
        # 50 entrées silent — ne doivent jamais générer un score
        lines = [_make_silent_line("wall_breakout_candidate") for _ in range(50)]
        _write_finalized(sl, lines)
        result = _run_with_patches(el, pp, sl)

        cand = next(
            (i for i in result["indicators"] if i["event_type"] == "wall_breakout_candidate"),
            None
        )
        assert cand is not None
        assert cand["score"] is None, (
            "wall_breakout_candidate est silent_only → score jamais affiché, "
            f"got score={cand['score']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 23. Aucun score pour silent-only sans outcome
# ─────────────────────────────────────────────────────────────────────────────

def test_silent_only_indicators_never_show_score():
    """
    Tous les event_types marqués silent_only ne doivent jamais afficher de score
    quelle que soit la quantité de données dans le silent log.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        sl = Path(td) / "event_store_silent.jsonl"
        el.write_text("")
        _write_pending(pp, [])
        # Injecter 200 entrées pour les deux types silent_only
        lines = (
            [_make_silent_line("wall_rejection_candidate") for _ in range(100)] +
            [_make_silent_line("wall_breakout_candidate")  for _ in range(100)]
        )
        _write_finalized(sl, lines)
        result = _run_with_patches(el, pp, sl)

        for ind in result["indicators"]:
            if ind.get("silent_only"):
                assert ind["score"] is None, (
                    f"BUG: {ind['event_type']} est silent_only → score doit être None, "
                    f"got {ind['score']}"
                )
                assert ind["winrate_pct"] is None, (
                    f"BUG: {ind['event_type']} est silent_only → winrate_pct doit être None"
                )


# ─────────────────────────────────────────────────────────────────────────────
# 24. Observations persistantes ne gonflent pas N statistique
# ─────────────────────────────────────────────────────────────────────────────

def test_repeated_same_setup_id_does_not_inflate_n():
    """
    100 records finalisés avec LE MÊME setup_id = n_setups_finalized=1.
    L'affichage de confidence doit rester 'insuffisant', jamais 'exploitable'.
    Le N statistique est le nombre de SETUPS DISTINCTS, pas d'observations.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # 100 enregistrements du MÊME setup persistant
        lines = [
            _make_finalized_line("max_pain_pull", hit=True, setup_id="PERSISTENT_SETUP_001")
            for _ in range(100)
        ]
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next(i for i in result["indicators"] if i["event_type"] == "max_pain_pull")

        assert ind["n_total"] == 100, "n_total (observations) = 100"
        assert ind["n_setups_finalized"] == 1, (
            "BUG: 100 records du même setup_id → n_setups_finalized DOIT être 1. "
            f"Got {ind['n_setups_finalized']}. "
            "Règle : un setup persistant compte une seule fois."
        )
        assert ind["confidence"] == "insuffisant", (
            f"1 setup finalisé < seuil minimum → insuffisant. Got '{ind['confidence']}'"
        )
        assert ind["score"] is None, "Score masqué car n_setups_finalized < 10"


def test_n_30_distinct_setups_reaches_exploitable():
    """
    30 records avec 30 setup_id DISTINCTS = n_setups_finalized=30 → exploitable.
    Valide que le seuil est basé sur les setups uniques.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        lines = [
            _make_finalized_line("dex_bearish", hit=True, setup_id=f"SETUP_{i:04d}")
            for i in range(30)
        ]
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next(i for i in result["indicators"] if i["event_type"] == "dex_bearish")

        assert ind["n_setups_finalized"] == 30
        assert ind["confidence"] == "exploitable", (
            "30 setups distincts finalisés → confidence='exploitable'"
        )
        assert ind["score"] is not None, "Score disponible à 30 setups finalisés"


def test_30_observations_same_setup_stays_insuffisant():
    """
    30 observations du MÊME setup → n_setups_finalized=1 → insuffisant.
    Contre-exemple : sans dedup, on aurait tort de dire 'exploitable'.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        # Même setup_id répété 30 fois (état marché persistant)
        lines = [
            _make_finalized_line("gravity_explosive", hit=True, setup_id="PERSISTENT_001")
            for _ in range(30)
        ]
        _write_finalized(el, lines)
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)
        ind = next(i for i in result["indicators"] if i["event_type"] == "gravity_explosive")

        assert ind["n_setups_finalized"] == 1, (
            f"30 obs du même setup → 1 setup finalisé unique. Got {ind['n_setups_finalized']}"
        )
        assert ind["confidence"] == "insuffisant", (
            "1 setup unique < seuil 10 → insuffisant (jamais exploitable avec 1 setup)"
        )

# ─────────────────────────────────────────────────────────────────────────────
# 25. n_setups_finalized dans la réponse API (format JSON attendu)
# ─────────────────────────────────────────────────────────────────────────────

def test_api_response_includes_n_setups_finalized_for_all_indicators():
    """
    Chaque indicateur dans la réponse API doit inclure n_setups_finalized,
    n_setups_pending, n_setups, confidence_basis='setup'.
    Ces champs sont requis pour le guard frontend et les règles de confiance.
    """
    with tempfile.TemporaryDirectory() as td:
        el = Path(td) / "event_store.jsonl"
        pp = Path(td) / "event_pending.json"
        el.write_text("")
        _write_pending(pp, [])
        result = _run_with_patches(el, pp)

        required_fields = [
            "n_setups_finalized", "n_setups_pending", "n_setups",
            "confidence_basis", "empty_reason",
        ]
        for ind in result["indicators"]:
            for field in required_fields:
                assert field in ind, (
                    f"{ind['event_type']}: champ '{field}' manquant dans la réponse API"
                )
