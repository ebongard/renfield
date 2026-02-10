import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Mail } from 'lucide-react';
import Modal from '../../components/Modal';

export default function EmailForwardDialog({ open, filename, onConfirm, onCancel }) {
  const { t } = useTranslation();
  const [to, setTo] = useState('');
  const [subject, setSubject] = useState(`Document: ${filename || ''}`);
  const [body, setBody] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!to.trim()) return;
    onConfirm(to.trim(), subject.trim(), body.trim());
    setTo('');
    setSubject('');
    setBody('');
  };

  return (
    <Modal isOpen={open} onClose={onCancel}>
      <form onSubmit={handleSubmit}>
        <div className="flex items-center gap-2 mb-4">
          <div className="w-10 h-10 rounded-full bg-primary-100 dark:bg-primary-900/30 flex items-center justify-center">
            <Mail className="w-5 h-5 text-primary-600 dark:text-primary-400" aria-hidden="true" />
          </div>
          <h2 className="text-lg font-bold text-gray-900 dark:text-white">
            {t('chat.emailDialogTitle')}
          </h2>
        </div>

        <div className="space-y-3 mb-4">
          <div>
            <label htmlFor="email-to" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('chat.emailTo')} *
            </label>
            <input
              id="email-to"
              type="email"
              required
              value={to}
              onChange={(e) => setTo(e.target.value)}
              className="input w-full"
              placeholder="user@example.com"
              autoFocus
            />
          </div>

          <div>
            <label htmlFor="email-subject" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('chat.emailSubject')}
            </label>
            <input
              id="email-subject"
              type="text"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              className="input w-full"
            />
          </div>

          <div>
            <label htmlFor="email-body" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('chat.emailBody')}
            </label>
            <textarea
              id="email-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              className="input w-full h-20 resize-none"
              rows={3}
            />
          </div>
        </div>

        <div className="flex space-x-3">
          <button
            type="button"
            onClick={onCancel}
            className="flex-1 btn btn-secondary"
          >
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            disabled={!to.trim()}
            className="flex-1 btn btn-primary disabled:opacity-50"
          >
            {t('chat.emailSend')}
          </button>
        </div>
      </form>
    </Modal>
  );
}
