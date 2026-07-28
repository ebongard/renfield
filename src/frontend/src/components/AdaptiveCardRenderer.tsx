import { Fragment, ReactNode } from 'react';
import { ExternalLink } from 'lucide-react';

import { CitationChip } from './wissensbasis/CitationChip';

/**
 * Renders a subset of Microsoft Adaptive Card JSON used by Reva.
 * Supports: TextBlock (with bold/italic/<cite> parsing), ColumnSet/Column,
 * Container, FactSet/Fact, Image, ActionSet, Action.OpenUrl, separator,
 * spacing, CitationChip.
 */

type AcSize = 'Small' | 'Default' | 'Medium' | 'Large' | 'ExtraLarge' | 'Auto';
type AcSpacing = 'None' | 'Small' | 'Default' | 'Medium' | 'Large' | 'ExtraLarge';
type AcStyle = 'good' | 'warning' | 'attention' | 'accent' | 'default' | 'emphasis';
type AcAlignment = 'Left' | 'Center' | 'Right';

interface AcBaseElement {
  spacing?: AcSpacing;
  separator?: boolean;
}

interface AcTextBlock extends AcBaseElement {
  type: 'TextBlock';
  text?: string;
  size?: AcSize;
  weight?: 'Lighter' | 'Default' | 'Bolder';
  color?: AcStyle;
  isSubtle?: boolean;
  wrap?: boolean;
  horizontalAlignment?: AcAlignment;
}

interface AcOpenUrlAction {
  type: 'Action.OpenUrl';
  title: string;
  url: string;
}

type AcAction = AcOpenUrlAction;

interface AcColumnSet extends AcBaseElement {
  type: 'ColumnSet';
  columns?: AcColumn[];
  selectAction?: AcOpenUrlAction;
}

interface AcColumn extends AcBaseElement {
  type?: 'Column';
  width?: 'stretch' | 'auto' | string;
  items?: AcElement[];
}

interface AcContainer extends AcBaseElement {
  type: 'Container';
  style?: AcStyle;
  bleed?: boolean;
  items?: AcElement[];
}

interface AcFact {
  title: string;
  value: string;
}

interface AcFactSet extends AcBaseElement {
  type: 'FactSet';
  facts?: AcFact[];
}

interface AcImage extends AcBaseElement {
  type: 'Image';
  url: string;
  altText?: string;
  size?: AcSize;
}

interface AcActionSet extends AcBaseElement {
  type: 'ActionSet';
  actions?: AcAction[];
}

interface AcCitationChip extends AcBaseElement {
  type: 'CitationChip';
  /** Canonical KGEntity.atom_id (UUID-shaped string) — backend-validated. */
  entity: string;
  label: string;
  entity_type?: string;
  /** True when backend could not resolve the entity. Renders disabled. */
  missing?: boolean;
}

type AcElement =
  | AcTextBlock
  | AcColumnSet
  | AcColumn
  | AcContainer
  | AcFactSet
  | AcImage
  | AcActionSet
  | AcCitationChip;

export interface AdaptiveCardSchema {
  body?: AcElement[];
  actions?: AcAction[];
}

const STYLE_COLORS: Record<string, string> = {
  good: 'text-green-600 dark:text-green-400',
  warning: 'text-yellow-600 dark:text-yellow-400',
  attention: 'text-red-600 dark:text-red-400',
  // DESIGN.md: accent is the turquoise axis. No blue in the palette.
  accent: 'text-accent-600 dark:text-accent-400',
  default: 'text-gray-800 dark:text-gray-200',
};

const CONTAINER_STYLES: Record<string, string> = {
  emphasis: 'bg-gray-100 dark:bg-gray-700/50',
  good: 'bg-green-50 dark:bg-green-900/20',
  warning: 'bg-yellow-50 dark:bg-yellow-900/20',
  attention: 'bg-red-50 dark:bg-red-900/20',
  accent: 'bg-accent-50 dark:bg-accent-900/20',
};

const SIZE_CLASSES: Record<string, string> = {
  Small: 'text-xs',
  Default: 'text-sm',
  Medium: 'text-base',
  Large: 'text-lg',
  ExtraLarge: 'text-xl',
};

const SPACING: Record<string, string> = {
  None: '',
  Small: 'mt-1',
  Default: 'mt-2',
  Medium: 'mt-3',
  Large: 'mt-4',
  ExtraLarge: 'mt-6',
};

/**
 * Server-side validator mirror — keep in sync with Reva's
 * _ALLOWED_ENTITY_RE in src/reva/wissensbasis/citation_chip.py.
 * Accepts alphanumeric + dash + underscore + slash (the slash supports
 * Digital.ai Release path-style IDs like Applications/Folder.../Release...).
 * Anything else (script tags, javascript:, smuggled HTML) and any ".."
 * substring (path traversal) renders as a missing chip.
 */
const CITE_ENTITY_RE = /^[A-Za-z0-9_\-/]{1,256}$/;
const isValidEntity = (v: string) => !!v && !v.includes('..') && CITE_ENTITY_RE.test(v);

/**
 * URL scheme allowlist for AdaptiveCard Action.OpenUrl / Image (review M3).
 * React does NOT sanitize URL schemes in href/src, so a `javascript:` (or
 * `data:`) URL from a card producer (incl. a federated Reva backend) would be a
 * clickable XSS in the Renfield origin. Only http(s) and protocol-relative/
 * same-origin relative URLs are allowed; anything else returns null so the
 * caller renders inert text instead of a live link/image. Mirrors the
 * fail-closed posture of the citation-chip path.
 */
const safeUrl = (v: string | undefined | null): string | null => {
  if (!v || typeof v !== 'string') return null;
  const s = v.trim();
  // Relative / same-origin (no scheme, not protocol-relative "//evil").
  if (/^\/(?!\/)/.test(s) || /^[.?#]/.test(s)) return s;
  try {
    const u = new URL(s, window.location.origin);
    return u.protocol === 'http:' || u.protocol === 'https:' ? s : null;
  } catch {
    return null;
  }
};

/**
 * Parse markdown bold/italic and inline citation tags into React elements.
 * No raw HTML injection — every dynamic value flows through React's
 * escape boundary.
 */
function renderFormattedText(text?: string): ReactNode {
  if (!text) return null;
  // Local non-global regex per-call. The pattern is re-used inside the
  // loop with `.match()` (not `.exec()`), so `g` is unnecessary and the
  // module-level `lastIndex` footgun is sidestepped entirely.
  const citeTagRe = /<cite\s+entity="([^"]+)"(?:\s+type="([^"]+)")?\s*>([^<]*)<\/cite>/i;

  const parts: ReactNode[] = [];
  let remaining = String(text);
  let key = 0;

  while (remaining.length > 0) {
    const boldMatch = remaining.match(/\*\*(.+?)\*\*/);
    const italicMatch = remaining.match(/_(.+?)_/);
    const citeMatch = remaining.match(citeTagRe);

    const candidates = [boldMatch, italicMatch, citeMatch].filter(
      (m): m is RegExpMatchArray => m !== null && m.index !== undefined,
    );
    const nextMatch = candidates.sort((a, b) => (a.index ?? 0) - (b.index ?? 0))[0];

    if (!nextMatch || nextMatch.index === undefined) {
      parts.push(remaining);
      break;
    }

    if (nextMatch.index > 0) {
      parts.push(remaining.substring(0, nextMatch.index));
    }

    if (nextMatch === boldMatch) {
      parts.push(<strong key={key++}>{nextMatch[1]}</strong>);
    } else if (nextMatch === italicMatch) {
      parts.push(<em key={key++}>{nextMatch[1]}</em>);
    } else {
      const [, entity, type, label] = nextMatch;
      const validEntity = isValidEntity(entity);
      parts.push(
        <CitationChip
          key={key++}
          entity={validEntity ? entity : ''}
          label={label}
          entityType={type}
          missing={!validEntity}
        />,
      );
    }

    remaining = remaining.substring(nextMatch.index + nextMatch[0].length);
  }

  return parts;
}

/**
 * Render a TextBlock's text as block-level markdown → real React elements.
 *
 * The synthesizer (and any multi-line tool result) emits markdown with
 * `### headings`, `-`/`*` bullet lists, and blank-line paragraph breaks.
 * `renderFormattedText` only parses INLINE marks (bold/italic/<cite>) and a
 * plain `<p>` collapses the source newlines, so that content used to render
 * as a run-on wall. This splits on newlines and emits headings, `<ul>` lists,
 * and paragraphs, delegating each line's inline marks to renderFormattedText.
 * No raw HTML — every node is a React element through the escape boundary,
 * same security model as the inline path.
 */
function renderMarkdownBlocks(text: string, keyBase: string): ReactNode {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let bullets: ReactNode[] = [];
  let k = 0;

  const flushBullets = () => {
    if (bullets.length > 0) {
      blocks.push(
        <ul key={`${keyBase}-ul-${k++}`} className="list-disc pl-5 space-y-0.5">
          {bullets}
        </ul>,
      );
      bullets = [];
    }
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    // Bullet: leading `-` or `*` followed by whitespace (single marker, so
    // `**bold**` at line start is NOT mistaken for a bullet).
    const bulletMatch = line.match(/^\s*[-*]\s+(.*)$/);
    if (bulletMatch) {
      bullets.push(
        <li key={`${keyBase}-li-${k++}`}>{renderFormattedText(bulletMatch[1])}</li>,
      );
      continue;
    }
    flushBullets();

    const headingMatch = line.match(/^\s*#{1,6}\s+(.*)$/);
    if (headingMatch) {
      blocks.push(
        <p key={`${keyBase}-h-${k++}`} className="font-semibold mt-2 first:mt-0">
          {renderFormattedText(headingMatch[1])}
        </p>,
      );
    } else if (line.trim() === '') {
      // Blank line → small paragraph gap.
      blocks.push(<div key={`${keyBase}-sp-${k++}`} className="h-2" aria-hidden="true" />);
    } else {
      blocks.push(<p key={`${keyBase}-p-${k++}`}>{renderFormattedText(line)}</p>);
    }
  }
  flushBullets();
  return blocks;
}

/**
 * A TextBlock needs block rendering when it carries newlines or opens with a
 * heading / bullet marker. Single-line titles and labels stay on the cheap
 * inline `<p>` path (backward-compatible).
 */
function isBlockText(text: string): boolean {
  return /\n/.test(text) || /^\s*(#{1,6}\s|[-*]\s)/.test(text);
}

function renderElement(element: AcElement | undefined, index: number | string = 0): ReactNode {
  if (!element) return null;
  // The recursive call from ColumnSet sets `type: 'Column'` on bare column
  // objects, so by the time we reach the switch every element has a type.

  const key = `ac-${index}`;
  const spacing = element.spacing ? (SPACING[element.spacing] ?? '') : '';
  const separator = element.separator ? 'border-t border-gray-200 dark:border-gray-600 pt-1' : '';

  switch (element.type) {
    case 'TextBlock': {
      const tb = element as AcTextBlock;
      const size = (tb.size && SIZE_CLASSES[tb.size]) || SIZE_CLASSES.Default;
      const weight = tb.weight === 'Bolder' ? 'font-semibold' : '';
      const color = (tb.color && STYLE_COLORS[tb.color]) || STYLE_COLORS.default;
      const subtle = tb.isSubtle ? 'opacity-60' : '';
      const wrap = tb.wrap !== false ? '' : 'truncate';
      const align = tb.horizontalAlignment === 'Center' ? 'text-center'
        : tb.horizontalAlignment === 'Right' ? 'text-right' : '';

      const tbClassName = `${size} ${weight} ${color} ${subtle} ${wrap} ${align} ${spacing} ${separator}`.trim();
      const tbText = tb.text ?? '';
      // Multi-line / block markdown → real heading/list/paragraph elements so
      // newlines and bullets survive. Single-line text keeps the inline <p>.
      if (isBlockText(tbText)) {
        return (
          <div key={key} className={tbClassName}>
            {renderMarkdownBlocks(tbText, key)}
          </div>
        );
      }
      return (
        <p key={key} className={tbClassName}>
          {renderFormattedText(tb.text)}
        </p>
      );
    }

    case 'ColumnSet': {
      const cs = element as AcColumnSet;
      // M3: only clickable when the OpenUrl target passes the scheme allowlist.
      const csHref = cs.selectAction?.type === 'Action.OpenUrl'
        ? safeUrl(cs.selectAction.url)
        : null;
      const clickable = csHref !== null;
      const Wrapper = clickable ? 'a' : 'div';
      const wrapperProps = csHref
        ? { href: csHref, target: '_blank', rel: 'noopener noreferrer' }
        : {};

      return (
        <Wrapper
          key={key}
          {...wrapperProps}
          className={`flex items-start gap-2 ${spacing} ${separator} ${clickable ? 'hover:bg-gray-50 dark:hover:bg-gray-700/30 rounded cursor-pointer' : ''}`.trim()}
        >
          {(cs.columns ?? []).map((col, i) => renderElement({ ...col, type: 'Column' }, `${index}-col-${i}`))}
        </Wrapper>
      );
    }

    case 'Column': {
      const c = element as AcColumn;
      const width = c.width === 'stretch' ? 'flex-1 min-w-0'
        : c.width === 'auto' ? 'flex-shrink-0'
        : 'flex-shrink-0';
      const style = c.width && String(c.width).match(/^\d+px$/)
        ? { width: c.width } : {};

      return (
        <div key={key} className={width} style={style}>
          {(c.items ?? []).map((item, i) => renderElement(item, `${index}-item-${i}`))}
        </div>
      );
    }

    case 'Container': {
      const cn = element as AcContainer;
      const bg = (cn.style && CONTAINER_STYLES[cn.style]) ?? '';
      const bleed = cn.bleed ? '-mx-3 px-3 py-2' : 'py-1';

      return (
        <div key={key} className={`${bg} ${bleed} ${spacing} ${separator} rounded`}>
          {(cn.items ?? []).map((item, i) => renderElement(item, `${index}-citem-${i}`))}
        </div>
      );
    }

    case 'FactSet': {
      const fs = element as AcFactSet;
      return (
        <div key={key} className={`grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm ${spacing} ${separator}`}>
          {(fs.facts ?? []).map((fact, i) => (
            <Fragment key={`${key}-fact-${i}`}>
              <span className="font-medium text-gray-600 dark:text-gray-400">{fact.title}</span>
              <span className="text-gray-800 dark:text-gray-200">{fact.value}</span>
            </Fragment>
          ))}
        </div>
      );
    }

    case 'Image': {
      const img = element as AcImage;
      const sizeMap: Record<string, string> = { Small: 'h-8', Medium: 'h-16', Large: 'h-24', Auto: '' };
      const imgSize = (img.size && sizeMap[img.size]) || sizeMap.Medium;
      // M3: drop images whose src isn't an allowed scheme (no data:/javascript:).
      const imgSrc = safeUrl(img.url);
      if (!imgSrc) return null;
      return (
        <img
          key={key}
          src={imgSrc}
          alt={img.altText || ''}
          className={`${imgSize} ${spacing} rounded`}
        />
      );
    }

    case 'ActionSet': {
      const as = element as AcActionSet;
      return (
        <div key={key} className={`flex flex-wrap gap-2 ${spacing} ${separator}`}>
          {(as.actions ?? []).map((action, i) => {
            if (action.type === 'Action.OpenUrl') {
              // M3: only render a live link for an allowed URL scheme; otherwise
              // show the title as inert text (no javascript:/data: href).
              const actionHref = safeUrl(action.url);
              if (!actionHref) {
                return (
                  <span
                    key={`${key}-action-${i}`}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium
                      bg-gray-300 text-gray-700 rounded dark:bg-gray-600 dark:text-gray-200"
                  >
                    {action.title}
                  </span>
                );
              }
              return (
                <a
                  key={`${key}-action-${i}`}
                  href={actionHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium
                    bg-blue-600 text-white rounded hover:bg-blue-700
                    dark:bg-blue-500 dark:hover:bg-blue-600 transition-colors"
                >
                  <ExternalLink className="w-3 h-3" />
                  {action.title}
                </a>
              );
            }
            return null;
          })}
        </div>
      );
    }

    case 'CitationChip': {
      // Standalone CitationChip element — used when card builders construct
      // chips programmatically (e.g. from a structured trace) rather than
      // emitting <cite> tags inline in TextBlock prose.
      //
      // Defense-in-depth: validate the entity attribute against the same
      // regex the inline parse path uses. The backend already validates
      // before emitting, but a malformed/smuggled value here would still
      // reach setSearchParams and a backend `?entity_id=` query. Marking
      // it missing keeps the chip visible (so the agent's intent is
      // preserved) while blocking the click.
      const cc = element as AcCitationChip;
      const validEntity = !cc.missing && isValidEntity(cc.entity ?? '');
      return (
        <CitationChip
          key={key}
          entity={validEntity ? cc.entity : ''}
          label={cc.label}
          entityType={cc.entity_type}
          missing={!validEntity}
        />
      );
    }

    default:
      return null;
  }
}

interface AdaptiveCardRendererProps {
  card: AdaptiveCardSchema | null | undefined;
}

export default function AdaptiveCardRenderer({ card }: AdaptiveCardRendererProps) {
  if (!card) return null;

  const body = card.body ?? [];

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-600
      bg-white dark:bg-gray-800 p-3 overflow-x-auto text-sm">
      {body.map((element, i) => renderElement(element, i))}
      {card.actions && renderElement({ type: 'ActionSet', actions: card.actions, spacing: 'Medium' }, 'actions')}
    </div>
  );
}
