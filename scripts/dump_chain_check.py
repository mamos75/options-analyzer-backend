#!/usr/bin/env python3
"""
dump_chain_check.py — Sprint 3 Phase 0
Vérifie la chaîne OI puts via l'API live du dashboard.
Usage : python3 scripts/dump_chain_check.py (sur VPS)
"""
import urllib.request, json

BASE = "http://127.0.0.1:80"
HOST = "options-analyzer.mamoscrypto.com"

def get(path):
    req = urllib.request.Request(BASE + path, headers={"Host": HOST})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# Snapshot brut
snap = get("/api/snapshot")
walls = snap.get("walls", [])
spot  = snap.get("spot", 0)
print(f"BTC spot: {spot:,.0f}")
print(f"Walls count: {len(walls)}")
for w in walls:
    print(f"  {w}")

# Narrative pour niveau_bas_label
narr = get("/api/narrative")
print(f"\nniveau_bas       : {narr.get('niveau_bas')}")
print(f"niveau_bas_label : {narr.get('niveau_bas_label')}")
print(f"niveau_haut      : {narr.get('niveau_haut')}")
print(f"max_pain_display : {narr.get('max_pain_display')}")

# Pro decision pour gex_regime
pro = get("/api/pro_decision")
regime_raw = pro.get("regime", {})
print(f"\npro.regime field : {regime_raw}")

# GEX via vex_cex
vc = get("/api/vex_cex")
print(f"\ngamma_flip_regime: {vc.get('gamma_flip_regime')}")

# Decision arbiter
dec = get("/api/decision")
print(f"\narbiter verdict  : {dec.get('verdict')}")
print(f"gex_regime used  : {dec.get('gex_regime', '(not in payload)')}")

