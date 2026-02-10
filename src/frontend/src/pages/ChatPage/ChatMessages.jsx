import React, { useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Volume2, Loader, FileText, AlertCircle, CheckCircle } from 'lucide-react';
import IntentCorrectionButton from '../../components/IntentCorrectionButton';
import AttachmentQuickActions from './AttachmentQuickActions';
import EmailForwardDialog from './EmailForwardDialog';
import { useChatContext } from './context/ChatContext';

export default function ChatMessages() {
  const { t } = useTranslation();
  const {
    messages, loading, historyLoading, speakText, handleFeedbackSubmit,
    actionLoading, actionResult, indexToKb, sendToPaperless, handleSummarize,
    handleSendViaEmail, emailDialog, confirmSendViaEmail, cancelEmailDialog,
  } = useChatContext();
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

            {/* Attachment chips */}
            {message.attachments && message.attachments.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {message.attachments.map(att => (
                  <div
                    key={att.id}
                    className={`flex items-center space-x-1 px-2 py-1 rounded text-xs ${
                      att.status === 'completed'
                        ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                        : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
                    }`}
                  >
                    <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                    <span className="truncate max-w-[140px]">{att.filename}</span>
                    {att.file_size && (
                      <span className="text-[10px] opacity-70">
                        ({att.file_size < 1024 * 1024
                          ? `${Math.round(att.file_size / 1024)} KB`
                          : `${(att.file_size / (1024 * 1024)).toFixed(1)} MB`
                        })
                      </span>
                    )}
                    {att.status === 'completed'
                      ? <CheckCircle className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                      : <AlertCircle className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                    }
                    <AttachmentQuickActions
                      attachment={att}
                      onIndexToKb={indexToKb}
                      onSendToPaperless={sendToPaperless}
                      onSendViaEmail={handleSendViaEmail}
                      onSummarize={handleSummarize}
                      actionLoading={actionLoading}
                    />
                  </div>
                ))}
              </div>
            )}

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

      {/* Quick action result toast */}
      {actionResult && (
        <div
          className={`mx-auto px-3 py-1.5 rounded text-xs font-medium ${
            actionResult.success
              ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
              : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
          }`}
          role="status"
        >
          {actionResult.success
            ? (actionResult.type === 'indexing' ? t('chat.indexingSuccess')
              : actionResult.type === 'email' ? t('chat.emailSuccess')
              : t('chat.paperlessSuccess'))
            : (actionResult.type === 'indexing' ? t('chat.indexingFailed')
              : actionResult.type === 'email' ? t('chat.emailFailed')
              : t('chat.paperlessFailed'))
          }
        </div>
      )}

      {/* Email Forward Dialog */}
      {emailDialog && (
        <EmailForwardDialog
          open={!!emailDialog}
          filename={emailDialog.filename}
          onConfirm={confirmSendViaEmail}
          onCancel={cancelEmailDialog}
        />
      )}

      {/* Scroll anchor */}
      <div ref={messagesEndRef} />
    </div>
  );
}
