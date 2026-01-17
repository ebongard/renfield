"""
Generic plugin executor - executes YAML-defined plugins
"""
import httpx
import os
import re
from typing import Dict, Any, Optional
from loguru import logger
from .plugin_schema import PluginDefinition, IntentDefinition
from .plugin_response import PluginResponse
from datetime import datetime, timedelta
from collections import deque


class GenericPlugin:
    """Executes YAML-defined plugin intents"""

    def __init__(self, definition: PluginDefinition):
        self.definition = definition
        self.config_cache = self._load_config()
        self.rate_limiter = self._create_rate_limiter()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        config = {}

        if self.definition.config.url:
            config['url'] = os.getenv(self.definition.config.url)

        if self.definition.config.api_key:
            config['api_key'] = os.getenv(self.definition.config.api_key)

        if self.definition.config.additional:
            for key, env_var in self.definition.config.additional.items():
                config[key] = os.getenv(env_var)

        return config

    def _create_rate_limiter(self) -> Optional[Dict]:
        """Create rate limiter if configured"""
        if self.definition.rate_limit:
            return {
                'limit': self.definition.rate_limit,
                'window': 60,  # seconds
                'requests': deque()
            }
        return None

    async def _check_rate_limit(self) -> bool:
        """Check if rate limit allows request"""
        if not self.rate_limiter:
            return True

        now = datetime.now()
        window_start = now - timedelta(seconds=self.rate_limiter['window'])

        # Remove old requests
        while (self.rate_limiter['requests'] and
               self.rate_limiter['requests'][0] < window_start):
            self.rate_limiter['requests'].popleft()

        # Check limit
        if len(self.rate_limiter['requests']) >= self.rate_limiter['limit']:
            logger.warning(f"⚠️  Rate limit exceeded for {self.definition.metadata.name}")
            return False

        # Record request
        self.rate_limiter['requests'].append(now)
        return True

    async def execute(self, intent_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute plugin intent with given parameters

        Args:
            intent_name: Intent to execute (e.g., "weather.get_current")
            parameters: Intent parameters

        Returns:
            Standardized response dictionary
        """
        # Check rate limit
        if not await self._check_rate_limit():
            return PluginResponse.error(
                "Zu viele Anfragen. Bitte später erneut versuchen.",
                error_code="RATE_LIMIT_EXCEEDED"
            )

        # Find intent definition
        intent_def = self._find_intent(intent_name)
        if not intent_def:
            return PluginResponse.error(
                f"Intent '{intent_name}' not found in plugin '{self.definition.metadata.name}'"
            )

        # Validate parameters
        validation_error = self._validate_parameters(intent_def, parameters)
        if validation_error:
            return PluginResponse.invalid_parameters(validation_error)

        # Execute API call
        try:
            result = await self._execute_api_call(intent_def, parameters)
            return result
        except Exception as e:
            logger.error(f"❌ Plugin execution error: {e}")
            return PluginResponse.error(str(e))

    def _find_intent(self, intent_name: str) -> Optional[IntentDefinition]:
        """Find intent definition by name"""
        for intent in self.definition.intents:
            if intent.name == intent_name:
                return intent
        return None

    def _validate_parameters(
        self,
        intent_def: IntentDefinition,
        parameters: Dict[str, Any]
    ) -> Optional[str]:
        """
        Validate parameters against intent definition

        Returns:
            Error message if validation fails, None if valid
        """
        for param_def in intent_def.parameters:
            param_name = param_def.name

            # Check required parameters
            if param_def.required and param_name not in parameters:
                return f"Missing required parameter: {param_name}"

            # Type validation (basic)
            if param_name in parameters:
                value = parameters[param_name]
                expected_type = param_def.type

                # Basic type checking
                if expected_type == "string" and not isinstance(value, str):
                    return f"Parameter '{param_name}' must be a string"
                elif expected_type == "integer" and not isinstance(value, int):
                    return f"Parameter '{param_name}' must be an integer"
                elif expected_type == "boolean" and not isinstance(value, bool):
                    return f"Parameter '{param_name}' must be a boolean"

                # Enum validation
                if param_def.enum and value not in param_def.enum:
                    return f"Parameter '{param_name}' must be one of {param_def.enum}"

                # Pattern validation (regex)
                if param_def.pattern and isinstance(value, str):
                    if not re.match(param_def.pattern, value):
                        return f"Parameter '{param_name}' doesn't match pattern {param_def.pattern}"

        return None

    async def _execute_api_call(
        self,
        intent_def: IntentDefinition,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute API call defined in intent"""
        api_def = intent_def.api

        # Build URL with template substitution (URL-encode parameters)
        url = self._substitute_template(api_def.url, parameters, url_encode=True)

        # Build headers
        headers = {}
        if api_def.headers:
            for key, value in api_def.headers.items():
                headers[key] = self._substitute_template(value, parameters)

        # Build body
        body = None
        if api_def.body:
            body = self._substitute_body(api_def.body, parameters)

        logger.info(f"🌐 API Call: {api_def.method} {url}")
        logger.debug(f"Headers: {headers}")
        logger.debug(f"Body: {body}")

        # Execute request
        async with httpx.AsyncClient() as client:
            try:
                if api_def.method == "GET":
                    response = await client.get(
                        url,
                        headers=headers,
                        timeout=api_def.timeout
                    )
                elif api_def.method == "POST":
                    response = await client.post(
                        url,
                        headers=headers,
                        json=body,
                        timeout=api_def.timeout
                    )
                elif api_def.method == "PUT":
                    response = await client.put(
                        url,
                        headers=headers,
                        json=body,
                        timeout=api_def.timeout
                    )
                elif api_def.method == "DELETE":
                    response = await client.delete(
                        url,
                        headers=headers,
                        timeout=api_def.timeout
                    )
                else:
                    return PluginResponse.error(f"Unsupported HTTP method: {api_def.method}")

                response.raise_for_status()

                # Parse response
                response_data = response.json() if response.text else {}

                # Apply response mapping if defined
                if api_def.response_mapping:
                    mapped_data = self._map_response(response_data, api_def.response_mapping)
                else:
                    mapped_data = response_data

                return PluginResponse.success(
                    message=f"Successfully executed {intent_def.name}",
                    data=mapped_data
                )

            except httpx.HTTPStatusError as e:
                error_msg = self._map_error(e.response.status_code)
                return PluginResponse.error(error_msg or str(e))
            except httpx.RequestError as e:
                return PluginResponse.error(f"Request failed: {str(e)}")

    def _substitute_template(self, template: str, parameters: Dict[str, Any], url_encode: bool = False) -> str:
        """
        Substitute template variables with values

        Supports:
        - {config.api_key} - from config
        - {params.location} - from parameters

        Args:
            template: Template string with placeholders
            parameters: Parameter values
            url_encode: If True, URL-encode parameter values (for URLs)
        """
        from urllib.parse import quote_plus

        result = template

        # Substitute config variables (never URL-encode config values like API keys)
        for key, value in self.config_cache.items():
            placeholder = f"{{config.{key}}}"
            if placeholder in result:
                result = result.replace(placeholder, str(value) if value is not None else "")

        # Substitute parameter variables
        for key, value in parameters.items():
            placeholder = f"{{params.{key}}}"
            if placeholder in result:
                # URL-encode if this is a URL template
                if url_encode:
                    result = result.replace(placeholder, quote_plus(str(value)))
                else:
                    result = result.replace(placeholder, str(value))

        return result

    def _substitute_body(self, body_template: Dict, parameters: Dict[str, Any]) -> Dict:
        """Recursively substitute template variables in body"""
        result = {}

        for key, value in body_template.items():
            if isinstance(value, str):
                result[key] = self._substitute_template(value, parameters)
            elif isinstance(value, dict):
                result[key] = self._substitute_body(value, parameters)
            elif isinstance(value, list):
                result[key] = [
                    self._substitute_template(item, parameters) if isinstance(item, str) else item
                    for item in value
                ]
            else:
                result[key] = value

        return result

    def _map_response(self, response_data: Dict, mapping: Dict[str, str]) -> Dict:
        """
        Map response data using JSONPath-style mappings

        Example mapping:
            {
                "temperature": "main.temp",
                "conditions": "weather[0].description"
            }
        """
        mapped = {}

        for target_key, source_path in mapping.items():
            value = self._extract_path(response_data, source_path)
            if value is not None:
                mapped[target_key] = value

        return mapped

    def _extract_path(self, data: Any, path: str) -> Any:
        """
        Extract value from nested structure using dot notation

        Supports:
        - "main.temp" - nested object access
        - "weather[0].description" - array access
        """
        parts = path.split('.')
        current = data

        for part in parts:
            # Handle array access
            if '[' in part:
                key, index_str = part.split('[')
                index = int(index_str.rstrip(']'))

                if isinstance(current, dict) and key in current:
                    current = current[key]
                    if isinstance(current, list) and len(current) > index:
                        current = current[index]
                    else:
                        return None
                else:
                    return None
            else:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None

        return current

    def _map_error(self, status_code: int) -> Optional[str]:
        """Map HTTP status code to user-friendly message"""
        if not self.definition.error_mappings:
            return None

        for error_mapping in self.definition.error_mappings:
            if error_mapping.code == status_code:
                return error_mapping.message

        return None
