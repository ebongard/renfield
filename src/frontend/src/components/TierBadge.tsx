import { useTranslation } from 'react-i18next';

/**
 * Visual signal for a circle_tier (0..4). Always color + symbol + label
 * (per DESIGN.md — color is never alone). Uses the `tier-badge-{n}` utility
 * classes from `src/frontend/src/index.css`.
 *
 * symbols: 0=█ (filled), 1=●, 2=○, 3=◐ (half), 4=◯ (open)
 * i18n keys: circles.tier.0 .. circles.tier.4
 */
export type CircleTier = 0 | 1 | 2 | 3 | 4;

const TIER_SYMBOLS: Record<CircleTier, string> = {
  0: '█',  // █ full block (SELF, most private)
  1: '●',  // ● filled circle
  2: '○',  // ○ hollow circle (HOUSEHOLD)
  3: '◐',  // ◐ half-filled
  4: '◯',  // ◯ large circle (PUBLIC)
};

const TIER_CLASS: Record<CircleTier, string> = {
  0: 'tier-badge-0',
  1: 'tier-badge-1',
  2: 'tier-badge-2',
  3: 'tier-badge-3',
  4: 'tier-badge-4',
};

interface TierBadgeProps {
  tier: number | CircleTier;
  labelKey?: string | null;
  className?: string;
}

export default function TierBadge({ tier, labelKey = null, className = '' }: TierBadgeProps) {
  const { t } = useTranslation();
  const safeTier = Math.max(0, Math.min(4, Number(tier) || 0)) as CircleTier;
  const symbol = TIER_SYMBOLS[safeTier];
  const label = labelKey ?? t(`circles.tier.${safeTier}`);
  const tierClass = TIER_CLASS[safeTier] ?? '';

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
