"""
Alertes Telegram — GEX flip, options wall, brief quotidien 8h UTC.

Architecture : buffer d'agrégation 15-30 min par topic → 1 synthèse trader.
Telegram = décision. Dashboard = information. Logs = détails techniques.
"""

import asyncio
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import httpx

_DATA_DIR = Path("/data")
STATS_LOG_PATH = (_DATA_DIR / "alert_log.jsonl") if _DATA_DIR.exists() else (Path(__file__).parent / "alert_log.jsonl")

from .deribit_client import DeribitClient
from .gex import compute_gex, GEXProfile, gex_summary
from .mopi import compute_mopi, mopi_summary
from .options_walls import compute_options_walls, OptionsWallsProfile
from .volatility_weather import compute_weather, weather_telegram_msg
from .dealer_pressure import compute_dealer_pressure, compute_dex_levels
from .gravity_map import compute_gravity_map, gravity_map_summary
from .squeeze_score import compute_squeeze_score, squeeze_summary, SqueezeScore
from .prediction_tracker import PredictionTracker
from .narrative_resolver import resolve_narrative, resolve_narrative_horizon, NarrativeResolved, HorizonNarrative, _gex_calibration_caveat, _apply_gex_confidence_wording
from .gex_activity_audit import compute_gex_activity_audit, GEXActivityAudit, compute_flip_activity_audit, FlipActivityAudit
from .gravity_activity_audit import compute_gravity_activity_audit
from .field_diagnostic import diag_gex_calibration
from .event_store import get_event_store
from .conviction_score import compute_conviction_score, MIN_SCORE_TO_SEND, format_simulation_report
from .wall_lifecycle import WallLifecycleStore

log = logging.getLogger(__name__)

COOLDOWN_TOPIC    = timedelta(minutes=30)  # 1 alerte max par topic / 30 min
MAX_DAILY_OPTIONS = 3                       # max alertes options / jour (hors critique)
NOISE_BAND_PCT    = 0.0025                  # ±0.25% autour d'un niveau = zone de bruit
CONFIRM_LOCKOUT   = timedelta(hours=2)      # critère 4 : pas d'alerte inverse dans les 2h
CONFIRM_HOLD_SECS = 15 * 60                 # critère 2 : maintien hors zone ≥ 15 min
CONFIRM_POLLS_MIN = 2                       # critère 3 : ≥ 2 polls consécutifs ≈ clôture 5m


# ─── Métriques 24h ───────────────────────────────────────────────────────────

@dataclass
class AlertMetrics:
    period_start:          datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events_received:       int = 0
    events_blocked_noise:  int = 0   # synthèse vide → bruit
    events_blocked_cooldown: int = 0
    events_blocked_budget:   int = 0
    events_blocked_distance: int = 0  # hard block distance >5% du spot
    events_blocked_conviction: int = 0  # conviction score insuffisant (distance ≤5%)
    syntheses_sent:        int = 0

    @property
    def total_blocked(self) -> int:
        return (
            self.events_blocked_noise + self.events_blocked_cooldown
            + self.events_blocked_budget + self.events_blocked_distance
            + self.events_blocked_conviction
        )

    @property
    def spam_reduction_rate(self) -> float:
        if self.events_received == 0:
            return 0.0
        return (self.events_received - self.syntheses_sent) / self.events_received


def _log_alert_entry(entry: dict) -> None:
    """Écrit une ligne JSONL dans alert_log.jsonl."""
    try:
        with open(STATS_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.warning(f"[metrics] impossible d'écrire dans {STATS_LOG_PATH}: {e}")


def log_feedback(alert_id: str, vote: str) -> None:
    """Enregistre un feedback utilisateur dans alert_log.jsonl (appelé via API)."""
    topic = None
    if STATS_LOG_PATH.exists():
        try:
            with open(STATS_LOG_PATH) as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("alert_id") == alert_id and entry.get("action") == "sent":
                            topic = entry.get("topic")
                            break
                    except Exception:
                        pass
        except Exception:
            pass
    _log_alert_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "feedback",
        "alert_id": alert_id,
        "vote": vote,
        "topic": topic,
    })
    log.info(f"[feedback] {alert_id} → {vote} (topic={topic})")


# ─── Événement bufférisé ─────────────────────────────────────────────────────

@dataclass
class LevelState:
    status:          str                = "IDLE"
    # "IDLE" | "TESTING" | "CONFIRMED"
    direction:       Optional[str]      = None   # "up" | "down"
    polls_beyond:    int                = 0      # polls consécutifs hors zone, même direction
    confirmed_at:    Optional[datetime] = None   # timestamp de la dernière cassure confirmée
    first_beyond_ts: Optional[datetime] = None   # 1er poll hors zone dans la direction courante
    last_alert_dir:  Optional[str]      = None   # direction de la dernière alerte envoyée
    last_alert_ts:   Optional[datetime] = None   # timestamp de la dernière alerte


@dataclass
class BufferedEvent:
    topic:      str
    timestamp:  datetime
    event_type: str       # wall_break_up | wall_break_down | wall_test_up | wall_test_down
                          # wall_appeared  | wall_removed    | wall_strength | wall_rejection
                          # gex_flip       | max_pain_moved
    price:      float     # BTC au moment de l'événement
    level:      float     # strike ou niveau concerné
    direction:  Optional[str]   = None   # "up" / "down"
    ratio:      Optional[float] = None   # variation % pour wall_strength
    metadata:   dict            = field(default_factory=dict)


# ─── Buffer d'agrégation ─────────────────────────────────────────────────────

class OptionsEventBuffer:
    BASE_WINDOW      = timedelta(minutes=15)
    EXTEND_WINDOW    = timedelta(minutes=30)
    EXTEND_THRESHOLD = 3   # > N events sur même topic → fenêtre étendue

    def __init__(self) -> None:
        self._events:   Dict[str, List[BufferedEvent]] = {}
        self._flush_at: Dict[str, datetime]            = {}
        self.total_ingested: int = 0   # compteur cumulatif brut

    def ingest(self, event: BufferedEvent) -> None:
        self.total_ingested += 1
        topic = event.topic
        now   = datetime.now(timezone.utc)
        if topic not in self._events:
            self._events[topic]   = []
            self._flush_at[topic] = now + self.BASE_WINDOW
        self._events[topic].append(event)
        if len(self._events[topic]) > self.EXTEND_THRESHOLD:
            first_ts              = self._events[topic][0].timestamp
            self._flush_at[topic] = first_ts + self.EXTEND_WINDOW

    def pop_ready(self) -> List[Tuple[str, List[BufferedEvent]]]:
        """Retourne et retire les topics dont la fenêtre est expirée."""
        now   = datetime.now(timezone.utc)
        ready = [
            (topic, events)
            for topic, events in list(self._events.items())
            if now >= self._flush_at[topic]
        ]
        for topic, _ in ready:
            del self._events[topic]
            del self._flush_at[topic]
        return ready

    def pending_count(self) -> int:
        return sum(len(v) for v in self._events.values())


# ─── Synthèse trader ─────────────────────────────────────────────────────────

def _synthesize(topic: str, events: List[BufferedEvent]) -> Optional[str]:
    """
    Génère UNE synthèse lisible pour un groupe d'événements sur un topic.
    Retourne None si la conclusion est 'bruit' (pas d'envoi Telegram).
    """
    if not events:
        return None

    level    = events[0].level
    spot_now = events[-1].price
    etypes   = [e.event_type for e in events]

    # ── GEX flip ──────────────────────────────────────────────────────────────
    if topic.startswith("gex_flip"):
        e = events[-1]
        regime_new = e.metadata.get("regime_new", "?")
        regime_old = e.metadata.get("regime_old", "?")
        gex_old    = e.metadata.get("gex_old", "?")
        gex_new    = e.metadata.get("gex_new", "?")
        _regime_label = {"STABILISANT": "Dealers Absorbent", "AMPLIFICATEUR": "Dealers Amplifient", "NEUTRE": "Neutres"}
        label_old = _regime_label.get(regime_old, regime_old)
        label_new = _regime_label.get(regime_new, regime_new)
        if regime_new == "AMPLIFICATEUR":
            return (
                f"⚡ *FLIP GEX — MARCHÉ AMPLIFIÉ*\n\n"
                f"Les market makers ont changé de camp : {label_old} → {label_new}.\n"
                f"Chaque move sera amplifié dans les deux sens. Gérer le risque.\n\n"
                f"GEX: {gex_old} → {gex_new}\n"
                f"BTC: ${spot_now:,.0f}\n\n"
                f"[📊 Dashboard](https://mamoscrypto.com/options)"
            )
        else:
            return (
                f"⚡ *FLIP GEX — MARCHÉ STABILISÉ*\n\n"
                f"Les market makers commencent à amortir les mouvements.\n"
                f"Régime : {label_old} → {label_new}. Volatilité en baisse probable.\n\n"
                f"GEX: {gex_old} → {gex_new}\n"
                f"BTC: ${spot_now:,.0f}\n\n"
                f"[📊 Dashboard](https://mamoscrypto.com/options)"
            )

    # ── Options wall ──────────────────────────────────────────────────────────
    side        = "résistance" if level > spot_now else "support"
    breaks_up   = etypes.count("wall_break_up")
    breaks_down = etypes.count("wall_break_down")
    tests_up    = etypes.count("wall_test_up")
    tests_down  = etypes.count("wall_test_down")
    appeared    = etypes.count("wall_appeared")
    removed     = etypes.count("wall_removed")
    rejections  = etypes.count("wall_rejection")
    reinforced  = sum(1 for e in events if e.event_type == "wall_strength" and (e.ratio or 0) > 0)
    weakened    = sum(1 for e in events if e.event_type == "wall_strength" and (e.ratio or 0) < 0)

    # ── Alertes cassure mur options — DÉSACTIVÉES EN ATTENTE D'AUDIT ─────────
    # Motif : logique directionnelle non validée (pas de vérification contexte
    # spot/gamma/OI/volume/rebond). Zéro alerte vaut mieux qu'une alerte fausse.
    # Réactiver uniquement après audit complet et validation multi-facteurs.
    if breaks_up > 0 or breaks_down > 0:
        log.debug(
            f"[wall_break] ${level:,.0f} {'↑' if breaks_up else '↓'} — "
            f"event logé, alerte Telegram SUSPENDUE (audit en cours)"
        )
        return None

    if rejections > 0 and reinforced > 0:
        return (
            f"🛡️ *Mur ${level:,.0f} défendu et renforcé*\n\n"
            f"BTC a testé ${level:,.0f} ({rejections} rejet(s)) et le mur s'est renforcé ({reinforced}x).\n"
            f"Forte conviction des market makers sur ce niveau.\n\n"
            f"*Conclusion :* La {side} à ${level:,.0f} tient. Ne pas trader contre.\n"
            f"BTC: ${spot_now:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    if rejections > 0:
        return (
            f"↩️ *Rejet sur mur ${level:,.0f}*\n\n"
            f"BTC a testé ${level:,.0f} ({rejections}x) sans franchir.\n"
            f"La barrière options tient pour l'instant.\n\n"
            f"*Conclusion :* ${level:,.0f} reste {side} valide.\n"
            f"BTC: ${spot_now:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    if reinforced >= 2:
        return (
            f"💪 *Mur ${level:,.0f} en renforcement*\n\n"
            f"Le mur options à ${level:,.0f} s'est renforcé {reinforced}x sur la fenêtre.\n"
            f"Les market makers accumulent du hedging sur ce niveau.\n\n"
            f"*Conclusion :* ${level:,.0f} est une {side} de plus en plus solide.\n"
            f"BTC: ${spot_now:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    if weakened >= 2:
        return (
            f"⚠️ *Mur ${level:,.0f} en affaiblissement*\n\n"
            f"Le mur options à ${level:,.0f} perd de sa force ({weakened}x affaibli).\n"
            f"La {side} pourrait céder si le prix approche.\n\n"
            f"*Conclusion :* Surveiller la tenue de ${level:,.0f} — niveau fragilisé.\n"
            f"BTC: ${spot_now:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    if appeared and not removed and len(events) > 1:
        return (
            f"🆕 *Nouveau mur confirmé — ${level:,.0f}*\n\n"
            f"Un niveau options significatif est apparu à ${level:,.0f} avec activité confirmée.\n"
            f"Ce niveau devient une {side} potentielle.\n\n"
            f"*Conclusion :* À surveiller. Aucune action avant confirmation.\n"
            f"BTC: ${spot_now:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    if removed and not appeared:
        return (
            f"💨 *Mur ${level:,.0f} disparu*\n\n"
            f"Le niveau options à ${level:,.0f} s'est évaporé.\n"
            f"La {side} à ce niveau n'est plus valide.\n\n"
            f"*Conclusion :* Zone dégagée. Nouveau niveau le plus proche à surveiller.\n"
            f"BTC: ${spot_now:,.0f}\n\n"
            f"[📊 Dashboard](https://mamoscrypto.com/options)"
        )

    # Tests seuls (statut TESTING) : surveillance dashboard uniquement, jamais Telegram
    if (tests_down > 0 or tests_up > 0) and breaks_up == 0 and breaks_down == 0:
        return None

    # Pas de conclusion exploitable → bruit → dashboard/logs uniquement
    return None


def _pcr_horizon_brief(mopi) -> str:
    """PCR par horizon pour le brief quotidien.
    Règle : pc_ratio_near = alertes court terme | pc_ratio_global = contexte | pc_ratio_institutional = structure longue.
    Ne jamais mélanger les trois horizons."""
    near = mopi.pc_ratio_near
    inst = mopi.pc_ratio_institutional

    def _lbl(v: float) -> str:
        if v > 1.5: return "peur élevée"
        if v > 1.2: return "pression bears"
        if v > 0.9: return "équilibré"
        if v > 0.7: return "confiance haussière"
        return "bulls dominants"

    near_lbl = _lbl(near)
    inst_lbl = _lbl(inst)

    near_bear, near_bull = near > 1.2, near < 0.9
    inst_bear, inst_bull = inst > 1.2, inst < 0.9
    if near_bear and inst_bull:
        reading = "Nervosité immédiate sans retournement structurel confirmé."
    elif near_bull and inst_bear:
        reading = "Optimisme court terme — institutions restent en mode protection."
    elif near_bear and inst_bear:
        reading = "Pression bears cohérente sur tous les horizons."
    elif near_bull and inst_bull:
        reading = "Pression bulls cohérente sur tous les horizons."
    else:
        reading = "Positionnement mixte — aucun signal directionnel clair."

    return (
        f"📊 **PCR par horizon**\n"
        f"• Court terme : {near_lbl} (P/C {near:.2f})\n"
        f"• Institutionnel : {inst_lbl} (P/C {inst:.2f})\n"
        f"↳ {reading}"
    )


def _horizon_brief_line(hn: HorizonNarrative) -> str:
    """Ligne courte pour le brief quotidien : '4h : DEX domine → biais HAUSSIER'."""
    fd = hn.force_dominante
    if fd == "AUCUNE":
        return f"{hn.horizon} : aucun signal exploitable"
    h_n = len(hn.forces_haussieres)
    b_n = len(hn.forces_baissieres)
    if hn.horizon in ("4h", "24h"):
        label = "biais" if hn.horizon == "4h" else "régime"
        direction = "HAUSSIER" if h_n > b_n else ("BAISSIER" if b_n > h_n else "NEUTRE")
        return f"{hn.horizon} : {fd} domine → {label} {direction}"
    # 72h : cible chiffrée quand possible
    if fd in ("MAX_PAIN", "GRAVITY", "WALLS") and hn.niveau_haut:
        return f"{hn.horizon} : {fd} → cible ${hn.niveau_haut:,.0f}"
    direction = "HAUSSIER" if h_n > b_n else ("BAISSIER" if b_n > h_n else "NEUTRE")
    return f"{hn.horizon} : {fd} domine → {direction}"


# ─── Alerteur Telegram ───────────────────────────────────────────────────────

class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self._base     = f"https://api.telegram.org/bot{bot_token}"

        self._buffer  = OptionsEventBuffer()
        self._metrics = AlertMetrics()
        self._tracker = PredictionTracker()

        # Anti-spam
        self._topic_last_sent:    Dict[str, datetime] = {}
        self._daily_options_sent: Dict[str, int]      = {}  # "YYYY-MM-DD" → count

        # Machine d'état par niveau options
        self._level_states: Dict[float, LevelState] = {}

        # Source de vérité narrative (mise à jour par AlertScheduler._poll_loop)
        self._last_narrative: Optional[NarrativeResolved] = None
        self._last_gex_audit: Optional[GEXActivityAudit] = None
        self._last_flip_audit: Optional[FlipActivityAudit] = None

        # Calibration GEX — statut transmis par AlertScheduler depuis _gex_calibration_cache
        self._cal_status: str = "available"
        self._cal_reason_code: str = "calibration_available"

        # Contexte signal pour event_store (mis à jour à chaque poll)
        self._signal_context: dict = {
            "gex_near": 0.0,
            "mopi_score": 50.0,
            "squeeze_score": 0.0,
            "nearest_wall": 0.0,
            "nearest_gravity_zone": 0.0,
        }

        # Wall lifecycle — état persistant par strike
        self._wall_lifecycle = WallLifecycleStore()

        # Compteur d'alertes supprimées par Conviction Score
        self._conviction_blocked: int = 0

    # ── Internes ─────────────────────────────────────────────────────────────

    def _today_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _is_topic_cooled(self, topic: str) -> bool:
        last = self._topic_last_sent.get(topic)
        if last is None:
            return True
        return datetime.now(timezone.utc) - last >= COOLDOWN_TOPIC

    def _daily_budget_ok(self, is_critical: bool = False) -> bool:
        if is_critical:
            return True
        today = self._today_key()
        return self._daily_options_sent.get(today, 0) < MAX_DAILY_OPTIONS

    def _record_sent(self, topic: str) -> None:
        self._topic_last_sent[topic] = datetime.now(timezone.utc)
        today = self._today_key()
        self._daily_options_sent[today] = self._daily_options_sent.get(today, 0) + 1

    def update_narrative(self, narrative: NarrativeResolved) -> None:
        """Reçoit la narrative du Narrative Resolver. Source de vérité globale."""
        self._last_narrative = narrative

    def update_gex_audit(self, audit: GEXActivityAudit) -> None:
        """Reçoit l'audit de qualité GEX. Gate les alertes GEX dormant."""
        self._last_gex_audit = audit

    def update_flip_audit(self, flip_audit: FlipActivityAudit) -> None:
        """Reçoit l'audit qualité du flip level. Gate les alertes flip dormant."""
        self._last_flip_audit = flip_audit

    def update_calibration(self, status: str, reason_code: str) -> None:
        """Reçoit le statut de calibration GEX. Alimente _narrative_footer()."""
        self._cal_status = status
        self._cal_reason_code = reason_code

    def update_signal_context(
        self,
        gex_near: float,
        mopi_score: float,
        squeeze_score: float,
        nearest_wall: float,
        nearest_gravity_zone: float,
    ) -> None:
        """Contexte snapshot pour enrichir les événements dans event_store."""
        self._signal_context.update({
            "gex_near": gex_near,
            "mopi_score": mopi_score,
            "squeeze_score": squeeze_score,
            "nearest_wall": nearest_wall,
            "nearest_gravity_zone": nearest_gravity_zone,
        })

    def _narrative_footer(self) -> str:
        """Ligne de contexte global à injecter dans les alertes Telegram."""
        if not self._last_narrative:
            return ""
        n = self._last_narrative
        parts = [f"_📌 {n.phrase_synthese}_"]
        if not n.gex_use_in_signal:
            parts.append(f"_{n.gex_activity_label} : {n.gex_activity_context}_")
        # Le wording est déjà adouci à la source dans phrase_synthese.
        # On ajoute uniquement un badge léger pour signaler le statut de calibration.
        if n.gex_use_in_signal and self._cal_status != "available":
            _BADGE = {
                "degraded":    "⚠️ _Calibration GEX dégradée_",
                "stale":       "⚠️ _Calibration GEX ancienne_",
                "unavailable": "⚠️ _Calibration GEX non validée_",
            }
            badge = _BADGE.get(self._cal_status, "")
            if badge:
                parts.append(badge)
        return "\n\n" + "\n".join(parts)

    # ── Conviction Score ──────────────────────────────────────────────────────

    def _compute_wall_conviction(self, events: List[BufferedEvent]):
        """Calcule le Conviction Score pour un groupe d'événements wall."""
        level     = events[0].level
        spot      = events[-1].price
        tag       = events[0].metadata.get("activity_tag", "DORMANT")
        dist_pct  = abs(spot - level) / level * 100 if level else 100.0

        n = self._last_narrative

        # DEX confirme si le signal DEX est exploitable (pas dormant)
        dex_confirms = bool(n and n.dex_use_in_signal)

        # GEX confirme si GEX actif ET non dormant
        gex_confirms = bool(
            n and n.gex_use_in_signal
            and self._last_gex_audit is not None
            and self._last_gex_audit.use_in_signal
        )

        # Gravity confirme si la zone gravity est proche du niveau (<3%)
        grav_zone     = self._signal_context.get("nearest_gravity_zone", 0.0)
        gravity_confirms = bool(
            grav_zone > 0 and level > 0
            and abs(grav_zone - level) / level < 0.03
        )

        # Contradiction majeure si la narrative signale des incohérences
        major_contradiction = bool(n and len(n.contradictions) > 0)

        return compute_conviction_score(
            activity_tag=tag,
            distance_pct=dist_pct,
            dex_confirms=dex_confirms,
            gex_confirms=gex_confirms,
            gravity_confirms=gravity_confirms,
            major_contradiction=major_contradiction,
        )

    def generate_conviction_audit(self) -> str:
        """Rapport d'audit Conviction Score : simulation + wall lifecycle."""
        lines = ["🎯 **AUDIT CONVICTION SCORE**\n"]

        # Résumé opérationnel
        lines.append(f"Alertes bloquées par Conviction Score (session) : **{self._conviction_blocked}**")
        lines.append(f"Seuil actuel : **{MIN_SCORE_TO_SEND}/10**\n")

        # Rapport Wall Lifecycle
        lines.append(self._wall_lifecycle.generate_audit_report())
        lines.append("")

        # Simulation de référence
        lines.append(format_simulation_report())

        return "\n".join(lines)

    # ── Envoi bas niveau ─────────────────────────────────────────────────────

    async def send(self, text: str, parse_mode: str = "Markdown", reply_markup: Optional[dict] = None) -> None:
        payload: dict = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self._base}/sendMessage",
                json=payload,
                timeout=10,
            )

    def _make_feedback_keyboard(self, alert_id: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "👍 Utile",     "callback_data": f"fb:{alert_id}:utile"},
                {"text": "👎 Inutile",   "callback_data": f"fb:{alert_id}:inutile"},
            ], [
                {"text": "🟡 Trop tôt",  "callback_data": f"fb:{alert_id}:trop_tot"},
                {"text": "🔴 Trop tard", "callback_data": f"fb:{alert_id}:trop_tard"},
            ]]
        }

    # ── Ingestion → buffer (pas d'envoi immédiat) ────────────────────────────

    def ingest_wall_events(
        self,
        walls: OptionsWallsProfile,
        prev_oi: Dict[float, float],
        prev_btc: float,
        near_walls: Set[float],
    ) -> None:
        """Parse les données de mur et empile des BufferedEvents. Aucun envoi Telegram.

        Règle : les walls DORMANT (sans activité) ne génèrent aucun événement Telegram
        mais produisent une observation silencieuse pour le suivi statistique.
        """
        spot         = walls.btc_price
        cur_oi       = {w.strike: w.total_oi for w in walls.walls}
        wall_tags    = {w.strike: w.tag for w in walls.walls}
        wall_by_strike = {w.strike: w for w in walls.walls}
        now          = datetime.now(timezone.utc)

        def _push(event_type: str, level: float, direction: Optional[str] = None, ratio: Optional[float] = None):
            topic    = f"options_wall_{level:.0f}"
            tag      = wall_tags.get(level, "DORMANT")
            wall_obj = wall_by_strike.get(level)
            self._buffer.ingest(BufferedEvent(
                topic=topic, timestamp=now,
                event_type=event_type, price=spot,
                level=level, direction=direction, ratio=ratio,
                metadata={
                    "activity_tag":     tag,
                    "ratio":            ratio,
                    "structural_score": wall_obj.structural_score if wall_obj else 0.0,
                    "active_score":     wall_obj.active_score     if wall_obj else 0.0,
                    "actionable_score": wall_obj.actionable_score if wall_obj else 0.0,
                    "side":             wall_obj.side             if wall_obj else "UNKNOWN",
                },
            ))
            # Mise à jour lifecycle par type d'événement
            if event_type == "wall_appeared":
                self._wall_lifecycle.on_appeared(level, tag)
            elif event_type == "wall_removed":
                self._wall_lifecycle.on_removed(level, tag)
            elif event_type == "wall_strength" and ratio is not None:
                if ratio > 0:
                    self._wall_lifecycle.on_reinforced(level, tag)
                else:
                    self._wall_lifecycle.on_weakened(level, tag)
            log.debug(f"[buffer] +{event_type} @ ${level:,.0f} (BTC ${spot:,.0f}) [{tag}]")

        # Observations silencieuses pour les walls DORMANT (edge statistique)
        for wall_obj in walls.walls:
            if wall_obj.tag == "DORMANT":
                dist_pct = abs(spot - wall_obj.strike) / spot * 100 if spot else 0.0
                get_event_store().log_silent_event(
                    event_type="wall_dormant_blocked",
                    spot=spot,
                    direction="DOWN" if wall_obj.strike > spot else "UP",
                    indicators={"family": "level"},
                    metadata={
                        "strike":           wall_obj.strike,
                        "side":             wall_obj.side,
                        "distance_pct":     round(dist_pct, 2),
                        "activity_tag":     "DORMANT",
                        "structural_score": wall_obj.structural_score,
                        "active_score":     wall_obj.active_score,
                        "actionable_score": wall_obj.actionable_score,
                        "conviction_score": 0,
                        "sent":             False,
                        "blocked_reason":   "activity_tag_dormant",
                    },
                    dedup_key=f"wall_dormant_blocked_{wall_obj.strike:.0f}",
                )

        for strike in cur_oi:
            if wall_tags.get(strike) == "DORMANT":
                continue
            if strike not in prev_oi:
                _push("wall_appeared", strike)

        for strike in prev_oi:
            if strike not in cur_oi:
                _push("wall_removed", strike)

        for strike in cur_oi:
            if wall_tags.get(strike) == "DORMANT":
                continue
            if strike in prev_oi and prev_oi[strike] > 0:
                ratio = (cur_oi[strike] - prev_oi[strike]) / prev_oi[strike]
                if abs(ratio) >= 0.30:
                    _push("wall_strength", strike, ratio=ratio)

        # Machine d'état par niveau — confirmation à 2 critères parmi 4 :
        #   1. distance > 0.25% hors zone       (toujours vrai hors zone de bruit)
        #   2. maintien hors zone ≥ 15 min      (first_beyond_ts)
        #   3. ≥ 2 polls consécutifs hors zone  (clôture 5m confirmée)
        #   4. pas d'alerte inverse dans 2h     (last_alert_dir / last_alert_ts)
        # Si 1 seul critère → TESTING, pas de Telegram.
        # Règle DORMANT : wall sans activité → aucun événement, aucune alerte.
        all_strikes = set(cur_oi) | set(prev_oi)
        for strike in all_strikes:
            if not strike:
                continue
            if wall_tags.get(strike) == "DORMANT":
                continue
            distance_pct = abs(spot - strike) / strike
            in_noise     = distance_pct < NOISE_BAND_PCT
            cur_dir      = "up" if spot > strike else "down"
            ls           = self._level_states.get(strike, LevelState())

            if in_noise:
                # Dans la zone de bruit : toujours TESTING, jamais de cassure
                self._level_states[strike] = LevelState(
                    status="TESTING", direction=cur_dir,
                    polls_beyond=0, confirmed_at=ls.confirmed_at,
                    first_beyond_ts=None,
                    last_alert_dir=ls.last_alert_dir, last_alert_ts=ls.last_alert_ts,
                )
                _push("wall_test_up" if cur_dir == "up" else "wall_test_down",
                      strike, direction=cur_dir)
                continue

            # Hors zone de bruit — calcul des 4 critères
            same_dir        = (ls.direction == cur_dir)
            polls_beyond    = (ls.polls_beyond + 1) if same_dir else 1
            first_beyond_ts = (ls.first_beyond_ts if same_dir else None) or now

            # Critère 1 : distance > 0.25% — toujours satisfait ici
            crit1 = True

            # Critère 2 : maintien hors zone ≥ 15 min
            crit2 = (now - first_beyond_ts).total_seconds() >= CONFIRM_HOLD_SECS

            # Critère 3 : ≥ 2 polls consécutifs hors zone (≈ clôture 5m confirmée)
            crit3 = polls_beyond >= CONFIRM_POLLS_MIN

            # Critère 4 : pas d'alerte inverse sur ce niveau dans les 2 dernières heures
            inverse_dir = "down" if cur_dir == "up" else "up"
            crit4 = not (
                ls.last_alert_dir == inverse_dir
                and ls.last_alert_ts is not None
                and (now - ls.last_alert_ts) < CONFIRM_LOCKOUT
            )

            criteria_met = sum([crit1, crit2, crit3, crit4])

            if ls.status == "CONFIRMED" and same_dir:
                # Maintien dans la même direction confirmée, pas de nouvel event
                self._level_states[strike] = LevelState(
                    status="CONFIRMED", direction=cur_dir,
                    polls_beyond=polls_beyond, confirmed_at=ls.confirmed_at,
                    first_beyond_ts=first_beyond_ts,
                    last_alert_dir=ls.last_alert_dir, last_alert_ts=ls.last_alert_ts,
                )

            elif criteria_met >= 2:
                # Cassure confirmée — 2+ critères satisfaits
                self._level_states[strike] = LevelState(
                    status="CONFIRMED", direction=cur_dir,
                    polls_beyond=polls_beyond, confirmed_at=now,
                    first_beyond_ts=first_beyond_ts,
                    last_alert_dir=cur_dir, last_alert_ts=now,
                )
                _push("wall_break_up" if cur_dir == "up" else "wall_break_down",
                      strike, direction=cur_dir)
                log.debug(
                    f"[cassure] ${strike:,.0f} {cur_dir} CONFIRMED "
                    f"(critères: dist={crit1} hold={crit2} polls={crit3} no-inv={crit4})"
                )

            else:
                # TESTING — 1 seul critère, pas d'alerte Telegram
                ev_type = "wall_rejection" if ls.status == "CONFIRMED" else (
                    "wall_test_up" if cur_dir == "up" else "wall_test_down"
                )
                self._level_states[strike] = LevelState(
                    status="TESTING", direction=cur_dir,
                    polls_beyond=polls_beyond, confirmed_at=ls.confirmed_at,
                    first_beyond_ts=first_beyond_ts,
                    last_alert_dir=ls.last_alert_dir, last_alert_ts=ls.last_alert_ts,
                )
                _push(ev_type, strike, direction=cur_dir)
                log.debug(
                    f"[cassure] ${strike:,.0f} {cur_dir} TESTING "
                    f"(critères: dist={crit1} hold={crit2} polls={crit3} no-inv={crit4})"
                )

        for strike in near_walls:
            if strike not in cur_oi:
                continue
            if wall_tags.get(strike) == "DORMANT":
                continue
            if abs(spot - strike) / strike > abs(prev_btc - strike) / strike + 0.007:
                _push("wall_rejection", strike)

    def ingest_gex_flip(self, old: GEXProfile, new: GEXProfile) -> None:
        """Buffèrise un flip GEX. Filtre zone neutre, non-flip, et GEX dormant."""
        if old.regime == "NEUTRE" or new.regime == "NEUTRE":
            return
        if old.regime == new.regime:
            return
        # Gate qualité GEX global : bloquer si GEX majoritairement dormant
        if self._last_gex_audit is not None and not self._last_gex_audit.use_in_signal:
            log.info(
                f"[gex_flip] bloqué — GEX {self._last_gex_audit.overall_profile} "
                f"(score={self._last_gex_audit.signal_quality_score}/10, "
                f"dormant={self._last_gex_audit.dormant.gex_pct:.0f}%) "
                f"flip {old.regime}→{new.regime} ignoré"
            )
            _log_alert_entry({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "blocked_gex_quality",
                "topic": f"gex_flip_{new.regime}",
                "gex_profile": self._last_gex_audit.overall_profile,
                "signal_quality_score": self._last_gex_audit.signal_quality_score,
                "dormant_pct": self._last_gex_audit.dormant.gex_pct,
                "regime_old": old.regime,
                "regime_new": new.regime,
            })
            return

        # Gate qualité flip level : bloquer si le niveau lui-même est dormant/structurel
        if self._last_flip_audit is not None and not self._last_flip_audit.flip_use_in_signal:
            log.info(
                f"[gex_flip] bloqué — flip level {self._last_flip_audit.flip_activity_tag} "
                f"(score={self._last_flip_audit.flip_signal_quality}/10, "
                f"dormant={self._last_flip_audit.window_dormant_pct:.0f}%) "
                f"flip {old.regime}→{new.regime} ignoré"
            )
            _log_alert_entry({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "blocked_flip_quality",
                "topic": f"gex_flip_{new.regime}",
                "flip_tag": self._last_flip_audit.flip_activity_tag,
                "flip_signal_quality": self._last_flip_audit.flip_signal_quality,
                "flip_dormant_pct": self._last_flip_audit.window_dormant_pct,
                "regime_old": old.regime,
                "regime_new": new.regime,
            })
            return

        topic = f"gex_flip_{new.regime}"
        self._buffer.ingest(BufferedEvent(
            topic=topic,
            timestamp=datetime.now(timezone.utc),
            event_type="gex_flip",
            price=new.btc_price,
            level=new.btc_price,
            metadata={
                "regime_old": old.regime,
                "regime_new": new.regime,
                "gex_old":    f"${old.total_gex / 1e6:+.1f}M",
                "gex_new":    f"${new.total_gex / 1e6:+.1f}M",
            },
        ))
        log.debug(f"[buffer] gex_flip {old.regime} → {new.regime}")

    # ── Flush ─────────────────────────────────────────────────────────────────

    async def flush_ready(self) -> int:
        """
        Traite les topics dont la fenêtre est expirée :
          synthèse → filtre bruit → filtre cooldown/budget → envoi Telegram.
        Retourne le nombre d'alertes envoyées.
        """
        ready = self._buffer.pop_ready()
        sent  = 0
        now   = datetime.now(timezone.utc).isoformat()

        for topic, events in ready:
            n = len(events)
            self._metrics.events_received += n

            msg = _synthesize(topic, events)
            if msg is None:
                self._metrics.events_blocked_noise += n
                log.debug(f"[flush] {topic}: bruit ({n} events) → dashboard/logs uniquement")
                _log_alert_entry({"ts": now, "action": "blocked_noise",
                                  "topic": topic, "event_count": n})
                continue

            # Adoucir le wording GEX à la source si calibration dégradée
            if self._cal_status != "available":
                msg = _apply_gex_confidence_wording(msg, self._cal_status)

            # ── Conviction Score — gate avant tout envoi ──────────────────────
            if topic.startswith("options_wall_"):
                conviction = self._compute_wall_conviction(events)
                if not conviction.send:
                    self._conviction_blocked += n
                    dist_pct = conviction.breakdown["distance"]["pct"]
                    is_distance_block = dist_pct > 5.0
                    blocked_action = "blocked_distance_gt_5pct" if is_distance_block else "blocked_conviction"
                    if is_distance_block:
                        self._metrics.events_blocked_distance += n
                    else:
                        self._metrics.events_blocked_conviction += n
                    log.info(
                        f"[conviction] {topic} bloqué score={conviction.score}/10 — {conviction.reason}"
                    )
                    _log_alert_entry({
                        "ts": now, "action": blocked_action,
                        "topic": topic, "event_count": n,
                        "conviction_score": conviction.score,
                        "distance_pct": dist_pct,
                        "reason": conviction.reason,
                    })
                    last_ev_blk = events[-1]
                    # wall_distance_blocked : observation dédiée avec full metadata
                    if is_distance_block:
                        meta0    = events[0].metadata
                        level_db = last_ev_blk.level
                        get_event_store().log_silent_event(
                            event_type="wall_distance_blocked",
                            spot=last_ev_blk.price,
                            direction=last_ev_blk.direction,
                            indicators={"family": "level"},
                            metadata={
                                "strike":           level_db,
                                "side":             meta0.get("side", "UNKNOWN"),
                                "distance_pct":     round(dist_pct, 2),
                                "activity_tag":     meta0.get("activity_tag", "UNKNOWN"),
                                "structural_score": meta0.get("structural_score", 0.0),
                                "active_score":     meta0.get("active_score", 0.0),
                                "actionable_score": meta0.get("actionable_score", 0.0),
                                "conviction_score": conviction.score,
                                "sent":             False,
                                "blocked_reason":   "distance_gt_5pct",
                            },
                            dedup_key=f"wall_distance_blocked_{level_db:.0f}",
                        )
                    self._log_event_to_store(
                        events, last_ev_blk,
                        sent=False,
                        blocked_reason=f"conviction:{conviction.score:.1f}",
                    )
                    continue
                log.info(
                    f"[conviction] {topic} PASSE score={conviction.score}/10 — {conviction.reason}"
                )

            is_critical = any(
                e.event_type in ("wall_break_up", "wall_break_down", "gex_flip")
                for e in events
            )
            if not self._is_topic_cooled(topic):
                self._metrics.events_blocked_cooldown += n
                log.debug(f"[flush] {topic}: cooldown actif, ignoré")
                _log_alert_entry({"ts": now, "action": "blocked_cooldown",
                                  "topic": topic, "event_count": n,
                                  "msg_preview": msg[:80]})
                last_ev_blk = events[-1]
                self._log_event_to_store(
                    events, last_ev_blk, sent=False, blocked_reason="cooldown"
                )
                continue

            if not self._daily_budget_ok(is_critical):
                self._metrics.events_blocked_budget += n
                log.info(f"[flush] budget quotidien atteint ({MAX_DAILY_OPTIONS}/j), {topic} ignoré")
                _log_alert_entry({"ts": now, "action": "blocked_budget",
                                  "topic": topic, "event_count": n,
                                  "msg_preview": msg[:80]})
                last_ev_blk = events[-1]
                self._log_event_to_store(
                    events, last_ev_blk, sent=False, blocked_reason="budget"
                )
                continue

            alert_id = uuid.uuid4().hex[:8]
            msg += self._narrative_footer()
            await self.send(msg, reply_markup=self._make_feedback_keyboard(alert_id))
            self._record_sent(topic)
            sent += 1
            self._metrics.syntheses_sent += 1
            log.info(f"[flush] alerte envoyée topic={topic} events={n} critical={is_critical} alert_id={alert_id}")
            _log_alert_entry({"ts": now, "action": "sent",
                              "alert_id": alert_id,
                              "topic": topic, "event_count": n,
                              "critical": is_critical, "msg_preview": msg[:120]})

            # Enregistre pour validation post-événement (1h/4h/24h)
            last_ev = events[-1]
            self._tracker.register(
                alert_id=alert_id,
                topic=topic,
                event_types=[e.event_type for e in events],
                btc_at_alert=last_ev.price,
                level=last_ev.level,
                direction=last_ev.direction,
                metadata=last_ev.metadata,
            )

            # Log dans event_store pour backtest / accuracy par event type
            self._log_event_to_store(events, last_ev)

        return sent

    def _log_event_to_store(
        self, events, last_ev,
        sent: bool = True,
        blocked_reason: str | None = None,
    ) -> None:
        """Mappe les événements du buffer vers la taxonomie event_store et les logge."""
        etypes = [e.event_type for e in events]
        ctx    = self._signal_context

        if any(et == "wall_break_up" for et in etypes):
            event_type = "wall_breakout"
            direction  = "UP"
            strength   = 80.0
        elif any(et == "wall_break_down" for et in etypes):
            event_type = "wall_breakout"
            direction  = "DOWN"
            strength   = 80.0
        elif any(et == "wall_rejection" for et in etypes):
            event_type = "wall_rejection"
            direction  = None
            strength   = 60.0
        elif any(et == "gex_flip" for et in etypes):
            # Correction bug: topic était référencé depuis le scope appelant (NameError en prod)
            e0 = events[-1]
            regime_new = e0.metadata.get("regime_new", "NEUTRE")
            etype_silent = "gex_amplifier" if regime_new == "AMPLIFICATEUR" else "gex_stabilizer"
            try:
                get_event_store().log_silent_event(
                    event_type=etype_silent,
                    spot=last_ev.price,
                    direction=None,
                    indicators={"family": "regime_shift", "source": "gex_flip_alert"},
                    metadata={
                        "regime_old": e0.metadata.get("regime_old"),
                        "regime_new": regime_new,
                        "gex_old": e0.metadata.get("gex_old"),
                        "gex_new": e0.metadata.get("gex_new"),
                        "alerted": True,
                    },
                )
            except Exception as e2:
                log.warning(f"[event_store] log_silent gex_flip: {e2}")
            return
        else:
            return  # bruit non mappé

        # Observations silencieuses pour walls bloqués (candidates)
        if not sent and event_type in ("wall_breakout", "wall_rejection"):
            candidate_etype = (
                "wall_breakout_candidate" if event_type == "wall_breakout"
                else "wall_rejection_candidate"
            )
            # Conviction score depuis le blocked_reason "conviction:X.X"
            conviction_val = 0.0
            if blocked_reason and blocked_reason.startswith("conviction:"):
                try:
                    conviction_val = float(blocked_reason.split(":")[1])
                except (IndexError, ValueError):
                    pass

            level_c  = last_ev.level
            dist_c   = abs(last_ev.price - level_c) / level_c * 100 if level_c else 0.0
            meta0_c  = events[0].metadata
            try:
                get_event_store().log_silent_event(
                    event_type=candidate_etype,
                    spot=last_ev.price,
                    direction=direction,
                    indicators={"family": "level"},
                    metadata={
                        "strike":           level_c,
                        "side":             meta0_c.get("side", "UNKNOWN"),
                        "distance_pct":     round(dist_c, 2),
                        "activity_tag":     meta0_c.get("activity_tag", "UNKNOWN"),
                        "structural_score": meta0_c.get("structural_score", 0.0),
                        "active_score":     meta0_c.get("active_score", 0.0),
                        "actionable_score": meta0_c.get("actionable_score", 0.0),
                        "conviction_score": conviction_val,
                        "sent":             False,
                        "blocked_reason":   blocked_reason,
                    },
                    dedup_key=f"{candidate_etype}_{level_c:.0f}",
                )
            except Exception as e_c:
                log.warning(f"[event_store] log_silent wall_candidate: {e_c}")

        try:
            get_event_store().log_event(
                event_type=event_type,
                spot=last_ev.price,
                signal_strength=strength,
                quality_state="ACTIONABLE",
                gex_near=ctx["gex_near"],
                mopi_score=ctx["mopi_score"],
                squeeze_score=ctx["squeeze_score"],
                nearest_wall=ctx["nearest_wall"],
                nearest_gravity_zone=ctx["nearest_gravity_zone"],
                direction=direction,
                sent=sent,
                blocked_reason=blocked_reason,
            )
        except Exception as e:
            log.warning(f"[event_store] log_event wall error: {e}")

    # ── Rapport signal/bruit ─────────────────────────────────────────────────

    def generate_report(self) -> str:
        """Génère le rapport 24h signal/bruit à envoyer sur Telegram."""
        m   = self._metrics
        dur = (datetime.now(timezone.utc) - m.period_start).total_seconds() / 3600

        lines: list = []
        if STATS_LOG_PATH.exists():
            with open(STATS_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            lines.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

        sent_entries    = [l for l in lines if l.get("action") == "sent"]
        blocked_entries = [l for l in lines if l.get("action", "").startswith("blocked")]

        spam_pct = m.spam_reduction_rate * 100
        old_sys  = m.events_received
        new_sys  = m.syntheses_sent

        report = (
            f"📊 **RAPPORT BUFFER — {dur:.1f}h de prod réelle**\n\n"
            f"**Compteurs**\n"
            f"• Events reçus : {m.events_received}\n"
            f"• Events bloqués : {m.total_blocked}\n"
            f"  ↳ Bruit (pas de conclusion) : {m.events_blocked_noise}\n"
            f"  ↳ Distance >5% (hors portée) : {m.events_blocked_distance}\n"
            f"  ↳ Conviction score insuff. : {m.events_blocked_conviction}\n"
            f"  ↳ Cooldown actif : {m.events_blocked_cooldown}\n"
            f"  ↳ Budget quotidien : {m.events_blocked_budget}\n"
            f"• Synthèses envoyées : {m.syntheses_sent}\n\n"
            f"**Signal / Bruit**\n"
            f"• Ancien système (sans buffer) : **{old_sys} alertes**\n"
            f"• Nouveau système (buffer) : **{new_sys} synthèses**\n"
            f"• Réduction spam : **{spam_pct:.1f}%**\n\n"
        )

        if sent_entries:
            report += "**Bonnes synthèses (dernières)**\n"
            for ex in sent_entries[-3:]:
                preview = ex.get("msg_preview", "")[:70].replace("\n", " ")
                report += f"✅ `{ex['topic']}` ({ex.get('event_count',1)} events)\n_{preview}_\n"
            report += "\n"

        if blocked_entries:
            report += "**Encore à améliorer (derniers bloqués)**\n"
            for ex in blocked_entries[-3:]:
                preview = ex.get("msg_preview", "")[:60].replace("\n", " ")
                reason  = ex["action"].replace("blocked_", "")
                tag     = f"_{preview}_" if preview else ""
                report += f"🚫 `{ex['topic']}` → {reason} {tag}\n"

        # ── Feedback qualité 7 derniers jours ─────────────────────────────
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        feedback_7d = []
        for entry in lines:
            if entry.get("action") == "feedback":
                try:
                    ts = datetime.fromisoformat(entry["ts"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff_7d:
                        feedback_7d.append(entry)
                except Exception:
                    pass

        report += "\n**📣 Feedback Qualité (7j)**\n"
        if feedback_7d:
            vote_map = [("utile","👍"), ("inutile","👎"), ("trop_tot","🟡"), ("trop_tard","🔴")]
            vote_counts = Counter(e.get("vote") for e in feedback_7d)
            total_fb = len(feedback_7d)
            report += f"• Alertes notées : {total_fb}\n"
            for vote, emoji in vote_map:
                n = vote_counts.get(vote, 0)
                pct = n / total_fb * 100 if total_fb else 0
                report += f"• {emoji} : {n} ({pct:.0f}%)\n"

            utile_topics  = [e.get("topic") for e in feedback_7d if e.get("vote") == "utile" and e.get("topic")]
            reduce_topics = [e.get("topic") for e in feedback_7d if e.get("vote") in ("inutile", "trop_tard") and e.get("topic")]
            if utile_topics:
                tops = ", ".join(f"`{t}` ({n}x)" for t, n in Counter(utile_topics).most_common(3))
                report += f"• Garder : {tops}\n"
            if reduce_topics:
                tops = ", ".join(f"`{t}` ({n}x)" for t, n in Counter(reduce_topics).most_common(3))
                report += f"• Réduire : {tops}\n"
        else:
            report += "• Aucun feedback reçu — tape 👍👎🟡🔴 sur les alertes.\n"

        return report

    async def send_report(self) -> None:
        """Envoie le rapport signal/bruit sur Telegram et remet les compteurs à zéro."""
        report = self.generate_report()
        await self.send(report)
        self._metrics = AlertMetrics()
        log.info("[metrics] rapport envoyé, compteurs réinitialisés")

    # ── Rapport qualité 7 jours ───────────────────────────────────────────────

    def _suggest_rule_adjustments(
        self,
        utile_topics: Counter,
        reduce_topics: Counter,
        trop_tot_topics: Counter,
        vote_counts: Counter,
        total_fb: int,
    ) -> List[str]:
        if total_fb < 5:
            return ["Trop peu de feedback (< 5 votes). Continue à noter les alertes pour des suggestions précises."]

        suggestions: List[str] = []
        trop_tard_n = vote_counts.get("trop_tard", 0)
        trop_tot_n  = vote_counts.get("trop_tot", 0)
        inutile_n   = vote_counts.get("inutile", 0)
        utile_n     = vote_counts.get("utile", 0)

        if trop_tard_n > total_fb * 0.30:
            suggestions.append(
                f"🔴 {trop_tard_n/total_fb*100:.0f}% 'trop tard' → "
                f"Réduire COOLDOWN_TOPIC de 30min → 15min"
            )
        if trop_tot_n > total_fb * 0.30:
            suggestions.append(
                f"🟡 {trop_tot_n/total_fb*100:.0f}% 'trop tôt' → "
                f"Augmenter BASE_WINDOW buffer de 15min → 25min"
            )
        for topic, n in reduce_topics.most_common(3):
            if n < 2:
                continue
            if "wall" in topic:
                suggestions.append(
                    f"📉 `{topic}` inutile {n}x → Filtrer ou augmenter seuil OI minimum"
                )
            elif "gex" in topic:
                suggestions.append(
                    f"📉 `{topic}` inutile {n}x → Vérifier seuil magnitude GEX flip ($5M)"
                )
        for topic, n in utile_topics.most_common(3):
            if n >= 3 and "gex_flip" in topic:
                suggestions.append(
                    f"✅ `{topic}` utile {n}x → Priorité critique, ne jamais bloquer via budget"
                )
        if utile_n > total_fb * 0.70:
            suggestions.append(
                f"✅ {utile_n/total_fb*100:.0f}% utiles — qualité bonne. "
                f"Peut augmenter MAX_DAILY_OPTIONS : {MAX_DAILY_OPTIONS} → {MAX_DAILY_OPTIONS + 1}"
            )
        elif utile_n < total_fb * 0.30 and total_fb >= 5:
            suggestions.append(
                f"⚠️ Seulement {utile_n/total_fb*100:.0f}% utiles → "
                f"Réduire MAX_DAILY_OPTIONS : {MAX_DAILY_OPTIONS} → {max(1, MAX_DAILY_OPTIONS - 1)}"
            )

        return suggestions or ["Signal insuffisant — continue à noter les alertes."]

    def generate_weekly_report(self) -> str:
        """Rapport qualité 7 jours : envoyées, feedback, tops, règles à ajuster."""
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        all_lines: List[dict] = []
        if STATS_LOG_PATH.exists():
            with open(STATS_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        def within_7d(entry: dict) -> bool:
            try:
                ts = datetime.fromisoformat(entry["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts >= cutoff_7d
            except Exception:
                return False

        recent      = [l for l in all_lines if within_7d(l)]
        sent_7d     = [l for l in recent if l.get("action") == "sent"]
        feedback_7d = [l for l in recent if l.get("action") == "feedback"]
        blocked_7d  = [l for l in recent if l.get("action", "").startswith("blocked")]

        total_sent = len(sent_7d)
        total_fb   = len(feedback_7d)
        vote_counts = Counter(e.get("vote") for e in feedback_7d)

        utile_n     = vote_counts.get("utile", 0)
        inutile_n   = vote_counts.get("inutile", 0)
        trop_tot_n  = vote_counts.get("trop_tot", 0)
        trop_tard_n = vote_counts.get("trop_tard", 0)

        fb_rate    = total_fb / total_sent * 100 if total_sent else 0
        utile_rate = utile_n / total_fb * 100 if total_fb else 0

        utile_topics  = Counter(e.get("topic") for e in feedback_7d if e.get("vote") == "utile"   and e.get("topic"))
        reduce_topics = Counter(e.get("topic") for e in feedback_7d if e.get("vote") in ("inutile","trop_tard") and e.get("topic"))
        trop_tot_topics = Counter(e.get("topic") for e in feedback_7d if e.get("vote") == "trop_tot" and e.get("topic"))

        report = "📈 **RAPPORT HEBDO QUALITÉ ALERTES — 7j**\n\n"

        report += "**Résumé**\n"
        report += f"• Alertes envoyées : {total_sent}\n"
        report += f"• Alertes bloquées : {len(blocked_7d)}\n"
        report += f"• Feedbacks reçus : {total_fb} ({fb_rate:.0f}% taux notation)\n\n"

        report += "**Taux Utile / Inutile**\n"
        report += f"• 👍 Utile : {utile_n}"
        report += f" ({utile_rate:.0f}%)\n" if total_fb else "\n"
        for vote, emoji in [("inutile","👎"),("trop_tot","🟡"),("trop_tard","🔴")]:
            n = vote_counts.get(vote, 0)
            pct = n / total_fb * 100 if total_fb else 0
            report += f"• {emoji} {vote.replace('_',' ').capitalize()} : {n} ({pct:.0f}%)\n"
        report += "\n"

        if utile_topics:
            report += "**✅ Types les plus utiles**\n"
            for topic, n in utile_topics.most_common(5):
                report += f"• `{topic}` : {n}x 👍\n"
            report += "\n"

        if reduce_topics:
            report += "**🚫 Types à réduire**\n"
            for topic, n in reduce_topics.most_common(5):
                report += f"• `{topic}` : {n}x 👎/🔴\n"
            report += "\n"

        report += "**⚙️ Règles à ajuster**\n"
        rules = self._suggest_rule_adjustments(
            utile_topics, reduce_topics, trop_tot_topics, vote_counts, total_fb
        )
        for rule in rules:
            report += f"• {rule}\n"

        # ── Prediction Accuracy ────────────────────────────────────────────
        report += "\n---\n"
        report += self._tracker.generate_accuracy_report(days=7)

        return report

    async def send_weekly_report(self) -> None:
        report = self.generate_weekly_report()
        await self.send(report)
        log.info("[weekly_report] rapport 7j envoyé")

    # ── Brief quotidien (synthèse déjà faite, envoi direct) ──────────────────

    async def send_daily_brief(
        self,
        gex: GEXProfile,
        mopi_score,
        weather,
        squeeze: Optional[SqueezeScore] = None,
        gravity_narrative: Optional[str] = None,
        vip_only_content: Optional[str] = None,
        narrative: Optional[NarrativeResolved] = None,
        horizon_narratives: Optional[Dict[str, HorizonNarrative]] = None,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

        # Narrative Resolver — source de vérité globale en ouverture du brief
        if narrative:
            narrative_block = (
                f"🧭 **{narrative.phrase_synthese}**\n\n"
                f"**Scénario :** {narrative.scenario_principal}\n"
                f"**Risque :** {narrative.risque_principal}\n"
                f"**Zone haute :** {narrative.niveau_haut_label}\n"
                f"**Zone basse :** {narrative.niveau_bas_label}\n\n"
                f"---\n"
            )
        else:
            narrative_block = ""

        # Lectures horizon 4h / 24h / 72h
        if horizon_narratives:
            lines = [
                _horizon_brief_line(hn)
                for h in ("4h", "24h", "72h")
                if (hn := horizon_narratives.get(h)) is not None
            ]
            horizon_block = (
                "📐 **Lectures Horizon**\n"
                + "\n".join(lines)
                + "\n⚠️ _Hypothèse V1 — non backtestée_\n\n---\n"
            ) if lines else ""
        else:
            horizon_block = ""

        msg = (
            f"📊 **BRIEF OPTIONS BTC — {now}**\n\n"
            f"{narrative_block}"
            f"{horizon_block}"
            f"{weather_telegram_msg(weather)}\n\n"
            f"---\n{gex_summary(gex)}\n\n"
            f"---\n{mopi_summary(mopi_score)}\n\n"
        )
        msg += f"---\n{_pcr_horizon_brief(mopi_score)}\n\n"
        if squeeze:
            msg += f"---\n{squeeze_summary(squeeze)}\n\n"
        if gravity_narrative:
            msg += f"---\n🗺️ **Gravity Map**\n{gravity_narrative}\n\n"
        if vip_only_content:
            msg += f"---\n🔐 **Analyse VIP** (membres uniquement)\n{vip_only_content}\n\n"
        msg += "[📊 Dashboard complet](https://mamoscrypto.com/options)"
        await self.send(msg)
        log.info("Brief quotidien envoyé")

    async def send_squeeze_alert(self, sq: SqueezeScore, btc_price: float) -> None:
        """Squeeze critique : synthèse déjà faite, envoi direct avec cooldown."""
        if sq.score < 80:
            return
        topic = "squeeze_critique"
        if not self._is_topic_cooled(topic):
            return
        bias_text = {"UP": "⬆️ HAUSSIER", "DOWN": "⬇️ BAISSIER", "NEUTRAL": "➡️ NEUTRE"}.get(sq.direction_bias, "")
        msg = (
            f"🚨 **SQUEEZE CRITIQUE — {sq.score:.0f}/100**\n"
            f"BTC: ${btc_price:,.0f}\n\n"
            f"Direction: {bias_text}\n"
            f"Zone déclencheur: ${sq.trigger_zone:,.0f}\n\n"
            f"Signal dominant: {sq.dominant_signal}\n\n"
            f"{''.join(f'• {s.name}: {s.description}' + chr(10) for s in sorted(sq.signals, key=lambda x: -x.score)[:3])}"
            f"{self._narrative_footer()}\n"
            f"\n[📊 Dashboard](https://mamoscrypto.com/options)"
        )
        alert_id = uuid.uuid4().hex[:8]
        await self.send(msg, reply_markup=self._make_feedback_keyboard(alert_id))
        self._record_sent(topic)
        _log_alert_entry({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "sent",
            "alert_id": alert_id,
            "topic": topic,
            "event_count": 1,
            "critical": True,
            "msg_preview": msg[:120],
        })
        log.info(f"Alerte Squeeze critique envoyée: score={sq.score} alert_id={alert_id}")

        # Validation post-événement : le squeeze se confirme-t-il ?
        direction = sq.direction_bias.lower() if sq.direction_bias and sq.direction_bias != "NEUTRAL" else None
        self._tracker.register(
            alert_id=alert_id,
            topic="squeeze_critique",
            event_types=["squeeze"],
            btc_at_alert=btc_price,
            level=sq.trigger_zone,
            direction=direction,
            metadata={},
        )

        # Log dans event_store pour backtest / accuracy
        if sq.direction_bias in ("UP", "DOWN"):
            etype = "squeeze_bullish" if sq.direction_bias == "UP" else "squeeze_bearish"
            ctx   = self._signal_context
            try:
                get_event_store().log_event(
                    event_type=etype,
                    spot=btc_price,
                    signal_strength=sq.score,
                    quality_state="ACTIONABLE",
                    gex_near=ctx["gex_near"],
                    mopi_score=ctx["mopi_score"],
                    squeeze_score=sq.score,
                    nearest_wall=ctx["nearest_wall"],
                    nearest_gravity_zone=ctx["nearest_gravity_zone"],
                    direction=sq.direction_bias,
                )
            except Exception as e:
                log.warning(f"[event_store] log_event squeeze error: {e}")


# ─── Scheduler ───────────────────────────────────────────────────────────────

class AlertScheduler:
    def __init__(
        self,
        alerter: TelegramAlerter,
        deribit: DeribitClient,
        iv_history: list,
        gex_calibration_cache: Optional[dict] = None,
    ):
        self.alerter    = alerter
        self.deribit    = deribit
        self.iv_history = iv_history
        self._cal_cache: dict = gex_calibration_cache if gex_calibration_cache is not None else {}

        self._last_gex:     Optional[GEXProfile] = None
        self._last_wall_oi: Dict[float, float]   = {}
        self._last_btc:     float                = 0.0
        self._near_walls:   Set[float]           = set()

        # État DEX/MOPI — instrumentation silencieuse J+30
        self._last_dex_dir:         Optional[str]      = None
        self._last_mopi_label:      Optional[str]      = None
        self._last_mopi_extreme_ts: Optional[datetime] = None

        # Cooldown gravity_magnet (4h)
        self._last_gravity_magnet_ts: Optional[datetime] = None

        # Velocity MOPI — historique ~5h pour calcul pente 4h
        self._mopi_score_history: list = []          # [(datetime, float)]
        self._last_mopi_score:    Optional[float] = None
        # Régime GEX dernier loggué silencieusement (évite doublons)
        self._last_gex_regime_silent: Optional[str] = None
        # Max Pain dernier loggué silencieusement
        self._last_max_pain_near_silent: Optional[float] = None
        # Gravity explosif — dernier state_hash loggué (zone_center+bias+dist_bucket)
        # Remplace l'ancien cooldown timestamp : même état = même setup, pas de nouveau PendingEvent
        self._last_gravity_explosive_state_hash: Optional[str] = None
        # Cooldown max_pain_pull pipeline 72h (4h — indépendant du log silencieux)
        self._last_max_pain_pull_72h_ts: Optional[datetime] = None

    def _log_silent_events(self, dp, dex_levels, mopi, gex, gmap, btc_price: float) -> None:
        """Instrumentation silencieuse DEX/MOPI — logs uniquement, zéro Telegram."""
        now_dt = datetime.now(timezone.utc)
        now    = now_dt.isoformat()
        store  = get_event_store()
        ctx    = self.alerter._signal_context  # corrigé : était self._signal_context (bug AttributeError)

        # ── 1. DEX — observation → pipeline outcome (log_observation, dédup par état) ─
        new_dex_dir = dp.direction
        if new_dex_dir != "NEUTRAL":
            dex_etype  = "dex_bullish" if new_dex_dir == "BULLISH_FLOWS" else "dex_bearish"
            dex_dir_ud = "UP" if new_dex_dir == "BULLISH_FLOWS" else "DOWN"
            dex_profile = dex_levels.dex_profile if dex_levels else "UNKNOWN"
            use_in_sig = (
                dex_levels is not None
                and dex_levels.dex_profile not in ("DORMANT", "STRUCTURAL")
            )
            # Grouper ACTIVE/ACTIONABLE comme "usable" : leur micro-alternance ne constitue pas
            # un nouveau setup statistique. Nouveau setup seulement si direction change,
            # usable/non_usable change, ou pression change de palier (arrondie à 10%).
            usable_group    = "usable" if dex_profile in ("ACTIVE", "ACTIONABLE") else "non_usable"
            pressure_bucket = str(int(abs(dp.pressure_pct) / 10) * 10)
            dex_obs_key     = f"obs_{dex_etype}_{dex_dir_ud}_{usable_group}_{pressure_bucket}"
            store.log_observation(
                event_type=dex_etype,
                spot=btc_price,
                direction=dex_dir_ud,
                signal_strength=min(100.0, abs(dp.pressure_pct)),
                quality_state=dex_profile,
                gex_near=ctx["gex_near"],
                mopi_score=ctx["mopi_score"],
                squeeze_score=ctx["squeeze_score"],
                nearest_wall=ctx["nearest_wall"],
                nearest_gravity_zone=ctx["nearest_gravity_zone"],
                indicators={
                    "family": "direction",
                    "source": "dealer_pressure",
                },
                metadata={
                    "dex_structural":    round(dex_levels.structural, 2) if dex_levels else None,
                    "dex_active":        round(dex_levels.active, 2)     if dex_levels else None,
                    "dex_actionable":    round(dex_levels.actionable, 2) if dex_levels else None,
                    "dex_profile":       dex_profile,
                    "dex_use_in_signal": use_in_sig,
                    "net_delta":         round(dp.net_delta, 2),
                    "pressure_pct":      round(dp.pressure_pct, 1),
                },
                dedup_key=dex_obs_key,
            )

        # Changement de direction DEX → log_event() pipeline 72h
        if self._last_dex_dir is not None and self._last_dex_dir != new_dex_dir:
            _log_alert_entry({
                "ts": now, "action": "silent_log", "event": "dex_regime_change",
                "from": self._last_dex_dir, "to": new_dex_dir,
                "net_delta": dp.net_delta, "btc_price": btc_price,
            })
            log.info(f"[silent] dex_regime_change {self._last_dex_dir} → {new_dex_dir} delta={dp.net_delta:.1f} BTC")
            dex_etype_72h = None
            if new_dex_dir == "BULLISH_FLOWS":
                dex_etype_72h = "dealer_buy_pressure"
            elif new_dex_dir == "BEARISH_FLOWS":
                dex_etype_72h = "dealer_sell_pressure"
            if dex_etype_72h:
                _dex_flip_dir = "UP" if dex_etype_72h == "dealer_buy_pressure" else "DOWN"
                _dex_flip_sid: Optional[str] = None
                try:
                    _, _dex_flip_sid = store._get_setup_tracker().process_observation(
                        dex_etype_72h, now,
                        state_params={"direction": _dex_flip_dir},
                    )
                except Exception:
                    pass
                try:
                    store.log_event(
                        event_type=dex_etype_72h,
                        spot=btc_price,
                        signal_strength=min(100.0, abs(dp.pressure_pct)),
                        quality_state="ACTIVE",
                        gex_near=ctx["gex_near"],
                        mopi_score=ctx["mopi_score"],
                        squeeze_score=ctx["squeeze_score"],
                        nearest_wall=ctx["nearest_wall"],
                        nearest_gravity_zone=ctx["nearest_gravity_zone"],
                        direction=_dex_flip_dir,
                        setup_id=_dex_flip_sid,
                    )
                except Exception as e:
                    log.warning(f"[event_store] log_event dex error: {e}")
        self._last_dex_dir = new_dex_dir

        # ── 2. MOPI — observations granulaires ────────────────────────────────

        # Historique velocity
        self._mopi_score_history.append((now_dt, mopi.score))
        cutoff_5h = now_dt - timedelta(hours=5)
        self._mopi_score_history = [
            (ts, s) for ts, s in self._mopi_score_history if ts > cutoff_5h
        ]
        prev_score = self._last_mopi_score

        # Extremes 70/30 (élargi de 75/25) avec cooldown 4h
        if mopi.score < 30 or mopi.score > 70:
            cooldown_ok = (
                self._last_mopi_extreme_ts is None
                or (now_dt - self._last_mopi_extreme_ts) >= timedelta(hours=4)
            )
            if cooldown_ok:
                ext_etype = "mopi_extreme_high" if mopi.score > 70 else "mopi_extreme_low"
                store.log_silent_event(
                    event_type=ext_etype,
                    spot=btc_price,
                    direction="UP" if mopi.score > 70 else "DOWN",
                    indicators={"family": "contrarian", "mopi_score": round(mopi.score, 1)},
                    metadata={"label": mopi.label, "threshold": 70 if mopi.score > 70 else 30},
                )
                self._last_mopi_extreme_ts = now_dt
                _log_alert_entry({
                    "ts": now, "action": "silent_log", "event": "mopi_extreme",
                    "score": round(mopi.score, 1), "label": mopi.label, "btc_price": btc_price,
                })
                log.info(f"[silent] mopi_extreme score={mopi.score:.1f} ({mopi.label})")
                # Pipeline 72h — seuil aligné avec l'observation silencieuse (70/30)
                if mopi.score > 70 or mopi.score < 30:
                    mopi_etype_72h = "mopi_bullish" if mopi.score > 75 else "mopi_bearish"
                    _mopi_ext_dir = "UP" if mopi_etype_72h == "mopi_bullish" else "DOWN"
                    _mopi_ext_sid: Optional[str] = None
                    try:
                        _, _mopi_ext_sid = store._get_setup_tracker().process_observation(
                            mopi_etype_72h, now,
                            state_params={"direction": _mopi_ext_dir},
                        )
                    except Exception:
                        pass
                    try:
                        store.log_event(
                            event_type=mopi_etype_72h,
                            spot=btc_price,
                            signal_strength=abs(mopi.score - 50) * 2,
                            quality_state="ACTIVE",
                            gex_near=ctx["gex_near"],
                            mopi_score=mopi.score,
                            squeeze_score=ctx["squeeze_score"],
                            nearest_wall=ctx["nearest_wall"],
                            nearest_gravity_zone=ctx["nearest_gravity_zone"],
                            direction=_mopi_ext_dir,
                            setup_id=_mopi_ext_sid,
                        )
                    except Exception as e:
                        log.warning(f"[event_store] log_event mopi error: {e}")

        # Crosses 55/45
        if prev_score is not None:
            if prev_score < 55 <= mopi.score:
                store.log_silent_event(
                    event_type="mopi_bullish_cross",
                    spot=btc_price,
                    direction="UP",
                    indicators={"family": "direction", "mopi_score": round(mopi.score, 1), "prev_score": round(prev_score, 1)},
                    metadata={"threshold": 55},
                )
                _mopi_cross_bull_sid: Optional[str] = None
                try:
                    _, _mopi_cross_bull_sid = store._get_setup_tracker().process_observation(
                        "mopi_cross", now, state_params={"direction": "UP"}
                    )
                except Exception:
                    pass
                try:
                    store.log_event(
                        event_type="mopi_cross",
                        spot=btc_price,
                        signal_strength=min(100.0, abs(mopi.score - 50) * 2),
                        quality_state="ACTIVE",
                        gex_near=ctx.get("gex_near", 0.0),
                        mopi_score=mopi.score,
                        squeeze_score=ctx.get("squeeze_score", 50.0),
                        nearest_wall=ctx.get("nearest_wall", 0.0),
                        nearest_gravity_zone=ctx.get("nearest_gravity_zone", 0.0),
                        direction="UP",
                        sent=False,
                        blocked_reason="silent_observation",
                        setup_id=_mopi_cross_bull_sid,
                    )
                except Exception as e:
                    log.warning(f"[event_store] log_event mopi_cross bullish: {e}")
            elif prev_score > 45 >= mopi.score:
                store.log_silent_event(
                    event_type="mopi_bearish_cross",
                    spot=btc_price,
                    direction="DOWN",
                    indicators={"family": "direction", "mopi_score": round(mopi.score, 1), "prev_score": round(prev_score, 1)},
                    metadata={"threshold": 45},
                )
                _mopi_cross_bear_sid: Optional[str] = None
                try:
                    _, _mopi_cross_bear_sid = store._get_setup_tracker().process_observation(
                        "mopi_cross", now, state_params={"direction": "DOWN"}
                    )
                except Exception:
                    pass
                try:
                    store.log_event(
                        event_type="mopi_cross",
                        spot=btc_price,
                        signal_strength=min(100.0, abs(mopi.score - 50) * 2),
                        quality_state="ACTIVE",
                        gex_near=ctx.get("gex_near", 0.0),
                        mopi_score=mopi.score,
                        squeeze_score=ctx.get("squeeze_score", 50.0),
                        nearest_wall=ctx.get("nearest_wall", 0.0),
                        nearest_gravity_zone=ctx.get("nearest_gravity_zone", 0.0),
                        direction="DOWN",
                        sent=False,
                        blocked_reason="silent_observation",
                        setup_id=_mopi_cross_bear_sid,
                    )
                except Exception as e:
                    log.warning(f"[event_store] log_event mopi_cross bearish: {e}")

        # Velocity ~4h (fenêtre 3-5h)
        old_4h_pts = [
            (ts, s) for ts, s in self._mopi_score_history
            if 3 * 3600 <= (now_dt - ts).total_seconds() <= 5 * 3600
        ]
        if old_4h_pts:
            oldest_ts, oldest_score = min(old_4h_pts, key=lambda x: x[0])
            velocity = mopi.score - oldest_score
            if velocity <= -15:
                store.log_silent_event(
                    event_type="mopi_velocity_down",
                    spot=btc_price,
                    direction="DOWN",
                    indicators={"family": "direction", "mopi_score": round(mopi.score, 1), "velocity_4h": round(velocity, 1)},
                    metadata={"old_score": round(oldest_score, 1), "old_ts": oldest_ts.isoformat()},
                )
            elif velocity >= 15:
                store.log_silent_event(
                    event_type="mopi_velocity_up",
                    spot=btc_price,
                    direction="UP",
                    indicators={"family": "direction", "mopi_score": round(mopi.score, 1), "velocity_4h": round(velocity, 1)},
                    metadata={"old_score": round(oldest_score, 1), "old_ts": oldest_ts.isoformat()},
                )

        new_mopi_label = mopi.label
        if self._last_mopi_label is not None and self._last_mopi_label != new_mopi_label:
            _log_alert_entry({
                "ts": now, "action": "silent_log", "event": "mopi_regime_change",
                "from": self._last_mopi_label, "to": new_mopi_label,
                "score": round(mopi.score, 1), "btc_price": btc_price,
            })
            log.info(f"[silent] mopi_regime_change {self._last_mopi_label} → {new_mopi_label} score={mopi.score:.1f}")
        self._last_mopi_label = new_mopi_label
        self._last_mopi_score = mopi.score

    def _log_silent_gex(self, gex, flip_audit, spot: float) -> None:
        """Log silencieux GEX — régime amplifier/stabilizer + flip actionnable + calibration."""
        store = get_event_store()

        # Régime — seulement si changement (évite doublons)
        if gex.regime != "NEUTRE" and gex.regime != self._last_gex_regime_silent:
            etype = "gex_amplifier" if gex.regime == "AMPLIFICATEUR" else "gex_stabilizer"
            store.log_silent_event(
                event_type=etype,
                spot=spot,
                direction=None,
                indicators={"family": "magnitude", "gex_total_m": round(gex.total_gex / 1e6, 2)},
                metadata={
                    "regime": gex.regime,
                    "gex_near_m": round(gex.gex_near / 1e6, 2),
                    "regime_confidence": gex.regime_confidence,
                    "regime_state": gex.regime_state,
                },
            )
            _gex_regime_sid: Optional[str] = None
            try:
                _, _gex_regime_sid = store._get_setup_tracker().process_observation(
                    "gex_regime", f"gex_regime_{gex.regime}",
                    state_params={"regime": gex.regime},
                )
            except Exception:
                pass
            try:
                ctx = self.alerter._signal_context
                store.log_event(
                    event_type="gex_regime",
                    spot=spot,
                    signal_strength=min(100.0, abs(gex.gex_near / 1e9) * 10),
                    quality_state="ACTIVE",
                    gex_near=gex.gex_near,
                    mopi_score=ctx.get("mopi_score", 50.0),
                    squeeze_score=ctx.get("squeeze_score", 50.0),
                    nearest_wall=ctx.get("nearest_wall", 0.0),
                    nearest_gravity_zone=ctx.get("nearest_gravity_zone", 0.0),
                    direction=None,
                    sent=False,
                    blocked_reason="silent_observation",
                    setup_id=_gex_regime_sid,
                )
            except Exception as e:
                log.warning(f"[event_store] log_event gex_regime: {e}")
        self._last_gex_regime_silent = gex.regime

        # Flip actionnable
        if flip_audit is not None and flip_audit.flip_use_in_signal and gex.flip_level is not None:
            dist_pct = abs(gex.flip_level - spot) / spot * 100
            store.log_silent_event(
                event_type="gex_flip_actionable",
                spot=spot,
                direction=None,
                indicators={"family": "level", "flip_level": gex.flip_level, "distance_pct": round(dist_pct, 2)},
                metadata={
                    "flip_activity_tag": flip_audit.flip_activity_tag,
                    "flip_signal_quality": flip_audit.flip_signal_quality,
                    "flip_use_in_signal": True,
                    "use_in_signal": dist_pct <= 5.0,
                },
            )

        # Calibration dégradée
        cal_status = self.alerter._cal_status
        if cal_status != "available":
            store.log_silent_event(
                event_type="gex_calibration_degraded",
                spot=spot,
                direction=None,
                indicators={"family": "quality", "cal_status": cal_status},
                metadata={"cal_reason_code": self.alerter._cal_reason_code},
            )

    def _log_silent_gravity(self, gmap, spot: float) -> None:
        """Log silencieux Gravity — zones explosives (cooldown 30 min) + magnet actif."""
        now_dt = datetime.now(timezone.utc)
        store  = get_event_store()

        # Magnet actif
        if gmap.gravity_score >= 65 and gmap.strongest_magnet > 0:
            dist_m = abs(gmap.strongest_magnet - spot) / spot * 100
            store.log_silent_event(
                event_type="gravity_magnet_active",
                spot=spot,
                direction="UP" if gmap.strongest_magnet > spot else "DOWN",
                indicators={"family": "level", "strength": round(gmap.gravity_score, 1), "distance_pct": round(dist_m, 2)},
                metadata={
                    "magnet_level": gmap.strongest_magnet,
                    "gravity_score": round(gmap.gravity_score, 1),
                    "use_in_signal": dist_m <= 5.0,
                },
            )

        # Zones explosives — dédup par state_hash (zone_center+bias+dist_bucket)
        # Règle : même zone + même bias + même distance ≈ même setup persistant, pas N signaux indépendants
        _BIAS_MAP = {
            "DOWN_ONLY":  ("gravity_explosive_down",      "DOWN"),
            "UP_ONLY":    ("gravity_explosive_up",        "UP"),
            "SYMMETRIC":  ("gravity_explosive_symmetric", None),
        }
        zones_sorted = sorted(
            (z for z in gmap.zones if z.zone_type == "EXPLOSIVE"),
            key=lambda z: abs(z.center - spot),
        )
        for zone in zones_sorted:
            dist_pct = abs(zone.center - spot) / spot * 100
            if dist_pct > 10.0:
                break
            mapped = _BIAS_MAP.get(zone.explosive_bias)
            if not mapped:
                continue
            etype, direction = mapped

            # State hash : zone_center (arrondi 1k) + bias + distance bucket (2%)
            center_k    = int(zone.center / 1000) * 1000
            dist_bucket = int(dist_pct / 2) * 2
            state_hash  = f"gravity_{etype}_{center_k}_{zone.explosive_bias}_{dist_bucket}"

            store.log_silent_event(
                event_type=etype,
                spot=spot,
                direction=direction,
                indicators={
                    "family": "magnitude",
                    "strength": round(zone.strength, 1),
                    "distance_pct": round(dist_pct, 2),
                    "activity_tag": "EXPLOSIVE",
                    "use_in_signal": dist_pct <= 5.0,
                },
                metadata={
                    "explosive_bias": zone.explosive_bias,
                    "explosive_score_down": round(zone.explosive_score_down, 1),
                    "explosive_score_up":   round(zone.explosive_score_up, 1),
                    "zone_center": zone.center,
                    "gex_zone": round(zone.gex, 0),
                },
                dedup_key=state_hash,
            )
            # Setup tracker pour le pipeline 72h gravity_explosive (état = state_hash)
            # Un changement de zone/bias/distance-bucket = nouveau setup statistique
            gravity_setup_id: Optional[str] = None
            try:
                _, gravity_setup_id = store._get_setup_tracker().process_observation(
                    "gravity_explosive", state_hash
                )
            except Exception:
                pass

            # Pipeline log_event : seulement si le state_hash change (nouvel état = nouveau setup)
            if state_hash != self._last_gravity_explosive_state_hash:
                try:
                    ctx = self.alerter._signal_context
                    store.log_event(
                        event_type="gravity_explosive",
                        spot=spot,
                        signal_strength=round(zone.strength, 1),
                        quality_state="ACTIVE",
                        gex_near=ctx.get("gex_near", 0.0),
                        mopi_score=ctx.get("mopi_score", 50.0),
                        squeeze_score=ctx.get("squeeze_score", 50.0),
                        nearest_wall=ctx.get("nearest_wall", 0.0),
                        nearest_gravity_zone=zone.center,
                        direction=direction,
                        sent=False,
                        blocked_reason="silent_observation",
                        setup_id=gravity_setup_id,
                    )
                except Exception as e:
                    log.warning(f"[event_store] log_event gravity_explosive: {e}")
                self._last_gravity_explosive_state_hash = state_hash
            break  # zone la plus proche uniquement

    def _log_silent_max_pain(self, gex, spot: float) -> None:
        """Log silencieux Max Pain — near-term à chaque poll + shift si changement ≥1%."""
        store     = get_event_store()
        mp_profile = gex.max_pain_profile
        if not mp_profile:
            return

        near     = mp_profile.near
        dist_pct = abs(near.strike - spot) / spot * 100
        last_mp  = self._last_max_pain_near_silent

        mp_direction = "UP" if near.strike > spot else "DOWN"
        # dedup_key : même expiry + même strike + même direction = même setup persistant
        # Sans cette clé, log_silent_event fire à chaque poll → compteur gonflé artificiellement
        mp_dedup_key = f"max_pain_pull_{near.expiry}_{near.strike}_{mp_direction}"
        store.log_silent_event(
            event_type="max_pain_near_pull",
            spot=spot,
            direction=mp_direction,
            indicators={"family": "level", "distance_pct": round(dist_pct, 2)},
            metadata={
                "max_pain_near": near.strike,
                "expiry": near.expiry,
                "dte": near.dte,
                "oi_total": round(near.oi_total, 0),
                "institutional_max_pain": mp_profile.institutional.strike,
                "use_in_signal": dist_pct <= 5.0,
            },
            dedup_key=mp_dedup_key,
        )
        # Setup tracker pour le pipeline 72h max_pain_pull (même clé = même setup)
        mp_setup_id: Optional[str] = None
        try:
            _, mp_setup_id = store._get_setup_tracker().process_observation(
                "max_pain_pull", mp_dedup_key
            )
        except Exception:
            pass

        # Pipeline 72h — cooldown 4h indépendant
        now_dt = datetime.now(timezone.utc)
        pull_ok = (
            self._last_max_pain_pull_72h_ts is None
            or (now_dt - self._last_max_pain_pull_72h_ts) >= timedelta(hours=4)
        )
        if pull_ok and dist_pct <= 5.0:
            try:
                ctx = self.alerter._signal_context
                store.log_event(
                    event_type="max_pain_pull",
                    spot=spot,
                    signal_strength=min(100.0, max(0.0, (5.0 - dist_pct) / 5.0 * 100)),
                    quality_state="ACTIVE",
                    gex_near=ctx.get("gex_near", 0.0),
                    mopi_score=ctx.get("mopi_score", 50.0),
                    squeeze_score=ctx.get("squeeze_score", 50.0),
                    nearest_wall=ctx.get("nearest_wall", 0.0),
                    nearest_gravity_zone=near.strike,
                    direction=mp_direction,
                    sent=False,
                    blocked_reason="silent_observation",
                    setup_id=mp_setup_id,
                )
                self._last_max_pain_pull_72h_ts = now_dt
            except Exception as e:
                log.warning(f"[event_store] log_event max_pain_pull: {e}")

        # Shift significatif ≥1%
        if last_mp is not None and last_mp > 0 and abs(near.strike - last_mp) / last_mp >= 0.01:
            shift_dir = "UP" if near.strike > last_mp else "DOWN"
            store.log_silent_event(
                event_type="max_pain_shift",
                spot=spot,
                direction=shift_dir,
                indicators={
                    "family": "level",
                    "shift_pct": round((near.strike - last_mp) / last_mp * 100, 2),
                },
                metadata={
                    "old_max_pain": last_mp,
                    "new_max_pain": near.strike,
                    "expiry": near.expiry,
                    "dte": near.dte,
                },
            )
            log.info(f"[silent] max_pain_shift {last_mp:,.0f} → {near.strike:,.0f} ({(near.strike - last_mp) / last_mp * 100:+.1f}%)")
            _mp_shift_sid: Optional[str] = None
            try:
                _, _mp_shift_sid = store._get_setup_tracker().process_observation(
                    "max_pain_shift", f"mp_shift_{near.strike}_{shift_dir}",
                    state_params={"direction": shift_dir},
                )
            except Exception:
                pass
            try:
                ctx = self.alerter._signal_context
                store.log_event(
                    event_type="max_pain_shift",
                    spot=spot,
                    signal_strength=min(100.0, abs((near.strike - last_mp) / last_mp) * 1000),
                    quality_state="ACTIVE",
                    gex_near=ctx.get("gex_near", 0.0),
                    mopi_score=ctx.get("mopi_score", 50.0),
                    squeeze_score=ctx.get("squeeze_score", 50.0),
                    nearest_wall=ctx.get("nearest_wall", 0.0),
                    nearest_gravity_zone=near.strike,
                    direction=shift_dir,
                    sent=False,
                    blocked_reason="silent_observation",
                    setup_id=_mp_shift_sid,
                )
            except Exception as e:
                log.warning(f"[event_store] log_event max_pain_shift: {e}")

        self._last_max_pain_near_silent = near.strike

    async def run_forever(self) -> None:
        await asyncio.gather(
            self._poll_loop(interval=300),
            self._flush_loop(interval=30),
            self._daily_brief_at_8h_utc(),
            self._daily_report_at_20h_utc(),
            self._weekly_report_loop(),
            self.alerter._tracker.run_validation_loop(),
            get_event_store().run_outcome_loop(),
        )

    async def _poll_loop(self, interval: int) -> None:
        """Récupère le snapshot Deribit (via cache) et empile les événements dans le buffer."""
        while True:
            try:
                snapshot    = await self.deribit.get_cached_snapshot()
                gex         = compute_gex(snapshot)
                mopi        = compute_mopi(snapshot, gex, self.iv_history)
                dp          = compute_dealer_pressure(snapshot)
                dex_levels  = compute_dex_levels(snapshot)
                sq          = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
                walls       = compute_options_walls(snapshot)
                gmap        = compute_gravity_map(snapshot, gex)
                spot        = snapshot.btc_price
                cur_wall_oi = {w.strike: w.total_oi for w in walls.walls}

                # Audit qualité GEX — gate alertes dormant + narrative
                try:
                    audit = compute_gex_activity_audit(snapshot)
                    self.alerter.update_gex_audit(audit)
                except Exception as e:
                    log.warning(f"[poll] compute_gex_activity_audit erreur: {e}")
                    audit = None

                # Audit qualité flip level — gate alerte flip dormant
                try:
                    flip_audit = compute_flip_activity_audit(snapshot, gex.flip_level)
                    self.alerter.update_flip_audit(flip_audit)
                except Exception as e:
                    log.warning(f"[poll] compute_flip_activity_audit erreur: {e}")
                    flip_audit = None

                gravity_audit = None
                try:
                    gravity_audit = compute_gravity_activity_audit(snapshot)
                except Exception as e:
                    log.warning(f"[poll] compute_gravity_activity_audit erreur: {e}")

                # Calibration GEX — statut transmis à l'alerter et au narrative
                try:
                    cal_diag = diag_gex_calibration(self._cal_cache)
                    self.alerter.update_calibration(cal_diag.status, cal_diag.reason_code)
                except Exception as e:
                    log.warning(f"[poll] diag_gex_calibration erreur: {e}")
                    cal_diag = type("_D", (), {"status": "available", "reason_code": "calibration_available"})()

                # Narrative Resolver — mise à jour source de vérité globale
                try:
                    narrative = resolve_narrative(
                        mopi, gex, dp, gmap, walls, sq, spot,
                        audit=audit, gravity_audit=gravity_audit,
                        flip_audit=flip_audit,
                        calibration_status=cal_diag.status,
                        calibration_reason_code=cal_diag.reason_code,
                    )
                    self.alerter.update_narrative(narrative)
                except Exception as e:
                    log.warning(f"[poll] resolve_narrative erreur: {e}")

                # Calcul du nearest_wall pour event_store context
                nearest_wall = 0.0
                if walls.major_call_wall and walls.major_put_wall:
                    dist_call = abs(spot - walls.major_call_wall)
                    dist_put  = abs(spot - walls.major_put_wall)
                    nearest_wall = walls.major_call_wall if dist_call < dist_put else walls.major_put_wall
                elif walls.major_call_wall:
                    nearest_wall = walls.major_call_wall
                elif walls.major_put_wall:
                    nearest_wall = walls.major_put_wall

                # Mise à jour du contexte signal (enrichit les événements event_store)
                self.alerter.update_signal_context(
                    gex_near=gex.gex_near,
                    mopi_score=mopi.score,
                    squeeze_score=sq.score,
                    nearest_wall=nearest_wall,
                    nearest_gravity_zone=gmap.strongest_magnet,
                )

                # Ne pas envoyer d'alertes trading sur données incomplètes (rate-limit Deribit)
                if self.deribit.data_stale:
                    log.warning("[poll] data_stale=True — alertes trading suspendues")
                elif self._last_gex:
                    self.alerter.ingest_gex_flip(self._last_gex, gex)
                    self.alerter.ingest_wall_events(
                        walls, self._last_wall_oi, self._last_btc, self._near_walls
                    )
                    await self.alerter.send_squeeze_alert(sq, spot)
                    self._log_silent_events(dp, dex_levels, mopi, gex, gmap, spot)

                    # Gravity magnet — pipeline 72h (cooldown 4h)
                    if gmap.gravity_score >= 65:
                        now_dt = datetime.now(timezone.utc)
                        gm_ok  = (
                            self._last_gravity_magnet_ts is None
                            or (now_dt - self._last_gravity_magnet_ts) >= timedelta(hours=4)
                        )
                        if gm_ok:
                            try:
                                get_event_store().log_event(
                                    event_type="gravity_magnet",
                                    spot=spot,
                                    signal_strength=gmap.gravity_score,
                                    quality_state="ACTIVE",
                                    gex_near=gex.gex_near,
                                    mopi_score=mopi.score,
                                    squeeze_score=sq.score,
                                    nearest_wall=nearest_wall,
                                    nearest_gravity_zone=gmap.strongest_magnet,
                                    direction="UP" if gmap.strongest_magnet > spot else "DOWN",
                                )
                                self._last_gravity_magnet_ts = now_dt
                            except Exception as e:
                                log.warning(f"[event_store] gravity_magnet: {e}")

                    # Observations silencieuses GEX / Gravity / Max Pain
                    try:
                        self._log_silent_gex(gex, flip_audit, spot)
                    except Exception as e:
                        log.warning(f"[silent] _log_silent_gex: {e}")
                    try:
                        self._log_silent_gravity(gmap, spot)
                    except Exception as e:
                        log.warning(f"[silent] _log_silent_gravity: {e}")
                    try:
                        self._log_silent_max_pain(gex, spot)
                    except Exception as e:
                        log.warning(f"[silent] _log_silent_max_pain: {e}")

                self._last_gex     = gex
                self._last_wall_oi = cur_wall_oi
                self._last_btc     = spot
                self._near_walls   = {s for s in cur_wall_oi if abs(spot - s) / s < 0.01}

            except Exception as e:
                log.error(f"Erreur poll: {e}")
            await asyncio.sleep(interval)

    async def _flush_loop(self, interval: int = 30) -> None:
        """Vérifie toutes les 30 s si des topics sont prêts à flusher."""
        while True:
            await asyncio.sleep(interval)
            try:
                n = await self.alerter.flush_ready()
                if n:
                    log.info(f"[flush_loop] {n} alerte(s) envoyée(s)")
            except Exception as e:
                log.error(f"Erreur flush_loop: {e}")

    async def _daily_report_at_20h_utc(self) -> None:
        """Envoie le rapport signal/bruit chaque jour à 20h UTC."""
        while True:
            now      = datetime.now(timezone.utc)
            next_20h = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now.hour >= 20:
                next_20h = next_20h + timedelta(days=1)
            await asyncio.sleep((next_20h - now).total_seconds())
            try:
                await self.alerter.send_report()
            except Exception as e:
                log.error(f"Erreur rapport signal/bruit: {e}")

    async def _weekly_report_loop(self) -> None:
        """Envoie le rapport qualité 7j chaque dimanche à 20h UTC."""
        while True:
            now = datetime.now(timezone.utc)
            days_until_sunday = (6 - now.weekday()) % 7
            if days_until_sunday == 0 and now.hour >= 20:
                days_until_sunday = 7
            next_sunday = (now + timedelta(days=days_until_sunday)).replace(
                hour=20, minute=0, second=0, microsecond=0
            )
            await asyncio.sleep((next_sunday - now).total_seconds())
            try:
                await self.alerter.send_weekly_report()
            except Exception as e:
                log.error(f"Erreur rapport hebdo: {e}")

    async def _daily_brief_at_8h_utc(self) -> None:
        while True:
            now     = datetime.now(timezone.utc)
            next_8h = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now.hour >= 8:
                next_8h = next_8h + timedelta(days=1)
            await asyncio.sleep((next_8h - now).total_seconds())
            try:
                snapshot  = await self.deribit.get_cached_snapshot()
                gex       = compute_gex(snapshot)
                mopi      = compute_mopi(snapshot, gex, self.iv_history)
                weather   = compute_weather(gex, mopi)
                dp        = compute_dealer_pressure(snapshot)
                sq        = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
                gmap      = compute_gravity_map(snapshot, gex)
                walls     = compute_options_walls(snapshot)
                try:
                    audit = compute_gex_activity_audit(snapshot)
                except Exception:
                    audit = None
                try:
                    g_audit = compute_gravity_activity_audit(snapshot)
                except Exception:
                    g_audit = None
                dex_levels = compute_dex_levels(snapshot)
                try:
                    flip_audit_b = compute_flip_activity_audit(snapshot, gex.flip_level)
                except Exception:
                    flip_audit_b = None
                try:
                    cal_diag_b = diag_gex_calibration(self._cal_cache)
                except Exception:
                    cal_diag_b = type("_D", (), {"status": "available", "reason_code": "calibration_available"})()
                narrative = resolve_narrative(
                    mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
                    audit=audit, gravity_audit=g_audit, flip_audit=flip_audit_b,
                    calibration_status=cal_diag_b.status,
                    calibration_reason_code=cal_diag_b.reason_code,
                )
                horizon_narratives: Dict[str, HorizonNarrative] = {}
                for h in ("4h", "24h", "72h"):
                    try:
                        horizon_narratives[h] = resolve_narrative_horizon(
                            mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
                            horizon=h, audit=audit,
                            dex_levels=dex_levels, gravity_audit=g_audit,
                            flip_audit=flip_audit_b,
                            calibration_status=cal_diag_b.status,
                            calibration_reason_code=cal_diag_b.reason_code,
                        )
                    except Exception as ex:
                        log.warning(f"Horizon {h} error: {ex}")
                await self.alerter.send_daily_brief(
                    gex, mopi, weather,
                    squeeze=sq,
                    gravity_narrative=gmap.narrative,
                    narrative=narrative,
                    horizon_narratives=horizon_narratives or None,
                )
            except Exception as e:
                log.error(f"Erreur brief quotidien: {e}")
