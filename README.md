# k0s-tailscale-platform

**English** · [Русский](README.ru.md)

A small Kubernetes platform you can bring up on a couple of cheap VMs with one command:
**k0s** over **Tailscale**, hardened by default, plus an example app — **jobmon**, a job
collector (ATS APIs and remote job boards → Postgres → Grafana over HTTPS).

```
                        tailnet (Tailscale / WireGuard)
 ┌─────────────────────────┐   Calico VXLAN   ┌──────────────────────────────┐
 │ control plane           │◄────────────────►│ worker(s)                    │
 │ k0s controller + etcd   │                  │ k0s worker                   │
 │ taint NoSchedule        │                  │ jobmon: db · collector ·     │
 │ ufw: 22/tcp, 41641/udp  │                  │ grafana · tailscale-proxy    │
 └─────────────────────────┘                  └──────────────────────────────┘

 https://grafana.<tailnet>.ts.net  — Let's Encrypt certificate issued by the Tailscale
                                     operator, reachable from tailnet devices only
```

- **One command:** `make deploy` — control plane, workers, add-ons, the app. Rerunnable.
- **Or a button:** GitHub Actions → *deploy* → *Run workflow*.
- **Private by default:** only SSH and Tailscale's UDP port face the internet; the cluster
  talks over the tailnet; Grafana gets HTTPS without a domain or open ports.

## What's inside

| Path | What it is |
|---|---|
| `Makefile` | every command: `make help` |
| `site.yml` | everything in order: `controller.yml` → `workers.yml` → `addons.yml` → `app.yml` |
| `roles/` | `base` (packages, sysctl, DNS), `tailscale`, `ufw`, `k0s_*`, `cluster_addons`, `jobmon` |
| `addons/` | local-path-provisioner v0.0.37 (volumes on the node's disk) |
| `app/collector/` | the collector and its `Dockerfile`; `profile.yml` and `companies.yml` say what to look for |
| `app/k8s/`, `app/kustomization.yaml` | jobmon for Kubernetes (base; per-install values come from `roles/jobmon`) |
| `app/docker-compose.yml` | jobmon on a single host, no Kubernetes |
| `app/db/`, `app/grafana/` | DB schema, Grafana data source and dashboard |
| `.github/workflows/` | `collector-image` — image to ghcr; `deploy` — deploy on demand |
| `group_vars/all.yml` | settings: versions, networks, tailnet domain, collector image |
| `inventory.example.ini`, `vars/secrets.example.yml` | templates; the real files never go into git |

## Requirements

**Hosts:** two or more, Ubuntu 22.04/24.04 x86_64, 2 GB RAM or more, SSH as `root` with a key.
One becomes the control plane, the rest are workers.

**Tailscale** (tailnet admin console, once):

1. *Settings → Keys:* a reusable, pre-approved auth key → `tailscale_authkey`.
2. *Access controls:* add to `tagOwners`
   `"tag:k8s-operator": []` and `"tag:k8s": ["tag:k8s-operator"]`.
3. *DNS:* enable MagicDNS and **HTTPS Certificates**. The tailnet suffix
   (`tailXXXX.ts.net`) is `tailnet_domain`.
4. *Trust credentials:* an OAuth client with **write** on General/Services, Devices/Core,
   Keys/Auth Keys, tag `tag:k8s-operator` → `tailscale_operator_oauth_client_id` / `_secret`.
   The secret is shown once.

**Where you run it from:** `ansible-core` ≥ 2.18, `helm`, `make`, `openssl`. It does not
need to be in the tailnet: hosts are reached over SSH by public IP.

## Deploy from scratch

```bash
git clone https://github.com/Borman322/k0s-tailscale-platform && cd k0s-tailscale-platform
make init        # Ansible collections + inventory.ini, vars/secrets.yml (random passwords)
```

Then fill in three files:

- `inventory.ini` — host addresses (`[first_controller]` and `[new_workers]`);
- `vars/secrets.yml` — the Tailscale keys from above;
- `group_vars/all.yml` — `tailnet_domain` and `jobmon_collector_image`.

```bash
make deploy      # cluster, add-ons and jobmon; safe to rerun
```

A few minutes later Grafana is at `https://grafana.<tailnet_domain>` (user `admin`,
password `jobmon_grafana_password`). Open it from a device in the same tailnet.

### Collector image

Push your copy to GitHub: the **collector-image** workflow builds the image and pushes it to
`ghcr.io/<owner>/<repo>-collector` with tags `sha-<commit>` and `latest`. The tag to use is in
the run summary — put it into `jobmon_collector_image`, then `make app`.

A new ghcr package may be private. Either make it public (*Package settings → Change
visibility*) or set `ghcr_pull_user` and `ghcr_pull_token` (a token with only
`read:packages`) in `vars/secrets.yml`.

## Make it look for your jobs

The collector polls company job boards directly — Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Recruitee, Personio, Teamtailor, BambooHR, Rippling, Gem, Breezy — plus
remote job boards (Himalayas, Jobicy, RemoteOK, We Work Remotely, Arbeitnow,
HN «Who is hiring?»). It scores every vacancy and stores the ones above `min_score`.

- `app/collector/profile.yml` — **what counts as a match:** weighted keywords, title
  include/exclude patterns, an optional `domain` word list (on broad boards keeps only
  vacancies mentioning your field). The example is an SRE / Platform / DevOps profile.
- `app/collector/companies.yml` — **where to look:** companies with their ATS and slug
  (`ats: probe` finds the system by itself) and which boards to read.
- `python discover.py` — finds the ATS and slug for names from `seed_companies.txt` and
  writes them into `companies.yml`.

Try it locally without a database:

```bash
cd app/collector && pip install -r requirements.txt
python collector.py --dry-run --top 20
```

Vacancies are tagged, not dropped, when they look region-locked (`us-only`, `eu-auth`) or
office-only (`onsite`); the dashboard shows that as a column.

## Deploy with a couple of clicks from GitHub

The **deploy** workflow runs the same `make <target>` on a GitHub runner.

1. A deploy key: `ssh-keygen -t ed25519 -f deploy_key -N ''`. Put the public half
   (`deploy_key.pub`) into `base_ssh_authorized_keys` in `group_vars/all.yml` and run
   `make cluster` once from your machine — the key lands on every host.
2. *Settings → Secrets and variables → Actions*, three secrets:
   - `SSH_PRIVATE_KEY` — contents of `deploy_key`;
   - `INVENTORY_INI` — contents of `inventory.ini`;
   - `SECRETS_YML` — contents of `vars/secrets.yml`.
3. *Actions → deploy → Run workflow*, pick a target: `app`, `deploy`, `cluster`, `workers`,
   `addons`, `status` or `backup` (the DB dump arrives as a run artifact).

When `inventory.ini` or `vars/secrets.yml` change, update the secrets too:
`gh secret set INVENTORY_INI < inventory.ini`.

## Day to day

| Command | What it does |
|---|---|
| `make status` | nodes and pods |
| `make app` | update jobmon (after changing manifests or the image tag) |
| `make workers` | join new hosts from `[new_workers]` |
| `make backup` | `pg_dump` of the database into `./backups/` |
| `make check` | syntax check of the playbooks |

`kubectl` only works on the controller (the API listens on the tailnet address):

```bash
ssh root@<controller> 'k0s kubectl -n jobmon get pods'
# an extra collector run
ssh root@<controller> 'k0s kubectl -n jobmon create job --from=cronjob/collector collector-manual-$(date +%s)'
# restore a dump
ssh root@<controller> 'k0s kubectl -n jobmon exec -i db-0 -- pg_restore -U jobs -d jobs --clean --if-exists --no-owner' < backups/jobmon-<time>.dump
```

**Add a worker:** append it to `[new_workers]` → `make workers`. Hosts that already joined
are left alone. With `--limit`, keep the controller in the list — it issues the join token.

## Without Kubernetes

On a single host with Docker:

```bash
cd app && docker compose up -d --build     # passwords in app/.env (created by make init)
```

Grafana listens on `127.0.0.1:3000` by default (reach it with `ssh -L 3000:localhost:3000`);
`GRAFANA_BIND=0.0.0.0` in `.env` opens it up.

## Design decisions

- **Calico VXLAN, not kube-router.** Tailscale only carries TCP, UDP and ICMP. kube-router
  sends pod traffic between nodes over IPIP (IP protocol 4), which tailscaled silently drops
  (`Drop: IPProto-4 … unknown-protocol-4`) while every node still reports `Ready`. VXLAN is
  UDP; MTU 1230 fits `tailscale0`'s 1280. The CNI cannot be changed on a live k0s cluster.
- **A `Ready` node proves little.** The playbooks wait for the system pods on each node:
  calico and konnectivity do not come up unless the pod network works across the tailnet.
- **The control plane is listed in `kubectl get nodes` but takes no workloads:** it runs a
  kubelet, and a `NoSchedule` taint keeps apps on the workers
  (modes in `roles/k0s_controller/defaults/main.yml`).
- **Swap stays on** (2 GB hosts need it); kubelet gets `--fail-swap-on=false`.
- **Only SSH and UDP 41641 are open**; all cluster traffic goes over `tailscale0`.
- **jobmon:** Pod Security `restricted` for its namespace (nothing runs as root, Postgres
  included), NetworkPolicies (Postgres only from the collector and Grafana, Grafana only from
  the Tailscale proxy), volumes on `local-path-retain` (deleting a PVC keeps the data),
  passwords in a Secret.
- **No secrets in git.** GitHub secret scanning reports Tailscale keys to Tailscale, which
  revokes them at once. `vars/secrets.yml` and `inventory.ini` are git-ignored; Actions
  read them from repository secrets.
