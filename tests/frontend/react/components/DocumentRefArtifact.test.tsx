/**
 * DocumentRefArtifact — der Knopf zum Paperless-Dokument unter einer
 * Scan-Fertigmeldung, in allen seinen Zustaenden.
 *
 * Geprueft wird ueber MSW am echten Endpunkt, nicht durch Mocken des Hooks: die
 * drei Zustaende entstehen aus den FELDERN der Antwort (`paperless_url`,
 * `in_paperless`, `status`), und genau diese Ableitung ist der Kern.
 *
 * 🛑 Die Zugaenglichkeitszusage ist mitgetestet: NUR der klickbare Zustand ist
 * ein fokussierbares Element. Ein `disabled`-Knopf fuer die uebrigen Faelle
 * waere im Tabbenverlauf sichtbar, wuerde als Schaltflaeche vorgelesen, obwohl
 * er nichts tut, und reisst regelmaessig den 4,5:1-Kontrast.
 */
import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import DocumentRefArtifact from '../../../../src/frontend/src/components/chat/artifacts/DocumentRefArtifact';
import { renderWithProviders } from '../test-utils';
import { server } from '../mocks/server';
import { TEST_CONFIG } from '../config';

const URL = `${TEST_CONFIG.API_BASE_URL}/api/knowledge/documents/:id`;

function doc(over: Record<string, unknown> = {}) {
  return {
    id: 515,
    filename: 'rechnung.pdf',
    status: 'completed',
    in_paperless: false,
    paperless_url: null,
    ...over,
  };
}

function antwortet(body: unknown, status = 200) {
  server.use(http.get(URL, () => HttpResponse.json(body, { status })));
}

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('DocumentRefArtifact', () => {
  it('zeigt einen echten Link, sobald die Paperless-Kennung da ist', async () => {
    antwortet(doc({ in_paperless: true, paperless_url: 'http://paperless.local:8000/documents/42/details' }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    const link = await screen.findByRole('link', { name: /In Paperless öffnen/i });
    expect(link).toHaveAttribute('href', 'http://paperless.local:8000/documents/42/details');
    // Fuehrt aus dem Gespraech heraus — das Gespraech bleibt stehen.
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('sagt waehrend der Ablage, dass sie laeuft — ohne Knopf', async () => {
    antwortet(doc({ status: 'processing' }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/wird gerade abgelegt/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('nennt die Dublette beim Namen: in Paperless, aber ohne eigene Kennung', async () => {
    antwortet(doc({ in_paperless: true, paperless_url: null }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/war schon in Paperless/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('unterscheidet ein echtes Nichtablegen von „laeuft noch"', async () => {
    antwortet(doc({ status: 'completed', in_paperless: false }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/nicht in Paperless abgelegt/i)).toBeInTheDocument();
  });

  it('ein 403 ist eine gueltige Antwort, keine Fehlerwand', async () => {
    antwortet({ detail: 'No access to this document' }, 403);
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    // Fuer die Person bedeutet es dasselbe wie „noch in Arbeit": kein Link.
    expect(await screen.findByText(/wird gerade abgelegt/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('ein Netzfehler faellt still zurueck, statt die Nachricht zu zerreissen', async () => {
    server.use(http.get(URL, () => HttpResponse.error()));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/wird gerade abgelegt/i)).toBeInTheDocument();
  });

  it('🛑 KEIN Zustand ausser dem Link ist fokussierbar', async () => {
    for (const fall of [
      doc({ status: 'processing' }),
      doc({ in_paperless: true, paperless_url: null }),
      doc({ status: 'completed', in_paperless: false }),
    ]) {
      antwortet(fall);
      const { unmount } = renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);
      await waitFor(() => expect(screen.queryByText(/abgelegt|Paperless/i)).toBeInTheDocument());
      expect(screen.queryByRole('link')).toBeNull();
      expect(screen.queryByRole('button')).toBeNull();
      unmount();
    }
  });
});
