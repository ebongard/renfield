import { Fragment, ReactNode } from 'react';
import { ExternalLink } from 'lucide-react';

/**
 * Renders a subset of Microsoft Adaptive Card JSON used by Reva.
 * Supports: TextBlock, ColumnSet/Column, Container, FactSet/Fact,
 * Image, ActionSet, Action.OpenUrl, separator, spacing.
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

type AcElement =
  | AcTextBlock
  | AcColumnSet
  | AcColumn
  | AcContainer
  | AcFactSet
  | AcImage
  | AcActionSet;

export interface AdaptiveCardSchema {
  body?: AcElement[];
  actions?: AcAction[];
}

const STYLE_COLORS: Record<string, string> = {
  good: 'text-green-600 dark:text-green-400',
  warning: 'text-yellow-600 dark:text-yellow-400',
  attention: 'text-red-600 dark:text-red-400',
  accent: 'text-blue-600 dark:text-blue-400',
  default: 'text-gray-800 dark:text-gray-200',
};

const CONTAINER_STYLES: Record<string, string> = {
  emphasis: 'bg-gray-100 dark:bg-gray-700/50',
  good: 'bg-green-50 dark:bg-green-900/20',
  warning: 'bg-yellow-50 dark:bg-yellow-900/20',
  attention: 'bg-red-50 dark:bg-red-900/20',
  accent: 'bg-blue-50 dark:bg-blue-900/20',
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

/** Parse **bold** and _italic_ into React elements (no raw HTML injection). */
function renderFormattedText(text?: string): ReactNode {
  if (!text) return null;
  const parts: ReactNode[] = [];
  let remaining = String(text);
  let key = 0;

  while (remaining.length > 0) {
    const boldMatch = remaining.match(/\*\*(.+?)\*\*/);
    const italicMatch = remaining.match(/_(.+?)_/);

    const candidates = [boldMatch, italicMatch].filter(
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
    } else {
      parts.push(<em key={key++}>{nextMatch[1]}</em>);
    }

    remaining = remaining.substring(nextMatch.index + nextMatch[0].length);
  }

  return parts;
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

      return (
        <p
          key={key}
          className={`${size} ${weight} ${color} ${subtle} ${wrap} ${align} ${spacing} ${separator}`.trim()}
        >
          {renderFormattedText(tb.text)}
        </p>
      );
    }

    case 'ColumnSet': {
      const cs = element as AcColumnSet;
      const clickable = cs.selectAction?.type === 'Action.OpenUrl';
      const Wrapper = clickable ? 'a' : 'div';
      const wrapperProps = clickable && cs.selectAction
        ? { href: cs.selectAction.url, target: '_blank', rel: 'noopener noreferrer' }
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
      return (
        <img
          key={key}
          src={img.url}
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
              return (
                <a
                  key={`${key}-action-${i}`}
                  href={action.url}
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
