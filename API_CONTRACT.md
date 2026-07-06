# API Contract — /api/decision

**Version F8.3** — Vocabulaire desk premium
**Date dépreciation aliases** : 21/07/2026

## Champs state + action (nouveaux — F8.3)

### `state` — Situation actuelle

| Valeur | Description |
|--------|-------------|
| `RAS` | Pas de signal / données insuffisantes |
| `TENSION` | Signal présent, pas de contradiction majeure |
| `CONFLIT` | Contradictions détectées entre indicateurs |
| `ZONE_CRITIQUE` | Signal directionnel mais confiance < 40% |

### `action` — Ce qu'il faut faire

| Valeur | Description |
|--------|-------------|
| `OBSERVER` | Pas d'action, surveiller |
| `PRÉPARER` | Signal directionnel mais confiance insuffisante pour agir |
| `AGIR_LONG` | Signal haussier convergent, confiance >= 60% |
| `AGIR_SHORT` | Signal baissier convergent, confiance >= 60% |

## Aliases mécaniques (dépreciés le 21/07/2026)

- `verdict` → alias de `action` (mapping : SIGNAL_UP→AGIR_LONG, OBSERVE→OBSERVER, etc.)
- `system_status` → alias de `state` (mapping : TRADEABLE→TENSION, CONFLICT→CONFLIT, etc.)

## Mapping `verdict` → `action`

| verdict | action (confiance < 60%) | action (confiance >= 60%) |
|---------|--------------------------|---------------------------|
| `SIGNAL_UP` | `PRÉPARER` | `AGIR_LONG` |
| `SIGNAL_DOWN` | `PRÉPARER` | `AGIR_SHORT` |
| `OBSERVE` | `OBSERVER` | `OBSERVER` |
| `NO_TRADE` | `OBSERVER` | `OBSERVER` |

## Mapping `system_status` → `state`

| system_status | state |
|---------------|-------|
| `OFFLINE` | `RAS` |
| `DEGRADED` | `RAS` |
| `CONFLICT` | `CONFLIT` |
| `TRADEABLE` | `TENSION` |
| `OBSERVE` | `TENSION` ou `ZONE_CRITIQUE` (selon confiance) |
