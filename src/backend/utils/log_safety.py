"""Fehlermeldungen protokollieren, ohne Nutzerinhalt mitzuschicken.

🛑 Der Anlass, am 2026-10-29 im Review gefunden: der Atom-Abruf hatte einen
Riegel gegen Inhalt im Protokoll — und vier der acht Abrufquellen umgingen ihn,
weil sie die rohe Ausnahme protokollieren (`logger.warning(f"… {e}")`).

SQLAlchemy haengt an seine Meldungen

    [SQL: SELECT ... WHERE ts @@ websearch_to_tsquery(%(or_query)s)]
    [parameters: {'or_query': '<die Frage der Person>'}]

Die gebundene Anfrage IST der Nutzertext. In `lexical_retrieval` ist es die
Suchanfrage, in `document_fact_retrieval` und `note_retrieval` dasselbe.

🛑 Der Schnitt bei `[SQL:` ist eine Untergrenze, keine Garantie. Zwei Formate
tragen den Wert in der PRIMAERmeldung und ueberleben ihn deshalb:
`invalid input syntax for type <t>: "<wert>"` (Postgres) und
`invalid input for query argument $N: <repr(wert)>` (asyncpg). Beide entstehen
nur bei einem Typfehler in der Bindung, nicht auf den Suchpfaden hier. Wer den
Riegel dicht haben will, protokolliert `type(e).__name__` plus `sqlstate` und
verzichtet auf die Meldung — das kostet aber genau die Diagnose, fuer die die
Zeile da ist. Die Abwaegung steht hier, damit sie nicht wieder erraten wird.
"""
from __future__ import annotations

#: Mehr als das braucht niemand zur Einordnung, und je laenger die Meldung,
#: desto wahrscheinlicher traegt sie etwas mit.
MAX_LEN = 200


def fehlertext_ohne_inhalt(e: BaseException) -> str:
    """Die Fehlermeldung bis vor den `[SQL: …]`-Anhang, gekappt."""
    return str(e).split("[SQL:")[0].strip()[:MAX_LEN]
