const MESSAGES = {
  sign_in_failed: 'That sign-in did not work. Check your saved method and try again.',
  passkey_failed: 'The passkey could not be verified. Try again with a passkey saved for this profile.',
  password_length: 'Use 8 to 128 characters. Spaces, Unicode and password-manager paste are welcome.',
  password_common: 'That password is too common. Choose a longer, less predictable passphrase.',
  username_format: 'Use 3 to 32 letters, numbers, dots, underscores or hyphens. Start with a letter or number.',
  username_unavailable: 'That username is already in use. Choose another.',
  username_required: 'Save a username before adding a passkey.',
  account_already_linked: 'That game account is already linked to this profile.',
  additional_accounts_unavailable: 'Linking additional game accounts is currently unavailable.',
  reauthenticate: 'Sign in again with a saved method before changing security settings.',
  sign_in_required: 'Sign in to continue.',
  last_sign_in_method: 'Keep at least one sign-in method. Add a password, passkey or Discord first.',
  provider_in_use: 'That Discord account belongs to another profile. Accounts cannot be merged here.',
  discord_already_linked: 'This profile already has a different Discord account linked.',
  discord_failed: 'Discord sign-in did not finish. Please start again.',
  challenge_expired: 'This verification expired or was already used. Start again in this browser.',
  verification_failed: 'That code did not match. Check the Cielago whisper and try again.',
  existing_account_sign_in: 'This game account already belongs to a portal profile. Use its saved sign-in method or ask an admin for recovery help.',
  game_verification_unavailable: 'Private game verification is unavailable right now. Try again later or use Discord.',
  character_unavailable: 'We could not identify one online character with that exact name. Check the name and stay in game.',
  rate_limited: 'Too many attempts. Wait a few minutes before trying again.',
  busy: 'Sign-in is busy. Wait a moment and try again.',
  csrf: 'Your browser session changed. Refresh this page and try again.',
  passkey_limit: 'You can save up to eight passkeys. Remove an old one first.',
  auth_unavailable: 'Sign-in settings are temporarily unavailable. Please try again later.',
};

export function authMessage(error) {
  if (error?.name === 'NotAllowedError') return 'The passkey prompt was cancelled or timed out. You can try again.';
  return MESSAGES[typeof error === 'string' ? error : error?.message] || 'That action did not finish. Refresh and try again.';
}

export async function authStatus() {
  const response = await fetch('/portal/auth/status', { credentials: 'same-origin', cache: 'no-store' });
  if (response.status === 404) return { ok: true, enabled: false, game_enabled: false };
  const data = await response.json();
  if (!response.ok || data.ok !== true) throw new Error(data.error || 'auth_unavailable');
  return data;
}

export async function authPost(action, body = {}) {
  const status = await authStatus();
  if (!status.enabled) throw new Error('auth_unavailable');
  const response = await fetch('/portal/auth/' + action, {
    method: 'POST', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-Portal-Auth-CSRF': status.csrf },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.ok !== true) throw new Error(data.error || 'auth_unavailable');
  return data;
}

function decode(value) {
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4)), c => c.charCodeAt(0));
}

function encode(value) {
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export async function usePasskey(optionsAction, finishAction, body = {}) {
  if (!globalThis.PublicKeyCredential || !navigator.credentials) throw new Error('passkey_failed');
  const result = await authPost(optionsAction, body);
  const publicKey = result.options;
  publicKey.challenge = decode(publicKey.challenge);
  const registering = !!publicKey.user;
  if (registering) publicKey.user.id = decode(publicKey.user.id);
  for (const name of ['excludeCredentials', 'allowCredentials']) {
    if (publicKey[name]) publicKey[name] = publicKey[name].map(c => ({ ...c, id: decode(c.id) }));
  }
  const credential = registering
    ? await navigator.credentials.create({ publicKey })
    : await navigator.credentials.get({ publicKey });
  if (!credential) throw new Error('passkey_failed');
  const response = { clientDataJSON: encode(credential.response.clientDataJSON) };
  if (registering) {
    response.attestationObject = encode(credential.response.attestationObject);
    response.transports = credential.response.getTransports?.() || [];
  } else {
    response.authenticatorData = encode(credential.response.authenticatorData);
    response.signature = encode(credential.response.signature);
    response.userHandle = credential.response.userHandle ? encode(credential.response.userHandle) : null;
  }
  return authPost(finishAction, { challenge_id: result.challenge_id,
    credential: { id: credential.id, rawId: encode(credential.rawId), type: credential.type, response } });
}
