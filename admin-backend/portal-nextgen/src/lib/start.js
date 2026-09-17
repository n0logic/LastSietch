export function welcomeReading(payload) {
  if (payload?.available !== true || !Array.isArray(payload.packages)) {
    return { state: 'unknown', label: 'Delivery status unavailable', detail: 'We could not read your package history. This does not mean nothing was sent.' };
  }
  const pack = payload.packages.find((entry) => entry?.kind === 'welcome');
  if (!pack) {
    return { state: 'empty', label: 'No welcome package recorded yet', detail: 'If you have already joined the game, check Mailbox or ask for help. This guide does not issue or claim a package.' };
  }
  if (pack.state === 'delivered') {
    return { state: 'recorded', label: 'Welcome package recorded as delivered', detail: 'Mailbox shows the recorded destination and status of each part.' };
  }
  return { state: 'pending', label: 'Welcome package recorded', detail: 'Some parts are waiting or have not been confirmed. Open Mailbox to see each delivery and any collection instructions.' };
}
