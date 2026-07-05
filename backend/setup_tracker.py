"""
Setup Tracker — Couche statistique : Setup Unique / Régime Unique.

Observation ≠ Échantillon statistique.
Un setup = une configuration marché unique ayant un outcome propre.
Un setup persiste tant que l'état (state_hash) ne change pas.
Nouveau setup SEULEMENT sur changement d'état.
"""

import json
import logging
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

_DATA_DIR = Path("/data")

SETUP_REGISTRY_PATH: Path = (
    (_DATA_DIR / "setup_registry.jsonl") if _DATA_DIR.exists()
    else (Path(__file__).parent / "setup_registry.jsonl")
)

ACTIVE_SETUPS_PATH: Path = (
    (_DATA_DIR / "active_setups.json") if _DATA_DIR.exists()
    else (Path(__file__).parent / "active_setups.json")
)

log = logging.getLogger(__name__)


def _infer_direction(event_type: str, state_params: dict) -> str:
    """Returns 'bullish', 'bearish', or 'neutral' from state_params or event_type name."""
    direction = (state_params or {}).get("direction", "")
    if direction in ("UP", "BULLISH", "UP_ONLY"):
        return "bullish"
    if direction in ("DOWN", "BEARISH", "DOWN_ONLY"):
        return "bearish"
    if direction == "SYMMETRIC":
        return "neutral"
    name = event_type.lower()
    if any(x in name for x in ("_bullish", "_buy", "bullish_")):
        return "bullish"
    if any(x in name for x in ("_bearish", "_sell", "bearish_")):
        return "bearish"
    if name.endswith("_up") or "_up_" in name:
        return "bullish"
    if name.endswith("_down") or "_down_" in name:
        return "bearish"
    return "neutral"


@dataclass
class Setup:
    setup_id:          str
    event_type:        str
    state_hash:        str
    state_params:      dict
    setup_started_at:  str
    setup_closed_at:   Optional[str] = None
    observation_count: int = 1


class SetupTracker:
    """
    Tracks unique market setups per event_type.

    Same state_hash = same setup (observation_count++, no new PendingEvent).
    State change = new setup (close old, open new PendingEvent).
    """

    def __init__(self) -> None:
        self._active: Dict[str, Setup] = {}
        self._load_active()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_active(self) -> None:
        if not ACTIVE_SETUPS_PATH.exists():
            return
        try:
            with open(ACTIVE_SETUPS_PATH) as f:
                data = json.load(f)
            fields = set(Setup.__dataclass_fields__)
            for et, sdata in data.items():
                s = Setup(**{k: v for k, v in sdata.items() if k in fields})
                if s.setup_closed_at is None:
                    self._active[et] = s
        except Exception as e:
            log.warning(f"[setup_tracker] load active: {e}")

    def _save_active(self) -> None:
        try:
            with open(ACTIVE_SETUPS_PATH, "w") as f:
                json.dump(
                    {et: asdict(s) for et, s in self._active.items()},
                    f, default=str,
                )
        except Exception as e:
            log.warning(f"[setup_tracker] save active: {e}")

    def _write_closed(self, setup: Setup) -> None:
        try:
            with open(SETUP_REGISTRY_PATH, "a") as f:
                f.write(json.dumps(asdict(setup), default=str) + "\n")
        except Exception as e:
            log.warning(f"[setup_tracker] write closed: {e}")

    # ── Core API ─────────────────────────────────────────────────────────────

    def process_observation(
        self,
        event_type:   str,
        state_hash:   str,
        state_params: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """
        Process one observation for the given event_type + state_hash.

        Returns:
            (is_new_setup, setup_id)

            is_new_setup=True  → state changed, caller MUST create a new PendingEvent
            is_new_setup=False → same state continues, no new PendingEvent needed
        """
        active = self._active.get(event_type)

        if active is not None and active.state_hash == state_hash:
            active.observation_count += 1
            self._save_active()
            return False, active.setup_id

        # State changed → close old setup, open new one
        if active is not None:
            active.setup_closed_at = datetime.now(timezone.utc).isoformat()
            self._write_closed(active)
            log.info(
                f"[setup_tracker] CLOSED {active.setup_id} ({event_type}) "
                f"after {active.observation_count} obs  hash={active.state_hash}"
            )

        new_id = uuid.uuid4().hex[:10]
        new_setup = Setup(
            setup_id=new_id,
            event_type=event_type,
            state_hash=state_hash,
            state_params=state_params or {},
            setup_started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._active[event_type] = new_setup
        self._save_active()
        log.info(
            f"[setup_tracker] NEW {new_id} ({event_type}) hash={state_hash}"
        )
        return True, new_id

    def get_active_setup_id(self, event_type: str) -> Optional[str]:
        s = self._active.get(event_type)
        return s.setup_id if s else None

    def get_setup_counts(self, days: int = 30) -> Dict[str, dict]:
        """
        Returns enriched setup counts by event_type within the last N days.

        Per key:
            n_setups_closed        int    — setups fermés dans la fenêtre
            n_setups_active        int    — setups actifs en mémoire
            n_setups_total         int    — total
            total_observations     int    — observations accumulées
            avg_obs_per_setup      float  — alias compression_ratio
            compression_ratio      float  — observations / setup (spam detector)
            avg_setup_duration_hours float|None — durée moyenne setups fermés (h)
            bullish_setups         int    — setups direction haussière
            bearish_setups         int    — setups direction baissière
            neutral_setups         int    — setups sans direction claire
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        def _init() -> dict:
            return {
                "n_setups_closed": 0, "n_setups_active": 0,
                "n_setups_total": 0, "total_observations": 0,
                "avg_obs_per_setup": 0.0, "compression_ratio": 0.0,
                "avg_setup_duration_hours": None,
                "bullish_setups": 0, "bearish_setups": 0, "neutral_setups": 0,
                "_dur_sum": 0.0, "_dur_n": 0,
            }

        counts: Dict[str, dict] = {}

        if SETUP_REGISTRY_PATH.exists():
            try:
                with open(SETUP_REGISTRY_PATH) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            et = rec.get("event_type", "")
                            ts_raw = rec.get("setup_started_at", "")
                            if not et or not ts_raw:
                                continue
                            ts = datetime.fromisoformat(ts_raw)
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts < cutoff:
                                continue
                            if et not in counts:
                                counts[et] = _init()
                            c = counts[et]
                            c["n_setups_closed"]    += 1
                            c["total_observations"] += rec.get("observation_count", 1)
                            # Direction
                            sp = rec.get("state_params") or {}
                            d  = _infer_direction(et, sp)
                            c[f"{d}_setups"] += 1
                            # Duration
                            closed_raw = rec.get("setup_closed_at")
                            if closed_raw:
                                try:
                                    closed_dt = datetime.fromisoformat(closed_raw)
                                    if closed_dt.tzinfo is None:
                                        closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                                    dur_h = (closed_dt - ts).total_seconds() / 3600
                                    if dur_h >= 0:
                                        c["_dur_sum"] += dur_h
                                        c["_dur_n"]   += 1
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"[setup_tracker] get_setup_counts registry: {e}")

        for et, setup in self._active.items():
            if et not in counts:
                counts[et] = _init()
            c = counts[et]
            c["n_setups_active"]    += 1
            c["total_observations"] += setup.observation_count
            d = _infer_direction(et, setup.state_params or {})
            c[f"{d}_setups"] += 1

        for c in counts.values():
            c["n_setups_total"] = c["n_setups_closed"] + c["n_setups_active"]
            total = c["n_setups_total"]
            obs   = c["total_observations"]
            ratio = round(obs / total, 1) if total > 0 else 0.0
            c["avg_obs_per_setup"]  = ratio
            c["compression_ratio"]  = ratio
            dur_n = c.pop("_dur_n")
            dur_s = c.pop("_dur_sum")
            c["avg_setup_duration_hours"] = round(dur_s / dur_n, 1) if dur_n > 0 else None

        return counts


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[SetupTracker] = None


def get_setup_tracker() -> SetupTracker:
    global _instance
    if _instance is None:
        _instance = SetupTracker()
    return _instance
