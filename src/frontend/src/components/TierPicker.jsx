import React from 'react';
import { useTranslation } from 'react-i18next';
import { TIER_SYMBOLS, TIER_CLASS } from './TierBadge';

/**
 * 5-segment tier selector — symbol + label per segment. Keyboard-navigable
 * (arrow keys move selection); follows DESIGN.md tier visual language.
 *
 * Props:
 *   - value: int 0..4 (current tier)
 *   - onChange: (tier: int) => void
 *   - disabled: bool
 */
export default function TierPicker({ value, onChange, disabled = false, className = '' }) {
  const { t } = useTranslation();

  const handleKey = (e, tier) => {
    if (disabled) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault();
      onChange(Math.min(4, tier + 1));
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      onChange(Math.max(0, tier - 1));
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onChange(tier);
    }
  };

  return (
    <div
      role="radiogroup"
      aria-label={t('circles.tierPickerLabel')}
      className={`flex flex-wrap gap-2 ${className}`}
    >
      {[0, 1, 2, 3, 4].map((tier) => {
        const selected = value === tier;
        return (
          <button
            key={tier}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => !disabled && onChange(tier)}
            onKeyDown={(e) => handleKey(e, tier)}
            tabIndex={selected || (value === undefined && tier === 0) ? 0 : -1}
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
