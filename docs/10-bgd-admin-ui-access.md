# 10. BattleGroupDirector (BGD) Admin UI - secure access

The BGD ships a built-in web admin UI on port `11717` inside the cluster, exposed by default through a Kubernetes `Service` of type `NodePort` (port `31282` on the host). It is fully functional: player stats, server controls, transfer settings, force-lock, etc.

**It ships without authentication.** Anyone who can reach the NodePort on the host's public IP can manage the battlegroup. Lock it down before you publish the server IP anywhere.

## Threat model

The BGD admin UI lets an anonymous caller:

- View player rosters, last login, stats, transfer requests
- Lock and unlock the world
- Change instance-scaling and battlegroup settings
- See the `BATTLEGROUP_REGION_NAME` and other envvars in some views

A scanner that finds your IP plus an open `31282/tcp` has full operator-level access until you fix this. Treat the unrestricted default as an exploit-class misconfig.

## Recommended posture: host-local only

Drop external traffic to `31282/tcp` at the firewall and reach the UI through an SSH tunnel. The cluster `Service` stays a `NodePort` (the operator manages it and reconciles changes anyway), but the host firewall refuses external packets before kube-proxy ever sees them.

### Firewall rule (nftables, persistent)

Add a `prerouting` chain at priority `raw` inside your existing `inet filter` table. Priority `raw` runs before kube-proxy's iptables DNAT, so the packet is dropped before it can be redirected to the pod.

```nft
# /etc/nftables.conf - inside `table inet filter { ... }`
chain prerouting_admin_block {
    type filter hook prerouting priority raw; policy accept;

    # Lock down BGD admin UI (NodePort 31282 -> bgd-svc:11717) to host-local only.
    # Reach the UI via SSH port-forward:  ssh -L 31282:127.0.0.1:31282 <host>
    iifname "<external-iface>" tcp dport 31282 drop
}
```

Replace `<external-iface>` with the public-facing NIC (e.g. `enp1s0f0`). Verify with:

```bash
ip route get 8.8.8.8 | awk '{ for(i=1;i<=NF;i++) if($i=="dev") print $(i+1) }'
```

Reload:

```bash
sudo systemctl reload nftables
sudo nft list chain inet filter prerouting_admin_block
```

### Why a `prerouting` hook, not `input`

The NodePort listens on the host, but Kubernetes uses a DNAT rule in `iptables` (table `nat`, chain `PREROUTING`, priority `dstnat = -100`) to rewrite the destination from `<host-ip>:31282` to `<pod-ip>:11717`. After DNAT, the destination is no longer local, so the packet skips the `input` chain and goes through `forward`. To drop the original packet before DNAT, hook at `prerouting` with priority `raw` (`-300`), which fires earlier.

A rule on `input` will *not* block this traffic. A rule on `forward` works but is fragile because it has to discriminate by destination pod IP.

## Reaching the UI as an operator

From a workstation with SSH access to the host:

```bash
ssh -L 31282:127.0.0.1:31282 <ssh-alias>
# leave the session open, then open http://localhost:31282/ in a browser
```

The NodePort still binds locally on the host (Kubernetes hasn't changed), and the host's own loopback bypasses the `prerouting_admin_block` chain (different `iifname`).

Alternatively, port-forward directly through Kubernetes (no NodePort exposure at all, even on the host):

```bash
sudo kubectl -n funcom-seabass-<bg-suffix> \
  port-forward svc/<bg>-bgd-svc 31282:11717
```

## Verification

Before locking down, expect:

```bash
$ curl -sI http://<public-ip>:31282/
HTTP/1.1 200 OK
Server: Kestrel
```

After locking down, the same `curl` from outside should time out:

```bash
$ curl --max-time 5 -sI http://<public-ip>:31282/
# (no response, exits 28)
```

From the host itself, loopback still works:

```bash
$ curl --max-time 3 -sI http://127.0.0.1:31282/
HTTP/1.1 200 OK
```

## Other admin surfaces to consider

The same threat model applies to anything else the BG operator exposes as a NodePort. Audit with:

```bash
sudo kubectl -n funcom-seabass-<bg-suffix> get svc -o wide
```

Notable services to look at:

- `*-bgd-svc` - BGD admin UI (this doc)
- `*-mq-admin-svc` - RabbitMQ management UI (also unauth by default on some deployments)
- `*-tr-deploy-svc`, `*-sgw-deploy-svc` - internal-only; should not be on a NodePort

Default to firewalling all of them and using SSH/kubectl port-forwards for operator access.
