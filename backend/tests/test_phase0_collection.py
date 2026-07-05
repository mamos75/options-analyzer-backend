"""
Tests Phase 0 — validation de la collecte de données.

Prouve que :
1. history_saver écrit bien plusieurs snapshots
2. log_silent_event crée un event silencieux (sent=False)
3. event bloqué quality gate → tracked sent=False
4. event bloqué conviction score → tracked blocked_reason
5. event envoyé → sent=True
6. validators +1h/+4h/+24h/+72h finalisent correctement
7. MFE/MAE calculés correctement
8. collection_health détecte DB vide
9. collection_health détecte event_store vide
10. warnings présents si conditions pas saines
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.event_store import EventStore, _compute_mfe_mae

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_db(path: str, rows: int = 0) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            mopi REAL, gex REAL, dex REAL,
            iv_rank REAL, pc_ratio REAL, max_pain REAL,
            flip_level REAL, btc_price REAL,
            pc_ratio_near REAL, gex_near REAL
        )
    """)
    now = int(time.time())
    for i in range(rows):
        conn.execute(
            "INSERT INTO metrics_history (ts,mopi,gex,dex,iv_rank,pc_ratio,max_pain,flip_level,btc_price) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now - i * 1800, 50.0, 1e9, -100.0, 60.0, 1.2, 70000.0, 68000.0, 73000.0),
        )
    conn.commit()
    conn.close()


def _make_es(tmpdir: str) -> EventStore:
    """Crée un EventStore isolé avec fichiers dans tmpdir."""
    es = EventStore.__new__(EventStore)
    es._pending = {}
    return es


def _log_event_blocked(es: EventStore, reason: str) -> str:
    return es.log_event(
        event_type="squeeze_bullish",
        spot=73000.0,
        signal_strength=50.0,
        quality_state="degraded",
        gex_near=1e9,
        mopi_score=65.0,
        squeeze_score=60.0,
        nearest_wall=72000.0,
        nearest_gravity_zone=74000.0,
        direction="UP",
        sent=False,
        blocked_reason=reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. history_saver écrit plusieurs snapshots
# ─────────────────────────────────────────────────────────────────────────────

def test_history_saver_writes_multiple_snapshots():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_history.db")
        import backend.history_store as hs
        orig = hs.DB_PATH
        try:
            hs.DB_PATH = db_path
            hs.init_db()
            hs.save_snapshot(50.0, 1e9, -100.0, 60.0, 1.2, 70000.0, 68000.0, 73000.0)
            hs.save_snapshot(55.0, 1.1e9, -150.0, 62.0, 1.3, 70500.0, 68500.0, 74000.0)
            hs.save_snapshot(48.0, 0.9e9, -80.0, 58.0, 1.1, 69500.0, 67500.0, 72500.0)
            rows = hs.get_history(1)
            assert len(rows) >= 2, f"Attendu ≥2 lignes, obtenu {len(rows)}"
        finally:
            hs.DB_PATH = orig


# ─────────────────────────────────────────────────────────────────────────────
# 2. log_silent_event crée bien un event silencieux
# ─────────────────────────────────────────────────────────────────────────────

def test_log_silent_event_creates_entry():
    with tempfile.TemporaryDirectory() as tmpdir:
        silent_path = Path(tmpdir) / "silent.jsonl"
        es = _make_es(tmpdir)

        with patch("backend.event_store.SILENT_LOG_PATH", silent_path):
            es.log_silent_event(
                "squeeze_bullish", 73000.0, "UP",
                {"mopi": 75.0, "gex_near": 1e9},
            )

        assert silent_path.exists(), "SILENT_LOG_PATH non créé"
        lines = [l for l in silent_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["sent"] is False
        assert rec["silent"] is True
        assert rec["event_type"] == "squeeze_bullish"


def test_log_silent_event_creates_pending_in_store():
    """log_silent_event n'ajoute PAS au pending (c'est voulu — écrit direct jsonl)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        silent_path = Path(tmpdir) / "silent.jsonl"
        es = _make_es(tmpdir)
        with patch("backend.event_store.SILENT_LOG_PATH", silent_path):
            es.log_silent_event("mopi_bearish", 72000.0, "DOWN", {"mopi": 30.0})
        assert len(es._pending) == 0  # silencieux ne passe pas par pending


# ─────────────────────────────────────────────────────────────────────────────
# 3. Event bloqué quality gate → sent=False, tracked dans pending
# ─────────────────────────────────────────────────────────────────────────────

def test_event_blocked_quality_gate_tracked():
    with tempfile.TemporaryDirectory() as tmpdir:
        pending_path = Path(tmpdir) / "pending.json"
        es = _make_es(tmpdir)

        with patch("backend.event_store.PENDING_EVENTS_PATH", pending_path):
            ev_id = _log_event_blocked(es, "quality_gate_degraded")

        assert ev_id in es._pending
        ev = es._pending[ev_id]
        assert ev.sent is False
        assert ev.blocked_reason == "quality_gate_degraded"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Event bloqué conviction score → blocked_reason présent
# ─────────────────────────────────────────────────────────────────────────────

def test_event_blocked_conviction_score_tracked():
    with tempfile.TemporaryDirectory() as tmpdir:
        pending_path = Path(tmpdir) / "pending.json"
        es = _make_es(tmpdir)

        with patch("backend.event_store.PENDING_EVENTS_PATH", pending_path):
            ev_id = _log_event_blocked(es, "conviction_score_below_threshold")

        assert ev_id in es._pending
        ev = es._pending[ev_id]
        assert ev.sent is False
        assert ev.blocked_reason == "conviction_score_below_threshold"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Event envoyé → sent=True dans pending
# ─────────────────────────────────────────────────────────────────────────────

def test_event_sent_tracked_sent_true():
    with tempfile.TemporaryDirectory() as tmpdir:
        pending_path = Path(tmpdir) / "pending.json"
        es = _make_es(tmpdir)

        with patch("backend.event_store.PENDING_EVENTS_PATH", pending_path):
            ev_id = es.log_event(
                event_type="wall_breakout",
                spot=73000.0,
                signal_strength=80.0,
                quality_state="active",
                gex_near=2e9,
                mopi_score=75.0,
                squeeze_score=70.0,
                nearest_wall=72000.0,
                nearest_gravity_zone=74000.0,
                direction="UP",
                sent=True,
                blocked_reason=None,
            )

        assert ev_id in es._pending
        ev = es._pending[ev_id]
        assert ev.sent is True
        assert ev.blocked_reason is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Validators +1h/+4h/+24h/+72h finalisent correctement
# ─────────────────────────────────────────────────────────────────────────────

def test_validator_finalizes_72h():
    with tempfile.TemporaryDirectory() as tmpdir:
        pending_path  = Path(tmpdir) / "pending.json"
        event_log_path = Path(tmpdir) / "event_store.jsonl"
        es = _make_es(tmpdir)

        with (
            patch("backend.event_store.PENDING_EVENTS_PATH", pending_path),
            patch("backend.event_store.EVENT_LOG_PATH", event_log_path),
        ):
            ev_id = es.log_event(
                event_type="mopi_bullish",
                spot=73000.0,
                signal_strength=80.0,
                quality_state="active",
                gex_near=1e9,
                mopi_score=75.0,
                squeeze_score=70.0,
                nearest_wall=72000.0,
                nearest_gravity_zone=74000.0,
                direction="UP",
                sent=True,
            )

            # Recule le timestamp de 73h pour déclencher +72h
            past = datetime.now(timezone.utc) - timedelta(hours=73)
            ev = es._pending[ev_id]
            ev.ts = past.isoformat()
            ev.btc_samples = [
                [past.timestamp() + i * 3600, 73000 + i * 150]
                for i in range(24)
            ]

            async def run():
                with patch.object(EventStore, "_fetch_btc", AsyncMock(return_value=74500.0)):
                    await es._tick()

            asyncio.run(run())

        assert ev_id not in es._pending, "Event devrait être finalisé (retiré du pending)"
        assert event_log_path.exists(), "event_store.jsonl devrait exister"

        lines = [l for l in event_log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["outcome_72h"] is not None
        assert "checked_72h" not in rec  # retiré dans _write_final


def test_validator_fills_all_checkpoints():
    """Vérifie que les 4 checkpoints sont remplis progressivement."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pending_path  = Path(tmpdir) / "pending.json"
        event_log_path = Path(tmpdir) / "event_store.jsonl"
        es = _make_es(tmpdir)

        with (
            patch("backend.event_store.PENDING_EVENTS_PATH", pending_path),
            patch("backend.event_store.EVENT_LOG_PATH", event_log_path),
        ):
            ev_id = es.log_event(
                event_type="dealer_buy_pressure",
                spot=73000.0,
                signal_strength=70.0,
                quality_state="active",
                gex_near=1.5e9,
                mopi_score=68.0,
                squeeze_score=62.0,
                nearest_wall=72000.0,
                nearest_gravity_zone=74000.0,
                direction="UP",
                sent=True,
            )

            ev = es._pending[ev_id]

            # Simule +2h
            ev.ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            asyncio.run(_tick_mock(es, 73500.0))
            assert ev.checked_1h is True
            assert ev.outcome_1h is not None
            assert ev.checked_4h is False

            # Simule +5h
            ev.ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
            asyncio.run(_tick_mock(es, 74000.0))
            assert ev.checked_4h is True
            assert ev.outcome_4h is not None
            assert ev.checked_24h is False


async def _tick_mock(es: EventStore, btc_price: float):
    with patch.object(EventStore, "_fetch_btc", AsyncMock(return_value=btc_price)):
        await es._tick()


# ─────────────────────────────────────────────────────────────────────────────
# 7. MFE/MAE calculés correctement
# ─────────────────────────────────────────────────────────────────────────────

def test_mfe_mae_bullish():
    """MFE = max hausse, MAE = max drawdown pour signal bullish."""
    samples = [
        [0, 73000.0],
        [1, 73500.0],
        [2, 72800.0],  # drawdown
        [3, 74200.0],  # nouveau high
    ]
    mfe, mae = _compute_mfe_mae("squeeze_bullish", "UP", 73000.0, samples)
    assert mfe is not None and mfe > 0
    assert mae is not None and mae >= 0
    # MFE = +74200/73000 - 1 ≈ 1.644%
    assert abs(mfe - round((74200 - 73000) / 73000 * 100, 3)) < 0.01


def test_mfe_mae_bearish():
    """MFE = max baisse, MAE = max rebond pour signal bearish."""
    samples = [
        [0, 73000.0],
        [1, 72000.0],  # favorable
        [2, 73500.0],  # adverse
        [3, 71000.0],  # nouveau low favorable
    ]
    mfe, mae = _compute_mfe_mae("squeeze_bearish", "DOWN", 73000.0, samples)
    assert mfe is not None and mfe > 0  # prix est descendu
    assert mae is not None and mae >= 0


def test_mfe_mae_empty_samples():
    """Pas de samples → MFE/MAE None."""
    mfe, mae = _compute_mfe_mae("mopi_bullish", "UP", 73000.0, [])
    assert mfe is None
    assert mae is None


# ─────────────────────────────────────────────────────────────────────────────
# 8. collection_health détecte DB vide
# ─────────────────────────────────────────────────────────────────────────────

def test_collection_health_detects_empty_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path      = Path(tmpdir) / "options_history.db"
        pending_path = Path(tmpdir) / "pending.json"
        event_log    = Path(tmpdir) / "event_store.jsonl"

        _make_db(str(db_path), rows=0)
        es = _make_es(tmpdir)

        with (
            patch("backend.event_store._DATA_DIR", Path(tmpdir)),
            patch("backend.event_store.PENDING_EVENTS_PATH", pending_path),
            patch("backend.event_store.EVENT_LOG_PATH", event_log),
        ):
            health = es.get_collection_health()

        assert health["status"] == "collecting_empty"
        assert health["history_db"]["rows"] == 0
        assert any("≤1 ligne" in w for w in health["warnings"])


# ─────────────────────────────────────────────────────────────────────────────
# 9. collection_health détecte event_store vide (même avec DB remplie)
# ─────────────────────────────────────────────────────────────────────────────

def test_collection_health_detects_empty_event_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path      = Path(tmpdir) / "options_history.db"
        pending_path = Path(tmpdir) / "pending.json"
        event_log    = Path(tmpdir) / "event_store.jsonl"

        _make_db(str(db_path), rows=10)  # DB bien remplie
        es = _make_es(tmpdir)             # mais aucun event en pending ni finalisé

        with (
            patch("backend.event_store._DATA_DIR", Path(tmpdir)),
            patch("backend.event_store.PENDING_EVENTS_PATH", pending_path),
            patch("backend.event_store.EVENT_LOG_PATH", event_log),
        ):
            health = es.get_collection_health()

        assert health["status"] == "collecting_empty"
        assert health["event_store"]["finalized"] == 0
        assert health["event_store"]["pending"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 10. collection_health → "healthy" quand tout est bon
# ─────────────────────────────────────────────────────────────────────────────

def test_collection_health_healthy_when_data_present():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path      = Path(tmpdir) / "options_history.db"
        pending_path = Path(tmpdir) / "pending.json"
        event_log    = Path(tmpdir) / "event_store.jsonl"

        _make_db(str(db_path), rows=10)

        # Écrit un event finalisé dans event_store.jsonl
        rec = {
            "id": "abc123",
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": "mopi_bullish",
            "spot_at_signal": 73000.0,
            "outcome_72h": 1.5,
            "sent": True,
        }
        event_log.write_text(json.dumps(rec) + "\n")

        es = _make_es(tmpdir)

        with (
            patch("backend.event_store._DATA_DIR", Path(tmpdir)),
            patch("backend.event_store.PENDING_EVENTS_PATH", pending_path),
            patch("backend.event_store.EVENT_LOG_PATH", event_log),
        ):
            health = es.get_collection_health()

        assert health["status"] == "healthy"
        assert health["history_db"]["rows"] == 10
        assert health["event_store"]["finalized"] == 1
        assert health["warnings"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 11. Warnings présents si DB stale (snapshot trop vieux)
# ─────────────────────────────────────────────────────────────────────────────

def test_collection_health_warns_stale_snapshot():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path      = Path(tmpdir) / "options_history.db"
        pending_path = Path(tmpdir) / "pending.json"
        event_log    = Path(tmpdir) / "event_store.jsonl"

        # DB avec 5 lignes mais dernier snapshot vieux de 2h
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                mopi REAL, gex REAL, dex REAL,
                iv_rank REAL, pc_ratio REAL, max_pain REAL,
                flip_level REAL, btc_price REAL
            )
        """)
        old_ts = int(time.time()) - 7201  # >2h
        for i in range(5):
            conn.execute(
                "INSERT INTO metrics_history (ts,mopi,gex,dex,iv_rank,pc_ratio,max_pain,flip_level,btc_price) "
                "VALUES (?,50,1e9,-100,60,1.2,70000,68000,73000)",
                (old_ts - i * 1800,),
            )
        conn.commit()
        conn.close()

        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event_type": "mopi_bullish"}
        event_log.write_text(json.dumps(rec) + "\n")

        es = _make_es(tmpdir)
        with (
            patch("backend.event_store._DATA_DIR", Path(tmpdir)),
            patch("backend.event_store.PENDING_EVENTS_PATH", pending_path),
            patch("backend.event_store.EVENT_LOG_PATH", event_log),
        ):
            health = es.get_collection_health()

        assert any("vieux" in w for w in health["warnings"]), (
            f"Attendu un warning stale snapshot, got: {health['warnings']}"
        )
