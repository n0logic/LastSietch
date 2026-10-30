// "Easier reading font" (Settings, Display). The device copy is `ls-reading-font`,
// which the boot script in app.html reads before first paint, so renaming the key
// or the attribute here would apply the choice on this page and lose it on the
// next load. The account copy is the `reading_font` pref, applied once the
// server doc has been read.
export const READING_FONT_KEY = 'ls-reading-font';
const ATTR = 'data-reading-font';

export function readingFontOn() {
  return document.documentElement.getAttribute(ATTR) === 'dyslexic';
}

export function applyReadingFont(on) {
  const el = document.documentElement;
  if (on) el.setAttribute(ATTR, 'dyslexic');
  else el.removeAttribute(ATTR);
  try {
    if (on) localStorage.setItem(READING_FONT_KEY, '1');
    else localStorage.removeItem(READING_FONT_KEY);
  } catch (e) {}
}

// Body text size while the reading font is on: 's' | 'm' | 'l', Medium the default.
// Same split as the font: `ls-reading-size` on the device (read pre-paint by
// app.html), `reading_size` on the account. Medium is the absent attribute, so
// app.css only needs rules for the other two.
export const READING_SIZE_KEY = 'ls-reading-size';
const SIZE_ATTR = 'data-reading-size';
export const READING_SIZES = ['s', 'm', 'l'];

export function readingSize() {
  const v = document.documentElement.getAttribute(SIZE_ATTR);
  return v === 's' || v === 'l' ? v : 'm';
}

export function applyReadingSize(size) {
  if (!READING_SIZES.includes(size)) return;
  const el = document.documentElement;
  if (size === 'm') el.removeAttribute(SIZE_ATTR);
  else el.setAttribute(SIZE_ATTR, size);
  try { localStorage.setItem(READING_SIZE_KEY, size); } catch (e) {}
}
