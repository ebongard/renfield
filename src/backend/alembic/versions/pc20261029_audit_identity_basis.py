"""federation_query_log: Richtung und Identitaetsgrundlage festhalten.

Bis heute hielt das Pruefprotokoll Status, Signatur, Anfragetext und
Antwortauszug fest — aber NICHT, auf welcher Identitaet eine foederierte
Antwort beruhte. Fuer eine Unterscheidung, die ueber die REICHWEITE eines
Zugriffs entscheidet, ist das zu wenig:

  gemappt   -> die Gegenstelle antwortet mit der vollen eigenen Reichweite
               der verknuepften Person
  Rueckfall -> sie antwortet als „die andere Instanz", nur auf der
               gewaehrten Stufe

Am 2026-10-29 lief der erste erfolgreiche foederierte Zug der
Projektgeschichte. Welcher der beiden Faelle es war, liess sich hinterher
NICHT feststellen — nur aus dem gleichen `querier_ref` auf beiden Seiten und
aus dem Code erschliessen. Der Pending-Satz, der `mapped` traegt, liegt im
Arbeitsspeicher und ist nach dem Zug weg.

🛑 Und: geschrieben hat bisher nur die FRAGERSEITE (`mcp_client.py`, ein
einziger Aufrufer). Die Entscheidung ueber die Identitaet faellt aber beim
ANTWORTENDEN — also muss er sie festhalten. Ein Vermerk auf der Fragerseite
waere die Behauptung der Gegenseite ueber ihre eigene Zugriffsentscheidung.
Darum `direction`: dieselbe Tabelle traegt jetzt beide Seiten.

`user_id` bleibt NOT NULL und bedeutet durchgehend „die lokale Person, deren
Reichweite benutzt wurde": beim Frager die fragende Person, beim Antwortenden
die verknuepfte Person (gemappt) bzw. der Eigentuemer der Paarung
(Rueckfall). Beide Faelle haben eine.

Revision ID: pc20261029_audit_basis
Revises: pc20261027_peer_tier
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261029_audit_basis"
down_revision: str | None = "pc20261027_peer_tier"
branch_labels: None = None
depends_on: None = None


def _has_column(table: str, column: str) -> bool:
    """Entwicklungsstaende fahren `create_all` und haben die Spalte dann schon."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("federation_query_log", "direction"):
        op.add_column(
            "federation_query_log",
            # Bestandszeilen sind alle von der Fragerseite — es gab keine
            # andere. `asked` ist damit kein Vorgabewert, sondern die Wahrheit.
            sa.Column("direction", sa.String(8), nullable=False,
                      server_default="asked"),
        )
    if not _has_column("federation_query_log", "identity_basis"):
        op.add_column(
            "federation_query_log",
            # Nullable: Bestandszeilen WISSEN es nicht, und das soll man sehen.
            # Ein Vorgabewert wuerde Unwissen als Tatsache ausgeben.
            sa.Column("identity_basis", sa.String(16), nullable=True),
        )


def downgrade() -> None:
    # 🛑 Der Rueckweg VERLIERT die Angabe, auf welcher Identitaet geantwortet
    # wurde — sie steht nirgends sonst. Aus „nachher" ist „vorher" hier
    # rekonstruierbar (die Spalten entfallen schlicht), aber die Protokollzeilen
    # der Antwortseite bleiben als `asked` zurueck und sind dann nicht mehr von
    # echten Frager-Zeilen zu unterscheiden.
    if _has_column("federation_query_log", "identity_basis"):
        op.drop_column("federation_query_log", "identity_basis")
    if _has_column("federation_query_log", "direction"):
        op.drop_column("federation_query_log", "direction")
