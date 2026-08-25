import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import ProjectDetailPage from '../../../../src/frontend/src/pages/ProjectDetailPage';
import { renderWithProviders } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';

// The page reads the route param; pin it to project 5 (renderWithProviders uses
// BrowserRouter, which doesn't supply a matched :id to a directly-rendered page).
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ id: '5' }) };
});

const PROJECT = {
  id: 5, name: 'Apollo', description: 'Rocket project', owner_id: 1,
  knowledge_base_id: 9, circle_tier: 2, status: 'active',
  created_at: '2026-07-10T09:00:00Z', document_count: 1,
};

describe('ProjectDetailPage', () => {
  it('renders the project header + a merged, newest-first timeline with deep-links', async () => {
    server.use(
      http.get(`${BASE_URL}/api/projects/5`, () => HttpResponse.json(PROJECT)),
      http.get(`${BASE_URL}/api/projects/5/timeline`, () =>
        HttpResponse.json([
          { kind: 'chat', id: 'chat-1', ts: '2026-07-13T08:00:00Z', title: 'planning chat',
            subtitle: null, document_id: null, meeting_id: null, conversation_session_id: 'sess-1' },
          { kind: 'decision', id: 'decision-3-0', ts: '2026-07-12T11:00:00Z', title: 'Ship it',
            subtitle: 'Anna', document_id: 20, meeting_id: 3, conversation_session_id: null },
          { kind: 'meeting', id: 'meeting-3', ts: '2026-07-12T10:00:00Z', title: 'Kickoff',
            subtitle: null, document_id: 20, meeting_id: 3, conversation_session_id: null },
          { kind: 'document', id: 'document-20', ts: '2026-07-10T09:00:00Z', title: 'Spec',
            subtitle: null, document_id: 20, meeting_id: null, conversation_session_id: null },
        ]),
      ),
    );

    renderWithProviders(<ProjectDetailPage />);

    // Header shows the project name.
    await waitFor(() => expect(screen.getByText('Apollo')).toBeInTheDocument());

    // All four event kinds render with their titles.
    expect(screen.getByText('planning chat')).toBeInTheDocument();
    expect(screen.getByText('Ship it')).toBeInTheDocument();
    expect(screen.getByText('Kickoff')).toBeInTheDocument();
    expect(screen.getByText('Spec')).toBeInTheDocument();

    // The decision carries its attribution.
    expect(screen.getByText(/Anna/)).toBeInTheDocument();

    // A document event deep-links into the knowledge base.
    const specLink = screen.getByText('Spec').closest('a');
    expect(specLink).toHaveAttribute('href', '/knowledge?doc=20');

    // A meeting event deep-links into the deliverable-first MEETING DETAIL page
    // (not the raw transcript doc), even though it also carries a document_id.
    expect(screen.getByText('Kickoff').closest('a')).toHaveAttribute('href', '/meetings/3');
    // A decision (flattened from a meeting's minutes) deep-links to its meeting.
    expect(screen.getByText('Ship it').closest('a')).toHaveAttribute('href', '/meetings/3');
  });

  it('shows the empty state when the timeline has no events', async () => {
    server.use(
      http.get(`${BASE_URL}/api/projects/5`, () => HttpResponse.json(PROJECT)),
      http.get(`${BASE_URL}/api/projects/5/timeline`, () => HttpResponse.json([])),
    );

    renderWithProviders(<ProjectDetailPage />);
    await waitFor(() =>
      expect(screen.getByText(i18n.t('projects.timeline.empty'))).toBeInTheDocument(),
    );
  });

  it('shows an error state when the project 404s', async () => {
    // A 404 surfaces through the query wrapper as the localized load-error; the
    // page renders the error card rather than the timeline.
    server.use(
      http.get(`${BASE_URL}/api/projects/5`, () => HttpResponse.json({}, { status: 404 })),
      http.get(`${BASE_URL}/api/projects/5/timeline`, () => HttpResponse.json({}, { status: 404 })),
    );

    renderWithProviders(<ProjectDetailPage />);
    await waitFor(
      () => expect(screen.getByText(i18n.t('projects.failedToLoad'))).toBeInTheDocument(),
      { timeout: 3000 },
    );
    // The timeline heading is NOT rendered in the error state.
    expect(screen.queryByText(i18n.t('projects.timeline.title'))).not.toBeInTheDocument();
  });
});
