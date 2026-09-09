# Configuration changes in the September refresh

This refresh removes deployment-specific defaults and unreviewed generated
inputs. It does not change any running deployment automatically. Review these
requirements before replacing an installed script.

| Component | Required local configuration |
|---|---|
| Forced-command relay dispatcher | LASTSIETCH_RELAY_ALLOWED_IPS, set on the game host, not assumed to forward from an SSH client |
| Chat-send bridge | CIELAGO_HOST_ID for the deployment's sender account |
| Welcome whispers | CIELAGO_HOST_ID and CIELAGO_FUNCOM_ID; unconfigured optional whispers are skipped |
| Command and broadcast wrappers | --token-file or DUNE_COMMAND_AUTH_TOKEN; no embedded credential fallback |
| Update orchestrator | DUNE_UPD_ENABLE=1, explicit host/namespace/build/window, owner and channel settings when notifications are enabled |
| Hotfix notification | DUNE_HFW_OWNER_ID, or disable DUNE_HFW_DM_ENABLE |
| Message capture loop | MQGIP from the operator's own service inventory |
| Storage MOVE | LASTSIETCH_STORAGE_MOVE_ENABLED=1 only after reviewing and validating the write path; default is off |
| Augment compatibility | DUNE_AUGMENT_CATALOG pointing to a reviewed local catalog |
| Item icon builder | DUNE_ITEM_ICON_MAP and optional DUNE_TEXTURE_ROOTS, separated by the platform path separator |
| Map icon builder | DUNE_TEXTURE_ROOT |
| Name builders and checks | DUNE_STRING_TABLE_JSON, DUNE_ITEM_NAME_SOURCE or the existing explicit source argument |

The augment and icon data exports are no longer bundled. Missing compatibility
data must not be treated as proven compatibility. Generated art, catalogs and
sidecars stay local. The minimal volume reference contains only the currency
zero-volume convention, not an extracted volume dataset. The legacy storage
path's unknown-volume audit behavior is not a capacity guarantee; review it
before enabling MOVE.

One-off event, backfill and notification helpers are not part of a reusable
deployment. Their removal is not a claim that prior public history was erased.

There is no turnkey public production rolling controller in this release. The
maintenance guide describes required safety boundaries, while the new clock
helper is read-only and has no restart capability.
