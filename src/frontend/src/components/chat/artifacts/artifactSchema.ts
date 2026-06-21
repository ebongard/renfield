/**
 * artifactSchema.ts — the AUTHORITATIVE shape validator for chat artifacts
 * (Lane A: table / list / keyvalue / chart).
 *
 * Per the locked design (`docs/design/chat-artifacts-sandbox.md` §8 decision 3)
 * validation is split by concern: the backend validates only the DoS gate
 * (kind allowlist + size/row/series/point caps); THIS zod schema is what the
 * renderer trusts for shape. A `safeParse` failure → the ArtifactRenderer
 * fallback (an escaped monospace code block), never a partial render of
 * attacker-shaped data. There is no shared codegen between the two — by design.
 *
 * Every artifact value is a plain string/number; there is no HTML/SVG string
 * anywhere, so injection is not applicable (React escapes every text child).
 * URL-valued cells are not rendered as links in v1 (text only); the renderer
 * additionally scheme-checks any value it ever turns into an href.
 */
import { z } from 'zod';

export const ARTIFACT_KINDS = ['table', 'list', 'keyvalue', 'chart', 'weather', 'device_control'] as const;
export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

// A finite number — rejects NaN / ±Infinity so a coordinate can never blow the
// SVG viewBox the ChartArtifact computes (matches the backend `_finite_number`).
const finiteNumber = z.number().finite();

export const tableDataSchema = z.object({
  columns: z.array(z.string()),
  rows: z.array(z.array(z.string())),
});

export const listDataSchema = z.object({
  ordered: z.boolean().optional(),
  items: z.array(z.string()),
});

export const keyValueDataSchema = z.object({
  pairs: z.array(z.object({ key: z.string(), value: z.string() })),
});

export const chartPointSchema = z.object({ x: finiteNumber, y: finiteNumber });
export const chartSeriesSchema = z.object({
  label: z.string(),
  points: z.array(chartPointSchema),
});
export const chartDataSchema = z.object({
  chartType: z.enum(['bar', 'line']),
  series: z.array(chartSeriesSchema),
});

// Weather widget (Gen-UI). `code` is a WMO weather code → condition icon.
export const weatherCurrentSchema = z.object({
  temp: finiteNumber,
  unit: z.string(),
  code: z.number(),
  condition: z.string(),
  feelsLike: finiteNumber.optional(),
  humidity: finiteNumber.optional(),
  windSpeed: finiteNumber.optional(),
  high: finiteNumber.optional(),
  low: finiteNumber.optional(),
});
export const weatherForecastSchema = z.object({
  date: z.string(),
  code: z.number(),
  high: finiteNumber,
  low: finiteNumber,
  condition: z.string().optional(),
  precipChance: finiteNumber.optional(),
});
export const weatherDataSchema = z.object({
  location: z.string(),
  current: weatherCurrentSchema,
  forecast: z.array(weatherForecastSchema).optional(),
});

// Interactive device-control widget (Gen-UI). A device whose domain isn't a
// controllable one is dropped backend-side; the renderer shows a toggle for
// light/switch and a run button for scene.
export const deviceControlDeviceSchema = z.object({
  entity_id: z.string(),
  domain: z.string(),
  name: z.string(),
  state: z.string(),
  room: z.string().optional(),
});
export const deviceControlDataSchema = z.object({
  devices: z.array(deviceControlDeviceSchema),
});

// Discriminated union on `kind` so a malformed `data` for the declared kind is
// a clean parse failure (→ fallback), not a wrong-renderer dispatch.
const baseFields = {
  id: z.string().min(1),
  title: z.string().optional(),
  partial: z.boolean().optional(),
};

export const artifactSchema = z.discriminatedUnion('kind', [
  z.object({ ...baseFields, kind: z.literal('table'), data: tableDataSchema }),
  z.object({ ...baseFields, kind: z.literal('list'), data: listDataSchema }),
  z.object({ ...baseFields, kind: z.literal('keyvalue'), data: keyValueDataSchema }),
  z.object({ ...baseFields, kind: z.literal('chart'), data: chartDataSchema }),
  z.object({ ...baseFields, kind: z.literal('weather'), data: weatherDataSchema }),
  z.object({ ...baseFields, kind: z.literal('device_control'), data: deviceControlDataSchema }),
]);

export type ChatArtifact = z.infer<typeof artifactSchema>;
export type TableData = z.infer<typeof tableDataSchema>;
export type ListData = z.infer<typeof listDataSchema>;
export type KeyValueData = z.infer<typeof keyValueDataSchema>;
export type ChartData = z.infer<typeof chartDataSchema>;
export type WeatherData = z.infer<typeof weatherDataSchema>;
export type DeviceControlData = z.infer<typeof deviceControlDataSchema>;
export type DeviceControlDevice = z.infer<typeof deviceControlDeviceSchema>;
export type ChartSeries = z.infer<typeof chartSeriesSchema>;

/** Parse an unknown payload into a typed artifact, or null on any shape failure. */
export function parseArtifact(raw: unknown): ChatArtifact | null {
  const result = artifactSchema.safeParse(raw);
  return result.success ? result.data : null;
}
