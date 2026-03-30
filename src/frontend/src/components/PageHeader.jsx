import React from 'react';

export default function PageHeader({ icon: Icon, title, subtitle, children }) {
  return (
    <div className="card">
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="flex items-center gap-4 flex-1 min-w-0">
          <div className="p-3 bg-primary-100 dark:bg-primary-900/30 rounded-xl shrink-0">
            <Icon className="w-6 h-6 text-primary-600 dark:text-primary-400" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <h1 className="text-2xl font-bold font-display text-gray-900 dark:text-white">{title}</h1>
            {subtitle && <p className="text-gray-500 dark:text-gray-400">{subtitle}</p>}
          </div>
        </div>
        {children && <div className="flex items-center space-x-2 self-end sm:self-auto shrink-0">{children}</div>}
      </div>
    </div>
  );
}
