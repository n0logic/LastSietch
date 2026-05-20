# 04 - Server Browser Visibility

A self-hosted server is useless if players can't find it. This guide explains how a self-hosted Dune: Awakening server actually gets listed in the in-game server browser (the Experimental tab), and how to debug the most common case where a "Healthy" server still doesn't appear.

## The mechanism

Listing a server in the browser is **one specific API call**, not a passive heartbeat. The Battlegroup Director (BGD) pod calls Funcom's FLS (Funcom Live Services) endpoint:

```
api/Battlegroups_DeclareBattlegroupUpdates
```

What makes a server appear is that call carrying a **populated** `UpDeclarationsByPartitionId` payload. Each partition entry in that payload contains:

- `ServerId`
- `GameAddress` - your public IP
- `GamePort` - in the 7777-7810 range
- `MapName`
- `IsStartingMap` - `true` for at least one partition

When FLS receives a `DeclareBattlegroupUpdates` call with a non-empty `UpDeclarationsByPartitionId`, it marks that partition browser-visible. An empty payload (`UpDeclarationsByPartitionId: {}`) does nothing.

## Heartbeats are not enough

The BGD also makes several other FLS calls on timers:

```
api/Battlegroups_SendBattlegroupHeartbeat
api/Battlegroups_DeclareMaxPlayerCapacities
api/Battlegroups_DeclarePopulationAndActivity
```

All of these can return HTTP 200 without your server ever appearing in the browser. **Only the populated `DeclareBattlegroupUpdates` call lists the server.** If you are looking at BGD logs and see heartbeats succeeding, that tells you the BGD can reach FLS - it does not tell you the server is visible.

## End-to-end: how a server becomes visible

The full chain from pod boot to browser listing:

1. A UE5 game-server pod boots.
2. The pod writes its row to Postgres - `(map, partition, dimension, address, port, revision)`.
3. The Server Gateway poller reads that row and logs a `Server X came up!` event.
4. The BGD reads the ServerGroup status.
5. The BGD fires `Battlegroups_DeclareBattlegroupUpdates` with a populated `UpDeclarationsByPartitionId`.
6. FLS marks the partition browser-visible.

**No external port-reachability probe is involved.** Observed (unconfirmed): a reference install was browser-visible without any inbound port forwards configured, which indicates FLS does not probe the advertised `GameAddress:GamePort` before listing. (Players still need those ports open to actually *connect* - see [01-install.md](01-install.md).)

It can take a few minutes for a server to appear in the browser after the `DeclareBattlegroupUpdates` call fires. Assumption: this is FLS-side propagation; allow ~2 minutes before assuming a failure.

## The most common failure: build skew

This is the failure mode to check first when a server is "Healthy" but invisible.

If the captured/running server build is **older** than Funcom's current build, the older BGD silently skips both:

- `api/Director_InitializeDirector`
- `api/Battlegroups_DeclareBattlegroupUpdates`

while still sending heartbeats, max-player-capacity, and population calls - all returning HTTP 200. The result: the Battlegroup phase reads `Healthy`, every pod is `Ready`, and the server is completely invisible in the browser.

The fix is to update the server build so it matches Funcom's current build. See [08-updates.md](08-updates.md).

Observed (unconfirmed): the skipped-call behavior was reproduced on one deployment running a build roughly 3000 revisions behind Funcom's current build. Updating the build restored visibility within seconds of the new BGD coming up.

## Debugging steps

When `kubectl get igwbg` shows `PHASE=Healthy` but the server is not in the browser:

### 1. Phase=Healthy is not a visibility check

A `Healthy` Battlegroup and `Ready` pods only confirm the cluster is running. They say nothing about whether `DeclareBattlegroupUpdates` fired.

### 2. Inspect the BGD pod logs

Find the BGD pod by its role label and look for `DeclareBattlegroupUpdates` calls:

```bash
NS=funcom-seabass-sh-<hostid>-<random>
BGD=$(sudo kubectl get pods -n $NS -l role=igw-battlegroup-director -o jsonpath='{.items[0].metadata.name}')

# How many Declare calls total
sudo kubectl logs -n $NS $BGD 2>&1 | grep -c "DeclareBattlegroupUpdates"

# How many of them are EMPTY (the bad case)
sudo kubectl logs -n $NS $BGD 2>&1 | grep "DeclareBattlegroupUpdates" | grep -c 'UpDeclarationsByPartitionId":{}'

# Most recent POPULATED Declare
sudo kubectl logs -n $NS $BGD 2>&1 | grep "DeclareBattlegroupUpdates" | grep -v 'UpDeclarationsByPartitionId":{}' | tail -1
```

You want to see at least one `DeclareBattlegroupUpdates` call with a non-empty `UpDeclarationsByPartitionId`.

If `DeclareBattlegroupUpdates` is **completely absent** from the logs, suspect build skew first (see above).

If `DeclareBattlegroupUpdates` appears but every call is **empty** (`{}`):

- Are all game pods reporting `Ready`?
- Is the Server Gateway logging `came up!` events?
- Did you set `--node-external-ip` on k3s? Without it the BGD has no public address to advertise. See [01-install.md](01-install.md).

### 3. Verify the populated payload

If a populated Declare exists, confirm the fields are correct:

- `GameAddress` is your public IP, not a `10.x` / `172.x` internal address
- `GamePort` is in the 7777-7810 range
- `IsStartingMap` is `true` on at least one partition
- The region matches the in-game browser region filter you're searching under

### 4. Use the audit script

`scripts/audit.sh` performs the `DeclareBattlegroupUpdates` check as part of its run - use it as a quick post-deploy and post-update sanity check rather than grepping logs by hand each time.

## Related documentation

- [01-install.md](01-install.md) - `--node-external-ip` and port forwarding
- [07-troubleshooting.md](07-troubleshooting.md) - "BG Healthy but server doesn't appear in browser"
- [08-updates.md](08-updates.md) - fixing build skew
