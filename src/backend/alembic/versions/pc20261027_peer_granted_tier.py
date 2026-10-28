"""peer_users.granted_tier — die Peer-Stufe gehoert an die Paarung (F-ID-3, BL-0135).

Bis heute schrieb `pairing_service._upsert_circle_membership` die ENTFERNTE
Nutzerkennung in `circle_memberships.member_user_id` — eine Spalte, deren
Fremdschluessel auf die LOKALE `users.id` zeigt. Zwei Folgen, beide am
2026-10-27 live beobachtet:

  - x-idra (nur Nutzer 1) bekam beim Abschluss `responder_user_id=2` → der
    Fremdschluessel brach, die Paarung starb mit einer 500.
  - Der Haushalt (Nutzer 1..8) nahm vorher `initiator_user_id=1` an → die Zahl
    gab es, also KEIN Fehler — stattdessen eine Zugriffszeile `owner=2
    member=1` fuer das eigene lokale Konto 1 statt fuer den Peer. Dieselbe
    Zahl, eine andere Person.

Die Stufe zieht darum an die Paarung. Danach gibt es keine Stelle mehr, an der
eine fremde Zahl fuer eine lokale Person gehalten wird.

🛑 KEIN Backfill aus den alten Mitgliedschaften. Bestehende Peers bekommen 4,
die ENGSTE Stufe. Ob eine Zeile `(owner=1, member=1, tier=2)` eine echte
Mitgliedschaft oder ein Paarungsrest ist, steht nirgends — und genau diese
Ununterscheidbarkeit IST der Fehler. Sie zur Grundlage einer Datenmigration zu
machen hiesse, ihn fortzuschreiben. Wer eine bestehende Paarung offen halten
will, setzt die Stufe sichtbar neu, statt sie zu erben.

🛑 Die Leiter laeuft gegen die Anschauung: `granted_tier <= atom.circle_tier`,
eine KLEINERE Zahl gewaehrt MEHR. Die Vorgabe 4 ist deshalb die sichere, nicht
die grosszuegige.

Revision ID: pc20261027_peer_tier
Revises: pc20260929_kg_validity
Create Date: 2026-10-27
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261027_peer_tier"
down_revision: str | None = "pc20260929_kg_validity"
branch_labels: None = None
depends_on: None = None


def _has_column(table: str, column: str) -> bool:
    """Entwicklungsstaende fahren `Base.metadata.create_all` und haben die
    Spalte dann schon. Ohne diesen Riegel scheitert die Migration dort."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("peer_users", "granted_tier"):
        return
    op.add_column(
        "peer_users",
        sa.Column(
            "granted_tier",
            sa.Integer(),
            nullable=False,
            # Bestandszeilen erben die engste Stufe — siehe Docstring.
            server_default="4",
        ),
    )


def downgrade() -> None:
    # 🛑 Der Rueckweg VERLIERT die Gewaehrungen: davor lagen sie in
    # `circle_memberships`, und diese Migration schreibt dorthin nichts
    # zurueck. Nach einem Downgrade steht jede Paarung ohne Stufe da und der
    # peer-bezogene Filter faellt auf „nur Oeffentliches" — fail-closed, aber
    # die Stufen sind weg und muessen neu gesetzt werden.
    if _has_column("peer_users", "granted_tier"):
        op.drop_column("peer_users", "granted_tier")
