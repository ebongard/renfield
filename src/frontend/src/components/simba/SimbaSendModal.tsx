import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader, CheckCircle2, AlertTriangle } from 'lucide-react';

import Modal from '../Modal';
import SimbaProposalForm, { type SimbaResolution } from './SimbaProposalForm';
import {
  useSendDocumentToSimba,
  useSimbaCategoriesQuery,
  type SimbaProposal,
} from '../../api/resources/simbaIngest';

interface Props {
  /** The document to send; `null` = closed. */
  documentId: number | null;
  /** Display name for the header + confirmation copy. */
  filename: string;
  /** From `simba_ingest_review_enabled`. */
  enabled: boolean;
  onClose: () => void;
}

/**
 * Doc-page "send to Simba" overlay — the natural inline flow: clicking the
 * action opens this modal, which creates (or reuses) the pending proposal,
 * prefills the confirm form, and performs the IRREVERSIBLE upload in place with
 * an explicit styled confirm and a positive success acknowledgement — instead of
 * firing a background action and bouncing the user to another page. Cancelling
 * leaves the proposal queued on /brain/review (the durable fallback).
 */
export default function SimbaSendModal({ documentId, filename, enabled, onClose }: Props) {
  const { t } = useTranslation();
  const send = useSendDocumentToSimba();
  const categoriesQuery = useSimbaCategoriesQuery(enabled && documentId != null);
  const [proposal, setProposal] = useState<SimbaProposal | null>(null);
  const [result, setResult] = useState<SimbaResolution | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isOpen = documentId != null;

  // On open, create/reuse the pending proposal to obtain its id + prefill fields.
  // Intentionally keyed on documentId only — filename/send/t are stable for a
  // given open and re-running on their identity would double-create.
  useEffect(() => {
    if (documentId == null) {
      setProposal(null);
      setResult(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setError(null);
    setResult(null);
    setProposal(null);
    send.mutate(documentId, {
      onSuccess: (data) => {
        if (cancelled) return;
        if (!data.success || data.proposal_id == null) {
          setError(data.message || t('simbaReview.uploadFailed'));
          return;
        }
        setProposal({
          id: data.proposal_id,
          document_id: documentId,
          filename,
          suggested_category: data.suggested_category,
          suggested_type: data.suggested_type,
          suggested_description: data.suggested_description,
        });
      },
      onError: (e) => {
        if (!cancelled) setError((e as Error).message || t('simbaReview.uploadFailed'));
      },
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId]);

  const onResolved = (r: SimbaResolution) => {
    if (r.uploaded) {
      setResult(r);
    } else {
      onClose(); // rejected/discarded — nothing to celebrate
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('simbaSend.title')} maxWidth="max-w-xl">
      <p className="text-sm font-medium text-gray-900 dark:text-white truncate mb-1" title={filename}>
        {filename}
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">{t('simbaSend.hint')}</p>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-700/60 dark:bg-red-900/20 dark:text-red-200">
          <p className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </p>
          <button onClick={onClose} className="btn-secondary min-h-[44px] mt-3">
            {t('common.close')}
          </button>
        </div>
      ) : result?.uploaded ? (
        <div className="rounded-lg border border-green-300 bg-green-50 p-4 text-sm text-green-800 dark:border-green-700/60 dark:bg-green-900/20 dark:text-green-200">
          <p className="flex items-center gap-2 font-medium mb-1">
            <CheckCircle2 className="w-5 h-5 shrink-0" aria-hidden="true" />
            {t('simbaSend.successTitle')}
          </p>
          <p className="mb-3">{t('simbaSend.success', { filename })}</p>
          <button onClick={onClose} className="btn-primary min-h-[44px]">
            {t('common.close')}
          </button>
        </div>
      ) : !proposal || categoriesQuery.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400 py-6 justify-center">
          <Loader className="w-4 h-4 animate-spin" aria-hidden="true" />
          {t('simbaSend.preparing')}
        </div>
      ) : (
        <SimbaProposalForm
          proposal={proposal}
          categories={categoriesQuery.data ?? {}}
          onResolved={onResolved}
          showReject={false}
          autoFocus
        />
      )}
    </Modal>
  );
}
