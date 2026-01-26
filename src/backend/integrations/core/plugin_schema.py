"""
Plugin schema definitions using Pydantic for validation
"""
from pydantic import BaseModel, validator
from typing import Dict, List, Optional, Any
from enum import Enum


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
    default: Optional[Any] = None
    enum: Optional[List[Any]] = None  # Valid values
    pattern: Optional[str] = None      # Regex pattern for strings


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
    headers: Optional[Dict[str, str]] = None
    body: Optional[Dict[str, Any]] = None
    timeout: Optional[int] = 10
    response_mapping: Optional[Dict[str, str]] = None  # JSONPath mappings


class IntentDefinition(BaseModel):
    """Intent definition"""
    name: str  # e.g., "weather.get_current"
    description: str
    parameters: List[PluginParameter] = []
    examples: List[str] = []  # Example user queries
    api: APIDefinition

    @validator('name')
    def validate_intent_name(cls, v):
        if '.' not in v:
            raise ValueError("Intent name must contain a dot (e.g., 'weather.get_current')")
        return v


class PluginConfig(BaseModel):
    """Plugin configuration requirements"""
    url: Optional[str] = None          # ENV var name for API URL
    api_key: Optional[str] = None      # ENV var name for API key
    additional: Optional[Dict[str, str]] = None  # Other config vars


class ErrorMapping(BaseModel):
    """Error code mapping"""
    code: int
    message: str


class PluginMetadata(BaseModel):
    """Plugin metadata"""
    name: str  # e.g., "weather"
    version: str = "1.0.0"
    description: str
    author: Optional[str] = None
    enabled_var: str  # ENV var name (e.g., WEATHER_ENABLED)


class PluginDefinition(BaseModel):
    """Complete plugin definition"""
    metadata: PluginMetadata
    config: PluginConfig
    intents: List[IntentDefinition]
    error_mappings: Optional[List[ErrorMapping]] = []
    rate_limit: Optional[int] = None  # Requests per minute

    class Config:
        use_enum_values = True
