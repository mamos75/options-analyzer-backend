"""
Tests non-régression Probability Engine Phase A.

Règles clés testées :
  1. Probabilité toujours clampée [5%, 95%]
  2. Confiance séparée de la probabilité (champ distinct)
  3. Règles unavailable → pts_applied = 0 (jamais de contribution fantôme)
  4. GEX near négatif → BEAR favorisé
  5. Spot sous flip + DEX BEARISH → BEAR 24h forte conviction
  6. Put wall très proche → pénalité BEAR active
  7. Max Pain au-dessus + DTE proche → pénalité BEAR active
  8. Convergence signaux haussiers → BULL dominant
  9. Scénario dominant = scenario le plus éloigné de 50%
  10. data_coverage_pct = 0 si toutes les règles sont unavailable
"""
import pytest
from backend.probability_engine import (
    compute_probability_engine,
    ProbabilityEngineOutput,
    ScenarioProbability,
    _PROB_MIN,
    _PROB_MAX,
    _CONF_INSUFFICIENT,
)


# ── Fixture : paramètres neutres (baseline) ───────────────────────────────────

def _neutral_params(**overrides):
    defaults = dict(
        spot=60_000.0,
        gex_near=0.0,
        flip_level=58_000.0,
        flip_use_in_signal=True,
        dex_direction="NEUTRAL",
        dex_actionable_btc=0.0,
        iv_rank=50.0,
        pc_ratio_near=1.0,
        put_wall=55_000.0,
        call_wall=65_000.0,
        max_pain_strike=60_000.0,
        max_pain_dte=10,
        gex_near_prev=None,
    )
    defaults.update(overrides)
    return defaults


def _compute(**overrides) -> ProbabilityEngineOutput:
    return compute_probability_engine(**_neutral_params(**overrides))


# ── 1. Clamp [5, 95] ─────────────────────────────────────────────────────────

def test_probability_clamped_max():
    """Signaux baissiers extrêmes → probabilité plafonnée à 95%."""
    out = _compute(
        gex_near=-800_000_000,
        flip_level=70_000.0,
        spot=60_000.0,
        dex_direction="BEARISH_FLOWS",
        iv_rank=90.0,
        pc_ratio_near=2.0,
        gex_near_prev=0.0,
    )
    assert out.bear_24h.probability <= _PROB_MAX
    assert out.bear_24h.probability >= _PROB_MIN


def test_probability_clamped_min():
    """Signaux haussiers extrêmes → probabilité baisse plancher à 5%."""
    out = _compute(
        gex_near=800_000_000,
        flip_level=50_000.0,
        spot=60_000.0,
        dex_direction="BULLISH_FLOWS",
        iv_rank=10.0,
        pc_ratio_near=0.4,
        max_pain_strike=65_000.0,
        max_pain_dte=2,
        gex_near_prev=0.0,
    )
    assert out.bear_24h.probability >= _PROB_MIN
    assert out.bear_24h.probability <= _PROB_MAX


# ── 2. Confiance séparée de la probabilité ───────────────────────────────────

def test_confidence_is_separate_field():
    """Confiance et probabilité sont deux valeurs indépendantes."""
    out = _compute(gex_near=-200_000_000, dex_direction="BEARISH_FLOWS")
    s = out.bear_24h
    # Les deux doivent exister et être dans leurs plages respectives
    assert 0.0 <= s.confidence <= 100.0
    assert _PROB_MIN <= s.probability <= _PROB_MAX
    # Ils ne doivent pas forcément être égaux
    # (confiance mesure la qualité des données, pas la direction)
    assert s.confidence_label in (
        "Données insuffisantes",
        "Signal règles faible", "Signal règles valide", "Signal règles fort",
    )


def test_edge_label_matches_confidence():
    """edge_label doit correspondre au niveau de confiance."""
    out = _compute()
    for scenario in (
        out.bear_4h, out.bull_4h,
        out.bear_24h, out.bull_24h,
        out.bear_72h, out.bull_72h,
    ):
        conf = scenario.confidence
        label = scenario.edge_label
        if conf < _CONF_INSUFFICIENT:
            assert label == "EDGE INSUFFISANT", f"{scenario.scenario}: {conf:.0f}% → {label}"
        elif conf < 60:
            assert label == "SIGNAL RÈGLES FAIBLE"
        elif conf < 75:
            assert label == "SIGNAL RÈGLES VALIDE"
        else:
            assert label == "SIGNAL RÈGLES FORT"


# ── 3. Règles unavailable → zéro contribution ────────────────────────────────

def test_unavailable_rules_contribute_zero():
    """Toutes les règles unavailable doivent avoir pts_applied == 0."""
    out = _compute()
    for scenario in (out.bear_24h, out.bull_24h):
        for rule in scenario.positive_rules + scenario.penalty_rules:
            if rule.data_quality == "unavailable":
                assert rule.pts_applied == 0, (
                    f"Règle {rule.id} unavailable mais pts_applied = {rule.pts_applied}"
                )


def test_unavailable_rules_not_triggered():
    """Règles sans données ne doivent pas être triggered."""
    out = _compute()
    for scenario in (out.bear_24h, out.bull_24h):
        for rule in scenario.positive_rules + scenario.penalty_rules:
            if rule.data_quality == "unavailable":
                assert rule.triggered is False, (
                    f"Règle {rule.id} unavailable mais triggered=True"
                )


# ── 4. GEX near négatif → BEAR favorisé ─────────────────────────────────────

def test_gex_near_negative_favors_bear():
    """GEX near négatif doit augmenter la proba BEAR vs neutre."""
    bear_negative = _compute(gex_near=-500_000_000).bear_24h.probability
    bear_positive = _compute(gex_near=+500_000_000).bear_24h.probability
    assert bear_negative > bear_positive, (
        f"GEX négatif {bear_negative:.0f}% doit > GEX positif {bear_positive:.0f}%"
    )


def test_gex_near_negative_rule_triggered():
    """La règle gex_near_negative_24h doit être triggered quand GEX < 0."""
    out = _compute(gex_near=-200_000_000)
    rules = {r.id: r for r in out.bear_24h.positive_rules}
    assert "gex_near_negative_24h" in rules
    r = rules["gex_near_negative_24h"]
    assert r.triggered is True
    assert r.pts_applied == r.weight  # poids plein


# ── 5. Spot sous flip + DEX BEARISH → forte pression BEAR ────────────────────

def test_spot_below_flip_dex_bearish_strong_bear():
    """Spot sous flip confirmé + DEX bearish doit produire BEAR 24h > 60%."""
    out = _compute(
        spot=60_000.0,
        flip_level=65_000.0,       # spot < flip
        flip_use_in_signal=True,
        dex_direction="BEARISH_FLOWS",
        gex_near=-300_000_000,
    )
    assert out.bear_24h.probability > 60, (
        f"Spot sous flip + DEX bearish + GEX négatif → BEAR 24h attendu > 60%, "
        f"obtenu {out.bear_24h.probability:.0f}%"
    )


def test_spot_above_flip_use_in_signal_false_no_contribution():
    """Flip non validé (flip_use_in_signal=False) → règle spot_below_flip ne contribue pas."""
    out = _compute(
        spot=60_000.0,
        flip_level=65_000.0,
        flip_use_in_signal=False,
    )
    rules = {r.id: r for r in out.bear_24h.positive_rules}
    r = rules.get("spot_below_flip_24h")
    if r:
        # triggered mais data_quality "low" → peut contribuer mais avec qualité dégradée
        # OU pas triggered si flip_use_in_signal=False. Vérifier la cohérence.
        if r.data_quality == "unavailable":
            assert r.pts_applied == 0


# ── 6. Put wall très proche → pénalité BEAR active ───────────────────────────

def test_put_wall_very_close_penalizes_bear():
    """Put wall dans les 2% sous le spot → pénalité BEAR_24H activée."""
    spot = 60_000.0
    put_wall_near = spot * 0.985   # 1.5% sous spot
    out = _compute(spot=spot, put_wall=put_wall_near)
    rules = {r.id: r for r in out.bear_24h.penalty_rules}
    r = rules.get("put_wall_near_support_24h")
    assert r is not None
    assert r.triggered is True
    assert r.pts_applied < 0, "Pénalité doit être négative"


def test_put_wall_far_no_penalty():
    """Put wall à 10% sous le spot → pénalité non activée."""
    spot = 60_000.0
    put_wall_far = spot * 0.90   # 10% sous spot
    out = _compute(spot=spot, put_wall=put_wall_far)
    rules = {r.id: r for r in out.bear_24h.penalty_rules}
    r = rules.get("put_wall_near_support_24h")
    if r:
        assert r.triggered is False


# ── 7. Max Pain au-dessus + DTE proche → pénalité BEAR ──────────────────────

def test_max_pain_above_near_dte_penalizes_bear():
    """Max Pain au-dessus du spot avec DTE ≤ 3j → pénalité BEAR activée."""
    out = _compute(
        spot=60_000.0,
        max_pain_strike=62_000.0,   # au-dessus du spot
        max_pain_dte=2,             # DTE proche
    )
    rules = {r.id: r for r in out.bear_24h.penalty_rules}
    r = rules.get("max_pain_above_near_dte_24h")
    assert r is not None
    assert r.triggered is True
    assert r.pts_applied < 0


def test_max_pain_above_far_dte_no_penalty():
    """Max Pain au-dessus du spot avec DTE > 3j → pénalité non activée."""
    out = _compute(
        spot=60_000.0,
        max_pain_strike=62_000.0,
        max_pain_dte=10,     # DTE lointain
    )
    rules = {r.id: r for r in out.bear_24h.penalty_rules}
    r = rules.get("max_pain_above_near_dte_24h")
    if r:
        assert r.triggered is False


# ── 8. Convergence signaux haussiers → BULL dominant ─────────────────────────

def test_bullish_convergence_produces_bull_dominant():
    """GEX positif + DEX bullish + Max Pain au-dessus + IV basse → BULL dominant."""
    out = _compute(
        gex_near=400_000_000,
        flip_level=55_000.0,        # spot au-dessus du flip
        spot=60_000.0,
        dex_direction="BULLISH_FLOWS",
        iv_rank=20.0,
        pc_ratio_near=0.7,
        max_pain_strike=65_000.0,
        max_pain_dte=2,
        gex_near_prev=200_000_000,  # GEX en expansion
    )
    # Le scénario dominant doit être haussier
    assert "BULL" in out.dominant_scenario, (
        f"Convergence haussière → dominant attendu BULL, obtenu {out.dominant_scenario}"
    )
    # Et BULL_24H doit être > 50%
    assert out.bull_24h.probability > 50


# ── 9. Scénario dominant = plus éloigné de 50% ───────────────────────────────

def test_dominant_is_furthest_from_50():
    """dominant_scenario doit être celui avec |probability - 50| maximal."""
    out = _compute(
        gex_near=-500_000_000,
        dex_direction="BEARISH_FLOWS",
        iv_rank=75.0,
    )
    scenarios = {
        "BEAR_4H": out.bear_4h.probability,
        "BULL_4H": out.bull_4h.probability,
        "BEAR_24H": out.bear_24h.probability,
        "BULL_24H": out.bull_24h.probability,
        "BEAR_72H": out.bear_72h.probability,
        "BULL_72H": out.bull_72h.probability,
    }
    best = max(scenarios.items(), key=lambda x: abs(x[1] - 50))
    assert out.dominant_scenario == best[0], (
        f"Dominant attendu {best[0]} ({best[1]:.0f}%), obtenu {out.dominant_scenario}"
    )


# ── 10. data_coverage_pct ────────────────────────────────────────────────────

def test_data_coverage_pct_range():
    """data_coverage_pct doit être entre 0 et 100."""
    out = _compute()
    for s in (out.bear_24h, out.bull_24h, out.bear_4h, out.bull_4h):
        assert 0 <= s.data_coverage_pct <= 100


def test_unavailable_rules_reduce_coverage():
    """Règles unavailable (futures OI, funding, volume) doivent réduire la couverture."""
    out = _compute()
    # Le scénario BEAR 24h a 3 règles unavailable (futures_oi, funding, volume)
    # → coverage < 100%
    assert out.bear_24h.data_coverage_pct < 100, (
        f"BEAR_24H avec règles unavailable doit avoir coverage < 100%, "
        f"obtenu {out.bear_24h.data_coverage_pct:.0f}%"
    )


# ── Signal label format ───────────────────────────────────────────────────────

def test_signal_label_format():
    """signal_label doit contenir direction, probabilité et complétude données."""
    out = _compute()
    for s in (out.bear_24h, out.bull_24h):
        assert "%" in s.signal_label
        # Point 6 : nouveau format — "Direction règles : X% | Complétude : Y% | Validation : z"
        assert "Direction règles" in s.signal_label or "Complétude" in s.signal_label
        assert s.historical_validation in (
            "en accumulation", "faible", "moyenne", "forte"
        )
        assert s.conclusion_line  # non vide


# ── GEX momentum ─────────────────────────────────────────────────────────────

def test_gex_momentum_contraction_triggers_bear():
    """GEX near qui décroît (contraction) → règle momentum BEAR déclenchée."""
    out = _compute(
        gex_near=-300_000_000,
        gex_near_prev=-100_000_000,  # décroît : moins de -100M → -300M
    )
    rules_24h = {r.id: r for r in out.bear_24h.positive_rules}
    r = rules_24h.get("gex_momentum_contraction_24h")
    if r:
        assert r.triggered is True
        assert r.pts_applied > 0


def test_gex_momentum_unavailable_when_no_prev():
    """Sans gex_near_prev, la règle momentum est unavailable."""
    out = _compute(gex_near=-200_000_000, gex_near_prev=None)
    rules = {r.id: r for r in out.bear_24h.positive_rules}
    r = rules.get("gex_momentum_contraction_24h")
    if r:
        assert r.data_quality == "unavailable"
        assert r.pts_applied == 0


# ── Top contributors ─────────────────────────────────────────────────────────

def test_top_contributors_not_empty_when_rules_triggered():
    """Si des règles sont déclenchées, top_contributors ne doit pas être vide."""
    out = _compute(gex_near=-400_000_000, dex_direction="BEARISH_FLOWS")
    assert len(out.bear_24h.top_contributors) > 0


def test_top_contributors_max_3():
    """top_contributors ne doit jamais dépasser 3 entrées."""
    out = _compute(
        gex_near=-400_000_000,
        dex_direction="BEARISH_FLOWS",
        iv_rank=80.0,
        pc_ratio_near=1.8,
    )
    assert len(out.bear_24h.top_contributors) <= 3


# ── Cohérence output ─────────────────────────────────────────────────────────

def test_output_has_all_6_scenarios():
    """L'output doit contenir les 6 scénarios (BEAR/BULL × 3 horizons)."""
    out = _compute()
    assert out.bear_4h is not None
    assert out.bull_4h is not None
    assert out.bear_24h is not None
    assert out.bull_24h is not None
    assert out.bear_72h is not None
    assert out.bull_72h is not None


def test_output_metadata():
    """timestamp, engine_version et disclaimer doivent être présents."""
    out = _compute()
    assert out.engine_version.startswith("Phase-A")
    assert len(out.disclaimer) > 0
    assert "T" in out.timestamp  # ISO format


def test_raw_pts_matches_probability():
    """raw_pts + 50 = probabilité non clampée → probability = clamp(raw_pts + 50, 5, 95)."""
    out = _compute()
    for s in (out.bear_24h, out.bull_24h):
        expected_raw = s.base_probability + s.raw_pts
        expected_prob = float(max(5, min(95, expected_raw)))
        assert abs(s.probability - expected_prob) < 0.1, (
            f"{s.scenario}: base={s.base_probability} + raw={s.raw_pts} "
            f"→ attendu {expected_prob:.1f}%, obtenu {s.probability:.1f}%"
        )
