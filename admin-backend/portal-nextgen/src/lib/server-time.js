export function parseTimeReading(data) {
  const instant = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
  if (typeof data?.server_now_utc !== 'string' || !instant.test(data.server_now_utc)) return null;
  const epochMs = Date.parse(data?.server_now_utc);
  const timeZone = data?.time_zone;
  if (data?.available !== true || !Number.isFinite(epochMs) || typeof timeZone !== 'string') return null;
  try { new Intl.DateTimeFormat('en-US', { timeZone }).format(epochMs); }
  catch { return null; }
  const reset = data.coriolis?.available === true && instant.test(data.coriolis.next_cycle_utc)
    ? Date.parse(data.coriolis.next_cycle_utc) : NaN;
  return { epochMs, timeZone, nextResetMs: Number.isFinite(reset) ? reset : null };
}

export function serverClockText(nowMs, timeZone) {
  if (!Number.isFinite(nowMs)) return 'Syncing time';
  return new Intl.DateTimeFormat('en-US', {
    timeZone, hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  }).format(nowMs);
}

export function resetDateText(resetMs, timeZone, compact = false) {
  if (!Number.isFinite(resetMs)) return 'Schedule unavailable';
  return new Intl.DateTimeFormat('en-US', {
    timeZone, ...(compact ? {} : { weekday: 'short', year: 'numeric' }),
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
  }).format(resetMs);
}

export function resetCountdown(resetMs, nowMs) {
  if (!Number.isFinite(resetMs) || !Number.isFinite(nowMs)) return '';
  const remaining = resetMs - nowMs;
  if (remaining <= 0) return 'Scheduled time reached';
  const minutes = Math.ceil(remaining / 60000);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  return `${days ? `${days}d ` : ''}${hours ? `${hours}h ` : ''}${mins}m remaining`;
}
