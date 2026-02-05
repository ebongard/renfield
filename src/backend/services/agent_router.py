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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import yaml
from loguru import logger

from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import get_agent_client

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
        elif role.mcp_servers is None:
            # general — always available
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

        # Choose model + client based on available Ollama instances.
        # If a separate agent Ollama is configured, use its model (the intent
        # model likely doesn't exist there). Otherwise use intent model on default.
        if settings.agent_ollama_url:
            client, _ = get_agent_client(fallback_url=settings.agent_ollama_url)
            router_model = settings.agent_model or settings.ollama_model
        else:
            client = ollama.client
            router_model = settings.ollama_intent_model or settings.ollama_model

        try:
            raw_response = await asyncio.wait_for(
                client.chat(
                    model=router_model,
                    messages=[
                        {"role": "user", "content": classify_prompt},
                    ],
                    options=llm_options,
                ),
                timeout=self.classify_timeout,
            )
            response_text = raw_response.message.content or ""
            logger.debug(f"Router LLM response: {response_text[:200]}")

            # Parse JSON response
            role_name = self._parse_classification(response_text)
            if role_name and role_name in self.roles:
                role = self.roles[role_name]
                logger.info(f"Router classified '{message[:60]}...' as '{role_name}'")
                return role

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

    def _parse_classification(self, response_text: str) -> str | None:
        """Parse the role name from the LLM classification response."""
        import re

        text = response_text.strip()
        if not text:
            return None

        # Try direct JSON parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed.get("role")
        except json.JSONDecodeError:
            pass

        # Try to find JSON in text
        match = re.search(r'\{[^}]*"role"\s*:\s*"([^"]+)"[^}]*\}', text)
        if match:
            return match.group(1)

        # Last resort: look for a known role name in the text
        for role_name in self.roles:
            if role_name in text.lower():
                return role_name

        return None


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
