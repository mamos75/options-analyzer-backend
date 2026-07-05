"""
Test simulé du buffer d'agrégation.
Lance : python -m dashboard_options.test_buffer
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from backend.alerts import (
    TelegramAlerter,
    OptionsEventBuffer,
    BufferedEvent,
    _synthesize,
    COOLDOWN_TOPIC,
    MAX_DAILY_OPTIONS,
)

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ─── Scénario ────────────────────────────────────────────────────────────────
# 5 événements sur le niveau $74K en 20 min → 1 seule synthèse envoyée.
#
# t+0  : wall_appeared   $74K    (nouveau mur détecté)
# t+3  : wall_break_up   $74K    (BTC perce $74K à la hausse)
# t+6  : wall_break_down $74K    (BTC retombe sous $74K)
# t+9  : wall_rejection  $74K    (BTC rejeté de $74K)
# t+12 : wall_strength   $74K    (+38% OI — mur renforcé)
# → fenêtre 15 min : 5 events > EXTEND_THRESHOLD=3 → étendue à 30 min
# → flush à t+30 → 1 message "fausse cassure"

async def run_test():
    now = datetime.now(timezone.utc)
    LEVEL = 74_000.0

    events = [
        BufferedEvent(
            topic="options_wall_74000",
            timestamp=now,
            event_type="wall_appeared",
            price=73_800.0,
            level=LEVEL,
        ),
        BufferedEvent(
            topic="options_wall_74000",
            timestamp=now + timedelta(minutes=3),
            event_type="wall_break_up",
            price=74_062.0,
            level=LEVEL,
            direction="up",
        ),
        BufferedEvent(
            topic="options_wall_74000",
            timestamp=now + timedelta(minutes=6),
            event_type="wall_break_down",
            price=73_950.0,
            level=LEVEL,
            direction="down",
        ),
        BufferedEvent(
            topic="options_wall_74000",
            timestamp=now + timedelta(minutes=9),
            event_type="wall_rejection",
            price=73_900.0,
            level=LEVEL,
        ),
        BufferedEvent(
            topic="options_wall_74000",
            timestamp=now + timedelta(minutes=12),
            event_type="wall_strength",
            price=73_920.0,
            level=LEVEL,
            ratio=0.38,
        ),
    ]

    # ── 1. Test synthèse directe ──────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("TEST 1 — Synthèse directe (5 events → 1 message)")
    print("═" * 60)

    msg = _synthesize("options_wall_74000", events)
    assert msg is not None, "La synthèse NE DOIT PAS être None pour une fausse cassure"
    assert "74" in msg, "Le niveau $74K doit apparaître dans le message"
    assert "Fausse cassure" in msg or "fausse cassure" in msg.lower(), "Doit détecter fausse cassure"
    print(msg)
    print("\n✅ Synthèse correcte — fausse cassure détectée")

    # ── 2. Test buffer + fenêtre ──────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("TEST 2 — Buffer : fenêtre étendue à 30 min (5 events > seuil 3)")
    print("═" * 60)

    buf = OptionsEventBuffer()
    for ev in events:
        buf.ingest(ev)

    topic_flush_at = buf._flush_at.get("options_wall_74000")
    assert topic_flush_at is not None
    window_duration = topic_flush_at - events[0].timestamp
    assert window_duration == OptionsEventBuffer.EXTEND_WINDOW, (
        f"Fenêtre doit être 30 min, reçu: {window_duration}"
    )
    print(f"  Fenêtre d'agrégation : {window_duration} ✅")

    ready = buf.pop_ready()
    assert len(ready) == 0, "Rien ne doit être prêt avant expiration de la fenêtre"
    print(f"  Avant flush : 0 topic prêt ✅")

    # Force l'expiration
    buf._flush_at["options_wall_74000"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    for ev in events:
        buf._events.setdefault("options_wall_74000", []).append(ev)

    ready = buf.pop_ready()
    assert len(ready) == 1, f"Exactement 1 topic doit être prêt, reçu: {len(ready)}"
    assert len(ready[0][1]) == len(events)
    print(f"  Après flush : 1 topic prêt, {len(ready[0][1])} events ✅")

    # ── 3. Test anti-spam complet avec TelegramAlerter ────────────────────────
    print("\n" + "═" * 60)
    print("TEST 3 — Anti-spam : 5 events → 1 seul envoi Telegram")
    print("═" * 60)

    sent_messages = []

    alerter = TelegramAlerter("FAKE_TOKEN", "FAKE_CHAT_ID")

    # Patch send pour capturer sans appel réseau
    async def mock_send(text, parse_mode="Markdown"):
        sent_messages.append(text)
        print(f"\n📨 MESSAGE ENVOYÉ :\n{text}")

    alerter.send = mock_send

    # Ingérer les 5 events
    for ev in events:
        alerter._buffer.ingest(ev)

    # Simuler flush avant expiration → 0 envois
    n = await alerter.flush_ready()
    assert n == 0, f"Aucun envoi avant expiration, reçu: {n}"
    print(f"\n  Flush avant expiration : {n} envoi(s) ✅")

    # Forcer expiration et réinjecter
    alerter._buffer._flush_at["options_wall_74000"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    for ev in events:
        alerter._buffer._events.setdefault("options_wall_74000", []).append(ev)

    # Flush → 1 envoi
    n = await alerter.flush_ready()
    assert n == 1, f"Exactement 1 envoi attendu, reçu: {n}"
    print(f"\n  Flush après expiration : {n} envoi(s) ✅")

    # Deuxième flush immédiat → 0 (cooldown 30 min)
    alerter._buffer._flush_at["options_wall_74000"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    for ev in events:
        alerter._buffer._events.setdefault("options_wall_74000", []).append(ev)

    n2 = await alerter.flush_ready()
    assert n2 == 0, f"Cooldown doit bloquer le 2e envoi, reçu: {n2}"
    print(f"  Cooldown 30 min actif → 2e flush bloqué : {n2} envoi(s) ✅")

    # ── 4. Test bruit ignoré ──────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("TEST 4 — Bruit : 1 seul event wall_appeared → pas d'envoi")
    print("═" * 60)

    noise_event = [BufferedEvent(
        topic="options_wall_80000",
        timestamp=now,
        event_type="wall_appeared",
        price=73_920.0,
        level=80_000.0,
    )]
    msg_bruit = _synthesize("options_wall_80000", noise_event)
    assert msg_bruit is None, "1 seul wall_appeared = bruit → synthèse doit être None"
    print("  1 event wall_appeared seul → synthèse=None (bruit) ✅")

    # ── Résumé ────────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("✅ TOUS LES TESTS PASSÉS")
    print(f"   Total messages Telegram envoyés : {len(sent_messages)} (attendu: 1)")
    print("═" * 60)


if __name__ == "__main__":
    asyncio.run(run_test())
