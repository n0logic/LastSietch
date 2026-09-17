/* ============================================================================
 * Admin landing (served at "/") — externalized from grid.html inline <script>.
 * Behavior preserved 1:1: 5 data sources, 30s single poll, threshold coloring,
 * degraded/unreachable states, change-only flash, aria labels, 401 redirect.
 * Adds: CPU sparkline ring buffer per infra card + per-game build chip /
 * pending-update strip (fail-silent, off the 30s render path).
 * ============================================================================ */
(function () {
  'use strict';

  /* ── Logout (slim header user menu calls doLogout()) ───────────────────── */
  function doLogout() {
    fetch('/admin/api/auth/csrf', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        return fetch('/admin/api/auth/logout', {
          method: 'POST',
          headers: { 'X-CSRF-Token': d.token },
          credentials: 'same-origin'
        });
      })
      .then(function () { window.location = '/admin/login'; })
      .catch(function () { window.location = '/admin/login'; });
  }
  window.doLogout = doLogout;

  /* ── Game registry ─────────────────────────────────────────────────────── */
  var GAME_REGISTRY = {
    conan: {
      display_name: 'Conan Exiles',
      css_class: 'conan',
      card_img: '/admin/static/img/v2/landing/conan-card.webp',
      fallback_img: '/assets/conan-hero.webp'
    },
    enshrouded: {
      display_name: 'Enshrouded',
      css_class: 'enshrouded',
      card_img: '/admin/static/img/v2/landing/enshrouded-card.webp',
      fallback_img: '/assets/enshrouded-hero.webp'
    },
    dune: {
      display_name: 'Dune Awakening',
      css_class: 'dune',
      card_img: '/admin/static/img/v2/landing/dune-card.webp',
      fallback_img: '/assets/dune-hero-poster.webp'
    }
  };

  /* ── Helpers ───────────────────────────────────────────────────────────── */
  function fmtBytes(bytes) {
    if (bytes == null || isNaN(bytes)) return null;
    var gb = bytes / (1024 * 1024 * 1024);
    if (gb >= 1000) return (gb / 1024).toFixed(2) + ' TB';
    return gb.toFixed(1) + ' GB';
  }

  function fmtBytesTB(bytes) {
    if (bytes == null || isNaN(bytes)) return null;
    var gb = bytes / (1024 * 1024 * 1024);
    if (gb >= 1000) return (gb / 1024).toFixed(2) + ' TB';
    return Math.round(gb) + ' GB';
  }

  function fmtUptime(seconds) {
    if (seconds == null || isNaN(seconds)) return null;
    var d = Math.floor(seconds / 86400);
    var h = Math.floor((seconds % 86400) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  function fmtTimeAgo(unixTs) {
    if (unixTs == null || isNaN(unixTs)) return null;
    var deltaSec = Math.floor(Date.now() / 1000) - unixTs;
    if (deltaSec < 0) return 'just now';
    if (deltaSec < 60) return deltaSec + 's ago';
    if (deltaSec < 3600) return Math.floor(deltaSec / 60) + 'm ago';
    if (deltaSec < 86400) return Math.floor(deltaSec / 3600) + 'h ago';
    if (deltaSec < 86400 * 30) return Math.floor(deltaSec / 86400) + 'd ago';
    return new Date(unixTs * 1000).toISOString().slice(0, 10);
  }

  function setValFlash(el, newText) {
    if (!el || el.textContent === newText) return;
    el.textContent = newText;
    el.classList.remove('is-flashing');
    void el.offsetWidth; /* force reflow */
    el.classList.add('is-flashing');
    setTimeout(function () { el.classList.remove('is-flashing'); }, 400);
  }

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  function escAttr(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /* ── CPU sparkline ring buffers ────────────────────────────────────────── */
  var SPARK_MAX = 30;
  var sparkBuffers = { infra: [], dune: [] };

  function pushSpark(key, value) {
    if (value == null || isNaN(value)) return;
    var buf = sparkBuffers[key];
    buf.push(value);
    if (buf.length > SPARK_MAX) buf.shift();
  }

  function renderSpark(key, prefix) {
    var wrap = document.getElementById(prefix + '-spark');
    if (!wrap) return;
    var line = document.getElementById(prefix + '-spark-line');
    var fill = document.getElementById(prefix + '-spark-fill');
    var buf = sparkBuffers[key];
    /* Need >=2 samples; scale against fixed 100% ceiling (CPU load %). */
    if (buf.length > 1) {
      var stepX = 200 / (buf.length - 1);
      var pts = [];
      for (var i = 0; i < buf.length; i++) {
        var x = (i * stepX).toFixed(2);
        var y = (44 - (Math.max(0, Math.min(100, buf[i])) / 100) * 40 - 2).toFixed(2);
        pts.push(x + ',' + y);
      }
      if (line) line.setAttribute('points', pts.join(' '));
      if (fill) fill.setAttribute('points', ('0,46 ' + pts.join(' ') + ' 200,46'));
      wrap.setAttribute('data-has-data', 'true');
    } else {
      if (line) line.setAttribute('points', '');
      if (fill) fill.setAttribute('points', '');
      wrap.setAttribute('data-has-data', 'false');
    }
  }

  /* ── Proxmox infra banner ──────────────────────────────────────────────── */
  function loadInfraStatus() {
    fetch('/admin/api/infra/status', { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { window.location = '/admin/login'; throw new Error('Unauthorized'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(renderInfraBanner)
      .catch(renderInfraBannerError);
  }

  function renderInfraBanner(d) {
    var dot = document.getElementById('infra-dot');
    var cpuEl = document.getElementById('stat-cpu');
    var ramEl = document.getElementById('stat-ram');
    var ramFreeEl = document.getElementById('stat-ram-free');
    var diskEl = document.getElementById('stat-disk');
    var diskFreeEl = document.getElementById('stat-disk-free');
    var uptimeEl = document.getElementById('stat-uptime');
    var vmsEl = document.getElementById('infra-vms');

    /* Restore dt labels if a prior error overrode them. */
    restoreInfraLabels();

    /* CPU */
    var cpuPct = d.cpu_percent != null ? d.cpu_percent : null;
    setValFlash(cpuEl, cpuPct != null ? Math.round(cpuPct) + '%' : '---');
    cpuEl.className = 'v2-landing__stat-value';
    if (cpuPct != null) {
      if (cpuPct > 95) cpuEl.classList.add('is-crit');
      else if (cpuPct > 80) cpuEl.classList.add('is-warn');
    }
    pushSpark('infra', cpuPct);
    renderSpark('infra', 'infra');

    /* RAM */
    var ramUsedBytes = d.mem_used_bytes != null ? d.mem_used_bytes : null;
    var ramTotalBytes = d.mem_total_bytes != null ? d.mem_total_bytes : null;
    var ramFreeBytes = (ramUsedBytes != null && ramTotalBytes != null)
      ? (ramTotalBytes - ramUsedBytes)
      : (d.mem_free_bytes != null ? d.mem_free_bytes : null);
    var ramUsedGb = ramUsedBytes != null ? (ramUsedBytes / (1024 * 1024 * 1024)).toFixed(1) : null;
    var ramTotalFmt = ramTotalBytes != null ? fmtBytes(ramTotalBytes) : null;
    var ramFreeFmt = ramFreeBytes != null ? fmtBytes(ramFreeBytes) : null;
    setValFlash(ramEl, (ramUsedGb && ramTotalFmt) ? ramUsedGb + ' / ' + ramTotalFmt : '---');
    ramEl.className = 'v2-landing__stat-value';
    var ramPct = (ramUsedBytes && ramTotalBytes) ? (ramUsedBytes / ramTotalBytes * 100) : null;
    if (ramPct != null) {
      if (ramPct > 95) ramEl.classList.add('is-crit');
      else if (ramPct > 85) ramEl.classList.add('is-warn');
    }
    ramFreeEl.textContent = ramFreeFmt ? '· ' + ramFreeFmt + ' free' : '';

    /* Disk */
    var diskUsedBytes = d.disk_used_bytes != null ? d.disk_used_bytes : null;
    var diskTotalBytes = d.disk_total_bytes != null ? d.disk_total_bytes : null;
    var diskFreeBytes = (diskUsedBytes != null && diskTotalBytes != null)
      ? (diskTotalBytes - diskUsedBytes)
      : (d.disk_free_bytes != null ? d.disk_free_bytes : null);
    var diskUsedGb = diskUsedBytes != null ? Math.round(diskUsedBytes / (1024 * 1024 * 1024)) : null;
    var diskTotalFmt = diskTotalBytes != null ? fmtBytesTB(diskTotalBytes) : null;
    var diskFreeFmt = diskFreeBytes != null ? fmtBytes(diskFreeBytes) : null;
    setValFlash(diskEl, (diskUsedGb != null && diskTotalFmt) ? diskUsedGb + ' GB / ' + diskTotalFmt : '---');
    diskEl.className = 'v2-landing__stat-value';
    var diskPct = (diskUsedBytes && diskTotalBytes) ? (diskUsedBytes / diskTotalBytes * 100) : null;
    if (diskPct != null) {
      if (diskPct > 90) diskEl.classList.add('is-crit');
      else if (diskPct > 75) diskEl.classList.add('is-warn');
    }
    var diskSuffix = diskFreeFmt ? '· ' + diskFreeFmt + ' free' : '';
    if (d.storage_type) diskSuffix += (diskSuffix ? ' · ' : '· ') + d.storage_type;
    diskFreeEl.textContent = diskSuffix;

    /* Uptime */
    setValFlash(uptimeEl, fmtUptime(d.uptime_seconds) || '---');
    uptimeEl.className = 'v2-landing__stat-value';

    /* VMs Running */
    var vmsRunning = d.vms_running != null ? d.vms_running : '?';
    var vmsTotal = d.vms_total != null ? d.vms_total : '?';
    var allRunning = (typeof d.vms_running === 'number' && typeof d.vms_total === 'number'
      && d.vms_running === d.vms_total);
    var vmsHtml = 'VMs Running: ';
    if (!allRunning && typeof d.vms_running === 'number') {
      vmsHtml += '<span class="accent">' + vmsRunning + ' / ' + vmsTotal + '</span>';
    } else {
      vmsHtml += vmsRunning + ' / ' + vmsTotal;
    }
    vmsEl.innerHTML = vmsHtml;

    /* Location / platform */
    var loc = (d.location && d.platform) ? (d.location + ' · ' + d.platform) : 'Proxmox';
    if (typeof d.network_speed_mbps === 'number' && d.network_speed_mbps > 0) {
      var spd = d.network_speed_mbps;
      loc += ' · ' + (spd >= 1000 ? (spd / 1000) + ' Gbps' : spd + ' Mbps');
    }
    document.getElementById('infra-location').textContent = loc;

    /* Status dot */
    var degraded = (cpuPct != null && cpuPct > 80) ||
                   (ramPct != null && ramPct > 85) ||
                   (diskPct != null && diskPct > 75);
    dot.className = 'v2-landing__dot ' + (degraded ? 'is-degraded' : 'is-healthy');
    dot.setAttribute('aria-label', 'Infrastructure status: ' + (degraded ? 'degraded' : 'healthy'));
  }

  function restoreInfraLabels() {
    var labels = ['VPS CPU', 'VPS RAM', 'VPS Storage', 'VPS Uptime'];
    document.querySelectorAll('#infra-banner .v2-landing__stat dt').forEach(function (dt, i) {
      if (labels[i]) dt.textContent = labels[i];
    });
  }

  function renderInfraBannerError() {
    ['stat-cpu', 'stat-ram', 'stat-disk', 'stat-uptime'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.textContent = '---'; el.className = 'v2-landing__stat-value'; }
    });
    ['stat-ram-free', 'stat-disk-free'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = '';
    });
    document.getElementById('infra-vms').textContent = 'VMs Running: ---';

    var dot = document.getElementById('infra-dot');
    dot.className = 'v2-landing__dot is-unreachable';
    dot.setAttribute('aria-label', 'Infrastructure status: Proxmox unreachable');

    document.querySelectorAll('#infra-banner .v2-landing__stat dt').forEach(function (dt) {
      dt.textContent = 'Proxmox unreachable';
    });
  }

  /* ── Dune VPS banner ───────────────────────────────────────────────────── */
  function loadDuneStatus() {
    fetch('/api/dune-status.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(renderDuneBanner)
      .catch(renderDuneBannerError);
  }

  function renderDuneBanner(d) {
    var h = d.host || {};
    var p = d.pods || {};
    var dot = document.getElementById('dune-infra-dot');
    var cpuEl = document.getElementById('dune-stat-cpu');
    var ramEl = document.getElementById('dune-stat-ram');
    var ramFreeEl = document.getElementById('dune-stat-ram-free');
    var diskEl = document.getElementById('dune-stat-disk');
    var diskFreeEl = document.getElementById('dune-stat-disk-free');
    var uptimeEl = document.getElementById('dune-stat-uptime');
    var podsEl = document.getElementById('dune-infra-pods');
    var locEl = document.getElementById('dune-infra-location');

    /* CPU */
    var cpuPct = (typeof h.cpu_pct === 'number') ? h.cpu_pct : null;
    setValFlash(cpuEl, cpuPct != null ? Math.round(cpuPct) + '%' : '---');
    cpuEl.className = 'v2-landing__stat-value';
    if (cpuPct != null) {
      if (cpuPct > 95) cpuEl.classList.add('is-crit');
      else if (cpuPct > 80) cpuEl.classList.add('is-warn');
    }
    pushSpark('dune', cpuPct);
    renderSpark('dune', 'dune');

    /* RAM */
    var ramUsed = h.mem_used_bytes;
    var ramTotal = h.mem_total_bytes;
    var ramFree = (ramUsed != null && ramTotal != null) ? (ramTotal - ramUsed) : null;
    var ramUsedGb = ramUsed != null ? (ramUsed / (1024 * 1024 * 1024)).toFixed(1) : null;
    var ramTotalFmt = ramTotal != null ? fmtBytes(ramTotal) : null;
    setValFlash(ramEl, (ramUsedGb && ramTotalFmt) ? ramUsedGb + ' / ' + ramTotalFmt : '---');
    ramEl.className = 'v2-landing__stat-value';
    var ramPct = (typeof h.mem_pct === 'number') ? h.mem_pct : null;
    if (ramPct != null) {
      if (ramPct > 95) ramEl.classList.add('is-crit');
      else if (ramPct > 85) ramEl.classList.add('is-warn');
    }
    ramFreeEl.textContent = ramFree != null ? '· ' + fmtBytes(ramFree) + ' free' : '';

    /* Disk */
    var diskUsed = h.disk_used_bytes;
    var diskTotal = h.disk_total_bytes;
    var diskFree = (diskUsed != null && diskTotal != null) ? (diskTotal - diskUsed) : null;
    var diskUsedGb = diskUsed != null ? Math.round(diskUsed / (1024 * 1024 * 1024)) : null;
    var diskTotalFmt = diskTotal != null ? fmtBytesTB(diskTotal) : null;
    setValFlash(diskEl, (diskUsedGb != null && diskTotalFmt) ? diskUsedGb + ' GB / ' + diskTotalFmt : '---');
    diskEl.className = 'v2-landing__stat-value';
    var diskPct = (typeof h.disk_pct === 'number') ? h.disk_pct : null;
    if (diskPct != null) {
      if (diskPct > 90) diskEl.classList.add('is-crit');
      else if (diskPct > 75) diskEl.classList.add('is-warn');
    }
    diskFreeEl.textContent = diskFree != null ? '· ' + fmtBytes(diskFree) + ' free' : '';

    /* Uptime */
    setValFlash(uptimeEl, fmtUptime(h.uptime_secs) || '---');
    uptimeEl.className = 'v2-landing__stat-value';

    /* Pods */
    var onCount = p.always_on_running != null ? p.always_on_running : '?';
    var onExp = p.always_on_expected != null ? p.always_on_expected : '?';
    var demand = p.on_demand_running != null ? p.on_demand_running : '?';
    var podsHtml = 'Game pods: ';
    var podsHealthy = (typeof p.always_on_running === 'number' && p.always_on_running === p.always_on_expected);
    if (!podsHealthy && typeof p.always_on_running === 'number') {
      podsHtml += '<span class="accent">' + onCount + ' / ' + onExp + ' always-on</span> · ' + demand + ' on-demand';
    } else {
      podsHtml += onCount + ' / ' + onExp + ' always-on · ' + demand + ' on-demand';
    }
    if (typeof d.online_count === 'number') {
      podsHtml += ' · ' + d.online_count + ' player' + (d.online_count === 1 ? '' : 's') + ' online';
    }
    podsEl.innerHTML = podsHtml;

    /* Location */
    if (d.location && d.platform) {
      var locText = d.location + ' · ' + d.platform;
      if (typeof d.network_speed_gbps === 'number' && d.network_speed_gbps > 0) {
        locText += ' · ' + d.network_speed_gbps + ' Gbps';
      } else if (typeof d.network_speed_mbps === 'number' && d.network_speed_mbps >= 1000) {
        locText += ' · ' + (d.network_speed_mbps / 1000) + ' Gbps';
      } else if (typeof d.network_speed_mbps === 'number') {
        locText += ' · ' + d.network_speed_mbps + ' Mbps';
      }
      locEl.textContent = locText;
    }

    /* Status dot */
    var unreachable = !d.reachable;
    var degraded = (cpuPct != null && cpuPct > 80) ||
                   (ramPct != null && ramPct > 85) ||
                   (diskPct != null && diskPct > 75) ||
                   !podsHealthy;
    dot.className = 'v2-landing__dot ' + (unreachable ? 'is-unreachable' : (degraded ? 'is-degraded' : 'is-healthy'));
    dot.setAttribute('aria-label', 'Dune VPS status: ' + (unreachable ? 'unreachable' : (degraded ? 'degraded' : 'healthy')));
  }

  function renderDuneBannerError() {
    ['dune-stat-cpu', 'dune-stat-ram', 'dune-stat-disk', 'dune-stat-uptime'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.textContent = '---'; el.className = 'v2-landing__stat-value'; }
    });
    ['dune-stat-ram-free', 'dune-stat-disk-free'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = '';
    });
    document.getElementById('dune-infra-pods').textContent = 'Game pods: ---';
    var dot = document.getElementById('dune-infra-dot');
    dot.className = 'v2-landing__dot is-unreachable';
    dot.setAttribute('aria-label', 'Dune VPS status: unreachable');
  }

  /* ── Game cards ────────────────────────────────────────────────────────── */
  function deriveBatchStatus(gameData) {
    if (!gameData) return { state: 'unknown', uptime: null };
    if (typeof gameData.provisioned !== 'undefined') {
      if (!gameData.provisioned) return { state: 'provisioned', uptime: null };
      if (!gameData.vm_running) return { state: 'offline', uptime: null };
      if (!gameData.server_running) return { state: 'offline', uptime: gameData.uptime_seconds };
      return { state: 'online', uptime: gameData.uptime_seconds };
    }
    if (gameData.vm_status === 'stopped') return { state: 'offline', uptime: null };
    if (!gameData.server_running) return { state: 'offline', uptime: null };
    return { state: 'online', uptime: null };
  }

  function buildStatusBadge(state) {
    var labels = { online: 'ONLINE', offline: 'OFFLINE', provisioned: 'PROVISIONED', unknown: 'UNKNOWN' };
    return '<span class="v2-landing__badge is-' + state + '" role="status">'
      + '<span class="v2-landing__badge-dot" aria-hidden="true"></span>'
      + labels[state]
      + '</span>';
  }

  function buildCardStats(gameId, batchData, statusInfo) {
    var state = statusInfo.state;

    if (gameId === 'dune') {
      return '<div class="v2-landing__pulse-row v2-landing__pulse-row--dune">'
        + '<div class="v2-landing__pulse"><span class="v2-landing__pulse-value" id="card-dune-players">---</span><span class="v2-landing__pulse-label">Players</span></div>'
        + '<div class="v2-landing__pulse"><span class="v2-landing__pulse-value" id="card-dune-status">---</span><span class="v2-landing__pulse-label">Status</span></div>'
        + '</div>';
    }

    if (state === 'provisioned') {
      return '<div class="v2-landing__coming-soon">Provisioning in progress. Server launch pending.</div>';
    }

    var players = '---';
    var uptime = statusInfo.uptime != null ? (fmtUptime(statusInfo.uptime) || '---') : '---';
    var snapshot = '---';

    if (batchData) {
      if (batchData.uptime && typeof batchData.uptime === 'string') uptime = batchData.uptime;
      if (batchData.last_snapshot) snapshot = batchData.last_snapshot;
      if (typeof batchData.last_snapshot_ts === 'number') snapshot = fmtTimeAgo(batchData.last_snapshot_ts) || snapshot;
      if (batchData.players != null && batchData.max_players != null) players = batchData.players + ' / ' + batchData.max_players;
    }

    if (state === 'offline') { uptime = '---'; players = '---'; }

    return '<div class="v2-landing__pulse-row">'
      + '<div class="v2-landing__pulse"><span class="v2-landing__pulse-value" id="card-' + gameId + '-players">' + players + '</span><span class="v2-landing__pulse-label">Players</span></div>'
      + '<div class="v2-landing__pulse"><span class="v2-landing__pulse-value" id="card-' + gameId + '-uptime">' + uptime + '</span><span class="v2-landing__pulse-label">Server Up</span></div>'
      + '<div class="v2-landing__pulse"><span class="v2-landing__pulse-value" id="card-' + gameId + '-snap">' + snapshot + '</span><span class="v2-landing__pulse-label">Last Snap</span></div>'
      + '</div>';
  }

  function buildAriaLabel(meta, gameId, batchData, statusInfo) {
    var name = meta ? meta.display_name : gameId;
    var state = statusInfo.state;
    if (state === 'provisioned') return name + ' — Provisioned, not yet available';
    if (state === 'online') {
      var players = (batchData && batchData.players != null && batchData.max_players != null)
        ? batchData.players + ' of ' + batchData.max_players + ' players'
        : 'Server Online';
      return name + ' — Server Online, ' + players;
    }
    return name + ' — Server Offline';
  }

  function duneState(duneStatus) {
    if (!duneStatus || duneStatus.available === false) return 'unknown';
    if (duneStatus.battlegroup && duneStatus.battlegroup.error) return 'offline';
    return 'online';
  }

  function buildCards(batchResult, duneStatus) {
    var gameOrder = ['conan', 'enshrouded', 'dune'];
    var grid = document.getElementById('game-grid');
    var html = '';

    gameOrder.forEach(function (gameId) {
      var meta = GAME_REGISTRY[gameId] || { display_name: gameId, css_class: gameId, card_img: '', fallback_img: '' };
      var batchData = batchResult ? batchResult[gameId] : null;
      var statusInfo = deriveBatchStatus(batchData);
      var state = statusInfo.state;
      var isInactive = (state === 'provisioned') && gameId !== 'dune';
      if (gameId === 'dune') state = duneState(duneStatus);
      var ariaLabel = buildAriaLabel(meta, gameId, batchData, statusInfo);

      var linkClass = 'v2-landing__card-link ' + (meta.css_class || gameId) + (isInactive ? ' inactive' : '');
      var cardClass = 'v2-landing__card ' + (meta.css_class || gameId);
      /* Dune card navigates to v2 overview; others to their v1 dashboard. */
      var href = isInactive ? '#' : (gameId === 'dune' ? '/admin/v2/overview' : '/admin/dashboard/' + gameId);
      var ariaDisabled = isInactive ? ' aria-disabled="true" tabindex="-1"' : '';

      html += '<a href="' + href + '" class="' + linkClass + '" role="article"'
        + ' aria-label="' + escAttr(ariaLabel) + '"' + ariaDisabled + ' id="card-link-' + gameId + '">';
      html += '<article class="' + cardClass + '">';

      /* Hero zone — <img> with onerror fallback to /assets/ poster. */
      html += '<div class="v2-landing__card-hero">';
      html += '<img class="v2-landing__card-art" alt="" loading="lazy"'
        + ' src="' + escAttr(meta.card_img) + '"'
        + ' onerror="this.onerror=null;this.src=\'' + escAttr(meta.fallback_img) + '\';">';
      html += '<div class="v2-landing__card-hero-inner">';
      html += '<span class="v2-landing__card-name">' + escHtml(meta.display_name) + '</span>';
      html += buildStatusBadge(state);
      html += '</div></div>';

      /* Body / data zone */
      html += '<div class="v2-landing__card-body">';
      if (gameId !== 'dune') {
        html += '<div class="v2-landing__buildline"><span class="v2-landing__build-chip" id="build-chip-' + gameId + '">build —</span></div>';
      }
      html += buildCardStats(gameId, batchData, statusInfo);
      if (gameId !== 'dune') {
        html += '<div class="v2-landing__pending" id="pending-' + gameId + '" hidden>⬆ Update available</div>';
      }
      if (!isInactive) {
        html += '<div class="v2-landing__manage" aria-hidden="true">↳ Open Dashboard</div>';
      }
      html += '</div>';

      html += '</article></a>';
    });

    grid.innerHTML = html;
  }

  function applyDuneStatus(duneStatus) {
    var playersEl = document.getElementById('card-dune-players');
    var statusEl = document.getElementById('card-dune-status');
    if (!playersEl || !statusEl) return;

    var players = (duneStatus && typeof duneStatus.online_players === 'number')
      ? String(duneStatus.online_players) : '---';
    var statusText;
    if (!duneStatus || duneStatus.available === false) statusText = 'Unknown';
    else if (duneStatus.battlegroup && duneStatus.battlegroup.error) statusText = 'Offline';
    else statusText = 'Online';

    setValFlash(playersEl, players);
    setValFlash(statusEl, statusText);
  }

  function updateCardAriaLabels(batchResult) {
    ['conan', 'enshrouded', 'dune'].forEach(function (gameId) {
      var link = document.getElementById('card-link-' + gameId);
      if (!link) return;
      var meta = GAME_REGISTRY[gameId];
      var batchData = batchResult ? batchResult[gameId] : null;
      var statusInfo = deriveBatchStatus(batchData);
      link.setAttribute('aria-label', buildAriaLabel(meta, gameId, batchData, statusInfo));
    });
  }

  function loadGamesBatch() {
    Promise.all([
      fetch('/admin/api/games/status/batch', { credentials: 'same-origin' })
        .then(function (r) {
          if (r.status === 401) { window.location = '/admin/login'; throw new Error('Unauthorized'); }
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }),
      fetch('/status.json').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
      fetch('/api/dune/status').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
    ])
      .then(function (results) {
        var data = results[0];
        var publicStatus = results[1];
        var duneStatus = results[2];
        if (publicStatus && publicStatus.servers) {
          ['conan', 'enshrouded'].forEach(function (gid) {
            var s = publicStatus.servers[gid];
            if (s && data[gid] && typeof s.players === 'number' && typeof s.maxPlayers === 'number') {
              data[gid].players = s.players;
              data[gid].max_players = s.maxPlayers;
            }
          });
        }
        var grid = document.getElementById('game-grid');
        if (!grid.dataset.built) {
          buildCards(data, duneStatus);
          grid.dataset.built = '1';
          applyDuneStatus(duneStatus);
        } else {
          applyDuneStatus(duneStatus);
          updateCardAriaLabels(data);
          ['conan', 'enshrouded', 'dune'].forEach(function (gameId) {
            var batchData = data[gameId];
            if (!batchData) return;
            var statusInfo = deriveBatchStatus(batchData);
            if (statusInfo.state === 'provisioned') return;

            var playersEl = document.getElementById('card-' + gameId + '-players');
            var uptimeEl = document.getElementById('card-' + gameId + '-uptime');
            var snapEl = document.getElementById('card-' + gameId + '-snap');

            if (playersEl && batchData.players != null && batchData.max_players != null) {
              setValFlash(playersEl, batchData.players + ' / ' + batchData.max_players);
            }
            if (uptimeEl) {
              var uptimeText = batchData.uptime && typeof batchData.uptime === 'string'
                ? batchData.uptime
                : (statusInfo.uptime != null ? (fmtUptime(statusInfo.uptime) || '---') : '---');
              if (statusInfo.state === 'offline') uptimeText = '---';
              setValFlash(uptimeEl, uptimeText);
            }
            if (snapEl) {
              if (typeof batchData.last_snapshot_ts === 'number') {
                setValFlash(snapEl, fmtTimeAgo(batchData.last_snapshot_ts) || '---');
              } else if (batchData.last_snapshot) {
                setValFlash(snapEl, batchData.last_snapshot);
              }
            }
          });
        }
      })
      .catch(function () {
        var grid = document.getElementById('game-grid');
        if (!grid.dataset.built) {
          buildCards(null, null);
          grid.dataset.built = '1';
        }
      });
  }

  /* ── Build chip + pending-update strip (fail-silent, off render path) ───── */
  function loadBuildStatus() {
    ['conan', 'enshrouded'].forEach(function (gameId) {
      fetch('/admin/' + gameId + '/server/update-available', { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (d) {
          var chip = document.getElementById('build-chip-' + gameId);
          var pending = document.getElementById('pending-' + gameId);
          if (chip) chip.textContent = d.installed != null ? 'build ' + d.installed : 'build —';
          if (pending) {
            if (d.available === true) {
              pending.textContent = '⬆ Update available' + (d.latest != null ? ': build ' + d.latest : '');
              pending.hidden = false;
            } else {
              pending.hidden = true;
            }
          }
        })
        .catch(function () { /* fail silent — leave chip at "build —" */ });
    });
  }

  /* ── Polling ───────────────────────────────────────────────────────────── */
  function refreshAll() {
    loadInfraStatus();
    loadDuneStatus();
    loadGamesBatch();
    loadBuildStatus();
  }

  refreshAll();
  setInterval(refreshAll, 30000);
})();
