import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { AlertCircle, AlertTriangle, CheckCircle, Info, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export type AlertVariant = 'error' | 'success' | 'warning' | 'info';

interface VariantStyle {
  icon: LucideIcon;
  container: string;
  iconColor: string;
  text: string;
}

const VARIANTS: Record<AlertVariant, VariantStyle> = {
  error: {
    icon: AlertCircle,
    container: 'bg-red-100 dark:bg-red-900/20 border-red-300 dark:border-red-700',
    iconColor: 'text-red-500',
    text: 'text-red-700 dark:text-red-400',
  },
  success: {
    icon: CheckCircle,
    container: 'bg-green-100 dark:bg-green-900/20 border-green-300 dark:border-green-700',
    iconColor: 'text-green-500',
    text: 'text-green-700 dark:text-green-400',
  },
  warning: {
    icon: AlertTriangle,
    container: 'bg-yellow-100 dark:bg-yellow-900/20 border-yellow-300 dark:border-yellow-700',
    iconColor: 'text-yellow-500',
    text: 'text-yellow-700 dark:text-yellow-400',
  },
  info: {
    icon: Info,
    container: 'bg-blue-100 dark:bg-blue-900/20 border-blue-300 dark:border-blue-700',
    iconColor: 'text-blue-500',
    text: 'text-blue-700 dark:text-blue-400',
  },
};

interface AlertProps {
  variant?: AlertVariant;
  children?: ReactNode;
  className?: string;
  /** When provided, renders an X button that calls this handler. */
  onClose?: () => void;
}

export default function Alert({ variant = 'info', children, className = '', onClose }: AlertProps) {
  const { t } = useTranslation();
  const v = VARIANTS[variant] ?? VARIANTS.info;
  const Icon = v.icon;

  return (
    <div className={`card ${v.container} ${className}`}>
      <div className="flex items-center space-x-3">
        <Icon className={`w-5 h-5 shrink-0 ${v.iconColor}`} aria-hidden="true" />
        <p className={`grow ${v.text}`}>{children}</p>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className={`shrink-0 p-1 rounded-md hover:bg-black/5 dark:hover:bg-white/10 transition-colors ${v.iconColor}`}
            aria-label={t('common.dismiss')}
          >
            <X className="w-4 h-4" aria-hidden="true" />
          </button>
        )}
      </div>
    </div>
  );
}
