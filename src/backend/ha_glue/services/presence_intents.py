"""HA presence intent definitions.

Defines the `PRESENCE_INTENTS` integration that used to live inline in
platform `services/intent_registry.py`. Registered with the platform's
IntentRegistry via `intent_registry.add_integration()` during
ha_glue's startup hook.

The `is_enabled_func` reads `ha_glue_settings.presence_enabled`, which
is resolved at the time `get_enabled_integrations()` is called — so
the presence intents appear in the prompt only when ha_glue is loaded
AND presence is enabled.
"""

from __future__ import annotations

from services.intent_registry import IntegrationIntents, IntentDef, IntentParam

from ha_glue.utils.config import ha_glue_settings


PRESENCE_INTENTS = IntegrationIntents(
    integration_name="presence",
    title_de="ANWESENHEIT",
    title_en="PRESENCE",
    is_enabled_func=lambda: ha_glue_settings.presence_enabled,
    intents=[
        IntentDef(
            name="internal.get_user_location",
            description_de="Aktuellen oder letzten bekannten Aufenthaltsort eines Benutzers abfragen",
            description_en="Get current or last known room location of a user",
            parameters=[
                IntentParam(
                    "user_name",
                    "Name des Benutzers (Username, Vorname oder Nachname)",
                    required=True,
                ),
            ],
            examples_de=["Wo ist Alex?", "In welchem Raum ist Alex?"],
            examples_en=["Where is Alex?", "Which room is Alex in?"],
        ),
        IntentDef(
            name="internal.get_all_presence",
            description_de="Alle aktuell anwesenden Benutzer und ihre Räume anzeigen",
            description_en="Get all currently present users and their room locations",
            examples_de=["Wer ist zuhause?", "Wo sind alle?"],
            examples_en=["Who is home?", "Where is everyone?"],
        ),
    ],
)
