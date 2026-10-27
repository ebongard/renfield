/**
 * DocumentRefArtifact — der Knopf zum Paperless-Dokument unter einer
 * Scan-Fertigmeldung.
 *
 * 🛑 Das Artefakt traegt NUR eine Dokumentkennung, nie eine URL. Der Link wird
 * hier beim Anzeigen aufgeloest, und zwar aus zwei gemessenen Gruenden
 * (2026-10-27, Haushalt, 60 Tage):
 *   - Zur Schreibzeit gibt es ihn meist noch nicht: die Paperless-Ablage ist ein
 *     entkoppelter Nachlauf, der Ingest brauchte im Mittel 363 s.
 *   - 6 von 37 abgeschlossenen Dokumenten bekommen NIE eine eigene Kennung —
 *     sie sind Dubletten. Ein beim Schreiben eingefrorener Link waere dort
 *     dauerhaft leer, ohne zu sagen warum.
 * Weil die Person die Antwort ohnehin erst Minuten spaeter wieder ansieht, ist
 * die Kennung beim Klicken meist da.
 *
 * 🛑 Nur der klickbare Zustand ist ein Element mit Fokus. Die uebrigen sind
 * schlichter Text: kein Tabben durch tote Schaltflaechen, und der Screenreader
 * kuendigt keine Taste an, die keine ist. Ein `disabled`-Knopf waere zudem
 * regelmaessig unter 4,5:1 Kontrast (DESIGN.md §Accessibility).
 *
 * 🛑 KEIN Bernstein fuer „nicht abgelegt". Eine Dublette ist kein Fehlschlag —
 * das Dokument liegt in Paperless, nur schon laenger. Bei rund jedem sechsten
 * Scan eine Warnfarbe zu zeigen nutzt sie ab; Bernstein bleibt den Faellen
 * vorbehalten, fuer die DESIGN.md es am 2026-09-28 eingefuehrt hat.
 */
import { useTranslation } from 'react-i18next';

import { useDocumentQuery } from '../../../api/resources/knowledge';
import type { DocumentRefData } from './artifactSchema';

/** Waehrend des Abrufs — dieselbe Gestalt wie das Geruest des Artefakt-Rahmens. */
function Skeleton() {
  return (
    <div
      className="animate-pulse motion-reduce:animate-none"
      aria-hidden="true"
    >
      <div className="h-8 w-44 rounded-sm bg-gray-200 dark:bg-gray-700" />
    </div>
  );
}

function Hint({ text }: { text: string }) {
  return <p className="text-sm text-gray-500 dark:text-gray-400">{text}</p>;
}

export default function DocumentRefArtifact({ data }: { data: DocumentRefData }) {
  const { t } = useTranslation();
  const { data: doc, isLoading, isError } = useDocumentQuery(data.documentId);

  if (isLoading) return <Skeleton />;

  // 🛑 Ein gescheiterter Abruf sieht aus wie „noch in Arbeit", und das ist
  // Absicht: fuer die Person bedeutet beides dasselbe — noch kein Link. Der
  // Endpunkt setzt den Zugriffsschutz durch, ein 403 ist also eine gueltige
  // Antwort und kein Fehler, den man ihr zeigen muesste.
  if (isError || !doc) return <Hint text={t('chat.artifacts.documentRef.filing')} />;

  if (doc.paperless_url) {
    return (
      <a
        href={doc.paperless_url}
        target="_blank"
        rel="noopener noreferrer"
        // min-h-11 = 44px Tippflaeche (DESIGN.md §Accessibility); rounded-sm ist
        // die Knopf-Stufe der Radius-Skala, nicht der Karten-Radius.
        className="btn-secondary inline-flex min-h-11 items-center rounded-sm text-sm"
      >
        {t('chat.artifacts.documentRef.open')}
      </a>
    );
  }

  // In Paperless gemeldet, aber ohne eigene Kennung: das ist die Dublette.
  // `in_paperless` ist genau dann wahr, wenn eine Kennung existiert ODER der
  // Ablagelauf „done" meldete — faellt der Link, bleibt nur der zweite Fall.
  if (doc.in_paperless) {
    return <Hint text={t('chat.artifacts.documentRef.alreadyThere')} />;
  }

  // Noch in Arbeit, solange der Ingest laeuft; danach ist es ein echtes
  // Nichtablegen (Paperless hat abgelehnt).
  const stillWorking = doc.status === 'pending' || doc.status === 'processing';
  return (
    <Hint
      text={t(
        stillWorking
          ? 'chat.artifacts.documentRef.filing'
          : 'chat.artifacts.documentRef.notFiled',
      )}
    />
  );
}
