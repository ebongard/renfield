/**
 * RoutingDashboardPage (#1524) — the page called two routes the backend never
 * had, swallowed the 404 and showed "no entries". Now: real rows, a notice when
 * recording is off, and an inline error when the request fails.
 */
import { describe, it, expect } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen } from '@testing-library/react';

import { renderWithProviders } from '../test-utils';
import { server } from '../mocks/server';
import RoutingDashboardPage from '../../../../src/frontend/src/pages/RoutingDashboardPage';
import { TEST_CONFIG } from '../config';

const BASE = TEST_CONFIG.API_BASE_URL;

function serve(enabled: boolean, traces: unknown[] = []) {
  server.use(
    http.get(`${BASE}/api/admin/routing-traces`, () =>
      HttpResponse.json({ traces, enabled, retention_days: 14 }),
    ),
    http.get(`${BASE}/api/admin/routing-stats`, () =>
      HttpResponse.json({
        total: traces.length,
        by_domain: traces.length ? { smart_home: traces.length } : {},
        by_layer: traces.length ? { semantic: traces.length } : {},
        enabled,
        retention_days: 14,
      }),
    ),
  );
}

describe('RoutingDashboardPage', () => {
  it('renders a decision with its layer, score and sub-intent', async () => {
    serve(true, [
      {
        id: 1,
        created_at: '2026-11-10T10:00:00',
        user_id: 2,
        message: 'Mach das Licht im Wohnzimmer an',
        domain: 'smart_home',
        sub_intent: 'lights',
        layer: 'semantic',
        confidence: 0.912,
        entity_matches: [{ id: 'light.wohnzimmer' }],
      },
    ]);
    renderWithProviders(<RoutingDashboardPage />);

    expect(await screen.findByText('Mach das Licht im Wohnzimmer an')).toBeInTheDocument();
    expect(screen.getByText('smart_home/lights')).toBeInTheDocument();
    expect(screen.getByText('0.91')).toBeInTheDocument();
    expect(screen.getByText('light.wohnzimmer')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('says that recording is off instead of looking empty', async () => {
    serve(false);
    renderWithProviders(<RoutingDashboardPage />);

    expect(await screen.findByRole('status')).toHaveTextContent('ROUTING_TRACES_ENABLED');
  });

  it('shows an inline error when the request fails', async () => {
    server.use(
      http.get(`${BASE}/api/admin/routing-traces`, () => new HttpResponse(null, { status: 404 })),
      http.get(`${BASE}/api/admin/routing-stats`, () => new HttpResponse(null, { status: 404 })),
    );
    renderWithProviders(<RoutingDashboardPage />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
