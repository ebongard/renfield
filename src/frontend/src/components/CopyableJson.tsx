import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Check, Copy } from 'lucide-react';

interface CopyableJsonProps {
  id: string;
  value: unknown;
  copyLabel: string;
}

/**
 * A signed pairing or identity-link blob as selectable text, with a copy button.
 *
 * The clipboard API exists only in a secure context: an instance served over
 * plain HTTP, or one whose self-signed certificate the browser does not trust,
 * refuses it. The dialogs used to offer only a QR code next to the button, so a
 * refused copy left nothing to carry to the other side. The text is the
 * fallback: a refused copy selects it and says so.
 */
export default function CopyableJson({ id, value, copyLabel }: CopyableJsonProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLTextAreaElement>(null);
  const [state, setState] = useState<'idle' | 'copied' | 'refused'>('idle');
  const text = JSON.stringify(value);

  const selectAll = () => ref.current?.select();

  const handleCopy = async () => {
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text);
      setState('copied');
      setTimeout(() => setState('idle'), 2000);
    } catch {
      selectAll();
      setState('refused');
    }
  };

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-xs font-medium text-gray-700 dark:text-gray-300">
          {t('circles.jsonAsText')}
        </label>
        <button
          type="button"
          onClick={handleCopy}
          className="btn-icon btn-icon-ghost"
          title={copyLabel}
          aria-label={copyLabel}
        >
          {state === 'copied' ? <Check className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
        </button>
      </div>
      <textarea
        id={id}
        ref={ref}
        readOnly
        value={text}
        rows={3}
        onFocus={selectAll}
        onClick={selectAll}
        className="input font-mono text-xs"
      />
      {state === 'refused' && (
        <p role="status" className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
          <AlertTriangle className="w-3 h-3 shrink-0" aria-hidden="true" />
          {t('circles.copyRefused')}
        </p>
      )}
    </div>
  );
}
