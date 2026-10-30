/* Base Vault for packed BRT backups (VAULT-03), shown inside the BRT backup drawer.
 *
 * Same rules as the placed-base drawer (base-vault.js): one request id per press and
 * never a second submission on its own; a request whose outcome is unknown blocks the
 * button until a status read settles it; the status line reports what the vault read
 * back, never what was asked for.
 */
(function () {
  'use strict';

  var API = '/admin/api/dune/v2/base-backups/';
  var timer = null, generation = 0;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function stamp(value) {
    var d = value ? new Date(value) : null;
    return d && !isNaN(d) ? d.toLocaleString() : 'Unknown';
  }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'Unknown'; }
  function backupId(value) { return /^[1-9][0-9]{0,18}$/.test(String(value)) ? String(value) : null; }

  function parts(section) {
    return {
      id: backupId(section.dataset.backupVault),
      capture: section.querySelector('[data-bv-backup-capture]'),
      refresh: section.querySelector('[data-bv-backup-refresh]'),
      status: section.querySelector('[data-bv-backup-status]'),
      history: section.querySelector('[data-bv-backup-history]'),
      origin: section.querySelector('[data-bv-backup-origin]')
    };
  }

  function say(p, text, kind) {
    p.status.textContent = text;
    p.status.dataset.status = kind || '';
  }

  function point(origin) {
    var c = origin && origin.claim_origin;
    if (!Array.isArray(c) || c.length !== 3) return null;
    return c.map(function (v) { return Number.isFinite(v) ? Math.round(v).toLocaleString() : '?'; }).join(', ');
  }

  // A5: the origin a Deep Desert replant needs. A native capture holds the parked base
  // and its claim origin; ranks and the original layout exist only in a live-base capture.
  function renderOrigin(p, data) {
    if (!p.origin) return;
    p.origin.replaceChildren();
    var totem = backupId(data.origin_totem_id);
    if (!totem) return;
    var rows = Array.isArray(data.origin_snapshots) ? data.origin_snapshots : [];
    if (data.origin_snapshot_available === true && rows.length) {
      var first = rows[0];
      var line = el('p', 'bv-check', 'Origin snapshot available');
      line.dataset.status = first.verification === 'verified' ? 'verified' : first.verification === 'failed' ? 'failed' : 'not_checked';
      var where = (first.source_map || 'Unknown map') + (first.source_dimension != null ? ' dim ' + first.source_dimension : '');
      var open = el('button', 'btn btn-sm bv-open', 'Open totem ' + totem + ' history');
      open.type = 'button';
      open.dataset.baseVault = totem;
      p.origin.append(line, el('p', 'bv-note', 'Live-base capture of totem ' + totem + ' on ' + where + ', ' + stamp(first.captured_at) +
        (first.verification === 'verified' ? ', checksum verified.' : first.verification === 'failed' ? ', checksum verification FAILED.' : ', checksum not checked yet.') +
        (rows.length > 1 ? ' ' + rows.length + ' live captures in all.' : '')), open);
    } else {
      p.origin.append(el('p', 'bv-note', 'No live-base snapshot of totem ' + totem + '. The original layout and member ranks are not archived anywhere; only the packed backup is.'));
    }
  }

  function render(p, data) {
    renderOrigin(p, data);
    p.history.replaceChildren();
    var rows = data.snapshots || [];
    if (!rows.length) {
      p.history.append(el('p', 'bv-note', 'No snapshots of this backup yet.'));
    }
    rows.forEach(function (row) {
      var article = el('article', 'bv-snapshot');
      article.append(el('h4', '', row.label || 'Packed backup snapshot'),
                     el('p', 'bv-note', 'Persisted state captured: ' + stamp(row.captured_at)));
      var v = row.verification || {}, lab = row.validation || {};
      var checks = el('div', 'bv-snapshot__checks');
      var sum = el('span', 'bv-check', v.status === 'verified' ? 'Checksum verified' : v.status === 'failed' ? 'Checksum verification failed' : 'Checksum not checked');
      sum.dataset.status = v.status || 'not_checked';
      var trip = el('span', 'bv-check', lab.status === 'passed' ? 'Database round-trip passed' : lab.status === 'failed' ? 'Database round-trip failed' : 'Database round-trip not run');
      trip.dataset.status = lab.status || 'not_run';
      checks.append(sum, trip);
      article.append(checks);
      var origin = point(row.origin);
      article.append(el('p', 'bv-note', origin ? 'Claim origin: ' + origin + ' (where the BRT rebuilds it)'
        : 'Claim origin not recorded (captured before origin metadata). The parked location is in the archive.'));
      var counts = row.counts || {};
      var total = Object.keys(counts).reduce(function (s, k) { return s + (Number.isSafeInteger(counts[k]) ? counts[k] : 0); }, 0);
      article.append(el('p', 'bv-note', count(total) + ' saved rows across ' + Object.keys(counts).length + ' tables / ' + count(row.archive_bytes) + ' compressed bytes'));
      var details = el('details');
      details.append(el('summary', '', 'Archive checksums'));
      [['Snapshot ID', row.snapshot_id], ['Payload SHA-256', row.payload_sha256], ['Archive SHA-256', row.archive_sha256]].forEach(function (e) {
        details.append(el('p', 'bv-note', e[0]), el('code', '', e[1] || 'Not recorded'));
      });
      article.append(details);
      p.history.append(article);
    });
  }

  function settle(p, data) {
    var op = data.operation;
    var running = op && ['pending', 'running'].indexOf(op.status) !== -1;
    var blocked = ['pending', 'running', 'uncertain'].indexOf(data.capture_blocked) !== -1;
    p.capture.disabled = !data.capture_allowed || running || blocked;
    p.capture.textContent = running ? 'Capturing snapshot...' : 'Take snapshot';
    if (op && op.status === 'uncertain' || data.capture_blocked === 'uncertain') {
      say(p, 'A capture outcome is uncertain. Further capture is blocked for operator review.', 'uncertain');
    } else if (running) {
      say(p, 'Capture is in progress. This panel checks the existing operation automatically.', 'running');
    } else if (blocked) {
      say(p, 'Another capture is in progress. Capture becomes available after it finishes.', 'running');
    } else if (op && op.status === 'completed') {
      say(p, 'Snapshot captured and archive verified. The backup is unchanged.', 'completed');
    } else if (op && op.status === 'failed') {
      say(p, 'Capture did not complete. No verified snapshot was recorded for this operation.', 'failed');
    } else if (!data.backup) {
      say(p, 'This backup is not available in the game database. Recorded snapshots remain listed.', 'unavailable');
    } else if (!data.capture_allowed) {
      say(p, 'Capture is unavailable for this backup (disabled, or some of its actors are not packed).', 'unavailable');
    } else {
      say(p, 'Ready to capture ' + count(data.backup.linked_actors) + ' packed actors. Every check runs again during capture.', 'ready');
    }
    return running || blocked;
  }

  async function load(section, ticket) {
    var p = parts(section);
    if (!p.id) return;
    p.history.setAttribute('aria-busy', 'true');
    try {
      var response = await fetch(API + encodeURIComponent(p.id) + '/vault', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('status ' + response.status);
      var data = await response.json();
      if (ticket !== generation) return;
      if (data.available !== true || String(data.backup_id) !== p.id || !Array.isArray(data.snapshots)) throw new Error('invalid');
      render(p, data);
      clearTimeout(timer);
      if (settle(p, data) && section.isConnected) timer = setTimeout(function () { load(section, ticket); }, 3000);
    } catch (_) {
      if (ticket !== generation) return;
      p.capture.disabled = true;
      say(p, 'Vault status could not be loaded. Capture stays unavailable until a successful status check.', 'unavailable');
    } finally {
      p.history.setAttribute('aria-busy', 'false');
    }
  }

  async function capture(section) {
    var p = parts(section);
    if (!p.id || p.capture.disabled) return;
    var request;
    try { request = crypto.randomUUID(); } catch (_) { say(p, 'This browser cannot create a capture request.', 'failed'); return; }
    p.capture.disabled = true;
    p.capture.textContent = 'Requesting snapshot...';
    say(p, 'Submitting one capture request.', 'running');
    try {
      await fetchCsrf();
      var result = await apiCall('POST', API + encodeURIComponent(p.id) + '/vault/snapshots', { request_id: request });
      if (!result.operation || result.operation.request_id !== request) throw new Error('unconfirmed');
    } catch (error) {
      say(p, error && error.status === 409 ? 'Capture refused: ' + (error.message || 'busy') + '. Status refreshed.'
        : 'Checking whether the vault accepted the request. No second capture will be submitted.', 'uncertain');
    }
    load(section, generation);
  }

  function attach(root) {
    var sections = (root || document).querySelectorAll('[data-backup-vault]');
    if (!sections.length) return;
    generation += 1;
    clearTimeout(timer);
    sections.forEach(function (section) {
      var p = parts(section);
      p.capture.addEventListener('click', function () { capture(section); });
      p.refresh.addEventListener('click', function () { load(section, generation); });
      load(section, generation);
    });
  }

  document.body.addEventListener('htmx:afterSwap', function (e) { attach(e.target); });
  attach(document);
})();
