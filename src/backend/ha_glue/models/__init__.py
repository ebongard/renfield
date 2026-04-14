"""Database models for the HA-glue layer.

All SQLAlchemy classes here share the platform's `Base` (imported from
`models.database`) so they register with the same metadata. This means
`Base.metadata.create_all()` creates ha-glue tables as well when this
package is imported, and platform string-based `relationship("Room")`
references resolve correctly at mapper-configure time.

For platform-only deployments that don't need HA features, simply don't
import `ha_glue.*` — the tables won't register and `create_all()` will
produce a lean platform-only schema.
"""
