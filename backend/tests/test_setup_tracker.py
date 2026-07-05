"""
Tests SetupTracker — Règles de déduplication setup unique.

Règles testées :
1.  max_pain dedup : même expiry+strike+direction = 1 setup (observations persistantes ≠ nouvel échantillon)
2.  gravity même état = 1 setup (state_hash inchangé)
3.  gravity changement de zone/bias/distance = nouveau setup (state_hash différent)
4.  DEX ACTIVE→ACTIONABLE même direction = pas nouveau setup (usable_group identique)
5.  DEX bearish→bullish = nouveau setup (direction change → dedup_key différente)
6.  Comptage n_setups_total vs total_observations
7.  Setup fermé correctement à la transition d'état
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.setup_tracker import SetupTracker


# ─────────────────────────────────────────────────────────────────────────────
# Helper : SetupTracker en mémoire (pas de fichiers)
# ─────────────────────────────────────────────────────────────────────────────

def _make_tracker(td: str) -> SetupTracker:
    """SetupTracker isolé dans un répertoire temporaire."""
    active_path   = Path(td) / "active_setups.json"
    registry_path = Path(td) / "setup_registry.jsonl"
    with (
        patch("backend.setup_tracker.ACTIVE_SETUPS_PATH",   active_path),
        patch("backend.setup_tracker.SETUP_REGISTRY_PATH",  registry_path),
    ):
        return SetupTracker()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Max Pain dedup — même clé = même setup
# ─────────────────────────────────────────────────────────────────────────────

def test_max_pain_same_dedup_key_is_1_setup():
    """
    RÈGLE : max_pain_near_pull avec même expiry+strike+direction (même dedup_key)
    = 1 setup unique, peu importe le nombre de polls.
    """
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)
        key = "max_pain_pull_2026-06-07_70000_UP"

        is_new1, sid1 = tracker.process_observation("max_pain_near_pull", key)
        is_new2, sid2 = tracker.process_observation("max_pain_near_pull", key)
        is_new3, sid3 = tracker.process_observation("max_pain_near_pull", key)

        assert is_new1 is True,  "Première observation = nouveau setup"
        assert is_new2 is False, "Même état → pas de nouveau setup"
        assert is_new3 is False, "Même état → pas de nouveau setup"
        assert sid1 == sid2 == sid3, "setup_id identique pour un état persistant"


def test_max_pain_different_strike_is_new_setup():
    """max_pain strike change (ex: 70k → 71k) = nouveau setup."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        key1 = "max_pain_pull_2026-06-07_70000_UP"
        key2 = "max_pain_pull_2026-06-07_71000_UP"

        is_new1, sid1 = tracker.process_observation("max_pain_near_pull", key1)
        is_new2, sid2 = tracker.process_observation("max_pain_near_pull", key2)

        assert is_new1 is True
        assert is_new2 is True, "Strike différent = changement d'état = nouveau setup"
        assert sid1 != sid2


def test_max_pain_direction_flip_is_new_setup():
    """max_pain direction change (UP→DOWN) = nouveau setup."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        key_up   = "max_pain_pull_2026-06-07_70000_UP"
        key_down = "max_pain_pull_2026-06-07_70000_DOWN"

        is_new1, sid1 = tracker.process_observation("max_pain_near_pull", key_up)
        is_new2, sid2 = tracker.process_observation("max_pain_near_pull", key_down)

        assert is_new1 is True
        assert is_new2 is True, "Direction flip = nouveau setup"
        assert sid1 != sid2


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gravity même état = 1 setup
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_same_state_hash_is_1_setup():
    """
    RÈGLE : zone explosive dont zone_center, bias et dist_bucket ne changent pas
    = 1 setup persistant, observation_count++ mais pas de nouveau setup.
    """
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)
        # state_hash calculé par alerts.py : gravity_{etype}_{center_k}_{bias}_{dist_bucket}
        state_hash = "gravity_gravity_explosive_down_70000_DOWN_ONLY_2"

        is_new1, sid1 = tracker.process_observation("gravity_explosive", state_hash)
        is_new2, sid2 = tracker.process_observation("gravity_explosive", state_hash)
        is_new3, sid3 = tracker.process_observation("gravity_explosive", state_hash)

        assert is_new1 is True
        assert is_new2 is False, "Même zone+bias+distance = même setup persistant"
        assert is_new3 is False
        assert sid1 == sid2 == sid3

        # observation_count doit refléter les 3 appels
        setup = tracker._active.get("gravity_explosive")
        assert setup is not None
        assert setup.observation_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# 3. Gravity changement de zone/bias = nouveau setup
# ─────────────────────────────────────────────────────────────────────────────

def test_gravity_zone_change_is_new_setup():
    """Zone explosive change de centre (70k → 72k) = nouveau setup."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        hash1 = "gravity_gravity_explosive_down_70000_DOWN_ONLY_2"
        hash2 = "gravity_gravity_explosive_down_72000_DOWN_ONLY_2"

        is_new1, sid1 = tracker.process_observation("gravity_explosive", hash1)
        is_new2, sid2 = tracker.process_observation("gravity_explosive", hash2)

        assert is_new1 is True
        assert is_new2 is True, "Centre de zone différent = changement d'état = nouveau setup"
        assert sid1 != sid2


def test_gravity_bias_change_is_new_setup():
    """Zone explosive change de bias (DOWN_ONLY → UP_ONLY) = nouveau setup."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        hash_down = "gravity_gravity_explosive_down_70000_DOWN_ONLY_2"
        hash_up   = "gravity_gravity_explosive_up_70000_UP_ONLY_2"

        is_new1, sid1 = tracker.process_observation("gravity_explosive", hash_down)
        is_new2, sid2 = tracker.process_observation("gravity_explosive", hash_up)

        assert is_new1 is True
        assert is_new2 is True, "Changement de bias = changement d'état = nouveau setup"
        assert sid1 != sid2


def test_gravity_distance_bucket_change_is_new_setup():
    """Zone explosive change de distance bucket (2%→4%) = nouveau setup."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        hash_near = "gravity_gravity_explosive_down_70000_DOWN_ONLY_2"
        hash_far  = "gravity_gravity_explosive_down_70000_DOWN_ONLY_4"

        is_new1, sid1 = tracker.process_observation("gravity_explosive", hash_near)
        is_new2, sid2 = tracker.process_observation("gravity_explosive", hash_far)

        assert is_new1 is True
        assert is_new2 is True, "Distance bucket différent = changement d'état = nouveau setup"
        assert sid1 != sid2


# ─────────────────────────────────────────────────────────────────────────────
# 4. DEX ACTIVE→ACTIONABLE même direction = pas nouveau setup
# ─────────────────────────────────────────────────────────────────────────────

def test_dex_active_to_actionable_same_direction_no_new_setup():
    """
    RÈGLE : ACTIVE et ACTIONABLE sont groupés dans "usable".
    ACTIVE→ACTIONABLE sur la même direction = même dedup_key = même setup.
    """
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        # La clé est construite par alerts.py :
        # f"obs_{dex_etype}_{dex_dir_ud}_{usable_group}_{pressure_bucket}"
        # ACTIVE et ACTIONABLE → usable_group = "usable"
        key_active     = "obs_dex_bearish_DOWN_usable_50"
        key_actionable = "obs_dex_bearish_DOWN_usable_50"  # même clé car même usable_group

        is_new1, sid1 = tracker.process_observation("dex_bearish", key_active)
        is_new2, sid2 = tracker.process_observation("dex_bearish", key_actionable)

        assert is_new1 is True
        assert is_new2 is False, "ACTIVE→ACTIONABLE même dir = même usable_group = même setup"
        assert sid1 == sid2


def test_dex_non_usable_to_usable_is_new_setup():
    """DORMANT/STRUCTURAL (non_usable) → ACTIVE (usable) = nouveau setup."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        key_non_usable = "obs_dex_bearish_DOWN_non_usable_30"
        key_usable     = "obs_dex_bearish_DOWN_usable_30"

        is_new1, sid1 = tracker.process_observation("dex_bearish", key_non_usable)
        is_new2, sid2 = tracker.process_observation("dex_bearish", key_usable)

        assert is_new1 is True
        assert is_new2 is True, "non_usable→usable = changement de groupe = nouveau setup"
        assert sid1 != sid2


# ─────────────────────────────────────────────────────────────────────────────
# 5. DEX bearish→bullish = nouveau setup
# ─────────────────────────────────────────────────────────────────────────────

def test_dex_bearish_to_bullish_is_new_setup():
    """
    RÈGLE : direction DEX change (bearish→bullish) = dedup_key différente = nouveau setup.
    """
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        key_bearish = "obs_dex_bearish_DOWN_usable_50"
        key_bullish = "obs_dex_bullish_UP_usable_50"

        is_new1, sid1 = tracker.process_observation("dex_bearish", key_bearish)
        # DEX bascule bullish : event_type ET dedup_key changent
        is_new2, sid2 = tracker.process_observation("dex_bullish", key_bullish)

        assert is_new1 is True
        assert is_new2 is True, "Direction flip bearish→bullish = nouveau setup"
        assert sid1 != sid2


def test_dex_bullish_to_bearish_then_back_is_two_setups():
    """DEX bullish → bearish → bullish = 3 setups distincts (pas de réutilisation de sid)."""
    with tempfile.TemporaryDirectory() as td:
        tracker = _make_tracker(td)

        is_new1, sid1 = tracker.process_observation("dex_bullish",  "obs_dex_bullish_UP_usable_50")
        is_new2, sid2 = tracker.process_observation("dex_bearish",  "obs_dex_bearish_DOWN_usable_50")
        is_new3, sid3 = tracker.process_observation("dex_bullish",  "obs_dex_bullish_UP_usable_60")

        assert is_new1 and is_new2 and is_new3, "Chaque changement de direction = nouveau setup"
        assert len({sid1, sid2, sid3}) == 3, "3 setup_id distincts"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Comptage n_setups_total vs total_observations
# ─────────────────────────────────────────────────────────────────────────────

def test_observation_count_vs_setup_count():
    """
    55 observations du même état marché = 1 setup, observation_count=55.
    N statistique = 1, pas 55.
    """
    with tempfile.TemporaryDirectory() as td:
        active_path   = Path(td) / "active_setups.json"
        registry_path = Path(td) / "setup_registry.jsonl"
        with (
            patch("backend.setup_tracker.ACTIVE_SETUPS_PATH",  active_path),
            patch("backend.setup_tracker.SETUP_REGISTRY_PATH", registry_path),
        ):
            tracker = SetupTracker()
            key = "obs_dex_bearish_DOWN_usable_50"

            for i in range(55):
                tracker.process_observation("dex_bearish", key)

            setup = tracker._active.get("dex_bearish")
            assert setup is not None
            assert setup.observation_count == 55, "55 appels = observation_count 55"

            # Un seul setup actif (aucun setup fermé → registry vide)
            counts = tracker.get_setup_counts(days=30)
            dex_count = counts.get("dex_bearish", {})
            assert dex_count.get("n_setups_active", 0) == 1, "1 setup actif, pas 55"
            assert dex_count.get("n_setups_closed", 0) == 0, "Aucun setup fermé"
            assert dex_count.get("total_observations", 0) == 55


# ─────────────────────────────────────────────────────────────────────────────
# 7. Setup fermé correctement à la transition d'état
# ─────────────────────────────────────────────────────────────────────────────

def test_setup_closed_on_state_change():
    """
    Quand l'état change, l'ancien setup doit être fermé (setup_closed_at renseigné)
    et enregistré dans le registry.
    """
    with tempfile.TemporaryDirectory() as td:
        active_path   = Path(td) / "active_setups.json"
        registry_path = Path(td) / "setup_registry.jsonl"
        with (
            patch("backend.setup_tracker.ACTIVE_SETUPS_PATH",  active_path),
            patch("backend.setup_tracker.SETUP_REGISTRY_PATH", registry_path),
        ):
            tracker = SetupTracker()

            is_new1, sid1 = tracker.process_observation("gravity_explosive", "hash_A")
            # Changement d'état → ferme hash_A, ouvre hash_B
            is_new2, sid2 = tracker.process_observation("gravity_explosive", "hash_B")

            assert is_new2 is True
            assert sid1 != sid2

            # L'ancien setup doit être dans le registry
            assert registry_path.exists(), "Le registry doit exister après une fermeture de setup"
            lines = [l.strip() for l in registry_path.read_text().splitlines() if l.strip()]
            assert len(lines) == 1, "Un setup fermé dans le registry"

            closed = json.loads(lines[0])
            assert closed["setup_id"] == sid1
            assert closed["setup_closed_at"] is not None
            assert closed["state_hash"] == "hash_A"
