import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { Routes, Route } from 'react-router';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import MeetingDetailPage from '../../../../src/frontend/src/pages/MeetingDetailPage';
import { renderWithProviders } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import type { Meeting } from '../../../../src/frontend/src/api/resources/meetings';

function mkMeeting(over: Partial<Meeting>): Meeting {
  return {
    id: 6,
    status: 'completed',
    title: 'Review',
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

/** Full feature-flag payload with overrides — mirrors the default handler shape. */
function features(over: Record<string, boolean> = {}) {
  return {
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
    meeting_minutes_enabled: false,
    ...over,
  };
}

/** Mount the detail page at /meetings/:id with a sentinel /meetings list route
 *  so navigation-away (e.g. after delete) is observable. */
function renderDetail(route = '/meetings/6') {
  return renderWithProviders(
    <Routes>
      <Route path="/meetings/:id" element={<MeetingDetailPage />} />
      <Route path="/meetings" element={<div>LIST-PAGE</div>} />
    </Routes>,
    { route },
  );
}

describe('MeetingDetailPage (§2 Track D)', () => {
  it('renders the meeting header with title and status', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json(features())),
      http.get(`${BASE_URL}/api/meetings/6`, () => HttpResponse.json(mkMeeting({ title: 'Kickoff' }))),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
    );

    renderDetail();

    await waitFor(() => expect(screen.getByText('Kickoff')).toBeInTheDocument());
    expect(screen.getByText(i18n.t('meetings.status.completed'))).toBeInTheDocument();
    // Open-transcript-doc deep link is present for a completed meeting.
    expect(screen.getByText(i18n.t('meetings.openTranscript'))).toBeInTheDocument();
  });

  it('deep-links to the linked project when one is set', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json(features({ projects_enabled: true })),
      ),
      http.get(`${BASE_URL}/api/projects`, () =>
        HttpResponse.json([
          { id: 9, name: 'Apollo', description: null, owner_id: 1, knowledge_base_id: null },
        ]),
      ),
      http.get(`${BASE_URL}/api/meetings/6`, () => HttpResponse.json(mkMeeting({ project_id: 9 }))),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
    );

    renderDetail();

    const link = await screen.findByRole('link', {
      name: new RegExp(i18n.t('meetings.openProject')),
    });
    expect(link).toHaveAttribute('href', '/projects/9');
  });

  it('shows NO open-project link when the meeting is not linked to a project', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json(features({ projects_enabled: true })),
      ),
      http.get(`${BASE_URL}/api/projects`, () =>
        HttpResponse.json([
          { id: 9, name: 'Apollo', description: null, owner_id: 1, knowledge_base_id: null },
        ]),
      ),
      http.get(`${BASE_URL}/api/meetings/6`, () => HttpResponse.json(mkMeeting({ project_id: null }))),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
    );

    renderDetail();

    // Wait for the page to render, then assert the navigate link is absent
    // (the ProjectSelect to link one is still shown, but nothing to open yet).
    await screen.findByText('Review');
    expect(
      screen.queryByRole('link', { name: new RegExp(i18n.t('meetings.openProject')) }),
    ).toBeNull();
  });

  it('shows an invalid id as not-found', async () => {
    renderDetail('/meetings/not-a-number');
    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.notFound'))).toBeInTheDocument(),
    );
  });

  it('puts minutes first and keeps the transcript behind a toggle', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json(features({ meeting_minutes_enabled: true })),
      ),
      http.get(`${BASE_URL}/api/meetings/6`, () =>
        HttpResponse.json(mkMeeting({ minutes_status: 'confirmed' })),
      ),
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () =>
        HttpResponse.json({
          id: 6,
          minutes_status: 'confirmed',
          minutes: { summary: 'Zusammenfassung.', decisions: [], action_items: [] },
        }),
      ),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({
          id: 6,
          status: 'completed',
          segments: [{ speaker: 'Sprecher 1', speaker_key: 'S1', start_s: 0, end_s: 2, text: 'Los gehts.' }],
        }),
      ),
    );

    renderDetail();
    const user = userEvent.setup();

    // Wait for the actual minutes CONTENT — the panel header also renders during
    // the minutes-loading spinner, so waiting on the title races the query.
    await screen.findByText('Zusammenfassung.');
    // Transcript content is hidden until the toggle is opened (minutes-first).
    expect(screen.queryByText('Los gehts.')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: i18n.t('meetings.transcriptSection') }));
    await waitFor(() => expect(screen.getByText('Los gehts.')).toBeInTheDocument());
  });

  it('surfaces the draft-confirm nudge when minutes are an unconfirmed draft', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json(features({ meeting_minutes_enabled: true })),
      ),
      http.get(`${BASE_URL}/api/meetings/6`, () =>
        HttpResponse.json(mkMeeting({ minutes_status: 'draft' })),
      ),
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () =>
        HttpResponse.json({
          id: 6,
          minutes_status: 'draft',
          minutes: { summary: 'Entwurf.', decisions: [], action_items: [] },
        }),
      ),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
    );

    renderDetail();

    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.draftNudgeTitle'))).toBeInTheDocument(),
    );
  });

  it('shows a still-processing state for a meeting that is not yet completed', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json(features())),
      http.get(`${BASE_URL}/api/meetings/6`, () =>
        HttpResponse.json(mkMeeting({ status: 'processing', transcript_document_id: null })),
      ),
    );

    renderDetail();

    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.stillProcessing'))).toBeInTheDocument(),
    );
    // No minutes/transcript deliverable yet, and no transcript request is needed.
    expect(screen.queryByText(i18n.t('meetings.minutes.title'))).not.toBeInTheDocument();
  });

  it('deletes the meeting and navigates back to the list', async () => {
    let deleted: number | null = null;
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json(features())),
      http.get(`${BASE_URL}/api/meetings/6`, () => HttpResponse.json(mkMeeting({}))),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
      http.delete(`${BASE_URL}/api/meetings/6`, () => {
        deleted = 6;
        return HttpResponse.json({ status: 'deleted', id: 6 });
      }),
    );

    renderDetail();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Review')).toBeInTheDocument());
    // First click reveals the confirm; the DELETE only fires on confirm.
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.delete') }));
    expect(deleted).toBeNull();
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.confirmDelete') }));

    await waitFor(() => expect(deleted).toBe(6));
    // Navigated to /meetings — the sentinel list route renders.
    await waitFor(() => expect(screen.getByText('LIST-PAGE')).toBeInTheDocument());
  });

  it('generates, edits, and confirms minutes from the detail page', async () => {
    let saved: { summary: string } | null = null;
    let confirmed = false;
    let state = { id: 6, minutes_status: 'none' as string, minutes: null as unknown };

    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json(features({ meeting_minutes_enabled: true })),
      ),
      http.get(`${BASE_URL}/api/meetings/6`, () =>
        HttpResponse.json(mkMeeting({ minutes_status: state.minutes_status as Meeting['minutes_status'] })),
      ),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () => HttpResponse.json(state)),
      http.post(`${BASE_URL}/api/meetings/6/minutes/generate`, () => {
        state = {
          id: 6,
          minutes_status: 'draft',
          minutes: { summary: 'Auto-Entwurf.', decisions: [], action_items: [] },
        };
        return HttpResponse.json(state);
      }),
      http.put(`${BASE_URL}/api/meetings/6/minutes`, async ({ request }) => {
        saved = (await request.json()) as typeof saved;
        state = { id: 6, minutes_status: 'draft', minutes: saved };
        return HttpResponse.json(state);
      }),
      http.post(`${BASE_URL}/api/meetings/6/minutes/confirm`, () => {
        confirmed = true;
        state = { ...state, minutes_status: 'confirmed' };
        return HttpResponse.json(state);
      }),
    );

    renderDetail();
    const user = userEvent.setup();

    const generateBtn = await screen.findByRole('button', {
      name: i18n.t('meetings.minutes.generate'),
    });
    await user.click(generateBtn);

    const summary = await screen.findByPlaceholderText(i18n.t('meetings.minutes.summaryPlaceholder'));
    await waitFor(() => expect((summary as HTMLTextAreaElement).value).toBe('Auto-Entwurf.'));

    await user.clear(summary);
    await user.type(summary, 'Von Hand editiert.');
    // Confirm without an explicit Save → must persist the edit first.
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.minutes.confirm') }));

    await waitFor(() => expect(saved?.summary).toBe('Von Hand editiert.'));
    await waitFor(() => expect(confirmed).toBe(true));
  });

  it('relabels a speaker from the transcript section', async () => {
    let relabeled: { speaker_key: string; label: string } | null = null;
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json(features())),
      http.get(`${BASE_URL}/api/meetings/6`, () => HttpResponse.json(mkMeeting({ title: 'Planning' }))),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({
          id: 6,
          status: 'completed',
          segments: [
            { speaker: 'Sprecher 1', speaker_key: 'S1', start_s: 0, end_s: 2, text: 'Guten Morgen.' },
          ],
        }),
      ),
      http.post(`${BASE_URL}/api/meetings/6/relabel`, async ({ request }) => {
        relabeled = (await request.json()) as { speaker_key: string; label: string };
        return HttpResponse.json(mkMeeting({}));
      }),
    );

    renderDetail();
    const user = userEvent.setup();

    // Minutes off → transcript renders directly.
    await waitFor(() => expect(screen.getByText('Guten Morgen.')).toBeInTheDocument());
    await user.type(
      screen.getByLabelText(i18n.t('meetings.relabelAria', { speaker: 'Sprecher 1' })),
      'Anna',
    );
    await user.click(screen.getAllByRole('button', { name: i18n.t('meetings.relabelSave') })[0]);

    await waitFor(() => expect(relabeled).toEqual({ speaker_key: 'S1', label: 'Anna' }));
  });

  it('shows the cross-meeting merge notice when a relabel propagates (§2 Track A)', async () => {
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json(features())),
      http.get(`${BASE_URL}/api/meetings/6`, () => HttpResponse.json(mkMeeting({ title: 'Planning' }))),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({
          id: 6,
          status: 'completed',
          segments: [
            { speaker: 'Sprecher 1', speaker_key: 'S1', start_s: 0, end_s: 2, text: 'Hi.', fingerprint_id: 3, fingerprint_label: 'Speaker AB' },
          ],
        }),
      ),
      // Relabel propagated to 2 other meetings sharing the fingerprint.
      http.post(`${BASE_URL}/api/meetings/6/relabel`, () =>
        HttpResponse.json({ ...mkMeeting({}), cross_meeting_applied: 2 }),
      ),
    );

    renderDetail();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Hi.')).toBeInTheDocument());
    await user.type(
      screen.getByLabelText(i18n.t('meetings.relabelAria', { speaker: 'Sprecher 1' })),
      'Anna',
    );
    await user.click(screen.getAllByRole('button', { name: i18n.t('meetings.relabelSave') })[0]);

    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.crossMeetingApplied', { count: 2 }))).toBeInTheDocument(),
    );
  });

  it('discards minutes back to the generate state', async () => {
    let discarded = false;
    let state = {
      id: 6,
      minutes_status: 'draft' as string,
      minutes: { summary: 'Entwurf.', decisions: [], action_items: [] } as unknown,
    };
    server.use(
      http.get(`${BASE_URL}/api/config/features`, () =>
        HttpResponse.json(features({ meeting_minutes_enabled: true })),
      ),
      http.get(`${BASE_URL}/api/meetings/6`, () =>
        HttpResponse.json(mkMeeting({ minutes_status: state.minutes_status as Meeting['minutes_status'] })),
      ),
      http.get(`${BASE_URL}/api/meetings/6/segments`, () =>
        HttpResponse.json({ id: 6, status: 'completed', segments: [] }),
      ),
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () => HttpResponse.json(state)),
      http.delete(`${BASE_URL}/api/meetings/6/minutes`, () => {
        discarded = true;
        state = { id: 6, minutes_status: 'none', minutes: null };
        return HttpResponse.json(state);
      }),
    );

    renderDetail();
    const user = userEvent.setup();

    // Draft loads → discard it → panel returns to the Generate CTA.
    await user.click(await screen.findByRole('button', { name: i18n.t('meetings.minutes.discard') }));
    await waitFor(() => expect(discarded).toBe(true));
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: i18n.t('meetings.minutes.generate') }),
      ).toBeInTheDocument(),
    );
  });
});
