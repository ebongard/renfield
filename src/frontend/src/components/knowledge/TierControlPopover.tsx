import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import TierBadge, { type CircleTier } from '../TierBadge';
import TierPicker from '../TierPicker';

/**
 * Clickable visibility control: a tier trigger that opens a popover with the
 * colored 5-tier picker. The whole point of putting it where documents live is
 * discoverability + at-a-glance current tier, so the trigger shows the tier in
 * colour (badge) at rest.
 *
 * Moving something to PUBLIC (tier 4) is privacy-sensitive (federation peers can
 * then read it), so a `confirmPublic` gate can veto that one transition — the
 * parent supplies a warning confirm. All other tiers commit immediately.
 *
 * Generic across atom kinds (documents now; facts/memories/KG entities later):
 * it only knows `tier` + `onChange`. a11y: the trigger is a real button with
 * aria-haspopup/expanded; the picker is TierPicker's roving-tabindex radiogroup;
 * Esc + outside-click close and restore focus to the trigger.
 */
interface TierControlPopoverProps {
  tier: number;
  onChange: (tier: CircleTier) => void;
  /** Async gate invoked before committing tier 4 (public). Return false to veto. */
  confirmPublic?: () => Promise<boolean>;
  disabled?: boolean;
  busy?: boolean;
  /** When set, the trigger is a labelled button (bulk) instead of a tier badge. */
  triggerLabel?: string;
}

export default function TierControlPopover({
  tier,
  onChange,
  confirmPublic,
  disabled = false,
  busy = false,
  triggerLabel,
}: TierControlPopoverProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverId = useId();

  const current = Math.max(0, Math.min(4, Number(tier) || 0)) as CircleTier;

  // Close on outside click + Esc; restore focus to the trigger on Esc.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const handlePick = async (next: CircleTier) => {
    if (next === current) {
      setOpen(false);
      return;
    }
    if (next === 4 && confirmPublic) {
      const ok = await confirmPublic();
      if (!ok) return; // keep the popover open so they can pick a different tier
    }
    onChange(next);
    setOpen(false);
    triggerRef.current?.focus();
  };

  return (
    <div ref={rootRef} className="relative inline-flex">
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled || busy}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        className={`inline-flex items-center min-h-11 rounded-md disabled:opacity-50 ${
          triggerLabel
            ? 'btn-secondary gap-2 px-3 text-sm'
            : 'px-1 hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500'
        } ${busy ? 'animate-pulse' : ''}`}
        title={triggerLabel ?? t('circles.tierPickerLabel')}
      >
        {triggerLabel ? triggerLabel : <TierBadge tier={current} />}
      </button>

      {open && (
        <div
          id={popoverId}
          role="dialog"
          aria-label={t('circles.tierPickerLabel')}
          className="absolute z-20 top-full left-0 mt-1 card shadow-lg p-3 w-64 space-y-2"
        >
          <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('circles.tierPickerLabel')}
          </p>
          <TierPicker
            value={triggerLabel ? undefined : current}
            onChange={handlePick}
            variant="pills"
            disabled={busy}
          />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t(`circles.tierDescription.${current}`)}
          </p>
        </div>
      )}
    </div>
  );
}
