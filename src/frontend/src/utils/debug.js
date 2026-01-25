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

export const debug = {
  log: isDev ? console.log.bind(console) : () => {},
  warn: isDev ? console.warn.bind(console) : () => {},
  error: isDev ? console.error.bind(console) : () => {},
  info: isDev ? console.info.bind(console) : () => {},
  group: isDev ? console.group.bind(console) : () => {},
  groupEnd: isDev ? console.groupEnd.bind(console) : () => {},
  table: isDev ? console.table.bind(console) : () => {},
};

export default debug;
