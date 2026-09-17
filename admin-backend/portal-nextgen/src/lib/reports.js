import { getJSON, sendCsrfJSON } from './api.js';

export async function loadReportArchive(period = 'all', before = null, signal) {
  const query = new URLSearchParams({ period, limit: '12' });
  if (before != null) query.set('before', String(before));
  const data = await getJSON(`/portal/reports?${query}`, { signal });
  if (data?.ok !== true || !Array.isArray(data.editions)) throw new Error('Report archive unavailable');
  return data;
}

export async function loadReport(id, signal) {
  const data = await getJSON(`/portal/reports/${encodeURIComponent(id)}`, { signal });
  if (data?.ok !== true || data.edition?.schema !== 1 || data.edition.id !== id) throw new Error('Report unavailable');
  return data.edition;
}

export async function previewReport(period) {
  const data = await sendCsrfJSON('POST', '/portal/reports/admin/preview', { period });
  if (data?.preview !== true || data.edition?.schema !== 1) throw new Error('Preview unavailable');
  return data.edition;
}

export function reportDate(value) {
  return new Intl.DateTimeFormat('en-US', { month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
    .format(new Date(`${value}T12:00:00Z`));
}

export function reportTime(value) {
  if (!value) return 'Not published';
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    timeZone: 'America/New_York', timeZoneName: 'short' }).format(new Date(value));
}

export function reportWindow(window) {
  return `${reportTime(window.start_utc)} to ${reportTime(window.end_utc)}`;
}

export function metricValue(metric) {
  const value = metric?.value;
  if (!Number.isFinite(value)) return 'Not recorded';
  const prefix = metric.estimated ? '~' : '';
  if (metric.unit === 'h') return `${prefix}${value.toLocaleString('en-US', { maximumFractionDigits: 1 })} h`;
  if (metric.unit === 'Solari') return `${new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(value)}`;
  return `${prefix}${value.toLocaleString('en-US', { maximumFractionDigits: 1 })}`;
}

export function changeLabel(metric) {
  if (!Number.isFinite(metric?.change)) return 'No comparable earlier edition';
  if (metric.change === 0) return 'Unchanged from the previous edition';
  return `${metric.change > 0 ? '+' : ''}${metric.change.toLocaleString('en-US', { maximumFractionDigits: 1 })}${metric.unit === 'h' ? ' h' : ''} from the previous edition`;
}
