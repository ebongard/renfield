#!/usr/bin/env python3
"""
Test script to verify media_player entity detection

Run from project root:
    python tests/manual/test_media_player.py
"""
import asyncio
import sys
from pathlib import Path

# Add project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "backend"))

from services.ollama_service import OllamaService
from utils.config import settings

async def test_media_player_intent():
    """Test if media player can be detected"""

    print("🧪 Testing Media Player Intent Extraction\n")
    print("=" * 60)

    ollama = OllamaService()

    test_messages = [
        "Starte den Mediaplayer im Arbeitszimmer",
        "Spiele Musik im Arbeitszimmer",
        "Schalte den Player im Arbeitszimmer ein",
        "Starte Spotify im Arbeitszimmer"
    ]

    for i, message in enumerate(test_messages, 1):
        print(f"\n📝 Test {i}: '{message}'")
        print("-" * 60)

        try:
            intent = await ollama.extract_intent(message)

            if intent:
                print(f"✅ Intent erkannt:")
                print(f"   Type: {intent.get('type')}")
                print(f"   Action: {intent.get('action')}")
                print(f"   Entity ID: {intent.get('entity_id')}")
                print(f"   Value: {intent.get('value')}")
                print(f"   Confidence: {intent.get('confidence', 'N/A')}")
            else:
                print("❌ Kein Intent erkannt (None returned)")

        except Exception as e:
            print(f"❌ Fehler: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print("✅ Tests abgeschlossen")

if __name__ == "__main__":
    asyncio.run(test_media_player_intent())
