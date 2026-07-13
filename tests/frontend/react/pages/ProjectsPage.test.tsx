import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import ProjectsPage from '../../../../src/frontend/src/pages/ProjectsPage';
import { renderWithProviders } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import type { Project } from '../../../../src/frontend/src/api/resources/projects';

function mkProject(over: Partial<Project>): Project {
  return {
    id: 1,
    name: 'Project',
    description: null,
    owner_id: null,
    knowledge_base_id: 10,
    circle_tier: 2,
    status: 'active',
    created_at: '2026-07-13T10:00:00Z',
    document_count: 0,
    ...over,
  };
}

describe('ProjectsPage', () => {
  it('renders the list of projects', async () => {
    server.use(
      http.get(`${BASE_URL}/api/projects`, () =>
        HttpResponse.json([
          mkProject({ id: 1, name: 'Alpha', document_count: 3 }),
          mkProject({ id: 2, name: 'Beta' }),
        ]),
      ),
    );

    renderWithProviders(<ProjectsPage />);

    await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument());
    expect(screen.getByText('Beta')).toBeInTheDocument();
  });

  it('shows the empty state when there are no projects', async () => {
    server.use(http.get(`${BASE_URL}/api/projects`, () => HttpResponse.json([])));

    renderWithProviders(<ProjectsPage />);

    await waitFor(() =>
      expect(screen.getByText(i18n.t('projects.noProjects'))).toBeInTheDocument(),
    );
  });

  it('creates a project and shows it in the refreshed list', async () => {
    const store: Project[] = [];
    server.use(
      http.get(`${BASE_URL}/api/projects`, () => HttpResponse.json(store)),
      http.post(`${BASE_URL}/api/projects`, async ({ request }) => {
        const body = (await request.json()) as { name: string; description: string | null };
        const created = mkProject({ id: 99, name: body.name, description: body.description });
        store.push(created);
        return HttpResponse.json(created);
      }),
    );

    renderWithProviders(<ProjectsPage />);
    const user = userEvent.setup();

    // The create form's name field is the first textbox; the submit is the only button.
    const [nameInput] = screen.getAllByRole('textbox');
    await user.type(nameInput, 'Gamma');
    await user.click(screen.getByRole('button', { name: i18n.t('projects.create') }));

    await waitFor(() => expect(screen.getByText('Gamma')).toBeInTheDocument());
  });
});
