/**
 * DocumentRefArtifact -- the button to the Paperless document under a scan
 * completion notice.
 *
 * 🛑 The artifact carries ONLY a document id, never a URL. The link is resolved
 * here at render time, for two measured reasons (2026-10-27, household,
 * 60 days):
 *   - At write time it usually does not exist yet: Paperless filing is a
 *     decoupled follow-up, and the ingest averaged 363 s.
 *   - 6 of 37 completed documents NEVER get an id of their own -- they are
 *     duplicates. A link frozen at write time would be permanently empty
 *     there, without saying why.
 * Because the person looks at the answer again minutes later anyway, the id is
 * usually there by the time they click.
 *
 * 🛑 Only the clickable state is a focusable element. The others are plain text:
 * no tabbing through dead buttons, and the screen reader does not announce a
 * control that is not one. A `disabled` button would also regularly fall below
 * 4.5:1 contrast (DESIGN.md §Accessibility).
 *
 * 🛑 NO amber for "not filed". A duplicate is not a failure -- the document is in
 * Paperless, just for longer. Showing a warning colour on roughly every sixth
 * scan wears it out; amber stays reserved for the cases DESIGN.md introduced
 * it for on 2026-09-28.
 */
import { useTranslation } from 'react-i18next';

import { useDocumentQuery } from '../../../api/resources/knowledge';
import type { DocumentRefData } from './artifactSchema';

/** While fetching -- the same shape as the artifact frame's skeleton. */
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
  // 🛑 `instance` set means: the document lives elsewhere. Do NOT query locally
  // -- the id belongs to the other instance, and because both run their own
  // sequences a local query would hit an unrelated document with the same
  // number. So the fetch is never started in the first place
  // (`enabled: false`), not merely its result discarded.
  const remote = data.instance;
  const { data: doc, isLoading, isError } = useDocumentQuery(data.documentId, !remote);

  if (remote) {
    return <Hint text={t('chat.artifacts.documentRef.otherInstance', { instance: remote })} />;
  }

  if (isLoading) return <Skeleton />;

  // 🛑 A failed fetch looks like "still in progress", and that is deliberate:
  // for the person both mean the same thing -- no link yet. The endpoint
  // enforces access control, so a 403 is a valid answer and not an error to
  // show them.
  if (isError || !doc) return <Hint text={t('chat.artifacts.documentRef.filing')} />;

  if (doc.paperless_url) {
    return (
      <a
        href={doc.paperless_url}
        target="_blank"
        rel="noopener noreferrer"
        // min-h-11 = 44px tap target (DESIGN.md §Accessibility); rounded-sm is
        // the button step of the radius scale, not the card radius.
        className="btn-secondary inline-flex min-h-11 items-center rounded-sm text-sm"
      >
        {t('chat.artifacts.documentRef.open')}
      </a>
    );
  }

  // Reported in Paperless but without an id of its own: that is the
  // duplicate. `in_paperless` is true exactly when an id exists OR the filing
  // run reported "done" -- with no link, only the second case remains.
  if (doc.in_paperless) {
    return <Hint text={t('chat.artifacts.documentRef.alreadyThere')} />;
  }

  // Still in progress while the ingest runs; after that it is a genuine
  // not-filed (Paperless refused).
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
