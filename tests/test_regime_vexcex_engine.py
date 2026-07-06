"""
Tests regime_vexcex_engine.classify_regime_vexcex

Fixtures par groupe (EXP/FB/FL/COMP/DIV/MOD/NEU).
Règle critique : prod fixture (spot=62670, flip=65000, GEX=+4.66B) → jamais COMP-0.
"""
import sys
sys.path.insert(0, '/root/telegram-claude-bot/dashboard_options')

from backend.regime_vexcex_engine import classify_regime_vexcex, VexCexInputs


def _i(**kw) -> VexCexInputs:
    """Helper : construit VexCexInputs avec des defaults raisonnables."""
    defaults = dict(
        vex=0.0, cex=0.0, gex=0.0, dex=0.0, spot=65000.0,
        vex_direction=None, cex_direction=None,
        vex_trend="FLAT", cex_trend="FLAT",
        flip_level=None, flip_dist_pct=None,
        regime_meca="NEUTRE", regime_source="gex_estime",
        gex_flip_incoherent=False,
    )
    defaults.update(kw)
    return VexCexInputs(**defaults)


# ── NEU group ──────────────────────────────────────────────────────────────

def test_neu0_vex_neutral():
    r = classify_regime_vexcex(_i(
        vex=1e9, cex=1000.0,
        vex_direction="NEUTRAL",
        cex_direction="BULLISH_CHARM",
    ))
    assert r.regime_id == "NEU-0", f"expected NEU-0, got {r.regime_id}"
    assert r.phase == "NEU"
    assert r.urgency == "NEUTRE"


def test_neu0_cex_neutral():
    r = classify_regime_vexcex(_i(
        vex=1e9, cex=1000.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="NEUTRAL",
    ))
    assert r.regime_id == "NEU-0", f"expected NEU-0, got {r.regime_id}"


def test_neu0_both_neutral():
    r = classify_regime_vexcex(_i(
        vex=0.0, cex=0.0,
        vex_direction="NEUTRAL",
        cex_direction="NEUTRAL",
    ))
    assert r.regime_id == "NEU-0"


def test_neu1_no_direction_no_magnitude():
    r = classify_regime_vexcex(_i(
        vex=100.0, cex=10.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
    ))
    # Small magnitude → MOD-UP or NEU-1
    assert r.phase in ("MOD", "NEU"), f"unexpected phase {r.phase}"


# ── FL group ──────────────────────────────────────────────────────────────

def test_fl0_critical_flip():
    """Spot à 0.3% du flip → FL-0 CRITIQUE."""
    spot = 65000.0
    flip = 65200.0  # 0.31% au-dessus du spot
    dist_pct = (spot - flip) / spot * 100  # ≈ -0.31%
    r = classify_regime_vexcex(_i(
        vex=2e9, cex=2000.0,
        vex_direction="BEARISH_VANNA",
        cex_direction="BEARISH_CHARM",
        spot=spot, flip_level=flip,
        flip_dist_pct=dist_pct,
        regime_meca="AMPLIFICATEUR",
    ))
    assert r.regime_id == "FL-0", f"expected FL-0, got {r.regime_id}"
    assert r.urgency == "CRITIQUE"


def test_fl1_near_flip_big_magnitude():
    """Spot à 0.7% du flip avec magnitude VEX big → FL-1."""
    spot = 65000.0
    flip = 65455.0  # ~0.7%
    dist_pct = (spot - flip) / spot * 100  # ≈ -0.70%
    r = classify_regime_vexcex(_i(
        vex=600e6, cex=100.0,  # VEX big, CEX small
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        spot=spot, flip_level=flip,
        flip_dist_pct=dist_pct,
        regime_meca="STABILISANT",
    ))
    assert r.regime_id == "FL-1", f"expected FL-1, got {r.regime_id}"
    assert r.phase == "FL"


def test_comp6_near_flip_no_magnitude():
    """Flip near mais VEX et CEX petits → COMP-6 (pas COMP-0)."""
    spot = 65000.0
    flip = 65455.0  # ~0.7%
    dist_pct = (spot - flip) / spot * 100
    r = classify_regime_vexcex(_i(
        vex=100e6, cex=50.0,   # Small VEX + small CEX
        vex_direction="BULLISH_VANNA",
        cex_direction="BEARISH_CHARM",  # contradictoire mais faible
        spot=spot, flip_level=flip,
        flip_dist_pct=dist_pct,
        regime_meca="STABILISANT",
    ))
    assert r.regime_id == "COMP-6", f"expected COMP-6, got {r.regime_id}"


# ── EXP group ──────────────────────────────────────────────────────────────

def test_exp_up1_full_convergence():
    """VEX big + GEX AMP + DEX bull → EXP-UP-1."""
    r = classify_regime_vexcex(_i(
        vex=800e6, cex=600.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        dex=500e6,
        regime_meca="AMPLIFICATEUR",
    ))
    assert r.regime_id == "EXP-UP-1", f"expected EXP-UP-1, got {r.regime_id}"
    assert r.phase == "EXP"


def test_exp_down1_full_convergence():
    """VEX big bear + GEX AMP + DEX bear → EXP-DOWN-1."""
    r = classify_regime_vexcex(_i(
        vex=-800e6, cex=-600.0,
        vex_direction="BEARISH_VANNA",
        cex_direction="BEARISH_CHARM",
        dex=-500e6,
        regime_meca="AMPLIFICATEUR",
    ))
    assert r.regime_id == "EXP-DOWN-1", f"expected EXP-DOWN-1, got {r.regime_id}"


def test_exp_up0_no_dex():
    """VEX big + GEX AMP mais DEX neutral → EXP-UP-0 (partiel)."""
    r = classify_regime_vexcex(_i(
        vex=800e6, cex=600.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        dex=0.0,   # DEX neutre
        regime_meca="AMPLIFICATEUR",
    ))
    assert r.regime_id == "EXP-UP-0", f"expected EXP-UP-0, got {r.regime_id}"
    assert r.urgency == "MODÉRÉE"


def test_exp_critique_extreme_vex():
    """VEX extrême + GEX AMP + DEX → urgency CRITIQUE."""
    r = classify_regime_vexcex(_i(
        vex=3e9, cex=600.0,   # > 2B
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        dex=500e6,
        regime_meca="AMPLIFICATEUR",
    ))
    assert r.phase == "EXP"
    assert r.urgency == "CRITIQUE"


# ── FB group ──────────────────────────────────────────────────────────────

def test_fb_up():
    """VEX extrême + CEX bull → FB-UP."""
    r = classify_regime_vexcex(_i(
        vex=3e9, cex=2100.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        dex=0.0,   # pas de DEX → pas EXP
        regime_meca="STABILISANT",  # pas AMP → pas EXP
    ))
    assert r.regime_id == "FB-UP", f"expected FB-UP, got {r.regime_id}"


def test_fb_down():
    """VEX extrême baissier + CEX bear → FB-DOWN."""
    r = classify_regime_vexcex(_i(
        vex=-3e9, cex=-2100.0,
        vex_direction="BEARISH_VANNA",
        cex_direction="BEARISH_CHARM",
        dex=0.0,
        regime_meca="STABILISANT",
    ))
    assert r.regime_id == "FB-DOWN", f"expected FB-DOWN, got {r.regime_id}"


# ── COMP group ─────────────────────────────────────────────────────────────

def test_comp0_vex_bull_cex_bear():
    """VEX bull big + CEX bear big → COMP-0."""
    r = classify_regime_vexcex(_i(
        vex=700e6, cex=-700.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BEARISH_CHARM",
        regime_meca="STABILISANT",
    ))
    assert r.regime_id == "COMP-0", f"expected COMP-0, got {r.regime_id}"
    assert r.phase == "COMP"


def test_comp0_vex_bear_cex_bull():
    """VEX bear big + CEX bull big → COMP-0."""
    r = classify_regime_vexcex(_i(
        vex=-700e6, cex=700.0,
        vex_direction="BEARISH_VANNA",
        cex_direction="BULLISH_CHARM",
        regime_meca="STABILISANT",
    ))
    assert r.regime_id == "COMP-0", f"expected COMP-0, got {r.regime_id}"


def test_comp0_gate_no_magnitude():
    """VEX + CEX contradictoires mais PETITS → pas COMP-0 (gate)."""
    r = classify_regime_vexcex(_i(
        vex=100e6, cex=-50.0,   # sous les seuils big
        vex_direction="BULLISH_VANNA",
        cex_direction="BEARISH_CHARM",
        regime_meca="STABILISANT",
    ))
    # Ne doit PAS être COMP-0 — magnitude insuffisante
    assert r.regime_id != "COMP-0", f"COMP-0 ne doit pas déclencher sans magnitude"


# ── DIV group ──────────────────────────────────────────────────────────────

def test_div0_vex_bull_dex_bear():
    """VEX bull big + DEX bear → DIV-0."""
    r = classify_regime_vexcex(_i(
        vex=700e6, cex=700.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        dex=-500e6,   # DEX contre VEX
        regime_meca="NEUTRE",  # pas AMP → pas EXP
    ))
    assert r.regime_id == "DIV-0", f"expected DIV-0, got {r.regime_id}"


# ── MOD group ──────────────────────────────────────────────────────────────

def test_mod_up_coherent():
    """VEX bull + CEX bull mais petits → MOD-UP."""
    r = classify_regime_vexcex(_i(
        vex=100e6, cex=100.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        regime_meca="NEUTRE",
    ))
    assert r.regime_id == "MOD-UP", f"expected MOD-UP, got {r.regime_id}"
    assert r.phase == "MOD"


def test_mod_down_coherent():
    """VEX bear + CEX bear mais petits → MOD-DOWN."""
    r = classify_regime_vexcex(_i(
        vex=-100e6, cex=-100.0,
        vex_direction="BEARISH_VANNA",
        cex_direction="BEARISH_CHARM",
        regime_meca="NEUTRE",
    ))
    assert r.regime_id == "MOD-DOWN", f"expected MOD-DOWN, got {r.regime_id}"


def test_mod_mix():
    """VEX bull + CEX bear sans magnitude → MOD-MIX."""
    r = classify_regime_vexcex(_i(
        vex=100e6, cex=-50.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BEARISH_CHARM",
        regime_meca="NEUTRE",
    ))
    assert r.regime_id == "MOD-MIX", f"expected MOD-MIX, got {r.regime_id}"


# ── Règle critique : fixture prod ─────────────────────────────────────────

def test_prod_fixture_never_comp0():
    """
    Fixture prod du 06/07/2026 :
      spot=62670, flip=65000, GEX=+4.66B (incoherent=True)
      VEX et CEX faibles (zone mort probable à l'époque).

    Règle : avec VEX neutre OU CEX neutre → jamais COMP-0.
    """
    # Scenario 1 : VEX neutre (le plus probable en prod)
    r1 = classify_regime_vexcex(_i(
        vex=4.66e9, cex=100.0,
        vex_direction="NEUTRAL",  # zone morte
        cex_direction="BULLISH_CHARM",
        spot=62670.0,
        flip_level=65000.0,
        flip_dist_pct=(62670 - 65000) / 62670 * 100,  # ≈ -3.72%
        regime_meca="AMPLIFICATEUR",
        gex_flip_incoherent=True,
    ))
    assert r1.regime_id != "COMP-0", f"COMP-0 interdit avec VEX NEUTRAL — got {r1.regime_id}"
    assert r1.regime_id == "NEU-0", f"expected NEU-0, got {r1.regime_id}"

    # Scenario 2 : VEX + CEX ont des directions, magnitudes petites
    dist_pct = (62670 - 65000) / 62670 * 100  # ≈ -3.72%, pas near flip
    r2 = classify_regime_vexcex(_i(
        vex=200e6, cex=-50.0,   # VEX sous _VEX_BIG_THRESH (500M)
        vex_direction="BULLISH_VANNA",
        cex_direction="BEARISH_CHARM",  # contradictoire
        spot=62670.0,
        flip_level=65000.0,
        flip_dist_pct=dist_pct,
        regime_meca="AMPLIFICATEUR",
        gex_flip_incoherent=True,
    ))
    # Magnitude insuffisante → pas COMP-0 (gate)
    assert r2.regime_id != "COMP-0", f"COMP-0 interdit sans magnitude — got {r2.regime_id}"


# ── Cohérence du dataclass ─────────────────────────────────────────────────

def test_output_fields():
    """Vérifie que tous les champs obligatoires sont présents."""
    r = classify_regime_vexcex(_i(
        vex=800e6, cex=600.0,
        vex_direction="BULLISH_VANNA",
        cex_direction="BULLISH_CHARM",
        dex=500e6,
        regime_meca="AMPLIFICATEUR",
    ))
    assert hasattr(r, "regime_id")
    assert hasattr(r, "phase")
    assert hasattr(r, "label")
    assert hasattr(r, "urgency")
    assert hasattr(r, "signals")
    assert hasattr(r, "magnitudes")
    assert hasattr(r, "trends")
    assert hasattr(r, "flip_context")
    assert isinstance(r.signals, list)
    assert isinstance(r.magnitudes, dict)
    assert isinstance(r.trends, dict)
    assert isinstance(r.flip_context, dict)


if __name__ == "__main__":
    tests = [
        test_neu0_vex_neutral,
        test_neu0_cex_neutral,
        test_neu0_both_neutral,
        test_neu1_no_direction_no_magnitude,
        test_fl0_critical_flip,
        test_fl1_near_flip_big_magnitude,
        test_comp6_near_flip_no_magnitude,
        test_exp_up1_full_convergence,
        test_exp_down1_full_convergence,
        test_exp_up0_no_dex,
        test_exp_critique_extreme_vex,
        test_fb_up,
        test_fb_down,
        test_comp0_vex_bull_cex_bear,
        test_comp0_vex_bear_cex_bull,
        test_comp0_gate_no_magnitude,
        test_div0_vex_bull_dex_bear,
        test_mod_up_coherent,
        test_mod_down_coherent,
        test_mod_mix,
        test_prod_fixture_never_comp0,
        test_output_fields,
    ]

    ok = 0
    fail = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL {t.__name__} — {e}")
            fail += 1
        except Exception as e:
            print(f"  ERR  {t.__name__} — {type(e).__name__}: {e}")
            fail += 1

    print(f"\n{ok}/{ok+fail} passed")
    import sys
    sys.exit(0 if fail == 0 else 1)
