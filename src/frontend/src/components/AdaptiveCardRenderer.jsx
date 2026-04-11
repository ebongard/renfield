import React from 'react';
import { ExternalLink } from 'lucide-react';

/**
 * Renders a subset of Microsoft Adaptive Card JSON used by Reva.
 * Supports: TextBlock, ColumnSet/Column, Container, FactSet/Fact,
 * Image, ActionSet, Action.OpenUrl, separator, spacing.
 */

const STYLE_COLORS = {
  good: 'text-green-600 dark:text-green-400',
  warning: 'text-yellow-600 dark:text-yellow-400',
  attention: 'text-red-600 dark:text-red-400',
  accent: 'text-blue-600 dark:text-blue-400',
  default: 'text-gray-800 dark:text-gray-200',
};

const CONTAINER_STYLES = {
  emphasis: 'bg-gray-100 dark:bg-gray-700/50',
  good: 'bg-green-50 dark:bg-green-900/20',
  warning: 'bg-yellow-50 dark:bg-yellow-900/20',
  attention: 'bg-red-50 dark:bg-red-900/20',
  accent: 'bg-blue-50 dark:bg-blue-900/20',
};

const SIZE_CLASSES = {
  Small: 'text-xs',
  Default: 'text-sm',
  Medium: 'text-base',
  Large: 'text-lg',
  ExtraLarge: 'text-xl',
};

const SPACING = {
  None: '',
  Small: 'mt-1',
  Default: 'mt-2',
  Medium: 'mt-3',
  Large: 'mt-4',
  ExtraLarge: 'mt-6',
};

/** Parse **bold** and _italic_ into React elements (no dangerouslySetInnerHTML). */
function renderFormattedText(text) {
  if (!text) return null;
  const parts = [];
  let remaining = String(text);
  let key = 0;

  while (remaining.length > 0) {
    const boldMatch = remaining.match(/\*\*(.+?)\*\*/);
    const italicMatch = remaining.match(/_(.+?)_/);

    const nextMatch = [boldMatch, italicMatch]
      .filter(Boolean)
      .sort((a, b) => a.index - b.index)[0];

    if (!nextMatch) {
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

function renderElement(element, index = 0) {
  if (!element || !element.type) return null;

  const key = `ac-${index}`;
  const spacing = element.spacing ? SPACING[element.spacing] || '' : '';
  const separator = element.separator ? 'border-t border-gray-200 dark:border-gray-600 pt-1' : '';

  switch (element.type) {
    case 'TextBlock': {
      const size = SIZE_CLASSES[element.size] || SIZE_CLASSES.Default;
      const weight = element.weight === 'Bolder' ? 'font-semibold' : '';
      const color = STYLE_COLORS[element.color] || STYLE_COLORS.default;
      const subtle = element.isSubtle ? 'opacity-60' : '';
      const wrap = element.wrap !== false ? '' : 'truncate';
      const align = element.horizontalAlignment === 'Center' ? 'text-center'
        : element.horizontalAlignment === 'Right' ? 'text-right' : '';

      return (
        <p
          key={key}
          className={`${size} ${weight} ${color} ${subtle} ${wrap} ${align} ${spacing} ${separator}`.trim()}
        >
          {renderFormattedText(element.text)}
        </p>
      );
    }

    case 'ColumnSet': {
      const clickable = element.selectAction?.type === 'Action.OpenUrl';
      const Wrapper = clickable ? 'a' : 'div';
      const wrapperProps = clickable
        ? { href: element.selectAction.url, target: '_blank', rel: 'noopener noreferrer' }
        : {};

      return (
        <Wrapper
          key={key}
          {...wrapperProps}
          className={`flex items-start gap-2 ${spacing} ${separator} ${clickable ? 'hover:bg-gray-50 dark:hover:bg-gray-700/30 rounded cursor-pointer' : ''}`.trim()}
        >
          {(element.columns || []).map((col, i) => renderElement({ ...col, type: 'Column' }, `${index}-col-${i}`))}
        </Wrapper>
      );
    }

    case 'Column': {
      const width = element.width === 'stretch' ? 'flex-1 min-w-0'
        : element.width === 'auto' ? 'flex-shrink-0'
        : 'flex-shrink-0';
      const style = element.width && String(element.width).match(/^\d+px$/)
        ? { width: element.width } : {};

      return (
        <div key={key} className={width} style={style}>
          {(element.items || []).map((item, i) => renderElement(item, `${index}-item-${i}`))}
        </div>
      );
    }

    case 'Container': {
      const bg = CONTAINER_STYLES[element.style] || '';
      const bleed = element.bleed ? '-mx-3 px-3 py-2' : 'py-1';

      return (
        <div key={key} className={`${bg} ${bleed} ${spacing} ${separator} rounded`}>
          {(element.items || []).map((item, i) => renderElement(item, `${index}-citem-${i}`))}
        </div>
      );
    }

    case 'FactSet': {
      return (
        <div key={key} className={`grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm ${spacing} ${separator}`}>
          {(element.facts || []).map((fact, i) => (
            <React.Fragment key={`${key}-fact-${i}`}>
              <span className="font-medium text-gray-600 dark:text-gray-400">{fact.title}</span>
              <span className="text-gray-800 dark:text-gray-200">{fact.value}</span>
            </React.Fragment>
          ))}
        </div>
      );
    }

    case 'Image': {
      const sizeMap = { Small: 'h-8', Medium: 'h-16', Large: 'h-24', Auto: '' };
      const imgSize = sizeMap[element.size] || sizeMap.Medium;
      return (
        <img
          key={key}
          src={element.url}
          alt={element.altText || ''}
          className={`${imgSize} ${spacing} rounded`}
        />
      );
    }

    case 'ActionSet': {
      return (
        <div key={key} className={`flex flex-wrap gap-2 ${spacing} ${separator}`}>
          {(element.actions || []).map((action, i) => {
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

export default function AdaptiveCardRenderer({ card }) {
  if (!card) return null;

  const body = card.body || [];

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-600
      bg-white dark:bg-gray-800 p-3 overflow-x-auto text-sm">
      {body.map((element, i) => renderElement(element, i))}
      {card.actions && renderElement({ type: 'ActionSet', actions: card.actions, spacing: 'Medium' }, 'actions')}
    </div>
  );
}
