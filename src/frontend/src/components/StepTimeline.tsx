import { useTranslation } from 'react-i18next';

interface TrajectoryStep {
  type?: string;
  tool?: string;
  input?: unknown;
  output?: unknown;
  ok?: boolean;
  error?: string;
  [key: string]: unknown;
}

interface StepTimelineProps {
  payload: Record<string, unknown> | null;
}

/**
 * Render the step list from an AgentTrajectory.raw_payload as a vertical
 * timeline. Tolerant of payload-shape drift: when the producer hasn't
 * normalised a field yet, the cell falls back to a JSON dump so the
 * admin still sees the underlying data.
 */
export default function StepTimeline({ payload }: StepTimelineProps) {
  const { t } = useTranslation();
  if (!payload) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('selfLearning.trajectories.noPayload')}
      </p>
    );
  }

  const userMessage = typeof payload.user_message === 'string' ? payload.user_message : null;
  const finalAnswer = typeof payload.final_answer === 'string' ? payload.final_answer : null;
  const steps = Array.isArray(payload.steps) ? (payload.steps as TrajectoryStep[]) : [];

  return (
    <div className="space-y-4" data-testid="step-timeline">
      {userMessage && (
        <section className="rounded-md border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
            {t('selfLearning.trajectories.userMessage')}
          </h4>
          <p className="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{userMessage}</p>
        </section>
      )}

      <ol className="space-y-2">
        {steps.map((step, idx) => {
          const ok = step.ok !== false;
          return (
            <li
              key={idx}
              className={`rounded-md border p-3 ${
                ok
                  ? 'border-gray-200 dark:border-gray-700'
                  : 'border-rose-300 dark:border-rose-700 bg-rose-50/30 dark:bg-rose-900/10'
              }`}
              data-testid={`step-${idx}`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-mono text-gray-500 dark:text-gray-400">
                  #{idx + 1} · {step.type ?? 'step'}
                </span>
                {step.tool && (
                  <span className="text-xs font-mono text-gray-700 dark:text-gray-300">
                    {step.tool}
                  </span>
                )}
              </div>
              {step.input !== undefined && (
                <pre className="mt-1 text-xs bg-gray-100 dark:bg-gray-900 rounded p-2 overflow-x-auto max-h-32">
                  {JSON.stringify(step.input, null, 2)}
                </pre>
              )}
              {step.output !== undefined && (
                <pre className="mt-1 text-xs bg-gray-100 dark:bg-gray-900 rounded p-2 overflow-x-auto max-h-32">
                  {JSON.stringify(step.output, null, 2)}
                </pre>
              )}
              {step.error && (
                <p className="mt-1 text-xs text-rose-700 dark:text-rose-300">{step.error}</p>
              )}
            </li>
          );
        })}
      </ol>

      {finalAnswer && (
        <section className="rounded-md border border-emerald-300 dark:border-emerald-700 bg-emerald-50/30 dark:bg-emerald-900/10 p-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-emerald-700 dark:text-emerald-300 mb-1">
            {t('selfLearning.trajectories.finalAnswer')}
          </h4>
          <p className="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{finalAnswer}</p>
        </section>
      )}
    </div>
  );
}
