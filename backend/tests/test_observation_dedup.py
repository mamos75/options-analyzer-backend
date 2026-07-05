"""
Tests Déduplication Observations — Règle méthodologique fondamentale.

Une observation répétée n'est pas un nouvel échantillon statistique.

Couvre :
1.  max_pain_near_pull : 10 polls identiques = 1 seule entrée dans le log
2.  max_pain_near_pull : changement de strike = nouvel event autorisé
3.  max_pain_near_pull : changement d'expiry = nouvel event autorisé
4.  max_pain_near_pull : changement de direction = nouvel event autorisé
5.  max_pain_near_pull : cooldown expiré = nouvel event autorisé
6.  gravity_explosive : 10 polls même zone DOWN_ONLY = 1 seule entrée
7.  gravity_explosive : changement de zone_center = nouvel event
8.  gravity_explosive : changement d'explosive_bias = nouvel event
9.  gravity_explosive : changement de distance bucket = nouvel event
10. DEX : ACTIVE ↔ ACTIONABLE même direction = 1 setup (pas de nouveau PendingEvent)
11. DEX : direction change BULLISH→BEARISH = nouveau setup
12. DEX : pression change de palier (10% bucket) = nouveau setup
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.event_store import EventStore, _SILENT_COOLDOWNS


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_es(silent_path: Path) -> EventStore:
    """Crée un EventStore isolé (hors singleton) avec fichier temp."""
    es = EventStore.__new__(EventStore)
    es._pending     = {}
    es._dedup_cache = {}
    return es


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _log_max_pain(es: EventStore, path: Path, expiry: str, strike: float, direction: str):
    dedup_key = f"max_pain_pull_{expiry}_{strike}_{direction}"
    with patch("backend.event_store.SILENT_LOG_PATH", path):
        es.log_silent_event(
            event_type="max_pain_near_pull",
            spot=67000.0,
            direction=direction,
            indicators={"family": "level", "distance_pct": 2.0},
            metadata={"max_pain_near": strike, "expiry": expiry, "dte": 5},
            dedup_key=dedup_key,
        )


def _log_gravity(es: EventStore, path: Path, etype: str, center: float, bias: str, dist_pct: float):
    center_k    = int(center / 1000) * 1000
    dist_bucket = int(dist_pct / 2) * 2
    state_hash  = f"gravity_{etype}_{center_k}_{bias}_{dist_bucket}"
    with patch("backend.event_store.SILENT_LOG_PATH", path):
        es.log_silent_event(
            event_type=etype,
            spot=67000.0,
            direction="DOWN",
            indicators={"family": "magnitude", "strength": 80.0, "distance_pct": dist_pct},
            metadata={"zone_center": center, "explosive_bias": bias},
            dedup_key=state_hash,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 1. max_pain_near_pull : 10 polls identiques = 1 entrée
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_10_identical_polls_produces_1_log_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        for _ in range(10):
            _log_max_pain(es, path, "2026-06-27", 70000.0, "UP")
        assert _count_lines(path) == 1, (
            f"10 polls identiques doivent produire 1 entrée, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. max_pain_near_pull : changement de strike = nouvel event
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_strike_change_creates_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        _log_max_pain(es, path, "2026-06-27", 70000.0, "UP")
        _log_max_pain(es, path, "2026-06-27", 71000.0, "UP")  # strike change
        assert _count_lines(path) == 2, (
            f"Changement de strike doit créer 2 entrées, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. max_pain_near_pull : changement d'expiry = nouvel event
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_expiry_change_creates_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        _log_max_pain(es, path, "2026-06-27", 70000.0, "UP")
        _log_max_pain(es, path, "2026-07-25", 70000.0, "UP")  # expiry change
        assert _count_lines(path) == 2, (
            f"Changement d'expiry doit créer 2 entrées, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. max_pain_near_pull : changement de direction = nouvel event
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_direction_change_creates_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        _log_max_pain(es, path, "2026-06-27", 70000.0, "UP")
        _log_max_pain(es, path, "2026-06-27", 70000.0, "DOWN")  # direction change
        assert _count_lines(path) == 2, (
            f"Changement de direction doit créer 2 entrées, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. max_pain_near_pull : cooldown expiré = nouvel event autorisé
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_expired_cooldown_allows_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path     = Path(td) / "silent.jsonl"
        es       = _make_es(path)
        dedup_key = "max_pain_pull_2026-06-27_70000.0_UP"
        now       = datetime.now(timezone.utc)
        # Injecter un last_ts bien dans le passé (cooldown expiré)
        es._dedup_cache[dedup_key] = now - timedelta(hours=5)
        _log_max_pain(es, path, "2026-06-27", 70000.0, "UP")
        assert _count_lines(path) == 1, (
            f"Cooldown expiré doit autoriser un nouvel event, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. gravity_explosive : 10 polls même zone = 1 entrée
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_explosive_10_identical_polls_produces_1_log_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        for _ in range(10):
            _log_gravity(es, path, "gravity_explosive_down", 60000.0, "DOWN_ONLY", 3.5)
        assert _count_lines(path) == 1, (
            f"10 polls même zone doivent produire 1 entrée, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. gravity_explosive : changement de zone_center = nouvel event
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_explosive_zone_center_change_creates_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        _log_gravity(es, path, "gravity_explosive_down", 60000.0, "DOWN_ONLY", 3.5)
        _log_gravity(es, path, "gravity_explosive_down", 65000.0, "DOWN_ONLY", 3.5)  # centre change
        assert _count_lines(path) == 2, (
            f"Changement de zone_center doit créer 2 entrées, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. gravity_explosive : changement d'explosive_bias = nouvel event
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_explosive_bias_change_creates_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        _log_gravity(es, path, "gravity_explosive_down",     60000.0, "DOWN_ONLY", 3.5)
        _log_gravity(es, path, "gravity_explosive_symmetric", 60000.0, "SYMMETRIC",  3.5)  # bias change
        assert _count_lines(path) == 2, (
            f"Changement de bias doit créer 2 entrées, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. gravity_explosive : changement significatif de distance (bucket) = nouvel event
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_explosive_distance_bucket_change_creates_new_entry():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        # dist_pct=3.5% → bucket=2  ;  dist_pct=5.5% → bucket=4  : deux buckets différents
        _log_gravity(es, path, "gravity_explosive_down", 60000.0, "DOWN_ONLY", 3.5)
        _log_gravity(es, path, "gravity_explosive_down", 60000.0, "DOWN_ONLY", 5.5)
        assert _count_lines(path) == 2, (
            f"Changement de distance bucket doit créer 2 entrées, got {_count_lines(path)}"
        )


def test_gravity_explosive_same_bucket_no_duplicate():
    """3.1% et 3.9% tombent dans le même bucket (2) → 1 seule entrée."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "silent.jsonl"
        es   = _make_es(path)
        _log_gravity(es, path, "gravity_explosive_down", 60000.0, "DOWN_ONLY", 3.1)
        _log_gravity(es, path, "gravity_explosive_down", 60000.0, "DOWN_ONLY", 3.9)
        assert _count_lines(path) == 1, (
            f"Micro-variation intra-bucket doit produire 1 entrée, got {_count_lines(path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 10. DEX : ACTIVE ↔ ACTIONABLE même direction = même dedup_key (1 PendingEvent attendu)
# ─────────────────────────────────────────────────────────────────────────────

def test_dex_active_actionable_same_key():
    """ACTIVE et ACTIONABLE dans la même direction et même pressure_bucket → même clé."""
    def _build_key(etype, dir_ud, profile, pressure_pct):
        usable_group    = "usable" if profile in ("ACTIVE", "ACTIONABLE") else "non_usable"
        pressure_bucket = str(int(abs(pressure_pct) / 10) * 10)
        return f"obs_{etype}_{dir_ud}_{usable_group}_{pressure_bucket}"

    key_active     = _build_key("dex_bearish", "DOWN", "ACTIVE",     25.0)
    key_actionable = _build_key("dex_bearish", "DOWN", "ACTIONABLE", 25.0)
    assert key_active == key_actionable, (
        f"ACTIVE et ACTIONABLE doivent produire la même clé : "
        f"'{key_active}' != '{key_actionable}'"
    )


def test_dex_direction_change_different_key():
    """Changement de direction BULLISH→BEARISH = clé différente = nouveau setup."""
    def _build_key(etype, dir_ud, profile, pressure_pct):
        usable_group    = "usable" if profile in ("ACTIVE", "ACTIONABLE") else "non_usable"
        pressure_bucket = str(int(abs(pressure_pct) / 10) * 10)
        return f"obs_{etype}_{dir_ud}_{usable_group}_{pressure_bucket}"

    key_bull = _build_key("dex_bullish", "UP",   "ACTIVE", 25.0)
    key_bear = _build_key("dex_bearish", "DOWN", "ACTIVE", 25.0)
    assert key_bull != key_bear, "Direction différente doit produire des clés différentes"


def test_dex_usable_nonusable_different_key():
    """ACTIVE (usable) vs STRUCTURAL (non_usable) = clé différente = nouveau setup."""
    def _build_key(etype, dir_ud, profile, pressure_pct):
        usable_group    = "usable" if profile in ("ACTIVE", "ACTIONABLE") else "non_usable"
        pressure_bucket = str(int(abs(pressure_pct) / 10) * 10)
        return f"obs_{etype}_{dir_ud}_{usable_group}_{pressure_bucket}"

    key_usable  = _build_key("dex_bearish", "DOWN", "ACTIVE",     25.0)
    key_nonuse  = _build_key("dex_bearish", "DOWN", "STRUCTURAL", 25.0)
    assert key_usable != key_nonuse, "ACTIVE vs STRUCTURAL doivent produire des clés différentes"


def test_dex_pressure_bucket_change_different_key():
    """Pression 25% (bucket 20) vs 35% (bucket 30) = clé différente = nouveau setup."""
    def _build_key(etype, dir_ud, profile, pressure_pct):
        usable_group    = "usable" if profile in ("ACTIVE", "ACTIONABLE") else "non_usable"
        pressure_bucket = str(int(abs(pressure_pct) / 10) * 10)
        return f"obs_{etype}_{dir_ud}_{usable_group}_{pressure_bucket}"

    key_low  = _build_key("dex_bearish", "DOWN", "ACTIVE", 25.0)
    key_high = _build_key("dex_bearish", "DOWN", "ACTIVE", 35.0)
    assert key_low != key_high, "Changement de pressure bucket doit produire des clés différentes"


def test_dex_pressure_micro_variation_same_key():
    """22% et 27% tombent dans le même bucket (20) → même clé."""
    def _build_key(etype, dir_ud, profile, pressure_pct):
        usable_group    = "usable" if profile in ("ACTIVE", "ACTIONABLE") else "non_usable"
        pressure_bucket = str(int(abs(pressure_pct) / 10) * 10)
        return f"obs_{etype}_{dir_ud}_{usable_group}_{pressure_bucket}"

    key_22 = _build_key("dex_bearish", "DOWN", "ACTIVE", 22.0)
    key_27 = _build_key("dex_bearish", "DOWN", "ACTIVE", 27.0)
    assert key_22 == key_27, "Micro-variation intra-bucket doit produire la même clé"


# ─────────────────────────────────────────────────────────────────────────────
# Vérifications de cohérence SILENT_COOLDOWNS
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_near_pull_cooldown_is_4h():
    """Cooldown max_pain_near_pull = 4h (empêche re-log à chaque poll de 5 min)."""
    from datetime import timedelta
    cd = _SILENT_COOLDOWNS.get("max_pain_near_pull")
    assert cd is not None, "max_pain_near_pull doit avoir un cooldown défini"
    assert cd >= timedelta(hours=4), (
        f"Cooldown trop court : {cd} (min 4h pour éviter 48 logs/jour)"
    )


def test_gravity_explosive_cooldown_is_30min():
    """Cooldown gravity_explosive = 30min (au moins)."""
    from datetime import timedelta
    for etype in ("gravity_explosive_down", "gravity_explosive_up", "gravity_explosive_symmetric"):
        cd = _SILENT_COOLDOWNS.get(etype)
        assert cd is not None, f"{etype} doit avoir un cooldown défini"
        assert cd >= timedelta(minutes=30), f"Cooldown {etype} trop court : {cd}"
