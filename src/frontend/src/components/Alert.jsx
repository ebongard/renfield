import React from 'react';
import { AlertCircle, CheckCircle, AlertTriangle, Info } from 'lucide-react';

const VARIANTS = {
  error: {
    icon: AlertCircle,
    container: 'bg-red-100 dark:bg-red-900/20 border-red-300 dark:border-red-700',
    icon_color: 'text-red-500',
    text: 'text-red-700 dark:text-red-400',
  },
  success: {
    icon: CheckCircle,
    container: 'bg-green-100 dark:bg-green-900/20 border-green-300 dark:border-green-700',
    icon_color: 'text-green-500',
    text: 'text-green-700 dark:text-green-400',
  },
  warning: {
    icon: AlertTriangle,
    container: 'bg-yellow-100 dark:bg-yellow-900/20 border-yellow-300 dark:border-yellow-700',
    icon_color: 'text-yellow-500',
    text: 'text-yellow-700 dark:text-yellow-400',
  },
  info: {
    icon: Info,
    container: 'bg-blue-100 dark:bg-blue-900/20 border-blue-300 dark:border-blue-700',
    icon_color: 'text-blue-500',
    text: 'text-blue-700 dark:text-blue-400',
  },
};

export default function Alert({ variant = 'info', children, className = '' }) {
  const v = VARIANTS[variant] || VARIANTS.info;
  const Icon = v.icon;

  return (
    <div className={`card ${v.container} ${className}`}>
      <div className="flex items-center space-x-3">
        <Icon className={`w-5 h-5 shrink-0 ${v.icon_color}`} aria-hidden="true" />
        <p className={v.text}>{children}</p>
      </div>
    </div>
  );
}
