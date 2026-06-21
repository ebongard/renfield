/**
 * WeatherArtifact — a typed `weather` widget (Gen-UI): current conditions + a
 * short daily forecast. The WMO `code` (int) maps to a lucide condition icon;
 * an unknown code falls back to a neutral cloud. All values are numbers/strings
 * → React text children (escaped); no HTML/SVG from the model.
 */
import { useTranslation } from 'react-i18next';
import {
  Sun, CloudSun, Cloud, CloudFog, CloudDrizzle, CloudRain, CloudSnow,
  CloudLightning, Droplets, Wind, type LucideIcon,
} from 'lucide-react';
import type { WeatherData } from './artifactSchema';

/** WMO weather code → condition icon (Open-Meteo coding). */
function iconForCode(code: number): LucideIcon {
  if (code === 0) return Sun;                                   // clear
  if (code <= 2) return CloudSun;                               // mainly clear / partly cloudy
  if (code === 3) return Cloud;                                 // overcast
  if (code === 45 || code === 48) return CloudFog;              // fog
  if (code >= 51 && code <= 57) return CloudDrizzle;            // drizzle
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return CloudRain;  // rain / showers
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return CloudSnow;  // snow
  if (code >= 95) return CloudLightning;                        // thunderstorm
  return Cloud;
}

function fmtTemp(n: number, unit: string): string {
  return `${Math.round(n)}${unit}`;
}

/** "YYYY-MM-DD" → a localized short weekday; falls back to the raw string. */
function weekday(date: string, lang: string): string {
  const d = new Date(`${date}T00:00:00`);
  if (Number.isNaN(d.getTime())) return date;
  return d.toLocaleDateString(lang, { weekday: 'short' });
}

export default function WeatherArtifact({ data }: { data: WeatherData }) {
  const { t, i18n } = useTranslation();
  const { current, forecast } = data;
  const CurrentIcon = iconForCode(current.code);
  const unit = current.unit;

  return (
    <div className="text-sm">
      {/* Current conditions */}
      <div className="flex items-center gap-3">
        <CurrentIcon
          className="h-10 w-10 shrink-0 text-accent-600 dark:text-accent-300"
          aria-hidden="true"
        />
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-semibold tabular-nums text-gray-900 dark:text-gray-100">
              {fmtTemp(current.temp, unit)}
            </span>
            <span className="truncate text-gray-600 dark:text-gray-300">{current.condition}</span>
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs tabular-nums text-gray-500 dark:text-gray-400">
            {current.high !== undefined && current.low !== undefined && (
              <span>{`↑ ${fmtTemp(current.high, unit)}  ↓ ${fmtTemp(current.low, unit)}`}</span>
            )}
            {current.feelsLike !== undefined && (
              <span>
                {t('chat.artifacts.weather.feelsLike', { temp: fmtTemp(current.feelsLike, unit) })}
              </span>
            )}
            {current.humidity !== undefined && (
              <span className="inline-flex items-center gap-1">
                <Droplets className="h-3 w-3" aria-hidden="true" />
                {`${Math.round(current.humidity)}%`}
              </span>
            )}
            {current.windSpeed !== undefined && (
              <span className="inline-flex items-center gap-1">
                <Wind className="h-3 w-3" aria-hidden="true" />
                {`${Math.round(current.windSpeed)} km/h`}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Daily forecast */}
      {forecast && forecast.length > 0 && (
        <div className="mt-3 flex gap-2 overflow-x-auto border-t border-gray-100 pt-3 dark:border-gray-700">
          {forecast.map((d, i) => {
            const DayIcon = iconForCode(d.code);
            return (
              <div key={i} className="flex min-w-[3.5rem] flex-col items-center gap-1 text-center">
                <span className="text-xs font-medium text-gray-600 dark:text-gray-400">
                  {weekday(d.date, i18n.language)}
                </span>
                <DayIcon
                  className="h-5 w-5 text-accent-600 dark:text-accent-300"
                  aria-label={d.condition || undefined}
                />
                <span className="text-xs tabular-nums text-gray-800 dark:text-gray-200">
                  {fmtTemp(d.high, unit)}
                </span>
                <span className="text-xs tabular-nums text-gray-400 dark:text-gray-500">
                  {fmtTemp(d.low, unit)}
                </span>
                {d.precipChance !== undefined && d.precipChance > 0 && (
                  <span className="text-[10px] tabular-nums text-blue-500 dark:text-blue-400">
                    {`${Math.round(d.precipChance)}%`}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
