import React from 'react';

export default function PageHeader({ icon: Icon, title, subtitle, children }) {
  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="p-3 bg-primary-100 dark:bg-primary-900/30 rounded-xl">
            <Icon className="w-6 h-6 text-primary-600 dark:text-primary-400" aria-hidden="true" />
          </div>
          <div>
            <h1 className="text-2xl font-bold font-display text-gray-900 dark:text-white">{title}</h1>
            {subtitle && <p className="text-gray-500 dark:text-gray-400">{subtitle}</p>}
          </div>
        </div>
        {children && <div className="flex items-center space-x-2">{children}</div>}
      </div>
    </div>
  );
}
