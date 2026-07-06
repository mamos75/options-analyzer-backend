"""
FastAPI backend — Dashboard Options BTC Mamos Crypto.
Run: uvicorn main:app --reload --port 8000
"""

import asyncio
import json
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .deribit_client import DeribitClient, MarketSnapshot
from .gex import compute_gex, GEXProfile, MaxPainExpiry, audit_flip_variants, flip_scenario_comparison, filter_options_by_dte, DTE_NEAR_MAX
from .deribit_client import MarketSnapshot as _MarketSnapshot


def _near_snapshot(snap):
    """Snapshot filtré sur 0-14j pour signal opérationnel."""
    from dataclasses import replace as _dc_replace
    filtered = filter_options_by_dte(snap.options, dte_min=0, dte_max=DTE_NEAR_MAX)
    return _MarketSnapshot(btc_price=snap.btc_price, options=filtered, timestamp=snap.timestamp)
from .mopi import compute_mopi, MOPIScore, get_atm_iv
from .volatility_weather import compute_weather, WeatherReport
from .alerts import TelegramAlerter, AlertScheduler, log_feedback
from .conviction_score import MIN_SCORE_TO_SEND
from .dealer_pressure import compute_dealer_pressure, compute_dex_levels, dex_narrative, dex_subtitle
from .options_walls import compute_options_walls, compute_options_walls_horizons
from .gravity_map import compute_gravity_map, compute_gravity_map_horizons
from .squeeze_score import compute_squeeze_score
from .narrative_resolver import resolve_narrative, resolve_narrative_horizon
from .field_diagnostic import build_all_diagnostics, diag_gex_calibration
from .gex_activity_audit import compute_gex_activity_audit, compute_flip_activity_audit
from .gravity_activity_audit import compute_gravity_activity_audit
from .dashboard_accuracy import DashboardAccuracyTracker
from .backtest import run_backtest
from .market_decision_builder import build_market_decision
from .backtest_demo import run_demo_backtest
from .data_quality import compute_data_quality
from .event_store import get_event_store
from . import history_store
from .system_edge_report import compute_system_edge_report, format_edge_report_telegram
from .directional_bias import directional_bias_to_dict
from .stats_edge import compute_stats_edge
from .decision_arbiter import compute_decision as _compute_decision
from .regime_vexcex_engine import classify_regime_vexcex, VexCexInputs as _VexCexInputs
from .auth import router as auth_router, init_db as init_auth_db
from .regime_engine import build_regime_engine_output, regime_engine_to_dict
from .probability_engine import compute_probability_engine, probability_engine_to_dict
from . import binance_feed
from . import model_arena as _model_arena
from .mopi_divergence_engine import (
    _get_history as _mde_get_history,
    _select_best_window as _mde_select_best,
)
from . import spy_worker
from .spy_vix_engine import compute_vix_features, compute_vix_regime, vix_regime_to_dict
from .spy_stress_rebound import compute_stress_rebound
from .spy_us_mopi import compute_us_mopi
from .spy_regime_engine import compute_spy_regime, spy_regime_description, spy_regime_color
from . import spy_arena as _spy_arena
from . import multi_index_worker as _multi_idx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

deribit = DeribitClient()
alerter = TelegramAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
accuracy_tracker = DashboardAccuracyTracker()
iv_history_cache: list = []  # ATM IV brut — alimenté par _history_saver

_IV_HISTORY_MAX = 2160  # 90 jours × 24h

_HISTORY_INTERVAL = int(os.environ.get("HISTORY_INTERVAL_SECONDS", 1800))

_loop_tasks: dict = {}  # nom → asyncio.Task, pour status Phase 0
_arena = _model_arena.ModelArena()

# Cache calibration near GEX — mis à jour par _history_saver toutes les 30min
_gex_calibration_cache: dict = {
    "cap_value": 500_000_000,
    "cap_mode": "static/bootstrap",
    "saturation_rate_7d": None,
    "neutralization_rate_7d": None,
    "n_points": 0,
}

_GEX_CAL_ALERT_FILE = "/tmp/gex_near_calibration_alert.log"
_GEX_SATURATION_ALERT_THRESHOLD = 0.30   # > 30% → cap trop bas, signal saturé
_GEX_NEUTRALIZATION_ALERT_THRESHOLD = 0.70  # > 70% → cap trop élevé, signal collé à 50


def _log_gex_calibration_alert(kind: str, cal: dict) -> None:
    import time as _t
    msg = (
        f"[GEX CAL ALERT/{kind}] cap={cal['cap_value']/1e6:.0f}M "
        f"mode={cal['cap_mode']} "
        f"saturation={cal.get('saturation_rate_7d')} "
        f"neutralization={cal.get('neutralization_rate_7d')} "
        f"n={cal.get('n_points')}"
    )
    log.warning(msg)
    try:
        with open(_GEX_CAL_ALERT_FILE, "a") as f:
            f.write(f"{int(_t.time())} {msg}\n")
    except Exception:
        pass


def _parse_expiry_date(expiry: str):
    """Convertit une expiry Deribit (ex: 27JUN25) en date. Retourne date.max si invalide."""
    from datetime import date
    try:
        return datetime.strptime(expiry.upper(), "%d%b%y").date()
    except Exception:
        return datetime.max.date()


def _compute_vol_structure(snapshot: MarketSnapshot) -> List[dict]:
    """ATM IV (±5% spot) par expiry — structure de terme avec OI par expiry."""
    spot = snapshot.btc_price
    by_expiry: dict = {}
    oi_by_expiry: dict = {}
    for opt in snapshot.options:
        if abs(opt.strike - spot) / spot <= 0.05 and opt.iv > 0:
            by_expiry.setdefault(opt.expiry, []).append(opt.iv)
        oi_by_expiry[opt.expiry] = oi_by_expiry.get(opt.expiry, 0) + opt.oi

    today = datetime.now(timezone.utc).date()
    total_oi = sum(oi_by_expiry.values()) or 1
    result = []
    for expiry in sorted(by_expiry.keys(), key=_parse_expiry_date):
        ivs = by_expiry[expiry]
        exp_date = _parse_expiry_date(expiry)
        try:
            dte = max(1, (exp_date - today).days)
        except Exception:
            dte = 0
        expiry_oi = oi_by_expiry.get(expiry, 0)
        result.append({
            "expiry": expiry,
            "dte": dte,
            "iv": round(sum(ivs) / len(ivs), 2),
            "total_oi": round(expiry_oi, 1),
            "oi_pct": round(expiry_oi / total_oi * 100, 1),
        })
    return sorted(result, key=lambda x: x["dte"])


async def _history_saver():
    """Sauvegarde les métriques toutes les HISTORY_INTERVAL secondes.
    Alimente aussi iv_history_cache pour que IV Rank soit réel (pas fallback 50)."""
    await asyncio.sleep(30)  # laisse le WS s'établir
    while True:
        try:
            snapshot = await deribit.get_cached_snapshot()
            gex = compute_gex(snapshot)
            mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
            dp = compute_dealer_pressure(snapshot)

            # Alimenter l'historique IV pour que IV Rank soit calculé sur données réelles
            current_iv = get_atm_iv(snapshot, snapshot.btc_price)
            if current_iv > 0:
                iv_history_cache.append(current_iv)
                if len(iv_history_cache) > _IV_HISTORY_MAX:
                    iv_history_cache.pop(0)

            from .vex_cex import compute_vex_cex as _compute_vex_cex
            vc = _compute_vex_cex(snapshot)
            # V5 — classify VEX/CEX regime for journaling
            _vexcex_inputs_snap = _VexCexInputs(
                vex=vc.vex_total,
                cex=vc.cex_total,
                gex=gex.total_gex,
                dex=getattr(dp, 'net_delta_usd', getattr(dp, 'net_delta', 0.0)) or 0.0,
                spot=snapshot.btc_price,
                vex_direction=getattr(vc, 'vex_direction', None),
                cex_direction=getattr(vc, 'cex_direction', None),
                flip_level=gex.flip_level,
                flip_dist_pct=(
                    (snapshot.btc_price - gex.flip_level) / snapshot.btc_price * 100
                    if gex.flip_level else None
                ),
                regime_meca=gex.regime_meca,
                regime_source=gex.regime_source,
                gex_flip_incoherent=gex.gex_flip_incoherent,
            )
            _snap_regime = classify_regime_vexcex(_vexcex_inputs_snap)
            history_store.save_snapshot(
                mopi=mopi.score,
                gex=gex.total_gex,
                dex=dp.net_delta,
                iv_rank=mopi.iv_rank,
                pc_ratio=mopi.pc_ratio,
                max_pain=gex.max_pain,
                flip_level=gex.flip_level,
                btc_price=snapshot.btc_price,
                pc_ratio_near=mopi.pc_ratio_near,  # court terme ≤14j — source alertes
                gex_near=gex.gex_near,             # near-term gamma effectif (DTE ≤ 14)
                vex=vc.vex_total,
                cex=vc.cex_total,
                regime_id=_snap_regime.regime_id,
                verdict_arbiter=None,  # sera rempli à chaque /api/decision
            )

            # ── Probability Engine snapshot ──────────────────────────────────
            try:
                snap_near_h = _near_snapshot(snapshot)
                walls_h = compute_options_walls(snap_near_h)
                dex_levels_h = compute_dex_levels(snapshot)
                flip_audit_h = compute_flip_activity_audit(snapshot, gex.flip_level)
                flip_use_h = flip_audit_h.flip_use_in_signal if flip_audit_h is not None else True
                mp_near_h = gex.max_pain_profile.near if gex.max_pain_profile else None
                max_pain_strike_h = mp_near_h.strike if mp_near_h else gex.max_pain
                max_pain_dte_h = mp_near_h.dte if mp_near_h else 99
                gex_near_prev_h: Optional[float] = None
                try:
                    recent_h = history_store.get_last_n_snapshots(2)
                    if len(recent_h) >= 2 and recent_h[1].get("gex_near") is not None:
                        gex_near_prev_h = float(recent_h[1]["gex_near"])
                except Exception:
                    pass
                gex_momentum_h = None
                if gex_near_prev_h is not None:
                    delta = gex.gex_near - gex_near_prev_h
                    gex_momentum_h = "increasing" if delta > 0 else "decreasing" if delta < 0 else "stable"
                bf_h = binance_feed.get_cache()
                pe_output_h = compute_probability_engine(
                    spot=snapshot.btc_price,
                    gex_near=gex.gex_near,
                    flip_level=gex.flip_level,
                    flip_use_in_signal=flip_use_h,
                    dex_direction=dp.direction,
                    dex_actionable_btc=abs(dex_levels_h.actionable) if dex_levels_h else 0.0,
                    iv_rank=mopi.iv_rank,
                    pc_ratio_near=mopi.pc_ratio_near,
                    put_wall=walls_h.major_put_wall or 0.0,
                    call_wall=walls_h.major_call_wall or 0.0,
                    max_pain_strike=max_pain_strike_h,
                    max_pain_dte=max_pain_dte_h,
                    mopi_score=mopi.score,
                    gex_near_prev=gex_near_prev_h,
                    funding_rate=bf_h.get("funding_rate"),
                    futures_oi=bf_h.get("futures_oi"),
                    futures_oi_prev=bf_h.get("futures_oi_prev"),
                    spot_volume_24h=bf_h.get("spot_volume_24h"),
                    spot_volume_7d_avg=bf_h.get("spot_volume_7d_avg"),
                    spot_prev=None,
                    # V2 — Crash Regime Gate
                    gex_regime=gex.regime,
                    dex_score=(dp.pressure_pct + 100.0) / 2.0,
                )
                history_store.save_pe_snapshot(
                    output_dict=probability_engine_to_dict(pe_output_h),
                    gex_near=gex.gex_near,
                    dex_direction=dp.direction,
                    flip_level=gex.flip_level,
                    gex_momentum=gex_momentum_h,
                )
                log.info(
                    f"[pe_history] saved dominant={pe_output_h.dominant_scenario} "
                    f"prob={pe_output_h.dominant_probability:.1f}% "
                    f"conf={pe_output_h.dominant_confidence:.1f}% "
                    f"momentum={gex_momentum_h}"
                )
                # ── Model Arena — run all engines ────────────────────────────
                try:
                    _spot_h = snapshot.btc_price
                    # Divergence MOPI vs prix — feature partagée entre tous les moteurs
                    try:
                        _mde_snaps = _mde_get_history(hours=26)
                        _mde_type, _mde_str, _mde_feat = _mde_select_best(_mde_snaps)
                    except Exception:
                        _mde_type, _mde_str, _mde_feat = "none", 0.0, {}
                    _features_snap = {
                        "gex_near": gex.gex_near,
                        "dex_direction": dp.direction,
                        "iv_rank": mopi.iv_rank,
                        "pc_ratio_near": mopi.pc_ratio_near,
                        "mopi_score": mopi.score,
                        "flip_level": gex.flip_level,
                        "gex_regime": gex.regime,
                        "max_pain_strike": max_pain_strike_h,
                        "max_pain_dte": max_pain_dte_h,
                        "put_wall": walls_h.major_put_wall or 0.0,
                        "call_wall": walls_h.major_call_wall or 0.0,
                        "funding_rate": bf_h.get("funding_rate"),
                        "futures_oi": bf_h.get("futures_oi"),
                        "spot_volume_24h": bf_h.get("spot_volume_24h"),
                        "flip_distance_pct": (
                            (gex.flip_level - _spot_h) / _spot_h
                            if gex.flip_level and _spot_h else 0.0
                        ),
                        # MOPI divergence — signal dynamique MOPI vs BTC (fenêtre 4-24h)
                        "mopi_div_type_enc": (
                            1.0 if _mde_type == "bullish" else -1.0 if _mde_type == "bearish" else 0.0
                        ),
                        "mopi_div_strength": _mde_str,
                        "mopi_price_corr":   float(_mde_feat.get("mopi_price_correlation", 0.0) or 0.0),
                    }
                    _arena.run_all(_spot_h, pe_output_h, _features_snap)
                    log.info("[arena] predictions saved")
                except Exception as _e_arena:
                    log.error(f"[arena] run_all error: {_e_arena}")
            except Exception as e_pe:
                log.error(f"[pe_history] save error: {e_pe}")

            # Recalibration rolling 7j du cap near GEX
            cal = history_store.compute_dynamic_gex_cap()
            _gex_calibration_cache.update(cal)

            sat = cal.get("saturation_rate_7d")
            neu = cal.get("neutralization_rate_7d")
            if sat is not None and sat > _GEX_SATURATION_ALERT_THRESHOLD:
                _log_gex_calibration_alert("SATURATION", cal)
            elif neu is not None and neu > _GEX_NEUTRALIZATION_ALERT_THRESHOLD:
                _log_gex_calibration_alert("NEUTRALIZATION", cal)

            log.info(
                f"[history] saved MOPI={mopi.score:.1f} GEX={gex.total_gex/1e6:.1f}M "
                f"GEXnear={gex.gex_near/1e6:.1f}M cap={cal['cap_value']/1e6:.0f}M({cal['cap_mode']}) "
                f"sat={sat} DEX={dp.net_delta:.1f}BTC MaxPain={gex.max_pain:.0f} "
                f"IVRank={mopi.iv_rank:.1f} (history={len(iv_history_cache)})"
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[history] save error: {e}")
        await asyncio.sleep(_HISTORY_INTERVAL)


async def _executive_logger():
    """Phase 6 — Log du résumé exécutif toutes les heures + rapport dimanche 20h UTC."""
    await asyncio.sleep(60)  # laisse le WS s'établir
    last_weekly_report_day: Optional[int] = None
    while True:
        try:
            snapshot = await deribit.get_cached_snapshot()
            gex  = compute_gex(snapshot)
            mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
            dp   = compute_dealer_pressure(snapshot)
            _snap_near = _near_snapshot(snapshot)
            gmap = compute_gravity_map(_snap_near, compute_gex(_snap_near))
            spot = snapshot.btc_price
            score = mopi.score

            if score >= 75:
                bias_label, bias_color = "BULL FORT", "green"
            elif score >= 55:
                bias_label, bias_color = "Bull modéré", "green"
            elif score >= 45:
                bias_label, bias_color = "Neutre", "yellow"
            elif score >= 25:
                bias_label, bias_color = "Bear modéré", "red"
            else:
                bias_label, bias_color = "BEAR FORT", "red"

            bull, bear = 0, 0
            if score >= 55: bull += 2
            elif score >= 50: bull += 1
            elif score <= 45: bear += 2
            else: bear += 1
            if gex.regime == "AMPLIFICATEUR":
                if score >= 50: bull += 1
                else: bear += 1
            if dp.direction == "BULLISH_FLOWS": bull += 1
            elif dp.direction == "BEARISH_FLOWS": bear += 1
            if gmap.strongest_magnet > spot * 1.005: bull += 1
            elif gmap.strongest_magnet < spot * 0.995: bear += 1
            total = bull + bear
            alignment = 5 if total == 0 else max(4, min(10, round(3 + max(bull, bear) / total * 7)))

            # ── Contexte régime pour apprentissage conditionnel ──────────────
            _iv_rank_val = mopi.iv_rank or 50.0
            _gex_regime_mapped = {
                "AMPLIFICATEUR": "Negative_Gamma",
                "STABILISANT":   "Positive_Gamma",
                "NEUTRE":        "Neutral",
            }.get(gex.regime, gex.regime)
            _vol_regime = (
                "Vol_Expansion" if _iv_rank_val > 60
                else "Vol_Contraction" if _iv_rank_val < 40
                else "Neutral_Vol"
            )
            _dex_pressure = getattr(dp, "pressure_pct", 0.0) or 0.0
            _panic = (
                gex.gex_near < -5_000_000
                and _iv_rank_val > 70
                and dp.direction == "BEARISH_FLOWS"
            )
            _flip_dist = (
                round((gex.flip_level - spot) / spot * 100, 2)
                if gex.flip_level and spot else None
            )
            _mp = getattr(gex, "max_pain", None)
            _mp_dist = (
                round((_mp - spot) / spot * 100, 2)
                if _mp and spot else None
            )
            # ── Métadonnées Arena pour visibilité apprentissage (Phase 5) ──────
            _arena_leader: Optional[str] = None
            _arena_rank:   Optional[int] = None
            try:
                _a_stats  = _model_arena.get_arena_stats(days=30)
                _a_best   = _a_stats.get("best_model") or _a_stats.get("best_primary_model")
                _a_models = list(_a_stats.get("performance", {}).keys())
                _arena_leader = _a_best
                _arena_rank   = (_a_models.index(_a_best) + 1) if _a_best and _a_best in _a_models else None
            except Exception:
                pass

            accuracy_tracker.log_summary(
                bias=bias_label,
                bias_color=bias_color,
                alignment=alignment,
                target=gmap.strongest_magnet,
                invalidation=gex.flip_level,
                btc_price=spot,
                gex_regime=_gex_regime_mapped,
                gex_near=gex.gex_near,
                iv_rank=_iv_rank_val,
                vol_regime=_vol_regime,
                panic=_panic,
                dex_score=round((_dex_pressure + 100.0) / 2.0, 1),
                mopi_score=mopi.score,
                flip_distance_pct=_flip_dist,
                max_pain_distance_pct=_mp_dist,
                arena_leader=_arena_leader,
                arena_rank=_arena_rank,
                adaptive_weights_mode="observe",
                adaptive_weights_active=False,
            )

            # Rapport hebdo chaque dimanche à 20h UTC
            now = datetime.now(timezone.utc)
            if now.weekday() == 6 and now.hour == 20 and last_weekly_report_day != now.date().isoformat():
                report = accuracy_tracker.generate_accuracy_report(days=7)
                await alerter.send(f"📅 **RAPPORT DASHBOARD — DIMANCHE**\n\n{report}")
                last_weekly_report_day = now.date().isoformat()
                log.info("[accuracy] rapport hebdo envoyé")

        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[accuracy] executive logger error: {e}")
        await asyncio.sleep(3600)


async def _arena_evaluator():
    """Model Arena — évalue les outcomes des prédictions passées toutes les heures."""
    await asyncio.sleep(300)  # laisse le système démarrer
    last_weekly_reset: Optional[str] = None
    while True:
        try:
            snapshot = await deribit.get_cached_snapshot()
            _model_arena.evaluate_pending_outcomes(snapshot.btc_price)
            # Refresh clusters AME après chaque évaluation d'outcomes
            try:
                n_clusters = _model_arena.refresh_ame_clusters()
                if n_clusters > 0:
                    log.info(f"[ame] {n_clusters} clusters mis à jour")
            except Exception as _e_ame:
                log.warning(f"[ame] refresh_clusters error: {_e_ame}")
            # Réinitialise les deltas hebdomadaires chaque lundi
            now = datetime.now(timezone.utc)
            week_key = f"{now.year}-W{now.isocalendar()[1]}"
            if now.weekday() == 0 and last_weekly_reset != week_key:
                _model_arena.reset_weekly_deltas()
                last_weekly_reset = week_key
                log.info("[arena] weekly weight deltas reset")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[arena] evaluator error: {e}")
        await asyncio.sleep(3600)  # toutes les heures


async def _arena_performance_snapshotter():
    """Snapshot des performances arena toutes les 30 min dans arena_performance_history."""
    await asyncio.sleep(180)  # laisse le système démarrer
    while True:
        try:
            n = _model_arena.snapshot_arena_performance()
            log.info(f"[arena_snapshot] {n} lignes insérées")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[arena_snapshot] error: {e}")
        await asyncio.sleep(1800)  # toutes les 30 min


async def _daily_edge_reporter():
    """Phase Observation — rapport d'edge système chaque jour à 20h UTC."""
    await asyncio.sleep(120)  # laisse le système démarrer
    last_report_day: Optional[str] = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == 20 and last_report_day != now.date().isoformat():
                report = compute_system_edge_report(days=14)
                msg    = format_edge_report_telegram(report)
                await alerter.send(msg)
                last_report_day = now.date().isoformat()
                log.info("[edge_report] rapport quotidien envoyé")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[edge_report] daily reporter error: {e}")
        await asyncio.sleep(3600)


_SPY_ARENA_RECORD_INTERVAL = 1800  # record event + validate outcomes toutes les 30min


async def _spy_arena_loop() -> None:
    """Enregistre un snapshot SPY Arena toutes les 30min + valide les outcomes."""
    await asyncio.sleep(60)  # laisse le spy_worker se stabiliser
    while True:
        try:
            cache = spy_worker.get_cache()
            if not spy_worker.is_stale(7200) and cache.get("spy_price") is not None:
                _spy_arena.record_spy_event(cache)
                _spy_arena.validate_spy_outcomes(cache)
        except Exception as e:
            log.error(f"[spy_arena_loop] {e}")
        await asyncio.sleep(_SPY_ARENA_RECORD_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    history_store.init_db()
    _model_arena.init_arena_db()
    spy_worker.init_spy_db()
    _spy_arena.init_spy_arena_db(
        db_path=os.environ.get("HISTORY_DB_PATH", "/data/options_history.db")
    )
    init_auth_db()
    await deribit.connect()
    # Worker de fond : UN seul fetch Deribit toutes les 60s
    # Tous les endpoints et alertes lisent le cache — zéro appel direct
    refresh_task = asyncio.create_task(deribit.run_background_refresh(60))
    scheduler = AlertScheduler(alerter, deribit, iv_history_cache, _gex_calibration_cache)
    alert_task = asyncio.create_task(scheduler.run_forever())
    hist_task        = asyncio.create_task(_history_saver())
    exec_log_task    = asyncio.create_task(_executive_logger())
    accuracy_task    = asyncio.create_task(accuracy_tracker.run_validation_loop())
    edge_task        = asyncio.create_task(_daily_edge_reporter())
    event_task       = asyncio.create_task(get_event_store().run_outcome_loop())
    binance_task     = asyncio.create_task(binance_feed.run_worker())
    arena_task       = asyncio.create_task(_arena_evaluator())
    arena_snap_task  = asyncio.create_task(_arena_performance_snapshotter())
    spy_task         = asyncio.create_task(spy_worker.run_worker())
    spy_arena_task   = asyncio.create_task(_spy_arena_loop())
    _multi_idx.init_multi_index_db()
    multi_idx_task   = asyncio.create_task(_multi_idx.run_worker(spy_worker.get_cache))
    _loop_tasks.update({
        "history_saver":          hist_task,
        "event_validator":        event_task,
        "prediction_validator":   accuracy_task,
        "daily_reporter":         edge_task,
        "binance_feed":           binance_task,
        "arena_evaluator":        arena_task,
        "arena_perf_snapshotter": arena_snap_task,
        "spy_worker":             spy_task,
        "spy_arena_loop":         spy_arena_task,
        "multi_index_worker":     multi_idx_task,
    })
    yield
    refresh_task.cancel()
    alert_task.cancel()
    hist_task.cancel()
    exec_log_task.cancel()
    accuracy_task.cancel()
    edge_task.cancel()
    event_task.cancel()
    binance_task.cancel()
    arena_task.cancel()
    arena_snap_task.cancel()
    spy_task.cancel()
    spy_arena_task.cancel()
    multi_idx_task.cancel()
    await deribit.close()


app = FastAPI(title="Mamos Options Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://mamoscrypto.com", "http://localhost:3000", "http://138.68.80.156"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)
app.include_router(auth_router)


class DashboardResponse(BaseModel):
    btc_price: float
    gex_total: float
    gex_regime: str
    flip_level: Optional[float] = None
    flip_level_reason: Optional[str] = None
    flip_available: bool = False
    regime_state: Optional[str] = None
    regime_confidence: Optional[str] = None
    max_pain: float
    gamma_walls: list[float]
    mopi_score: float
    mopi_label: str
    mopi_emoji: str
    mopi_gex_component: float    # composante GEX dans le score MOPI (0-100)
    mopi_pc_component: float     # composante Put/Call dans le score MOPI (0-100)
    iv_rank: float
    pc_ratio: float
    mopi_squeeze_heuristic: float = 0.0  # B3: composant interne MOPI (heuristique)
    squeeze_prob: float = 0.0          # DEPRECATED — alias compat pour frontend embarque
    weather_state: str
    weather_emoji: str
    weather_description: str
    weather_color: str = "#eab308"  # B6: couleur hex du regime meteo (frontend n'a plus de map locale)
    timestamp: float
    data_stale: bool = False  # True = Deribit rate-limited, données du dernier snapshot valide
    mopi_delta_24h: Optional[float] = None
    max_pain_near: Optional[dict] = None          # near-term max pain structuré par expiry
    max_pain_institutional: Optional[dict] = None  # institutional max pain (highest OI expiry)
    max_pain_label: Optional[str] = None          # "Cible 06JUN (J-6) : $73,500" — affichage unifié
    pc_ratio_global: float = 0.0         # structure totale toutes expiries, non pondéré
    pc_ratio_near: float = 0.0           # ≤14 DTE, non pondéré — pression immédiate brute
    pc_ratio_mid: float = 0.0            # 15-45j, non pondéré
    pc_ratio_far: float = 0.0            # >45j, non pondéré
    pc_ratio_weighted: float = 0.0       # toutes expiries, 1/sqrt(DTE) — MOPI V2
    pc_ratio_institutional: float = 0.0  # backward compat — expiry OI max
    dominant_expiry: Optional[dict] = None  # expiry dominant dans pcr_weighted
    # V3-bis — regime mecanique spot/flip (source unique pour le frontend)
    regime_meca: str = "NEUTRE"              # STABILISANT | AMPLIFICATEUR | ZONE_DE_FLIP | NEUTRE
    regime_source: str = "gex_estime"        # "flip" | "gex_estime"
    gex_intensity: float = 0.0               # |gex_total| — magnitude separee du regime
    gex_flip_incoherent: bool = False        # True si signe GEX contredit le regime mecanique


def _mp_dict(mp: MaxPainExpiry) -> dict:
    return {
        "strike": mp.strike,
        "expiry": mp.expiry,
        "dte": mp.dte,
        "oi_total": round(mp.oi_total, 1),
    }




@app.get("/api/snapshot")
async def get_snapshot():
    """
    Endpoint agregatif B5 — retourne tous les payloads en un seul appel HTTP.

    Avantage : un seul fetch Deribit, coherence garantie (meme snapshot_ts pour tous).
    Structure : { snapshot_ts, spot, dashboard, walls, squeeze, dealer, narrative, bme_status }

    Le frontend peut utiliser cet endpoint au lieu des 8 appels individuels.
    Fallback cote frontend : si 404, repliement sur les 8 endpoints individuels.
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    snap_ts  = snapshot.timestamp
    spot     = snapshot.btc_price

    # ── Calculs partages (1x par snapshot) ────────────────────────────────────
    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    weather = compute_weather(gex, mopi)
    dp = compute_dealer_pressure(snapshot)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    walls_all = compute_options_walls_horizons(snapshot)
    walls_near = walls_all.get("near") or compute_options_walls(snapshot)

    # ── Gravity map ────────────────────────────────────────────────────────────
    try:
        from .gravity_map import compute_gravity_map
        gravity = compute_gravity_map(snapshot, gex)
        gravity_data = {
            "btc_price": spot,
            "timestamp": snap_ts,
            "zones": [
                {
                    "level": z.level, "strength": round(z.strength, 3),
                    "type": z.zone_type, "label": z.label, "active": z.active,
                }
                for z in (gravity.zones if gravity else [])
            ],
        }
    except Exception as e:
        log.warning(f"[snapshot] gravity error: {e}")
        gravity_data = {"error": str(e), "timestamp": snap_ts}

    # ── Narrative ──────────────────────────────────────────────────────────────
    try:
        from .narrative_resolver import resolve_narrative
        from .gex_activity_audit import compute_gex_activity_audit, compute_flip_activity_audit
        from .gravity_activity_audit import compute_gravity_activity_audit
        _audit  = compute_gex_activity_audit(snapshot)
        _flip   = compute_flip_activity_audit(snapshot, gex)
        _gaudit = compute_gravity_activity_audit(snapshot, gex)
        gmap_lv = compute_gravity_map(snapshot, gex)
        dex_lv  = compute_dex_levels(snapshot)
        narrative_out = resolve_narrative(
            mopi=mopi, gex=gex, dp=dp, gmap=gmap_lv,
            walls=walls_near, sq=sq, spot=spot,
            audit=_audit, dex_levels=dex_lv,
            gravity_audit=_gaudit, flip_audit=_flip,
        )
        narrative_data = narrative_out if isinstance(narrative_out, dict) else vars(narrative_out)
        narrative_data["timestamp"] = snap_ts
    except Exception as e:
        log.warning(f"[snapshot] narrative error: {e}")
        narrative_data = {"error": str(e), "timestamp": snap_ts}

    # ── BME status ─────────────────────────────────────────────────────────────
    try:
        from .btc_momentum_engine import get_bme
        bme = get_bme()
        bme_status_data = bme.get_status()
        bme_backtests = {}
        for hz in ["4h", "24h", "72h"]:
            if bme_status_data["horizons"][hz]["model_ready"]:
                bme_backtests[hz] = bme.backtest(hz, last_n=200)
        bme_data = {"status": bme_status_data, "backtests": bme_backtests}
    except Exception as e:
        log.warning(f"[snapshot] bme_status error: {e}")
        bme_data = {"error": str(e)}

    # ── Dashboard payload ──────────────────────────────────────────────────────
    hist_24h = history_store.get_history(1)
    mopi_delta_24h = round(mopi.score - hist_24h[0]["mopi"], 1) if hist_24h else None

    dashboard_data = {
        "btc_price":             spot,
        "gex_total":             gex.total_gex,
        "gex_regime":            gex.regime,
        "flip_level":            gex.flip_level,
        "mopi_score":            mopi.score,
        "mopi_label":            mopi.label,
        "mopi_emoji":            mopi.emoji,
        "iv_rank":               mopi.iv_rank,
        "mopi_squeeze_heuristic": mopi.mopi_squeeze_heuristic,
        "squeeze_prob":          mopi.squeeze_prob,  # DEPRECATED alias
        "weather_state":         weather.state,
        "weather_emoji":         weather.emoji,
        "weather_description":   weather.description,
        "weather_color":         weather.color,  # B6: couleur hex serveur
        "mopi_delta_24h":        mopi_delta_24h,
        "timestamp":             snap_ts,
    }

    # ── Dealer payload ─────────────────────────────────────────────────────────
    dex_lv_h = compute_dex_levels(snapshot)
    dealer_data = {
        "btc_price": spot,
        "direction": dp.direction,
        "pressure_pct": dp.pressure_pct,
        "structural": dex_lv_h.structural if dex_lv_h else None,
        "active": dex_lv_h.active if dex_lv_h else None,
        "actionable": dex_lv_h.actionable if dex_lv_h else None,
        "timestamp": snap_ts,
    }

    # ── Squeeze payload ────────────────────────────────────────────────────────
    squeeze_data = {
        "btc_price": spot,
        "score": sq.score,
        "label": sq.label,
        "emoji": sq.emoji,
        "direction_bias": sq.direction_bias,
        "timestamp": snap_ts,
    }

    # ── Walls payload ──────────────────────────────────────────────────────────
    walls_data = {
        **_serialize_walls_profile(walls_near, snap_ts),
        "horizons": {
            k: _serialize_walls_profile(v, snap_ts) for k, v in walls_all.items()
        },
    }

    return {
        "snapshot_ts":  snap_ts,
        "spot":         spot,
        "dashboard":    dashboard_data,
        "walls":        walls_data,
        "dealer":       dealer_data,
        "squeeze":      squeeze_data,
        "narrative":    narrative_data,
        "gravity":      gravity_data,
        "bme_status":   bme_data,
    }

@app.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    weather = compute_weather(gex, mopi)

    hist_24h = history_store.get_history(1)
    mopi_delta_24h = round(mopi.score - hist_24h[0]['mopi'], 1) if hist_24h else None

    return DashboardResponse(
        btc_price=snapshot.btc_price,
        gex_total=gex.total_gex,
        gex_regime=gex.regime,
        flip_level=gex.flip_level,
        flip_level_reason=gex.flip_level_reason,
        flip_available=gex.flip_available,
        regime_state=gex.regime_state,
        regime_confidence=gex.regime_confidence,
        max_pain=gex.max_pain,
        gamma_walls=sorted(gex.gamma_walls),
        mopi_score=mopi.score,
        mopi_label=mopi.label,
        mopi_emoji=mopi.emoji,
        mopi_gex_component=mopi.gex_component,
        mopi_pc_component=mopi.pc_ratio_component,
        iv_rank=mopi.iv_rank,
        pc_ratio=mopi.pc_ratio,
        mopi_squeeze_heuristic=mopi.mopi_squeeze_heuristic,
        squeeze_prob=mopi.squeeze_prob,  # DEPRECATED alias
        weather_state=weather.state,
        weather_emoji=weather.emoji,
        weather_description=weather.description,
        weather_color=weather.color,
        timestamp=snapshot.timestamp,
        data_stale=deribit.data_stale,
        mopi_delta_24h=mopi_delta_24h,
        max_pain_near=_mp_dict(gex.max_pain_profile.near) if gex.max_pain_profile else None,
        max_pain_institutional=_mp_dict(gex.max_pain_profile.institutional) if gex.max_pain_profile else None,
        max_pain_label=(
            f"Cible {gex.max_pain_profile.near.expiry} (J-{gex.max_pain_profile.near.dte}) : "
            f"${gex.max_pain_profile.near.strike:,.0f}"
            if gex.max_pain_profile else f"${gex.max_pain:,.0f}"
        ),
        pc_ratio_global=mopi.pc_ratio_global,
        pc_ratio_near=mopi.pc_ratio_near,
        pc_ratio_mid=mopi.pc_ratio_mid,
        pc_ratio_far=mopi.pc_ratio_far,
        pc_ratio_weighted=mopi.pc_ratio_weighted,
        pc_ratio_institutional=mopi.pc_ratio_institutional,
        dominant_expiry=mopi.dominant_expiry or None,
        regime_meca=gex.regime_meca,
        regime_source=gex.regime_source,
        gex_intensity=gex.gex_intensity,
        gex_flip_incoherent=gex.gex_flip_incoherent,
    )


@app.get("/api/gex_calibration")
async def get_gex_calibration():
    """Observabilité cap near GEX : cap actif, mode, saturation/neutralisation 7j."""
    snapshot = await deribit.get_cached_snapshot()
    gex = compute_gex(snapshot)
    cal = _gex_calibration_cache.copy()
    return {
        "gex_near": gex.gex_near,
        "near_gex_cap": cal["cap_value"],
        "cap_mode": cal["cap_mode"],
        "saturation_rate_7d": cal.get("saturation_rate_7d"),
        "neutralization_rate_7d": cal.get("neutralization_rate_7d"),
        "n_points_7d": cal.get("n_points", 0),
        "alert_saturation": (
            cal.get("saturation_rate_7d") is not None
            and cal["saturation_rate_7d"] > _GEX_SATURATION_ALERT_THRESHOLD
        ),
        "alert_neutralization": (
            cal.get("neutralization_rate_7d") is not None
            and cal["neutralization_rate_7d"] > _GEX_NEUTRALIZATION_ALERT_THRESHOLD
        ),
        "btc_price": snapshot.btc_price,
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/gex_by_strike")
async def get_gex_by_strike():
    snapshot = await deribit.get_cached_snapshot()
    gex = compute_gex(snapshot)
    return {
        "btc_price": snapshot.btc_price,
        "data": [
            {
                "strike": strike,
                "gex_net": val,
                "gex_call": gex.call_gex_by_strike.get(strike, 0),
                "gex_put": gex.put_gex_by_strike.get(strike, 0),
            }
            for strike, val in sorted(gex.gex_by_strike.items())
        ],
    }


@app.get("/api/flip_audit")
async def get_flip_audit():
    """Audit des 4 variantes du flip level GEX + qualité d'activité du flip actif."""
    snapshot = await deribit.get_cached_snapshot()
    gex = compute_gex(snapshot)
    spot = snapshot.btc_price
    variants = audit_flip_variants(gex.gex_by_strike, spot)

    result = {}
    for name, level in variants.items():
        dist_pct = (level - spot) / spot * 100 if spot > 0 else 0
        result[name] = {
            "level": round(level, 0),
            "distance_pct": round(dist_pct, 2),
            "direction": "sous le spot" if level < spot else "au-dessus du spot",
        }

    scenarios = flip_scenario_comparison()
    flip_activity = compute_flip_activity_audit(snapshot, gex.flip_level)

    return {
        "btc_price": spot,
        "gex_total": gex.total_gex,
        "regime": gex.regime,
        "flip_actif": round(gex.flip_level, 0) if gex.flip_level is not None else None,
        "variants": result,
        "avant_apres_5_snapshots": scenarios,
        "flip_activity_tag": flip_activity.flip_activity_tag,
        "flip_signal_quality": flip_activity.flip_signal_quality,
        "flip_signal_label": flip_activity.flip_signal_label,
        "flip_use_in_signal": flip_activity.flip_use_in_signal,
        "flip_activity_context": (
            f"💀 Flip dormant" if flip_activity.flip_activity_tag == "DORMANT" else
            f"🪨 Flip structurel" if flip_activity.flip_activity_tag == "STRUCTURAL" else
            f"⚡ Flip actif" if flip_activity.flip_activity_tag == "ACTIVE" else
            f"🔥 Flip actionnable"
        ),
        "flip_window": {
            "gex_total": flip_activity.window_gex_total,
            "dormant_pct": flip_activity.window_dormant_pct,
            "structural_pct": flip_activity.window_structural_pct,
            "active_pct": flip_activity.window_active_pct,
            "actionable_pct": flip_activity.window_actionable_pct,
        },
        "flip_top_contributors": flip_activity.top_contributors,
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/mopi_free")
async def get_mopi_free():
    """Endpoint public — score MOPI seul (pas les détails VIP)."""
    snapshot = await deribit.get_cached_snapshot()
    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    weather = compute_weather(gex, mopi)
    return {
        "mopi_score": mopi.score,
        "mopi_emoji": mopi.emoji,
        "weather_emoji": weather.emoji,
        "weather_state": weather.state,
        "max_pain": gex.max_pain,
    }


@app.get("/api/max_pain_by_expiry")
async def get_max_pain_by_expiry():
    """Max Pain structuré par expiry — near-term (actionnable) et institutional (structure longue)."""
    snapshot = await deribit.get_cached_snapshot()
    gex = compute_gex(snapshot)
    mp = gex.max_pain_profile
    if not mp:
        raise HTTPException(503, "No valid expiry data available")
    return {
        "max_pain_near": _mp_dict(mp.near),
        "max_pain_institutional": _mp_dict(mp.institutional),
        "btc_price": snapshot.btc_price,
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/dealer_pressure")
async def get_dealer_pressure():
    snapshot = await deribit.get_cached_snapshot()
    dp = compute_dealer_pressure(snapshot)
    dex = compute_dex_levels(snapshot)
    return {
        "btc_price": snapshot.btc_price,
        "net_delta": dp.net_delta,
        "net_delta_usd": dp.net_delta_usd,
        "direction": dp.direction,
        "intensity": dp.intensity,
        "pressure_pct": dp.pressure_pct,
        "gauge_color": dp.gauge_color,
        "flux_conditionnel": dp.flux_conditionnel,
        "direction_risque_trader": dp.direction_risque_trader,
        "exposition_nette_btc": dp.exposition_nette_btc,
        "delta_by_strike": dp.delta_by_strike,
        "dex_narrative": dex_narrative(dp),
        "dex_subtitle": dex_subtitle(dp),
        # DEX 3 niveaux — Structural / Active / Actionable
        "dex_structural": dex.structural,
        "dex_structural_usd": dex.structural_usd,
        "dex_active": dex.active,
        "dex_active_usd": dex.active_usd,
        "dex_actionable": dex.actionable,
        "dex_actionable_usd": dex.actionable_usd,
        "dex_low_oi_anomaly_count": dex.low_oi_anomaly_count,
        "dex_profile": dex.dex_profile,
        "dex_active_pct": dex.dex_active_pct,
        "dex_actionable_pct": dex.dex_actionable_pct,
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/vex_cex")
async def get_vex_cex():
    """VEX (Vanna Exposure) et CEX (Charm Exposure) des dealers."""
    from .vex_cex import compute_vex_cex
    snapshot = await deribit.get_cached_snapshot()
    profile = compute_vex_cex(snapshot)
    return {
        "vex_total":               profile.vex_total,
        "cex_total":               profile.cex_total,
        "vex_total_fmt":           profile.vex_total_fmt,
        "cex_total_fmt":           profile.cex_total_fmt,
        "vex_direction":           profile.vex_direction,
        "cex_direction":           profile.cex_direction,
        "vex_interpretation":      profile.vex_interpretation,
        "cex_interpretation":      profile.cex_interpretation,
        "vex_by_strike":           profile.vex_by_strike,
        "cex_by_strike":           profile.cex_by_strike,
        "gamma_flip":              profile.gamma_flip,
        "gamma_flip_dist_pct":     profile.gamma_flip_dist_pct,
        "gamma_flip_side":         profile.gamma_flip_side,
        "gamma_flip_regime":       profile.gamma_flip_regime,
        "gamma_flip_interpretation": profile.gamma_flip_interpretation,
        "btc_price":               profile.btc_price,
        "timestamp":               profile.timestamp,
    }


@app.get("/api/vex_cex_history")
async def get_vex_cex_history(period: str = "7d"):
    """Historique VEX/CEX depuis la base."""
    days = {"7d": 7, "14d": 14, "30d": 30}.get(period, 7)
    rows = history_store.get_vex_cex_history(days)
    return {
        "period":   period,
        "n_points": len(rows),
        "points":   rows,
    }


def _serialize_walls_profile(profile, timestamp: float) -> dict:
    def _wall_list(walls):
        return [
            {
                "strike": w.strike,
                "type": w.wall_type,
                "side": w.side,
                "total_oi": w.total_oi,
                "call_oi": w.call_oi,
                "put_oi": w.put_oi,
                "notional_usd": w.notional_usd,
                "oi": w.total_oi,
                "volume_24h": w.volume_24h,
                "structural_score": w.structural_score,
                "active_score": w.active_score,
                "actionable_score": w.actionable_score,
                "dte_nearest_active": w.dte_nearest_active,
                "tag": w.tag,
            }
            for w in walls
        ]
    return {
        "btc_price": profile.btc_price,
        "major_call_wall": profile.major_call_wall,
        "major_put_wall": profile.major_put_wall,
        "walls": _wall_list(profile.walls),
        "timestamp": timestamp,
    }


@app.get("/api/options_walls")
async def get_options_walls():
    snapshot = await deribit.get_cached_snapshot()
    horizons = compute_options_walls_horizons(snapshot)
    near = horizons["near"]
    return {
        **_serialize_walls_profile(near, snapshot.timestamp),
        "horizons": {
            "near": _serialize_walls_profile(horizons["near"], snapshot.timestamp),
            "monthly": _serialize_walls_profile(horizons["monthly"], snapshot.timestamp),
            "global": _serialize_walls_profile(horizons["global"], snapshot.timestamp),
        },
    }


@app.get("/api/oi_heatmap")
async def get_oi_heatmap():
    snapshot = await deribit.get_cached_snapshot()
    spot = snapshot.btc_price

    # Grouper par (strike, expiry) pour la heatmap
    grid: dict = {}
    for opt in snapshot.options:
        key = (opt.strike, opt.expiry)
        if key not in grid:
            grid[key] = {"call_oi": 0.0, "put_oi": 0.0}
        if opt.option_type == "call":
            grid[key]["call_oi"] += opt.oi
        else:
            grid[key]["put_oi"] += opt.oi

    strikes = sorted({k[0] for k in grid})
    # Tri chronologique par date (pas alphabétique — "1AUG25" < "30MAY25" en ASCII)
    expirations = sorted({k[1] for k in grid}, key=_parse_expiry_date)

    # OI total par expiry pour afficher l'importance relative sur la heatmap
    oi_by_expiry: dict = {}
    for (strike, expiry), oi in grid.items():
        oi_by_expiry[expiry] = oi_by_expiry.get(expiry, 0) + oi["call_oi"] + oi["put_oi"]
    total_oi = sum(oi_by_expiry.values()) or 1

    # Format [strikeIdx, expiryIdx, total_oi] pour ECharts
    heatmap = []
    for (strike, expiry), oi in grid.items():
        total = round(oi["call_oi"] + oi["put_oi"], 2)
        if total > 0:
            heatmap.append([strikes.index(strike), expirations.index(expiry), total])

    return {
        "btc_price": spot,
        "strikes": strikes,
        "expirations": expirations,
        "oi_by_expiry": {e: round(v, 1) for e, v in oi_by_expiry.items()},
        "oi_pct_by_expiry": {e: round(v / total_oi * 100, 1) for e, v in oi_by_expiry.items()},
        "heatmap": heatmap,
        "timestamp": snapshot.timestamp,
    }


def _serialize_gravity_map(gmap) -> dict:
    return {
        "btc_price": gmap.btc_price,
        "strongest_magnet": gmap.strongest_magnet,
        "next_explosive": gmap.next_explosive,
        "gravity_score": gmap.gravity_score,
        "gravity_global_label": gmap.gravity_global_label,
        "asymmetric_risk": gmap.asymmetric_risk,
        "narrative": gmap.narrative,
        "timestamp": gmap.timestamp,
        "zones": [
            {
                "price_low": z.price_low,
                "price_high": z.price_high,
                "center": z.center,
                "zone_type": z.zone_type,
                "strength": z.strength,
                "label": z.label,
                "color": z.color,
                "oi_usd": z.oi_usd,
                "gex": z.gex,
                "explosive_bias": z.explosive_bias,
                "explosive_score_down": z.explosive_score_down,
                "explosive_score_up": z.explosive_score_up,
            }
            for z in gmap.zones
        ],
    }


@app.get("/api/gravity_map")
async def get_gravity_map():
    snapshot = await deribit.get_cached_snapshot()
    horizons = compute_gravity_map_horizons(snapshot)
    near = horizons["near"]
    return {
        **_serialize_gravity_map(near),
        "horizons": {
            "near": _serialize_gravity_map(horizons["near"]),
            "monthly": _serialize_gravity_map(horizons["monthly"]),
            "global": _serialize_gravity_map(horizons["global"]),
        },
    }


@app.get("/api/squeeze_score")
async def get_squeeze_score():
    snapshot = await deribit.get_cached_snapshot()
    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    return {
        "btc_price": snapshot.btc_price,
        "score": sq.score,
        "label": sq.label,
        "emoji": sq.emoji,
        "direction_bias": sq.direction_bias,
        "trigger_zone": sq.trigger_zone,
        "dominant_signal": sq.dominant_signal,
        "global_risk_label": sq.global_risk_label,
        "local_risk_label": sq.local_risk_label,
        "local_risk_level": sq.local_risk_level,
        "signals": [
            {
                "name": sig.name,
                "score": sig.score,
                "weight": sig.weight,
                "description": sig.description,
            }
            for sig in sq.signals
        ],
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/narrative")
async def get_narrative():
    """Narrative Resolver — cohérence globale du dashboard.
    Produit un scénario unique, le risque principal, les niveaux clés et la phrase de synthèse.
    Détecte et résout les contradictions entre widgets."""
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    dex_levels = compute_dex_levels(snapshot)
    _snap_near = _near_snapshot(snapshot)
    _gex_near = compute_gex(_snap_near)
    gmap = compute_gravity_map(_snap_near, _gex_near)
    walls = compute_options_walls(_snap_near)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    audit = compute_gex_activity_audit(snapshot)
    gravity_audit = compute_gravity_activity_audit(snapshot)
    flip_audit = compute_flip_activity_audit(snapshot, gex.flip_level)

    cal_diag = diag_gex_calibration(_gex_calibration_cache)
    narrative = resolve_narrative(
        mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
        audit=audit, dex_levels=dex_levels, gravity_audit=gravity_audit,
        flip_audit=flip_audit,
        calibration_status=cal_diag.status,
        calibration_reason_code=cal_diag.reason_code,
    )

    return {
        "btc_price": snapshot.btc_price,
        "scenario_principal": narrative.scenario_principal,
        "risque_principal": narrative.risque_principal,
        "niveau_haut": narrative.niveau_haut,
        "niveau_haut_label": narrative.niveau_haut_label,
        "niveau_bas": narrative.niveau_bas,
        "niveau_bas_label": narrative.niveau_bas_label,
        "invalidation": narrative.invalidation,
        "phrase_synthese": narrative.phrase_synthese,
        "banner_message": narrative.banner_message,
        "range_mode": narrative.range_mode,
        "asymmetric_side": narrative.asymmetric_side,
        "max_pain_display": narrative.max_pain_display,
        "dex_coherent": narrative.dex_coherent,
        "contradictions": narrative.contradictions,
        "gex_activity_label": narrative.gex_activity_label,
        "gex_activity_context": narrative.gex_activity_context,
        "gex_use_in_signal": narrative.gex_use_in_signal,
        "dex_activity_label": narrative.dex_activity_label,
        "dex_activity_context": narrative.dex_activity_context,
        "dex_use_in_signal": narrative.dex_use_in_signal,
        "gravity_target": narrative.gravity_target,
        "gravity_zone": narrative.gravity_zone,
        "gravity_tag": narrative.gravity_tag,
        "flip_level": gex.flip_level,
        "flip_level_reason": gex.flip_level_reason,
        "flip_available": gex.flip_available,
        "regime_state": gex.regime_state,
        "regime_confidence": gex.regime_confidence,
        "flip_activity_tag": narrative.flip_activity_tag,
        "flip_signal_quality": narrative.flip_signal_quality,
        "flip_use_in_signal": narrative.flip_use_in_signal,
        "flip_activity_context": narrative.flip_activity_context,
        "flip_top_contributors": narrative.flip_top_contributors,
        "calibration_status": cal_diag.status,
        "calibration_reason_code": cal_diag.reason_code,
        "timestamp": snapshot.timestamp,
        "data_stale": deribit.data_stale,
        "directional_bias": directional_bias_to_dict(narrative.directional_bias) if narrative.directional_bias else None,
        # Point 8 — Risk Matrix
        "risk_matrix": narrative.risk_matrix,
    }


@app.get("/api/decision")
async def get_decision():
    """Decision Arbiter — moteur supérieur qui tranche entre tous les signaux.
    Produit UNE seule conclusion actionnelle avec signaux utilisés et exclus."""
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    dex_levels = compute_dex_levels(snapshot)
    _snap_near = _near_snapshot(snapshot)
    _gex_near = compute_gex(_snap_near)
    gmap = compute_gravity_map(_snap_near, _gex_near)
    walls = compute_options_walls(_snap_near)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    audit = compute_gex_activity_audit(snapshot)
    gravity_audit = compute_gravity_activity_audit(snapshot)
    flip_audit = compute_flip_activity_audit(snapshot, gex.flip_level)

    cal_diag = diag_gex_calibration(_gex_calibration_cache)
    narrative = resolve_narrative(
        mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
        audit=audit, dex_levels=dex_levels, gravity_audit=gravity_audit,
        flip_audit=flip_audit,
        calibration_status=cal_diag.status,
        calibration_reason_code=cal_diag.reason_code,
    )

    narrative_dict = {
        "contradictions": narrative.contradictions,
        "data_stale": deribit.data_stale,
        "range_mode": narrative.range_mode,
        "asymmetric_side": narrative.asymmetric_side,
        "gex_regime": gex.regime,
        "dex_direction": dp.direction,
    }

    try:
        arena_health = _model_arena.get_arena_health()
    except Exception:
        arena_health = None

    mopi_n_outcomes = 0
    if arena_health:
        lb = arena_health.get("leaderboard_detail", {})
        mde_detail = lb.get(_model_arena._MDE_NAME, {})
        mopi_n_outcomes = mde_detail.get("live_outcomes", 0) or 0

    # V5 — classify VEX/CEX regime for decision
    try:
        from .vex_cex import compute_vex_cex as _cvc_dec
        _vc_dec = _cvc_dec(snapshot)
    except Exception:
        _vc_dec = None
    _vexcex_inputs_dec = _VexCexInputs(
        vex=getattr(_vc_dec, 'vex_total', 0.0) or 0.0,
        cex=getattr(_vc_dec, 'cex_total', 0.0) or 0.0,
        gex=gex.total_gex,
        dex=getattr(dp, 'net_delta_usd', getattr(dp, 'net_delta', 0.0)) or 0.0,
        spot=snapshot.btc_price,
        vex_direction=getattr(_vc_dec, 'vex_direction', None),
        cex_direction=getattr(_vc_dec, 'cex_direction', None),
        flip_level=gex.flip_level,
        flip_dist_pct=(
            (snapshot.btc_price - gex.flip_level) / snapshot.btc_price * 100
            if gex.flip_level else None
        ),
        regime_meca=gex.regime_meca,
        regime_source=gex.regime_source,
        gex_flip_incoherent=gex.gex_flip_incoherent,
    )
    _vexcex_regime_dec = classify_regime_vexcex(_vexcex_inputs_dec)

    decision = _compute_decision(
        narrative_data=narrative_dict,
        arena_data=arena_health,
        health_data={"live_outcomes": arena_health.get("live_outcomes", 0)} if arena_health else None,
        mopi_score=mopi.score,
        mopi_n_outcomes=mopi_n_outcomes,
        flip_use_in_signal=narrative.flip_use_in_signal,
        gex_use_in_signal=narrative.gex_use_in_signal,
        dex_use_in_signal=narrative.dex_use_in_signal,
        vexcex_regime_id=_vexcex_regime_dec.regime_id,
        vexcex_phase=_vexcex_regime_dec.phase,
        vexcex_urgency=_vexcex_regime_dec.urgency,
        vexcex_label=_vexcex_regime_dec.label,
    )

    return {
        "verdict": decision.verdict,
        "confidence": decision.confidence,
        "confidence_pct": decision.confidence_pct,
        "phrase": decision.phrase,
        "signals_used": decision.signals_used,
        "signals_ignored": decision.signals_ignored,
        "contradictions": decision.contradictions,
        "data_quality": decision.data_quality,
        "arena_status": decision.arena_status,
        "arena_leader": decision.arena_leader,
        "arena_leader_wr": decision.arena_leader_wr,
        "arena_leader_ev": decision.arena_leader_ev,
        "generated_at": decision.generated_at,
        "btc_price": snapshot.btc_price,
        "system_status": decision.system_status,
        "vexcex_regime_id": decision.vexcex_regime_id,
        "vexcex_phase": decision.vexcex_phase,
        "vexcex_urgency": decision.vexcex_urgency,
        "vexcex_label": decision.vexcex_label,
        "vexcex_contribution": decision.vexcex_contribution,
    }


@app.get("/api/regime_engine")
async def get_regime_engine():
    """Mamos Options Regime Engine — Moteur institutionnel explicable.

    Retourne les 10 champs requis :
    1. regime_principal          — STABILISANT / AMPLIFICATEUR / NEUTRE
    2. risque_dominant           — menace principale
    3. direction_mecanique       — direction si edge backtest suffisant (winrate > 52%, N ≥ 30)
    4. probabilites_par_horizon  — probabilité conditionnelle 4h / 24h / 72h
    5. strategie_recommandee     — action actionnable
    6. a_eviter                  — comportement à proscrire
    7. niveau_invalidation       — prix d'invalidation mécanique
    8. raisonnement_explicable   — forces actives + forces vetoed
    9. qualite_donnees           — calibration, stale, contradictions
    10. confiance_statistique    — grade, winrate, EV, p-value bootstrap
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    dex_levels = compute_dex_levels(snapshot)
    _snap_near = _near_snapshot(snapshot)
    _gex_near = compute_gex(_snap_near)
    gmap = compute_gravity_map(_snap_near, _gex_near)
    walls = compute_options_walls(_snap_near)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    audit = compute_gex_activity_audit(snapshot)
    gravity_audit = compute_gravity_activity_audit(snapshot)
    flip_audit = compute_flip_activity_audit(snapshot, gex.flip_level)

    cal_diag = diag_gex_calibration(_gex_calibration_cache)
    narrative = resolve_narrative(
        mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
        audit=audit, dex_levels=dex_levels, gravity_audit=gravity_audit,
        flip_audit=flip_audit,
        calibration_status=cal_diag.status,
        calibration_reason_code=cal_diag.reason_code,
    )

    # Backtest (non-bloquant : si DB vide, on retourne quand même le regime engine)
    try:
        backtest_result = run_backtest(days=90)
    except Exception:
        backtest_result = None

    engine_output = build_regime_engine_output(
        narrative=narrative,
        gex=gex,
        mopi=mopi,
        dp=dp,
        spot=snapshot.btc_price,
        backtest_result=backtest_result,
        data_stale=deribit.data_stale,
        calibration_status=cal_diag.status,
    )

    result = regime_engine_to_dict(engine_output)
    result["btc_price"] = snapshot.btc_price
    result["timestamp"] = snapshot.timestamp
    return result


def _mdl_fallback(snapshot, data_stale: bool, error: str) -> dict:
    """Réponse propre retournée quand les calculs MDL échouent partiellement."""
    return {
        "btc_price": snapshot.btc_price,
        "directional": {
            "is_directional": False, "label": "Données insuffisantes", "score": 0,
            "color": "yellow", "confidence": "—", "confluence": 0,
            "reason": "Calcul interrompu — réessayer dans quelques secondes",
        },
        "dealer_regime": {"mode": "NEUTRE", "label": "Indéterminé", "color": "yellow", "phrase": "Données insuffisantes"},
        "dominant_risk": {"type": "none", "label": "Non calculé", "color": "yellow", "phrase": "—", "squeeze_score": 0},
        "key_levels": [],
        "confirmation": {"price": None, "label": "—", "phrase": "Données insuffisantes"},
        "invalidation": {"price": None, "label": "—", "phrase": "Données insuffisantes"},
        "watch_message": "Erreur de calcul — actualiser la page",
        "timestamp": snapshot.timestamp,
        "data_stale": data_stale,
        "confidence": "indisponible",
        "warnings": [f"Erreur de calcul interne : {error}"],
        "source_status": {"compute": "error"},
    }


@app.get("/api/market_decision")
async def get_market_decision():
    """Market Decision Layer — 7 réponses claires pour le particulier avancé.

    1. Directionnel ou non ?
    2. Dealers amortisseurs ou amplificateurs ?
    3. Risque dominant (squeeze / flush / max_pain_magnet / range / directional_trap)
    4. 2 niveaux BTC les plus importants maintenant
    5. Niveau de confirmation du scénario
    6. Niveau d'invalidation
    7. Message de surveillance

    Champs garantis : warnings[], source_status{}, confidence, data_stale.
    Jamais de 500 si une source est partielle ou absente.
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    try:
        gex = compute_gex(snapshot)
        mopi = compute_mopi(
            snapshot, gex, iv_history_cache,
            gex_near_cap=_gex_calibration_cache["cap_value"],
            cap_mode=_gex_calibration_cache["cap_mode"],
            saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
        )
        dp = compute_dealer_pressure(snapshot)
        dex_levels = compute_dex_levels(snapshot)
        snap_near = _near_snapshot(snapshot)
        gex_near_profile = compute_gex(snap_near)
        gmap = compute_gravity_map(snap_near, gex_near_profile)
        walls = compute_options_walls(snap_near)
        sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
        audit = compute_gex_activity_audit(snapshot)
        gravity_audit = compute_gravity_activity_audit(snapshot)
        flip_audit_mdl = compute_flip_activity_audit(snapshot, gex.flip_level)
        cal_diag = diag_gex_calibration(_gex_calibration_cache)

        narrative = resolve_narrative(
            mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
            audit=audit, dex_levels=dex_levels, gravity_audit=gravity_audit,
            flip_audit=flip_audit_mdl,
            calibration_status=cal_diag.status,
            calibration_reason_code=cal_diag.reason_code,
        )
    except Exception as e:
        log.error("market_decision: computation error: %s", e)
        return _mdl_fallback(snapshot, deribit.data_stale, str(e))

    result = build_market_decision(narrative, gex, sq, snapshot.btc_price, deribit.data_stale)
    result["timestamp"] = snapshot.timestamp
    result["data_stale"] = deribit.data_stale
    return result


@app.get("/api/probability_engine")
async def get_probability_engine():
    """Probability Engine Phase A — Moteur probabiliste par règles expertes pondérées.

    Produit pour chaque horizon (4h / 24h / 72h) :
      - probabilité directionnelle BEAR et BULL [5%-95%]
      - confiance SÉPARÉE de la probabilité (qualité du signal)
      - détail de chaque règle (poids, pts_appliqués, qualité données, condition observée)
      - top contributeurs
      - signal_label : "Baisse 24h : 64%  |  Confiance : 52%"

    Règle fondamentale :
      Probabilité = scénario dominant
      Confiance   = qualité du signal (data coverage + consensus)

    Seuils confiance :
      < 40%    → EDGE INSUFFISANT
      40-60%   → SIGNAL FAIBLE / À SURVEILLER
      60-75%   → SIGNAL VALIDE
      ≥ 75%    → SIGNAL FORT
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    dex_levels = compute_dex_levels(snapshot)
    snap_near = _near_snapshot(snapshot)
    walls = compute_options_walls(snap_near)
    flip_audit_pe = compute_flip_activity_audit(snapshot, gex.flip_level)

    spot = snapshot.btc_price
    flip_use = flip_audit_pe.flip_use_in_signal if flip_audit_pe is not None else True

    # Max Pain near-term
    mp_near = gex.max_pain_profile.near if gex.max_pain_profile else None
    max_pain_strike = mp_near.strike if mp_near else gex.max_pain
    max_pain_dte = mp_near.dte if mp_near else 99

    # GEX near précédent — pour détecter le momentum
    gex_near_prev: Optional[float] = None
    try:
        recent = history_store.get_last_n_snapshots(2)
        if len(recent) >= 2:
            prev_gex_near = recent[1].get("gex_near")
            if prev_gex_near is not None:
                gex_near_prev = float(prev_gex_near)
    except Exception:
        pass

    # Accuracy scores par module pour reliability factors (point 5)
    _module_accuracy: dict = {}
    try:
        from .indicator_accuracy import compute_indicator_accuracy
        _acc = compute_indicator_accuracy(days=14)
        _module_accuracy = _acc.get("scores", {}) or {}
    except Exception:
        pass

    bf = binance_feed.get_cache()
    output = compute_probability_engine(
        spot=spot,
        gex_near=gex.gex_near,
        flip_level=gex.flip_level,
        flip_use_in_signal=flip_use,
        dex_direction=dp.direction,
        dex_actionable_btc=abs(dex_levels.actionable) if dex_levels else 0.0,
        iv_rank=mopi.iv_rank,
        pc_ratio_near=mopi.pc_ratio_near,
        put_wall=walls.major_put_wall or 0.0,
        call_wall=walls.major_call_wall or 0.0,
        max_pain_strike=max_pain_strike,
        max_pain_dte=max_pain_dte,
        mopi_score=mopi.score,
        gex_near_prev=gex_near_prev,
        funding_rate=bf.get("funding_rate"),
        futures_oi=bf.get("futures_oi"),
        futures_oi_prev=bf.get("futures_oi_prev"),
        spot_volume_24h=bf.get("spot_volume_24h"),
        spot_volume_7d_avg=bf.get("spot_volume_7d_avg"),
        spot_prev=None,  # non disponible en temps réel — utilisé uniquement en historique
        # V2 — Crash Regime Gate
        gex_regime=gex.regime,
        dex_score=(dp.pressure_pct + 100.0) / 2.0,
        # V2 — Module Reliability (point 5)
        module_accuracy_scores=_module_accuracy if _module_accuracy else None,
    )

    result = probability_engine_to_dict(output)
    result["data_stale"] = deribit.data_stale
    result["binance_feed_stale"] = binance_feed.is_stale()
    return result


@app.get("/api/pe_history")
async def get_pe_history(hours: int = Query(default=24, ge=1, le=168)):
    """Historique des snapshots Probability Engine (1h–168h).

    Retourne les colonnes indexées (sans le JSON complet) pour le graphique frontend.
    Inclut le nombre total de snapshots collectés.
    """
    rows = history_store.get_pe_history(hours=hours)
    return {
        "hours": hours,
        "n_snapshots": history_store.get_pe_snapshot_count(),
        "snapshots": rows,
    }


@app.get("/api/directional_bias")
async def get_directional_bias():
    """Directional Bias Score — synthèse directionnelle -100/+100.
    Agrège MOPI + DEX Actionable + PCR Weighted + GEX Asymétrie.
    Retourne score, cible probable, stop logique mécanique.
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    dex = compute_dex_levels(snapshot)
    gmap = compute_gravity_map(snapshot, gex)
    walls = compute_options_walls(snapshot)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    audit = compute_gex_activity_audit(snapshot)
    gravity_audit = compute_gravity_activity_audit(snapshot)
    flip_audit = compute_flip_activity_audit(snapshot, gex.flip_level)
    cal_diag = diag_gex_calibration(_gex_calibration_cache)

    narrative = resolve_narrative(
        mopi, gex, dp, gmap, walls, sq,
        spot=snapshot.btc_price,
        audit=audit,
        dex_levels=dex,
        gravity_audit=gravity_audit,
        flip_audit=flip_audit,
        calibration_status=cal_diag.status,
        calibration_reason_code=cal_diag.reason_code,
    )

    if narrative.directional_bias is None:
        raise HTTPException(500, "Directional bias computation failed")

    return {
        **directional_bias_to_dict(narrative.directional_bias),
        "spot": snapshot.btc_price,
        "timestamp": snapshot.timestamp,
        "data_stale": deribit.data_stale,
    }


@app.get("/api/narrative/horizon")
async def get_narrative_horizon(horizon: str = Query("4h", pattern="^(4h|24h|72h)$")):
    """Narrative pondérée par horizon temporel (4h / 24h / 72h).
    Hypothèse V1 — non backtestée, affichable en lecture, pas en alerte forte."""
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    dex_levels = compute_dex_levels(snapshot)
    _snap_near2 = _near_snapshot(snapshot)
    _gex_near2 = compute_gex(_snap_near2)
    gmap = compute_gravity_map(_snap_near2, _gex_near2)
    walls = compute_options_walls(_snap_near2)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    audit = compute_gex_activity_audit(snapshot)
    gravity_audit = compute_gravity_activity_audit(snapshot)
    flip_audit_h = compute_flip_activity_audit(snapshot, gex.flip_level)

    cal_diag_h = diag_gex_calibration(_gex_calibration_cache)
    hn = resolve_narrative_horizon(
        mopi, gex, dp, gmap, walls, sq, snapshot.btc_price,
        horizon=horizon,
        audit=audit,
        dex_levels=dex_levels,
        gravity_audit=gravity_audit,
        flip_audit=flip_audit_h,
        calibration_status=cal_diag_h.status,
        calibration_reason_code=cal_diag_h.reason_code,
    )

    return {
        "horizon": hn.horizon,
        "force_dominante": hn.force_dominante,
        "scenario": hn.scenario,
        "niveau_haut": hn.niveau_haut,
        "niveau_haut_label": hn.niveau_haut_label,
        "niveau_bas": hn.niveau_bas,
        "niveau_bas_label": hn.niveau_bas_label,
        "forces_haussieres": hn.forces_haussieres,
        "forces_baissieres": hn.forces_baissieres,
        "forces_neutres": hn.forces_neutres,
        "vetoed_forces": hn.vetoed_forces,
        "confidence": hn.confidence,
        "hypothesis_version": hn.hypothesis_version,
        "hypothesis_disclaimer": hn.hypothesis_disclaimer,
        "btc_price": snapshot.btc_price,
        "flip_activity_tag": hn.flip_activity_tag,
        "flip_use_in_signal": hn.flip_use_in_signal,
        "flip_activity_context": hn.flip_activity_context,
        "calibration_status": cal_diag_h.status,
        "calibration_reason_code": cal_diag_h.reason_code,
        "timestamp": snapshot.timestamp,
        "data_stale": deribit.data_stale,
    }


@app.get("/api/history")
async def get_history(period: str = Query("7d", pattern="^(7d|30d|90d)$")):
    days_map = {"7d": 7, "30d": 30, "90d": 90}
    days = days_map[period]
    data = history_store.get_history(days)
    return {"period": period, "count": len(data), "data": data}


@app.get("/api/vol_structure")
async def get_vol_structure():
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")
    structure = _compute_vol_structure(snapshot)
    return {
        "btc_price": snapshot.btc_price,
        "timestamp": snapshot.timestamp,
        "data": structure,
    }


@app.get("/api/vol_smile")
async def get_vol_smile(
    expiry: str = Query(None),
    sort: str = Query("oi", pattern="^(oi|dte)$"),
):
    """IV par strike pour une expiry donnée — Volatility Smile.

    sort=oi  (défaut) : onglet par défaut = expiry avec le plus d'OI (institutionnel)
    sort=dte           : onglet par défaut = expiry la plus proche > 6 jours
    """
    from datetime import datetime, timezone as tz
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    spot = snapshot.btc_price
    today = datetime.now(tz.utc).date()

    expiries_dte: dict = {}
    for opt in snapshot.options:
        if opt.expiry in expiries_dte:
            continue
        try:
            exp_date = datetime.strptime(opt.expiry.upper(), "%d%b%y").date()
            expiries_dte[opt.expiry] = max(1, (exp_date - today).days)
        except Exception:
            pass

    # OI par expiry — nécessaire pour le tri et les métadonnées
    oi_by_exp: dict = {}
    for opt in snapshot.options:
        oi_by_exp[opt.expiry] = oi_by_exp.get(opt.expiry, 0) + opt.oi
    total_oi_all = sum(oi_by_exp.values()) or 1

    if not expiry:
        valid = {e: d for e, d in expiries_dte.items() if d > 1}
        if sort == "oi" and valid:
            # Expiry avec le plus d'OI = là où sont les institutions
            valid_oi = {e: oi_by_exp.get(e, 0) for e in valid}
            expiry = max(valid_oi, key=valid_oi.get) if valid_oi else None
        if not expiry:
            valid_dte = {e: d for e, d in valid.items() if d > 6}
            expiry = min(valid_dte, key=valid_dte.get) if valid_dte else (
                min(valid, key=valid.get) if valid else None
            )

    if not expiry:
        raise HTTPException(404, "No options available")

    by_strike: dict = {}
    for opt in snapshot.options:
        if opt.expiry != expiry or opt.iv <= 0:
            continue
        if abs(opt.strike - spot) / spot > 0.42:
            continue
        if opt.strike not in by_strike:
            by_strike[opt.strike] = {"call_iv": None, "put_iv": None}
        if opt.option_type == "call":
            by_strike[opt.strike]["call_iv"] = opt.iv
        else:
            by_strike[opt.strike]["put_iv"] = opt.iv

    data = []
    for s in sorted(by_strike):
        d = by_strike[s]
        data.append({
            "strike": s,
            "call_iv": round(d["call_iv"], 2) if d["call_iv"] else None,
            "put_iv": round(d["put_iv"], 2) if d["put_iv"] else None,
            "moneyness": round(s / spot, 4),
        })

    # available_expiries triées par OI décroissant + métadonnées
    available = []
    for e, dte in expiries_dte.items():
        exp_oi = oi_by_exp.get(e, 0)
        available.append({
            "expiry": e,
            "dte": dte,
            "total_oi": round(exp_oi, 1),
            "oi_pct": round(exp_oi / total_oi_all * 100, 1),
        })
    available.sort(key=lambda x: x["total_oi"], reverse=True)

    return {
        "btc_price": spot,
        "expiry": expiry,
        "dte": expiries_dte.get(expiry, 0),
        "expiry_oi": round(oi_by_exp.get(expiry, 0), 1),
        "expiry_oi_pct": round(oi_by_exp.get(expiry, 0) / total_oi_all * 100, 1),
        "data": data,
        "available_expiries": available,
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/expiry_oi")
async def get_expiry_oi():
    """OI par expiry trié par OI décroissant.
    Répond à : où sont les positions institutionnelles ?
    Utilisé pour le tri des onglets Smile/Surface et l'audit UX."""
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    today = datetime.now(timezone.utc).date()
    oi_by_expiry: dict = {}
    call_oi_by: dict = {}
    put_oi_by: dict = {}
    for opt in snapshot.options:
        oi_by_expiry[opt.expiry] = oi_by_expiry.get(opt.expiry, 0) + opt.oi
        if opt.option_type == "call":
            call_oi_by[opt.expiry] = call_oi_by.get(opt.expiry, 0) + opt.oi
        else:
            put_oi_by[opt.expiry] = put_oi_by.get(opt.expiry, 0) + opt.oi

    total_oi = sum(oi_by_expiry.values()) or 1
    result = []
    for expiry, oi in oi_by_expiry.items():
        exp_date = _parse_expiry_date(expiry)
        try:
            dte = max(1, (exp_date - today).days)
        except Exception:
            dte = 0
        result.append({
            "expiry": expiry,
            "dte": dte,
            "total_oi": round(oi, 1),
            "call_oi": round(call_oi_by.get(expiry, 0), 1),
            "put_oi": round(put_oi_by.get(expiry, 0), 1),
            "oi_pct": round(oi / total_oi * 100, 1),
            "oi_usd": round(oi * snapshot.btc_price, 0),
        })

    result.sort(key=lambda x: x["total_oi"], reverse=True)
    return {
        "btc_price": snapshot.btc_price,
        "total_oi": round(total_oi, 1),
        "data": result,
        "timestamp": snapshot.timestamp,
    }


@app.get("/api/data_quality")
async def get_data_quality():
    """
    Radiographie des contributions réelles par métrique options.

    Pour GEX, DEX, Walls, Gravity, PCR, Max Pain, Squeeze :
    - % signal DTE ≤ 14 / 15-45 / > 45
    - % signal Top 10% OI
    - % signal Top 10% Volume
    - Top 20 contributeurs réels

    Endpoint temporaire de diagnostic — aucun refactoring, lecture seule.
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    result = compute_data_quality(snapshot)
    return result


@app.get("/api/vol_surface")
async def get_vol_surface():
    """Surface de volatilité — strikes × DTEs × IV pour heatmap 2D."""
    from datetime import datetime, timezone as tz
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    spot = snapshot.btc_price
    today = datetime.now(tz.utc).date()

    expiries_dte: dict = {}
    for opt in snapshot.options:
        if opt.expiry in expiries_dte:
            continue
        try:
            exp_date = datetime.strptime(opt.expiry.upper(), "%d%b%y").date()
            expiries_dte[opt.expiry] = max(1, (exp_date - today).days)
        except Exception:
            pass

    grid: dict = {}
    for opt in snapshot.options:
        if opt.iv <= 0 or opt.expiry not in expiries_dte:
            continue
        if abs(opt.strike - spot) / spot > 0.35:
            continue
        key = (opt.strike, expiries_dte[opt.expiry])
        grid.setdefault(key, []).append(opt.iv)

    data = [
        {
            "strike": strike,
            "dte": dte,
            "iv": round(sum(ivs) / len(ivs), 2),
            "moneyness": round(strike / spot, 4),
        }
        for (strike, dte), ivs in grid.items()
    ]

    return {
        "btc_price": spot,
        "data": sorted(data, key=lambda x: (x["dte"], x["strike"])),
        "timestamp": snapshot.timestamp,
    }


class FeedbackRequest(BaseModel):
    alert_id: str
    vote: str


@app.post("/api/feedback")
async def post_feedback(req: FeedbackRequest):
    valid_votes = {"utile", "inutile", "trop_tot", "trop_tard"}
    if req.vote not in valid_votes:
        raise HTTPException(400, f"vote invalide — options: {sorted(valid_votes)}")
    if not req.alert_id or len(req.alert_id) > 32:
        raise HTTPException(400, "alert_id invalide")
    log_feedback(req.alert_id, req.vote)
    return {"ok": True, "alert_id": req.alert_id, "vote": req.vote}


@app.get("/api/rapport_7j")
async def get_rapport_7j():
    """Génère et retourne le rapport qualité 7 jours à la demande."""
    report = alerter.generate_weekly_report()
    return {"ok": True, "report": report}


@app.get("/api/conviction_audit")
async def get_conviction_audit():
    """Audit Conviction Score : simulation de référence + wall lifecycle + compteurs."""
    from .conviction_score import run_simulation
    report   = alerter.generate_conviction_audit()
    sim_rows = run_simulation()
    suppressed_by_conviction = alerter._conviction_blocked
    suppressed_by_lifecycle  = alerter._wall_lifecycle.events_suppressed_count()
    wall_states = [
        {
            "strike":           s.strike,
            "tag":              s.current_tag,
            "duration_hours":   round(s.duration_hours, 1),
            "reinforced":       s.reinforced_count,
            "weakened":         s.weakened_count,
            "disappeared":      s.disappeared_count,
            "net":              s.net_reinforcement,
            "structural":       s.is_structurally_significant(),
        }
        for s in alerter._wall_lifecycle.all_states()
    ]
    return {
        "ok": True,
        "conviction_threshold": MIN_SCORE_TO_SEND,
        "blocked_by_conviction": suppressed_by_conviction,
        "suppressed_by_lifecycle": suppressed_by_lifecycle,
        "wall_states": wall_states,
        "simulation": sim_rows,
        "report": report,
    }


@app.get("/api/prediction_accuracy")
async def get_prediction_accuracy(days: int = Query(7, ge=1, le=90)):
    """Rapport Prediction Accuracy par type d'alerte sur N jours."""
    report = alerter._tracker.generate_accuracy_report(days=days)
    stats  = alerter._tracker.get_accuracy_stats(days=days)
    return {"ok": True, "days": days, "report": report, "stats": stats}


@app.get("/api/executive_summary")
async def get_executive_summary():
    """Résumé Exécutif — biais, raisons, risques, objectif, invalidation, confiance."""
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    _snap_near3 = _near_snapshot(snapshot)
    gmap = compute_gravity_map(_snap_near3, compute_gex(_snap_near3))
    spot = snapshot.btc_price
    score = mopi.score

    # Seuils alignés exactement sur _classify_mopi() pour cohérence widget ↔ résumé
    if score >= 75:
        bias_label, bias_color = "BULL FORT", "green"
    elif score >= 55:
        bias_label, bias_color = "Bull modéré", "green"
    elif score >= 45:
        bias_label, bias_color = "Neutre", "yellow"
    elif score >= 25:
        bias_label, bias_color = "Bear modéré", "red"
    else:
        bias_label, bias_color = "BEAR FORT", "red"

    _intensity_fr = {"EXTREME": "forte", "HIGH": "élevée", "MODERATE": "modérée", "LOW": "faible"}
    _gex_reason = {
        "STABILISANT": "Les market makers freinent les mouvements brusques",
        "AMPLIFICATEUR": "Les market makers amplifient les mouvements haussiers ou baissiers",
        "NEUTRE": "Les market makers n'ont pas d'effet directionnel en ce moment",
    }
    reasons = [
        f"Options : {mopi.label.lower()} ({score:.0f}/100)",
        _gex_reason.get(gex.regime, f"Régime {gex.regime.lower()}"),
        f"BTC est attiré vers ${gmap.strongest_magnet:,.0f} à l'expiration" if gmap.strongest_magnet > 0 else "Pas d'aimant principal identifié",
    ]

    risks = []
    # Conflits inter-indicateurs (détectés avant les risques individuels)
    _dex_contradicts_mopi = (
        (dp.direction == "BEARISH_FLOWS" and bias_color == "green") or
        (dp.direction == "BULLISH_FLOWS" and bias_color == "red")
    )
    _gex_contradicts_mopi = (
        gex.regime == "AMPLIFICATEUR" and 45 <= score <= 55
    )
    signals_conflict = _dex_contradicts_mopi or _gex_contradicts_mopi

    # DEX listé comme risque uniquement s'il contredit le biais du résumé
    if dp.direction == "BEARISH_FLOWS" and bias_color == "green":
        intensity_fr = _intensity_fr.get(dp.intensity, dp.intensity.lower())
        risks.append(f"Les market makers pourraient vendre si BTC monte (pression {intensity_fr})")
    elif dp.direction == "BULLISH_FLOWS" and bias_color == "red":
        intensity_fr = _intensity_fr.get(dp.intensity, dp.intensity.lower())
        risks.append(f"Les market makers pourraient acheter face à la baisse (pression {intensity_fr})")
    zone_exp = gmap.next_explosive
    if zone_exp and abs(zone_exp - spot) / spot < 0.15:
        side = "sous" if zone_exp < spot else "au-dessus"
        risks.append(f"Si BTC atteint ${zone_exp:,.0f} ({side}), le mouvement peut s'emballer")
    # Risque régime AMPLIFICATEUR sans consensus directionnel clair
    if gex.regime == "AMPLIFICATEUR" and 40 <= score <= 60:
        risks.append("Régime AMPLIFICATEUR sans direction confirmée — les deux sens peuvent s'emballer")
    if not risks:
        risks.append("Pas de risque mécanique options critique identifié")

    # Alignement des signaux : ratio de consensus directionnel (4-10)
    # Interprétation : 10/10 = tous les indicateurs options alignés, PAS une probabilité de succès
    bull, bear = 0, 0
    if score >= 55: bull += 2
    elif score >= 50: bull += 1
    elif score <= 45: bear += 2
    else: bear += 1
    # AMPLIFICATEUR amplifie la direction → +1 dans le sens du biais
    # STABILISANT comprime → signal neutre, pas d'apport directionnel
    if gex.regime == "AMPLIFICATEUR":
        if score >= 50: bull += 1
        else: bear += 1
    if dp.direction == "BULLISH_FLOWS": bull += 1
    elif dp.direction == "BEARISH_FLOWS": bear += 1
    if gmap.strongest_magnet > spot * 1.005: bull += 1
    elif gmap.strongest_magnet < spot * 0.995: bear += 1
    total = bull + bear
    # Conflits inter-signaux → malus de confiance
    raw_confidence = 5 if total == 0 else max(4, min(10, round(3 + max(bull, bear) / total * 7)))
    confidence = max(4, raw_confidence - (2 if signals_conflict else 0))

    # Flip level : uniquement si disponible (flip_available=True)
    invalidation_level = gex.flip_level if gex.flip_available else None

    # ── Executive V2 — 4 lignes + warning (point 7) ──────────────────────────
    # Appels légers (réutilise les données déjà calculées)
    _pe_dominant_conf = 0.0
    _pe_dominant_dir = None
    _pe_historical_val = "en accumulation"
    try:
        _pe_snap_near = _near_snapshot(snapshot)
        _pe_walls = compute_options_walls(_pe_snap_near)
        _flip_audit_ex = compute_flip_activity_audit(snapshot, gex.flip_level)
        _flip_use_ex = _flip_audit_ex.flip_use_in_signal if _flip_audit_ex else True
        _mp_near_ex = gex.max_pain_profile.near if gex.max_pain_profile else None
        _mp_strike_ex = _mp_near_ex.strike if _mp_near_ex else gex.max_pain
        _mp_dte_ex = _mp_near_ex.dte if _mp_near_ex else 99
        _dex_levels_ex = compute_dex_levels(snapshot)
        _module_acc_ex: dict = {}
        try:
            from .indicator_accuracy import compute_indicator_accuracy as _ica
            _acc_ex = _ica(days=14)
            _module_acc_ex = _acc_ex.get("scores", {}) or {}
        except Exception:
            pass
        _pe_out = compute_probability_engine(
            spot=spot, gex_near=gex.gex_near, flip_level=gex.flip_level,
            flip_use_in_signal=_flip_use_ex, dex_direction=dp.direction,
            dex_actionable_btc=abs(_dex_levels_ex.actionable) if _dex_levels_ex else 0.0,
            iv_rank=mopi.iv_rank, pc_ratio_near=mopi.pc_ratio_near,
            put_wall=_pe_walls.major_put_wall or 0.0, call_wall=_pe_walls.major_call_wall or 0.0,
            max_pain_strike=_mp_strike_ex, max_pain_dte=_mp_dte_ex,
            mopi_score=mopi.score, gex_regime=gex.regime,
            dex_score=(dp.pressure_pct + 100.0) / 2.0,
            module_accuracy_scores=_module_acc_ex if _module_acc_ex else None,
        )
        _pe_dom = max(
            [_pe_out.bear_24h, _pe_out.bull_24h],
            key=lambda s: abs(s.probability - 50),
        )
        _pe_dominant_conf = _pe_dom.confidence
        _pe_dominant_dir = _pe_dom.direction
        _pe_historical_val = _pe_dom.historical_validation
    except Exception:
        pass

    # Scores système (edge réel)
    _sys_avg_score = None
    try:
        from .system_edge_report import compute_system_edge_report as _cser
        _ser = _cser(days=14)
        _sys_avg_score = _ser.get("system_score", {}).get("avg_score")
    except Exception:
        pass

    def _build_lecture_principale() -> str:
        """1 phrase — ce qui se passe vraiment."""
        regime_txt = {
            "STABILISANT": "Compression mécanique",
            "AMPLIFICATEUR": "Régime amplificateur",
            "NEUTRE": "Régime neutre",
        }.get(gex.regime, gex.regime)
        if score >= 65:
            sens = "haussière"
        elif score <= 35:
            sens = "baissière"
        else:
            sens = "sans direction confirmée"
        return f"{regime_txt} — pression options {sens} (MOPI {score:.0f}/100)"

    def _build_signal_exploitable() -> str:
        """Signal exploitable OUI/NON avec raison."""
        sys_ok = _sys_avg_score is not None and _sys_avg_score >= 40
        pe_rules_ok = _pe_dominant_conf >= 60
        hist_ok = _pe_historical_val in ("moyenne", "forte")
        if pe_rules_ok and hist_ok and sys_ok:
            return "OUI — règles alignées + validation historique confirmée"
        reasons_non = []
        if not pe_rules_ok:
            reasons_non.append("règles peu alignées")
        if not hist_ok:
            reasons_non.append(f"validation historique {_pe_historical_val}")
        if not sys_ok:
            reasons_non.append(f"edge système faible ({_sys_avg_score}/100)" if _sys_avg_score is not None else "edge système non disponible")
        return f"NON — {', '.join(reasons_non)}"

    def _build_zone_cle() -> str:
        """Zone clé = niveau qui change tout."""
        if invalidation_level:
            dist_pct = abs(invalidation_level - spot) / spot * 100
            side = "au-dessus" if invalidation_level > spot else "en dessous"
            return f"${invalidation_level:,.0f} ({dist_pct:.1f}% {side}) — invalidation du biais si cassé"
        return "Pas de flip disponible — se référer aux murs options"

    def _build_zone_attraction() -> str:
        """Zone d'attraction principale."""
        lines = []
        if gmap.strongest_magnet and abs(gmap.strongest_magnet - spot) / spot < 0.12:
            dist = (gmap.strongest_magnet - spot) / spot * 100
            lines.append(f"Gravity ${gmap.strongest_magnet:,.0f} ({dist:+.1f}%)")
        mp_near_ex = gex.max_pain_profile.near if gex.max_pain_profile else None
        if mp_near_ex:
            dist_mp = (mp_near_ex.strike - spot) / spot * 100
            lines.append(f"Max Pain ${mp_near_ex.strike:,.0f} J-{mp_near_ex.dte} ({dist_mp:+.1f}%)")
        return " | ".join(lines) if lines else "Aucune zone d'attraction immédiate identifiée"

    def _build_warning() -> Optional[str]:
        """Warning si PE dit fort mais edge réel faible."""
        pe_fort = _pe_dominant_conf >= 75
        edge_faible = _sys_avg_score is not None and _sys_avg_score < 30
        hist_faible = _pe_historical_val in ("faible", "en accumulation")
        if pe_fort and (edge_faible or hist_faible):
            parts = []
            if hist_faible:
                parts.append(f"validation historique {_pe_historical_val}")
            if edge_faible:
                parts.append(f"edge système {_sys_avg_score}/100")
            reason = " + ".join(parts)
            dir_txt = "bearish" if _pe_dominant_dir == "BEAR" else "bullish" if _pe_dominant_dir == "BULL" else ""
            return (
                f"⚠️ Probability Engine {dir_txt} fort (règles {_pe_dominant_conf:.0f}%), "
                f"mais {reason}. "
                "Ne pas traiter ce signal comme un trade confirmé."
            )
        return None

    executive_v2 = {
        "lecture_principale": _build_lecture_principale(),
        "signal_exploitable": _build_signal_exploitable(),
        "zone_cle":           _build_zone_cle(),
        "zone_attraction":    _build_zone_attraction(),
        "warning":            _build_warning(),
        "pe_dominant_conf":   round(_pe_dominant_conf, 1),
        "pe_historical_validation": _pe_historical_val,
        "sys_avg_score":      _sys_avg_score,
    }

    return {
        "btc_price": spot,
        "bias_label": bias_label,
        "bias_color": bias_color,
        "reasons": reasons,
        "risks": risks,
        "objective": gmap.strongest_magnet,
        "invalidation": invalidation_level,
        "flip_available": gex.flip_available,
        "signals_conflict": signals_conflict,
        "confidence": confidence,
        "timestamp": snapshot.timestamp,
        "data_stale": deribit.data_stale,
        # Point 7 — Executive V2
        "executive_v2": executive_v2,
    }


@app.get("/api/dashboard_accuracy")
async def get_dashboard_accuracy(days: int = Query(7, ge=1, le=90)):
    """Phase 6 — Score Dashboard : accuracy par type de biais sur N jours."""
    report = accuracy_tracker.generate_accuracy_report(days=days)
    stats  = accuracy_tracker.get_accuracy_stats(days=days)
    return {"ok": True, "days": days, "report": report, "stats": stats}


@app.get("/api/rapport_dashboard")
async def get_rapport_dashboard(days: int = Query(7, ge=1, le=90)):
    """Phase 6 — Rapport hebdomadaire à la demande."""
    report = accuracy_tracker.generate_accuracy_report(days=days)
    return {"ok": True, "days": days, "report": report}


@app.get("/api/regime_data_health")
async def get_regime_data_health():
    """Phase 5 — Santé des données régime (alias → /api/data_health)."""
    return accuracy_tracker.get_data_health()


@app.get("/api/data_health")
async def get_data_health():
    """Phase 1 — Audit couverture données régime.

    Retourne par champ : count, coverage_pct, status (OK/ALERTE/PARTIEL).
    Inclut : cause racine flip_distance_pct, global_status, liste des alertes.
    """
    return accuracy_tracker.get_data_health()


@app.get("/api/data_health_history")
async def get_data_health_history():
    """Phase 4 — Surveillance temporelle de l'enrichissement.

    Retourne la progression horaire des snapshots enrichis.
    Permet de vérifier que cumulative_enriched monte : 1→2→3→4 (pas 1→1→1→1).
    """
    return accuracy_tracker.get_data_health_history()


@app.get("/api/enrichment_growth")
async def get_enrichment_growth():
    """Phase 4 DATA HEALTH V2 — Croissance réelle du pipeline enrichi sur 24h.

    Retourne current / 24h_ago / growth / growing.
    Prouve que le compteur avance réellement.
    """
    return accuracy_tracker.get_enrichment_growth()


@app.get("/api/bias_regime_matrix_progress")
async def get_bias_regime_matrix_progress():
    """Visibilité progression apprentissage conditionnel — cellule par cellule.

    Pour chaque combinaison bias×régime (5×6=30 cellules) :
      - n          : observations accumulées
      - target_n   : 30 (seuil EXPLOITABLE)
      - progress_pct
      - status     : EMPTY / COLLECTING / EXPLOITABLE / ROBUST
      - eta_days   : estimation jours avant N=30

    Permet de savoir précisément quelles cellules deviennent statistiquement exploitables.
    top_10_advanced, bottom_10_empty, almost_exploitable, dead_zones inclus.
    """
    return accuracy_tracker.compute_bias_regime_matrix_progress()


@app.get("/api/bias_regime_matrix")
async def get_bias_regime_matrix(days: int = Query(90, ge=7, le=365)):
    """Phase 3 — Matrice biais × régime.

    Retourne EV/WR/PF par combinaison (Biais × Régime).
    N<30 = insufficient — accumulation en cours.
    Objectif : répondre dans 30 jours à "quel signal fonctionne dans quel régime ?"
    """
    return accuracy_tracker.compute_bias_regime_matrix(days=days)


@app.get("/api/signal_accuracy")
async def get_signal_accuracy(days: int = Query(30, ge=1, le=90)):
    """Gap #5 — Accuracy par event type : winrate, MFE/MAE, outcomes +1h/+4h/+24h/+72h.

    Couvre : squeeze_bullish/bearish, wall_rejection, wall_breakout, gravity_magnet,
    dealer_buy/sell_pressure, mopi_bullish/bearish.
    Les verdicts arrivent 72h après chaque signal. Significatif après J+30.
    """
    es     = get_event_store()
    stats  = es.get_accuracy_by_event_type(days=days)
    report = es.generate_accuracy_report(days=days)
    return {
        "ok":      True,
        "days":    days,
        "pending": es.get_pending_count(),
        "stats":   stats,
        "report":  report,
    }


@app.get("/api/indicator_accuracy")
async def get_indicator_accuracy(days: int = Query(30, ge=7, le=90)):
    """Score 0-100 par indicateur sur les N derniers jours.

    Répond : {"gex": 68, "dex": 74, "gravity": 41, "walls": 37, "squeeze": 81, ...}
    Score = winrate réel des signaux envoyés (hit_target / total) × 100.
    None si données insuffisantes (N < 5 signaux).
    Significatif après ~14 jours d'accumulation.
    """
    from .indicator_accuracy import compute_indicator_accuracy
    return compute_indicator_accuracy(days=days)


@app.get("/api/backtest")
async def get_backtest(
    days: int = Query(90, ge=7, le=365),
    demo: bool = Query(False, description="true = simulation sur données BTC réelles + options synthétiques"),
):
    """Phase 4 — Backtest Signal Mamos : validation statistique des composantes.

    Mesure le pouvoir prédictif de chaque indicateur sur BTC à +24h, +72h, +7j.
    Retourne winrate, performance moyenne, max drawdown par signal.

    ?demo=true : utilise les vrais prix BTC (Binance 90j) + métriques options synthétiques.
    Sans demo   : utilise les données Deribit réelles accumulées (significatif après ~30j).
    """
    if demo:
        return run_demo_backtest(days=days)
    return run_backtest(days=days)


@app.get("/api/mopi_vs_btc")
async def get_mopi_vs_btc(
    period:     str = Query("7d", pattern="^(7d|30d|90d)$"),
    resolution: str = Query("1h", pattern="^(30m|1h|4h|1d)$"),
):
    """MOPI vs BTC — Validation du pouvoir prédictif historique.

    Répond à UNE question : 'Le MOPI aide-t-il réellement à gagner de l'argent ?'
    Source : options_history.db — données réelles accumulées.
    """
    from .mopi_vs_btc import compute_mopi_vs_btc
    return compute_mopi_vs_btc(period=period, resolution=resolution)


@app.get("/api/gex_activity_audit")
async def get_gex_activity_audit():
    """GEX Activity Audit — qualité réelle du signal GEX avant usage dans Signal Mamos.

    Mesure la part du GEX provenant de positions :
      DORMANT    : OI sans flux — gonfle le signal sans valeur prédictive
      STRUCTURAL : gros OI, flux faible, loin du spot ou long-daté
      ACTIVE     : OI + flux récent
      ACTIONABLE : flux + ATM + DTE court — impact BTC immédiat

    Verdict : le GEX actuel est-il fiable pour Signal Mamos ?
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    audit = compute_gex_activity_audit(snapshot)

    def _cat(stats):
        return {
            "gex_abs_usd": stats.gex_abs_usd,
            "gex_net_usd": stats.gex_net_usd,
            "gex_pct": stats.gex_pct,
            "count": stats.count,
            "top_contributors": stats.top_contributors,
        }

    return {
        "btc_price": audit.btc_price,
        "gex_total_usd": audit.gex_total_usd,
        "gex_regime": audit.gex_regime,
        "timestamp": audit.timestamp,
        "data_stale": deribit.data_stale,
        "categories": {
            "dormant":    _cat(audit.dormant),
            "structural": _cat(audit.structural),
            "active":     _cat(audit.active),
            "actionable": _cat(audit.actionable),
        },
        "activity_scores": {
            "gex_structural": audit.gex_structural_score,
            "gex_active": audit.gex_active_score,
            "gex_actionable": audit.gex_actionable_score,
            "active_pct": audit.active_pct,
            "actionable_pct": audit.actionable_pct,
            "overall_profile": audit.overall_profile,
        },
        "signal_quality": {
            "score": audit.signal_quality_score,
            "label": audit.signal_quality_label,
            "color": audit.signal_quality_color,
            "verdict": audit.signal_verdict,
            "use_in_signal": audit.use_in_signal,
        },
        "low_oi_anomaly_count": audit.low_oi_anomaly_count,
    }


@app.get("/api/gravity_activity_audit")
async def get_gravity_activity_audit():
    """Gravity Activity Audit — qualité réelle des zones Gravity.

    Pour chaque zone, détermine si l'OI qui la constitue est :
      DORMANT    : OI sans flux (LEAPS dormants) — zone fantôme
      STRUCTURAL : OI longue date, peu de flux   — contexte de fond
      ACTIVE     : OI + flux récent              — zone surveillée
      ACTIONABLE : flux + ATM + DTE court        — impact BTC immédiat

    Verdict par zone :
      💀 Gravity Dormant      → ne pas cibler court terme
      🪨 Gravity Structurelle → contexte de fond
      ⚡ Gravity Active       → zone surveillée
      🔥 Gravity Actionnable  → influence BTC maintenant

    Signal Quality (0-10) + use_in_signal — même modèle que /api/gex_activity_audit.
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    audit = compute_gravity_activity_audit(snapshot)

    def _breakdown(b):
        return {
            "oi_usd": b.oi_usd,
            "oi_pct": b.oi_pct,
            "count": b.count,
        }

    def _zone(z):
        return {
            "strike": z.strike,
            "zone_type": z.zone_type,
            "strength": z.strength,
            "oi_usd_total": z.oi_usd_total,
            "contribution_pct": z.contribution_pct,
            "structural_score": z.structural_score,
            "active_score": z.active_score,
            "actionable_score": z.actionable_score,
            "activity_tag": z.activity_tag,
            "activity_label": z.activity_label,
            "activity_verdict": z.activity_verdict,
            "use_in_signal": z.use_in_signal,
            "signal_quality": {
                "score": z.signal_quality_score,
                "label": z.signal_quality_label,
                "color": z.signal_quality_color,
            },
            "breakdown": {
                "dormant":    _breakdown(z.dormant),
                "structural": _breakdown(z.structural),
                "active":     _breakdown(z.active),
                "actionable": _breakdown(z.actionable),
            },
            "top_contributors": z.top_contributors,
        }

    return {
        "btc_price": audit.btc_price,
        "timestamp": audit.timestamp,
        "data_stale": deribit.data_stale,
        "total_gravity_oi_usd": audit.total_gravity_oi_usd,
        "global_summary": {
            "dormant_pct":    audit.global_dormant_pct,
            "structural_pct": audit.global_structural_pct,
            "active_pct":     audit.global_active_pct,
            "actionable_pct": audit.global_actionable_pct,
            "overall_tag":    audit.overall_tag,
            "overall_label":  audit.overall_label,
            "overall_verdict": audit.overall_verdict,
        },
        "activity_scores": {
            "structural":            audit.global_structural_score,
            "active":                audit.global_active_score,
            "actionable":            audit.global_actionable_score,
            "active_engine_pct":     audit.global_active_engine_pct,
            "actionable_engine_pct": audit.global_actionable_engine_pct,
        },
        "signal_quality": {
            "score":         audit.signal_quality_score,
            "label":         audit.signal_quality_label,
            "color":         audit.signal_quality_color,
            "use_in_signal": audit.use_in_signal,
        },
        "zones": [_zone(z) for z in audit.zones],
    }


@app.get("/api/field_diagnostics")
async def get_field_diagnostics():
    """Gap #6/#9 — Diagnostic explicite pour chaque champ critique du dashboard.

    Chaque champ retourne : {status, reason_code, value, debug}.
    status : available | degraded | unavailable | stale
    Aucun champ critique ne renvoie None sans reason_code.

    Champs couverts :
      flip_level        — Seuil de régime dealers
      gravity_magnet    — Zone d'Attraction
      gravity_explosive — Zone Explosive
      wall_call         — Major Call Wall
      wall_put          — Major Put Wall
      mopi              — Score MOPI
      squeeze           — Score Squeeze
      dealer_pressure   — Pression Dealers
      gex_calibration   — Calibration cap GEX near-term
    """
    try:
        snapshot = await deribit.get_cached_snapshot()
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    gex = compute_gex(snapshot)
    mopi = compute_mopi(
        snapshot, gex, iv_history_cache,
        gex_near_cap=_gex_calibration_cache["cap_value"],
        cap_mode=_gex_calibration_cache["cap_mode"],
        saturation_rate_7d=_gex_calibration_cache.get("saturation_rate_7d"),
    )
    dp = compute_dealer_pressure(snapshot)
    sq = compute_squeeze_score(snapshot, gex, dp, mopi.iv_rank)
    _snap_near_fd = _near_snapshot(snapshot)
    gmap = compute_gravity_map(_snap_near_fd, compute_gex(_snap_near_fd))
    walls = compute_options_walls(_snap_near_fd)

    diagnostics = build_all_diagnostics(
        gex, snapshot, gmap, walls, mopi, sq, dp,
        gex_calibration_cache=_gex_calibration_cache,
    )

    return {
        "btc_price": snapshot.btc_price,
        "timestamp": snapshot.timestamp,
        "data_stale": deribit.data_stale,
        "diagnostics": diagnostics,
    }


@app.get("/api/system_edge_report")
async def get_system_edge_report(days: int = Query(14, ge=1, le=90)):
    """Phase Observation — rapport d'edge système complet.

    Agrège : events capturés/envoyés/bloqués, outcomes +4h/+24h/+72h,
    scores par indicateur, top5 best/worst, feedback utilisateur,
    taux de blocage conviction, EV approximatif.

    Le champ observation.message indique si le système est prêt
    à recalibrer ou toujours en phase d'accumulation.
    """
    return compute_system_edge_report(days=days)


@app.get("/api/stats_edge")
async def get_stats_edge(days: int = Query(30, ge=7, le=90)):
    """Vue exécutive Stats & Edge — Validation réelle.

    Agrège phase collecte, observations totales, outcomes validés +4h/+24h/+72h,
    progression vers les seuils de recalibration, et tableau par event_type
    (confidence, score, statut) pour la section dashboard /test/option.

    Reconnaissance : gex_regime, gravity_explosive, max_pain_pull/shift, mopi_cross,
    squeeze_bullish/bearish, wall_rejection/breakout, dex_bullish/bearish.
    """
    return compute_stats_edge(days=days)


@app.get("/api/stats/collection_health")
async def collection_health():
    es     = get_event_store()
    health = es.get_collection_health()

    def _task_status(name: str) -> str:
        t = _loop_tasks.get(name)
        if t is None:
            return "not_started"
        return "stopped" if t.done() else "running"

    loops = {
        "history_saver":       _task_status("history_saver"),
        "event_validator":     _task_status("event_validator"),
        "prediction_validator": _task_status("prediction_validator"),
        "daily_reporter":      _task_status("daily_reporter"),
    }

    warnings = list(health.pop("warnings", []))
    for name, st in loops.items():
        if st != "running":
            warnings.append(f"loop {name} est {st}")

    return {
        **health,
        "prediction_tracker": {
            "pending":   accuracy_tracker.get_pending_count(),
            "finalized": accuracy_tracker.get_finalized_count(),
        },
        "loops":    loops,
        "warnings": warnings,
    }


@app.get("/api/model_arena")
async def get_model_arena(days: int = Query(30, ge=7, le=90)):
    """Model Arena — performance comparative des moteurs de prédiction."""
    return _model_arena.get_arena_stats(days=days)


@app.get("/api/model_arena/history")
async def get_model_arena_history(hours: int = Query(24, ge=1, le=168)):
    """Timeline des prédictions et outcomes des moteurs primaires."""
    return _model_arena.get_arena_history(hours=hours)


@app.get("/api/model_arena/debug")
async def get_model_arena_debug():
    """Debug : compteurs, états des workers, prochaines évaluations."""
    return _model_arena.get_arena_debug()


@app.get("/api/model_arena/health")
async def get_model_arena_health():
    """Audit complet Arena Health — traçabilité données, workers, progression, trust gate."""
    return _model_arena.get_arena_health()


@app.get("/api/model_arena/multi_leaderboard")
async def get_model_arena_multi_leaderboard(days: int = Query(30, ge=7, le=90)):
    """Leaderboard multi-métriques — 6 phases (WR/EV/PF/Sharpe, horizons, significativité, bruit, conclusion)."""
    return _model_arena.get_multi_metric_leaderboard(days=days)


@app.get("/api/model_arena/leaderboard")
async def get_model_arena_leaderboard(days: int = Query(30, ge=7, le=90)):
    """Leaderboard simplifié — classement moteurs par EV (critère principal), pour affichage rapide."""
    stats = _model_arena.get_arena_stats(days=days)
    perf = stats.get("performance", {})
    models = list(perf.keys())

    ranking = []
    for mn in models:
        model_perf = perf[mn]
        n_total = sum(s.get("n_signals", 0) for s in model_perf.values() if isinstance(s, dict))
        wrs = [s["winrate"] for s in model_perf.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        evs = [s["ev_mean"] for s in model_perf.values() if isinstance(s, dict) and s.get("n_signals", 0) > 0]
        horizon_detail = {
            hz: {
                "winrate": model_perf[hz].get("winrate"),
                "n": model_perf[hz].get("n_signals", 0),
                "ev": model_perf[hz].get("ev_mean"),
            }
            for hz in ["4h", "24h", "72h"]
            if isinstance(model_perf.get(hz), dict)
        }
        ranking.append({
            "model": mn,
            "version": (stats.get("current_predictions", {}).get(mn) or {}).get("4h", {}).get("model_version"),
            "status": stats["model_statuses"].get(mn, "collecting"),
            "is_principal": mn == stats["principal_engine"],
            "is_best": mn == stats["best_primary_model"],
            "n_evaluated": n_total,
            "avg_winrate": round(sum(wrs) / len(wrs), 3) if wrs else None,
            "avg_ev": round(sum(evs) / len(evs), 3) if evs else None,
            "horizon_detail": horizon_detail,
        })

    ranking.sort(key=lambda x: (x["avg_ev"] if x["avg_ev"] is not None else -999), reverse=True)

    total_outcomes = stats["meta"]["total_outcomes_db"]
    days_of_data   = stats["meta"]["days_of_data"]

    # Trust gate — Phase 6
    min_14d  = days_of_data >= _model_arena._MIN_DAYS_PROMOTION
    min_100  = total_outcomes >= _model_arena._MIN_OUTCOMES_PROMOTION
    winner_label = (
        "Moteur gagnant"    if (min_14d and min_100)
        else "Leader provisoire" if total_outcomes >= _model_arena._MIN_OUTCOMES_COLLECTING
        else "Collecte en cours"
    )

    return {
        "ranking": ranking,
        "principal_engine": stats["principal_engine"],
        "best_model": stats["best_primary_model"],
        "total_outcomes": total_outcomes,
        "days_of_data": days_of_data,
        "next_eval_in": None,
        "trust_gate": {
            "min_14_days":      min_14d,
            "min_100_outcomes": min_100,
            "winner_label":     winner_label,
        },
    }


@app.get("/api/model_arena/confusion_matrix")
async def get_confusion_matrix(days: int = Query(30, ge=7, le=90)):
    """Matrice de confusion par moteur — diagnostique les prédictions inversées.

    Retourne : pour chaque moteur × horizon, combien de fois pred=X réel=Y.
    Inclut aussi le WR directionnel (hors RANGE) et si le mode contrarian est actif.
    """
    return _model_arena.get_confusion_matrix(days=days)


@app.get("/api/model_arena/performance_history")
async def get_model_arena_performance_history(days: int = Query(7, ge=1, le=30)):
    """Série temporelle des performances — sparklines et tendances par moteur."""
    return _model_arena.get_arena_performance_history(days=days)


@app.get("/api/model_arena/shadow_debug")
async def get_shadow_debug():
    """Shadow debug — poids globaux, régime actuel, poids finaux, propositions appliquées/bloquées."""
    return _model_arena.get_shadow_debug()


@app.get("/api/model_arena/outcome_audit")
async def get_outcome_audit(limit: int = Query(50, ge=10, le=200)):
    """Phase 1 — Audit des outcomes : valider la logique d'évaluation."""
    return _model_arena.get_outcome_audit(limit=limit)


@app.get("/api/model_arena/weights")
async def get_weights_audit():
    """Phase 2 — Audit auto-calibration : poids initiaux vs actuels."""
    return _model_arena.get_weights_audit()


@app.get("/api/model_arena/neural_tabular_debug")
async def get_neural_tabular_debug():
    """Debug Neural Tabular Engine — architecture MLP, training log, feature importance, confusion matrix."""
    return _model_arena.get_neural_tabular_debug()


@app.get("/api/model_arena/temporal_neural_debug")
async def get_temporal_neural_debug():
    """Debug Temporal Neural Engine — architecture GRU, training log, confusion matrix, séquences."""
    return _model_arena.get_temporal_neural_debug()


@app.get("/api/model_arena/bme_status")
async def get_bme_status():
    """BTC Momentum Engine — statut, métriques de validation, backtest par horizon."""
    from .btc_momentum_engine import get_bme
    bme = get_bme()
    status = bme.get_status()
    backtests = {}
    for hz in ["4h", "24h", "72h"]:
        if status["horizons"][hz]["model_ready"]:
            backtests[hz] = bme.backtest(hz, last_n=200)
    return {"status": status, "backtests": backtests}


@app.post("/api/model_arena/bme_train")
async def trigger_bme_train():
    """Force retrain du BTC Momentum Engine sur tous les horizons."""
    from .btc_momentum_engine import train_all
    results = train_all()
    return {"results": results, "message": "BME retrain completed"}


@app.post("/api/model_arena/bme_enrich")
async def trigger_bme_enrichment(force: bool = False):
    """Fetch klines Binance 90j et enrichit le dataset d'entraînement du BME."""
    from .bme_binance_enrichment import build_and_store, get_stats
    inserted = build_and_store(force=force)
    stats = get_stats()
    return {"inserted": inserted, "stats": stats}


@app.get("/api/model_arena/bme_enrich_stats")
async def get_bme_enrich_stats():
    """Distribution du dataset Binance par horizon et label."""
    from .bme_binance_enrichment import get_stats
    return get_stats()


@app.get("/api/mopi_divergence")
async def get_mopi_divergence():
    """MOPI Divergence Engine — état actuel de la divergence MOPI vs prix BTC.

    Retourne : type bullish/bearish/none, force, fenêtre détectée,
    probas 4h/24h/72h, raw_event_count vs unique_setup_count,
    dernier signal détecté.
    """
    from .mopi_divergence_engine import get_current_mde_signal
    return get_current_mde_signal()


@app.get("/api/mde_vs_naive")
async def get_mde_vs_naive(days: int = Query(30, ge=7, le=90)):
    """Comparaison head-to-head MOPI Divergence Engine vs Naive Baseline.

    Verdict WR / EV / Profit Factor par horizon.
    Applique les règles de sécurité EXPLORATION / SIGNAL FRAGILE / SIGNAL ROBUSTE.
    Indique si MDE bat réellement le Naive et s'il est prêt à le remplacer.
    """
    from .model_arena import get_mde_vs_naive_comparison
    return get_mde_vs_naive_comparison(days=days)


@app.get("/api/neural_health")
async def get_neural_health():
    """Phase 2 — Preuve de vie Neural Engines : statut, outcomes réels, alertes."""
    return _model_arena.get_neural_health()


@app.get("/api/neural_training_log")
async def get_neural_training_log(limit: int = Query(50, ge=10, le=200)):
    """Phase 3 — Journal visuel des retrains neuronaux avec métriques de validation."""
    return _model_arena.get_neural_training_log(limit=limit)


@app.get("/api/neural_learning_curve")
async def get_neural_learning_curve():
    """Phase 5 — Courbe d'apprentissage : n_outcomes → WR/EV/PF de validation."""
    return _model_arena.get_neural_learning_curve()


@app.get("/api/feature_audit")
async def get_feature_audit(days: int = Query(30, ge=7, le=90)):
    """Phase 3+4 — Feature Audit + Ranking : valeur prédictive réelle de chaque feature."""
    return _model_arena.get_feature_audit(days=days)


@app.get("/api/feature_combination_audit")
async def get_feature_combination_audit(days: int = Query(30, ge=7, le=90)):
    """Phase 5 — Combinaisons de features : quelles paires apportent du signal."""
    return _model_arena.get_feature_combination_audit(days=days)


@app.get("/api/feature_health")
async def get_feature_health():
    """Phase 6 — Feature Health : couverture, fraîcheur, qualité par feature."""
    return _model_arena.get_feature_health()


@app.get("/api/regime_performance")
async def get_regime_performance(days: int = Query(30, ge=7, le=90)):
    """Regime Segmentation Engine — Performance conditionnelle par régime de marché.

    Répond à : une feature gagne-t-elle partout, ou seulement dans certains régimes ?

    Régimes :
      Positive_Gamma  — GEX > 5M (STABILISANT)
      Negative_Gamma  — GEX < -5M (AMPLIFICATEUR)
      Neutral         — GEX dans la zone neutre
      Vol_Expansion   — IV Rank > 60
      Vol_Contraction — IV Rank < 40
      Panic           — GEX négatif + IV > 70 + DEX extrême baissier

    Retourne :
      matrix        — {event_type: {regime: {ev, winrate, profit_factor, n}}}
      engine_matrix — {engine_family: {regime: {ev, winrate, profit_factor, n}}}
      insights      — découvertes clés (meilleur régime par moteur)
    """
    from .regime_segmentation import compute_regime_performance
    return compute_regime_performance(days=days)


@app.get("/api/regime_adaptive_weights")
async def get_regime_adaptive_weights(days: int = Query(30, ge=7, le=90)):
    """Poids adaptatifs par régime — Conviction Score V2 (observe mode).

    Pour chaque moteur (squeeze, walls, gravity, dealer, mopi, gex, max_pain),
    calcule le poids proposé selon les performances historiques par régime.

    Garde-fous :
      N < 30  → BLOQUÉ (aucun ajustement)
      N 30-99 → ajustement max ±5%
      N 100+  → ajustement max ±15%

    Mode actuel : observe (propose, n'applique PAS au moteur principal).

    Retourne :
      mode             — observe / shadow / active
      current_regime   — régime(s) détecté(s) sur l'état de marché actuel
      summary          — poids proposés pour les régimes actuels par moteur
      weight_table     — tableau complet {régime: {moteur: {base, proposé, delta, N, EV, WR, PF}}}
      stats            — comptages bloqué / augmenté / réduit / inchangé
    """
    from .regime_adaptive_weights import compute_adaptive_weights, format_adaptive_weights_report
    report = compute_adaptive_weights(days=days)
    return format_adaptive_weights_report(report)


# ═══════════════════════════════════════════════════════════════════════════════
# SPY / US Markets — Phases 1-6
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/spy")
async def get_spy_dashboard():
    """Dashboard SPY complet : VIX Regime + US-MOPI + Stress Rebound + SPY Regime."""
    cache = spy_worker.get_cache()
    stale = spy_worker.is_stale(7200)

    vix = cache.get("vix")
    feats = compute_vix_features(cache)
    vix_regime = cache.get("vix_regime") or compute_vix_regime(feats)

    return {
        "ts": cache.get("last_updated"),
        "stale": stale,
        "spy": {
            "price":        cache.get("spy_price"),
            "change_1d":    cache.get("spy_change_1d"),
            "drawdown_3d":  cache.get("spy_drawdown_3d"),
            "drawdown_5d":  cache.get("spy_drawdown_5d"),
            "dist_52w_high":cache.get("spy_dist_52w_high"),
            "volume":       cache.get("spy_volume"),
        },
        "vix": {
            "current":      vix,
            "vix9d":        cache.get("vix9d"),
            "vix3m":        cache.get("vix3m"),
            "vix6m":        cache.get("vix6m"),
            "vvix":         cache.get("vvix"),
            "change_1d":    cache.get("vix_change_1d"),
            "change_5d":    cache.get("vix_change_5d"),
            "percentile":   cache.get("vix_percentile"),
            "iv_rank":      cache.get("iv_rank"),
            "contango":     cache.get("contango"),
            "vix9d_vix_spread":  cache.get("vix9d_vix_spread"),
            "vix_vix3m_spread":  cache.get("vix_vix3m_spread"),
        },
        "vix_regime": vix_regime_to_dict(vix_regime, feats),
        "pcr": {
            "equity":     cache.get("pcr_equity"),
            "index":      cache.get("pcr_index"),
            "spy_volume": cache.get("pcr_spy_volume"),
            "spy_oi":     cache.get("pcr_spy_oi"),
            "spy_near":   cache.get("pcr_spy_near"),
        },
        "us_mopi": {
            "score":            cache.get("us_mopi"),
            "label":            cache.get("us_mopi_label"),
            **compute_us_mopi(cache),
        },
        "spy_regime": {
            "regime":      cache.get("spy_regime"),
            "description": spy_regime_description(cache.get("spy_regime") or "NEUTRAL"),
            "color":       spy_regime_color(cache.get("spy_regime") or "NEUTRAL"),
        },
        "stress_rebound": {
            "prob_1d":   cache.get("prob_rebound_1d"),
            "prob_3d":   cache.get("prob_rebound_3d"),
            "prob_5d":   cache.get("prob_rebound_5d"),
            "confidence":cache.get("rebound_confidence"),
            "factors":   json.loads(cache.get("rebound_factors") or "[]"),
        },
    }


@app.get("/api/spy/history")
async def get_spy_history(limit: int = Query(100, ge=10, le=500)):
    """Historique snapshots SPY (30min interval)."""
    return spy_worker.get_spy_history(limit=limit)


@app.get("/api/spy/vix_regime")
async def get_spy_vix_regime():
    """Régime VIX détaillé avec term structure."""
    cache = spy_worker.get_cache()
    feats = compute_vix_features(cache)
    regime = cache.get("vix_regime") or compute_vix_regime(feats)
    return {
        "ts": cache.get("last_updated"),
        "stale": spy_worker.is_stale(7200),
        **vix_regime_to_dict(regime, feats),
    }


@app.get("/api/spy/us_mopi")
async def get_spy_us_mopi():
    """US-MOPI score 0-100 avec composantes et signal contrarian."""
    cache = spy_worker.get_cache()
    result = compute_us_mopi(cache)
    result["ts"] = cache.get("last_updated")
    result["stale"] = spy_worker.is_stale(7200)
    return result


@app.get("/api/spy/stress_rebound")
async def get_spy_stress_rebound():
    """Stress Rebound Engine — probabilités rebond SPY à +1j/+3j/+5j."""
    cache = spy_worker.get_cache()
    result = compute_stress_rebound(cache)
    result["ts"] = cache.get("last_updated")
    result["stale"] = spy_worker.is_stale(7200)
    result["vix_current"] = cache.get("vix")
    result["spy_current"] = cache.get("spy_price")
    return result


@app.get("/api/spy/arena")
async def get_spy_arena():
    """SPY Arena — 5 moteurs prédictifs (Phase 6).
    Retourne toujours le leaderboard historique ; predictions live gated sur staleness.
    """
    cache = spy_worker.get_cache()
    stale = spy_worker.is_stale(7200)
    history = _spy_arena._get_history(200)
    leaderboard = _spy_arena._build_leaderboard(history)
    if stale:
        return {
            "stale": True,
            "ts": int(__import__("time").time()),
            "engines": {},
            "consensus": None,
            "leaderboard": leaderboard,
        }
    return _spy_arena.get_spy_arena_snapshot(cache)


@app.get("/api/spy/arena/history")
async def get_spy_arena_history(limit: int = Query(50, ge=10, le=200)):
    """Historique événements SPY Arena avec outcomes."""
    return _spy_arena._get_history(limit)


@app.get("/api/spy/arena/leaderboard")
async def get_spy_arena_leaderboard():
    """Leaderboard SPY Arena : WR / EV / PF par moteur."""
    history = _spy_arena._get_history(500)
    return {"leaderboard": _spy_arena._build_leaderboard(history)}


@app.get("/api/multi_index")
async def get_multi_index():
    """Phase 8 — QQQ/IWM/DIA/GLD/TLT : market breadth + performance relative."""
    cache = _multi_idx.get_cache()
    stale = _multi_idx.is_stale(7200)

    ticker_labels = {
        "qqq": {"name": "QQQ", "desc": "Nasdaq 100"},
        "iwm": {"name": "IWM", "desc": "Russell 2000"},
        "dia": {"name": "DIA", "desc": "Dow Jones"},
        "gld": {"name": "GLD", "desc": "Gold"},
        "tlt": {"name": "TLT", "desc": "Long Bonds"},
    }

    tickers = {}
    for key, meta in ticker_labels.items():
        data = cache.get(key) or {}
        tickers[key] = {
            **meta,
            "price":        data.get("price"),
            "change_1d":    data.get("change_1d"),
            "change_5d":    data.get("change_5d"),
            "change_1mo":   data.get("change_1mo"),
            "dist_52w_high": data.get("dist_52w_high"),
            "dist_52w_low":  data.get("dist_52w_low"),
            "rel_spy_1d":   data.get("rel_spy_1d"),
            "rel_spy_5d":   data.get("rel_spy_5d"),
        }

    raw_breadth = cache.get("breadth") or {}
    breadth = {
        "breadth_score":    raw_breadth.get("score"),
        "breadth_label":    raw_breadth.get("label"),
        "n_up_1d":          raw_breadth.get("n_up_1d"),
        "n_down_1d":        raw_breadth.get("n_down_1d"),
        "strongest_1d":     raw_breadth.get("strongest_1d"),
        "weakest_1d":       raw_breadth.get("weakest_1d"),
        "risk_on_score":    raw_breadth.get("risk_on_score"),
        "risk_on_label":    raw_breadth.get("risk_on_label"),
        "interpretation":   raw_breadth.get("interpretation"),
    }

    return {
        "stale":        stale,
        "last_updated": cache.get("last_updated"),
        "tickers":      tickers,
        "breadth":      breadth,
    }


@app.get("/api/multi_index/history")
async def get_multi_index_history(
    ticker: str = Query("qqq"),
    limit: int = Query(100, ge=10, le=500)
):
    """Historique d'un ticker multi-index."""
    allowed = {"qqq", "iwm", "dia", "gld", "tlt"}
    if ticker not in allowed:
        return {"error": f"ticker must be one of {allowed}"}
    return _multi_idx.get_multi_index_history(ticker=ticker, limit=limit)


@app.get("/health")
async def health():
    return {"status": "ok"}
