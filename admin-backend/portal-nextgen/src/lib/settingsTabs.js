// The five Settings categories, in the order the list shows them. One route,
// `/settings?tab=<id>`, so a help entry or a backend redirect can point at a
// category without a new page.
export const SETTINGS_TABS = [
  { id: 'account', label: 'Account and sign-in', summary: 'Password, passkeys, Discord, sessions' },
  { id: 'characters', label: 'Characters', summary: 'Linked accounts, playing as, add a game account' },
  { id: 'display', label: 'Display', summary: 'Day or night, house colours, layout' },
  { id: 'privacy', label: 'Privacy and data', summary: 'Directory listing, your data' },
  { id: 'transfers', label: 'Transfers and activity', summary: 'Identity code, activity, transfer history' },
];

export const DEFAULT_TAB = 'account';

// Anchors from the single-page Settings. The backend still redirects profile
// sessions to #sign-in-security from the unlink, relink and + Alt endpoints,
// and all three are about game accounts, which now live under Characters.
const LEGACY_ANCHORS = {
  'sign-in-security': 'characters',
};

export function settingsTab(asked, hash = '') {
  const id = String(asked || '').toLowerCase();
  if (SETTINGS_TABS.some((t) => t.id === id)) return id;
  if (!asked) return LEGACY_ANCHORS[String(hash).replace(/^#/, '')] || DEFAULT_TAB;
  return DEFAULT_TAB;
}
