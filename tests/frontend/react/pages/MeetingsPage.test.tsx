import { describe, it, expect } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import MeetingsPage from '../../../../src/frontend/src/pages/MeetingsPage';
import { renderWithProviders } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import type { Meeting } from '../../../../src/frontend/src/api/resources/meetings';

function mkMeeting(over: Partial<Meeting>): Meeting {
  return {
    id: 1,
    status: 'completed',
    title: 'Standup',
    date: '2026-07-14',
    error: null,
    transcript_document_id: 42,
    created_at: '2026-07-14T09:00:00Z',
    ...over,
  };
}

describe('MeetingsPage', () => {
  it('renders meetings with their status', async () => {
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([
          mkMeeting({ id: 1, title: 'Kickoff', status: 'completed' }),
          mkMeeting({ id: 2, title: 'Retro', status: 'processing', transcript_document_id: null }),
        ]),
      ),
    );

    renderWithProviders(<MeetingsPage />);

    await waitFor(() => expect(screen.getByText('Kickoff')).toBeInTheDocument());
    expect(screen.getByText('Retro')).toBeInTheDocument();
    expect(screen.getByText(i18n.t('meetings.status.completed'))).toBeInTheDocument();
    expect(screen.getByText(i18n.t('meetings.status.processing'))).toBeInTheDocument();
  });

  it('renders a failed meeting with its error and no expand affordance', async () => {
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([
          mkMeeting({ id: 3, title: 'Broken', status: 'failed', error: 'audio exceeds size limit', transcript_document_id: null }),
        ]),
      ),
    );

    renderWithProviders(<MeetingsPage />);

    await waitFor(() => expect(screen.getByText('Broken')).toBeInTheDocument());
    expect(screen.getByText('audio exceeds size limit')).toBeInTheDocument();
    expect(screen.getByText(i18n.t('meetings.status.failed'))).toBeInTheDocument();
    // A failed meeting is not expandable → its title button is disabled.
    expect(screen.getByRole('button', { name: /Broken/ })).toBeDisabled();
  });

  it('shows the error state when the list fails to load', async () => {
    // A 500 with no extractable detail → the query wrapper falls back to the
    // localized meetings.failedToLoad message.
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () => HttpResponse.json({}, { status: 500 })),
    );

    renderWithProviders(<MeetingsPage />);

    await waitFor(
      () => expect(screen.getByText(i18n.t('meetings.failedToLoad'))).toBeInTheDocument(),
      { timeout: 3000 },
    );
    // The empty-state must NOT show when the list errored.
    expect(screen.queryByText(i18n.t('meetings.empty'))).not.toBeInTheDocument();
  });

  it('shows the empty state when there are no meetings', async () => {
    server.use(http.get(`${BASE_URL}/api/meetings`, () => HttpResponse.json([])));

    renderWithProviders(<MeetingsPage />);

    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.empty'))).toBeInTheDocument(),
    );
  });

  it('keeps the upload button disabled until a file is chosen and consent is given', async () => {
    server.use(http.get(`${BASE_URL}/api/meetings`, () => HttpResponse.json([])));
    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    const uploadBtn = await screen.findByRole('button', { name: i18n.t('meetings.upload') });
    expect(uploadBtn).toBeDisabled();

    const file = new File(['audio'], 'rec.wav', { type: 'audio/wav' });
    await user.upload(screen.getByLabelText(i18n.t('meetings.audioLabel')), file);
    // File chosen but consent not yet given → still disabled.
    expect(uploadBtn).toBeDisabled();

    await user.click(screen.getByRole('checkbox'));
    expect(uploadBtn).toBeEnabled();
  });

  it('uploads a recording with consent and refreshes the list', async () => {
    const store: Meeting[] = [];
    let sentConsent: string | null = null;
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () => HttpResponse.json(store)),
      http.post(`${BASE_URL}/api/meetings/transcribe`, async ({ request }) => {
        const form = await request.formData();
        sentConsent = String(form.get('consent_confirmed'));
        const created = mkMeeting({ id: 7, title: 'New', status: 'pending', transcript_document_id: null });
        store.push(created);
        return HttpResponse.json(created);
      }),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    const file = new File(['audio'], 'rec.wav', { type: 'audio/wav' });
    await user.upload(await screen.findByLabelText(i18n.t('meetings.audioLabel')), file);
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.upload') }));

    await waitFor(() => expect(screen.getByText('New')).toBeInTheDocument());
    expect(sentConsent).toBe('true');
  });

  it('expands a completed meeting to show its transcript and relabels a speaker', async () => {
    let relabeled: { speaker_key: string; label: string } | null = null;
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([mkMeeting({ id: 5, title: 'Planning', status: 'completed' })]),
      ),
      http.get(`${BASE_URL}/api/meetings/5/segments`, () =>
        HttpResponse.json({
          id: 5,
          status: 'completed',
          segments: [
            { speaker: 'Sprecher 1', speaker_key: 'S1', start_s: 0, end_s: 2, text: 'Guten Morgen.' },
            { speaker: 'Sprecher 2', speaker_key: 'S2', start_s: 2, end_s: 4, text: 'Hallo.' },
          ],
        }),
      ),
      http.post(`${BASE_URL}/api/meetings/5/relabel`, async ({ request }) => {
        relabeled = (await request.json()) as { speaker_key: string; label: string };
        return HttpResponse.json(mkMeeting({ id: 5 }));
      }),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('Planning'));

    // Transcript turns render.
    await waitFor(() => expect(screen.getByText('Guten Morgen.')).toBeInTheDocument());
    expect(screen.getByText('Hallo.')).toBeInTheDocument();

    // Relabel the first speaker cluster.
    const firstRow = screen.getByLabelText(i18n.t('meetings.relabelAria', { speaker: 'Sprecher 1' }));
    await user.type(firstRow, 'Anna');
    const saveBtns = screen.getAllByRole('button', { name: i18n.t('meetings.relabelSave') });
    await user.click(saveBtns[0]);

    await waitFor(() => expect(relabeled).toEqual({ speaker_key: 'S1', label: 'Anna' }));
  });
});
