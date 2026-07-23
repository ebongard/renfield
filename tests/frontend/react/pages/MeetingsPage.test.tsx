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
    minutes_status: 'none',
    project_id: null,
    language: null,
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

  it('links a completed meeting to its detail page (Track D)', async () => {
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([
          mkMeeting({ id: 1, title: 'Kickoff', status: 'completed' }),
          mkMeeting({ id: 2, title: 'Queued', status: 'processing', transcript_document_id: null }),
        ]),
      ),
    );

    renderWithProviders(<MeetingsPage />);

    // Completed → the card heading is a link to /meetings/1.
    const link = await screen.findByRole('link', {
      name: i18n.t('meetings.openDetailFor', { title: 'Kickoff' }),
    });
    expect(link).toHaveAttribute('href', '/meetings/1');

    // A non-completed meeting is NOT a detail link.
    expect(
      screen.queryByRole('link', { name: i18n.t('meetings.openDetailFor', { title: 'Queued' }) }),
    ).not.toBeInTheDocument();
  });

  it('links the draft-ready badge to the detail page', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json({
          schicht_a_extraction_enabled: false,
          wissen_workspace_enabled: false,
          command_palette_enabled: false,
          role_surfacing_enabled: false,
          message_search_enabled: false,
          artifacts_typed_enabled: false,
          room_handoff_enabled: false,
          chat_branching_enabled: false,
          projects_enabled: false,
          meeting_transcription_enabled: true,
          meeting_minutes_enabled: true,
        }),
      ),
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([
          mkMeeting({ id: 6, title: 'Review', status: 'completed', minutes_status: 'draft' }),
        ]),
      ),
    );

    renderWithProviders(<MeetingsPage />);

    const badge = await screen.findByRole('link', {
      name: i18n.t('meetings.minutes.draftReadyBadge'),
    });
    expect(badge).toHaveAttribute('href', '/meetings/6');
    // No inline expand: the transcript toggle / minutes panel never render here.
    expect(
      screen.queryByRole('button', { name: i18n.t('meetings.transcriptSection') }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(i18n.t('meetings.minutes.title'))).not.toBeInTheDocument();
  });

  it('links a meeting to a project via the card picker when projects exist', async () => {
    const user = userEvent.setup();
    let patchBody: unknown = null;
    server.use(
      // The projects query is gated on projects_enabled — turn it on for this test.
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json({
          schicht_a_extraction_enabled: false,
          wissen_workspace_enabled: false,
          command_palette_enabled: false,
          role_surfacing_enabled: false,
          message_search_enabled: false,
          artifacts_typed_enabled: false,
          room_handoff_enabled: false,
          chat_branching_enabled: false,
          projects_enabled: true,
          meeting_transcription_enabled: true,
          meeting_minutes_enabled: false,
        }),
      ),
      http.get(`${BASE_URL}/api/projects`, () =>
        HttpResponse.json([
          { id: 7, name: 'Alpha', description: null, owner_id: 1, knowledge_base_id: null },
        ]),
      ),
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([mkMeeting({ id: 1, title: 'Kickoff', project_id: null })]),
      ),
      http.patch(`${BASE_URL}/api/meetings/1`, async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json(mkMeeting({ id: 1, title: 'Kickoff', project_id: 7 }));
      }),
    );

    renderWithProviders(<MeetingsPage />);

    // The per-meeting project picker appears (aria-label carries the title),
    // offering the project + the "no project" option.
    const select = (await screen.findByLabelText(
      i18n.t('meetings.projectForMeeting', { title: 'Kickoff' }),
    )) as HTMLSelectElement;
    expect(within(select).getByRole('option', { name: 'Alpha' })).toBeInTheDocument();

    await user.selectOptions(select, '7');
    await waitFor(() => expect(patchBody).toEqual({ project_id: 7 }));
  });

  it('renders a failed meeting with its error and no detail link', async () => {
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
    // A failed meeting is not a detail link.
    expect(
      screen.queryByRole('link', { name: i18n.t('meetings.openDetailFor', { title: 'Broken' }) }),
    ).not.toBeInTheDocument();
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

  it('deletes a meeting after an inline confirm', async () => {
    let deleted: number | null = null;
    const store: Meeting[] = [mkMeeting({ id: 8, title: 'ToDelete', status: 'completed' })];
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () => HttpResponse.json(store)),
      http.delete(`${BASE_URL}/api/meetings/8`, () => {
        deleted = 8;
        store.length = 0;
        return HttpResponse.json({ status: 'deleted', id: 8 });
      }),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('ToDelete')).toBeInTheDocument());
    // First click reveals the confirm; the DELETE only fires on the confirm click.
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.delete') }));
    expect(deleted).toBeNull();
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.confirmDelete') }));

    await waitFor(() => expect(deleted).toBe(8));
    await waitFor(() => expect(screen.getByText(i18n.t('meetings.empty'))).toBeInTheDocument());
  });
});
