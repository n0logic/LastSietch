-- Move a parked base backup to a new origin: the stored origin AND every actor and building piece,
-- by one offset, so the BRT ghost is built where the origin is. Setting the origin alone leaves the
-- pieces at their old coordinates (backup 112, 2026-10-28: no outline, "Not enough Support").
-- Rotation is unchanged. Only the backup's own linked rows, and only while all of them are parked.
--   psql -v ON_ERROR_STOP=1 -v bid=112 -v x=179377 -v y=-98644 -v z=2199 -v db=dune -f relocate.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('relocate.bid', :'bid', true), set_config('relocate.x', :'x', true),
       set_config('relocate.y', :'y', true), set_config('relocate.z', :'z', true),
       set_config('relocate.db', :'db', true);
DO $$
DECLARE
  bid bigint := current_setting('relocate.bid')::bigint;
  tid bigint; o real[]; lo int; dx float8; dy float8; dz float8;
  nlinks int; nparked int; nbi int; na int; nb int;
BEGIN
  IF current_database() <> current_setting('relocate.db') THEN
    RAISE EXCEPTION 'relocate_wrong_database %', current_database();
  END IF;
  SELECT t.id, t.landclaim_original_global_location INTO STRICT tid, o
    FROM dune.totems t JOIN dune.base_backup_linked_actors l ON l.actor_id = t.id WHERE l.id = bid;
  lo := array_lower(o, 1);
  dx := current_setting('relocate.x')::float8 - o[lo];
  dy := current_setting('relocate.y')::float8 - o[lo+1];
  dz := current_setting('relocate.z')::float8 - o[lo+2];

  SELECT count(*), count(*) FILTER (WHERE a.state = 'BaseBackup') INTO nlinks, nparked
    FROM dune.base_backup_linked_actors l JOIN dune.actors a ON a.id = l.actor_id WHERE l.id = bid;
  IF nlinks = 0 OR nparked <> nlinks THEN
    RAISE EXCEPTION 'relocate_not_parked links=% parked=%', nlinks, nparked;
  END IF;
  SELECT count(*) INTO nbi
    FROM dune.building_instances bi JOIN dune.base_backup_linked_actors l ON l.actor_id = bi.building_id WHERE l.id = bid;

  UPDATE dune.totems SET
      landclaim_original_global_location[lo]   = o[lo]   + dx,
      landclaim_original_global_location[lo+1] = o[lo+1] + dy,
      landclaim_original_global_location[lo+2] = o[lo+2] + dz
    WHERE id = tid;
  UPDATE dune.actors a SET
      transform.location.x = (a.transform).location.x + dx,
      transform.location.y = (a.transform).location.y + dy,
      transform.location.z = (a.transform).location.z + dz
    FROM dune.base_backup_linked_actors l WHERE l.actor_id = a.id AND l.id = bid;
  GET DIAGNOSTICS na = ROW_COUNT;
  -- Game-written rows use both 0-based and 1-based arrays; offset from each row's own lower bound.
  UPDATE dune.building_instances bi SET
      transform[array_lower(bi.transform,1)]   = bi.transform[array_lower(bi.transform,1)]   + dx,
      transform[array_lower(bi.transform,1)+1] = bi.transform[array_lower(bi.transform,1)+1] + dy,
      transform[array_lower(bi.transform,1)+2] = bi.transform[array_lower(bi.transform,1)+2] + dz
    FROM dune.base_backup_linked_actors l WHERE l.actor_id = bi.building_id AND l.id = bid;
  GET DIAGNOSTICS nb = ROW_COUNT;
  IF na <> nlinks OR nb <> nbi THEN
    RAISE EXCEPTION 'relocate_row_count actors=%/% pieces=%/%', na, nlinks, nb, nbi;
  END IF;
  RAISE NOTICE 'relocated backup % totem % by (%, %, %): actors=% pieces=%', bid, tid, dx, dy, dz, na, nb;
END $$;
COMMIT;
