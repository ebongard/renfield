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

  it('shows the merge-on-enroll message when a relabel propagates across meetings', async () => {
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([mkMeeting({ id: 5, title: 'Planning', status: 'completed' })]),
      ),
      http.get(`${BASE_URL}/api/meetings/5/segments`, () =>
        HttpResponse.json({
          id: 5, status: 'completed',
          segments: [{ speaker: 'Sprecher 1', speaker_key: 'S1', start_s: 0, end_s: 2, text: 'Hi.', fingerprint_id: 3, fingerprint_label: 'Speaker AB' }],
        }),
      ),
      // §2 Track A: relabel propagated to 2 other meetings sharing the fingerprint.
      http.post(`${BASE_URL}/api/meetings/5/relabel`, () =>
        HttpResponse.json({ ...mkMeeting({ id: 5 }), cross_meeting_applied: 2 }),
      ),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('Planning'));
    await waitFor(() => expect(screen.getByText('Hi.')).toBeInTheDocument());
    await user.type(screen.getByLabelText(i18n.t('meetings.relabelAria', { speaker: 'Sprecher 1' })), 'Anna');
    await user.click(screen.getAllByRole('button', { name: i18n.t('meetings.relabelSave') })[0]);

    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.crossMeetingApplied', { count: 2 }))).toBeInTheDocument(),
    );
  });
});

/** Enable the minutes feature flag and stub the transcript segments so a
 *  completed meeting can be expanded down to the minutes panel. */
function useMinutesEnabled(meetingId = 6) {
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
      HttpResponse.json([mkMeeting({ id: meetingId, title: 'Review', status: 'completed' })]),
    ),
    http.get(`${BASE_URL}/api/meetings/${meetingId}/segments`, () =>
      HttpResponse.json({
        id: meetingId,
        status: 'completed',
        segments: [
          { speaker: 'Sprecher 1', speaker_key: 'S1', start_s: 0, end_s: 2, text: 'Los gehts.' },
        ],
      }),
    ),
  );
}

describe('MeetingsPage — minutes (§2 Phase 3)', () => {
  it('hides the minutes panel when the flag is off', async () => {
    // Default /api/config/features handler has meeting_minutes_enabled: false.
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([mkMeeting({ id: 9, title: 'NoMinutes', status: 'completed' })]),
      ),
      http.get(`${BASE_URL}/api/meetings/9/segments`, () =>
        HttpResponse.json({ id: 9, status: 'completed', segments: [] }),
      ),
    );
    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('NoMinutes'));
    // The transcript loads, but the minutes panel title is never rendered.
    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.noSegments'))).toBeInTheDocument(),
    );
    expect(screen.queryByText(i18n.t('meetings.minutes.title'))).not.toBeInTheDocument();
  });

  it('surfaces a draft-ready badge on the collapsed card and puts minutes above a collapsible transcript', async () => {
    useMinutesEnabled(6);
    server.use(
      http.get(`${BASE_URL}/api/meetings`, () =>
        HttpResponse.json([
          mkMeeting({ id: 6, title: 'Review', status: 'completed', minutes_status: 'draft' }),
        ]),
      ),
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () =>
        HttpResponse.json({
          id: 6,
          minutes_status: 'draft',
          minutes: { summary: 'Entwurf.', decisions: [], action_items: [] },
        }),
      ),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    // The badge is visible on the collapsed card; the transcript toggle is not.
    const badge = await screen.findByRole('button', {
      name: i18n.t('meetings.minutes.draftReadyBadge'),
    });
    expect(
      screen.queryByRole('button', { name: i18n.t('meetings.transcriptToggle') }),
    ).not.toBeInTheDocument();

    // Clicking the badge expands → minutes render, transcript is behind a toggle.
    await user.click(badge);
    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.minutes.title'))).toBeInTheDocument(),
    );
    expect(
      screen.getByRole('button', { name: i18n.t('meetings.transcriptToggle') }),
    ).toBeInTheDocument();
    // Transcript content stays hidden until the toggle is opened.
    expect(screen.queryByText('Los gehts.')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.transcriptToggle') }));
    await waitFor(() => expect(screen.getByText('Los gehts.')).toBeInTheDocument());
  });

  it('generates, edits, and confirms minutes', async () => {
    let saved: { summary: string; decisions: unknown[]; action_items: unknown[] } | null = null;
    let confirmed = false;
    let generateCalls = 0;
    // Server-side minutes state the handlers mutate as the flow progresses.
    let state = { id: 6, minutes_status: 'none' as string, minutes: null as unknown };

    useMinutesEnabled(6);
    server.use(
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () => HttpResponse.json(state)),
      http.post(`${BASE_URL}/api/meetings/6/minutes/generate`, () => {
        generateCalls += 1;
        state = {
          id: 6,
          minutes_status: 'draft',
          minutes: { summary: 'Wir haben X beschlossen.', decisions: [], action_items: [] },
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

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    // Expand the completed meeting → the minutes panel shows the Generate CTA.
    await user.click(await screen.findByText('Review'));
    const generateBtn = await screen.findByRole('button', {
      name: i18n.t('meetings.minutes.generate'),
    });
    await user.click(generateBtn);
    expect(generateCalls).toBe(1);

    // Draft renders in the editable summary field.
    const summary = await screen.findByPlaceholderText(i18n.t('meetings.minutes.summaryPlaceholder'));
    await waitFor(() =>
      expect((summary as HTMLTextAreaElement).value).toBe('Wir haben X beschlossen.'),
    );

    // Edit the summary and Save → PUT carries the edit.
    await user.clear(summary);
    await user.type(summary, 'Korrigierte Zusammenfassung.');
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.minutes.save') }));
    await waitFor(() => expect(saved?.summary).toBe('Korrigierte Zusammenfassung.'));

    // Confirm → POST confirm, then the confirmed badge appears.
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.minutes.confirm') }));
    await waitFor(() => expect(confirmed).toBe(true));
    await waitFor(() =>
      expect(screen.getByText(i18n.t('meetings.minutes.confirmedBadge'))).toBeInTheDocument(),
    );
  });

  it('auto-saves unsaved edits when confirming directly (no explicit Save)', async () => {
    // Regression: Confirm must persist the live edit first, else the backend
    // confirms the last-saved draft and the reseed silently drops the edit.
    let saved: { summary: string } | null = null;
    let confirmedAfterSave = false;
    let state = {
      id: 6,
      minutes_status: 'draft' as string,
      minutes: { summary: 'Roh-Entwurf.', decisions: [], action_items: [] } as unknown,
    };
    useMinutesEnabled(6);
    server.use(
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () => HttpResponse.json(state)),
      http.put(`${BASE_URL}/api/meetings/6/minutes`, async ({ request }) => {
        saved = (await request.json()) as typeof saved;
        state = { id: 6, minutes_status: 'draft', minutes: saved };
        return HttpResponse.json(state);
      }),
      http.post(`${BASE_URL}/api/meetings/6/minutes/confirm`, () => {
        // Confirm must arrive AFTER the edit was persisted.
        confirmedAfterSave = saved?.summary === 'Von Hand editiert.';
        state = { ...state, minutes_status: 'confirmed' };
        return HttpResponse.json(state);
      }),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('Review'));
    const summary = await screen.findByPlaceholderText(i18n.t('meetings.minutes.summaryPlaceholder'));
    await user.clear(summary);
    await user.type(summary, 'Von Hand editiert.');
    // Click Confirm WITHOUT clicking Save first.
    await user.click(screen.getByRole('button', { name: i18n.t('meetings.minutes.confirm') }));

    await waitFor(() => expect(saved?.summary).toBe('Von Hand editiert.'));
    await waitFor(() => expect(confirmedAfterSave).toBe(true));
  });

  it('discards minutes back to the generate state', async () => {
    let discarded = false;
    let state = {
      id: 6,
      minutes_status: 'draft' as string,
      minutes: { summary: 'Entwurf.', decisions: [], action_items: [] } as unknown,
    };
    useMinutesEnabled(6);
    server.use(
      http.get(`${BASE_URL}/api/meetings/6/minutes`, () => HttpResponse.json(state)),
      http.delete(`${BASE_URL}/api/meetings/6/minutes`, () => {
        discarded = true;
        state = { id: 6, minutes_status: 'none', minutes: null };
        return HttpResponse.json(state);
      }),
    );

    renderWithProviders(<MeetingsPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('Review'));
    // Draft loads → discard it.
    await user.click(await screen.findByRole('button', { name: i18n.t('meetings.minutes.discard') }));
    await waitFor(() => expect(discarded).toBe(true));
    // Back to the Generate CTA.
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: i18n.t('meetings.minutes.generate') }),
      ).toBeInTheDocument(),
    );
  });
});
