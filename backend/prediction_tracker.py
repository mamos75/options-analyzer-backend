"""
Validation post-événement des alertes Telegram.

Pour chaque alerte envoyée, vérifie le BTC price à +1h, +4h, +24h
et classe : ✅ Confirmée / ⚠️ Partiellement / ❌ Invalidée.

Métrique : Prediction Accuracy par type d'alerte.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

_DATA_DIR = Path("/data")
PREDICTION_LOG_PATH = (
    (_DATA_DIR / "prediction_log.jsonl") if _DATA_DIR.exists()
    else (Path(__file__).parent / "prediction_log.jsonl")
)
PENDING_FILE = (
    (_DATA_DIR / "pending_validations.json") if _DATA_DIR.exists()
    else (Path(__file__).parent / "pending_validations.json")
)

log = logging.getLogger(__name__)

ALERT_TYPES = ["Wall Break", "Wall Reinforcement", "GEX Flip", "Max Pain Shift", "Gravity Alert"]

VERDICT_LABELS = {
    "confirmed":   "✅ Confirmée",
    "partial":     "⚠️ Partielle",
    "invalidated": "❌ Invalidée",
}


def _log_prediction(entry: dict) -> None:
    try:
        with open(PREDICTION_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.warning(f"[prediction] write error: {e}")


def _topic_and_etypes_to_type(topic: str, event_types: List[str]) -> str:
    if "gex_flip" in topic:
        return "GEX Flip"
    if "squeeze" in topic:
        return "Gravity Alert"
    if "max_pain" in topic:
        return "Max Pain Shift"
    if "wall_break_up" in event_types or "wall_break_down" in event_types:
        return "Wall Break"
    return "Wall Reinforcement"


def _classify(
    alert_type: str,
    direction: Optional[str],
    btc_at_alert: float,
    level: float,
    btc_1h: Optional[float],
    btc_4h: Optional[float],
    btc_24h: Optional[float],
) -> str:
    if btc_at_alert <= 0:
        return "partial"

    def chg(btc: Optional[float]) -> Optional[float]:
        if btc is None:
            return None
        return (btc - btc_at_alert) / btc_at_alert * 100

    c4h  = chg(btc_4h)
    c24h = chg(btc_24h)
    primary = c4h if c4h is not None else c24h

    if alert_type == "Wall Break":
        if primary is None:
            return "partial"
        if direction == "up":
            if primary >= 1.5:
                return "confirmed"
            if primary < -0.5:
                return "invalidated"
        elif direction == "down":
            if primary <= -1.5:
                return "confirmed"
            if primary > 0.5:
                return "invalidated"
        return "partial"

    elif alert_type == "Wall Reinforcement":
        # Alerte dit "le mur tient" — vérifie à 24h si le niveau a été respecté
        p = btc_24h if btc_24h is not None else btc_4h
        if p is None:
            return "partial"
        is_resistance = level > btc_at_alert
        if is_resistance:
            return "confirmed" if p < level * 1.005 else "invalidated"
        else:
            return "confirmed" if p > level * 0.995 else "invalidated"

    elif alert_type == "GEX Flip":
        if primary is None:
            return "partial"
        if direction == "amplify":
            if abs(primary) >= 2.0:
                return "confirmed"
            if abs(primary) < 0.5:
                return "invalidated"
        elif direction == "stabilize":
            if abs(primary) < 1.0:
                return "confirmed"
            if abs(primary) >= 3.0:
                return "invalidated"
        return "partial"

    elif alert_type == "Gravity Alert":
        # Squeeze alert — mesure sur 24h
        p = c24h if c24h is not None else c4h
        if p is None:
            return "partial"
        if abs(p) >= 5.0:
            return "confirmed"
        if abs(p) < 1.0:
            return "invalidated"
        return "partial"

    elif alert_type == "Max Pain Shift":
        p = c24h if c24h is not None else c4h
        if p is None:
            return "partial"
        if abs(p) >= 2.0:
            return "confirmed"
        return "partial"

    return "partial"


@dataclass
class PendingValidation:
    alert_id:    str
    alert_type:  str
    topic:       str
    direction:   Optional[str]
    level:       float
    btc_at_alert: float
    sent_at:     str
    btc_1h:      Optional[float] = None
    btc_4h:      Optional[float] = None
    btc_24h:     Optional[float] = None
    btc_72h:     Optional[float] = None
    checked_1h:  bool = False
    checked_4h:  bool = False
    checked_24h: bool = False
    checked_72h: bool = False
    verdict:     Optional[str] = None


class PredictionTracker:
    def __init__(self):
        self._pending: Dict[str, PendingValidation] = {}
        self._load_pending()

    # ── Persistance ──────────────────────────────────────────────────────────

    def _load_pending(self):
        if not PENDING_FILE.exists():
            return
        try:
            with open(PENDING_FILE) as f:
                data = json.load(f)
            for item in data:
                pv = PendingValidation(**{k: v for k, v in item.items() if k in PendingValidation.__dataclass_fields__})
                if pv.verdict is None:
                    self._pending[pv.alert_id] = pv
            log.info(f"[prediction] {len(self._pending)} validations pendantes chargées")
        except Exception as e:
            log.warning(f"[prediction] chargement pending: {e}")

    def _save_pending(self):
        try:
            data = [asdict(pv) for pv in self._pending.values()]
            with open(PENDING_FILE, "w") as f:
                json.dump(data, f, default=str)
        except Exception as e:
            log.warning(f"[prediction] sauvegarde pending: {e}")

    # ── Enregistrement ────────────────────────────────────────────────────────

    def register(
        self,
        alert_id:    str,
        topic:       str,
        event_types: List[str],
        btc_at_alert: float,
        level:       float,
        direction:   Optional[str] = None,
        metadata:    Optional[dict] = None,
    ):
        alert_type = _topic_and_etypes_to_type(topic, event_types)

        # Pour GEX flip, direction dérivée du metadata (regime_new)
        if alert_type == "GEX Flip" and metadata:
            direction = "amplify" if metadata.get("regime_new") == "AMPLIFICATEUR" else "stabilize"

        pv = PendingValidation(
            alert_id=alert_id,
            alert_type=alert_type,
            topic=topic,
            direction=direction,
            level=level,
            btc_at_alert=btc_at_alert,
            sent_at=datetime.now(timezone.utc).isoformat(),
        )
        self._pending[alert_id] = pv
        self._save_pending()
        log.info(f"[prediction] enregistré {alert_id} type={alert_type} dir={direction} BTC=${btc_at_alert:,.0f}")

    # ── Loop de validation ────────────────────────────────────────────────────

    async def run_validation_loop(self):
        """Vérifie toutes les 5 min les alertes pendantes (+1h/+4h/+24h)."""
        while True:
            await asyncio.sleep(300)
            await self._check_pending()

    async def _fetch_btc_price(self) -> float:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            resp.raise_for_status()
            return float(resp.json()["price"])

    async def _check_pending(self):
        if not self._pending:
            return

        try:
            btc_now = await self._fetch_btc_price()
        except Exception as e:
            log.warning(f"[prediction] fetch BTC price: {e}")
            return

        now     = datetime.now(timezone.utc)
        changed = False

        for alert_id, pv in list(self._pending.items()):
            try:
                sent_at = datetime.fromisoformat(pv.sent_at)
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            elapsed = (now - sent_at).total_seconds()

            if not pv.checked_1h and elapsed >= 3600:
                pv.btc_1h     = btc_now
                pv.checked_1h = True
                changed       = True
                chg = (btc_now - pv.btc_at_alert) / pv.btc_at_alert * 100
                log.info(f"[prediction] +1h {alert_id} ({pv.alert_type}): BTC=${btc_now:,.0f} ({chg:+.2f}%)")

            if not pv.checked_4h and elapsed >= 14400:
                pv.btc_4h     = btc_now
                pv.checked_4h = True
                changed       = True
                chg = (btc_now - pv.btc_at_alert) / pv.btc_at_alert * 100
                log.info(f"[prediction] +4h {alert_id} ({pv.alert_type}): BTC=${btc_now:,.0f} ({chg:+.2f}%)")

            if not pv.checked_24h and elapsed >= 86400:
                pv.btc_24h     = btc_now
                pv.checked_24h = True
                changed        = True

                verdict = _classify(
                    pv.alert_type, pv.direction,
                    pv.btc_at_alert, pv.level,
                    pv.btc_1h, pv.btc_4h, pv.btc_24h,
                )
                pv.verdict = verdict or "partial"
                log.info(f"[prediction] +24h verdict {alert_id}: {pv.verdict}")

            if not pv.checked_72h and elapsed >= 259200:
                pv.btc_72h     = btc_now
                pv.checked_72h = True
                changed        = True

                def _chg(b):
                    if b is None or pv.btc_at_alert == 0:
                        return None
                    return round((b - pv.btc_at_alert) / pv.btc_at_alert * 100, 2)

                _log_prediction({
                    "ts":          now.isoformat(),
                    "alert_id":    alert_id,
                    "alert_type":  pv.alert_type,
                    "topic":       pv.topic,
                    "direction":   pv.direction,
                    "level":       pv.level,
                    "btc_at_alert": pv.btc_at_alert,
                    "btc_1h":      pv.btc_1h,
                    "btc_4h":      pv.btc_4h,
                    "btc_24h":     pv.btc_24h,
                    "btc_72h":     pv.btc_72h,
                    "chg_1h_pct":  _chg(pv.btc_1h),
                    "chg_4h_pct":  _chg(pv.btc_4h),
                    "chg_24h_pct": _chg(pv.btc_24h),
                    "chg_72h_pct": _chg(pv.btc_72h),
                    "verdict":     pv.verdict,
                })

                del self._pending[alert_id]
                log.info(f"[prediction] verdict final {alert_id}: {pv.verdict}")

        if changed:
            self._save_pending()

    # ── Rapport ───────────────────────────────────────────────────────────────

    def get_accuracy_stats(self, days: int = 7) -> Dict[str, dict]:
        """Retourne les stats de précision par type d'alerte pour les N derniers jours."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stats: Dict[str, Dict[str, int]] = {}

        if not PREDICTION_LOG_PATH.exists():
            return {}

        try:
            with open(PREDICTION_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        ts = datetime.fromisoformat(entry["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                    except Exception:
                        continue

                    atype   = entry.get("alert_type", "Unknown")
                    verdict = entry.get("verdict", "partial")
                    if atype not in stats:
                        stats[atype] = {"confirmed": 0, "partial": 0, "invalidated": 0, "total": 0}
                    stats[atype][verdict] = stats[atype].get(verdict, 0) + 1
                    stats[atype]["total"] += 1
        except Exception as e:
            log.warning(f"[prediction] read stats: {e}")

        return stats

    def generate_accuracy_report(self, days: int = 7) -> str:
        stats = self.get_accuracy_stats(days)
        if not stats:
            return (
                f"📊 **PREDICTION ACCURACY ({days}j)**\n\n"
                "Aucune donnée — les validations s'accumulent après 24h par alerte envoyée.\n"
                "Continue à générer des alertes pour alimenter la métrique."
            )

        lines = [f"📊 **PREDICTION ACCURACY ({days}j)**\n"]

        for atype in ALERT_TYPES:
            if atype not in stats:
                continue
            s          = stats[atype]
            total      = s["total"]
            confirmed  = s.get("confirmed", 0)
            partial    = s.get("partial", 0)
            invalidated = s.get("invalidated", 0)
            rate       = confirmed / total * 100 if total else 0
            filled     = int(rate // 20)
            bar        = "🟩" * filled + "⬜" * (5 - filled)
            lines.append(f"{bar} **{atype}** — {rate:.0f}% confirmées ({confirmed}/{total})")
            lines.append(
                f"  ✅ {confirmed} confirmées | ⚠️ {partial} partielles | ❌ {invalidated} invalidées\n"
            )

        low_perf = [
            (t, s) for t, s in stats.items()
            if s["total"] >= 3 and s.get("confirmed", 0) / s["total"] < 0.50
        ]
        if low_perf:
            lines.append("**⚠️ Alertes faible prédictivité (< 50% — à réduire)**")
            for t, s in sorted(low_perf, key=lambda x: x[1].get("confirmed", 0) / x[1]["total"]):
                rate = s.get("confirmed", 0) / s["total"] * 100
                lines.append(f"• `{t}` : {rate:.0f}% → ajuster seuil ou réduire fréquence")

        return "\n".join(lines)
