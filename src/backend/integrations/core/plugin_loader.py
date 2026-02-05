"""
Plugin loader - parses and validates YAML plugin definitions
"""
import os
from pathlib import Path

import yaml
from loguru import logger

from .plugin_schema import PluginDefinition


class PluginLoader:
    """Loads and validates YAML plugin definitions"""

    def __init__(self, plugin_dir: str = "backend/integrations/plugins"):
        # Resolve plugin directory path intelligently
        plugin_path = Path(plugin_dir)

        # If absolute path, use it directly
        if plugin_path.is_absolute():
            self.plugin_dir = plugin_path
        else:
            # Try multiple resolution strategies
            # 1. Relative to current working directory
            if plugin_path.exists():
                self.plugin_dir = plugin_path.resolve()
            # 2. Relative to backend directory (where main.py is)
            elif (Path.cwd() / plugin_dir).exists():
                self.plugin_dir = (Path.cwd() / plugin_dir).resolve()
            # 3. Relative to project root (parent of backend)
            elif (Path.cwd().parent / plugin_dir).exists():
                self.plugin_dir = (Path.cwd().parent / plugin_dir).resolve()
            # 4. Strip 'backend/' prefix and try relative to cwd
            elif plugin_dir.startswith("backend/"):
                stripped = plugin_dir.replace("backend/", "", 1)
                if (Path.cwd() / stripped).exists():
                    self.plugin_dir = (Path.cwd() / stripped).resolve()
                else:
                    # Use original path as fallback
                    self.plugin_dir = plugin_path
            else:
                # Use original path as fallback
                self.plugin_dir = plugin_path

        self.loaded_plugins: dict[str, PluginDefinition] = {}
        logger.debug(f"🔍 Plugin directory resolved to: {self.plugin_dir.absolute()}")

    def _scan_plugin_files(self) -> list[Path]:
        """
        Scan plugin directory for YAML files.

        Returns:
            List of Path objects for plugin YAML files
        """
        if not self.plugin_dir.exists():
            logger.warning(f"⚠️  Plugin directory not found: {self.plugin_dir}")
            return []

        yaml_files = list(self.plugin_dir.glob("*.yaml")) + list(self.plugin_dir.glob("*.yml"))
        logger.debug(f"🔍 Found {len(yaml_files)} plugin files in {self.plugin_dir}")
        return yaml_files

    def load_all_plugins(self) -> dict[str, PluginDefinition]:
        """
        Load all plugins from plugin directory

        Returns:
            Dict mapping plugin name to PluginDefinition
        """
        logger.info(f"🔌 Loading plugins from {self.plugin_dir} (absolute: {self.plugin_dir.absolute()})")
        logger.debug(f"🔍 Current working directory: {Path.cwd()}")

        if not self.plugin_dir.exists():
            logger.warning(f"⚠️  Plugin directory not found: {self.plugin_dir}")
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"📁 Created plugin directory: {self.plugin_dir}")
            return {}

        yaml_files = self._scan_plugin_files()

        if not yaml_files:
            logger.info(f"📭 No plugin files found in {self.plugin_dir}")
            return {}

        for yaml_file in yaml_files:
            try:
                plugin = self._load_plugin_file(yaml_file)
                if plugin and self._is_plugin_enabled(plugin):
                    self.loaded_plugins[plugin.metadata.name] = plugin
                    logger.info(f"✅ Loaded plugin: {plugin.metadata.name} v{plugin.metadata.version}")
                elif plugin:
                    logger.info(f"⏭️  Skipped disabled plugin: {plugin.metadata.name}")
            except Exception as e:
                logger.error(f"❌ Failed to load plugin from {yaml_file}: {e}")

        logger.info(f"🎉 Loaded {len(self.loaded_plugins)} plugins")
        return self.loaded_plugins

    def _load_plugin_file(self, file_path: Path) -> PluginDefinition | None:
        """Load and validate single plugin YAML file"""
        try:
            with open(file_path, encoding='utf-8') as f:
                raw_data = yaml.safe_load(f)

            if not raw_data:
                logger.warning(f"⚠️  Empty plugin file: {file_path}")
                return None

            # Transform flat structure to nested structure for Pydantic
            plugin_data = self._transform_yaml_structure(raw_data)

            # Validate using Pydantic
            plugin = PluginDefinition(**plugin_data)

            # Validate config variables exist
            self._validate_config_vars(plugin)

            return plugin
        except yaml.YAMLError as e:
            logger.error(f"❌ YAML parsing error in {file_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Plugin validation error in {file_path}: {e}")
            return None

    def _transform_yaml_structure(self, raw_data: dict) -> dict:
        """
        Transform flat YAML structure into nested Pydantic structure

        YAML:
            name: weather
            version: 1.0
            enabled_var: WEATHER_ENABLED
            config:
              url: OPENWEATHER_API_URL

        Transforms to:
            metadata:
              name: weather
              version: 1.0
              enabled_var: WEATHER_ENABLED
            config:
              url: OPENWEATHER_API_URL
        """
        metadata_fields = ['name', 'version', 'description', 'author', 'enabled_var']

        metadata = {k: raw_data.get(k) for k in metadata_fields if k in raw_data}

        # Validate required metadata fields
        if 'name' not in metadata:
            raise ValueError("Plugin must have a 'name' field")
        if 'enabled_var' not in metadata:
            raise ValueError("Plugin must have an 'enabled_var' field")

        return {
            'metadata': metadata,
            'config': raw_data.get('config', {}),
            'intents': raw_data.get('intents', []),
            'error_mappings': raw_data.get('error_mappings', []),
            'rate_limit': raw_data.get('rate_limit')
        }

    def _is_plugin_enabled(self, plugin: PluginDefinition) -> bool:
        """Check if plugin is enabled via environment variable"""
        enabled_var = plugin.metadata.enabled_var
        value = os.getenv(enabled_var, "false").lower()
        enabled = value in ("true", "1", "yes", "on")

        if enabled:
            logger.debug(f"✅ Plugin '{plugin.metadata.name}' enabled ({enabled_var}={value})")
        else:
            logger.debug(f"⏭️  Plugin '{plugin.metadata.name}' disabled ({enabled_var}={value})")

        return enabled

    def _validate_config_vars(self, plugin: PluginDefinition):
        """Validate that required config variables exist in environment or secrets"""
        from integrations.core.generic_plugin import GenericPlugin
        config = plugin.config
        missing_vars = []

        if config.url:
            if not GenericPlugin._resolve_config_var(config.url):
                missing_vars.append(config.url)

        if config.api_key:
            if not GenericPlugin._resolve_config_var(config.api_key):
                missing_vars.append(config.api_key)

        if config.additional:
            for var_name in config.additional.values():
                if not GenericPlugin._resolve_config_var(var_name):
                    missing_vars.append(var_name)

        if missing_vars:
            logger.warning(
                f"⚠️  Plugin '{plugin.metadata.name}' has missing config vars: {missing_vars}. "
                f"Plugin will be loaded but may fail at runtime."
            )
