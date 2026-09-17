<script>
  // Optional lazy three.js hero for the SELECTED container only. Gated
  // tier === 'high' && webgl2 && !reducedMotion (DECISION 7); any gate miss or load
  // failure renders NOTHING and the PNG tile stays the visual. three is imported
  // dynamically so it never touches the shell path. A MutationObserver re-reads the
  // theme tokens (light tint) when data-theme/data-house flips. Auto-rotate is
  // killed under reduced motion (the gate already excludes it, kept as defense).
  import { onMount, onDestroy } from 'svelte';
  import { detectQuality } from '$lib/quality.js';
  import { probeWebGL2 } from '$lib/map/holo/stage-adapter.js';

  let { glb, name = '' } = $props();

  let host = $state(null);
  let active = $state(false);   // did the gate pass + did we mount?
  let teardown = null;
  let observer = null;

  function gatePass() {
    const q = detectQuality();
    return q.tier === 'high' && !q.reducedMotion && probeWebGL2();
  }

  function readTokens() {
    const cs = getComputedStyle(document.documentElement);
    const bg = cs.getPropertyValue('--bg-card').trim() || '#1b160e';
    const accent = cs.getPropertyValue('--accent').trim() || '#d4891c';
    return { bg, accent };
  }

  async function mount() {
    if (!host || !glb || !gatePass()) return;
    let THREE, GLTFLoader;
    try {
      THREE = await import('three');
      ({ GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js'));
    } catch (e) {
      return; // three unavailable -> PNG stays
    }

    const width = host.clientWidth || 320;
    const height = host.clientHeight || 200;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(width, height, false);
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, width / height, 0.1, 100);
    camera.position.set(0, 1.1, 4.2);
    camera.lookAt(0, 0.4, 0);

    let tokens = readTokens();
    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(2.5, 4, 3);
    scene.add(key);
    const rim = new THREE.DirectionalLight(new THREE.Color(tokens.accent), 1.4);
    rim.position.set(-3, 1.5, -2);
    scene.add(rim);
    scene.add(new THREE.AmbientLight(0xffffff, 0.55));

    let model = null, raf = 0, disposed = false;
    const loader = new GLTFLoader();
    loader.load(
      glb,
      (gltf) => {
        if (disposed) return;
        model = gltf.scene;
        // Frame the model: center it and scale to a unit-ish box.
        const box = new THREE.Box3().setFromObject(model);
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z) || 1;
        const s = 2.4 / maxDim;
        model.scale.setScalar(s);
        model.position.sub(center.multiplyScalar(s));
        model.position.y += 0.1;
        scene.add(model);
        active = true;
      },
      undefined,
      () => { /* load error -> nothing renders, PNG stays */ }
    );

    function frame() {
      raf = requestAnimationFrame(frame);
      if (model) model.rotation.y += 0.005;
      renderer.render(scene, camera);
    }
    frame();

    function onResize() {
      if (!host) return;
      const w = host.clientWidth || width, h = host.clientHeight || height;
      camera.aspect = w / h; camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
    }
    window.addEventListener('resize', onResize);

    // Re-tint the rim light when the theme/house flips.
    observer = new MutationObserver(() => {
      tokens = readTokens();
      rim.color = new THREE.Color(tokens.accent);
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-house'] });

    teardown = () => {
      disposed = true;
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
      observer?.disconnect(); observer = null;
      scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose?.();
        if (o.material) {
          const mats = Array.isArray(o.material) ? o.material : [o.material];
          for (const m of mats) { m.map?.dispose?.(); m.dispose?.(); }
        }
      });
      renderer.dispose();
      renderer.domElement.remove();
      active = false;
    };
  }

  function unmount() { teardown?.(); teardown = null; }

  onMount(() => {
    mount();
    return () => unmount();
  });
  onDestroy(unmount);

  // Re-mount when the selected container's GLB changes. lastGlb is a deliberate
  // previous-value tracker seeded from the initial prop (not a reactive read).
  // svelte-ignore state_referenced_locally
  let lastGlb = glb;
  $effect(() => {
    if (glb !== lastGlb) {
      lastGlb = glb;
      unmount();
      mount();
    }
  });
</script>

<div class="hero" class:active bind:this={host} aria-hidden="true" title={name}></div>

<style>
  /* Zero-height until a frame is actually drawing, so a gate miss leaves no gap. */
  .hero { width: 100%; height: 0; overflow: hidden; border-radius: var(--radius-sm); transition: height var(--motion-mid) var(--ease-out); }
  .hero.active { height: 200px; background: radial-gradient(60% 60% at 50% 40%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 70%); }
  .hero :global(canvas) { display: block; width: 100%; height: 100%; }
  @media (prefers-reduced-motion: reduce) { .hero { transition: none; } }
</style>
