# 05 - Sietch Display Name

There are two human-readable names attached to a self-hosted World:

| Concept | What it is | Where it's configured | Where it shows up |
|---|---|---|---|
| **World name** (Battlegroup title) | The top-level identifier for your World - what shows as the parent entry in the in-game server browser | The `world name` prompt during the wizard, sed-substituted into `{WORLD_NAME}` in the world template. Also `spec.title` on the BG CR. | Top-level row in the Experimental tab of the server browser, with all Sietches in this World nested below it |
| **Sietch display name** | The per-sietch name that shows under the parent World entry - what players see when picking which Sietch to join | `Bgd.ServerDisplayName` console variable in `UserEngine.ini` `[ConsoleVariables]` | Nested under the World name in the server browser; also shown as your sietch's identity in-game |

The wizard prompts for the World name but does NOT prompt for the Sietch display name. By default the sietch shows up with a procedural placeholder ("Sietch Abbir", "Sietch Hagal", etc., generated client-side). You have to set the Sietch display name manually after the wizard.

## What works

The right key is `Bgd.ServerDisplayName` in `UserEngine.ini` `[ConsoleVariables]`. Specifically:

- The **`Bgd.` prefix is mandatory** - bare `ServerDisplayName` is not the same key
- The file is **`UserEngine.ini`** - not `UserGame.ini`, not `director.ini`
- The section is **`[ConsoleVariables]`** - not `[Bgd]` or any per-map section
- Funcom ships the file with a commented example you just uncomment and edit:
  ```ini
  ;Bgd.ServerDisplayName="My Arrakis, My Dune"
  ```

The value applies battlegroup-wide - every Sietch in your BG gets the same name. Per-sietch naming would require the in-game Battlegroup Editor (or a Funcom-internal field we haven't fully mapped).

## What doesn't work (don't waste time on these)

| Attempt | What happens |
|---|---|
| Bare `ServerDisplayName="..."` (no `Bgd.` prefix) | Inert. Client still shows procedural placeholder. |
| `ServerDisplayName` in the BGD's `director.ini` configmap, per-map section like `[ Survival_1 ]` | Inert. The operator owns this file and reverts manual edits; even when applied successfully it doesn't influence the broadcast `displayName`. |
| Setting it in `UserGame.ini` | Wrong file. The cvar is only read by the UE5 engine config, not the gameplay config. |

## How to set it

The PVC backing the filebrowser pod is mounted at `Saved/UserSettings/`. You can edit `UserEngine.ini` in two ways.

### Option A - via the filebrowser web UI

`battlegroup.bat → file browser` (Windows) or browse to the filebrowser pod's exposed port (Linux self-host). Edit `UserSettings/UserEngine.ini` in the web editor, uncomment the line, set your name.

### Option B - directly on the host's PVC mount

```bash
NS=funcom-seabass-sh-<hostid>-<random>
PVC=$(sudo ls -d /var/lib/rancher/k3s/storage/pvc-*${NS}*-pvc 2>/dev/null | grep -v db-pvc | head -1)
sudo sed -i 's|^;Bgd.ServerDisplayName="[^"]*"$|Bgd.ServerDisplayName="My Sietch Name"|' \
  "$PVC/Saved/UserSettings/UserEngine.ini"
```

Adjust the path if your storage class differs.

### Option C - via kubectl exec into the filebrowser pod

```bash
FB=$(sudo kubectl get pods -n $NS -l app.kubernetes.io/component=file-browser -o jsonpath='{.items[0].metadata.name}')
sudo kubectl -n $NS exec $FB -- sed -i 's|^;Bgd.ServerDisplayName=".*"$|Bgd.ServerDisplayName="My Sietch Name"|' \
  /files/dune-server/UserSettings/UserEngine.ini
```

(Path inside the filebrowser pod may differ slightly between versions.)

## When the change takes effect

The cvar is read on game-server pod startup. After editing, you need to restart partition pods:

```bash
sudo kubectl -n $NS delete pod -l app.kubernetes.io/component=server
```

The operator immediately re-spawns them. The new pods read the updated UserEngine.ini and the BGD's next `Battlegroups_DeclareBattlegroupUpdates` call broadcasts the new `displayName`.

You can verify this in the BGD logs - `kubectl logs <bgd-pod> | grep DisplayName`.

## Special characters

Funcom's comment in the default file says:

> Special characters like ' and | are not allowed and double quotes should be used

This means:

- Use straight double quotes `"`, not curly quotes
- No backslash escapes
- Avoid `'`, `|`, control characters
- Letters, numbers, spaces, hyphens, periods are safe
- Some unicode (like `0` for `o` in clever spellings) seems to be tolerated based on community names in the browser

## Common failure mode: empty DisplayName in Declare payload

If the BG is `Healthy` but your server doesn't appear in the browser:

```bash
BGD=$(sudo kubectl get pods -n $NS -l role=igw-battlegroup-director -o jsonpath='{.items[0].metadata.name}')
sudo kubectl logs -n $NS $BGD | grep DeclareBattlegroupUpdates | tail -1 | grep -oE '"DisplayName":"[^"]*"'
```

If you see `"DisplayName":""` (empty), your cvar isn't being read. Possible causes:

- You edited `UserGame.ini` instead of `UserEngine.ini`
- You used bare `ServerDisplayName` instead of `Bgd.ServerDisplayName`
- You forgot to restart the partition pods after editing
- A Funcom update overwrote your custom file with the default (audit script catches this - see [scripts/audit.sh](../scripts/audit.sh))

An empty `DisplayName` causes FLS or the in-game UI to filter the server out of browser results - even with everything else correct.

## Related

- [02-canonical-config.md](02-canonical-config.md)
- [04-server-browser-visibility.md](04-server-browser-visibility.md) - broader visibility debugging
