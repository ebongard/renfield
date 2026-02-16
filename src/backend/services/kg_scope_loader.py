"""
Knowledge Graph Scope Loader — Load scope definitions from YAML config.
"""
import yaml
from pathlib import Path
from loguru import logger
from models.database import KG_SCOPE_PERSONAL


class KGScopeLoader:
    """Loads and validates KG scope definitions from YAML."""

    def __init__(self, config_path: str = "config/kg_scopes.yaml"):
        self.config_path = Path(config_path)
        self._scopes = {}
        self.load()

    def load(self):
        """Load scopes from YAML file."""
        if not self.config_path.exists():
            logger.warning(f"KG scopes file not found: {self.config_path}, using defaults")
            self._load_defaults()
            return

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)

            scopes_list = data.get('scopes', [])
            for scope_def in scopes_list:
                name = scope_def.get('name')
                if not name:
                    logger.warning("KG scope missing 'name', skipping")
                    continue

                self._scopes[name] = {
                    'label_de': scope_def.get('label_de', name),
                    'label_en': scope_def.get('label_en', name),
                    'description_de': scope_def.get('description_de', ''),
                    'description_en': scope_def.get('description_en', ''),
                    'roles': scope_def.get('roles', []),
                }

            logger.info(f"Loaded {len(self._scopes)} KG scopes from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load KG scopes: {e}")
            self._load_defaults()

    def _load_defaults(self):
        """Load default scopes if YAML fails."""
        self._scopes = {
            'family': {
                'label_de': 'Familie',
                'label_en': 'Family',
                'description_de': 'Sichtbar für Familienmitglieder',
                'description_en': 'Visible to family members',
                'roles': ['Admin', 'Familie'],
            },
            'public': {
                'label_de': 'Öffentlich',
                'label_en': 'Public',
                'description_de': 'Sichtbar für alle Benutzer',
                'description_en': 'Visible to all users',
                'roles': ['Admin', 'Familie', 'Gast'],
            },
        }

    def get_all_scopes(self, lang: str = 'de') -> list[dict]:
        """
        Get all available scopes including 'personal'.

        Returns:
            [
                {'name': 'personal', 'label': 'Persönlich', 'description': '...'},
                {'name': 'family', 'label': 'Familie', 'description': '...'},
                ...
            ]
        """
        label_key = f'label_{lang}'
        desc_key = f'description_{lang}'

        # Built-in personal scope
        result = [{
            'name': KG_SCOPE_PERSONAL,
            'label': 'Persönlich' if lang == 'de' else 'Personal',
            'description': 'Nur für den Besitzer sichtbar' if lang == 'de' else 'Visible only to owner',
        }]

        # Custom scopes from YAML
        for name, scope_def in self._scopes.items():
            result.append({
                'name': name,
                'label': scope_def.get(label_key, name),
                'description': scope_def.get(desc_key, ''),
            })

        return result

    def is_valid_scope(self, scope: str) -> bool:
        """Check if a scope name is valid."""
        return scope == KG_SCOPE_PERSONAL or scope in self._scopes

    def can_access_scope(self, scope: str, user_role: str | None) -> bool:
        """
        Check if a user with the given role can access entities with this scope.

        Args:
            scope: Scope name (e.g., 'family', 'public')
            user_role: User's role name (e.g., 'Familie', 'Gast')

        Returns:
            True if user can access this scope
        """
        if scope == KG_SCOPE_PERSONAL:
            # Personal scope is owner-only, checked separately
            return True

        scope_def = self._scopes.get(scope)
        if not scope_def:
            logger.warning(f"Unknown scope: {scope}")
            return False

        if user_role is None:
            # Unauthenticated users cannot access custom scopes
            return False

        return user_role in scope_def.get('roles', [])

    def get_accessible_scopes(self, user_role: str | None, include_personal: bool = True) -> list[str]:
        """
        Get list of scope names accessible to a user.

        Args:
            user_role: User's role name
            include_personal: Whether to include 'personal' in the list

        Returns:
            ['personal', 'family', 'public'] (example)
        """
        result = []
        if include_personal:
            result.append(KG_SCOPE_PERSONAL)

        for name, scope_def in self._scopes.items():
            if user_role and user_role in scope_def.get('roles', []):
                result.append(name)

        return result


# Singleton instance
_scope_loader = None


def get_scope_loader() -> KGScopeLoader:
    """Get the global scope loader instance."""
    global _scope_loader
    if _scope_loader is None:
        _scope_loader = KGScopeLoader()
    return _scope_loader
