/**
 * Own number for calls (#1683) on "Mein Konto".
 *
 * Guards: flag off => no card and no request to /api/preferences/call-number;
 * the number is only shown as confirmed after the code step; the code form
 * appears only while a code call is pending.
 */
import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders } from '../test-utils';
import CallNumberCard from '../../../../src/frontend/src/components/CallNumberCard';

interface Api {
  calls: string[];
  state: { number: string | null; verified_at: string | null; pending: string | null };
}

function mockApi(flag = true): Api {
  const api: Api = { calls: [], state: { number: null, verified_at: null, pending: null } };
  server.use(
    http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json({ telephony_outbound_enabled: flag })),
    http.get(`${BASE_URL}/api/preferences/call-number`, () => {
      api.calls.push('GET');
      return HttpResponse.json(api.state);
    }),
    http.put(`${BASE_URL}/api/preferences/call-number`, async ({ request }) => {
      const body = (await request.json()) as { number: string };
      api.calls.push(`PUT ${body.number}`);
      api.state = { ...api.state, pending: '+491701234567' };
      return HttpResponse.json(api.state);
    }),
    http.post(`${BASE_URL}/api/preferences/call-number/verify`, async ({ request }) => {
      const body = (await request.json()) as { code: string };
      api.calls.push(`VERIFY ${body.code}`);
      if (body.code !== '4712') {
        return HttpResponse.json({ detail: 'Der Code stimmt nicht oder ist abgelaufen.' }, { status: 400 });
      }
      api.state = { number: '+491701234567', verified_at: '2026-11-28T12:00:00', pending: null };
      return HttpResponse.json(api.state);
    }),
  );
  return api;
}

describe('CallNumberCard', () => {
  it('renders nothing and asks nothing while the flag is off', async () => {
    const api = mockApi(false);
    const { container } = renderWithProviders(<CallNumberCard />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(api.calls).toEqual([]);
  });

  it('starts a code call, then confirms the number with the right code', async () => {
    const api = mockApi();
    const user = userEvent.setup();
    renderWithProviders(<CallNumberCard />);

    const input = await screen.findByRole('textbox', { name: /Telefonnummer|Phone number/i });
    expect(screen.queryByRole('textbox', { name: /Code/i })).toBeNull();
    await user.type(input, '0170 1234567');
    await user.click(screen.getByRole('button', { name: /Bestätigungsanruf|Confirmation call/i }));
    await waitFor(() => expect(api.calls).toContain('PUT 0170 1234567'));

    const code = await screen.findByRole('textbox', { name: /Code/i });
    await user.type(code, '0000');
    await user.click(screen.getByRole('button', { name: /^(Bestätigen|Confirm)$/i }));
    expect(await screen.findByText('Der Code stimmt nicht oder ist abgelaufen.')).toBeInTheDocument();
    expect(screen.queryByText(/(Bestätigt|Confirmed):/)).toBeNull();

    await user.clear(code);
    await user.type(code, '4712');
    await user.click(screen.getByRole('button', { name: /^(Bestätigen|Confirm)$/i }));
    expect(await screen.findByText(/(Bestätigt|Confirmed): \+491701234567/)).toBeInTheDocument();
  });
});
