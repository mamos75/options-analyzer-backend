"""
Event Store — Backtest / accuracy par event type.

Un signal actionnable sans suivi d'outcome = opinion.
Un signal + event log + MFE/MAE = système mesurable.

Event types :
  squeeze_bullish | squeeze_bearish
  wall_rejection  | wall_breakout
  gravity_magnet
  dealer_buy_pressure | dealer_sell_pressure
  mopi_bullish | mopi_bearish
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

_WRITE_FREQUENCY_SEC = int(os.environ.get("HISTORY_INTERVAL_SECONDS", 1800))

_DATA_DIR = Path("/data")
EVENT_LOG_PATH = (
    (_DATA_DIR / "event_store.jsonl") if _DATA_DIR.exists()
    else (Path(__file__).parent / "event_store.jsonl")
)
SILENT_LOG_PATH = (
    (_DATA_DIR / "event_store_silent.jsonl") if _DATA_DIR.exists()
    else (Path(__file__).parent / "event_store_silent.jsonl")
)
PENDING_EVENTS_PATH = (
    (_DATA_DIR / "event_pending.json") if _DATA_DIR.exists()
    else (Path(__file__).parent / "event_pending.json")
)

log = logging.getLogger(__name__)

EVENT_TYPES = [
    "squeeze_bullish",
    "squeeze_bearish",
    "squeeze_violent_move_unknown_direction",
    "wall_rejection",
    "wall_breakout",
    "wall_rejection_candidate",
    "wall_breakout_candidate",
    "wall_dormant_blocked",
    "wall_distance_blocked",
    "gravity_magnet",
    "dealer_buy_pressure",
    "dealer_sell_pressure",
    "mopi_bullish",
    "mopi_bearish",
    # Phase 1B — outcome tracking signaux silencieux
    "gex_regime",           # changement régime GEX (amplificateur/stabilisant)
    "gravity_explosive",    # zone explosive détectée (directionnelle ou symétrique)
    "max_pain_pull",        # attraction near-term max pain (cooldown 4h)
    "max_pain_shift",       # déplacement du strike near ≥1%
    "mopi_cross",           # crossing 55/45 MOPI
    # Phase 1C — observations DEX avec pipeline outcome (déduplication état direction+profil)
    "dex_bullish",          # pression haussière dealer active/actionnable
    "dex_bearish",          # pression baissière dealer active/actionnable
]

# Cooldowns déduplication pour log_silent_event (event_key → last_ts)
_SILENT_COOLDOWNS: Dict[str, timedelta] = {
    "dex_bullish":                             timedelta(hours=4),
    "dex_bearish":                             timedelta(hours=4),
    "mopi_bullish_cross":                      timedelta(hours=1),
    "mopi_bearish_cross":                      timedelta(hours=1),
    "mopi_velocity_up":                        timedelta(hours=1),
    "mopi_velocity_down":                      timedelta(hours=1),
    "mopi_extreme_high":                       timedelta(hours=4),
    "mopi_extreme_low":                        timedelta(hours=4),
    "gravity_magnet_active":                   timedelta(hours=4),
    "gravity_explosive_up":                    timedelta(minutes=30),
    "gravity_explosive_down":                  timedelta(minutes=30),
    "gravity_explosive_symmetric":             timedelta(minutes=30),
    "max_pain_near_pull":                      timedelta(hours=4),
    "gex_amplifier":                           timedelta(hours=4),
    "gex_stabilizer":                          timedelta(hours=4),
    "gex_flip_actionable":                     timedelta(hours=1),
    "gex_calibration_degraded":                timedelta(hours=4),
    "wall_dormant_blocked":                    timedelta(hours=4),
    "wall_distance_blocked":                   timedelta(hours=4),
    "wall_rejection_candidate":                timedelta(hours=1),
    "wall_breakout_candidate":                 timedelta(hours=1),
    "squeeze_violent_move_unknown_direction":   timedelta(hours=4),
}

# Cooldowns pour log_observation — déduplication par état (direction + profil)
# Un changement d'état (direction ou profil) bypass le cooldown via une nouvelle clé
_OBSERVATION_COOLDOWNS: Dict[str, timedelta] = {
    "dex_bullish":    timedelta(hours=1),
    "dex_bearish":    timedelta(hours=1),
}

# Préfixes famille pour comptage des observations dans collection_health
_FAMILY_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "DEX":      ("dex_",),
    "MOPI":     ("mopi_",),
    "GEX":      ("gex_",),
    "Gravity":  ("gravity_",),
    "Walls":    ("wall_",),
    "Squeeze":  ("squeeze_",),
    "MaxPain":  ("max_pain_",),
}

# (direction_rule, hit_threshold_pct)
# direction_rule: "UP" | "DOWN" | "ANY" | "REV"
_HIT_CONFIG: Dict[str, Tuple[str, float]] = {
    "squeeze_bullish":      ("UP",   1.5),
    "squeeze_bearish":      ("DOWN", 1.5),
    "wall_breakout":        ("ANY",  1.5),
    "wall_rejection":       ("REV",  0.5),
    "gravity_magnet":       ("ANY",  2.0),
    "dealer_buy_pressure":  ("UP",   1.0),
    "dealer_sell_pressure": ("DOWN", 1.0),
    "mopi_bullish":         ("UP",   1.5),
    "mopi_bearish":         ("DOWN", 1.5),
    # Phase 1B
    "gex_regime":           ("ANY",  2.0),  # regime → mouvement amplifié attendu
    "gravity_explosive":    ("ANY",  2.0),  # zone explosive → gros move
    "max_pain_pull":        ("ANY",  1.0),  # attraction vers max pain (directionnel)
    "max_pain_shift":       ("ANY",  1.5),  # shift max pain → repositionnement
    "mopi_cross":           ("ANY",  1.5),  # crossing MOPI 55/45 → directionnel
    # Phase 1C
    "dex_bullish":          ("UP",   1.5),  # pression dealer haussière → mouvement UP attendu
    "dex_bearish":          ("DOWN", 1.5),  # pression dealer baissière → mouvement DOWN attendu
}

# Squeeze prend 24h+ pour se résoudre — évaluer sur outcome_24h, pas 4h
_PRIMARY_24H: frozenset = frozenset({"squeeze_bullish", "squeeze_bearish"})


def _pct(price: float, ref: float) -> float:
    if ref == 0:
        return 0.0
    return (price - ref) / ref * 100


def _compute_mfe_mae(
    event_type: str,
    direction: Optional[str],
    spot: float,
    samples: List[List],
) -> Tuple[Optional[float], Optional[float]]:
    if not samples or spot == 0:
        return None, None

    changes = [_pct(s[1], spot) for s in samples]

    is_up = direction == "UP" or event_type in (
        "squeeze_bullish", "dealer_buy_pressure", "mopi_bullish"
    )
    is_down = direction == "DOWN" or event_type in (
        "squeeze_bearish", "dealer_sell_pressure", "mopi_bearish"
    )

    if is_up:
        mfe = max(changes)                      # favorable = prix monte (max positif)
        mae = max(0.0, -min(changes))           # adverse = drawdown (0 si jamais en négatif)
    elif is_down:
        mfe = max(0.0, -min(changes))           # favorable = prix baisse (amplitude négative)
        mae = max(0.0, max(changes))            # adverse = rebond contre la position
    else:
        mfe = max(abs(c) for c in changes)
        mae = None

    return (
        round(mfe, 3) if mfe is not None else None,
        round(mae, 3) if mae is not None else None,
    )


def _determine_hit(
    event_type: str,
    direction: Optional[str],
    outcome_4h: Optional[float],
    outcome_24h: Optional[float],
) -> Tuple[Optional[bool], Optional[bool]]:
    # Squeeze se joue sur 24h+ — fenêtre 4h trop courte pour confirmer la cassure
    if event_type in _PRIMARY_24H:
        primary = outcome_24h if outcome_24h is not None else outcome_4h
    else:
        primary = outcome_4h if outcome_4h is not None else outcome_24h
    if primary is None:
        return None, None

    dir_rule, threshold = _HIT_CONFIG.get(event_type, ("ANY", 1.5))

    if dir_rule == "UP":
        hit = primary >= threshold
        inv = primary <= -(threshold / 2)
    elif dir_rule == "DOWN":
        hit = primary <= -threshold
        inv = primary >= (threshold / 2)
    elif dir_rule == "REV":
        # wall_rejection : le prix doit rebondir (amplitude dans un sens ou l'autre)
        hit = abs(primary) >= threshold
        inv = abs(primary) < 0.2
    elif dir_rule == "ANY":
        if direction == "UP":
            hit = primary >= threshold
            inv = primary <= -(threshold / 3)
        elif direction == "DOWN":
            hit = primary <= -threshold
            inv = primary >= (threshold / 3)
        else:
            hit = abs(primary) >= threshold
            inv = abs(primary) < 0.3
    else:
        hit = abs(primary) >= threshold
        inv = abs(primary) < 0.2

    return bool(hit), bool(inv)


@dataclass
class PendingEvent:
    id:                   str
    ts:                   str
    event_type:           str
    spot_at_signal:       float
    signal_strength:      float
    quality_state:        str
    gex_near:             float
    mopi_score:           float
    squeeze_score:        float
    nearest_wall:         float
    nearest_gravity_zone: float
    direction:            Optional[str]

    # Remplis progressivement
    outcome_1h:  Optional[float] = None
    outcome_4h:  Optional[float] = None
    outcome_24h: Optional[float] = None
    checked_1h:  bool = False
    checked_4h:  bool = False
    checked_24h: bool = False

    # MFE/MAE sampling (retiré du log final)
    btc_samples: List[List] = field(default_factory=list)   # [[epoch, price], …]

    # Verdict final
    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion:   Optional[float] = None
    hit_target:              Optional[bool]  = None
    invalidated:             Optional[bool]  = None

    outcome_72h: Optional[float] = None
    checked_72h: bool = False

    # Tracking envoi / blocage
    sent:           bool            = True
    blocked_reason: Optional[str]   = None
    silent:         bool            = False   # observation sans alerte Telegram (log_observation)

    # Setup unique — un setup = une configuration marché distincte (state_hash unique)
    # Plusieurs observations du même état → même setup_id, un seul PendingEvent
    setup_id: Optional[str] = None


class EventStore:
    def __init__(self) -> None:
        self._pending: Dict[str, PendingEvent] = {}
        self._dedup_cache: Dict[str, datetime] = {}  # event_key → last logged ts
        self._load_pending()

    def _get_setup_tracker(self):
        """Lazy init du SetupTracker (évite l'import circulaire au niveau module)."""
        if not hasattr(self, "_setup_tracker_obj"):
            from .setup_tracker import get_setup_tracker
            self._setup_tracker_obj = get_setup_tracker()
        return self._setup_tracker_obj

    # ── Persistance ──────────────────────────────────────────────────────────

    def _load_pending(self) -> None:
        if not PENDING_EVENTS_PATH.exists():
            return
        try:
            with open(PENDING_EVENTS_PATH) as f:
                data = json.load(f)
            fields = set(PendingEvent.__dataclass_fields__)
            for item in data:
                ev = PendingEvent(**{k: v for k, v in item.items() if k in fields})
                if not ev.checked_72h:
                    self._pending[ev.id] = ev
            log.info(f"[event_store] {len(self._pending)} événements pendants chargés")
        except Exception as e:
            log.warning(f"[event_store] chargement pending: {e}")

    def _save_pending(self) -> None:
        try:
            with open(PENDING_EVENTS_PATH, "w") as f:
                json.dump([asdict(ev) for ev in self._pending.values()], f, default=str)
        except Exception as e:
            log.warning(f"[event_store] sauvegarde pending: {e}")

    def _write_final(self, ev: PendingEvent) -> None:
        record = asdict(ev)
        for key in ("btc_samples", "checked_1h", "checked_4h", "checked_24h", "checked_72h"):
            record.pop(key, None)
        try:
            with open(EVENT_LOG_PATH, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            log.warning(f"[event_store] write_final: {e}")

    # ── Enregistrement d'un signal ────────────────────────────────────────────

    def log_event(
        self,
        event_type:           str,
        spot:                 float,
        signal_strength:      float,
        quality_state:        str,
        gex_near:             float,
        mopi_score:           float,
        squeeze_score:        float,
        nearest_wall:         float,
        nearest_gravity_zone: float,
        direction:            Optional[str] = None,
        sent:                 bool          = True,
        blocked_reason:       Optional[str] = None,
        setup_id:             Optional[str] = None,
    ) -> str:
        if event_type not in EVENT_TYPES:
            log.warning(f"[event_store] event_type inconnu: {event_type}")
            return ""

        ev_id = uuid.uuid4().hex[:10]
        ev = PendingEvent(
            id=ev_id,
            ts=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            spot_at_signal=round(spot, 2),
            signal_strength=round(signal_strength, 2),
            quality_state=quality_state,
            gex_near=round(gex_near, 0),
            mopi_score=round(mopi_score, 2),
            squeeze_score=round(squeeze_score, 2),
            nearest_wall=round(nearest_wall, 0),
            nearest_gravity_zone=round(nearest_gravity_zone, 0),
            direction=direction,
            sent=sent,
            blocked_reason=blocked_reason,
            setup_id=setup_id,
        )
        self._pending[ev_id] = ev
        self._save_pending()
        log.info(
            f"[event_store] +{event_type} id={ev_id} "
            f"spot=${spot:,.0f} dir={direction} strength={signal_strength:.0f} "
            f"quality={quality_state} sent={sent} blocked={blocked_reason}"
        )
        return ev_id

    def log_silent_event(
        self,
        event_type:  str,
        spot:        float,
        direction:   Optional[str],
        indicators:  dict,
        metadata:    Optional[dict] = None,
        dedup_key:   Optional[str]  = None,
    ) -> None:
        """Enregistre un signal calculé mais non envoyé (threshold non atteint).
        Écrit directement dans event_store_silent.jsonl sans pipeline 72h.

        dedup_key : clé de déduplication (ex: "wall_dormant_blocked_70000").
          Si fourni, vérifie le cooldown dans _dedup_cache avant d'écrire.
          Cooldown par event_type défini dans _SILENT_COOLDOWNS (défaut 1h).
        """
        if dedup_key is not None:
            now_dt   = datetime.now(timezone.utc)
            last_ts  = self._dedup_cache.get(dedup_key)
            cooldown = _SILENT_COOLDOWNS.get(event_type, timedelta(hours=1))
            if last_ts is not None and (now_dt - last_ts) < cooldown:
                return
            self._dedup_cache[dedup_key] = now_dt
            # Tracking setup : comptage brut après chaque écriture dans le log
            try:
                self._get_setup_tracker().process_observation(event_type, dedup_key)
            except Exception:
                pass

        record = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "spot":       round(spot, 2),
            "direction":  direction,
            "sent":       False,
            "silent":     True,
            **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in indicators.items()},
            **(metadata or {}),
        }
        try:
            with open(SILENT_LOG_PATH, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            log.warning(f"[event_store] log_silent_event: {e}")

    def log_observation(
        self,
        event_type:           str,
        spot:                 float,
        direction:            Optional[str],
        signal_strength:      float,
        quality_state:        str,
        gex_near:             float = 0.0,
        mopi_score:           float = 50.0,
        squeeze_score:        float = 0.0,
        nearest_wall:         float = 0.0,
        nearest_gravity_zone: float = 0.0,
        indicators:           Optional[dict] = None,
        metadata:             Optional[dict] = None,
        dedup_key:            Optional[str]  = None,
        cooldown:             Optional[timedelta] = None,
    ) -> Optional[str]:
        """Observation silencieuse importante → log brut + pipeline outcome 72h.

        Contrairement à log_silent_event (log brut sans pipeline), cette méthode
        crée aussi un PendingEvent(sent=False, silent=True) pour suivi +4h/+24h/+72h
        sans envoyer aucune alerte Telegram.

        dedup_key doit encoder l'état (ex: "obs_dex_bearish_DOWN_ACTIVE") :
        tout changement d'état génère un nouveau key → nouveau PendingEvent,
        même si le cooldown n'est pas expiré.
        """
        if event_type not in EVENT_TYPES:
            log.warning(f"[event_store] log_observation: event_type inconnu: {event_type}")
            return None

        # ── 1. Setup dedup (état marché) — crée 1 PendingEvent par setup unique ──
        # Same dedup_key = même état → même setup → pas de nouveau PendingEvent.
        # Changement de dedup_key = changement d'état → nouveau setup → nouveau PendingEvent.
        is_new_setup = True
        setup_id: Optional[str] = None
        if dedup_key is not None:
            try:
                is_new_setup, setup_id = self._get_setup_tracker().process_observation(
                    event_type, dedup_key
                )
            except Exception:
                pass  # fallback : comportement legacy si setup_tracker indisponible

        # ── 2. Cooldown log brut (débit d'écriture dans silent_log) ──────────────
        write_silent = True
        if dedup_key is not None:
            now_dt  = datetime.now(timezone.utc)
            last_ts = self._dedup_cache.get(dedup_key)
            cd      = cooldown or _OBSERVATION_COOLDOWNS.get(event_type, timedelta(hours=1))
            if last_ts is not None and (now_dt - last_ts) < cd:
                write_silent = False
            else:
                self._dedup_cache[dedup_key] = now_dt

        # Skip complet si même état ET dans le cooldown
        if not is_new_setup and not write_silent:
            return None

        # ── 3. Log brut dans silent_log (traçabilité) ────────────────────────────
        if write_silent:
            record = {
                "ts":               datetime.now(timezone.utc).isoformat(),
                "event_type":       event_type,
                "spot":             round(spot, 2),
                "direction":        direction,
                "sent":             False,
                "silent":           True,
                "requires_outcome": True,
                **(indicators or {}),
                **(metadata or {}),
            }
            try:
                with open(SILENT_LOG_PATH, "a") as f:
                    f.write(json.dumps(record, default=str) + "\n")
            except Exception as e:
                log.warning(f"[event_store] log_observation write: {e}")

        # ── 4. PendingEvent — uniquement pour un nouveau setup ───────────────────
        if not is_new_setup:
            return None

        ev_id = uuid.uuid4().hex[:10]
        ev = PendingEvent(
            id=ev_id,
            ts=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            spot_at_signal=round(spot, 2),
            signal_strength=round(signal_strength, 2),
            quality_state=quality_state,
            gex_near=round(gex_near, 0),
            mopi_score=round(mopi_score, 2),
            squeeze_score=round(squeeze_score, 2),
            nearest_wall=round(nearest_wall, 0),
            nearest_gravity_zone=round(nearest_gravity_zone, 0),
            direction=direction,
            sent=False,
            blocked_reason="silent_observation",
            silent=True,
            setup_id=setup_id,
        )
        self._pending[ev_id] = ev
        self._save_pending()
        log.info(
            f"[event_store] +obs {event_type} id={ev_id} setup={setup_id} "
            f"spot=${spot:,.0f} dir={direction} quality={quality_state}"
        )
        return ev_id

    # ── Boucle de validation outcomes ─────────────────────────────────────────

    async def run_outcome_loop(self) -> None:
        """Toutes les 5 min : collecte MFE/MAE + calcule outcomes +1h/+4h/+24h."""
        while True:
            await asyncio.sleep(300)
            try:
                await self._tick()
            except Exception as e:
                log.error(f"[event_store] run_outcome_loop tick error (loop survit): {e}")

    async def _fetch_btc(self) -> float:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
            )
            r.raise_for_status()
            return float(r.json()["price"])

    async def _tick(self) -> None:
        if not self._pending:
            return
        try:
            btc_now = await self._fetch_btc()
        except Exception as e:
            log.warning(f"[event_store] fetch BTC: {e}")
            return

        now     = datetime.now(timezone.utc)
        epoch   = now.timestamp()
        changed = False

        for ev_id, ev in list(self._pending.items()):
            try:
                ts_signal = datetime.fromisoformat(ev.ts)
                if ts_signal.tzinfo is None:
                    ts_signal = ts_signal.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            try:
                elapsed = (now - ts_signal).total_seconds()

                if elapsed <= 86400 and len(ev.btc_samples) < 290:
                    ev.btc_samples.append([epoch, btc_now])
                    changed = True

                if not ev.checked_1h and elapsed >= 3600:
                    ev.outcome_1h = round(_pct(btc_now, ev.spot_at_signal), 3)
                    ev.checked_1h = True
                    changed       = True
                    log.info(
                        f"[event_store] +1h {ev_id} ({ev.event_type}): "
                        f"BTC=${btc_now:,.0f} ({ev.outcome_1h:+.2f}%)"
                    )

                if not ev.checked_4h and elapsed >= 14400:
                    ev.outcome_4h = round(_pct(btc_now, ev.spot_at_signal), 3)
                    ev.checked_4h = True
                    changed       = True
                    log.info(
                        f"[event_store] +4h {ev_id} ({ev.event_type}): "
                        f"BTC=${btc_now:,.0f} ({ev.outcome_4h:+.2f}%)"
                    )

                if not ev.checked_24h and elapsed >= 86400:
                    ev.outcome_24h = round(_pct(btc_now, ev.spot_at_signal), 3)
                    ev.checked_24h = True
                    ev.max_favorable_excursion, ev.max_adverse_excursion = _compute_mfe_mae(
                        ev.event_type, ev.direction, ev.spot_at_signal, ev.btc_samples
                    )
                    ev.hit_target, ev.invalidated = _determine_hit(
                        ev.event_type, ev.direction, ev.outcome_4h, ev.outcome_24h
                    )
                    changed = True
                    self._save_pending()  # persiste avant tout log
                    o1  = f"{ev.outcome_1h:+.2f}" if ev.outcome_1h is not None else "N/A"
                    o4  = f"{ev.outcome_4h:+.2f}" if ev.outcome_4h is not None else "N/A"
                    log.info(
                        f"[event_store] +24h {ev_id} ({ev.event_type}): "
                        f"1h={o1}% 4h={o4}% 24h={ev.outcome_24h:+.2f}% "
                        f"hit={ev.hit_target} inv={ev.invalidated} "
                        f"MFE={ev.max_favorable_excursion} MAE={ev.max_adverse_excursion}"
                    )

                if not ev.checked_72h and elapsed >= 259200:
                    ev.outcome_72h = round(_pct(btc_now, ev.spot_at_signal), 3)
                    ev.checked_72h = True
                    self._write_final(ev)
                    del self._pending[ev_id]
                    changed = True
                    log.info(
                        f"[event_store] verdict final {ev_id} ({ev.event_type}): "
                        f"72h={ev.outcome_72h:+.2f}%"
                    )
            except Exception as e:
                log.error(f"[event_store] tick error on event {ev_id}: {e}")

        if changed:
            self._save_pending()

    # ── API ───────────────────────────────────────────────────────────────────

    def get_accuracy_by_event_type(self, days: int = 30) -> dict:
        """Stats d'accuracy par event_type pour les N derniers jours."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stats: Dict[str, dict] = {}

        if not EVENT_LOG_PATH.exists():
            return {}

        try:
            with open(EVENT_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    try:
                        ts = datetime.fromisoformat(rec["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                    except Exception:
                        continue

                    etype = rec.get("event_type", "unknown")
                    if etype not in stats:
                        stats[etype] = {
                            "total": 0,
                            "hit": 0,
                            "invalidated": 0,
                            "neutral": 0,
                            "winrate_pct": 0.0,
                            "avg_outcome_4h": None,
                            "avg_outcome_24h": None,
                            "avg_outcome_72h": None,
                            "avg_mfe": None,
                            "avg_mae": None,
                            "_o4": [], "_o24": [], "_o72": [], "_mfe": [], "_mae": [],
                        }
                    s = stats[etype]
                    s["total"] += 1

                    # Migration on-the-fly : squeeze évalué sur 24h (anciens records sur 4h)
                    if etype in _PRIMARY_24H and rec.get("outcome_24h") is not None:
                        hit_t, inv_t = _determine_hit(
                            etype, rec.get("direction"),
                            None, rec["outcome_24h"],
                        )
                    else:
                        hit_t = rec.get("hit_target")
                        inv_t = rec.get("invalidated")

                    if hit_t is True:
                        s["hit"] += 1
                    elif inv_t is True:
                        s["invalidated"] += 1
                    else:
                        s["neutral"] += 1

                    if rec.get("outcome_4h") is not None:
                        s["_o4"].append(rec["outcome_4h"])
                    if rec.get("outcome_24h") is not None:
                        s["_o24"].append(rec["outcome_24h"])
                    if rec.get("outcome_72h") is not None:
                        s["_o72"].append(rec["outcome_72h"])
                    if rec.get("max_favorable_excursion") is not None:
                        s["_mfe"].append(rec["max_favorable_excursion"])
                    if rec.get("max_adverse_excursion") is not None:
                        s["_mae"].append(rec["max_adverse_excursion"])
        except Exception as e:
            log.warning(f"[event_store] get_accuracy: {e}")

        for s in stats.values():
            t = s["total"]
            s["winrate_pct"] = round(s["hit"] / t * 100, 1) if t else 0.0
            s["avg_outcome_4h"]  = round(sum(s["_o4"]) / len(s["_o4"]), 3) if s["_o4"] else None
            s["avg_outcome_24h"] = round(sum(s["_o24"]) / len(s["_o24"]), 3) if s["_o24"] else None
            s["avg_outcome_72h"] = round(sum(s["_o72"]) / len(s["_o72"]), 3) if s["_o72"] else None
            s["avg_mfe"] = round(sum(s["_mfe"]) / len(s["_mfe"]), 3) if s["_mfe"] else None
            s["avg_mae"] = round(sum(s["_mae"]) / len(s["_mae"]), 3) if s["_mae"] else None
            for k in ("_o4", "_o24", "_o72", "_mfe", "_mae"):
                del s[k]

        return stats

    def generate_accuracy_report(self, days: int = 30) -> str:
        stats = self.get_accuracy_by_event_type(days)
        pending_n = len(self._pending)

        if not stats:
            return (
                f"📊 **SIGNAL ACCURACY — {days}j**\n\n"
                f"Aucune donnée complète encore ({pending_n} signaux en attente de verdict).\n"
                "Les verdicts arrivent 24h après chaque signal actionnable."
            )

        lines = [f"📊 **SIGNAL ACCURACY — {days}j** ({pending_n} en attente)\n"]

        for etype in EVENT_TYPES:
            s = stats.get(etype)
            if not s:
                continue
            t    = s["total"]
            hit  = s["hit"]
            inv  = s["invalidated"]
            rate = s["winrate_pct"]
            bar  = "🟩" * int(rate // 20) + "⬜" * (5 - int(rate // 20))

            o4  = f"{s['avg_outcome_4h']:+.2f}%" if s["avg_outcome_4h"] is not None else "–"
            o24 = f"{s['avg_outcome_24h']:+.2f}%" if s["avg_outcome_24h"] is not None else "–"
            o72 = f"{s['avg_outcome_72h']:+.2f}%" if s["avg_outcome_72h"] is not None else "–"
            mfe = f"MFE={s['avg_mfe']:+.2f}%" if s["avg_mfe"] is not None else ""
            mae = f"MAE={s['avg_mae']:+.2f}%" if s["avg_mae"] is not None else ""

            lines.append(f"{bar} **{etype}** — {rate:.0f}% hit ({hit}/{t})")
            lines.append(f"  4h: {o4} | 24h: {o24} | 72h: {o72}  {mfe} {mae}")
            lines.append(f"  ✅{hit} ❌{inv} ➖{s['neutral']}\n")

        low = [
            (et, s) for et, s in stats.items()
            if s["total"] >= 5 and s["winrate_pct"] < 40
        ]
        if low:
            lines.append("**⚠️ Signaux sous-performants (< 40% hit — à recalibrer)**")
            for et, s in sorted(low, key=lambda x: x[1]["winrate_pct"]):
                lines.append(f"• `{et}` : {s['winrate_pct']:.0f}% → réduire seuil ou supprimer")

        return "\n".join(lines)

    def get_pending_count(self) -> int:
        return len(self._pending)

    def get_intermediate_accuracy_4h(self, min_n: int = 5) -> Dict[str, Optional[dict]]:
        """
        Scores preview basés sur les outcomes 4h des pending events.
        Retourne None par type si N < min_n.
        hit_4h = mouvement dans la bonne direction > 1% (0.5% pour max_pain).
        """
        from collections import defaultdict
        buckets: Dict[str, list] = defaultdict(list)
        for ev in self._pending.values():
            if not ev.checked_4h or ev.outcome_4h is None:
                continue
            direction = getattr(ev, "direction", None) or ""
            o4 = ev.outcome_4h
            # Directional accuracy pour preview 4h
            if direction == "DOWN":
                hit = o4 < 0
            elif direction == "UP":
                hit = o4 > 0
            else:
                # ANY/SYMMETRIC — vérifie amplitude vs demi-seuil (o4!=0 = tautologie)
                _, thresh = _HIT_CONFIG.get(ev.event_type, ("ANY", 1.0))
                hit = abs(o4) > thresh / 2
            buckets[ev.event_type].append((hit, o4))

        result: Dict[str, Optional[dict]] = {}
        for etype, pairs in buckets.items():
            if len(pairs) < min_n:
                result[etype] = None
                continue
            hits = sum(1 for h, _ in pairs if h)
            avgs = [o for _, o in pairs]
            result[etype] = {
                "n": len(pairs),
                "hit": hits,
                "winrate_4h_pct": round(hits / len(pairs) * 100, 1),
                "avg_outcome_4h": round(sum(avgs) / len(avgs), 2),
                "preview": True,
            }
        return result

    def _count_silent_by_family(self) -> Dict[str, int]:
        """Compte les observations silencieuses par famille depuis SILENT_LOG_PATH."""
        counts: Dict[str, int] = {fam: 0 for fam in _FAMILY_PREFIXES}
        if not SILENT_LOG_PATH.exists():
            return counts
        try:
            with open(SILENT_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        etype = json.loads(line).get("event_type", "")
                    except Exception:
                        continue
                    for fam, prefixes in _FAMILY_PREFIXES.items():
                        if any(etype.startswith(p) for p in prefixes):
                            counts[fam] += 1
                            break
        except Exception as e:
            log.warning(f"[event_store] _count_silent_by_family: {e}")
        return counts

    def get_collection_health(self) -> dict:
        """Retourne l'état de la collecte — Phase 0.

        status = "healthy"         → DB >1 ligne ET au moins un event (pending ou finalisé)
        status = "collecting_empty" → collecte insuffisante pour produire des stats
        """
        import sqlite3, time as _time
        now = _time.time()

        # ── options_history.db ────────────────────────────────────────────────
        db_path = (
            _DATA_DIR / "options_history.db" if _DATA_DIR.exists()
            else Path(__file__).parent / "options_history.db"
        )
        db_rows = 0
        db_last_ts = None
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT COUNT(*), MAX(ts) FROM metrics_history").fetchone()
            db_rows, db_last_ts = (row[0] or 0), row[1]
            conn.close()
        except Exception:
            pass

        last_snapshot_age_sec = int(now - db_last_ts) if db_last_ts else None

        # ── event_store.jsonl ─────────────────────────────────────────────────
        finalized = 0
        last_event_ts: Optional[float] = None
        if EVENT_LOG_PATH.exists():
            try:
                with open(EVENT_LOG_PATH) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        finalized += 1
                        try:
                            ts_val = json.loads(line).get("ts", "")
                            ts_f = datetime.fromisoformat(ts_val).timestamp()
                            if last_event_ts is None or ts_f > last_event_ts:
                                last_event_ts = ts_f
                        except Exception:
                            pass
            except Exception:
                pass

        last_event_age_sec = int(now - last_event_ts) if last_event_ts else None

        # ── Pending ───────────────────────────────────────────────────────────
        pending = list(self._pending.values())
        pending_sent    = sum(1 for e in pending if e.sent)
        pending_blocked = sum(1 for e in pending if not e.sent)

        # ── Stuck events (>25h sans checked_24h) ─────────────────────────────
        stuck_ids = []
        for e in pending:
            if e.checked_24h:
                continue
            try:
                ts_str = e.ts.replace("Z", "+00:00") if "Z" in e.ts else e.ts
                ev_ts  = datetime.fromisoformat(ts_str)
                if ev_ts.tzinfo is None:
                    ev_ts = ev_ts.replace(tzinfo=timezone.utc)
                if (now - ev_ts.timestamp()) > 90000:
                    stuck_ids.append(e.id)
            except Exception:
                pass

        # ── Warnings ──────────────────────────────────────────────────────────
        warnings = []
        if db_rows <= 1:
            warnings.append("options_history.db a ≤1 ligne — collecte non démarrée")
        if last_snapshot_age_sec is not None and last_snapshot_age_sec > _WRITE_FREQUENCY_SEC * 3:
            warnings.append(f"Dernier snapshot DB vieux de {last_snapshot_age_sec}s (attendu toutes les {_WRITE_FREQUENCY_SEC}s)")
        if stuck_ids:
            warnings.append(f"{len(stuck_ids)} events bloqués >25h sans checked_24h : {stuck_ids[:5]}")

        # ── Observations par famille (silent + pending + finalisé) ────────────
        obs_by_family = self._count_silent_by_family()
        # Enrichit avec les pending/finalisés qui ont une famille connue
        for ev in pending:
            etype = ev.event_type or ""
            for fam, prefixes in _FAMILY_PREFIXES.items():
                if any(etype.startswith(p) for p in prefixes):
                    obs_by_family[fam] = obs_by_family.get(fam, 0) + 1
                    break

        # Warning si une famille n'a aucune observation après 6h de collecte
        if db_rows > 10:  # collecte active depuis un moment
            zero_families = [f for f, c in obs_by_family.items() if c == 0]
            if zero_families:
                warnings.append(f"Familles sans observation : {', '.join(zero_families)}")

        # ── Status global ──────────────────────────────────────────────────────
        is_collecting = db_rows > 1 and (finalized > 0 or len(pending) > 0)
        status = "healthy" if (is_collecting and not warnings) else "collecting_empty"

        return {
            "status": status,
            "history_db": {
                "rows":                  db_rows,
                "last_snapshot_age_sec": last_snapshot_age_sec,
                "write_frequency_sec":   _WRITE_FREQUENCY_SEC,
            },
            "event_store": {
                "pending":            len(pending),
                "pending_sent":       pending_sent,
                "pending_blocked":    pending_blocked,
                "finalized":          finalized,
                "last_event_age_sec": last_event_age_sec,
            },
            "observations_by_family": obs_by_family,
            "warnings": warnings,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[EventStore] = None


def get_event_store() -> EventStore:
    global _instance
    if _instance is None:
        _instance = EventStore()
    return _instance
