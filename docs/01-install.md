# 01 - Install on Debian 12

This walks through deploying a Dune: Awakening self-hosted server on a bare-metal **Debian 12** host, using the same Funcom-shipped bootstrap script the official Windows wizard uses inside its Hyper-V VM.

## What this guide assumes

- A clean Debian 12 host with root access
- 64+ GB RAM (Funcom's minimum is 20 GB; we recommend more for headroom)
- 100+ GB free disk on `/`
- A public IP, with UDP `7777-7810` and TCP `31982` reachable from the internet
- A self-host token already minted at <https://account.duneawakening.com/> (one-time, from your Funcom account)

## Step 1 - Create the `dune` user

The Funcom bootstrap expects a `dune` user with passwordless sudo.

```bash
sudo adduser dune --gecos "Dune Self-Host,,," --disabled-password
sudo usermod -aG sudo dune
sudo bash -c 'echo "dune ALL=(ALL:ALL) NOPASSWD: ALL" > /etc/sudoers.d/dune'
sudo chmod 440 /etc/sudoers.d/dune
```

## Step 2 - Install steamcmd

The bootstrap downloads the server payload via Steam's anonymous CDN. You need `steamcmd` on the host.

```bash
sudo dpkg --add-architecture i386
sudo apt update
sudo apt install -y software-properties-common
echo steam steam/question select "I AGREE" | sudo debconf-set-selections
echo steam steam/license note '' | sudo debconf-set-selections
sudo apt install -y steamcmd lib32gcc-s1 bc
```

## Step 3 - Install k3s

Funcom's setup chain expects k3s to be installed and the `k3s` service available via `rc-service` (OpenRC) **or** systemd (`systemctl`). On Debian 12 (systemd), patch the wizard's `rc-service` call by symlinking a shim or by installing OpenRC alongside systemd. The simplest path is to use the official k3s installer with a `--node-external-ip` flag baked in.

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--node-external-ip <YOUR_PUBLIC_IP>" sh -
```

Replace `<YOUR_PUBLIC_IP>` with the IP players will connect to. This flag is critical - without it the Battlegroup Director publishes a private/wrong address in the Declare payloads and your server won't appear in the in-game browser.

Wait for k3s to be ready:

```bash
sudo kubectl get nodes
# Expect: NAME   STATUS   ROLES                  AGE   VERSION
#         <host> Ready    control-plane,master   1m    v1.34.5+k3s1
```

## Step 4 - Persist the containerd socket symlink

Funcom's `battlegroup.sh update-from-downloads` calls `ctr -n k8s.io images import` without specifying a socket path. k3s puts its socket at `/run/k3s/containerd/containerd.sock` but `ctr` defaults to `/run/containerd/containerd.sock`. Bridge them via tmpfiles.d so the symlink persists across reboots:

```bash
sudo bash -c 'cat > /etc/tmpfiles.d/k3s-containerd-symlink.conf' << 'EOF'
L /run/containerd /run/k3s/containerd
EOF
sudo systemd-tmpfiles --create /etc/tmpfiles.d/k3s-containerd-symlink.conf
sudo ls -la /run/containerd
# Expect: /run/containerd -> /run/k3s/containerd
```

## Step 5 - Pre-stage the dune home directory

The Funcom wizard expects a specific directory layout under `/home/dune/.dune/`:

```bash
sudo -u dune mkdir -p /home/dune/.dune/bin
sudo -u dune mkdir -p /home/dune/.dune/download
```

## Step 6 - Write `settings.conf` with your public IP

The bootstrap reads the public IP from `/home/dune/.dune/settings.conf`. The wizard normally generates this with three blank lines followed by the IP:

```bash
sudo -u dune bash -c 'printf "\n\n\n%s\n" "<YOUR_PUBLIC_IP>" > /home/dune/.dune/settings.conf'
```

Replace `<YOUR_PUBLIC_IP>` with the same IP you passed to `--node-external-ip`.

## Step 7 - Install the Funcom bootstrap setup script

Take a copy of Funcom's bootstrap from the official Windows install - the file is at:

```
<Steam library>\steamapps\common\Dune Awakening Self-Hosted Server\battlegroup-management\bootstrap\setup
```

Convert line endings to LF and copy to the host:

```bash
# On a Windows / WSL2 box with the official install:
cp "/mnt/<drive>/Games/Steam/steamapps/common/Dune Awakening Self-Hosted Server/battlegroup-management/bootstrap/setup" /tmp/dune-setup
sed -i 's/\r$//' /tmp/dune-setup
scp /tmp/dune-setup root@<your-host>:/tmp/dune-setup

# On the host:
sudo cp /tmp/dune-setup /home/dune/.dune/bin/setup
sudo chown dune:dune /home/dune/.dune/bin/setup
sudo chmod +x /home/dune/.dune/bin/setup
```

Verify the appid embedded in the script:

```bash
grep app_update /home/dune/.dune/bin/setup
# Expect:  steamcmd +set_spew_level 1 1 +force_install_dir "$DOWNLOAD_PATH" +login anonymous +app_update 4754530 +logoff +quit
```

If you see `3104830` instead of `4754530`, you have the **older PTC bootstrap**. Get a fresh one from the GA Steam product. The GA product is named **"Dune: Awakening Self-Hosted Server"** (app ID `4754530`), under **TOOLS** in your Steam library.

## Step 8 - Run the bootstrap

The setup script will:

1. Validate disk space
2. `steamcmd +login anonymous +app_update 4754530` - pulls about 5 GB (1 GB server payload + 4 GB Steam Linux Runtime shared depots)
3. Run `$DOWNLOAD_PATH/scripts/setup.sh` which chains:
   - `k3s.sh` - ensures k3s is up, imports prerequisite images
   - `system.sh` - symlinks `battlegroup` and `bg-util` into `/home/dune/.dune/bin/`
   - `world.sh` - prompts for **world name**, **region**, and **JWT** (this is the interactive part)
   - `battlegroup.sh update-from-downloads` - imports the Funcom container images into containerd
   - `battlegroup.sh apply-default-usersettings` - writes default `UserGame.ini` + `UserEngine.ini` into the filebrowser pod

You can pipe the three world.sh inputs in non-interactively. Save them to a file `dune-setup-inputs` with one prompt per line:

```
<your world name, max 50 chars>
2
<your JWT>
```

Then run:

```bash
sudo -u dune /home/dune/.dune/bin/setup < /tmp/dune-setup-inputs 2>&1 | tee /tmp/dune-setup.log
```

Region option `2` is **North America Test**; option `1` is **Europe Test**. These are the only choices the wizard offers, and despite the "Test" suffix in the name, "North America Test" IS the canonical GA region tag for North America (the in-game UI filter strips the suffix when displayed). Pick by your geography.

Expect the run to take 5-15 minutes depending on bandwidth.

## Step 9 - Verify

When the setup finishes, you should see a Battlegroup CR in a new namespace:

```bash
sudo kubectl get ns | grep funcom-seabass
sudo kubectl -n funcom-seabass-sh-<your-hostid>-<random> get igwbg
sudo kubectl -n funcom-seabass-sh-<your-hostid>-<random> get pods
```

The pods will go through `Pending` → `ContainerCreating` (during image extract - first run is slow) → `Running`. The Battlegroup phase progresses `Pending` → `BootingUp` → `Healthy`. Expect ~5-10 minutes from CR creation to first `Healthy` status.

Once `Healthy`, your server should appear in the in-game **Experimental** tab of the server browser within ~2 minutes.

## Next steps

- **Apply the canonical config** - see [02-canonical-config.md](02-canonical-config.md)
- **Set up dual Deep Desert** - see [03-dual-deep-desert.md](03-dual-deep-desert.md)
- **Configure the sietch display name** - see [05-display-name.md](05-display-name.md)

## Troubleshooting first-run

| Symptom | Most likely cause | Fix |
|---|---|---|
| Steam download fails immediately | App ID is wrong (still PTC `3104830`?) | Re-extract bootstrap from the GA product `4754530` |
| `world.sh` hangs at "Enter your self hosted token" | stdin not connected to the wizard | Run interactively in a tmux/screen session and type by hand |
| Pods stuck in `ImagePullBackOff` | containerd socket symlink missing | Re-create the symlink (Step 4) and retry `battlegroup.sh update-from-downloads` |
| BG reports `Healthy` but doesn't appear in browser | k3s node has no `ExternalIP` set | Re-install k3s with `--node-external-ip` |
| `Bgd.ServerDisplayName` empty in Declare logs | Default `UserEngine.ini` was applied AFTER your custom edits | Apply Bgd.ServerDisplayName AFTER `apply-default-usersettings`, then restart partition pods |
| Wizard says "Steam download failed" twice | Anonymous login rate-limited or transient steam error | Wait 10 minutes, re-run |

See [07-troubleshooting.md](07-troubleshooting.md) for the long list.
