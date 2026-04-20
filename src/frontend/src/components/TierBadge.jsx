import React from 'react';
import { useTranslation } from 'react-i18next';

/**
 * Visual signal for a circle_tier (0..4). Always color + symbol + label
 * (per DESIGN.md — color is never alone). Uses the `tier-badge-{n}` utility
 * classes from `src/frontend/src/index.css`.
 *
 * symbols: 0=█ (filled), 1=●, 2=○, 3=◐ (half), 4=◯ (open)
 * i18n keys: circles.tier.0 .. circles.tier.4
 */
const TIER_SYMBOLS = {
  0: '\u2588',  // █ full block (SELF, most private)
  1: '\u25CF',  // ● filled circle
  2: '\u25CB',  // ○ hollow circle (HOUSEHOLD)
  3: '\u25D0',  // ◐ half-filled
  4: '\u25EF',  // ◯ large circle (PUBLIC)
};

const TIER_CLASS = {
  0: 'tier-badge-0',
  1: 'tier-badge-1',
  2: 'tier-badge-2',
  3: 'tier-badge-3',
  4: 'tier-badge-4',
};

export default function TierBadge({ tier, labelKey = null, className = '' }) {
  const { t } = useTranslation();
  const safeTier = Math.max(0, Math.min(4, Number(tier) || 0));
  const symbol = TIER_SYMBOLS[safeTier];
  const label = labelKey || t(`circles.tier.${safeTier}`);
  const tierClass = TIER_CLASS[safeTier] || '';

  return (
    <span
      className={`tier-badge ${tierClass} ${className}`}
      aria-label={t('circles.tierAriaLabel', { tier: safeTier, label })}
    >
      <span aria-hidden="true" className="font-bold">{symbol}</span>
      <span>{label}</span>
    </span>
  );
}

export { TIER_SYMBOLS, TIER_CLASS };
