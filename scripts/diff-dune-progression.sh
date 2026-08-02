#!/usr/bin/env bash
# Snapshot the operator's Dune progression state and diff against the most recent
# pre-snapshot. Use after earning a skill point, intel point, claim-rewards
# delivery, or any progression event you want to RE.
#
# Usage:
#   ./diff-dune-progression.sh snapshot           # take a new snapshot, return its path
#   ./diff-dune-progression.sh diff <pre> <post>  # diff two named snapshot dirs
#   ./diff-dune-progression.sh diff <pre>         # snapshot now + diff vs <pre>

set -euo pipefail

NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
POD="${POD:-sh-<your-hostid>-<random>-db-dbdepl-sts-0}"
ACCT=2
ACTOR=19

ACTION="${1:-diff}"

snapshot() {
  local label="${1:-post}"
  ssh lastsietch-dune "
    set -e
    PGPASS=\$(sudo kubectl exec -n $NS $POD -- printenv POSTGRES_PASSWORD)
    TS=\$(date -u +%Y%m%dT%H%M%SZ)
    DIR=/root/dune-snapshot-${label}-\$TS
    sudo mkdir -p \$DIR
    D() { sudo kubectl exec -n $NS $POD -- env PGPASSWORD=\$PGPASS psql -h localhost -p 15432 -U postgres -d dune -c \"\$2\" | sudo tee \$DIR/\$1.txt > /dev/null; }
    D '01_specialization_tracks'      'SELECT * FROM dune.specialization_tracks WHERE player_id=$ACTOR ORDER BY track_type;'
    D '02_purchased_keystones'        'SELECT * FROM dune.purchased_specialization_keystones WHERE player_id=$ACTOR ORDER BY keystone_id;'
    D '03_specialization_refund_id'   'SELECT * FROM dune.specialization_refund_id WHERE player_id=$ACTOR;'
    D '04_mnemonic_recall'            'SELECT id, account_id, lesson_id, lesson_state, lesson_progress, is_new FROM dune.mnemonic_recall WHERE account_id=$ACCT ORDER BY lesson_id;'
    D '05_journey_story_node_count'   'SELECT COUNT(*) FROM dune.journey_story_node WHERE account_id=$ACCT;'
    D '06_journey_story_node_pending' 'SELECT account_id, story_node_id, has_pending_reward, override_reward_block FROM dune.journey_story_node WHERE account_id=$ACCT AND has_pending_reward=true ORDER BY story_node_id;'
    D '07_journey_story_node_full'    'SELECT account_id, story_node_id, has_pending_reward FROM dune.journey_story_node WHERE account_id=$ACCT ORDER BY story_node_id;'
    D '08_currency_balances'          'SELECT * FROM dune.player_virtual_currency_balances WHERE player_controller_id IN (SELECT player_controller_id FROM dune.encrypted_player_state WHERE account_id=$ACCT);'
    D '09_dune_exchange_users'        'SELECT * FROM dune.dune_exchange_users WHERE owner_id=$ACTOR;'
    D '10_encrypted_player_state'     'SELECT account_id, player_controller_id, player_pawn_id, online_status, last_login_time, last_returning_player_event_time, last_returning_player_awarded_time FROM dune.encrypted_player_state WHERE account_id=$ACCT;'
    D '11_player_faction_reputation'  'SELECT * FROM dune.player_faction_reputation WHERE actor_id=$ACTOR ORDER BY faction_id;'
    D '12_player_faction'             'SELECT * FROM dune.player_faction WHERE actor_id=$ACTOR;'
    D '13_building_progression'       'SELECT * FROM dune.building_progression WHERE account_id=$ACCT;'
    D '14_journey_tracked_cards'      'SELECT * FROM dune.journey_tracked_cards WHERE player_id=$ACTOR;'
    D '15_actor_pawn_props'           'SELECT jsonb_pretty(properties) FROM dune.actors WHERE id=$ACTOR;'
    D '16_actor_controller_props'     'SELECT jsonb_pretty(properties) FROM dune.actors WHERE id IN (SELECT player_controller_id FROM dune.encrypted_player_state WHERE account_id=$ACCT);'
    D '17_tech_knowledge_points'      'SELECT id, properties#>'\''{TechKnowledgePlayerComponent,m_TechKnowledgePoints}'\'' AS intel_points, properties#>'\''{TechKnowledgePlayerComponent,m_NextTechTreeUpgradeIndex}'\'' AS next_tech_idx FROM dune.actors WHERE id=$ACTOR;'
    D '18_quarantined_rewards'        'SELECT id, jsonb_pretty(properties#>'\''{BP_DunePlayerCharacter_C,m_QuarantinedPlayerRewards}'\'') FROM dune.actors WHERE id=$ACTOR;'
    D '19_actor_state_props'          'SELECT jsonb_pretty(properties) FROM dune.actors WHERE id IN (SELECT player_state_id FROM dune.encrypted_player_state WHERE account_id=$ACCT);'
    D '20_actor_pawn_flat'            'WITH RECURSIVE keys AS (SELECT key::text AS path, value FROM dune.actors, jsonb_each(properties) WHERE id=$ACTOR UNION ALL SELECT k.path||'\''.'\''||sub.key, sub.value FROM keys k, jsonb_each(k.value) sub WHERE jsonb_typeof(k.value)=$$object$$) SELECT path, jsonb_typeof(value), CASE WHEN jsonb_typeof(value) IN ($$string$$, $$number$$, $$boolean$$) THEN value::text WHEN jsonb_typeof(value)=$$array$$ THEN $$<arr len $$||jsonb_array_length(value)||$$>$$ ELSE NULL END AS val FROM keys WHERE jsonb_typeof(value) != $$object$$ ORDER BY path;'
    D '21_actor_controller_flat'      'WITH RECURSIVE keys AS (SELECT key::text AS path, value FROM dune.actors, jsonb_each(properties) WHERE id IN (SELECT player_controller_id FROM dune.encrypted_player_state WHERE account_id=$ACCT) UNION ALL SELECT k.path||'\''.'\''||sub.key, sub.value FROM keys k, jsonb_each(k.value) sub WHERE jsonb_typeof(k.value)=$$object$$) SELECT path, jsonb_typeof(value), CASE WHEN jsonb_typeof(value) IN ($$string$$, $$number$$, $$boolean$$) THEN value::text WHEN jsonb_typeof(value)=$$array$$ THEN $$<arr len $$||jsonb_array_length(value)||$$>$$ ELSE NULL END AS val FROM keys WHERE jsonb_typeof(value) != $$object$$ ORDER BY path;'
    D '22_inv14_items'                'SELECT position_index, template_id, stack_size, quality_level FROM dune.items WHERE inventory_id=14 ORDER BY position_index;'
    D '23_encrypted_player_state'     'SELECT account_id, player_controller_id, player_pawn_id, player_state_id, octet_length(encrypted_character_name) AS encname_bytes, online_status, life_state, previous_server_partition_id, return_dimension_index, home_dimension_index FROM dune.encrypted_player_state WHERE account_id=$ACCT;'
    D '24_actor_state'                'SELECT actor_id, state FROM dune.actor_state WHERE actor_id IN ($ACTOR, (SELECT player_controller_id FROM dune.encrypted_player_state WHERE account_id=$ACCT), (SELECT player_state_id FROM dune.encrypted_player_state WHERE account_id=$ACCT));'
    D '25_vendor_stock_state'         'SELECT * FROM dune.vendor_stock_state WHERE player_id=$ACTOR;'
    D '26_dune_exchange_users'        'SELECT * FROM dune.dune_exchange_users WHERE owner_id=$ACTOR;'
    D '27_guild_party'                'SELECT $$guild$$ AS context, gm.guild_id::text AS info FROM dune.guild_members gm WHERE gm.player_id=$ACTOR UNION ALL SELECT $$party$$, pm.party_id::text FROM dune.party_members pm WHERE pm.player_id=$ACTOR;'
    echo \"\$DIR\"
  "
}

case "$ACTION" in
  snapshot)
    snapshot "post"
    ;;
  diff)
    PRE="${2:?usage: $0 diff <pre-snapshot-dir> [post-snapshot-dir]}"
    POST="${3:-}"
    if [[ -z "$POST" ]]; then
      echo "snapshotting current state..."
      POST=$(snapshot "post" | tail -1)
    fi
    echo "=== diff between $PRE and $POST ==="
    ssh lastsietch-dune "
      PRE=$PRE
      POST=$POST
      for f in \$(sudo ls \$PRE/*.txt); do
        name=\$(basename \$f)
        if ! sudo diff -q \$PRE/\$name \$POST/\$name > /dev/null 2>&1; then
          echo '======================================'
          echo \">> \$name CHANGED:\"
          sudo diff -u \$PRE/\$name \$POST/\$name | head -80
          echo ''
        fi
      done
      echo '=== summary: unchanged files ==='
      for f in \$(sudo ls \$PRE/*.txt); do
        name=\$(basename \$f)
        if sudo diff -q \$PRE/\$name \$POST/\$name > /dev/null 2>&1; then
          echo \"  unchanged: \$name\"
        fi
      done
    "
    ;;
  *)
    echo "usage: $0 {snapshot|diff <pre> [<post>]}"
    exit 1
    ;;
esac
