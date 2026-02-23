import React from 'react';

const COLOR_MAP = {
  blue:   'bg-blue-100 text-blue-600 dark:bg-blue-600/20 dark:text-blue-400',
  green:  'bg-green-100 text-green-600 dark:bg-green-600/20 dark:text-green-400',
  red:    'bg-red-100 text-red-600 dark:bg-red-600/20 dark:text-red-400',
  yellow: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-600/20 dark:text-yellow-400',
  purple: 'bg-purple-100 text-purple-600 dark:bg-purple-600/20 dark:text-purple-400',
  amber:  'bg-amber-100 text-amber-700 dark:bg-amber-600/20 dark:text-amber-400',
  pink:   'bg-pink-100 text-pink-600 dark:bg-pink-600/20 dark:text-pink-400',
  teal:   'bg-teal-100 text-teal-600 dark:bg-teal-600/20 dark:text-teal-400',
  gray:   'bg-gray-200 text-gray-600 dark:bg-gray-600/20 dark:text-gray-400',
  accent: 'bg-accent-100 text-accent-700 dark:bg-accent-600/20 dark:text-accent-400',
};

export default function Badge({ color = 'gray', icon: Icon, children, className = '' }) {
  const colors = COLOR_MAP[color] || COLOR_MAP.gray;

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-1 text-xs rounded-sm font-medium ${colors} ${className}`}>
      {Icon && <Icon className="w-3 h-3" aria-hidden="true" />}
      {children}
    </span>
  );
}
