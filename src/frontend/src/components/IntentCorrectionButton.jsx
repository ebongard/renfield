import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, Check, ChevronDown } from 'lucide-react';
import axios from '../utils/axios';

/**
 * Button component for correcting wrong intent classifications.
 * Shows a dropdown with available intent options when clicked.
 * Dynamically fetches MCP tools from the API to include all available intents.
 *
 * @param {Object} props
 * @param {string} props.messageText - The original user message
 * @param {string} props.detectedIntent - The intent that was detected
 * @param {string} props.feedbackType - "intent", "agent_tool", or "complexity"
 * @param {Function} props.onCorrect - Callback(messageText, feedbackType, originalValue, correctedValue)
 * @param {boolean} props.proactive - Whether this was proactively requested by the backend
 */

// Module-level cache for MCP intent options
let _mcpOptionsCache = null;
let _mcpOptionsFetchPromise = null;

async function fetchMcpIntentOptions() {
  if (_mcpOptionsCache) return _mcpOptionsCache;
  if (_mcpOptionsFetchPromise) return _mcpOptionsFetchPromise;

  _mcpOptionsFetchPromise = axios.get('/api/intents/status')
    .then(res => {
      const mcpTools = res.data?.mcp_tools || [];
      // Group by server — use first tool per server as representative intent
      const serverMap = {};
      for (const tool of mcpTools) {
        const server = tool.server || 'unknown';
        if (!serverMap[server]) {
          serverMap[server] = {
            value: tool.intent,
            label: server.charAt(0).toUpperCase() + server.slice(1),
            server,
          };
        }
      }
      _mcpOptionsCache = Object.values(serverMap);
      return _mcpOptionsCache;
    })
    .catch(() => {
      _mcpOptionsFetchPromise = null;
      return [];
    });

  return _mcpOptionsFetchPromise;
}

export default function IntentCorrectionButton({
  messageText,
  detectedIntent,
  detectedConfidence,
  feedbackType = 'intent',
  onCorrect,
  proactive = false,
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(proactive);
  const [intentExpanded, setIntentExpanded] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [mcpOptions, setMcpOptions] = useState([]);

  useEffect(() => {
    if (feedbackType === 'intent') {
      fetchMcpIntentOptions().then(setMcpOptions);
    }
  }, [feedbackType]);

  // Core intent options (always available)
  const coreOptions = [
    { value: 'general.conversation', label: t('feedback.intentConversation') },
    { value: 'knowledge.ask', label: t('feedback.intentKnowledge') },
  ];

  // Build dynamic intent options: core + MCP tools
  const intentOptions = [
    ...coreOptions,
    ...mcpOptions.map(opt => ({
      value: opt.value,
      label: opt.label,
    })),
  ].filter(opt => opt.value !== detectedIntent);

  const complexityOptions = [
    { value: 'complex', label: t('feedback.shouldBeComplex') },
    { value: 'simple', label: t('feedback.shouldBeSimple') },
  ];

  const options = feedbackType === 'complexity' ? complexityOptions : intentOptions;

  const handleSelect = async (correctedValue) => {
    if (onCorrect) {
      await onCorrect(messageText, feedbackType, detectedIntent, correctedValue);
    }
    setSubmitted(true);
    setOpen(false);
  };

  if (submitted) {
    return (
      <div className="mt-1 flex items-center space-x-1 text-xs text-green-600 dark:text-green-400">
        <Check className="w-3 h-3" aria-hidden="true" />
        <span>{t('feedback.correctionSaved')}</span>
      </div>
    );
  }

  const pct = detectedConfidence != null ? Math.round(detectedConfidence * 100) : null;

  return (
    <div className="mt-1 relative">
      <div className="flex items-center gap-2 flex-wrap">
        {/* Intent info (collapsible) */}
        {detectedIntent && pct != null && (
          <button
            onClick={() => setIntentExpanded(!intentExpanded)}
            className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 flex items-center gap-1"
          >
            <span>{intentExpanded ? '▾' : '▸'}</span>
            {intentExpanded ? (
              <span><code>{detectedIntent}</code> · {pct}%</span>
            ) : (
              <span>{t('chat.intent')}</span>
            )}
          </button>
        )}

        {/* Proactive hint */}
        {proactive && !open && (
          <span className="text-xs text-amber-600 dark:text-amber-400">
            {t('feedback.proactiveQuestion')}
          </span>
        )}

        {/* Correction toggle */}
        {onCorrect && (
          <button
            onClick={() => setOpen(!open)}
            className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center space-x-1"
            aria-label={t('feedback.wrongIntent')}
          >
            <AlertCircle className="w-3 h-3" aria-hidden="true" />
            <span>{feedbackType === 'complexity' ? t('feedback.wrongComplexity') : t('feedback.wrongIntent')}</span>
            <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
          </button>
        )}
      </div>

      {open && (
        <div className="absolute bottom-full left-0 mb-1 z-10 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg p-2 min-w-48">
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-1 px-1">
            {feedbackType === 'complexity'
              ? t('feedback.selectComplexity')
              : t('feedback.selectCorrectIntent')}
          </p>
          {options.map(opt => (
            <button
              key={opt.value}
              onClick={() => handleSelect(opt.value)}
              className="block w-full text-left text-sm px-2 py-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300"
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
