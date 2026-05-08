import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AttachmentQuickActions from '../../../../../src/frontend/src/pages/ChatPage/AttachmentQuickActions';
import type { MessageAttachment } from '../../../../../src/frontend/src/pages/ChatPage/context/ChatContext';

// Mock axios — the component uses it directly to fetch the KB list.
vi.mock('../../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({
      data: [
        { id: 1, name: 'Hauptwissensbasis' },
        { id: 2, name: 'Reise-KB' },
      ],
    }),
  },
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

interface RenderProps {
  onIndexToKb?: ReturnType<typeof vi.fn<IndexToKb>>;
  onSendToPaperless?: ReturnType<typeof vi.fn<SingleArg>>;
  onSendToBoth?: ReturnType<typeof vi.fn<IndexToKb>>;
  onSendViaEmail?: ReturnType<typeof vi.fn<SingleArg>>;
  onSummarize?: ReturnType<typeof vi.fn<SingleArg>>;
  attachment?: MessageAttachment;
}

function renderActions(overrides: RenderProps = {}) {
  const onIndexToKb = overrides.onIndexToKb ?? vi.fn<IndexToKb>();
  const onSendToPaperless = overrides.onSendToPaperless ?? vi.fn<SingleArg>();
  const onSendToBoth = overrides.onSendToBoth ?? vi.fn<IndexToKb>();
  const onSendViaEmail = overrides.onSendViaEmail ?? vi.fn<SingleArg>();
  const onSummarize = overrides.onSummarize ?? vi.fn<SingleArg>();
  render(
    <AttachmentQuickActions
      attachment={overrides.attachment ?? baseAttachment}
      onIndexToKb={onIndexToKb}
      onSendToPaperless={onSendToPaperless}
      onSendToBoth={onSendToBoth}
      onSendViaEmail={onSendViaEmail}
      onSummarize={onSummarize}
    />,
  );
  return { onIndexToKb, onSendToPaperless, onSendToBoth, onSendViaEmail, onSummarize };
}

describe('AttachmentQuickActions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
});
