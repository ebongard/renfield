"""
Intent Registry API Routes

Provides endpoints for viewing available intents and integration status.
Admin-only endpoints for system monitoring and debugging.
"""
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from services.intent_registry import intent_registry, IntentDef, CORE_INTEGRATIONS
from utils.config import settings


router = APIRouter()


# =============================================================================
# Response Schemas
# =============================================================================

class IntentParameterResponse(BaseModel):
    """Parameter definition for an intent."""
    name: str
    description: str
    required: bool
    param_type: str


class IntentResponse(BaseModel):
    """Single intent definition."""
    name: str
    description: str
    parameters: List[IntentParameterResponse]
    examples: List[str]


class IntegrationStatusResponse(BaseModel):
    """Status of a single integration."""
    name: str
    title: str
    enabled: bool
    intent_count: int
    intents: List[IntentResponse]


class PluginIntentResponse(BaseModel):
    """Plugin intent summary."""
    name: str
    description: str
    plugin: str


class MCPToolResponse(BaseModel):
    """MCP tool summary."""
    intent: str
    description: str
    server: Optional[str] = None


class IntentRegistryStatusResponse(BaseModel):
    """Full status of the intent registry."""
    total_intents: int
    enabled_integrations: int
    disabled_integrations: int
    integrations: List[IntegrationStatusResponse]
    plugins: List[PluginIntentResponse]
    mcp_tools: List[MCPToolResponse]


class IntentPromptResponse(BaseModel):
    """Generated intent prompt for debugging."""
    language: str
    intent_types: str
    examples: str


# =============================================================================
# API Endpoints
# =============================================================================

@router.get("/status", response_model=IntentRegistryStatusResponse)
async def get_intent_status(
    lang: str = Query("de", description="Language for descriptions (de/en)")
):
    """
    Get full status of the intent registry.

    Shows all integrations, their enabled/disabled status, and available intents.
    Useful for debugging and understanding system capabilities.
    """
    integrations = []
    enabled_count = 0
    disabled_count = 0
    total_intents = 0

    for integration in CORE_INTEGRATIONS:
        is_enabled = integration.is_enabled_func()

        if is_enabled:
            enabled_count += 1
        else:
            disabled_count += 1

        # Build intent list
        intents = []
        for intent in integration.intents:
            params = [
                IntentParameterResponse(
                    name=p.name,
                    description=p.description,
                    required=p.required,
                    param_type=p.param_type
                )
                for p in intent.parameters
            ]
            intents.append(IntentResponse(
                name=intent.name,
                description=intent.get_description(lang),
                parameters=params,
                examples=intent.get_examples(lang)
            ))

        if is_enabled:
            total_intents += len(intents)

        integrations.append(IntegrationStatusResponse(
            name=integration.integration_name,
            title=integration.get_title(lang),
            enabled=is_enabled,
            intent_count=len(intents),
            intents=intents
        ))

    # Plugin intents
    plugins = []
    if intent_registry._plugin_registry and settings.plugins_enabled:
        for intent_def in intent_registry._plugin_registry.get_all_intents():
            plugins.append(PluginIntentResponse(
                name=intent_def.name,
                description=intent_def.description,
                plugin=intent_def.name.split(".")[0] if "." in intent_def.name else "unknown"
            ))
        total_intents += len(plugins)

    # MCP tools
    mcp_tools = []
    if settings.mcp_enabled and intent_registry._mcp_tools:
        for tool in intent_registry._mcp_tools:
            mcp_tools.append(MCPToolResponse(
                intent=tool.get("intent", tool.get("name", "unknown")),
                description=tool.get("description", ""),
                server=tool.get("server")
            ))
        total_intents += len(mcp_tools)

    return IntentRegistryStatusResponse(
        total_intents=total_intents,
        enabled_integrations=enabled_count,
        disabled_integrations=disabled_count,
        integrations=integrations,
        plugins=plugins,
        mcp_tools=mcp_tools
    )


@router.get("/prompt", response_model=IntentPromptResponse)
async def get_intent_prompt(
    lang: str = Query("de", description="Language for prompt (de/en)")
):
    """
    Get the generated intent prompt for debugging.

    Shows exactly what prompt text is sent to the LLM for intent recognition.
    """
    intent_types = intent_registry.build_intent_prompt(lang=lang)
    examples = intent_registry.build_examples_prompt(lang=lang, max_examples=15)

    return IntentPromptResponse(
        language=lang,
        intent_types=intent_types,
        examples=examples
    )


@router.get("/check/{intent_name}")
async def check_intent_available(intent_name: str):
    """
    Check if a specific intent is available.

    Returns whether the intent is registered and its integration is enabled.
    """
    is_available = intent_registry.is_intent_available(intent_name)
    intent_def = intent_registry.get_intent_definition(intent_name)

    result = {
        "intent": intent_name,
        "available": is_available,
        "definition": None
    }

    if intent_def:
        result["definition"] = {
            "name": intent_def.name,
            "description_de": intent_def.description_de,
            "description_en": intent_def.description_en,
            "parameters": [
                {"name": p.name, "required": p.required}
                for p in intent_def.parameters
            ]
        }

    return result


@router.get("/integrations/summary")
async def get_integrations_summary():
    """
    Get a quick summary of integration status.

    Lightweight endpoint for dashboard widgets.
    """
    enabled = []
    disabled = []

    for integration in CORE_INTEGRATIONS:
        info = {
            "name": integration.integration_name,
            "intents": len(integration.intents)
        }
        if integration.is_enabled_func():
            enabled.append(info)
        else:
            disabled.append(info)

    return {
        "enabled": enabled,
        "disabled": disabled,
        "plugins_enabled": settings.plugins_enabled,
        "mcp_enabled": settings.mcp_enabled
    }
