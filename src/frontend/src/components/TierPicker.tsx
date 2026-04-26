import { KeyboardEvent, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';

import type { CircleTier } from './TierBadge';
import { TIER_CLASS, TIER_SYMBOLS } from './TierBadge';

/**
 * 5-segment tier selector — symbol + label per segment. Keyboard-navigable
 * (arrow keys move selection AND focus); follows DESIGN.md tier visual language.
 */
interface TierPickerProps {
  value?: CircleTier | number;
  onChange: (tier: CircleTier) => void;
  disabled?: boolean;
  className?: string;
}

const ALL_TIERS: CircleTier[] = [0, 1, 2, 3, 4];

export default function TierPicker({ value, onChange, disabled = false, className = '' }: TierPickerProps) {
  const { t } = useTranslation();
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // Track which tier *we* just moved to via keyboard so we can restore focus
  // after React re-renders with the new selection. Without this, roving-tabindex
  // leaves focus on the previously-selected (now tabindex=-1) button and the
  // user can't arrow past the end.
  const pendingFocusTier = useRef<CircleTier | null>(null);

  useEffect(() => {
    if (pendingFocusTier.current != null) {
      const tier = pendingFocusTier.current;
      pendingFocusTier.current = null;
      buttonRefs.current[tier]?.focus();
    }
  }, [value]);

  const move = (newTier: number): void => {
    if (disabled) return;
    const clamped = Math.max(0, Math.min(4, newTier)) as CircleTier;
    pendingFocusTier.current = clamped;
    onChange(clamped);
  };

  const handleKey = (e: KeyboardEvent<HTMLButtonElement>, tier: CircleTier): void => {
    if (disabled) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault();
      move(tier + 1);
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      move(tier - 1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      move(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      move(4);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onChange(tier);
    }
  };

  // Roving tabindex: only one button is tabbable. Default to the selected
  // one, or tier 0 if `value` is undefined.
  const focusedTier: CircleTier = (value != null ? (value as CircleTier) : 0);

  return (
    <div
      role="radiogroup"
      aria-label={t('circles.tierPickerLabel')}
      className={`flex flex-wrap gap-2 ${className}`}
    >
      {ALL_TIERS.map((tier) => {
        const selected = value === tier;
        return (
          <button
            key={tier}
            ref={(el) => { buttonRefs.current[tier] = el; }}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => !disabled && onChange(tier)}
            onKeyDown={(e) => handleKey(e, tier)}
            tabIndex={tier === focusedTier ? 0 : -1}
            className={`tier-badge ${TIER_CLASS[tier]} cursor-pointer
                        ${selected ? 'ring-2 ring-accent-500 ring-offset-1 dark:ring-offset-gray-900' : ''}
                        ${disabled ? 'opacity-50 cursor-not-allowed' : 'hover:scale-105 transition-transform'}`}
          >
            <span aria-hidden="true" className="font-bold">{TIER_SYMBOLS[tier]}</span>
            <span>{t(`circles.tier.${tier}`)}</span>
          </button>
        );
      })}
    </div>
  );
}
