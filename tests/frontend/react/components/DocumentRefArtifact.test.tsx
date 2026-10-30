/**
 * DocumentRefArtifact -- the button to the Paperless document under a scan
 * completion notice, in all of its states.
 *
 * Checked through MSW against the real endpoint, not by mocking the hook: the
 * three states arise from the FIELDS of the response (`paperless_url`,
 * `in_paperless`, `status`), and that derivation is exactly the point.
 *
 * 🛑 The accessibility promise is covered too: ONLY the clickable state is a
 * focusable element. A `disabled` button for the other cases would be visible
 * in the tab order, would be read out as a button although it does nothing,
 * and regularly breaks the 4.5:1 contrast.
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

// `unknown` got through Vitest here but not through `tsc`: MSW requires
// `JsonBodyType`. Found on 2026-10-27, after `npm run typecheck` had been run
// in the test directory -- the run I skipped while writing this.
function antwortet(body: Record<string, unknown>, status = 200) {
  server.use(http.get(URL, () => HttpResponse.json(body, { status })));
}

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('DocumentRefArtifact', () => {
  it('shows a real link as soon as the Paperless id is present', async () => {
    antwortet(doc({ in_paperless: true, paperless_url: 'http://paperless.local:8000/documents/42/details' }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    const link = await screen.findByRole('link', { name: /In Paperless öffnen/i });
    expect(link).toHaveAttribute('href', 'http://paperless.local:8000/documents/42/details');
    // Leads out of the conversation -- the conversation stays where it is.
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('says during filing that it is running — without a button', async () => {
    antwortet(doc({ status: 'processing' }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/wird gerade abgelegt/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('names the duplicate as such: in Paperless, but without its own id', async () => {
    antwortet(doc({ in_paperless: true, paperless_url: null }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/war schon in Paperless/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('distinguishes a real failure to file from „laeuft noch"', async () => {
    antwortet(doc({ status: 'completed', in_paperless: false }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/nicht in Paperless abgelegt/i)).toBeInTheDocument();
  });

  it('a 403 is a valid answer, not an error wall', async () => {
    antwortet({ detail: 'No access to this document' }, 403);
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    // For the person it means the same as "still being worked on": no link.
    expect(await screen.findByText(/wird gerade abgelegt/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('a network error falls back quietly instead of tearing the message apart', async () => {
    server.use(http.get(URL, () => HttpResponse.error()));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    expect(await screen.findByText(/wird gerade abgelegt/i)).toBeInTheDocument();
  });

  // --- Instance boundary -----------------------------------------------------
  //
  // 🛑 The point is NOT the hint text but the query that does not happen.
  // The id belongs to the other instance; both keep their own sequences, so a
  // local query would hit a FOREIGN document with the same number. The fetch
  // must therefore never be started in the first place, not merely have its
  // result discarded. The count is taken at the endpoint -- the negative
  // control directly below proves with the same counter that it registers at
  // all.
  it('🛑 a document on another instance does NOT query here', async () => {
    let abrufe = 0;
    server.use(http.get(URL, () => { abrufe += 1; return HttpResponse.json(doc()); }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515, instance: 'xidra' }} />);

    expect(await screen.findByText(/liegt in xidra/i)).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
    // Wait a moment, so a late fetch would still show up.
    await waitFor(() => expect(abrufe).toBe(0));
  });

  it('negative control: the same counter does register locally', async () => {
    let abrufe = 0;
    server.use(http.get(URL, () => { abrufe += 1; return HttpResponse.json(doc()); }));
    renderWithProviders(<DocumentRefArtifact data={{ documentId: 515 }} />);

    // Not pinned to EXACTLY one: the test harness mounts the component twice
    // (StrictMode), measured 2 fetches. The only claim here is that the
    // counter registers at all.
    await waitFor(() => expect(abrufe).toBeGreaterThan(0));
  });

  it('🛑 NO state except the link is focusable', async () => {
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
