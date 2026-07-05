"""
Phase Observation — System Edge Report.

Agrège tous les métriques moteur pour mesurer l'edge réel.
Le juge n'est plus la logique du développeur — c'est l'outcome réel du marché.

Règle : ne recalibrer MIN_SCORE_TO_SEND que si :
  - 30 signaux envoyés
  - 30 signaux bloqués
  - 10 outcomes +72h par grande catégorie d'indicateur
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .event_store import get_event_store, EVENT_LOG_PATH, PENDING_EVENTS_PATH
from .indicator_accuracy import compute_indicator_accuracy, _INDICATOR_GROUPS
from .alerts import STATS_LOG_PATH

log = logging.getLogger(__name__)

MIN_SENT_BEFORE_RECAL    = 30
MIN_BLOCKED_BEFORE_RECAL = 30
MIN_72H_PER_CATEGORY     = 10


def _read_jsonl(path: Path, days: int) -> List[dict]:
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
    entries: List[dict] = []
    if not path.exists():
        return entries
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_raw = rec.get("ts")
                    if ts_raw:
                        ts = datetime.fromisoformat(ts_raw)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            entries.append(rec)
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"[edge_report] read {path.name}: {e}")
    return entries


def compute_system_edge_report(days: int = 14) -> dict:
    """
    Rapport Phase Observation complet.
    Retourne un dict structuré prêt pour l'API et Telegram.
    """
    now = datetime.now(timezone.utc)

    # ── 1. Alert log ──────────────────────────────────────────────────────────
    alert_log = _read_jsonl(STATS_LOG_PATH, days=days)

    sent_entries     = [e for e in alert_log if e.get("action") == "sent"]
    blocked_entries  = [e for e in alert_log if e.get("action", "").startswith("blocked")]
    blocked_noise    = [e for e in alert_log if e.get("action") == "blocked_noise"]
    blocked_cd       = [e for e in alert_log if e.get("action") == "blocked_cooldown"]
    blocked_budget   = [e for e in alert_log if e.get("action") == "blocked_budget"]
    feedback_entries = [e for e in alert_log if e.get("action") == "feedback"]

    events_sent    = len(sent_entries)
    events_blocked = len(blocked_entries)
    total_events   = events_sent + events_blocked
    block_rate     = round(events_blocked / total_events * 100, 1) if total_events else None

    fb_counts = Counter(e.get("vote") for e in feedback_entries)
    total_fb  = len(feedback_entries)
    utile_n   = fb_counts.get("utile", 0)
    inutile_n = fb_counts.get("inutile", 0)
    sat_rate  = round(utile_n / total_fb * 100, 1) if total_fb else None

    # ── 2. Outcomes ───────────────────────────────────────────────────────────
    es = get_event_store()
    pending_total = es.get_pending_count()

    pending_events: List[dict] = []
    if PENDING_EVENTS_PATH.exists():
        try:
            with open(PENDING_EVENTS_PATH) as f:
                pending_events = json.load(f)
        except Exception:
            pass

    outcomes_4h_count  = sum(1 for p in pending_events if p.get("checked_4h"))
    outcomes_24h_count = sum(1 for p in pending_events if p.get("checked_24h"))

    finalized = _read_jsonl(EVENT_LOG_PATH, days=days)
    outcomes_72h_count = len(finalized)

    # ── 3. Per-indicator stats depuis les events finalisés ────────────────────
    _etype_to_ind = {
        et: ind
        for ind, etypes in _INDICATOR_GROUPS.items()
        for et in etypes
    }

    ind_data: Dict[str, dict] = {}
    for rec in finalized:
        etype = rec.get("event_type", "unknown")
        ind   = _etype_to_ind.get(etype, "unknown")
        if ind not in ind_data:
            ind_data[ind] = {
                "n": 0, "hit": 0, "inv": 0,
                "n_4h": 0, "n_24h": 0, "n_72h": 0,
                "hit_4h": 0,
                "o4": [], "o24": [], "o72": [],
            }
        d   = ind_data[ind]
        hit = rec.get("hit_target")
        inv = rec.get("invalidated")
        d["n"] += 1
        if hit: d["hit"] += 1
        if inv: d["inv"] += 1

        o4  = rec.get("outcome_4h")
        o24 = rec.get("outcome_24h")
        o72 = rec.get("outcome_72h")
        if o4 is not None:
            d["n_4h"] += 1
            d["o4"].append(o4)
            if hit: d["hit_4h"] += 1
        if o24 is not None:
            d["n_24h"] += 1
            d["o24"].append(o24)
        if o72 is not None:
            d["n_72h"] += 1
            d["o72"].append(o72)

    indicator_stats: Dict[str, dict] = {}
    for ind, d in ind_data.items():
        n4, n24, n72 = d["n_4h"], d["n_24h"], d["n_72h"]
        wr4  = round(d["hit_4h"] / n4 * 100, 1) if n4  else None
        avg4 = round(sum(d["o4"])  / n4,  2)     if n4  else None
        avg24= round(sum(d["o24"]) / n24, 2)     if n24 else None
        avg72= round(sum(d["o72"]) / n72, 2)     if n72 else None
        indicator_stats[ind] = {
            "n":            d["n"],
            "n_4h":         n4,
            "n_24h":        n24,
            "n_72h":        n72,
            "hit":          d["hit"],
            "winrate_pct":  round(d["hit"] / d["n"] * 100, 1) if d["n"] else None,
            "winrate_4h":   wr4,
            "avg_4h":       avg4,
            "avg_24h":      avg24,
            "avg_72h":      avg72,
            "ev_4h":        round(wr4 / 100 * avg4, 3) if (wr4 and avg4) else None,
        }

    # ── 4. Scores 0-100 (via indicator_accuracy, min 5 signaux) ──────────────
    acc      = compute_indicator_accuracy(days=days)
    scores   = acc.get("scores", {})
    sig_moyen = acc.get("signal_moyen")

    for ind, s in indicator_stats.items():
        s["accuracy_score"] = scores.get(ind)

    # ── 5. Top 5 best / worst par event_type ─────────────────────────────────
    raw_stats = es.get_accuracy_by_event_type(days=days)
    ranked    = [
        (et, s) for et, s in raw_stats.items() if s["total"] >= 3
    ]
    ranked.sort(key=lambda x: x[1]["winrate_pct"], reverse=True)

    def _sig(et: str, s: dict) -> dict:
        return {
            "signal":  et,
            "winrate": s["winrate_pct"],
            "n":       s["total"],
            "avg_4h":  s["avg_outcome_4h"],
            "avg_72h": s["avg_outcome_72h"],
        }

    top5_best  = [_sig(et, s) for et, s in ranked[:5]]
    top5_worst = (
        [_sig(et, s) for et, s in reversed(ranked[-5:])]
        if len(ranked) >= 2 else []
    )

    # ── 6. Phase Observation thresholds ──────────────────────────────────────
    cat_72h = {ind: s.get("n_72h", 0) for ind, s in indicator_stats.items()}
    cats_ok = {ind: n >= MIN_72H_PER_CATEGORY for ind, n in cat_72h.items()}
    ready   = (
        events_sent    >= MIN_SENT_BEFORE_RECAL and
        events_blocked >= MIN_BLOCKED_BEFORE_RECAL and
        bool(cats_ok) and all(cats_ok.values())
    )

    return {
        "generated_at": now.isoformat(),
        "days": days,
        "events": {
            "captured":                  pending_total + outcomes_72h_count,
            "sent":                      events_sent,
            "blocked":                   events_blocked,
            "blocked_noise":             len(blocked_noise),
            "blocked_cooldown":          len(blocked_cd),
            "blocked_budget":            len(blocked_budget),
            "conviction_block_rate_pct": block_rate,
        },
        "outcomes": {
            "pending":       pending_total,
            "validated_4h":  outcomes_4h_count,
            "validated_24h": outcomes_24h_count,
            "validated_72h": outcomes_72h_count,
        },
        "system_score": {
            "avg_score":           sig_moyen,
            "scores_by_indicator": scores,
            "data_sufficient":     sig_moyen is not None,
        },
        "indicator_stats": indicator_stats,
        "ev_by_indicator": {
            ind: s.get("ev_4h") for ind, s in indicator_stats.items()
        },
        "top5_best":  top5_best,
        "top5_worst": top5_worst,
        "feedback": {
            "total":                 total_fb,
            "utile":                 utile_n,
            "inutile":               inutile_n,
            "satisfaction_rate_pct": sat_rate,
        },
        "observation": {
            "ready_to_recalibrate": ready,
            "thresholds": {
                "min_sent":             MIN_SENT_BEFORE_RECAL,
                "min_blocked":          MIN_BLOCKED_BEFORE_RECAL,
                "min_72h_per_category": MIN_72H_PER_CATEGORY,
            },
            "current": {
                "sent":            events_sent,
                "blocked":         events_blocked,
                "72h_by_category": cat_72h,
            },
            "message": (
                "Calibration possible — données suffisantes."
                if ready else
                "Edge en accumulation — données insuffisantes pour conclure."
            ),
        },
    }


def format_edge_report_telegram(report: dict) -> str:
    """Format Telegram du rapport d'edge système (Phase Observation)."""
    lines = ["📡 **RAPPORT EDGE SYSTÈME — Phase Observation**\n"]

    obs = report.get("observation", {})
    ready = obs.get("ready_to_recalibrate", False)
    msg   = obs.get("message", "")
    lines.append(f"{'✅' if ready else '⏳'} _{msg}_\n")

    # Events
    ev = report.get("events", {})
    lines.append("**📊 Signaux**")
    lines.append(f"• Capturés : {ev.get('captured', 0)}")
    lines.append(f"• Envoyés  : {ev.get('sent', 0)}")
    bl = ev.get('blocked', 0)
    lines.append(f"• Bloqués  : {bl}")
    if bl:
        lines.append(f"  ↳ Bruit : {ev.get('blocked_noise',0)} | Cooldown : {ev.get('blocked_cooldown',0)} | Budget : {ev.get('blocked_budget',0)}")
    brate = ev.get("conviction_block_rate_pct")
    if brate is not None:
        lines.append(f"• Taux blocage conviction : {brate:.0f}%")
    lines.append("")

    # Outcomes
    out = report.get("outcomes", {})
    lines.append("**⏱ Outcomes validés**")
    lines.append(f"• En attente : {out.get('pending', 0)}")
    lines.append(f"• +4h  : {out.get('validated_4h', 0)}")
    lines.append(f"• +24h : {out.get('validated_24h', 0)}")
    lines.append(f"• +72h : {out.get('validated_72h', 0)}")
    lines.append("")

    # Scores
    sys_s = report.get("system_score", {})
    avg   = sys_s.get("avg_score")
    lines.append("**🎯 Score moyen système**")
    if avg is not None:
        lines.append(f"• **{avg}/100**")
    else:
        lines.append("• Insuffisant — N < 5 par indicateur")

    scores = sys_s.get("scores_by_indicator", {})
    if scores:
        for ind, sc in sorted(scores.items(), key=lambda x: (x[1] is None, -(x[1] or 0))):
            if sc is not None:
                bar = "🟩" * (sc // 20) + "⬜" * (5 - sc // 20)
                lines.append(f"  {bar} {ind}: {sc}")
            else:
                lines.append(f"  ⬜⬜⬜⬜⬜ {ind}: N insuffisant")
    lines.append("")

    # Winrate + EV
    ind_stats = report.get("indicator_stats", {})
    ev_by_ind = report.get("ev_by_indicator", {})
    if ind_stats:
        lines.append("**📈 Winrate 4h / EV par indicateur**")
        for ind, s in sorted(ind_stats.items()):
            wr4 = s.get("winrate_4h")
            ev4 = ev_by_ind.get(ind)
            wr4_str = f"{wr4:.0f}%" if wr4 is not None else "–"
            ev4_str = f"{ev4:+.2f}%" if ev4 is not None else "–"
            lines.append(f"• {ind}: WR={wr4_str} | EV={ev4_str} | n72h={s.get('n_72h',0)}")
        lines.append("")

    # Top 5 best
    best = report.get("top5_best", [])
    if best:
        lines.append("**🏆 Top 5 meilleurs signaux**")
        for i, s in enumerate(best, 1):
            avg4 = f"{s['avg_4h']:+.2f}%" if s.get("avg_4h") is not None else "–"
            lines.append(
                f"{i}. `{s['signal']}` — {s['winrate']:.0f}% hit "
                f"({s['n']}x) | +4h moy: {avg4}"
            )
        lines.append("")

    # Top 5 worst
    worst = report.get("top5_worst", [])
    if worst:
        lines.append("**⚠️ Top 5 pires signaux**")
        for i, s in enumerate(worst, 1):
            avg4 = f"{s['avg_4h']:+.2f}%" if s.get("avg_4h") is not None else "–"
            lines.append(
                f"{i}. `{s['signal']}` — {s['winrate']:.0f}% hit "
                f"({s['n']}x) | +4h moy: {avg4}"
            )
        lines.append("")

    # Feedback
    fb = report.get("feedback", {})
    total_fb = fb.get("total", 0)
    if total_fb > 0:
        sat = fb.get("satisfaction_rate_pct")
        lines.append("**👍 Satisfaction utilisateur**")
        lines.append(f"• Notes : {total_fb} | 👍 {fb.get('utile',0)} | 👎 {fb.get('inutile',0)}")
        if sat is not None:
            lines.append(f"• Taux satisfaction : {sat:.0f}%")
        lines.append("")

    # Thresholds
    cur = obs.get("current", {})
    thr = obs.get("thresholds", {})
    lines.append("**🔬 Seuils recalibration**")
    s_ok  = "✅" if cur.get("sent", 0)    >= thr.get("min_sent", 30)    else "⏳"
    bl_ok = "✅" if cur.get("blocked", 0) >= thr.get("min_blocked", 30) else "⏳"
    lines.append(f"{s_ok} Envoyés : {cur.get('sent',0)}/{thr.get('min_sent',30)}")
    lines.append(f"{bl_ok} Bloqués : {cur.get('blocked',0)}/{thr.get('min_blocked',30)}")
    min72 = thr.get("min_72h_per_category", 10)
    for ind, n72 in sorted(cur.get("72h_by_category", {}).items()):
        ok = "✅" if n72 >= min72 else "⏳"
        lines.append(f"{ok} {ind} +72h : {n72}/{min72}")

    lines.append("\n[📊 Dashboard](https://mamoscrypto.com/options)")
    return "\n".join(lines)
