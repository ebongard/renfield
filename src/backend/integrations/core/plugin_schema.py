"""
Plugin schema definitions using Pydantic for validation
"""
from enum import Enum
from typing import Any

from pydantic import BaseModel, validator


class ParameterType(str, Enum):
    """Parameter types for intent parameters"""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


class PluginParameter(BaseModel):
    """Parameter definition for intent"""
    name: str
    type: ParameterType
    required: bool = False
    description: str
    default: Any | None = None
    enum: list[Any] | None = None  # Valid values
    pattern: str | None = None      # Regex pattern for strings


class HTTPMethod(str, Enum):
    """HTTP methods for API calls"""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class APIDefinition(BaseModel):
    """API call definition"""
    method: HTTPMethod
    url: str  # Supports templating: {config.api_key}, {params.location}
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None
    timeout: int | None = 10
    response_mapping: dict[str, str] | None = None  # JSONPath mappings


class IntentDefinition(BaseModel):
    """Intent definition"""
    name: str  # e.g., "weather.get_current"
    description: str
    parameters: list[PluginParameter] = []
    examples: list[str] = []  # Legacy: German-only examples
    examples_de: list[str] = []  # German examples (takes priority over `examples`)
    examples_en: list[str] = []  # English examples
    api: APIDefinition

    def get_examples(self, lang: str = "de") -> list[str]:
        """Get examples in specified language with fallback to legacy field."""
        if lang == "en" and self.examples_en:
            return self.examples_en
        if self.examples_de:
            return self.examples_de
        return self.examples  # Fallback to legacy German-only field

    @validator('name')
    def validate_intent_name(cls, v):
        if '.' not in v:
            raise ValueError("Intent name must contain a dot (e.g., 'weather.get_current')")
        return v


class PluginConfig(BaseModel):
    """Plugin configuration requirements"""
    url: str | None = None          # ENV var name for API URL
    api_key: str | None = None      # ENV var name for API key
    additional: dict[str, str] | None = None  # Other config vars


class ErrorMapping(BaseModel):
    """Error code mapping"""
    code: int
    message: str


class PluginMetadata(BaseModel):
    """Plugin metadata"""
    name: str  # e.g., "weather"
    version: str = "1.0.0"
    description: str
    author: str | None = None
    enabled_var: str  # ENV var name (e.g., WEATHER_ENABLED)


class PluginDefinition(BaseModel):
    """Complete plugin definition"""
    metadata: PluginMetadata
    config: PluginConfig
    intents: list[IntentDefinition]
    error_mappings: list[ErrorMapping] | None = []
    rate_limit: int | None = None  # Requests per minute

    class Config:
        use_enum_values = True
