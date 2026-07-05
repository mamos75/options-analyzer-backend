"""
Wall Lifecycle Store — état persistant d'un mur options sur la journée.

Problème résolu :
  Wall 78k renforcé × 3 + affaibli × 2 + disparu × 1 = 6 alertes individuelles
  → 6 Telegram bruyants et 62% inutile.

Solution :
  Un état unique par strike accumule les événements de la journée.
  Une SEULE alerte consolidée quand un changement significatif est détecté.

  🧱 WALL $78,000 — BILAN 6H
  Renforcé : 3x  |  Affaibli : 2x  |  Disparu : 1x
  Impact réel : Oui — maintenu > 4h, renforcement net +1
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional


# ── Seuils ────────────────────────────────────────────────────────────────────

# Changement de renforcement net requis pour déclencher un rapport
_NET_DELTA_THRESHOLD = 3

# Durée minimale d'existence d'un mur pour être considéré "structurellement significatif"
_MIN_DURATION_HOURS = 4.0

# Cooldown entre deux rapports sur le même strike
_REPORT_COOLDOWN = timedelta(hours=2)


@dataclass
class WallLifecycleState:
    strike:             float
    first_seen:         datetime
    last_seen:          datetime
    reinforced_count:   int = 0
    weakened_count:     int = 0
    disappeared_count:  int = 0
    reappeared_count:   int = 0
    current_tag:        str = "DORMANT"
    last_reported_at:   Optional[datetime] = None
    last_reported_net:  int = 0          # net_reinforcement au dernier rapport

    @property
    def duration_hours(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds() / 3600

    @property
    def net_reinforcement(self) -> int:
        return self.reinforced_count - self.weakened_count

    @property
    def total_events(self) -> int:
        return (
            self.reinforced_count + self.weakened_count
            + self.disappeared_count + self.reappeared_count
        )

    def is_structurally_significant(self) -> bool:
        return self.duration_hours >= _MIN_DURATION_HOURS and self.net_reinforcement > 0

    def cooldown_elapsed(self) -> bool:
        if self.last_reported_at is None:
            return True
        return datetime.now(timezone.utc) - self.last_reported_at >= _REPORT_COOLDOWN

    def should_send_summary(self, spot: float = 0.0) -> bool:
        """
        Déclencher un rapport si :
        - Changement net ≥ threshold depuis dernier rapport, ET cooldown écoulé
        - OU mur disparu (event important structurellement)

        Règle absolue (partagée avec Conviction Score) :
          distance >5% du spot = jamais un déclencheur, même avec confluence.
        """
        if spot > 0 and self.strike > 0:
            dist_pct = abs(self.strike - spot) / self.strike * 100
            if dist_pct > 5.0:
                return False
        if not self.cooldown_elapsed():
            return False
        delta = abs(self.net_reinforcement - self.last_reported_net)
        return delta >= _NET_DELTA_THRESHOLD or self.disappeared_count > 0

    def generate_summary(self, spot: float) -> str:
        side   = "résistance" if self.strike > spot else "support"
        impact = "Oui ✅" if self.is_structurally_significant() else "Non ❌"
        net    = self.net_reinforcement
        net_str = f"+{net}" if net > 0 else str(net)

        return (
            f"🧱 *WALL ${self.strike:,.0f} — BILAN {self.duration_hours:.1f}h*\n\n"
            f"Renforcé : {self.reinforced_count}x  |  "
            f"Affaibli : {self.weakened_count}x  |  "
            f"Net : {net_str}\n"
            f"Disparu : {self.disappeared_count}x  |  "
            f"Réapparu : {self.reappeared_count}x\n\n"
            f"Tag actuel : *{self.current_tag}*\n"
            f"Impact structure : {impact}\n"
            f"Côté : {side}\n"
            f"BTC : ${spot:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    def mark_reported(self) -> None:
        self.last_reported_at  = datetime.now(timezone.utc)
        self.last_reported_net = self.net_reinforcement


class WallLifecycleStore:
    """Registre de cycle de vie de tous les murs actifs."""

    def __init__(self) -> None:
        self._states: Dict[float, WallLifecycleState] = {}

    def on_appeared(self, strike: float, tag: str) -> None:
        now   = datetime.now(timezone.utc)
        state = self._states.get(strike)
        if state is None:
            self._states[strike] = WallLifecycleState(
                strike=strike, first_seen=now, last_seen=now, current_tag=tag,
            )
        else:
            state.reappeared_count += 1
            state.last_seen = now
            state.current_tag = tag

    def on_removed(self, strike: float, tag: str) -> None:
        now   = datetime.now(timezone.utc)
        state = self._states.get(strike)
        if state:
            state.disappeared_count += 1
            state.last_seen = now
            state.current_tag = tag

    def on_reinforced(self, strike: float, tag: str) -> None:
        now   = datetime.now(timezone.utc)
        state = self._states.get(strike)
        if state:
            state.reinforced_count += 1
            state.last_seen = now
            state.current_tag = tag

    def on_weakened(self, strike: float, tag: str) -> None:
        now   = datetime.now(timezone.utc)
        state = self._states.get(strike)
        if state:
            state.weakened_count += 1
            state.last_seen = now
            state.current_tag = tag

    def get(self, strike: float) -> Optional[WallLifecycleState]:
        return self._states.get(strike)

    def events_suppressed_count(self) -> int:
        """
        Estimation du nombre d'alertes individuelles supprimées par la fusion.
        = total_events sur tous les murs - 1 rapport par mur (si rapport envoyé).
        """
        total = 0
        for s in self._states.values():
            total += max(0, s.total_events - 1)
        return total

    def all_states(self) -> list:
        return list(self._states.values())

    def generate_audit_report(self) -> str:
        if not self._states:
            return "Aucun mur actif en mémoire."
        lines = ["🧱 **WALL LIFECYCLE AUDIT**\n"]
        for s in sorted(self._states.values(), key=lambda x: -x.total_events):
            net = s.net_reinforcement
            net_str = f"+{net}" if net > 0 else str(net)
            sig = "✅ structurel" if s.is_structurally_significant() else "⚠️ éphémère"
            lines.append(
                f"• ${s.strike:,.0f} [{s.current_tag}] {s.duration_hours:.1f}h "
                f"— ↑{s.reinforced_count} ↓{s.weakened_count} net={net_str} "
                f"disparu={s.disappeared_count}x — {sig}"
            )
        suppressed = self.events_suppressed_count()
        lines.append(f"\nÉvénements fusionnés (non envoyés) : **{suppressed}**")
        return "\n".join(lines)
