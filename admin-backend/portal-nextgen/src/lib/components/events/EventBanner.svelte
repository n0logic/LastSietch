<script>
  // The event's art plate. Same discipline as SealedPanel's empty plates: the
  // caller passes a BARE SLUG, never a URL, so this component owns the directory
  // and the one cache-bust key for the whole set. A re-cut banner is one edit
  // here rather than a hunt through every call site, and no slug can ever carry
  // its own version.
  //
  // The slug is refused, not sanitised. A value carrying `/`, `.` or `?` is a
  // path or a query trying to leave the banner directory, and the answer is to
  // render nothing at all rather than to strip characters until it looks safe.
  // Nothing is also the answer for a missing banner: an event without art is a
  // card without a strip, never a placeholder plate standing in for one.
  //
  // The art is decoration (`alt=""`, `aria-hidden`), so `title` is what carries
  // the accessible and visible name when this is used as a hero: the caption sets
  // over the plate. A caller that already prints the title next to the banner
  // (the card does) simply omits it.
  import { base } from '$app/paths';
  import { detectQuality } from '$lib/quality.js';

  let { banner = null, title = '' } = $props();

  const BANNER_DIR = '/img/v2/banners/';
  // Bumped whenever the banner set is re-cut. One key for the set: they ship and
  // change together, and a per-file key is a per-file thing to forget.
  const BANNER_VERSION = '20260904a';

  // The one quality detector the app already has; SSR-safe (no window, no saver).
  const saveData = detectQuality().saveData;

  let slug = $derived(typeof banner === 'string' ? banner.trim() : '');
  let safe = $derived(slug !== '' && !/[/.?]/.test(slug));
  let url = $derived(
    safe && !saveData ? `${base}${BANNER_DIR}${slug}.webp?v=${BANNER_VERSION}` : null
  );
</script>

{#if url}
  <figure class="banner">
    <img src={url} alt="" aria-hidden="true" loading="lazy" decoding="async" />
    {#if title}<figcaption class="cap">{title}</figcaption>{/if}
  </figure>
{/if}

<style>
  .banner {
    position: relative; margin: 0; overflow: hidden;
    aspect-ratio: 1200 / 450;
    background: var(--bg-deep);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .banner img { display: block; width: 100%; height: 100%; object-fit: cover; }
  .cap {
    position: absolute; inset: auto 0 0 0;
    padding: var(--space-4) var(--space-4) var(--space-3);
    font-family: var(--font-display);
    font-size: clamp(1.1rem, 3.4vw, 1.9rem); line-height: 1.1;
    letter-spacing: .02em; text-transform: uppercase; color: var(--text);
    background: linear-gradient(to top, var(--bg-deep) 6%, transparent);
    text-shadow: 0 2px 10px var(--bg-deep);
  }
</style>
