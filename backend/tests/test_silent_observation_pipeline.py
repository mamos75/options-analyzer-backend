"""
Tests Phase 1C — Pipeline silent observation → outcome tracking.

Prouve que :
1.  log_observation() crée un PendingEvent avec sent=False, silent=True
2.  log_observation() retourne un ev_id non vide
3.  Déduplication : même dedup_key + cooldown → skip (aucun PendingEvent)
4.  Changement d'état (nouvelle clé) → nouveau PendingEvent même si cooldown pas expiré
5.  dex_bearish via log_observation → visible dans stats_edge avec confidence=insuffisant
6.  dex_bearish via log_observation + 0 outcomes → jamais "exploitable"
7.  outcome +4h validé sur un PendingEvent créé par log_observation
8.  dex_bearish avec 0 outcomes ne spam pas (dedup cooldown respecté)
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.event_store import EventStore, PENDING_EVENTS_PATH, SILENT_LOG_PATH, EVENT_LOG_PATH
from backend.stats_edge import compute_stats_edge


def _make_es(tmp_dir: Path) -> EventStore:
    """EventStore isolé avec chemins temporaires."""
    es = EventStore.__new__(EventStore)
    es._pending = {}
    es._dedup_cache = {}
    es._pending_path = tmp_dir / "event_pending.json"
    es._silent_path  = tmp_dir / "event_store_silent.jsonl"

    # Monkey-patch les chemins globaux dans event_store
    import backend.event_store as _es_mod
    _es_mod.PENDING_EVENTS_PATH = es._pending_path
    _es_mod.SILENT_LOG_PATH     = es._silent_path
    _es_mod.EVENT_LOG_PATH      = tmp_dir / "event_store.jsonl"
    (tmp_dir / "event_store.jsonl").write_text("")

    # Isoler le setup_tracker dans le répertoire temporaire
    import backend.setup_tracker as _st_mod
    _st_mod.ACTIVE_SETUPS_PATH  = tmp_dir / "active_setups.json"
    _st_mod.SETUP_REGISTRY_PATH = tmp_dir / "setup_registry.jsonl"
    _st_mod._instance           = None  # reset du singleton pour utiliser les nouveaux chemins

    return es


def _run_stats(tmp_dir: Path) -> dict:
    import backend.event_store as _es_mod
    with (
        patch("backend.stats_edge.EVENT_LOG_PATH",    _es_mod.EVENT_LOG_PATH),
        patch("backend.stats_edge.PENDING_EVENTS_PATH", _es_mod.PENDING_EVENTS_PATH),
        patch("backend.stats_edge.SILENT_LOG_PATH",   _es_mod.SILENT_LOG_PATH),
    ):
        return compute_stats_edge(days=30)


# ─────────────────────────────────────────────────────────────────────────────
# 1. log_observation crée un PendingEvent sent=False, silent=True
# ─────────────────────────────────────────────────────────────────────────────

def test_log_observation_creates_pending_sent_false():
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        ev_id = es.log_observation(
            event_type="dex_bearish",
            spot=67000.0,
            direction="DOWN",
            signal_strength=60.0,
            quality_state="ACTIVE",
        )
        assert ev_id is not None and ev_id != "", "log_observation doit retourner un ev_id"
        assert ev_id in es._pending
        ev = es._pending[ev_id]
        assert ev.sent is False, "PendingEvent doit avoir sent=False"
        assert ev.silent is True, "PendingEvent doit avoir silent=True"
        assert ev.blocked_reason == "silent_observation"


# ─────────────────────────────────────────────────────────────────────────────
# 2. log_observation écrit aussi dans silent_log (traçabilité)
# ─────────────────────────────────────────────────────────────────────────────

def test_log_observation_writes_to_silent_log():
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        es.log_observation(
            event_type="dex_bearish",
            spot=67000.0,
            direction="DOWN",
            signal_strength=60.0,
            quality_state="ACTIVE",
        )
        lines = es._silent_path.read_text().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event_type"] == "dex_bearish"
        assert rec["silent"] is True
        assert rec["requires_outcome"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. Déduplication : même dedup_key dans le cooldown → skip
# ─────────────────────────────────────────────────────────────────────────────

def test_log_observation_dedup_same_key_skips():
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        key = "obs_dex_bearish_DOWN_ACTIVE"
        ev1 = es.log_observation(
            event_type="dex_bearish", spot=67000.0, direction="DOWN",
            signal_strength=60.0, quality_state="ACTIVE",
            dedup_key=key, cooldown=timedelta(hours=1),
        )
        ev2 = es.log_observation(
            event_type="dex_bearish", spot=67100.0, direction="DOWN",
            signal_strength=65.0, quality_state="ACTIVE",
            dedup_key=key, cooldown=timedelta(hours=1),
        )
        assert ev1 is not None, "Premier appel doit créer un event"
        assert ev2 is None, "Second appel avec même key doit être dédupliqué"
        assert len(es._pending) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Changement d'état (nouveau dedup_key) → nouveau PendingEvent
# ─────────────────────────────────────────────────────────────────────────────

def test_log_observation_new_key_creates_new_event():
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        ev1 = es.log_observation(
            event_type="dex_bearish", spot=67000.0, direction="DOWN",
            signal_strength=60.0, quality_state="ACTIVE",
            dedup_key="obs_dex_bearish_DOWN_ACTIVE",
        )
        # Changement de profil ACTIVE → ACTIONABLE = nouvelle clé = nouveau event
        ev2 = es.log_observation(
            event_type="dex_bearish", spot=67000.0, direction="DOWN",
            signal_strength=80.0, quality_state="ACTIONABLE",
            dedup_key="obs_dex_bearish_DOWN_ACTIONABLE",
        )
        assert ev1 is not None
        assert ev2 is not None, "Nouvelle clé = nouveau PendingEvent même dans le cooldown"
        assert ev1 != ev2
        assert len(es._pending) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 5-6. dex_bearish via log_observation → stats_edge confidence=insuffisant
#       jamais "exploitable" avec 0 outcomes
# ─────────────────────────────────────────────────────────────────────────────

def test_dex_bearish_via_log_observation_insuffisant():
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        for i in range(54):
            key = f"obs_dex_bearish_DOWN_ACTIVE_{i}"
            es.log_observation(
                event_type="dex_bearish", spot=67000.0 + i * 10, direction="DOWN",
                signal_strength=60.0, quality_state="ACTIVE",
                dedup_key=key,
            )
        result = _run_stats(Path(td))
        ind = next((i for i in result["indicators"] if i["event_type"] == "dex_bearish"), None)
        assert ind is not None
        assert ind["confidence"] != "exploitable", (
            f"BUG CRITIQUE: dex_bearish 0 outcomes ne peut pas être 'exploitable'. "
            f"Got confidence='{ind['confidence']}', status='{ind['status']}'"
        )
        assert ind["confidence"] == "insuffisant"
        assert ind["outcome_72h"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 7. outcome +4h validé sur un PendingEvent créé par log_observation
# ─────────────────────────────────────────────────────────────────────────────

def test_log_observation_outcome_4h_tracked():
    """Simule +4h écoulé → outcome_4h doit être présent dans le pending."""
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        ev_id = es.log_observation(
            event_type="dex_bearish", spot=67000.0, direction="DOWN",
            signal_strength=60.0, quality_state="ACTIVE",
        )
        assert ev_id in es._pending
        ev = es._pending[ev_id]

        # Simuler que 4h s'est écoulé en rétrodatant le ts
        ts_4h_ago = datetime.now(timezone.utc) - timedelta(hours=4, minutes=5)
        ev.ts = ts_4h_ago.isoformat()

        # Simuler le tick outcome avec prix BTC actuel
        import asyncio
        import backend.event_store as _es_mod

        async def _fake_fetch():
            return 65500.0  # BTC a baissé (-2.2%)

        original = es._fetch_btc
        es._fetch_btc = _fake_fetch
        asyncio.run(es._tick())
        es._fetch_btc = original

        ev_after = es._pending.get(ev_id)
        assert ev_after is not None, "Event doit encore être pending (pas encore 72h)"
        assert ev_after.checked_4h is True, "checked_4h doit être True après 4h"
        assert ev_after.outcome_4h is not None, "outcome_4h doit être calculé"
        assert ev_after.outcome_4h < 0, "BTC a baissé → outcome négatif pour dex_bearish"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Déduplication DEX : même état → pas de spam toutes les 5min
# ─────────────────────────────────────────────────────────────────────────────

def test_dex_no_spam_same_state():
    """10 appels avec même dedup_key → 1 seul PendingEvent créé."""
    with tempfile.TemporaryDirectory() as td:
        es = _make_es(Path(td))
        key = "obs_dex_bearish_DOWN_ACTIVE"
        results = []
        for _ in range(10):
            r = es.log_observation(
                event_type="dex_bearish", spot=67000.0, direction="DOWN",
                signal_strength=60.0, quality_state="ACTIVE",
                dedup_key=key, cooldown=timedelta(hours=1),
            )
            results.append(r)
        created = [r for r in results if r is not None]
        assert len(created) == 1, f"Doit créer 1 seul event, got {len(created)}"
        assert len(es._pending) == 1
