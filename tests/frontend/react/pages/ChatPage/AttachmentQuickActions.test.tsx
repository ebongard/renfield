import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AttachmentQuickActions from '../../../../../src/frontend/src/pages/ChatPage/AttachmentQuickActions';
import type { MessageAttachment } from '../../../../../src/frontend/src/pages/ChatPage/context/ChatContext';

// Mock axios — the component uses it to fetch the KB list AND (for Simba) the
// category→type map. Return the right shape per URL.
vi.mock('../../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn((url: string) =>
      url.includes('simba/categories')
        ? Promise.resolve({ data: { categories: { Belege: ['Ausgangsrechnung'] } } })
        : Promise.resolve({
            data: [
              { id: 1, name: 'Hauptwissensbasis' },
              { id: 2, name: 'Reise-KB' },
            ],
          }),
    ),
  },
}));

// Mock the feature-flags hook (react-query) — the component gates the Simba
// items on simba_upload_enabled. A mutable ref lets each test set it.
const { flagsRef } = vi.hoisted(() => ({
  flagsRef: { current: {} as { simba_upload_enabled?: boolean } },
}));
vi.mock('../../../../../src/frontend/src/api/resources/brain', () => ({
  useFeatureFlags: () => ({ data: flagsRef.current }),
}));

// Minimal i18n mock — the component uses `useTranslation` with simple
// keys and one interpolated translation. Returning the key itself is
// enough for these tests since we assert by role/title, not by the
// translated label text.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: 'en' },
  }),
}));

const baseAttachment: MessageAttachment = {
  id: 'upload-42',
  filename: 'document.pdf',
  status: 'completed',
};

type IndexToKb = (attachmentId: string, kbId: string | number) => void;
type SingleArg = (attachmentId: string) => void;

type SimbaFn = (attachmentId: string, category: string, type: string) => void;

interface RenderProps {
  onIndexToKb?: ReturnType<typeof vi.fn<IndexToKb>>;
  onSendToPaperless?: ReturnType<typeof vi.fn<SingleArg>>;
  onSendToBoth?: ReturnType<typeof vi.fn<IndexToKb>>;
  onSendToSimba?: ReturnType<typeof vi.fn<SimbaFn>>;
  onSendToPaperlessAndSimba?: ReturnType<typeof vi.fn<SimbaFn>>;
  onSendViaEmail?: ReturnType<typeof vi.fn<SingleArg>>;
  onSummarize?: ReturnType<typeof vi.fn<SingleArg>>;
  attachment?: MessageAttachment;
}

function renderActions(overrides: RenderProps = {}) {
  const onIndexToKb = overrides.onIndexToKb ?? vi.fn<IndexToKb>();
  const onSendToPaperless = overrides.onSendToPaperless ?? vi.fn<SingleArg>();
  const onSendToBoth = overrides.onSendToBoth ?? vi.fn<IndexToKb>();
  const onSendToSimba = overrides.onSendToSimba ?? vi.fn<SimbaFn>();
  const onSendToPaperlessAndSimba = overrides.onSendToPaperlessAndSimba ?? vi.fn<SimbaFn>();
  const onSendViaEmail = overrides.onSendViaEmail ?? vi.fn<SingleArg>();
  const onSummarize = overrides.onSummarize ?? vi.fn<SingleArg>();
  render(
    <AttachmentQuickActions
      attachment={overrides.attachment ?? baseAttachment}
      onIndexToKb={onIndexToKb}
      onSendToPaperless={onSendToPaperless}
      onSendToBoth={onSendToBoth}
      onSendToSimba={onSendToSimba}
      onSendToPaperlessAndSimba={onSendToPaperlessAndSimba}
      onSendViaEmail={onSendViaEmail}
      onSummarize={onSummarize}
    />,
  );
  return { onIndexToKb, onSendToPaperless, onSendToBoth, onSendToSimba, onSendToPaperlessAndSimba, onSendViaEmail, onSummarize };
}

describe('AttachmentQuickActions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flagsRef.current = {}; // simba disabled by default
  });

  it('opens the menu and shows the Paperless + KB combo at the top', async () => {
    const user = userEvent.setup();
    renderActions();

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));

    // Top of the menu: combined dispatch.
    const items = screen.getAllByRole('button');
    const labels = items.map((b) => b.textContent ?? '');
    const comboIdx = labels.findIndex((l) => l.includes('chat.sendToPaperlessAndKb'));
    const addToKbIdx = labels.findIndex((l) => l === 'chat.addToKb' || l.includes('chat.addToKb'));
    expect(comboIdx).toBeGreaterThan(-1);
    expect(addToKbIdx).toBeGreaterThan(-1);
    // The combo should appear before plain Add-to-KB in the menu.
    expect(comboIdx).toBeLessThan(addToKbIdx);
  });

  it('combo: clicking Paperless + KB opens the KB picker, picking a KB calls onSendToBoth (not onIndexToKb)', async () => {
    const user = userEvent.setup();
    const { onSendToBoth, onIndexToKb } = renderActions();

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));
    await user.click(screen.getByText('chat.sendToPaperlessAndKb'));

    // KB list loaded via mocked axios.
    await waitFor(() => {
      expect(screen.getByText('Hauptwissensbasis')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Reise-KB'));

    expect(onSendToBoth).toHaveBeenCalledTimes(1);
    expect(onSendToBoth).toHaveBeenCalledWith('upload-42', 2);
    // Routing must NOT cross-fire the standalone indexer — that would
    // double-index and trigger 409s on retry.
    expect(onIndexToKb).not.toHaveBeenCalled();
  });

  it('add-to-KB still routes to onIndexToKb after the combo refactor', async () => {
    const user = userEvent.setup();
    const { onIndexToKb, onSendToBoth } = renderActions();

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));
    await user.click(screen.getByText('chat.addToKb'));

    await waitFor(() => {
      expect(screen.getByText('Hauptwissensbasis')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Hauptwissensbasis'));

    expect(onIndexToKb).toHaveBeenCalledWith('upload-42', 1);
    expect(onSendToBoth).not.toHaveBeenCalled();
  });

  it('hides Paperless + KB when the attachment is already indexed (no double-index)', async () => {
    const user = userEvent.setup();
    renderActions({
      attachment: { ...baseAttachment, indexed: true },
    });

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));

    expect(screen.queryByText('chat.sendToPaperlessAndKb')).not.toBeInTheDocument();
    expect(screen.queryByText('chat.addToKb')).not.toBeInTheDocument();
    // Standalone Paperless still available — user might want to forward
    // a doc that's already in their KB to Paperless separately.
    expect(screen.getByText('chat.sendToPaperless')).toBeInTheDocument();
  });

  it('hides the Simba items when the simba MCP is not configured (flag off)', async () => {
    const user = userEvent.setup();
    renderActions(); // flagsRef default = simba disabled

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));

    expect(screen.queryByText('chat.sendToSimba')).not.toBeInTheDocument();
    expect(screen.queryByText('chat.sendToPaperlessAndSimba')).not.toBeInTheDocument();
  });

  it('shows Simba items when enabled; drilling category→type + confirm calls onSendToSimba', async () => {
    flagsRef.current = { simba_upload_enabled: true };
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    const { onSendToSimba } = renderActions();

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));
    expect(screen.getByText('chat.sendToSimba')).toBeInTheDocument();

    await user.click(screen.getByText('chat.sendToSimba'));
    // Category list from the mocked categories endpoint.
    await waitFor(() => expect(screen.getByText('Belege')).toBeInTheDocument());

    await user.click(screen.getByText('Belege'));
    await user.click(screen.getByText('Ausgangsrechnung'));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(onSendToSimba).toHaveBeenCalledWith('upload-42', 'Belege', 'Ausgangsrechnung');
    confirmSpy.mockRestore();
  });

  it('cancelling the Simba confirm does not upload', async () => {
    flagsRef.current = { simba_upload_enabled: true };
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const user = userEvent.setup();
    const { onSendToSimba } = renderActions();

    await user.click(screen.getByRole('button', { name: 'chat.quickActions' }));
    await user.click(screen.getByText('chat.sendToSimba'));
    await waitFor(() => expect(screen.getByText('Belege')).toBeInTheDocument());
    await user.click(screen.getByText('Belege'));
    await user.click(screen.getByText('Ausgangsrechnung'));

    expect(onSendToSimba).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
