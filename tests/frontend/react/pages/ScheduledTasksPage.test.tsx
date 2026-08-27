import { createElement, Fragment } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import ScheduledTasksPage from '../../../../src/frontend/src/pages/ScheduledTasksPage';
import { renderWithProviders } from '../test-utils';
import type { ModalProps } from '../../../../src/frontend/src/components/Modal';
import type { UseConfirmDialogResult } from '../../../../src/frontend/src/components/ConfirmDialog';
import type {
  ScheduledTask,
  ScheduledTaskRun,
  ScheduledTasksResponse,
} from '../../../../src/frontend/src/api/resources/scheduledTasks';

// Auto-confirm any destructive confirm() dialog.
vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => {
  const result: UseConfirmDialogResult = {
    confirm: () => Promise.resolve(true),
    ConfirmDialogComponent: createElement(Fragment),
  };
  return { useConfirmDialog: (): UseConfirmDialogResult => result };
});

// Render the modal inline (no portal) so its content is queryable.
vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, onClose, title, children }: ModalProps) => {
    if (!isOpen) return null;
    return (
      <div data-testid="modal">
        <h2>{title}</h2>
        <button onClick={onClose}>schließen</button>
        {children}
      </div>
    );
  },
}));

function mkTask(over: Partial<ScheduledTask>): ScheduledTask {
  return {
    id: 1,
    name: 'Task',
    handler_key: 'cleanup',
    schedule_kind: 'interval',
    interval_seconds: 300,
    cron_expr: null,
    params: {},
    enabled: true,
    run_at_boot: false,
    start_at: null,
    end_at: null,
    next_run_at: '2026-08-27T10:00:00',
    last_run_at: '2026-08-27T09:00:00',
    last_status: 'ok',
    last_error: null,
    last_duration_ms: 42,
    is_builtin: false,
    created_at: '2026-08-01T00:00:00',
    updated_at: '2026-08-01T00:00:00',
    ...over,
  };
}

function mkResponse(tasks: ScheduledTask[]): ScheduledTasksResponse {
  return { tasks, available_handlers: ['cleanup', 'digest'], engine_tick_seconds: 30 };
}

function useTasks(tasks: ScheduledTask[]) {
  server.use(
    http.get(`${BASE_URL}/api/scheduled-tasks`, () => HttpResponse.json(mkResponse(tasks))),
  );
}

describe('ScheduledTasksPage', () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('renders the list of tasks with their schedule', async () => {
    useTasks([
      mkTask({ id: 1, name: 'Nightly cleanup', interval_seconds: 300 }),
      mkTask({ id: 2, name: 'Weekly digest', schedule_kind: 'cron', interval_seconds: null, cron_expr: '0 3 * * 1', is_builtin: true }),
    ]);

    renderWithProviders(<ScheduledTasksPage />);

    await waitFor(() => expect(screen.getByText('Nightly cleanup')).toBeInTheDocument());
    expect(screen.getByText('Weekly digest')).toBeInTheDocument();
    // Interval humanized ("alle 5 Min") + the cron expression rendered verbatim.
    expect(screen.getByText('alle 5 Min')).toBeInTheDocument();
    expect(screen.getByText('0 3 * * 1')).toBeInTheDocument();
  });

  it('shows the empty state when there are no tasks', async () => {
    useTasks([]);

    renderWithProviders(<ScheduledTasksPage />);

    await waitFor(() => expect(screen.getByText('Keine geplanten Aufgaben')).toBeInTheDocument());
  });

  it('toggles enabled via PATCH', async () => {
    useTasks([mkTask({ id: 7, name: 'Toggle me', enabled: true })]);
    let patched: { enabled?: boolean } | null = null;
    server.use(
      http.patch(`${BASE_URL}/api/scheduled-tasks/:id`, async ({ request }) => {
        patched = (await request.json()) as { enabled?: boolean };
        return HttpResponse.json(mkTask({ id: 7, name: 'Toggle me', enabled: false }));
      }),
    );

    renderWithProviders(<ScheduledTasksPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Toggle me')).toBeInTheDocument());

    await user.click(screen.getByRole('switch'));

    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched!.enabled).toBe(false);
  });

  it('triggers run-now via POST', async () => {
    useTasks([mkTask({ id: 9, name: 'Run me', enabled: true })]);
    let ran = false;
    server.use(
      http.post(`${BASE_URL}/api/scheduled-tasks/:id/run-now`, () => {
        ran = true;
        return HttpResponse.json(mkTask({ id: 9, name: 'Run me' }));
      }),
    );

    renderWithProviders(<ScheduledTasksPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Run me')).toBeInTheDocument());

    await user.click(screen.getByTitle('Jetzt ausführen'));

    await waitFor(() => expect(ran).toBe(true));
  });

  it('deletes a custom (non-builtin) task', async () => {
    useTasks([mkTask({ id: 4, name: 'Custom task', is_builtin: false })]);
    let deletedId: string | null = null;
    server.use(
      http.delete(`${BASE_URL}/api/scheduled-tasks/:id`, ({ params }) => {
        deletedId = params.id as string;
        return HttpResponse.json({ success: true, deleted: 'Custom task' });
      }),
    );

    renderWithProviders(<ScheduledTasksPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Custom task')).toBeInTheDocument());

    await user.click(screen.getByTitle('Aufgabe löschen'));

    await waitFor(() => expect(deletedId).toBe('4'));
  });

  it('does not offer delete for a built-in task', async () => {
    useTasks([mkTask({ id: 5, name: 'Builtin task', is_builtin: true })]);

    renderWithProviders(<ScheduledTasksPage />);

    await waitFor(() => expect(screen.getByText('Builtin task')).toBeInTheDocument());

    // Built-ins carry the "Eingebaut" badge and expose no delete affordance.
    expect(screen.getByText('Eingebaut')).toBeInTheDocument();
    expect(screen.queryByTitle('Aufgabe löschen')).not.toBeInTheDocument();
    // …but are still editable.
    expect(screen.getByTitle('Aufgabe bearbeiten')).toBeInTheDocument();
  });

  it('lazily loads and renders per-task run history when expanded', async () => {
    useTasks([mkTask({ id: 11, name: 'Dedupe task' })]);
    const runs: ScheduledTaskRun[] = [
      {
        id: 2,
        started_at: '2026-08-27T13:19:00',
        finished_at: '2026-08-27T13:19:41',
        status: 'ok',
        duration_ms: 41000,
        detail: 'deleted=200 remaining=1652',
        error: null,
      },
      {
        id: 1,
        started_at: '2026-08-27T12:56:00',
        finished_at: '2026-08-27T12:56:30',
        status: 'error',
        duration_ms: 30000,
        detail: null,
        error: 'Tool-Aufruf Timeout: dedupe_documents',
      },
    ];
    let runsRequested = false;
    server.use(
      http.get(`${BASE_URL}/api/scheduled-tasks/:id/runs`, ({ params }) => {
        runsRequested = true;
        expect(params.id).toBe('11');
        return HttpResponse.json(runs);
      }),
    );

    renderWithProviders(<ScheduledTasksPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Dedupe task')).toBeInTheDocument());
    // Not fetched until the row is expanded.
    expect(runsRequested).toBe(false);

    await user.click(screen.getByRole('button', { name: 'Verlauf' }));

    // ok run's detail + the error run's message render.
    await waitFor(() =>
      expect(screen.getByText('deleted=200 remaining=1652')).toBeInTheDocument(),
    );
    expect(runsRequested).toBe(true);
    expect(screen.getByText('Tool-Aufruf Timeout: dedupe_documents')).toBeInTheDocument();
    // Duration humanized (41000ms → "41s").
    expect(screen.getByText('41s')).toBeInTheDocument();
    // The error run carries the red "Fehler" status badge.
    expect(screen.getByText('Fehler')).toBeInTheDocument();
  });

  it('surfaces a 409 error when creating a duplicate task', async () => {
    useTasks([mkTask({ id: 1, name: 'Existing' })]);
    server.use(
      http.post(`${BASE_URL}/api/scheduled-tasks`, () =>
        HttpResponse.json({ detail: 'Task name already exists' }, { status: 409 }),
      ),
    );

    renderWithProviders(<ScheduledTasksPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Existing')).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: /aufgabe anlegen/i }));

    const modal = await screen.findByTestId('modal');
    await user.type(within(modal).getByPlaceholderText(/nächtliche bereinigung/i), 'Existing');
    // Submit button inside the modal (also labelled "Aufgabe anlegen").
    const submit = within(modal).getByRole('button', { name: /aufgabe anlegen/i });
    await user.click(submit);

    await waitFor(() =>
      expect(screen.getByText(/task name already exists/i)).toBeInTheDocument(),
    );
  });
});
