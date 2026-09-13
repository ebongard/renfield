/**
 * Ingest credentials UI (#1218 Phase 2).
 *
 * The assertions that matter are the ones the single shared token could not
 * express: a token is shown exactly once, rotation is gated behind a warning
 * naming the client that breaks, and an env-managed legacy token offers no
 * rotation at all — because the backend would silently revert it.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../../../src/frontend/src/i18n';

const get = vi.fn();
const post = vi.fn();
const del = vi.fn();

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));

const { default: IngestCredentials } = await import(
  '../../../../src/frontend/src/components/integrations/IngestCredentials'
);

const credential = {
  client_id: 'scanner',
  label: 'Scanner',
  route: 'folder_ingest',
  owner: null,
  tier: 1,
  kb_name: 'Scans',
  created_at: '2026-09-08T10:00:00Z',
  rotated_at: null,
  last_authenticated_at: '2026-09-08T11:00:00Z',
  revoked_at: null,
  is_enabled: true,
};

const body = (o: Record<string, unknown> = {}) => ({
  data: { credentials: [credential], legacy: [], enabled: true, ...o },
});

function renderIt() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={qc}>
        <IngestCredentials />
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage('en');
  get.mockResolvedValue(body());
});

describe('IngestCredentials', () => {
  it('lists a credential with its client id', async () => {
    renderIt();
    expect(await screen.findByText('scanner')).toBeInTheDocument();
  });

  it('never renders a token value in the list', async () => {
    const { container } = renderIt();
    await screen.findByText('scanner');
    // Only a hash is stored server-side; nothing token-shaped may reach the DOM.
    expect(container.textContent).not.toContain('rfi.');
  });

  it('requires confirmation before rotating, naming the affected client', async () => {
    const user = userEvent.setup();
    renderIt();
    await screen.findByText('scanner');
    await user.click(screen.getByRole('button', { name: /^rotate$/i }));
    // A bare rotate button is a footgun: the client keeps presenting the old
    // token until its copy is updated, so the warning must name it.
    expect(await screen.findByRole('button', { name: /confirm rotation/i })).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/keeps presenting the old token/i);
    expect(post).not.toHaveBeenCalled();
  });

  it('shows the token once after minting, with a not-retrievable warning', async () => {
    post.mockResolvedValue({ data: { client_id: 'newone', token: 'rfi.newone.s3cr3t' } });
    const user = userEvent.setup();
    renderIt();
    await screen.findByText('scanner');
    await user.type(screen.getByPlaceholderText('scanner'), 'newone');
    await user.click(screen.getByRole('button', { name: /create token/i }));
    expect(await screen.findByText('rfi.newone.s3cr3t')).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/cannot be retrieved again/i);
  });

  it('requires confirmation before revoking, which is the more destructive action', async () => {
    const user = userEvent.setup();
    renderIt();
    await screen.findByText('scanner');
    await user.click(screen.getByRole('button', { name: /^revoke$/i }));
    expect(await screen.findByRole('button', { name: /confirm revocation/i })).toBeInTheDocument();
    // The client id stays taken after revocation, so the warning must say how
    // to get it back rather than implying the name is freed.
    expect(document.body.textContent).toMatch(/stays taken/i);
    expect(del).not.toHaveBeenCalled();
  });

  it('shows where the client files, which the client itself cannot choose', async () => {
    renderIt();
    await screen.findByText('scanner');
    expect(document.body.textContent).toMatch(/Files into/i);
    expect(document.body.textContent).toMatch(/Scans/);
    expect(document.body.textContent).toMatch(/Tier 1/);
  });

  it('does not render a tier when the field is absent, not just null', async () => {
    // A response missing the field would otherwise render "Tier undefined".
    get.mockResolvedValue(body({
      credentials: [{ ...credential, tier: undefined, kb_name: undefined }],
    }));
    renderIt();
    await screen.findByText('scanner');
    expect(document.body.textContent).not.toMatch(/undefined/);
  });

  it('warns when the feature flag is off', async () => {
    get.mockResolvedValue(body({ enabled: false }));
    renderIt();
    await waitFor(() =>
      expect(document.body.textContent).toMatch(/INGEST_CREDENTIALS_ENABLED/));
  });

  it('renders an env-managed legacy token read-only, with no rotate action', async () => {
    // The backend re-seeds it from env on every boot, so a rotation offered
    // here would silently revert at the next restart.
    get.mockResolvedValue(body({
      credentials: [],
      legacy: [{ route: 'folder_ingest', configured: true, source: 'env' }],
    }));
    renderIt();
    await waitFor(() =>
      expect(document.body.textContent).toMatch(/managed by the backend environment/i));
    expect(screen.queryByRole('button', { name: /^rotate$/i })).toBeNull();
  });
});
