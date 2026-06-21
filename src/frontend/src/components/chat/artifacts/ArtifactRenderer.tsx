/**
 * ArtifactRenderer — dispatcher + error boundary + fallback for chat artifacts.
 *
 * Security-critical contract (`docs/design/chat-artifacts-sandbox.md` §5):
 *   - The raw payload is validated against the AUTHORITATIVE zod schema
 *     (`parseArtifact`). A shape failure → the escaped-code-block fallback.
 *   - A sub-renderer that throws is caught by the error boundary → fallback.
 *   - An unknown `kind` → fallback (the zod union already rejects it).
 *   - A `partial` artifact that never finalized (the turn ended) → fallback.
 *   The fallback is ALWAYS the escaped-text path (today's safe fenced-code
 *   behavior). It NEVER injects markup. Fail closed.
 *
 * Empty (well-formed but 0 data) is NOT a failure — the sub-renderer shows a
 * warm per-kind empty state; only schema-invalid / throw / unknown / stuck
 * partial fall back.
 */
import { Component, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles } from 'lucide-react';

import { parseArtifact, type ChatArtifact } from './artifactSchema';
import TableArtifact from './TableArtifact';
import ListArtifact from './ListArtifact';
import KeyValueArtifact from './KeyValueArtifact';
import ChartArtifact from './ChartArtifact';
import WeatherArtifact from './WeatherArtifact';
import DeviceControlArtifact, { type DeviceActionFn } from './DeviceControlArtifact';
import PresenceMapArtifact from './PresenceMapArtifact';

export interface ArtifactRendererProps {
  /** The raw artifact payload (from a WS frame or rehydrated metadata). */
  artifact: unknown;
  /** True while a `partial` artifact is still streaming → show the skeleton. */
  loading?: boolean;
  /**
   * True once the turn has finalized (done/error). A still-`partial` artifact
   * at that point resolves to the fallback, not a perpetual skeleton.
   */
  finalized?: boolean;
  /** Interactive device-control widget: toggle/run handler (Gen-UI). When
   *  absent the controls render disabled. */
  onDeviceAction?: DeviceActionFn;
}

/** Render any unknown value as an inert, escaped monospace code block. */
function FallbackBlock({ raw }: { raw: unknown }) {
  const { t } = useTranslation();
  let text: string;
  try {
    text = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2);
  } catch {
    text = String(raw);
  }
  return (
    <div className="text-sm">
      <p className="mb-1 text-xs italic text-gray-500 dark:text-gray-400">
        {t('chat.artifacts.fallbackNote')}
      </p>
      {/* React text child → escaped; <pre><code> never parses markup. */}
      <pre className="overflow-x-auto rounded-md bg-gray-100 dark:bg-gray-800 p-2 text-xs">
        <code className="font-mono text-gray-800 dark:text-gray-200">{text}</code>
      </pre>
    </div>
  );
}

function LoadingSkeleton() {
  const { t } = useTranslation();
  return (
    <div
      className="animate-pulse motion-reduce:animate-none space-y-2"
      aria-label={t('chat.artifacts.loading')}
    >
      <div className="h-3 w-1/3 rounded bg-gray-200 dark:bg-gray-700" />
      <div className="h-3 w-full rounded bg-gray-200 dark:bg-gray-700" />
      <div className="h-3 w-5/6 rounded bg-gray-200 dark:bg-gray-700" />
    </div>
  );
}

/** Dispatch a validated artifact to its typed sub-renderer. */
function ArtifactBody({ artifact, onDeviceAction }: { artifact: ChatArtifact; onDeviceAction?: DeviceActionFn }) {
  switch (artifact.kind) {
    case 'table':
      return <TableArtifact data={artifact.data} />;
    case 'list':
      return <ListArtifact data={artifact.data} />;
    case 'keyvalue':
      return <KeyValueArtifact data={artifact.data} />;
    case 'chart':
      return <ChartArtifact data={artifact.data} title={artifact.title} />;
    case 'weather':
      return <WeatherArtifact data={artifact.data} />;
    case 'device_control':
      return <DeviceControlArtifact data={artifact.data} onAction={onDeviceAction} />;
    case 'presence_map':
      return <PresenceMapArtifact data={artifact.data} />;
    default:
      // Unreachable — the zod discriminated union rejects unknown kinds before
      // we get here — but exhaustiveness keeps it honest.
      return null;
  }
}

interface BoundaryProps {
  fallback: ReactNode;
  children: ReactNode;
}
interface BoundaryState {
  hasError: boolean;
}

/**
 * Error boundary: a sub-renderer that throws on attacker-shaped data must NOT
 * crash the chat thread — it renders the fallback instead. Class component
 * because React error boundaries require lifecycle methods.
 */
class ArtifactErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { hasError: false };

  static getDerivedStateFromError(): BoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    // Keep it quiet but observable; never re-throw.
    console.warn('ArtifactRenderer sub-renderer threw — falling back:', error);
  }

  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}

/** Container chrome: bounded `.card`-family box + the quiet "generiert" affordance. */
function ArtifactShell({ title, children }: { title?: string; children: ReactNode }) {
  const { t } = useTranslation();
  const label = title
    ? t('chat.artifacts.regionLabel', { title })
    : t('chat.artifacts.regionLabelUntitled');
  return (
    // aria-live="off": the artifact arrives after the prose inside the thread's
    // role="log" aria-live="polite" region; we don't want a double-announce.
    <section
      className="mt-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-3"
      aria-label={label}
      aria-live="off"
    >
      {title && (
        <h3 className="mb-2 text-sm font-medium text-gray-800 dark:text-gray-200">{title}</h3>
      )}
      {children}
      {/* "generiert" affordance — DM Sans text + a subtle inline icon in the
          info/accent token. NOT an icon-in-a-colored-circle (AI-slop #3). */}
      <p className="mt-2 flex items-center gap-1 text-xs text-accent-700 dark:text-accent-300">
        <Sparkles className="h-3 w-3" aria-hidden="true" />
        <span>{t('chat.artifacts.generated')}</span>
      </p>
    </section>
  );
}

export default function ArtifactRenderer({ artifact, loading, finalized, onDeviceAction }: ArtifactRendererProps) {
  const parsed = parseArtifact(artifact);

  // Loading: a still-streaming partial that hasn't finalized → skeleton.
  if (loading && !finalized) {
    const title =
      parsed?.title ??
      (typeof (artifact as { title?: unknown })?.title === 'string'
        ? (artifact as { title: string }).title
        : undefined);
    return (
      <ArtifactShell title={title}>
        <LoadingSkeleton />
      </ArtifactShell>
    );
  }

  // Schema-invalid → fallback. Also: a partial that never finalized (turn is
  // done but the artifact still says partial) → fallback, not a stuck skeleton.
  const stuckPartial = !!parsed?.partial && !!finalized;
  if (!parsed || stuckPartial) {
    const title =
      typeof (artifact as { title?: unknown })?.title === 'string'
        ? (artifact as { title: string }).title
        : undefined;
    return (
      <ArtifactShell title={title}>
        <FallbackBlock raw={artifact} />
      </ArtifactShell>
    );
  }

  return (
    <ArtifactShell title={parsed.title}>
      <ArtifactErrorBoundary fallback={<FallbackBlock raw={artifact} />}>
        <ArtifactBody artifact={parsed} onDeviceAction={onDeviceAction} />
      </ArtifactErrorBoundary>
    </ArtifactShell>
  );
}
