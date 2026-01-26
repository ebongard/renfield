/**
 * Debug Logger Utility
 *
 * Logs messages only in development mode.
 * In production builds, all debug calls are no-ops.
 *
 * Usage:
 *   import { debug } from '../utils/debug';
 *   debug.log('Message');
 *   debug.warn('Warning');
 *   debug.error('Error');
 */

const isDev = import.meta.env.DEV;

// No-op function for production
const noop = (): void => {};

interface DebugLogger {
  log: typeof console.log;
  warn: typeof console.warn;
  error: typeof console.error;
  info: typeof console.info;
  group: typeof console.group;
  groupEnd: typeof console.groupEnd;
  table: typeof console.table;
}

export const debug: DebugLogger = {
  log: isDev ? console.log.bind(console) : noop,
  warn: isDev ? console.warn.bind(console) : noop,
  error: isDev ? console.error.bind(console) : noop,
  info: isDev ? console.info.bind(console) : noop,
  group: isDev ? console.group.bind(console) : noop,
  groupEnd: isDev ? console.groupEnd.bind(console) : noop,
  table: isDev ? console.table.bind(console) : noop,
};

export default debug;
