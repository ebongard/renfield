import type { ReactNode } from 'react';

interface AdminListPageShellProps {
  title: string;
  description?: string;
  toolbar?: ReactNode;
  isLoading?: boolean;
  errorMessage?: string | null;
  emptyState?: ReactNode;
  itemCount?: number;
  children: ReactNode;
  testId?: string;
}

/**
 * Shared layout for the four self-learning admin pages (Skills Inbox,
 * Tool-Health, Trajectories, Curator). Pulls the page header, toolbar,
 * loading/error/empty branches, and list body into one DRY shell so the
 * four pages stay visually consistent and a future polish pass touches
 * one file instead of four.
 */
export default function AdminListPageShell({
  title,
  description,
  toolbar,
  isLoading = false,
  errorMessage = null,
  emptyState,
  itemCount,
  children,
  testId,
}: AdminListPageShellProps) {
  const isEmpty = !isLoading && !errorMessage && itemCount === 0;

  return (
    <section
      className="max-w-6xl mx-auto px-4 py-6 space-y-6"
      data-testid={testId ?? 'admin-list-page-shell'}
    >
      <header className="space-y-2">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">{title}</h1>
        {description && (
          <p className="text-sm text-gray-600 dark:text-gray-400">{description}</p>
        )}
      </header>

      {toolbar && (
        <div className="flex flex-wrap items-center gap-2" data-testid="admin-toolbar">
          {toolbar}
        </div>
      )}

      {errorMessage && (
        <div
          className="rounded-lg border border-rose-300 dark:border-rose-700 bg-rose-50 dark:bg-rose-900/20 p-4 text-sm text-rose-800 dark:text-rose-200"
          role="alert"
          data-testid="admin-error"
        >
          {errorMessage}
        </div>
      )}

      {isLoading && (
        <div
          className="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center text-sm text-gray-500 dark:text-gray-400"
          data-testid="admin-loading"
        >
          ...
        </div>
      )}

      {isEmpty && emptyState && (
        <div
          className="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-8 text-center text-sm text-gray-500 dark:text-gray-400"
          data-testid="admin-empty"
        >
          {emptyState}
        </div>
      )}

      {!isLoading && !errorMessage && !isEmpty && children}
    </section>
  );
}
