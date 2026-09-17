(function () {
  'use strict';

  var dialog = document.getElementById('bv-dialog');
  if (!dialog) return;
  var current = null, generation = 0, timer = null, loading = false, opener = null;
  var state = null, unconfirmed = {}, refused = {}, directory = [], directoryLoading = false;
  var capture = document.getElementById('bv-capture');
  var refresh = document.getElementById('bv-refresh');
  var history = document.getElementById('bv-history');
  var status = document.getElementById('bv-status');
  var context = document.getElementById('bv-context');
  var recovery = document.getElementById('bv-recovery');
  var planBody = document.getElementById('bv-plan-body');
  var planStatus = document.getElementById('bv-plan-status');
  var planRefresh = document.getElementById('bv-plan-refresh');
  var planRestage = document.getElementById('bv-plan-restage');
  var planSelect = document.getElementById('bv-plans');
  var restorePlan = null, selectedPlanId = null, restoreBusy = false, restoreGeneration = 0, expiryTimer = null;
  var pendingPlan = null, snapshots = {}, stageButtons = [];
  var STORAGE_KEY = 'lastsietch.baseVault.pending.v1';
  try { unconfirmed = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '{}') || {}; } catch (_) {}

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function clear(node) { node.replaceChildren(); }
  function id(value) { return /^[1-9][0-9]{0,18}$/.test(String(value)) ? String(value) : null; }
  function uuid(value) { return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value); }
  function stamp(value) {
    if (!value) return 'Not recorded';
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Not recorded' : date.toLocaleString(undefined, { timeZoneName: 'short' });
  }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'Unknown'; }
  function mapName(base) {
    if (!base || !base.map) return 'Unknown map';
    return base.map + ' / dimension ' + (base.dimension_index == null ? 'unknown' : base.dimension_index);
  }
  function remember() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(unconfirmed)); } catch (_) {}
  }
  function path(totem) { return '/admin/api/dune/v2/claims/' + encodeURIComponent(totem) + '/vault'; }
  function message(text, kind) {
    status.textContent = text;
    status.dataset.state = kind || '';
  }
  function empty(node, title, description) {
    clear(node);
    var box = el('div', 'bv-empty');
    box.append(el('h3', '', title), el('p', '', description));
    node.append(box);
  }
  function skeleton(node) {
    clear(node);
    for (var i = 0; i < 3; i++) {
      var line = el('div', 'bv-skeleton');
      line.setAttribute('aria-hidden', 'true');
      node.append(line);
    }
  }
  function field(name, value) { context.append(el('dt', '', name), el('dd', '', value == null ? 'Unknown' : value)); }
  function renderContext(base) {
    clear(context);
    document.getElementById('bv-title').textContent = base && base.label || 'Totem ' + current;
    field('Totem', current);
    if (base) {
      field('Owner', base.owner_name || 'No owner on record');
      field('Owner controller', base.owner_controller_id);
      field('Map instance', mapName(base));
    } else {
      field('Placed base', 'Not available. Existing snapshot history is retained.');
    }
  }

  function renderSnapshots(data) {
    var rows = data.snapshots || [];
    snapshots = {};
    stageButtons = [];
    clear(history);
    document.getElementById('bv-history-count').textContent = data.truncated
      ? 'Newest ' + rows.length + ' of ' + count(data.total_snapshots)
      : count(data.total_snapshots == null ? rows.length : data.total_snapshots) + (rows.length === 1 ? ' snapshot' : ' snapshots');
    if (!rows.length) {
      empty(history, 'No snapshots yet', 'Take a snapshot to preserve this base\'s currently saved state. Captures appear here after archive verification completes.');
      return;
    }
    rows.forEach(function (row) {
      var article = el('article', 'bv-snapshot');
      var heading = el('h4', '', row.label || 'Base snapshot');
      var saved = el('p', 'bv-note', 'Persisted state captured: ');
      var time = el('time', '', stamp(row.captured_at));
      if (row.captured_at) time.dateTime = row.captured_at;
      saved.append(time);
      article.append(heading, saved);
      var checks = el('div', 'bv-snapshot__checks');
      var verification = row.verification || {}, validation = row.validation || {};
      var checked = verification.status === 'verified' ? 'Checksum verified' : verification.status === 'failed' ? 'Checksum verification failed' : 'Checksum not checked';
      var verified = el('span', 'bv-check', checked + (verification.checked_at ? ' / ' + stamp(verification.checked_at) : ''));
      verified.dataset.status = verification.status || 'not_checked';
      var tested = validation.status === 'passed' ? 'Database round-trip passed' : validation.status === 'failed' ? 'Database round-trip failed' : 'Database round-trip not run';
      var lab = el('span', 'bv-check', tested + (validation.completed_at ? ' / ' + stamp(validation.completed_at) : ''));
      lab.dataset.status = validation.status || 'not_run';
      checks.append(verified, lab);
      article.append(checks);
      var counts = row.counts || {}, tables = Object.keys(counts);
      var total = tables.reduce(function (sum, key) { return sum + (Number.isSafeInteger(counts[key]) ? counts[key] : 0); }, 0);
      article.append(el('p', 'bv-note', count(total) + ' saved rows across ' + tables.length + ' tables / ' + count(row.archive_bytes) + ' compressed bytes'));
      var details = el('details');
      details.append(el('summary', '', 'Counts and archive checksums'));
      var list = el('dl', 'bv-counts');
      tables.sort().forEach(function (key) { list.append(el('dt', '', key.replace(/_/g, ' ')), el('dd', '', count(counts[key]))); });
      details.append(list);
      [['Snapshot ID', row.snapshot_id], ['Payload SHA-256', row.payload_sha256], ['Archive SHA-256', row.archive_sha256]].forEach(function (entry) {
        details.append(el('p', 'bv-note', entry[0]), el('code', '', entry[1] || 'Not recorded'));
      });
      article.append(details);
      if (uuid(row.snapshot_id)) {
        snapshots[row.snapshot_id] = row;
        var stage = el('button', 'btn btn-sm bv-stage', 'Stage for restore');
        stage.type = 'button';
        stage.dataset.restoreSnapshot = row.snapshot_id;
        stage.disabled = restoreBusy || !!pendingPlan || data.restore_planning_enabled !== true;
        stage.setAttribute('aria-label', 'Stage snapshot from ' + stamp(row.captured_at) + ' for restore');
        stage.addEventListener('click', function () { stagePlan(row.snapshot_id); });
        stageButtons.push(stage);
        article.append(stage);
      }
      history.append(article);
    });
  }

  function planMessage(text, kind) {
    planStatus.textContent = text;
    planStatus.dataset.state = kind || '';
  }
  function planningBusy(value) {
    restoreBusy = value;
    planBody.setAttribute('aria-busy', value ? 'true' : 'false');
    planRefresh.disabled = value || (!selectedPlanId && !pendingPlan);
    planRestage.disabled = value || !!pendingPlan || !restorePlan || !state || state.restore_planning_enabled !== true;
    planSelect.disabled = value;
    stageButtons.forEach(function (button) { button.disabled = value || !!pendingPlan || !state || state.restore_planning_enabled !== true; });
  }
  function resetRestore() {
    restoreGeneration += 1;
    clearTimeout(expiryTimer);
    restorePlan = null;
    selectedPlanId = null;
    pendingPlan = null;
    snapshots = {};
    stageButtons = [];
    clear(planBody);
    planSelect.replaceChildren(new Option('Choose a plan', ''));
    document.getElementById('bv-plan-picker').hidden = true;
    planningBusy(false);
    planMessage('Choose Stage for restore on a snapshot below.');
  }
  function planFreshness(plan) {
    var created = Date.parse(plan.created_at), expires = Date.parse(plan.expires_at);
    if (!Number.isFinite(created) || !Number.isFinite(expires) || expires <= created || created > Date.now() + 60000) return 'unknown';
    return expires <= Date.now() ? 'stale' : 'current';
  }
  function bindingMatches(plan, expected) {
    var source = plan.source || {}, target = plan.target || {};
    if (!uuid(plan.plan_id) || !uuid(plan.snapshot_id) || source.snapshot_id !== plan.snapshot_id ||
        plan.mode !== 'original_location_missing_base' || String(source.totem_id) !== current ||
        !id(source.owner_controller_id) || !source.map || !id(source.partition_id) ||
        !Number.isSafeInteger(source.dimension_index) || !Array.isArray(plan.checks) ||
        !Array.isArray(plan.blocking_reasons) || !/^[0-9a-f]{64}$/.test(source.payload_sha256 || '')) return false;
    if (['map', 'partition_id', 'dimension_index', 'owner_controller_id'].some(function (key) { return String(source[key]) !== String(target[key]); })) return false;
    return !expected || plan.snapshot_id === expected.snapshot_id && source.payload_sha256 === expected.payload_sha256;
  }
  function renderPlan(plan, expected) {
    if (!bindingMatches(plan, expected)) throw new Error('Restore source binding could not be verified');
    clearTimeout(expiryTimer);
    restorePlan = plan;
    selectedPlanId = plan.plan_id;
    if (!Array.from(planSelect.options).some(function (option) { return option.value === plan.plan_id; })) {
      planSelect.append(new Option(stamp(plan.created_at) + ' / ' + plan.plan_id.slice(0, 8), plan.plan_id));
    }
    planSelect.value = plan.plan_id;
    document.getElementById('bv-plan-picker').hidden = false;
    clear(planBody);
    var source = plan.source, target = plan.target;
    var pairing = el('div', 'bv-plan-source');
    var from = el('section'), to = el('section');
    from.append(el('h4', '', 'Source snapshot'), el('p', '', 'Totem ' + source.totem_id),
      el('p', '', 'Owner controller ' + source.owner_controller_id), el('p', '', 'Saved ' + stamp(source.captured_at)),
      el('code', '', plan.snapshot_id));
    to.append(el('h4', '', 'Original target'), el('p', '', mapName(target)),
      el('p', '', 'Partition ' + target.partition_id), el('p', '', 'Owner controller ' + target.owner_controller_id),
      el('p', 'bv-note', 'Original coordinates and ownership. No relocation or overwrite.'));
    pairing.append(from, to);
    planBody.append(pairing);
    var meta = el('dl', 'bv-plan-meta');
    [['Plan ID', plan.plan_id], ['Checked', stamp(plan.created_at)], ['Expires', stamp(plan.expires_at)], ['Source payload SHA-256', source.payload_sha256]].forEach(function (entry) {
      meta.append(el('dt', '', entry[0]), el('dd', '', entry[1]));
    });
    planBody.append(meta);
    var checks = el('ul', 'bv-plan-checks');
    plan.checks.forEach(function (check) {
      var status = ['passed', 'blocked', 'unknown'].indexOf(check.status) === -1 ? 'unknown' : check.status;
      var labels = { archive_integrity: 'Archive integrity', schema: 'Schema compatibility', missing_base: 'Original base absent',
        id_collisions: 'ID collisions', dependencies: 'External dependencies', ownership: 'Ownership bindings',
        triggers: 'Database triggers', game_build: 'Game build compatibility', map_lifecycle: 'Map lifecycle',
        production_approval: 'Production approval', gameplay_compatibility: 'In-game compatibility' };
      var label = labels[check.id] || String(check.id || 'Unspecified check').replace(/_/g, ' ');
      var item = el('li', 'bv-plan-check'); item.dataset.state = status;
      var head = el('div', 'bv-plan-check__head');
      head.append(el('strong', '', label),
        el('span', 'bv-plan-check__status', { passed: 'Passed', blocked: 'Blocked', unknown: 'Unknown' }[status]));
      item.append(head, el('p', 'bv-note', check.detail || 'No details recorded.'));
      checks.append(item);
    });
    planBody.append(checks);
    var blockers = el('section', 'bv-plan-blockers');
    blockers.append(el('h4', '', 'Before live recovery'));
    var reasons = el('ul');
    var text = plan.blocking_reasons.length ? plan.blocking_reasons : ['Live apply is unavailable. A checked plan does not authorize a game-data write.'];
    text.forEach(function (reason) { reasons.append(el('li', '', reason)); });
    blockers.append(reasons); planBody.append(blockers);
    var lab = plan.lab || {};
    var labText = lab.status === 'passed' ? 'Passed' : lab.status === 'failed' ? 'Failed' : 'Not run';
    planBody.append(el('p', 'bv-note', 'Isolated database restore check: ' + labText + (lab.completed_at ? ' / ' + stamp(lab.completed_at) : '') + '. This does not verify in-game recovery.'));
    updatePlanFreshness();
    planningBusy(false);
  }
  function updatePlanFreshness() {
    if (!restorePlan || !dialog.open) return;
    var freshness = planFreshness(restorePlan);
    if (freshness === 'stale') {
      planMessage('Plan expired. Recheck snapshot to create a new plan with fresh checks. Live apply remains unavailable.', 'stale');
    } else if (freshness === 'unknown') {
      planMessage('Plan freshness is unknown. Recheck snapshot before relying on these checks. Live apply remains unavailable.', 'unknown');
    } else {
      var blocked = restorePlan.blocking_reasons.length || restorePlan.checks.some(function (check) { return check.status !== 'passed'; });
      planMessage(blocked ? 'Plan checked with blockers. Review the source, target and lifecycle requirements below.' : 'Plan checked. Live apply remains unavailable pending the controlled recovery workflow.', blocked ? 'blocked' : 'checked');
      expiryTimer = setTimeout(updatePlanFreshness, Math.min(30000, Math.max(1, Date.parse(restorePlan.expires_at) - Date.now())));
    }
  }
  function renderPlanIndex(data) {
    var plans = Array.isArray(data.restore_plans) ? data.restore_plans : [];
    planSelect.replaceChildren(new Option('Choose a plan', ''));
    plans.filter(function (plan) { return uuid(plan.plan_id); }).forEach(function (plan) {
      planSelect.append(new Option(stamp(plan.created_at) + ' / ' + plan.plan_id.slice(0, 8), plan.plan_id));
    });
    document.getElementById('bv-plan-picker').hidden = planSelect.options.length < 2;
    if (restorePlan) {
      if (!Array.from(planSelect.options).some(function (option) { return option.value === restorePlan.plan_id; })) {
        planSelect.append(new Option(stamp(restorePlan.created_at) + ' / ' + restorePlan.plan_id.slice(0, 8), restorePlan.plan_id));
      }
      planSelect.value = restorePlan.plan_id;
      document.getElementById('bv-plan-picker').hidden = false;
    }
    if (!restorePlan && !pendingPlan && data.restore_planning_enabled !== true) planMessage('Restore planning is unavailable. Snapshot history remains readable.', 'unavailable');
  }
  async function readPlan(planId) {
    if (!uuid(planId) || restoreBusy) return;
    var ticket = ++restoreGeneration, drawer = generation;
    clearTimeout(expiryTimer);
    restorePlan = null;
    selectedPlanId = planId;
    clear(planBody);
    planningBusy(true);
    planMessage('Loading restore plan...', 'loading');
    try {
      var plan = await read(path(current) + '/restore-plans/' + encodeURIComponent(planId));
      if (ticket !== restoreGeneration || drawer !== generation) return;
      if (plan.plan_id !== planId) throw new Error('Wrong plan');
      renderPlan(plan, snapshots[plan.snapshot_id]);
    } catch (_) {
      if (ticket !== restoreGeneration || drawer !== generation) return;
      planMessage('Restore plan could not be verified or loaded. Live apply remains unavailable. Refresh the plan to check again.', 'unknown');
    } finally {
      if (ticket === restoreGeneration && drawer === generation) planningBusy(false);
    }
  }
  async function stagePlan(snapshotId) {
    var expected = snapshots[snapshotId];
    if (!expected || restoreBusy || pendingPlan || !state || state.restore_planning_enabled !== true) return;
    var ticket = ++restoreGeneration, drawer = generation, totem = current;
    var request;
    try { request = crypto.randomUUID(); } catch (_) { planMessage('A secure browser is required to stage a restore plan.', 'unknown'); return; }
    pendingPlan = { request_id: request, snapshot: expected };
    restorePlan = null;
    selectedPlanId = null;
    planSelect.value = '';
    clearTimeout(expiryTimer);
    clear(planBody);
    planningBusy(true);
    planMessage('Checking snapshot ' + snapshotId + ' for original-location recovery. No game-data changes are requested.', 'loading');
    recovery.scrollIntoView({ block: 'start' });
    planStatus.focus();
    var timeout, submitted = false;
    try {
      await fetchCsrf();
      submitted = true;
      var plan = await Promise.race([
        apiCall('POST', path(totem) + '/restore-plans', { request_id: request, snapshot_id: snapshotId }),
        new Promise(function (_, reject) { timeout = setTimeout(function () { reject(new Error('Plan request timed out')); }, 30000); })
      ]);
      if (ticket !== restoreGeneration || drawer !== generation) return;
      if (plan.request_id !== request) throw new Error('Wrong request');
      renderPlan(plan, expected);
      pendingPlan = null;
      planRefresh.disabled = false;
    } catch (error) {
      if (ticket !== restoreGeneration || drawer !== generation) return;
      if (!submitted || error.status >= 400 && error.status < 500) {
        pendingPlan = null;
        planMessage('Staging was refused or could not be authorized. No plan was confirmed. Refresh vault status before trying again.', 'blocked');
      } else {
        planMessage('Staging could not be confirmed. Refresh plan to look for this request. No live restore was requested.', 'unknown');
      }
    } finally {
      clearTimeout(timeout);
      if (ticket === restoreGeneration && drawer === generation) planningBusy(false);
    }
  }

  planSelect.addEventListener('change', function () { readPlan(planSelect.value); });
  planRestage.addEventListener('click', function () {
    if (restoreBusy || pendingPlan || !restorePlan) return;
    var snapshotId = restorePlan.snapshot_id;
    if (!snapshots[snapshotId]) snapshots[snapshotId] = { snapshot_id: snapshotId, payload_sha256: restorePlan.source.payload_sha256 };
    stagePlan(snapshotId);
  });
  planRefresh.addEventListener('click', async function () {
    if (restoreBusy) return;
    if (!pendingPlan) { readPlan(selectedPlanId); return; }
    var ticket = ++restoreGeneration, drawer = generation;
    planningBusy(true);
    planMessage('Looking for the existing staging request...', 'loading');
    try {
      var data = await read(path(current));
      if (ticket !== restoreGeneration || drawer !== generation) return;
      if (data.available !== true || String(data.totem_id) !== current) throw new Error('Wrong target');
      var plan = (data.restore_plans || []).find(function (row) { return row.request_id === pendingPlan.request_id; });
      if (!plan) {
        planMessage('No completed plan is recorded for this request yet. Check again before staging another plan. No live restore was requested.', 'unknown');
        return;
      }
      renderPlan(plan, pendingPlan.snapshot);
      pendingPlan = null;
      renderPlanIndex(data);
    } catch (_) {
      if (ticket === restoreGeneration && drawer === generation) planMessage('The staging request could not be checked. Live apply remains unavailable.', 'unknown');
    } finally {
      if (ticket === restoreGeneration && drawer === generation) planningBusy(false);
    }
  });

  function operationStatus(data) {
    var op = data.operation;
    var pending = unconfirmed[current];
    if (pending && op && op.request_id === pending && ['completed', 'failed'].indexOf(op.status) !== -1) {
      delete unconfirmed[current];
      remember();
      pending = null;
    }
    var running = op && ['pending', 'running'].indexOf(op.status) !== -1;
    var globallyBlocked = ['pending', 'running', 'uncertain'].indexOf(data.capture_blocked) !== -1;
    var uncertain = pending && (!op || op.request_id !== pending) || op && ['pending', 'running', 'completed', 'failed'].indexOf(op.status) === -1;
    capture.disabled = loading || !data.capture_allowed || running || !!uncertain || globallyBlocked;
    capture.textContent = running ? 'Capturing snapshot...' : 'Take snapshot';
    var operation = document.getElementById('bv-operation');
    operation.textContent = op ? 'Operation ' + op.request_id + ' / started ' + stamp(op.started_at) : '';
    if (uncertain) {
      message('Capture outcome is uncertain. Further capture is blocked. Check status or ask an operator to review the existing operation.', 'uncertain');
    } else if (running) {
      message('Capture is in progress. The game keeps running. This panel checks the existing operation automatically.', 'running');
    } else if (globallyBlocked) {
      message(data.capture_blocked === 'uncertain'
        ? 'Another capture has an uncertain outcome. Further capture is blocked for operator review.'
        : 'Another base capture is in progress. Capture will become available after that operation finishes.', data.capture_blocked);
    } else if (op && op.status === 'completed') {
      message('Snapshot captured and archive verified. The base remains in the game world.', 'completed');
    } else if (op && op.status === 'failed') {
      message('Capture did not complete. No verified snapshot was recorded for this operation. Review the operation before trying again.', 'failed');
    } else if (!data.capture_allowed) {
      message('Capture is unavailable for this base. You can still inspect its recorded history.', 'unavailable');
    } else if (refused[current]) {
      message('The last capture request was refused or could not be authorized. Status has been refreshed; no capture was confirmed.', 'failed');
    } else {
      message('Ready to capture. Ownership and supported references are checked again during capture.', 'ready');
    }
    return running || !!uncertain || globallyBlocked;
  }

  function queuePoll(ticket) {
    clearTimeout(timer);
    if (dialog.open && ticket === generation) timer = setTimeout(function () { load(false); }, 3000);
  }
  async function read(url) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 20000);
    try {
      var response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('Request failed');
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  async function load(initial) {
    if (loading || !current) return;
    var ticket = generation;
    loading = true;
    refresh.disabled = true;
    capture.disabled = true;
    history.setAttribute('aria-busy', 'true');
    if (initial) { skeleton(history); message('Loading snapshot history...', 'loading'); }
    try {
      var data = await read(path(current));
      if (ticket !== generation) return;
      if (data.available !== true || String(data.totem_id) !== current || !Array.isArray(data.snapshots)) throw new Error('Invalid response');
      state = data;
      renderContext(data.base);
      renderSnapshots(data);
      renderPlanIndex(data);
      loading = false;
      if (operationStatus(data)) queuePoll(ticket);
    } catch (_) {
      if (ticket !== generation) return;
      state = null;
      message('Vault status could not be loaded. Capture stays unavailable until a successful status check.' + (initial ? '' : ' Displayed history is from the previous read.'), 'unavailable');
      if (initial) empty(history, 'History unavailable', 'Check status to try reading the vault again. This does not request a capture.');
      if (unconfirmed[current]) queuePoll(ticket);
    } finally {
      if (ticket === generation) {
        loading = false;
        refresh.disabled = false;
        history.setAttribute('aria-busy', 'false');
      }
    }
  }

  function open(totem, trigger) {
    totem = id(totem);
    if (!totem) return;
    clearTimeout(timer);
    generation += 1;
    current = totem;
    state = null;
    loading = false;
    resetRestore();
    opener = trigger || document.activeElement;
    document.getElementById('bv-title').textContent = 'Totem ' + current;
    clear(context);
    document.getElementById('bv-operation').textContent = '';
    document.getElementById('bv-history-count').textContent = '';
    if (!dialog.open) dialog.showModal();
    document.getElementById('bv-close').focus();
    load(true);
  }
  dialog.addEventListener('close', function () {
    generation += 1;
    clearTimeout(timer);
    loading = false;
    restoreGeneration += 1;
    clearTimeout(expiryTimer);
    if (opener && opener.isConnected) opener.focus();
  });
  document.getElementById('bv-close').addEventListener('click', function () { dialog.close(); });
  refresh.addEventListener('click', function () { load(false); });

  capture.addEventListener('click', async function () {
    if (capture.disabled || !state || !state.capture_allowed || loading) return;
    var totem = current, ticket = generation;
    var request;
    try { request = crypto.randomUUID(); } catch (_) { message('This browser cannot create a capture request. Use a current browser over HTTPS.', 'failed'); return; }
    unconfirmed[totem] = request;
    delete refused[totem];
    remember();
    loading = true;
    capture.disabled = true;
    refresh.disabled = true;
    capture.textContent = 'Requesting snapshot...';
    message('Submitting one capture request. Waiting for the vault to acknowledge it.', 'running');
    var submitted = false, acknowledgementTimeout;
    try {
      await fetchCsrf();
      submitted = true;
      var result = await Promise.race([
        apiCall('POST', path(totem) + '/snapshots', { request_id: request }),
        new Promise(function (_, reject) {
          acknowledgementTimeout = setTimeout(function () { reject(new Error('Acknowledgement timed out')); }, 20000);
        })
      ]);
      if (!result.operation || result.operation.request_id !== request) throw new Error('Unconfirmed request');
    } catch (error) {
      if (!submitted || error.status >= 400 && error.status < 500) {
        delete unconfirmed[totem];
        refused[totem] = true;
        remember();
      }
      if (ticket === generation) message(submitted ? 'Checking whether the vault accepted this request. No second capture will be submitted.' : 'The request could not be authorized. Check status before trying again.', 'uncertain');
    } finally {
      clearTimeout(acknowledgementTimeout);
      if (ticket === generation) {
        loading = false;
        refresh.disabled = false;
        load(false);
      }
    }
  });

  window.BaseVault = { open: open };
  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-base-vault]');
    if (button) open(button.dataset.baseVault, button);
  });

  var directoryRoot = document.getElementById('bv-directory');
  if (!directoryRoot) return;
  var list = document.getElementById('bv-directory-list');
  var search = document.getElementById('bv-search');
  var maps = document.getElementById('bv-map-filter');
  var directoryStatus = document.getElementById('bv-directory-status');
  var directoryRefresh = document.getElementById('bv-directory-refresh');
  var directoryMeta = '';
  function renderDirectory() {
    var needle = search.value.trim().toLowerCase();
    var rows = directory.filter(function (base) {
      return (!maps.value || mapName(base) === maps.value) &&
        (!needle || [base.label, base.totem_id, base.owner && base.owner.name, base.owner && base.owner.account_id].join(' ').toLowerCase().indexOf(needle) !== -1);
    });
    directoryStatus.textContent = rows.length + ' of ' + directory.length + ' placed bases. ' + directoryMeta;
    if (!rows.length) {
      empty(list, directory.length ? 'No matching bases' : 'No placed bases found', directory.length ? 'Try a different name, totem or map instance.' : 'The directory contains no placed claims. Packed BRT backups appear in Backups.');
      return;
    }
    var wrap = el('div', 'bv-table-wrap'), table = el('table', 'bv-table');
    var head = el('thead'), headings = el('tr');
    ['Base / totem', 'Owner / map', 'Saved composition', 'Snapshot history'].forEach(function (title) {
      var th = el('th', '', title); th.scope = 'col'; headings.append(th);
    });
    head.append(headings); table.append(head);
    var body = el('tbody');
    rows.forEach(function (base) {
      var tr = el('tr'), name = el('td'), owner = el('td'), composition = el('td'), action = el('td');
      name.append(el('strong', '', base.label || 'Unnamed base'), el('span', 'bv-meta', 'Totem ' + base.totem_id));
      owner.append(el('span', '', base.owner && base.owner.name || 'No owner on record'), el('span', 'bv-meta', mapName(base)));
      composition.append(el('span', '', count(base.pieces) + ' pieces'), el('span', 'bv-meta', count(base.placeables) + ' placeables'));
      var button = el('button', 'btn btn-sm', 'Open vault');
      button.type = 'button'; button.dataset.baseVault = base.totem_id;
      button.setAttribute('aria-label', 'Open vault for ' + (base.label || 'totem ' + base.totem_id));
      action.append(button); tr.append(name, owner, composition, action); body.append(tr);
    });
    table.append(body); wrap.append(table); list.replaceChildren(wrap);
  }
  async function loadDirectory() {
    if (directoryLoading) return;
    directoryLoading = true;
    directoryRefresh.disabled = true;
    list.setAttribute('aria-busy', 'true');
    directoryStatus.textContent = 'Loading placed bases...';
    skeleton(list);
    try {
      var data = await read('/admin/api/dune/v2/bases');
      if (data.available !== true || !Array.isArray(data.bases)) throw new Error('Directory unavailable');
      directory = data.bases.filter(function (base) { return base.ownership !== 'stored_backup' && id(base.totem_id); });
      var selected = maps.value;
      maps.replaceChildren(new Option('All map instances', ''));
      Array.from(new Set(directory.map(mapName))).sort().forEach(function (name) { maps.append(new Option(name, name)); });
      maps.value = Array.from(maps.options).some(function (option) { return option.value === selected; }) ? selected : '';
      directoryMeta = data.stale ? 'Cached directory: latest refresh failed. Capture rechecks the target.' : 'Directory may be cached for 5 minutes. Capture rechecks the target.';
      renderDirectory();
    } catch (_) {
      directory = [];
      directoryStatus.textContent = 'Directory unavailable.';
      empty(list, 'Placed bases could not be loaded', 'Refresh the directory to try again. Existing archives remain in the vault.');
    } finally {
      directoryLoading = false;
      directoryRefresh.disabled = false;
      list.setAttribute('aria-busy', 'false');
    }
  }
  search.addEventListener('input', renderDirectory);
  maps.addEventListener('change', renderDirectory);
  directoryRefresh.addEventListener('click', loadDirectory);
  document.getElementById('bv-lookup').addEventListener('submit', function (event) {
    event.preventDefault();
    var input = document.getElementById('bv-lookup-totem');
    if (id(input.value.trim())) open(input.value.trim(), event.submitter || input);
  });
  loadDirectory();
})();
