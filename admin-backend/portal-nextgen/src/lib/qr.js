// Vector QR for the 8-symbol identity code. No dependency (CSP is script-src
// 'self'), and no URL anywhere: the payload IS the code, never a link, so a
// scanner cannot be pointed at a page by something it read off a player's
// screen.
//
// One symbol shape only: version 1 (21x21 modules), error correction level H,
// alphanumeric mode. The payload is `^[A-Z2-9]{8}$` and version 1-H holds ten
// alphanumeric characters, so the version never has to move and the geometry
// below can be written out flat. Anything else throws rather than quietly
// encoding something a reader would not recognise as a code.

export const QR_MODULES = 21;

const PAYLOAD = /^[A-Z2-9]{8}$/;

const DATA_CODEWORDS = 9;   // version 1-H
const EC_CODEWORDS = 17;    // one block

// Reed-Solomon divisor for 17 error-correction codewords, highest power first
// with the leading 1 dropped, and the 15-bit format words for level H, mask 0
// through 7 (BCH(15,5), XOR 0x5412). Both are fixed for version 1-H, and both
// are pinned rather than derived so the test can recompute them independently.
const DIVISOR = [119, 66, 83, 120, 119, 22, 197, 83, 249, 41, 143, 134, 85, 53, 125, 99, 79];
const FORMATS = [0x1689, 0x13be, 0x1ce7, 0x19d0, 0x0762, 0x0255, 0x0d0c, 0x083b];

// GF(256) with the QR primitive polynomial 0x11d.
const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
{
  let x = 1;
  for (let i = 0; i < 255; i += 1) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i += 1) EXP[i] = EXP[i - 255];
}

function mul(a, b) {
  return a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]];
}

/** The 9 data codewords: mode 0010, 9-bit count, 11 bits per character pair. */
function dataCodewords(code) {
  const bits = [];
  const push = (value, len) => {
    for (let i = len - 1; i >= 0; i -= 1) bits.push((value >> i) & 1);
  };
  const value = (c) => (c >= 'A' ? c.charCodeAt(0) - 55 : c.charCodeAt(0) - 48);

  push(0b0010, 4);
  push(code.length, 9);
  for (let i = 0; i < code.length; i += 2) {
    push(value(code[i]) * 45 + value(code[i + 1]), 11);
  }
  push(0, 4); // terminator (57 + 4 bits, well inside the 72-bit capacity)
  while (bits.length % 8) bits.push(0);

  const out = new Uint8Array(DATA_CODEWORDS);
  for (let i = 0; i < bits.length; i += 8) {
    let b = 0;
    for (let j = 0; j < 8; j += 1) b = (b << 1) | bits[i + j];
    out[i / 8] = b;
  }
  for (let i = bits.length / 8, pad = 0; i < DATA_CODEWORDS; i += 1, pad += 1) {
    out[i] = pad % 2 === 0 ? 0xec : 0x11;
  }
  return out;
}

function eccCodewords(data) {
  const rem = new Uint8Array(EC_CODEWORDS);
  for (const byte of data) {
    const factor = byte ^ rem[0];
    rem.copyWithin(0, 1);
    rem[EC_CODEWORDS - 1] = 0;
    for (let i = 0; i < EC_CODEWORDS; i += 1) rem[i] ^= mul(DIVISOR[i], factor);
  }
  return rem;
}

function blank() {
  return Array.from({ length: QR_MODULES }, () => new Uint8Array(QR_MODULES));
}

/** Finders, separators, timing, the dark module and the reserved format strip.
 *  `fn` marks every module the data stream and the mask must skip. */
function functionPatterns(m, fn) {
  const set = (r, c, v) => { m[r][c] = v; fn[r][c] = 1; };
  for (const [r0, c0] of [[0, 0], [0, QR_MODULES - 7], [QR_MODULES - 7, 0]]) {
    for (let r = -1; r <= 7; r += 1) {
      for (let c = -1; c <= 7; c += 1) {
        const rr = r0 + r;
        const cc = c0 + c;
        if (rr < 0 || rr >= QR_MODULES || cc < 0 || cc >= QR_MODULES) continue;
        const ring = Math.max(Math.abs(r - 3), Math.abs(c - 3));
        set(rr, cc, ring === 2 || ring > 3 ? 0 : 1);
      }
    }
  }
  for (let i = 8; i < QR_MODULES - 8; i += 1) {
    set(6, i, i % 2 === 0 ? 1 : 0);
    set(i, 6, i % 2 === 0 ? 1 : 0);
  }
  // The format strip, and the module that turns dark with it, are reserved
  // light so the mask skips them; drawFormat writes the real bits at the end.
  for (let i = 0; i < QR_MODULES; i += 1) {
    if (i > 8 && i < QR_MODULES - 8) continue;
    if (!fn[8][i]) set(8, i, 0);
    if (!fn[i][8]) set(i, 8, 0);
  }
}

/** Zig-zag the 26 codewords up and down the two-module columns, right to left,
 *  skipping the vertical timing column. */
function placeCodewords(m, fn, all) {
  let bit = 0;
  for (let right = QR_MODULES - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let v = 0; v < QR_MODULES; v += 1) {
      for (let j = 0; j < 2; j += 1) {
        const c = right - j;
        const upward = ((right + 1) & 2) === 0;
        const r = upward ? QR_MODULES - 1 - v : v;
        if (fn[r][c] || bit >= all.length * 8) continue;
        m[r][c] = (all[bit >> 3] >> (7 - (bit & 7))) & 1;
        bit += 1;
      }
    }
  }
}

function maskBit(mask, r, c) {
  switch (mask) {
    case 0: return (r + c) % 2 === 0;
    case 1: return r % 2 === 0;
    case 2: return c % 3 === 0;
    case 3: return (r + c) % 3 === 0;
    case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
    case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
    case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
    default: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
  }
}

function drawFormat(m, mask) {
  const bits = FORMATS[mask];
  const at = (i) => (bits >> i) & 1;
  const set = (r, c, v) => { m[r][c] = v; };
  for (let i = 0; i <= 5; i += 1) set(i, 8, at(i));
  set(7, 8, at(6));
  set(8, 8, at(7));
  set(8, 7, at(8));
  for (let i = 9; i <= 14; i += 1) set(8, 14 - i, at(i));
  for (let i = 0; i <= 7; i += 1) set(8, QR_MODULES - 1 - i, at(i));
  for (let i = 8; i <= 14; i += 1) set(QR_MODULES - 15 + i, 8, at(i));
  set(QR_MODULES - 8, 8, 1);
}

/** Penalty rule 3: the 1:1:3:1:1 finder ratio in a line, preceded or followed
 *  by four light modules. The symbol edge counts as that light area, and a
 *  scored run is stepped over rather than rescanned, so an overlapping pair is
 *  charged once. */
function finderLike(seq) {
  const RATIO = [1, 0, 1, 1, 1, 0, 1];
  const light = (from, to) => {
    for (let i = Math.max(from, 0); i < Math.min(to, QR_MODULES); i += 1) {
      if (seq[i]) return false;
    }
    return true;
  };
  let score = 0;
  let i = 0;
  while (i + 7 <= QR_MODULES) {
    if (RATIO.some((v, k) => seq[i + k] !== v)) { i += 1; continue; }
    if (i === 0 || i === QR_MODULES - 7 || light(i - 4, i) || light(i + 7, i + 11)) {
      score += 40;
      i += 7;
    } else {
      i += 4;
    }
  }
  return score;
}

/** The four penalty rules, scored on the masked symbol. Lowest wins. */
function penalty(m) {
  const runs = (seq) => {
    let sub = 0;
    let run = 1;
    for (let i = 1; i < QR_MODULES; i += 1) {
      if (seq[i] !== seq[i - 1]) run = 1;
      else if ((run += 1) === 5) sub += 3;
      else if (run > 5) sub += 1;
    }
    return sub;
  };
  let score = 0;
  let dark = 0;
  for (let i = 0; i < QR_MODULES; i += 1) {
    const col = m.map((row) => row[i]);
    score += runs(m[i]) + runs(col) + finderLike(m[i]) + finderLike(col);
    for (let k = 0; k < QR_MODULES; k += 1) dark += m[i][k];
  }
  for (let r = 0; r < QR_MODULES - 1; r += 1) {
    for (let c = 0; c < QR_MODULES - 1; c += 1) {
      const v = m[r][c];
      if (v === m[r][c + 1] && v === m[r + 1][c] && v === m[r + 1][c + 1]) score += 3;
    }
  }
  const total = QR_MODULES * QR_MODULES;
  return score + Math.floor(Math.abs(dark * 20 - total * 10) / total) * 10;
}

/** The finished 21x21 module grid for one code. */
export function qrModules(code) {
  if (typeof code !== 'string' || !PAYLOAD.test(code)) {
    throw new Error('qr: payload must match ^[A-Z2-9]{8}$');
  }
  const data = dataCodewords(code);
  const all = new Uint8Array(DATA_CODEWORDS + EC_CODEWORDS);
  all.set(data, 0);
  all.set(eccCodewords(data), DATA_CODEWORDS);

  const base = blank();
  const baseFn = blank();
  functionPatterns(base, baseFn);
  placeCodewords(base, baseFn, all);

  // The mask is chosen on the masked symbol BEFORE the format bits are written
  // (ISO/IEC 18004 7.8): scoring a symbol that already carries its format
  // string lets the format bits vote on their own mask.
  const masked = (mask) => {
    const m = base.map((row) => row.slice());
    for (let r = 0; r < QR_MODULES; r += 1) {
      for (let c = 0; c < QR_MODULES; c += 1) {
        if (!baseFn[r][c] && maskBit(mask, r, c)) m[r][c] ^= 1;
      }
    }
    return m;
  };
  let best = 0;
  let bestScore = Infinity;
  for (let mask = 0; mask < 8; mask += 1) {
    const s = penalty(masked(mask));
    if (s < bestScore) { bestScore = s; best = mask; }
  }
  const out = masked(best);
  drawFormat(out, best);
  return out;
}

/** One SVG path covering every dark module, in module units (0,0)-(21,21).
 *  Horizontal runs are merged so the string stays short. The caller supplies
 *  the quiet zone through its viewBox and the colour through `fill`. */
export function qrPath(code) {
  const m = qrModules(code);
  const parts = [];
  for (let r = 0; r < QR_MODULES; r += 1) {
    let c = 0;
    while (c < QR_MODULES) {
      if (!m[r][c]) { c += 1; continue; }
      let len = 1;
      while (c + len < QR_MODULES && m[r][c + len]) len += 1;
      parts.push(`M${c} ${r}h${len}v1h-${len}z`);
      c += len;
    }
  }
  return parts.join('');
}
