/* ============================================================================
 * V2 Player Moderation: Kick / Ban / Unban modal handlers.
 *
 * Mirrors the whisper modal pattern from v2-player.js (open/close, apiCall,
 * showMsg/hideMsg from base.html). Each action gates on a typed-CONFIRM token
 * (KICK / BAN / UNBAN) so a stray click cannot fire a destructive action.
 *
 * Endpoints (admin + CSRF gated server-side):
 *   POST /admin/api/dune/v2/player/{aid}/_kick   { reason, confirm_token: 'KICK' }
 *   POST /admin/api/dune/v2/player/{aid}/_ban    { reason, note?, duration_minutes?, confirm_token: 'BAN' }
 *   POST /admin/api/dune/v2/player/{aid}/_unban  { unban_reason, confirm_token: 'UNBAN' }
 * ============================================================================ */
(function () {
  var CONFIRM_TOKENS = { kick: 'KICK', ban: 'BAN', unban: 'UNBAN' };

  function $(id) { return document.getElementById(id); }

  function getContext() {
    var actions = $('v2-player-actions');
    if (!actions) return null;
    return {
      accountId: actions.getAttribute('data-account-id'),
      playerName: actions.getAttribute('data-player-name') || ('account #' + actions.getAttribute('data-account-id')),
    };
  }

  function closeV2Mod(kind) {
    var o = $('v2-' + kind + '-overlay');
    if (o) o.classList.remove('active');
  }
  window.closeV2Mod = closeV2Mod;

  function resetModalInputs(kind) {
    var ids = {
      kick: ['v2-kick-reason', 'v2-kick-confirm'],
      ban: ['v2-ban-reason', 'v2-ban-duration', 'v2-ban-note', 'v2-ban-confirm'],
      unban: ['v2-unban-reason', 'v2-unban-confirm'],
    }[kind] || [];
    for (var i = 0; i < ids.length; i++) {
      var el = $(ids[i]);
      if (el) el.value = '';
    }
    hideMsg('v2-' + kind + '-msg');
    var send = $('v2-' + kind + '-send');
    if (send) send.disabled = true;
  }

  function openV2Mod(kind) {
    var ctx = getContext();
    if (!ctx) return;
    resetModalInputs(kind);
    var label = { kick: 'Kick', ban: 'Ban', unban: 'Unban' }[kind] || kind;
    var title = $('v2-' + kind + '-title');
    if (title) title.textContent = label + ' ' + ctx.playerName;
    var target = $('v2-' + kind + '-target');
    if (target) target.textContent = 'Target: ' + ctx.playerName + ' (account #' + ctx.accountId + ')';
    var overlay = $('v2-' + kind + '-overlay');
    if (overlay) overlay.classList.add('active');
    setTimeout(function () {
      var firstField = $('v2-' + kind + '-reason') || $('v2-' + kind + (kind === 'unban' ? '-reason' : '-reason'));
      if (firstField) firstField.focus();
    }, 60);
  }

  function tokenMatches(kind) {
    var el = $('v2-' + kind + '-confirm');
    if (!el) return false;
    return (el.value || '').trim() === CONFIRM_TOKENS[kind];
  }

  function refreshSendState(kind) {
    var send = $('v2-' + kind + '-send');
    if (!send) return;
    var reasonId = kind === 'unban' ? 'v2-unban-reason' : 'v2-' + kind + '-reason';
    var reason = ($(reasonId) && $(reasonId).value.trim()) || '';
    var ok = reason.length > 0 && tokenMatches(kind);
    send.disabled = !ok;
  }

  function readBanDuration() {
    var el = $('v2-ban-duration');
    if (!el) return null;
    var raw = (el.value || '').trim();
    if (!raw) return null;
    var n = parseInt(raw, 10);
    if (!isFinite(n) || n < 1) return null;
    return n;
  }

  function submit(kind) {
    var ctx = getContext();
    if (!ctx) return;
    var msgId = 'v2-' + kind + '-msg';
    hideMsg(msgId);

    var reasonField = kind === 'unban' ? 'v2-unban-reason' : 'v2-' + kind + '-reason';
    var reason = ($(reasonField) && $(reasonField).value.trim()) || '';
    if (!reason) {
      showMsg(msgId, 'Reason is required.', 'error');
      return;
    }
    if (!tokenMatches(kind)) {
      showMsg(msgId, 'Type ' + CONFIRM_TOKENS[kind] + ' to confirm.', 'error');
      return;
    }

    var payload;
    if (kind === 'kick') {
      payload = { reason: reason, confirm_token: CONFIRM_TOKENS.kick };
    } else if (kind === 'ban') {
      var note = ($('v2-ban-note') && $('v2-ban-note').value.trim()) || null;
      var duration = readBanDuration();
      payload = {
        reason: reason,
        note: note,
        duration_minutes: duration,
        confirm_token: CONFIRM_TOKENS.ban,
      };
    } else {
      payload = { unban_reason: reason, confirm_token: CONFIRM_TOKENS.unban };
    }

    var sendBtn = $('v2-' + kind + '-send');
    if (sendBtn) sendBtn.disabled = true;
    showMsg(msgId, 'Submitting...', 'info');

    var path = '/admin/api/dune/v2/player/' + encodeURIComponent(ctx.accountId) + '/_' + kind;
    apiCall('POST', path, payload).then(function (d) {
      var ok = d && d.success;
      var detail = (d && (d.detail || d.message)) || '';
      if (ok) {
        showMsg(msgId, kind.charAt(0).toUpperCase() + kind.slice(1) + ' applied.', 'success');
        setTimeout(function () { closeV2Mod(kind); }, 800);
      } else {
        showMsg(msgId, 'Failed: ' + (detail || 'unknown error'), 'error');
        if (sendBtn) sendBtn.disabled = false;
      }
    }).catch(function (e) {
      showMsg(msgId, 'Error: ' + (e && e.message ? e.message : String(e)), 'error');
      if (sendBtn) sendBtn.disabled = false;
    });
  }

  /* Wire input listeners on the static modal fields (present at full-page
     render; HTMX never swaps the modals). */
  ['kick', 'ban', 'unban'].forEach(function (kind) {
    var reasonId = kind === 'unban' ? 'v2-unban-reason' : 'v2-' + kind + '-reason';
    var confirmId = 'v2-' + kind + '-confirm';
    var reasonEl = $(reasonId);
    var confirmEl = $(confirmId);
    if (reasonEl) reasonEl.addEventListener('input', function () { refreshSendState(kind); });
    if (confirmEl) confirmEl.addEventListener('input', function () { refreshSendState(kind); });
  });

  document.addEventListener('click', function (e) {
    var kickBtn = e.target.closest('#v2-action-kick');
    if (kickBtn) { e.preventDefault(); openV2Mod('kick'); return; }
    var banBtn = e.target.closest('#v2-action-ban');
    if (banBtn) { e.preventDefault(); openV2Mod('ban'); return; }
    var unbanBtn = e.target.closest('#v2-action-unban');
    if (unbanBtn) { e.preventDefault(); openV2Mod('unban'); return; }

    var kickSend = e.target.closest('#v2-kick-send');
    if (kickSend) { e.preventDefault(); submit('kick'); return; }
    var banSend = e.target.closest('#v2-ban-send');
    if (banSend) { e.preventDefault(); submit('ban'); return; }
    var unbanSend = e.target.closest('#v2-unban-send');
    if (unbanSend) { e.preventDefault(); submit('unban'); return; }

    // Quick-reason presets: fill the reason (+ duration for ban) and refresh
    // the send-button state. Dispatching 'input' triggers the existing listener.
    var preset = e.target.closest('.v2-mod-preset');
    if (preset) {
      e.preventDefault();
      var kind = preset.getAttribute('data-target');
      var reasonEl = $('v2-' + kind + '-reason');
      if (reasonEl) {
        reasonEl.value = preset.getAttribute('data-reason') || '';
        reasonEl.dispatchEvent(new Event('input', { bubbles: true }));
      }
      if (kind === 'ban') {
        var durEl = $('v2-ban-duration');
        if (durEl) {
          durEl.value = preset.getAttribute('data-duration') || '';
          durEl.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
      if (reasonEl) reasonEl.focus();
      return;
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    ['kick', 'ban', 'unban'].forEach(function (kind) {
      var overlay = $('v2-' + kind + '-overlay');
      if (overlay && overlay.classList.contains('active')) closeV2Mod(kind);
    });
  });
})();
