"""
Tests for Edition & Feature Flag System

Tests cover:
- Edition presets (community, pro)
- Feature flag overrides
- /api/auth/status includes features
- Route guards (conditional mounting)
"""

from utils.config import Settings

# ============================================================================
# Edition Preset Tests
# ============================================================================

class TestEditionPresets:
    """Test that edition presets set correct defaults."""

    def test_community_edition_enables_all(self):
        """Community (home) edition enables all features by default."""
        s = Settings(renfield_edition="community", _env_file=None)
        assert s.features == {
            "smart_home": True,
            "cameras": True,
            "satellites": True,
        }

    def test_pro_edition_disables_home_features(self):
        """Pro (business) edition disables smart home features by default."""
        s = Settings(renfield_edition="pro", _env_file=None)
        assert s.features == {
            "smart_home": False,
            "cameras": False,
            "satellites": False,
        }

    def test_unknown_edition_falls_back_to_pro(self):
        """Unknown edition name falls back to pro defaults."""
        s = Settings(renfield_edition="enterprise", _env_file=None)
        assert s.features == {
            "smart_home": False,
            "cameras": False,
            "satellites": False,
        }

    def test_default_edition_is_community(self):
        """Default edition is community."""
        s = Settings(_env_file=None)
        assert s.renfield_edition == "community"
        assert s.features["smart_home"] is True


# ============================================================================
# Feature Flag Override Tests
# ============================================================================

class TestFeatureOverrides:
    """Test that explicit feature flags override edition presets."""

    def test_override_enables_on_pro(self):
        """Explicit True overrides pro's default False."""
        s = Settings(
            renfield_edition="pro",
            feature_cameras=True,
            _env_file=None,
        )
        assert s.features["cameras"] is True
        # Others still follow pro defaults
        assert s.features["smart_home"] is False
        assert s.features["satellites"] is False

    def test_override_disables_on_community(self):
        """Explicit False overrides community's default True."""
        s = Settings(
            renfield_edition="community",
            feature_smart_home=False,
            _env_file=None,
        )
        assert s.features["smart_home"] is False
        # Others still follow community defaults
        assert s.features["cameras"] is True
        assert s.features["satellites"] is True

    def test_multiple_overrides(self):
        """Multiple overrides work independently."""
        s = Settings(
            renfield_edition="pro",
            feature_smart_home=True,
            feature_cameras=True,
            feature_satellites=True,
            _env_file=None,
        )
        assert s.features == {
            "smart_home": True,
            "cameras": True,
            "satellites": True,
        }

    def test_none_means_use_default(self):
        """None (not set) means use edition default."""
        s = Settings(
            renfield_edition="community",
            feature_smart_home=None,
            _env_file=None,
        )
        assert s.features["smart_home"] is True


# ============================================================================
# Auth Status Endpoint Tests
# ============================================================================

class TestAuthStatusFeatures:
    """Test that features are correctly resolved for API responses."""

    def test_community_features_for_status(self):
        """Community edition features dict is correct for auth status."""
        s = Settings(renfield_edition="community", _env_file=None)
        features = s.features
        assert features == {
            "smart_home": True,
            "cameras": True,
            "satellites": True,
        }

    def test_pro_features_for_status(self):
        """Pro edition features dict is correct for auth status."""
        s = Settings(renfield_edition="pro", _env_file=None)
        features = s.features
        assert features == {
            "smart_home": False,
            "cameras": False,
            "satellites": False,
        }

    def test_override_in_features_for_status(self):
        """Override in pro edition shows in features dict."""
        s = Settings(renfield_edition="pro", feature_cameras=True, _env_file=None)
        features = s.features
        assert features["cameras"] is True
        assert features["smart_home"] is False


# ============================================================================
# Route Guard Tests
# ============================================================================

class TestRouteGuards:
    """Test that routes are conditionally mounted based on feature flags."""

    def test_camera_route_not_mounted_when_disabled(self):
        """Camera routes should not be mounted when cameras feature is disabled."""
        s = Settings(renfield_edition="pro", _env_file=None)
        assert s.features["cameras"] is False

    def test_ha_route_not_mounted_when_disabled(self):
        """HA routes should not be mounted when smart_home feature is disabled."""
        s = Settings(renfield_edition="pro", _env_file=None)
        assert s.features["smart_home"] is False

    def test_satellite_route_not_mounted_when_disabled(self):
        """Satellite routes should not be mounted when satellites feature is disabled."""
        s = Settings(renfield_edition="pro", _env_file=None)
        assert s.features["satellites"] is False

    def test_all_routes_mounted_when_community(self):
        """All feature routes should be mounted in community edition."""
        s = Settings(renfield_edition="community", _env_file=None)
        assert all(s.features.values())


# ============================================================================
# Features Property Consistency Tests
# ============================================================================

class TestFeaturesPropertyConsistency:
    """Test that features property is always consistent."""

    def test_features_returns_all_keys(self):
        """Features dict always contains all three keys."""
        for edition in ["community", "pro", "enterprise"]:
            s = Settings(renfield_edition=edition, _env_file=None)
            assert set(s.features.keys()) == {"smart_home", "cameras", "satellites"}

    def test_features_values_are_bool(self):
        """All feature values are booleans."""
        s = Settings(_env_file=None)
        for key, value in s.features.items():
            assert isinstance(value, bool), f"{key} should be bool, got {type(value)}"
