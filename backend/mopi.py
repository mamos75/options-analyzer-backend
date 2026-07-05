"""
MOPI — Mamos Options Pressure Index (0-100).
Score synthétique exclusif : GEX + IV Rank + Put/Call ratio + Squeeze probability.

0-25  → BEARISH PRESSURE (rouge)
25-45 → BEARISH NEUTRE
45-55 → NEUTRE
55-75 → BULLISH NEUTRE
75-100 → BULLISH PRESSURE (vert)
"""

from dataclasses import dataclass, field
from typing import List, Optional
from .deribit_client import OptionData, MarketSnapshot
from .gex import GEXProfile, _compute_dte

# Cap near-term statique (fallback bootstrap) — remplacé dynamiquement après 48 points d'historique
_NEAR_GEX_CAP = 500_000_000


@dataclass
class MOPIScore:
    score: float        # 0-100
    label: str          # BEARISH / NEUTRE / BULLISH
    emoji: str
    gex_component: float
    iv_rank_component: float
    pc_ratio_component: float
    squeeze_component: float
    iv_rank: float
    pc_ratio: float           # backward compat = pc_ratio_global
    squeeze_prob: float
    pc_ratio_global: float = 0.0         # structure totale toutes expiries, non pondéré
    pc_ratio_near: float = 0.0           # DTE ≤ 14j, non pondéré — pression immédiate brute
    pc_ratio_mid: float = 0.0            # 15-45j, non pondéré
    pc_ratio_far: float = 0.0            # >45j, non pondéré
    pc_ratio_weighted: float = 0.0       # toutes expiries, 1/sqrt(DTE) — synthèse MOPI V2
    pc_ratio_institutional: float = 0.0  # backward compat — expiry OI max
    dominant_expiry: dict = field(default_factory=dict)  # expiry dominant dans pcr_weighted
    # Observabilité cap near GEX
    gex_near_cap: float = 500_000_000
    cap_mode: str = "static/bootstrap"
    saturation_rate_7d: Optional[float] = None


def compute_mopi(
    snapshot: MarketSnapshot,
    gex_profile: GEXProfile,
    iv_history_90d: List[float],  # historique IV 90 jours pour calculer le rank
    gex_near_cap: float = _NEAR_GEX_CAP,
    cap_mode: str = "static/bootstrap",
    saturation_rate_7d: Optional[float] = None,
) -> MOPIScore:
    # --- Composante GEX (0-100) ---
    # Near-term gamma effectif uniquement — far-term ne pilote jamais le score MOPI
    gex_score = _normalize_gex_near(gex_profile.gex_near, cap=gex_near_cap)

    # --- Composante IV Rank (0-100) ---
    # IV rank élevé → options chères → pression → bearish signal
    current_iv = _average_atm_iv(snapshot, gex_profile.btc_price)
    iv_rank = _compute_iv_rank(current_iv, iv_history_90d)
    # IV rank élevé = marché craintif = bearish → inverser pour score bullish
    iv_score = 100 - iv_rank

    # --- Composante Put/Call Ratio (0-100) ---
    # global      = structure totale, non pondéré (backward compat)
    # near        = ≤14 DTE, non pondéré — pression immédiate brute
    # mid         = 15-45 DTE, non pondéré
    # far         = >45 DTE, non pondéré
    # weighted    = toutes expiries, 1/sqrt(DTE) — synthèse robuste → MOPI V2
    # inst        = expiry OI max → backward compat
    pc_ratio_global = _compute_pc_ratio(snapshot.options)
    pc_ratio_near = _compute_pc_ratio_near(snapshot.options)
    pc_ratio_mid = _compute_pc_ratio_mid(snapshot.options)
    pc_ratio_far = _compute_pc_ratio_far(snapshot.options)
    pc_ratio_weighted = _compute_pc_ratio_weighted(snapshot.options)
    pc_ratio_inst = _compute_pc_ratio_institutional(snapshot.options)
    dominant = _compute_dominant_expiry(snapshot.options)
    pc_score = _pc_ratio_to_score(pc_ratio_weighted)

    # --- Composante Squeeze Probability (0-100) ---
    squeeze_prob = _estimate_squeeze_prob(gex_profile, snapshot)
    squeeze_score = squeeze_prob  # déjà 0-100

    # Pondération
    weights = {"gex": 0.40, "iv": 0.25, "pc": 0.20, "squeeze": 0.15}
    score = (
        weights["gex"] * gex_score
        + weights["iv"] * iv_score
        + weights["pc"] * pc_score
        + weights["squeeze"] * squeeze_score
    )
    score = max(0, min(100, score))

    label, emoji = _classify_mopi(score)

    return MOPIScore(
        score=round(score, 1),
        label=label,
        emoji=emoji,
        gex_component=round(gex_score, 1),
        iv_rank_component=round(iv_score, 1),
        pc_ratio_component=round(pc_score, 1),
        squeeze_component=round(squeeze_score, 1),
        iv_rank=round(iv_rank, 1),
        pc_ratio=round(pc_ratio_global, 3),          # backward compat
        squeeze_prob=round(squeeze_prob, 1),
        pc_ratio_global=round(pc_ratio_global, 3),
        pc_ratio_near=round(pc_ratio_near, 3),
        pc_ratio_mid=round(pc_ratio_mid, 3),
        pc_ratio_far=round(pc_ratio_far, 3),
        pc_ratio_weighted=round(pc_ratio_weighted, 3),
        pc_ratio_institutional=round(pc_ratio_inst, 3),
        dominant_expiry=dominant,
        gex_near_cap=gex_near_cap,
        cap_mode=cap_mode,
        saturation_rate_7d=saturation_rate_7d,
    )


def _normalize_gex(total_gex: float) -> float:
    # Cap calibré sur l'ordre de grandeur réel du GEX BTC (milliards, pas millions)
    cap = 5_000_000_000
    normalized = (total_gex / cap + 1) / 2 * 100
    return max(0, min(100, normalized))


def _normalize_gex_near(gex_near: float, cap: float = _NEAR_GEX_CAP) -> float:
    normalized = (gex_near / cap + 1) / 2 * 100
    return max(0, min(100, normalized))


def get_atm_iv(snapshot: MarketSnapshot, btc_price: float) -> float:
    """ATM IV brut (±5% du spot) — à stocker dans iv_history_cache pour IV Rank réel."""
    return _average_atm_iv(snapshot, btc_price)


def _average_atm_iv(snapshot: MarketSnapshot, spot: float, window_pct: float = 0.05) -> float:
    atm_ivs = [
        o.iv for o in snapshot.options
        if abs(o.strike - spot) / spot <= window_pct and o.iv > 0
    ]
    return sum(atm_ivs) / len(atm_ivs) if atm_ivs else 60.0


def _compute_iv_rank(current_iv: float, history: List[float]) -> float:
    if not history:
        return 50.0
    low, high = min(history), max(history)
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (current_iv - low) / (high - low) * 100))


def _compute_pc_ratio(options: List[OptionData]) -> float:
    put_oi = sum(o.oi for o in options if o.option_type == "put")
    call_oi = sum(o.oi for o in options if o.option_type == "call")
    return put_oi / call_oi if call_oi > 0 else 1.0


def _compute_pc_ratio_near(options: List[OptionData], max_dte: int = 14) -> float:
    """PCR court terme : DTE ≤ 14j, non pondéré — pression immédiate brute.
    Fallback global si aucune option near-term ou call_oi == 0.
    """
    puts = 0.0
    calls = 0.0
    for o in options:
        dte = _compute_dte(o.expiry)
        if dte <= 0 or dte > max_dte:
            continue
        if o.option_type == "put":
            puts += o.oi
        else:
            calls += o.oi
    if calls == 0:
        return _compute_pc_ratio(options)
    return puts / calls


def _compute_pc_ratio_mid(options: List[OptionData]) -> float:
    """PCR moyen terme : 15j ≤ DTE ≤ 45j, non pondéré.
    Fallback global si aucune option mid ou call_oi == 0.
    """
    puts = 0.0
    calls = 0.0
    for o in options:
        dte = _compute_dte(o.expiry)
        if dte < 15 or dte > 45:
            continue
        if o.option_type == "put":
            puts += o.oi
        else:
            calls += o.oi
    if calls == 0:
        return _compute_pc_ratio(options)
    return puts / calls


def _compute_pc_ratio_far(options: List[OptionData]) -> float:
    """PCR long terme : DTE > 45j, non pondéré.
    Fallback global si aucune option far ou call_oi == 0.
    """
    puts = 0.0
    calls = 0.0
    for o in options:
        dte = _compute_dte(o.expiry)
        if dte <= 45:
            continue
        if o.option_type == "put":
            puts += o.oi
        else:
            calls += o.oi
    if calls == 0:
        return _compute_pc_ratio(options)
    return puts / calls


def _compute_pc_ratio_weighted(options: List[OptionData]) -> float:
    """PCR pondéré : toutes expiries avec weight = 1/sqrt(max(DTE,1)).
    DTE=0 exclu. Synthèse robuste : les expiries proches pèsent plus sans dominer.
    Fallback global si aucune option valide ou call_w == 0.
    """
    put_w = 0.0
    call_w = 0.0
    for o in options:
        dte = _compute_dte(o.expiry)
        if dte <= 0:
            continue
        w = 1.0 / (max(dte, 1) ** 0.5)
        if o.option_type == "put":
            put_w += o.oi * w
        else:
            call_w += o.oi * w
    if call_w == 0:
        return _compute_pc_ratio(options)
    return put_w / call_w


def _compute_dominant_expiry(options: List[OptionData]) -> dict:
    """Expiry qui contribue le plus au PCR pondéré (par OI total pondéré).
    Retourne: {expiry, dte, weight, oi_contribution_pct}
    """
    contrib: dict = {}
    total_w = 0.0
    for o in options:
        dte = _compute_dte(o.expiry)
        if dte <= 0:
            continue
        w = 1.0 / (max(dte, 1) ** 0.5)
        contrib[o.expiry] = contrib.get(o.expiry, 0.0) + o.oi * w
        total_w += o.oi * w
    if not contrib or total_w == 0:
        return {}
    dominant = max(contrib, key=lambda e: contrib[e])
    dom_dte = _compute_dte(dominant)
    return {
        "expiry": dominant,
        "dte": dom_dte,
        "weight": round(1.0 / (max(dom_dte, 1) ** 0.5), 4),
        "oi_contribution_pct": round(contrib[dominant] / total_w * 100, 1),
    }


def _compute_pc_ratio_institutional(options: List[OptionData]) -> float:
    """PCR institutionnel : uniquement l'expiry avec le plus grand OI total (DTE > 0).

    Révèle la structure de positionnement longue, ignorant la pression court terme.
    Fallback global si aucune option valide.
    """
    oi_by_expiry: dict[str, float] = {}
    for o in options:
        if _compute_dte(o.expiry) <= 0:
            continue
        oi_by_expiry[o.expiry] = oi_by_expiry.get(o.expiry, 0.0) + o.oi
    if not oi_by_expiry:
        return _compute_pc_ratio(options)
    top_expiry = max(oi_by_expiry, key=lambda e: oi_by_expiry[e])
    return _compute_pc_ratio([o for o in options if o.expiry == top_expiry])


def _pc_ratio_to_score(pc: float) -> float:
    # PC > 1.5 → contrarian bullish → score 70+
    # PC < 0.5 → contrarian bearish → score 30-
    if pc >= 1.5:
        return min(100, 70 + (pc - 1.5) * 20)
    elif pc <= 0.5:
        return max(0, 30 - (0.5 - pc) * 20)
    # Linéaire entre 0.5 et 1.5
    return 30 + (pc - 0.5) / 1.0 * 40


def _estimate_squeeze_prob(profile: GEXProfile, snapshot: MarketSnapshot) -> float:
    spot = profile.btc_price
    prob = 50.0

    # Near-term gamma effectif uniquement — far-term n'amplifie pas le squeeze immédiat
    # Diviseurs ~10x plus petits que pour total_gex (magnitude near-term réduite)
    if profile.gex_near < 0:
        prob += min(30, abs(profile.gex_near) / 16_700_000)
    else:
        prob -= min(20, profile.gex_near / 25_000_000)

    # Prix proche du flip level → tension maximale
    if profile.flip_level is not None:
        distance = abs(spot - profile.flip_level) / spot
        if distance < 0.02:
            prob += 20
        elif distance < 0.05:
            prob += 10

    return max(0, min(100, prob))


def _classify_mopi(score: float):
    if score >= 75:
        return "Pression haussière forte", "🟢"
    elif score >= 55:
        return "Légère avance acheteurs", "🟩"
    elif score >= 45:
        return "Forces équilibrées", "🟡"
    elif score >= 25:
        return "Légère avance vendeurs", "🟧"
    return "Pression baissière forte", "🔴"


def mopi_summary(m: MOPIScore) -> str:
    if m.score >= 75:
        line1 = "Les acheteurs prennent progressivement le contrôle."
        line2 = "La pression options devient favorable à une accélération haussière."
        line3 = "Tant que cette situation continue, les probabilités restent orientées vers le haut."
    elif m.score >= 55:
        line1 = "Légère avance des acheteurs dans les options."
        line2 = "Les acheteurs ont un léger avantage, mais aucune direction n'est encore confirmée."
        line3 = "Une confirmation haussière viendrait d'un score qui continue de monter."
    elif m.score >= 45:
        line1 = "Forces équilibrées — ni les acheteurs ni les vendeurs ne dominent."
        line2 = "Le marché options ne donne pas encore de signal directionnel clair."
        line3 = "Attends une cassure du range avant de prendre position."
    elif m.score >= 25:
        line1 = "Légère avance des vendeurs dans les options."
        line2 = "Les vendeurs ont un léger avantage, sans signal d'accélération pour l'instant."
        line3 = "Surveille si la pression baissière continue d'augmenter — signal qui se confirme."
    else:
        line1 = "Les vendeurs dominent nettement les options."
        line2 = "La pression devient défavorable à une reprise haussière."
        line3 = "Risque d'accélération baissière si rien ne change dans les prochaines heures."
    return (
        f"{m.emoji} **Pression Options {m.score:.0f}/100**\n"
        f"{line1}\n"
        f"{line2}\n"
        f"{line3}"
    )
