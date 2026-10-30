# Persistent pod replacement watcher

## September 10 planned-renewal correction

Status: deployed and verified September 10 after the owner's approval.
The actual systemd service executed the new watcher successfully; its timer is
active and all six pod UIDs and observation times are populated. Previous start
times and restart counts were preserved. Before-source and state snapshots are
retained in the private deployment stage on the game host.
The portal storage hardening is a separate, completed deployment.

Habbanya renewal `roll-af390abb0fca4359b22d4f6aa6f6f45d` requested its normal
deletion at 10:31:27 Eastern. The replacement started at 10:31:50, and recovery
completed at 10:35:14. Independent readback verified the newer credential,
restored admission and ready Hagga members 1, 32 and 33. Completed receipt hash:
`5c4be40024d7fe007ff433a4eb2c4e65a7193eb65abc0b13f059102620447cbb`.

The original executed watcher, SHA-256
`912e91eb432eec2dff07c43c6ba0d0160a1f823ef7dcf2480315954c1e58b699`,
recognized only recent `ServerRestart` custom resources. The rolling controller
uses an exact-UID pod deletion recorded in its shared maintenance journal.
Consequently this watcher reported no planned record even though the renewal
was recorded and announced.

The correction records pod UID and observation time in the existing watcher
state. For a replacement, `planned-rolling.py` reads the protected journal and
up to eight completed receipts for that target. A match requires:

- The previous observed UID matches the operation's old UID and deletion
  precondition. The new UID differs, and pod name, namespace, ServerSet owner
  and image match the recorded operation.
- The operation reached its persisted deletion intent after its verified notice,
  backup and observed hold. Merely preparing a renewal does not match.
- The previous observation is at most 180 seconds old, and replacement creation
  is within 900 seconds of deletion. Whole-second timestamp rounding gets one
  second of tolerance. A replacement created after the recorded completion is
  refused. Missing identity, stale state, unsafe files and ambiguous matches
  cannot establish a planned renewal.
- A container restart on the replacement is kept visible as a crash. Existing
  native `ServerRestart` recognition and missing/memory alerts remain active.

A verified correlation gets a `PLANNED CHAT RENEWAL` notice with its operation
identifier. Renewal completion remains the controller's responsibility. The
fallback now says that a planned match could not be verified, which also covers
unavailable evidence. Planned notices make no promise about saved progression or
completed recovery. All watcher posts disable mentions.

Legacy state without a UID cannot establish this new match. An ordinary watcher
cycle populates UIDs and observation times without changing the stored starts or
replaying old events. The existing state and all maintenance receipts must be
retained during deployment. No global maintenance mute is needed for attribution.

## Validation and operation

Sixteen tests cover active and completed operations, exact identity, stale and
incomplete evidence, missing owner/image fields, unsafe files and malformed
journals. Tests run the actual shell watcher with fake Kubernetes/Discord
transports for planned renewal, native planned restart, unrelated replacement,
corrupt evidence and a crash on a newly replaced pod. Six CI registration checks,
shell syntax and secret scans pass. No live test restart or test message is used.
Pod JSON travels on stdin; a fixture marker check proves it stays out of helper
and JSON-tool process arguments. Target names are checked against the controller.

A read-only Habbanya replay matched the retained deletion-time fields and actual
replacement, with reconstructed observation times. Wrong-UID and container-crash
controls refused. The first replay exposed an incorrect StatefulSet assumption;
the native ServerSet owner was independently checked and the matcher corrected
before deployment. This was a local false negative, not a live watcher change.

The systemd service executes `/opt/lastsietch-pod-watcher/watcher.sh` as root every
60 seconds. Install its same-directory `planned-rolling.py` helper first, then
atomically replace the watcher after saving both source and state snapshots in a
root-private deployment directory. Preserve `.discord.env`, notification channel,
maintenance marker and existing state. Stop only the watcher timer for the short
cutover, wait for its current invocation to finish, and verify the actual next
service execution and all six populated UIDs. The helper is read-only and never
initializes, claims or changes maintenance state.

The separate CHAT-02 issue is recovery's premature operator-attention wording;
this correction does not change recovery decisions or that alert classification.
