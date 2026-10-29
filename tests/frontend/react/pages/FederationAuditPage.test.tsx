/**
 * F4d — federation audit page.
 *
 * Covers: empty state, happy-path render with multiple entries, row
 * expansion, peer filter via query param, and the success/failed/unknown
 * status icons + localized copy.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import type { AxiosResponse } from 'axios';
import FederationAuditPage, { basisHelpKey, basisBadgeClass } from '../../../../src/frontend/src/pages/FederationAuditPage';
import { renderWithRouter } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import apiClient from '../../../../src/frontend/src/utils/axios';
import type { AuditEntry } from '../../../../src/frontend/src/api/resources/federation';

interface AuditResponseBody {
  entries: AuditEntry[];
  limit: number;
  offset: number;
  peer_pubkey: string | null;
}

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
  },
  extractApiError: (err: unknown, fallback: string): string => {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (!detail) return fallback;
    if (typeof detail === 'string') return detail;
    return fallback;
  },
  extractFieldErrors: (): Record<string, string> => ({}),
}));

beforeAll(() => {
  // In case any child component tries scrollIntoView
  Element.prototype.scrollIntoView = vi.fn();
});

const MOM_PUBKEY = 'm'.repeat(64);
const DAD_PUBKEY = 'd'.repeat(64);

// Helper: typed handle on the mocked apiClient.get
const mockedGet = vi.mocked(apiClient.get);

function mockAuditResponse(body: AuditResponseBody) {
  const response: AxiosResponse<AuditResponseBody> = {
    data: body,
    status: 200,
    statusText: 'OK',
    headers: {},
    // axios v1 requires a config object — use a permissive typed empty cast
    // through the AxiosResponse generic (no `as any`).
    config: { headers: {} as never },
  };
  mockedGet.mockResolvedValueOnce(response);
}

describe('FederationAuditPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGet.mockReset();
  });

  it('renders empty state when no entries', async () => {
    mockAuditResponse({ entries: [], limit: 50, offset: 0, peer_pubkey: null });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText(/Noch keine föderierten Anfragen/i)).toBeInTheDocument();
    });
  });

  it('renders rows with peer, query, and initiation time', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann ist Omas Geburtstag?',
          initiated_at: '2026-04-22T10:15:00',
          finalized_at: '2026-04-22T10:15:03',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 14. Juni.',
        },
        {
          id: '2',
          peer_pubkey: DAD_PUBKEY,
          peer_display_name: 'Dad',
          query_text: 'Wie spät ist es bei dir?',
          initiated_at: '2026-04-22T09:00:00',
          final_status: 'failed',
          verified_signature: false,
          error_message: 'Peer connection refused',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: null,
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText('Mom')).toBeInTheDocument();
      expect(screen.getByText('Dad')).toBeInTheDocument();
    });
    expect(screen.getByText('Wann ist Omas Geburtstag?')).toBeInTheDocument();
    expect(screen.getByText('Wie spät ist es bei dir?')).toBeInTheDocument();
  });

  it('expanding a row shows the full answer and fingerprint', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann ist Omas Geburtstag?',
          initiated_at: '2026-04-22T10:15:00',
          finalized_at: '2026-04-22T10:15:03',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 14. Juni. Sie wird 87.',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: null,
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    const row = await screen.findByRole('button', { expanded: false, name: /Mom/i });
    fireEvent.click(row);

    await waitFor(() => {
      expect(screen.getByText('Am 14. Juni. Sie wird 87.')).toBeInTheDocument();
      expect(screen.getByText(new RegExp(MOM_PUBKEY.slice(0, 12)))).toBeInTheDocument();
    });
  });

  it('renders the error message on a failed entry when expanded', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '99',
          peer_pubkey: DAD_PUBKEY,
          peer_display_name: 'Dad',
          query_text: 'Was gibt es zum Abendessen?',
          initiated_at: '2026-04-22T18:00:00',
          finalized_at: '2026-04-22T18:00:05',
          final_status: 'failed',
          verified_signature: false,
          error_message: 'Responder signature verification failed',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: null,
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    const row = await screen.findByRole('button', { name: /Dad/i });
    fireEvent.click(row);

    await waitFor(() => {
      expect(screen.getByText('Responder signature verification failed')).toBeInTheDocument();
    });
  });

  // 🛑 Die Identitätsgrundlage entscheidet über die REICHWEITE eines Zugriffs.
  // „als du" heißt: die Gegenseite hat mit der vollen eigenen Reichweite der
  // verknüpften Person geantwortet, nicht nur mit dem Freigegebenen.
  it('zeigt „als du" bei einer Antwort auf verknüpfter Identität', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'was liegt in x-idra?',
          initiated_at: '2026-10-29T14:43:37',
          final_status: 'success',
          verified_signature: true,
          direction: 'answered',
          identity_basis: 'mapped',
        },
      ],
      limit: 50, offset: 0, peer_pubkey: null,
    });
    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });
    expect(await screen.findByText(/als du/i)).toBeInTheDocument();
    expect(screen.getByText(/beantwortet/i)).toBeInTheDocument();
  });

  it('zeigt „als fremde Instanz" im Rückfall', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'was liegt in x-idra?',
          initiated_at: '2026-10-29T14:43:37',
          final_status: 'success',
          direction: 'answered',
          identity_basis: 'peer_tier',
        },
      ],
      limit: 50, offset: 0, peer_pubkey: null,
    });
    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });
    expect(await screen.findByText(/als fremde Instanz/i)).toBeInTheDocument();
  });

  // 🛑 Die wichtigste der drei. `null` heißt „nicht festgehalten", NICHT
  // „peer_tier": bei Frager-Zeilen weiß diese Instanz es nicht, und Zeilen von
  // vor dem 2026-10-29 kennen die Angabe gar nicht. Würde die Oberfläche hier
  // „als fremde Instanz" zeigen, behauptete sie die ENGERE Stufe für Züge, die
  // mit voller Reichweite liefen — die gefährlichere Richtung des Irrtums.
  it('behauptet NICHTS, wenn die Grundlage fehlt', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann ist Omas Geburtstag?',
          initiated_at: '2026-04-22T10:15:00',
          final_status: 'success',
          direction: 'asked',
        },
      ],
      limit: 50, offset: 0, peer_pubkey: null,
    });
    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });
    // Die Zeile ist da …
    expect(await screen.findByText(/Omas Geburtstag/i)).toBeInTheDocument();
    // … aber ohne jede Behauptung über die Identität.
    expect(screen.queryByText(/als du/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/als fremde Instanz/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/beantwortet/i)).not.toBeInTheDocument();
  });

  // 🛑 Der Riegel gegen eine stille Fehlbeschriftung: die Spalte ist
  // serverseitig ein freier Text. Fiele ein unbekannter Wert in den
  // `peer_tier`-Zweig, behauptete die Ansicht die ENGERE Reichweite für einen
  // Zugriff, über den sie nichts weiß — dieselbe gefährliche Richtung wie beim
  // fehlenden Wert, nur schwerer zu bemerken.
  it('zeigt einen unbekannten Wert ROH, statt ihn als „fremde Instanz" auszugeben', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'was liegt in x-idra?',
          initiated_at: '2026-10-29T14:43:37',
          final_status: 'success',
          direction: 'answered',
          identity_basis: 'delegated',
        },
      ],
      limit: 50, offset: 0, peer_pubkey: null,
    });
    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });
    const plaettchen = await screen.findByText('delegated');
    expect(screen.queryByText(/als fremde Instanz/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/als du/i)).not.toBeInTheDocument();
    // 🛑 Die VERDRAHTUNG, nicht nur die Funktion: ersetzte jemand
    // `title={t(basisHelpKey(...))}` durch einen festen Schlüssel, blieben die
    // Funktionstests grün und die Kurzinfo behauptete wieder die engere Stufe.
    expect(plaettchen).toHaveAttribute(
      'title', i18n.t('federationAudit.basisUnknownHelp'));
    // 🛑 Und die FARBE: dasselbe Grau wie `peer_tier` wäre die engere
    // Behauptung im Bild statt im Wort.
    expect(plaettchen.className).not.toBe(
      `text-xs px-1.5 py-0.5 rounded ${basisBadgeClass('peer_tier')}`);
  });

  it('färbt die drei Fälle unterscheidbar', async () => {
    const m = basisBadgeClass('mapped');
    const p = basisBadgeClass('peer_tier');
    const u = basisBadgeClass('delegated');
    expect(new Set([m, p, u]).size).toBe(3);
    expect(u).toBe(basisBadgeClass(''));
  });

  it('zeigt den Schlüssel, wenn der Name der Gegenstelle fehlt', async () => {
    // 🛑 Zeilen, die der Antwortende vor dem 2026-10-29 geschrieben hat,
    // tragen `peer_display_name_snapshot=""`. An dieser Stelle steht, WER
    // zugegriffen hat — eine Lücke wäre dort das Gegenteil eines Prüfprotokolls.
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: '',
          query_text: 'was liegt in x-idra?',
          initiated_at: '2026-10-29T14:43:37',
          final_status: 'success',
          direction: 'answered',
          identity_basis: 'mapped',
        },
      ],
      limit: 50, offset: 0, peer_pubkey: null,
    });
    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });
    expect(
      await screen.findByText(`${MOM_PUBKEY.slice(0, 12)}\u2026`),
    ).toBeInTheDocument();
  });

  it('ordnet jedem Wert seinen eigenen Hilfetext zu', async () => {
    // Direkt an der Zuordnungsfunktion gemessen, nicht am `title`-Attribut:
    // ein Kurzinfo-Text ist auf Fingergeräten unsichtbar, die Zuordnung aber
    // ist die Zusicherung, die trägt.
    expect(basisHelpKey('mapped')).toBe('federationAudit.basisMappedHelp');
    expect(basisHelpKey('peer_tier')).toBe('federationAudit.basisPeerTierHelp');
    expect(basisHelpKey('delegated')).toBe('federationAudit.basisUnknownHelp');
    expect(basisHelpKey('')).toBe('federationAudit.basisUnknownHelp');
  });

  it('der Hilfetext behauptet nicht, WELCHE Instanz geantwortet hat', async () => {
    // 🛑 Er erscheint auch auf einer Frager-Zeile, sobald die Gegenseite die
    // Angabe einmal mitliefert. „Diese Instanz hat geantwortet" wäre dort
    // schlicht falsch — also sagt der Text es gar nicht erst.
    for (const k of ['basisMappedHelp', 'basisPeerTierHelp', 'basisUnknownHelp']) {
      const text = i18n.t(`federationAudit.${k}`);
      // i18next gibt bei einem fehlenden Schlüssel den Schlüssel zurück — der
      // enthält „Diese Instanz" nie, der Test ginge also leer durch. Erst
      // belegen, dass überhaupt ein Text da ist.
      expect(text).not.toBe(`federationAudit.${k}`);
      expect(text).not.toMatch(/Diese Instanz/i);
    }
  });

  it('nennt einen abgebrochenen Zugriff abgebrochen, nicht unbekannt', async () => {
    // 🛑 `unknown` und `aborted` sind nicht dasselbe. Auf der Antwortseite
    // WEISS diese Instanz, wie weit sie gekommen ist — und ob dabei fremde
    // Daten schon gelesen waren. Das unter „Status unbekannt" zu führen wäre
    // eine Beschönigung.
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'was liegt in x-idra?',
          initiated_at: '2026-10-29T14:43:37',
          final_status: 'aborted',
          error_message: 'Abgebrochen nach dem Abruf — 3 Treffer gelesen, keine Antwort ausgeliefert.',
          direction: 'answered',
          identity_basis: 'mapped',
        },
      ],
      limit: 50, offset: 0, peer_pubkey: null,
    });
    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });
    // Aufklappen — dort steht der Status im Klartext.
    fireEvent.click(await screen.findByText(/was liegt in x-idra/i));
    expect(await screen.findByText('Abgebrochen')).toBeInTheDocument();
    expect(screen.queryByText(/unbekannt/i)).not.toBeInTheDocument();
    // Und der GRUND, der die datenschutzrelevante Hälfte trägt.
    expect(screen.getByText(/3 Treffer gelesen/)).toBeInTheDocument();
  });

  it('filters by peer when ?peer= query param is present', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann war die Hochzeit?',
          initiated_at: '2026-04-22T10:00:00',
          finalized_at: '2026-04-22T10:00:02',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 3. Juli.',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: MOM_PUBKEY,
    });

    renderWithRouter(<FederationAuditPage />, { route: `/brain/audit?peer=${MOM_PUBKEY}` });

    await waitFor(() => {
      expect(mockedGet).toHaveBeenCalledWith(
        expect.stringContaining(`peer_pubkey=${MOM_PUBKEY}`),
      );
      expect(screen.getByText(/Gefiltert auf Mom/i)).toBeInTheDocument();
    });
  });

  it('shows error alert on API failure', async () => {
    mockedGet.mockRejectedValueOnce(new Error('500'));

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText(/Verlauf konnte nicht geladen werden/i)).toBeInTheDocument();
    });
  });
});
