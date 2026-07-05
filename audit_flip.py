"""
AUDIT FLIP LEVEL — script standalone via REST Deribit
Run: cd /root/telegram-claude-bot && python3 dashboard_options/audit_flip.py
"""

import json
import urllib.request
import urllib.parse
import sys

DERIBIT_REST = "https://www.deribit.com/api/v2"
CONTRACT_SIZE = 1.0


def rest_call(method, params=None):
    url = f"{DERIBIT_REST}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())["result"]


def main():
    print("Fetching BTC spot price...")
    ticker = rest_call("public/get_index_price", {"index_name": "btc_usd"})
    spot = float(ticker["index_price"])
    print(f"Spot BTC: ${spot:,.0f}")

    print("Fetching instruments...")
    instruments = rest_call("public/get_instruments", {
        "currency": "BTC",
        "kind": "option",
        "expired": "false"
    })
    print(f"Total instruments: {len(instruments)}")

    # ─── Fetch summaries batch ────────────────────────────────────────────────
    # On prend TOUS les instruments (comme le client principal)
    # Deribit permet get_book_summary_by_currency pour tout récupérer d'un coup
    print("Fetching book summaries (all options)...")
    summaries = rest_call("public/get_book_summary_by_currency", {
        "currency": "BTC",
        "kind": "option",
    })
    print(f"Summaries récupérées: {len(summaries)}")

    # Map instrument → summary
    summary_map = {s["instrument_name"]: s for s in summaries}

    # ─── Parse options ────────────────────────────────────────────────────────
    options = []
    missing_gamma = 0
    for inst in instruments:
        name = inst["instrument_name"]
        parts = name.split("-")
        if len(parts) != 4:
            continue
        _, expiry, strike_str, opt_type = parts
        try:
            strike = float(strike_str)
        except ValueError:
            continue
        opt_type_clean = "call" if opt_type.upper() == "C" else "put"

        s = summary_map.get(name, {})
        oi = float(s.get("open_interest", 0) or 0)
        gamma = float(s.get("greeks", {}).get("gamma", 0) if s.get("greeks") else 0)
        iv = float(s.get("mark_iv", 0) or 0)

        # Si greeks absent du summary, on skippe pour le GEX (gamma=0)
        if gamma == 0:
            missing_gamma += 1

        options.append({
            "name": name,
            "strike": strike,
            "expiry": expiry,
            "type": opt_type_clean,
            "oi": oi,
            "gamma": gamma,
            "iv": iv,
        })

    print(f"Options parsées: {len(options)} ({missing_gamma} sans gamma dans summary)")

    # ─── Fetch ticker pour avoir les greeks (sample check) ───────────────────
    # Le summary contient les greeks. Vérifions sur quelques instruments.
    sample = [o for o in options if o["gamma"] > 0][:3]
    print(f"\nSample greeks disponibles: {len(sample)} exemples")
    for s in sample:
        print(f"  {s['name']}: gamma={s['gamma']:.8f}, OI={s['oi']:.1f}")

    # ─── Rebuild gex_by_strike ───────────────────────────────────────────────
    gex_by_strike = {}
    call_gex = {}
    put_gex = {}

    for opt in options:
        strike = opt["strike"]
        g = opt["gamma"] * opt["oi"] * CONTRACT_SIZE * (spot ** 2)
        if opt["type"] == "call":
            gex = g
            call_gex[strike] = call_gex.get(strike, 0) + gex
        else:
            gex = -g
            put_gex[strike] = put_gex.get(strike, 0) + gex
        gex_by_strike[strike] = gex_by_strike.get(strike, 0) + gex

    # Filtre strikes sans GEX réel (tous gamma=0)
    gex_nonzero = {k: v for k, v in gex_by_strike.items() if v != 0}
    print(f"\nStrikes avec GEX non-zéro: {len(gex_nonzero)} / {len(gex_by_strike)} total")

    # ─── Simuler _find_flip_level() exactement ───────────────────────────────
    strikes_sorted = sorted(gex_by_strike.keys())
    cumulative = 0.0
    flip_strike = None
    last_strike = spot

    rows = []
    for strike in strikes_sorted:
        gex = gex_by_strike[strike]
        cumulative += gex
        sign = "+" if cumulative >= 0 else "-"
        rows.append((strike, gex, cumulative, sign))
        if cumulative < 0 and flip_strike is None:
            flip_strike = strike
        if cumulative >= 0:
            last_strike = strike

    flip_returned = flip_strike if flip_strike else last_strike

    # ─── SORTIE 1 : Tableau complet ──────────────────────────────────────────
    TARGET_STRIKES = {64000, 64500, 65000, 69000, 70000, 71000, 72500, 73500, 74500}

    print("\n" + "="*80)
    print("TABLEAU COMPLET : Strike | GEX | Cumul | Signe")
    print("="*80)
    print(f"{'Strike':>10} | {'GEX ($)':>18} | {'Cumul ($)':>18} | Signe")
    print("-"*80)
    for strike, gex, cumul, sign in rows:
        marker = " ◄ FLIP" if strike == flip_strike else ""
        print(f"${strike:>9,.0f} | {gex:>18,.0f} | {cumul:>18,.0f} | {sign}{marker}")

    # ─── SORTIE 2 : Strikes cibles ───────────────────────────────────────────
    print("\n" + "="*80)
    print("FOCUS : Strikes 64k / 65k / 69k-74.5k + zone flip")
    print("="*80)
    print(f"{'Strike':>10} | {'GEX ($)':>18} | {'Cumul ($)':>18} | Signe")
    print("-"*80)
    for strike, gex, cumul, sign in rows:
        is_target = any(abs(strike - t) < 300 for t in TARGET_STRIKES)
        is_near_flip = flip_strike and abs(strike - flip_strike) < 2000
        if is_target or is_near_flip:
            marker = " ◄ FLIP" if strike == flip_strike else ""
            print(f"${strike:>9,.0f} | {gex:>18,.0f} | {cumul:>18,.0f} | {sign}{marker}")

    # ─── SORTIE 3 : Top 10 contributeurs GEX ─────────────────────────────────
    print("\n" + "="*80)
    print("TOP 10 CONTRIBUTEURS GEX ABSOLUS")
    print("="*80)
    top10 = sorted(gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
    print(f"{'Rang':>4} | {'Strike':>10} | {'GEX net':>18} | {'Call GEX':>16} | {'Put GEX':>16} | Type")
    print("-"*90)
    for rank, (strike, gex) in enumerate(top10, 1):
        cg = call_gex.get(strike, 0)
        pg = put_gex.get(strike, 0)
        typ = "CALL DOM." if cg > abs(pg) else "PUT DOM." if abs(pg) > cg else "EQ"
        vs_spot = f"({(strike-spot)/spot*100:+.1f}% spot)"
        print(f"{rank:>4} | ${strike:>9,.0f} | {gex:>18,.0f} | {cg:>16,.0f} | {pg:>16,.0f} | {typ} {vs_spot}")

    # ─── SORTIE 4 : Expiries ─────────────────────────────────────────────────
    print("\n" + "="*80)
    print("EXPIRIES DANS LE DATASET")
    print("="*80)
    from collections import defaultdict
    by_expiry = defaultdict(lambda: {"n": 0, "oi": 0.0})
    for opt in options:
        by_expiry[opt["expiry"]]["n"] += 1
        by_expiry[opt["expiry"]]["oi"] += opt["oi"]
    for exp in sorted(by_expiry.keys()):
        d = by_expiry[exp]
        print(f"  {exp:>10} : {d['n']:>4} options, OI = {d['oi']:>10,.1f}")

    # ─── SORTIE 5 : Vérification strikes cibles ───────────────────────────────
    print("\n" + "="*80)
    print("VÉRIFICATION PRÉSENCE STRIKES CIBLES")
    print("="*80)
    cible_map = {
        "64k": 64000, "65k": 65000, "69k": 69000, "70k": 70000,
        "71k": 71000, "72.5k": 72500, "73.5k": 73500, "74.5k": 74500
    }
    for label, target in cible_map.items():
        exact = gex_by_strike.get(float(target))
        nearest = min(strikes_sorted, key=lambda s: abs(s - target))
        in_cumul_before_flip = target < (flip_strike or 0)
        if exact is not None:
            n_opts = sum(1 for o in options if o["strike"] == target)
            oi_t = sum(o["oi"] for o in options if o["strike"] == target)
            print(f"  {label:>6}: ✅  GEX={exact:,.0f}  OI={oi_t:.1f}  opts={n_opts}  avant_flip={'OUI' if in_cumul_before_flip else 'NON'}")
        else:
            print(f"  {label:>6}: ❌ absent (plus proche: ${nearest:,.0f})")

    # ─── SORTIE 6 : Résumé + diagnostic mathématique ─────────────────────────
    total_gex = sum(gex_by_strike.values())
    call_total = sum(call_gex.values())
    put_total = sum(put_gex.values())
    gex_below_spot = sum(v for k, v in gex_by_strike.items() if k < spot)
    gex_above_spot = sum(v for k, v in gex_by_strike.items() if k > spot)

    print("\n" + "="*80)
    print("FLIP LEVEL RETOURNÉ PAR _find_flip_level()")
    print("="*80)
    print(f"  Flip level  : ${flip_returned:,.0f}")
    if flip_returned:
        dist = abs(flip_returned - spot) / spot * 100
        side = "en-dessous" if flip_returned < spot else "au-dessus"
        print(f"  Distance    : {dist:.1f}% {side} du spot (${spot:,.0f})")
    print(f"\n  GEX total          : ${total_gex:,.0f}")
    print(f"  Call GEX total     : ${call_total:,.0f}")
    print(f"  Put GEX total      : ${put_total:,.0f}")
    print(f"  GEX strikes < spot : ${gex_below_spot:,.0f}")
    print(f"  GEX strikes > spot : ${gex_above_spot:,.0f}")

    if flip_strike:
        gex_before_flip = sum(v for k, v in gex_by_strike.items() if k < flip_strike)
        gex_at_flip = gex_by_strike.get(flip_strike, 0)
        print(f"\n  GEX cumulé AVANT le flip (exclu) : ${gex_before_flip:,.0f}")
        print(f"  GEX au strike flip               : ${gex_at_flip:,.0f}")
        print(f"  Cumul AU MOMENT du flip          : ${gex_before_flip + gex_at_flip:,.0f}")

    print("\n" + "="*80)
    print("DIAGNOSTIC MATHÉMATIQUE")
    print("="*80)
    print(f"""
L'algorithme _find_flip_level() itère LES STRIKES TRIÉS DU PLUS BAS AU PLUS HAUT.
Il accumule GEX[strike] et retourne le premier strike où cumul < 0.

La divergence flip=65k vs GEX dominant 70k-75k s'explique ainsi :

1. Les puts OTM profonds (strikes 30k-65k) ont un GEX NÉGATIF.
   Même avec gamma faible, leur OI cumulé pèse lourd.

2. L'algorithme n'exclut pas les strikes très éloignés du spot.
   → Un put à 30k contribue autant qu'un put à 90k au cumul.

3. Si les puts entre [lowest_strike → 65k] génèrent un GEX cumulé < 0
   AVANT que les calls 70k-75k viennent compenser,
   → le flip ressort à 65k même si les calls dominants sont à 70k-75k.

4. CONSÉQUENCE : le flip retourné est le premier point de croisement zéro
   en partant du bas — pas le "centre de gravité" des dealers.
   Ce n'est pas nécessairement le niveau où le RÉGIME RÉEL bascule.

SUSPECT si : GEX nonzero entre 0 et {flip_strike or 0:,.0f} est dominé par puts OTM profonds
            avec des strikes comme 20k, 30k, 40k qui ont OI faible mais gamma encore actif.
""")


if __name__ == "__main__":
    main()
