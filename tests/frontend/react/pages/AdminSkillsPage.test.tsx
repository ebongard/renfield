/**
 * AdminSkillsPage — MSW-driven integration test for the Skills Inbox.
 * Confirms the list renders, the status filter switches the query, and
 * the approve button hits POST /api/skills/{id}/approve.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '../test-utils';
import { server } from '../mocks/server';
import AdminSkillsPage from '../../../../src/frontend/src/pages/AdminSkillsPage';
import { TEST_CONFIG } from '../config';

const BASE = TEST_CONFIG.API_BASE_URL;

interface MockSkill {
  id: number;
  title: string;
  body_md: string;
  trigger_examples: string[];
  tool_sequence: string[];
  source: string;
  status: string;
  version: number;
  success_count: number;
  failure_count: number;
  last_used_at: string | null;
  pinned: boolean;
  circle_tier: number;
  atom_id: string | null;
  merged_into_id: number | null;
  user_id: number | null;
  created_at: string;
  updated_at: string;
  is_owner: boolean;
}

function makeSkill(id: number, status = 'draft'): MockSkill {
  return {
    id,
    title: `Skill #${id}`,
    body_md: '- body',
    trigger_examples: [`trigger ${id}`],
    tool_sequence: [`mcp.x.${id}`],
    source: 'auto_extracted',
    status,
    version: 1,
    success_count: 0,
    failure_count: 0,
    last_used_at: null,
    pinned: false,
    circle_tier: 0,
    atom_id: null,
    merged_into_id: null,
    user_id: 9,
    created_at: '2026-05-26T00:00:00Z',
    updated_at: '2026-05-26T00:00:00Z',
    is_owner: false,
  };
}

beforeEach(() => {
  server.use(
    http.get(`${BASE}/api/skills`, ({ request }) => {
      const url = new URL(request.url);
      const status = url.searchParams.get('status') ?? 'draft';
      if (status === 'draft') {
        return HttpResponse.json([makeSkill(1), makeSkill(2)]);
      }
      return HttpResponse.json([makeSkill(99, status)]);
    }),
  );
});

afterEach(() => {
  server.resetHandlers();
});

describe('AdminSkillsPage', () => {
  it('renders draft skills returned by the API', async () => {
    renderWithProviders(<AdminSkillsPage />);
    await waitFor(() => {
      expect(screen.getByTestId('skill-card-1')).toBeInTheDocument();
      expect(screen.getByTestId('skill-card-2')).toBeInTheDocument();
    });
  });

  it('switches filter via status pill', async () => {
    renderWithProviders(<AdminSkillsPage />);
    await waitFor(() => {
      expect(screen.getByTestId('skill-card-1')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('status-filter-approved'));
    await waitFor(() => {
      expect(screen.getByTestId('skill-card-99')).toBeInTheDocument();
    });
  });

  it('approve button triggers POST /api/skills/:id/approve', async () => {
    const approveHits: number[] = [];
    server.use(
      http.post(`${BASE}/api/skills/:id/approve`, ({ params }) => {
        const id = Number(params.id);
        approveHits.push(id);
        return HttpResponse.json({ ...makeSkill(id, 'approved') });
      }),
    );
    renderWithProviders(<AdminSkillsPage />);
    await waitFor(() => screen.getByTestId('skill-card-1'));
    const approveBtn = screen.getAllByTestId('approve-button')[0];
    fireEvent.click(approveBtn);
    await waitFor(() => {
      expect(approveHits).toContain(1);
    });
  });
});
