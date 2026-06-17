/**
 * MediaHandoffIndicator — room-handoff affordance (chat-UI roadmap item 8).
 *
 * Renders a quiet inline meta line when a `renfield-media-handoff` window event
 * fires (re-dispatched by useDeviceConnection from the device-WS `media_handoff`
 * frame). Covers: gated OFF (flag false) → nothing; flag ON → the line renders,
 * is a11y-labeled (role=status + aria-label, icon paired with text → not
 * color-only) and degrades to text; unknown room → generic label; the
 * 'continued' kind; and TTL auto-fade (transient). The feature flag is read via
 * React Query → MSW. German is the test default.
 *
 * Real timers throughout: React Query's async resolution and fake timers
 * deadlock `findBy*`. The listener only attaches once the flag query resolves
 * to enabled, so each enabled-case test first waits for that, then dispatches.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import MediaHandoffIndicator from '../../../../src/frontend/src/components/chat/MediaHandoffIndicator';
import { renderWithRouter } from '../test-utils';
import { server } from '../mocks/server';
import type { MediaHandoffMessage } from '../../../../src/frontend/src/types/device';

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const FEATURES_URL = `${BASE_URL}/api/config/features`;

const BASE_FLAGS = {
  schicht_a_extraction_enabled: false,
  wissen_workspace_enabled: false,
  command_palette_enabled: false,
  role_surfacing_enabled: false,
  message_search_enabled: false,
  artifacts_typed_enabled: false,
  room_handoff_enabled: false,
};

function mockFlags(roomHandoffEnabled: boolean) {
  server.use(
    http.get(FEATURES_URL, () =>
      HttpResponse.json({ ...BASE_FLAGS, room_handoff_enabled: roomHandoffEnabled }),
    ),
  );
}

function fireHandoff(detail: Partial<MediaHandoffMessage>) {
  act(() => {
    window.dispatchEvent(
      new CustomEvent('renfield-media-handoff', {
        detail: { type: 'media_handoff', kind: 'media_followed', room: 'Küche', title: 'Thriller', ...detail },
      }),
    );
  });
}

/**
 * Fire the handoff event and wait for `text` to appear. The window listener
 * only attaches AFTER the feature-flag query resolves to enabled, and a
 * one-shot dispatch sent before that is lost — so we re-fire on each poll until
 * the line shows (or the matcher gives up).
 */
async function fireAndExpect(detail: Partial<MediaHandoffMessage>, text: string) {
  await waitFor(() => {
    fireHandoff(detail);
    expect(screen.getByText(text)).toBeInTheDocument();
  });
}

describe('MediaHandoffIndicator', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing while the feature flag is off, even on a handoff event', async () => {
    mockFlags(false);
    const { container } = renderWithRouter(<MediaHandoffIndicator />);
    // Let the feature-flag query settle to disabled.
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    fireHandoff({ room: 'Küche' });
    // The listener is never attached → no status region appears.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a localized, a11y-labeled meta line when enabled', async () => {
    mockFlags(true);
    // Long TTL so the line doesn't fade out from under the a11y assertions.
    renderWithRouter(<MediaHandoffIndicator ttlMs={60_000} />);

    // de.json chat.mediaHandoff.followed = "Wiedergabe folgt nach {{room}}"
    await fireAndExpect({ kind: 'media_followed', room: 'Küche' }, 'Wiedergabe folgt nach Küche');

    // a11y: a status region wraps it (announced once) and each line carries an
    // aria-label — the icon is decorative (aria-hidden), text is the signal,
    // so it is NOT color-only.
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Hinweis: Medienwiedergabe ist dir in einen anderen Raum gefolgt'),
    ).toBeInTheDocument();
  });

  it('drops the room suffix when the room is unknown', async () => {
    mockFlags(true);
    renderWithRouter(<MediaHandoffIndicator ttlMs={60_000} />);
    // de.json chat.mediaHandoff.followedUnknownRoom = "Wiedergabe folgt dir"
    await fireAndExpect({ kind: 'media_followed', room: null }, 'Wiedergabe folgt dir');
  });

  it("renders the 'continued' kind with its own label", async () => {
    mockFlags(true);
    renderWithRouter(<MediaHandoffIndicator ttlMs={60_000} />);
    // de.json chat.mediaHandoff.continued = "Fortgesetzt in {{room}}"
    await fireAndExpect({ kind: 'continued', room: 'Wohnzimmer' }, 'Fortgesetzt in Wohnzimmer');
  });

  it('auto-fades the line after the TTL (transient, not persisted)', async () => {
    mockFlags(true);
    // Short TTL so the transient-fade assertion stays fast (prod default 12s).
    renderWithRouter(<MediaHandoffIndicator ttlMs={50} />);
    await fireAndExpect({ kind: 'media_followed', room: 'Küche' }, 'Wiedergabe folgt nach Küche');

    // The line removes itself after the TTL — never lingers, never persists.
    await waitFor(() =>
      expect(screen.queryByText('Wiedergabe folgt nach Küche')).not.toBeInTheDocument(),
    );
  });
});
