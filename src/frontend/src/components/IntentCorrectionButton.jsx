import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, Check, ChevronDown } from 'lucide-react';

/**
 * Button component for correcting wrong intent classifications.
 * Shows a dropdown with available intent options when clicked.
 *
 * @param {Object} props
 * @param {string} props.messageText - The original user message
 * @param {string} props.detectedIntent - The intent that was detected
 * @param {string} props.feedbackType - "intent", "agent_tool", or "complexity"
 * @param {Function} props.onCorrect - Callback(messageText, feedbackType, originalValue, correctedValue)
 * @param {boolean} props.proactive - Whether this was proactively requested by the backend
 */
export default function IntentCorrectionButton({
  messageText,
  detectedIntent,
  feedbackType = 'intent',
  onCorrect,
  proactive = false,
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(proactive);
  const [submitted, setSubmitted] = useState(false);

  const intentOptions = [
    { value: 'general.conversation', label: t('feedback.intentConversation') },
    { value: 'knowledge.ask', label: t('feedback.intentKnowledge') },
    { value: 'mcp.homeassistant', label: t('feedback.intentHomeAssistant') },
    { value: 'mcp.search.web', label: t('feedback.intentWebSearch') },
    { value: 'mcp.weather', label: t('feedback.intentWeather') },
    { value: 'mcp.news', label: t('feedback.intentNews') },
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

  return (
    <div className="mt-1 relative">
      {proactive && !open && (
        <span className="text-xs text-amber-600 dark:text-amber-400 mr-2">
          {t('feedback.proactiveQuestion')}
        </span>
      )}
      <button
        onClick={() => setOpen(!open)}
        className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center space-x-1"
        aria-label={t('feedback.wrongIntent')}
      >
        <AlertCircle className="w-3 h-3" aria-hidden="true" />
        <span>{feedbackType === 'complexity' ? t('feedback.wrongComplexity') : t('feedback.wrongIntent')}</span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
      </button>

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
