import React, { useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Volume2, Loader } from 'lucide-react';
import IntentCorrectionButton from '../../components/IntentCorrectionButton';
import { useChatContext } from './context/ChatContext';

export default function ChatMessages() {
  const { t } = useTranslation();
  const { messages, loading, historyLoading, speakText, handleFeedbackSubmit } = useChatContext();
  const messagesEndRef = useRef(null);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  return (
    <div
      className="flex-1 overflow-y-auto card space-y-4 mb-4 mx-4 md:mx-0"
      role="log"
      aria-live="polite"
      aria-label={t('chat.conversations')}
      aria-relevant="additions"
    >
      {/* History Loading State */}
      {historyLoading && (
        <div className="flex items-center justify-center py-8">
          <Loader className="w-6 h-6 text-gray-500 dark:text-gray-400 animate-spin mr-2" aria-hidden="true" />
          <span className="text-gray-500 dark:text-gray-400">{t('chat.loadingConversation')}</span>
        </div>
      )}

      {/* Empty State */}
      {!historyLoading && messages.length === 0 && (
        <div className="text-center py-12">
          <p className="text-gray-500 dark:text-gray-400 mb-4">{t('chat.startConversation')}</p>
          <p className="text-sm text-gray-400 dark:text-gray-500">
            {t('chat.useTextOrMic')}
          </p>
        </div>
      )}

      {/* Messages */}
      {messages.map((message, index) => (
        <div
          key={index}
          className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
          role="article"
          aria-label={message.role === 'user' ? t('chat.yourMessage') : t('chat.assistantResponse')}
        >
          <div
            className={`max-w-[70%] px-4 py-2 rounded-lg ${
              message.role === 'user'
                ? 'bg-primary-600 text-white'
                : 'bg-gray-200 text-gray-900 dark:bg-gray-700 dark:text-gray-100'
            }`}
          >
            <p className="whitespace-pre-wrap">{message.content}</p>

            {/* TTS Button for assistant messages */}
            {message.role === 'assistant' && !message.streaming && speakText && (
              <button
                onClick={() => speakText(message.content)}
                className="mt-2 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-white flex items-center space-x-1"
                aria-label={t('chat.readAloud')}
              >
                <Volume2 className="w-3 h-3" aria-hidden="true" />
                <span>{t('chat.readAloud')}</span>
              </button>
            )}

            {/* Intent info + Correction Button */}
            {message.role === 'assistant' && !message.streaming && message.intentInfo && (
              <IntentCorrectionButton
                messageText={message.userQuery || ''}
                detectedIntent={message.intentInfo.intent}
                detectedConfidence={message.intentInfo.confidence}
                feedbackType="intent"
                onCorrect={handleFeedbackSubmit}
                proactive={message.feedbackRequested === true}
              />
            )}
          </div>
        </div>
      ))}

      {/* Loading Indicator */}
      {loading && (
        <div className="flex justify-start" role="status" aria-label="Renfield denkt nach">
          <div className="bg-gray-200 dark:bg-gray-700 px-4 py-2 rounded-lg">
            <Loader className="w-5 h-5 animate-spin text-gray-500 dark:text-gray-400" aria-hidden="true" />
            <span className="sr-only">{t('chat.thinkingStatus')}</span>
          </div>
        </div>
      )}

      {/* Scroll anchor */}
      <div ref={messagesEndRef} />
    </div>
  );
}
