"""
Agent Router — Classifies user messages into specialized agent roles.

Replaces the ComplexityDetector + ranked intent dual-path with a single
unified routing step. Every message goes through the router which assigns
exactly one role (e.g. smart_home, documents, conversation).

Each role defines:
- Which MCP servers are available (tool filtering)
- Which internal tools are available
- Maximum agent loop steps
- A role-specific prompt key
"""
import asyncio
import json
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

import yaml
from loguru import logger

from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_agent_client,
    get_classification_chat_kwargs,
)

if TYPE_CHECKING:
    from services.mcp_client import MCPManager
    from services.ollama_service import OllamaService


@dataclass
class AgentRole:
    """Definition of a specialized agent role."""
    name: str
    description: dict[str, str]  # lang -> description
    mcp_servers: list[str] | None = None  # None = all servers
    internal_tools: list[str] | None = None  # None = all internal tools
    max_steps: int = 8
    prompt_key: str = "agent_prompt"
    has_agent_loop: bool = True  # False for conversation and knowledge roles
    model: str | None = None  # Per-role model override
    ollama_url: str | None = None  # Per-role Ollama URL override
    sub_intent: str | None = None  # Set per-classification (on returned copy)
    sub_intent_definitions: dict[str, dict[str, str]] | None = None  # From config


# Pre-built fallback roles
CONVERSATION_ROLE = AgentRole(
    name="conversation",
    description={"de": "Konversation", "en": "Conversation"},
    has_agent_loop=False,
)

KNOWLEDGE_ROLE = AgentRole(
    name="knowledge",
    description={"de": "Wissensdatenbank", "en": "Knowledge base"},
    has_agent_loop=False,
)

GENERAL_ROLE = AgentRole(
    name="general",
    description={"de": "Allgemein", "en": "General"},
    mcp_servers=None,
    internal_tools=None,
    max_steps=12,
    prompt_key="agent_prompt",
)


def _parse_roles(config: dict) -> dict[str, AgentRole]:
    """Parse role definitions from YAML config into AgentRole objects."""
    roles = {}
    roles_config = config.get("roles", {})

    for name, role_data in roles_config.items():
        if not isinstance(role_data, dict):
            continue

        description = role_data.get("description", {})
        if isinstance(description, str):
            description = {"de": description, "en": description}

        # Roles without mcp_servers and without prompt_key are non-agent roles
        has_agent_loop = "prompt_key" in role_data

        # Parse sub_intent_definitions from config
        sub_intents_raw = role_data.get("sub_intents")
        if sub_intents_raw and isinstance(sub_intents_raw, dict):
            sub_intents = {}
            for si_name, si_val in sub_intents_raw.items():
                if isinstance(si_val, str):
                    sub_intents[si_name] = {"de": si_val, "en": si_val}
                elif isinstance(si_val, dict):
                    sub_intents[si_name] = si_val
            sub_intent_definitions = sub_intents if sub_intents else None
        else:
            sub_intent_definitions = None

        role = AgentRole(
            name=name,
            description=description,
            mcp_servers=role_data.get("mcp_servers"),
            internal_tools=role_data.get("internal_tools"),
            max_steps=role_data.get("max_steps", 8),
            prompt_key=role_data.get("prompt_key", "agent_prompt"),
            has_agent_loop=has_agent_loop,
            model=role_data.get("model"),
            ollama_url=role_data.get("ollama_url"),
            sub_intent_definitions=sub_intent_definitions,
        )
        roles[name] = role

    return roles


def _filter_available_roles(
    roles: dict[str, AgentRole],
    connected_servers: list[str] | None = None,
) -> dict[str, AgentRole]:
    """Filter out roles whose required MCP servers aren't connected.

    Roles with mcp_servers=None (general) or no agent loop (conversation, knowledge)
    are always kept.
    """
    if connected_servers is None:
        return roles

    connected_set = set(connected_servers)
    filtered = {}

    for name, role in roles.items():
        if not role.has_agent_loop:
            # conversation, knowledge — always available
            filtered[name] = role
        elif role.mcp_servers is None or role.mcp_servers == []:
            # general or internal-only roles — always available
            filtered[name] = role
        else:
            # Check if at least one required server is connected
            if any(server in connected_set for server in role.mcp_servers):
                filtered[name] = role
            else:
                logger.debug(
                    f"Role '{name}' excluded: servers {role.mcp_servers} "
                    f"not in connected {connected_servers}"
                )

    return filtered


class AgentRouter:
    """Routes user messages to specialized agent roles via LLM classification."""

    def __init__(
        self,
        roles_config: dict,
        mcp_manager: Optional["MCPManager"] = None,
        classify_timeout: float = 30.0,
    ):
        all_roles = _parse_roles(roles_config)
        self.classify_timeout = classify_timeout

        # Get connected MCP servers
        connected_servers = None
        if mcp_manager:
            connected_servers = mcp_manager.get_connected_server_names()

        self.roles = _filter_available_roles(all_roles, connected_servers)
        logger.info(
            f"AgentRouter initialized: {len(self.roles)} roles available "
            f"({', '.join(sorted(self.roles.keys()))})"
        )

    def get_role(self, name: str) -> AgentRole:
        """Get a role by name, falling back to general."""
        return self.roles.get(name, GENERAL_ROLE)

    def _build_role_descriptions(self, lang: str = "de") -> str:
        """Build compact role descriptions for the classification prompt."""
        lines = []
        for name, role in sorted(self.roles.items()):
            desc = role.description.get(lang, role.description.get("de", name))
            lines.append(f"- {name}: {desc}")
            if role.sub_intent_definitions:
                for si_name, si_desc in role.sub_intent_definitions.items():
                    si_text = si_desc.get(lang, si_desc.get("de", si_name))
                    lines.append(f"  > {name}/{si_name}: {si_text}")
        return "\n".join(lines)

    async def classify(
        self,
        message: str,
        ollama: "OllamaService",
        conversation_history: list[dict] | None = None,
        lang: str = "de",
    ) -> AgentRole:
        """Classify a user message into one agent role.

        Uses a fast LLM call with a compact classification prompt.
        Falls back to 'general' on parse failure or timeout.

        Args:
            message: The user's message
            ollama: OllamaService for LLM calls
            conversation_history: Recent conversation history for context
            lang: Language for prompts

        Returns:
            The classified AgentRole
        """
        # Build role descriptions for the prompt
        role_descriptions = self._build_role_descriptions(lang)

        # Build optional history context
        history_context = ""
        if conversation_history:
            recent = conversation_history[-3:]
            history_lines = []
            for msg in recent:
                role_label = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content", "")[:200]
                history_lines.append(f"  {role_label}: {content}")
            history_context = prompt_manager.get(
                "router", "history_context_template", lang=lang,
                history_lines="\n".join(history_lines)
            )

        # Build classification prompt
        classify_prompt = prompt_manager.get(
            "router", "classify_prompt", lang=lang,
            message=message,
            history_context=history_context,
            role_descriptions=role_descriptions,
        )

        # Get LLM options for router (fast, deterministic)
        llm_options = prompt_manager.get_config("router", "llm_options") or {
            "temperature": 0.0, "top_p": 0.1, "num_predict": 128, "num_ctx": 4096
        }

        # Choose model + client for router classification.
        # Priority: agent_router_model/url > agent_model/url > intent_model > default
        router_url = settings.agent_router_url or settings.agent_ollama_url
        router_model = settings.agent_router_model or settings.ollama_intent_model or settings.ollama_model
        if router_url:
            client, _ = get_agent_client(fallback_url=router_url)
        else:
            client = ollama.client

        try:
            logger.info(f"Router using model: {router_model}")
            # Option A: Disable thinking mode for classification tasks
            classification_kwargs = get_classification_chat_kwargs(router_model)
            raw_response = await asyncio.wait_for(
                client.chat(
                    model=router_model,
                    messages=[
                        {"role": "user", "content": classify_prompt},
                    ],
                    options=llm_options,
                    **classification_kwargs,
                ),
                timeout=self.classify_timeout,
            )
            # Option B: Failsafe for empty content with thinking
            response_text = extract_response_content(raw_response)
            logger.info(f"Router LLM raw response: {response_text[:300]}")

            # Parse JSON response
            role_name, sub_intent = self._parse_classification(response_text)
            if role_name and role_name in self.roles:
                role = self.roles[role_name]
                # Validate sub_intent against role's defined sub_intents
                valid_sub = None
                if sub_intent and role.sub_intent_definitions:
                    # Strip "role/" prefix if LLM included it (e.g. "release/my_dashboard" → "my_dashboard")
                    clean_sub = sub_intent.split("/", 1)[-1] if "/" in sub_intent else sub_intent
                    if clean_sub in role.sub_intent_definitions:
                        valid_sub = clean_sub
                # Fallback: infer sub_intent from user message keywords
                # when LLM didn't return one (e.g. prose response)
                if not valid_sub and role.sub_intent_definitions:
                    valid_sub = self._infer_sub_intent(
                        message, role.sub_intent_definitions, lang
                    )
                result = replace(role, sub_intent=valid_sub)
                logger.info(
                    f"Router classified '{message[:60]}...' as '{role_name}'"
                    + (f" (sub_intent={valid_sub})" if valid_sub else "")
                )
                return result

            logger.warning(
                f"Router: invalid role '{role_name}' from LLM, "
                f"falling back to 'general'"
            )
            return self.get_role("general")

        except TimeoutError:
            logger.warning("Router: LLM timeout, falling back to 'general'")
            return self.get_role("general")
        except Exception as e:
            logger.error(f"Router classification failed: {e}")
            return self.get_role("general")

    @staticmethod
    def _infer_sub_intent(
        message: str,
        definitions: dict[str, dict[str, str]],
        lang: str = "de",
    ) -> str | None:
        """Infer sub_intent by matching user message words against description keywords.

        Used as fallback when the router LLM returns prose instead of JSON.
        Returns the sub_intent name with the most keyword hits, or None.
        """
        msg_lower = message.lower()
        best_name: str | None = None
        best_hits = 0
        for si_name, si_desc in definitions.items():
            # Use the description in the user's language (fallback to de)
            desc_text = si_desc.get(lang, si_desc.get("de", ""))
            keywords = [k.strip().lower() for k in desc_text.split(",") if k.strip()]
            hits = sum(1 for kw in keywords if kw in msg_lower)
            if hits > best_hits:
                best_hits = hits
                best_name = si_name
        return best_name if best_hits > 0 else None

    def _parse_classification(self, response_text: str) -> tuple[str | None, str | None]:
        """Parse the role name and optional sub_intent from the LLM response."""
        import re

        text = response_text.strip()
        if not text:
            return None, None

        # Try direct JSON parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed.get("role"), parsed.get("sub_intent")
        except json.JSONDecodeError:
            pass

        # Try to find JSON in text
        match = re.search(r'\{[^}]*"role"\s*:\s*"([^"]+)"[^}]*\}', text)
        if match:
            si_match = re.search(r'"sub_intent"\s*:\s*"([^"]+)"', match.group(0))
            return match.group(1), (si_match.group(1) if si_match else None)

        # Last resort: look for a known role name in the text
        for role_name in self.roles:
            if role_name in text.lower():
                return role_name, None

        return None, None


def load_roles_config(config_path: str) -> dict:
    """Load agent roles configuration from YAML file.

    Supports environment variable substitution in values.

    Args:
        config_path: Path to agent_roles.yaml

    Returns:
        Parsed YAML config dict, or empty dict on error
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()

        # Substitute environment variables (same pattern as mcp_servers.yaml)
        import re
        def _env_sub(match):
            var_expr = match.group(1)
            if ":-" in var_expr:
                var_name, default = var_expr.split(":-", 1)
                return os.environ.get(var_name, default)
            return os.environ.get(var_expr, match.group(0))

        raw = re.sub(r'\$\{([^}]+)\}', _env_sub, raw)
        config = yaml.safe_load(raw)
        return config or {}
    except FileNotFoundError:
        logger.warning(f"Agent roles config not found: {config_path}")
        return {}
    except Exception as e:
        logger.error(f"Failed to load agent roles config: {e}")
        return {}
