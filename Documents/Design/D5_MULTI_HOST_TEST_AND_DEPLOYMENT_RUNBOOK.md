# D5 Multi-host Test and Deployment Runbook

**Status (12/11/2026): Production-only runbook for `PRODUCTION_MULTI_HOST_ACCEPTANCE`; this is not a prerequisite for `DEMO_ACCEPTANCE`.** The owner confirms current cloud/development accounts are personal, while company infrastructure has not been provided. Do not run this against personal Firebase/Google Cloud. No cloud resource has been provisioned or contacted by this work.

**Accessed:** 2026-11-12. Prices and free-tier eligibility can change; recheck the linked official pages before approving spend.

## 1. What exists in this repository and workspace

- `FirestoreAtomicStateStore` implements the `AtomicStateStore` transaction port but is not wired into the public application runtime. It is suitable for an explicitly composed test worker; existing code does not prove a Firestore-backed deployment.
- `LocalSQLiteAtomicStateStore` and the current multi-process tests prove same-host process coordination only. The adapter explicitly rejects production use.
- `DistributedWorkerRunner` provides a polling loop, graceful stop, and lets errors reach the process supervisor. No service manager configuration or production worker composition exists yet.
- The workspace has no `docker`/`podman`, PostgreSQL client/server, or Redis server. It has `systemctl`, but that does not show access to two independent hosts.
- No organization-owned cloud project, Firestore database, VM, billing account, or service identity can be verified from this checkout. No credentials were inspected or used.

## 2. Lowest-complexity option

When the owner separately authorizes the production gate, first reuse **already existing, organization-owned, non-production resources** if all of these are true:

1. An approved Google Cloud project already has a Firestore Native default database. Do not reuse a developer-owned project or a production database.
2. Two independent, organization-managed Linux hosts already exist and can reach that database. Use systemd on each host as the process supervisor.
3. Each host can use its own attached workload identity or service account through ADC. Do not download or distribute service-account key files.

This option reuses the repository's Firestore adapter and existing Firestore dependency. The automated probe uses fixed `SYNTHETIC` data and does not instantiate provider adapters. If any prerequisite is missing, leave `PRODUCTION_MULTI_HOST_ACCEPTANCE=BLOCKED`; do not create a project, database, VM, billing account, or credential as part of D5. Local demo acceptance does not wait on these resources.

If suitable hosts do not already exist, two small Compute Engine VMs with systemd are the simplest fully controlled fallback. A reference price for `e2-small` in the listed US region is about **$0.01675/hour per VM**, approximately **$24.46/month for two at 730 hours**, before disk, network, tax, and regional differences. The smaller `e2-micro` table rate is about **$12.23/month for two**, before extras, but its 1 GiB memory may be insufficient for the Python runtime. This is an estimate, not approval to provision. A free-tier VM allowance is limited and does not cover two always-on instances.

## 3. Services, accounts, cost, and minimum access

| Need | Account/service | Minimum request to the organization | Possible cost |
|---|---|---|---|
| Shared atomic backing | Existing org-owned Google Cloud project with Firestore Native default DB | Project/database identity, owner confirmation, non-production approval, region, and a dedicated D5 test namespace | Firestore has a free quota on one eligible database per project (50,000 reads/day, 20,000 writes/day, 1 GiB storage, and 10 GiB monthly egress). Beyond quota, the listed US rates include $0.03/100,000 reads and $0.09/100,000 writes; storage, indexes, and network may add cost. |
| Runtime identity | Organization-owned Google Cloud service identity per worker fleet (or a distinct identity per host if required by policy) | Attach through VM identity/ADC or approved workload identity federation; no exported key | No separate credential fee; requests and compute are billed under the project's billing configuration. |
| Firestore permissions | Custom IAM role granted only in the non-production test project | `datastore.databases.get`, `datastore.entities.get`, `datastore.entities.create`, and `datastore.entities.update`; add nothing else unless observed API calls require it | No direct IAM-role fee. Firestore IAM is project scoped, so use a dedicated non-production project to bound data access. The service identity must not create databases, change IAM, manage billing, delete data, or administer the project. |
| Two worker hosts | Prefer two existing org-managed Linux hosts; fallback is two Compute Engine VMs | SSH/operations access for deployment, systemd unit installation/restart, egress to the approved Firestore endpoint, and unique attached runtime identity | Existing capacity may have no incremental compute cost. New VMs incur hourly compute plus disks/network. Current reference price is above; region and disk choices change it. |
| Supervisor | systemd on each Linux host | Unit install/restart authority for the operator, not for the anonymous web user | Included with Linux hosts. |

The D5 service identity only needs the data permissions above. It does not need `roles/owner`, `roles/editor`, Firestore Admin, project creation, billing access, or the ability to create service-account keys. Setup and cleanup remain administrator actions. Firestore server libraries use IAM; Firebase client Security Rules do not constrain this server identity. The linked permission table identifies transaction, read, and write permissions.

### What must be provided or approved to attempt the production gate

To move the real gate, provide or identify:

1. The **existing non-production Google Cloud project ID** and confirmation that it is organization-owned, has Firestore Native `(default)` already enabled, and is not production.
2. Two reachable **independent Linux host names/instance IDs** with systemd, or confirmation that existing hosts are unavailable. No personal cloud credentials are needed.
3. Approval for the service identity to use the minimum custom Firestore role in that non-production project, and approval of the bounded Firestore read/write charges and host resource use.
4. A deployment operator who can install/restart the service on both hosts and later remove the probe documents. The runtime identity intentionally lacks delete permission.

Do not send credential values in chat. The organization should attach an identity to each host; the script rejects user OAuth ADC credentials.

## 4. Offline preparation and real-host gate

Run the automated offline suite first:

```bash
bash Backend/scripts/verify_offline_readiness.sh
```

The following opt-in harness uses the existing Firestore adapter, a single fixed Wikimedia source identity, a synthetic handler, and the normal `DistributedFetchWorker`/`DistributedWorkerRunner`. It does not make any provider request. Do not run it against production or against a developer-owned project.

Choose a fresh random hex run ID (8–32 characters), and the same two host IDs on both machines. After the owner confirms the project and access above, install the same reviewed revision on each host and run concurrently:

```bash
cd Backend
.venv/bin/python scripts/multihost_gate_probe.py \
  --project-id ORGANIZATION_NONPROD_PROJECT \
  --confirm-nonproduction-project ORGANIZATION_NONPROD_PROJECT \
  --run-id a1b2c3d4 \
  --acknowledge-nonproduction-writes \
  worker --host-id worker-a --expected-hosts worker-a,worker-b
```

On the second independent host, use the same project/run/expected-hosts and `--host-id worker-b`. Each process waits until the other unique Linux machine identity has registered before consuming jobs. Its output must contain `worker_ready` with a different hostname; the stored machine identity is a SHA-256 digest, not the raw machine ID.

On one approved operator session, submit one uniquely keyed synthetic job:

```bash
.venv/bin/python scripts/multihost_gate_probe.py \
  --project-id ORGANIZATION_NONPROD_PROJECT \
  --confirm-nonproduction-project ORGANIZATION_NONPROD_PROJECT \
  --run-id a1b2c3d4 \
  --acknowledge-nonproduction-writes \
  submit --idempotency-key d5-mh-a1b2c3d4
```

Poll the returned job ID:

```bash
.venv/bin/python scripts/multihost_gate_probe.py \
  --project-id ORGANIZATION_NONPROD_PROJECT \
  --confirm-nonproduction-project ORGANIZATION_NONPROD_PROJECT \
  --run-id a1b2c3d4 \
  --acknowledge-nonproduction-writes \
  inspect --job-id RETURNED_JOB_ID
```

The job must reach `COMPLETED` with `data_mode=SYNTHETIC`, one `D5_MULTIHOST_PROBE` record, and exactly one recorded `worker_host_id` matching one of the two registered hosts. Check both host logs to confirm the non-owner remained available and did not execute the same job. Then stop each process cleanly. To verify automatic supervisor recovery, send `SIGKILL` to one unit with `systemctl kill --signal=SIGKILL trendscout-d5-probe.service`; confirm systemd restarts it and the same host returns to `worker_ready`, then run a fresh probe job. Save the two cloud instance IDs, host logs, job ID, result, and restart evidence with the D5 approval record.

The harness writes a small persistent coordination document and job receipt to `_ts_d5_coordination`. This is a dedicated test run ID, not an automatic cleanup mechanism. After evidence is reviewed, the project administrator removes only documents with the recorded probe run ID using an administrator account; do not grant the runtime identity delete access. Never point the harness at production. The harness and runbook do not create accounts, databases, VMs, billing, or credentials.

## 5. systemd supervisor template

Install the reviewed application in `/opt/trendscout/Backend` and place a unit such as `/etc/systemd/system/trendscout-d5-probe.service` on each host. Replace the project and run ID through an administrator-controlled environment file; do not put credentials in it.

```ini
[Unit]
Description=TrendScout D5 synthetic multi-host worker probe
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trendscout
Group=trendscout
WorkingDirectory=/opt/trendscout/Backend
EnvironmentFile=/etc/trendscout/d5-probe.env
ExecStart=/opt/trendscout/Backend/.venv/bin/python scripts/multihost_gate_probe.py --project-id ${PROJECT_ID} --confirm-nonproduction-project ${PROJECT_ID} --run-id ${RUN_ID} --acknowledge-nonproduction-writes worker --host-id ${HOST_ID} --expected-hosts ${EXPECTED_HOSTS}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Review unit compatibility with the host's systemd version before installation. The process needs no writable application path. The service account needs only the attached Firestore runtime identity, not an API key file. Verify `systemctl status`, journal output, controlled restart behavior, and clean termination on both hosts.

## 6. Acceptance and limits

**PASS for `PRODUCTION_MULTI_HOST_ACCEPTANCE` requires all of:** two distinct approved host instance IDs; the same organization-owned non-production Firestore database; worker registration from both; one atomic job execution and result with correct synthetic provenance; proof that the other host did not duplicate execution; supervisor restart and subsequent successful work; no provider calls; and reviewed cost/access evidence.

Local multi-process SQLite tests, a fake Firestore SDK, source inspection, or two processes on one machine are not multi-host evidence. Until the real run is approved and completed, keep `PRODUCTION_MULTI_HOST_ACCEPTANCE=BLOCKED` (or `PARTIAL` only after a real two-host run has produced partial evidence). This does not reduce or alter the separately measured `DEMO_ACCEPTANCE` score, and must never be reported as multi-host PASS.

The probe covers the shared backing, atomic queue claim, and process supervision boundary for synthetic jobs. It does not certify public API deployment, provider credentials, provider live access, rate limits across application regions, production IAM design, or provider-cost controls.

## References checked 2026-11-12

- [Firestore pricing](https://cloud.google.com/firestore/pricing): free quotas, read/write/storage/network rates vary by location and billing eligibility.
- [Firestore server-library IAM permissions](https://docs.cloud.google.com/firestore/native/docs/security/iam): transaction begin/rollback, entity reads, and writes map to the permissions listed above; verify exact runtime calls before finalizing the custom role.
- [Compute Engine general-purpose pricing](https://cloud.google.com/products/compute/pricing/general-purpose): E2 on-demand hourly rates vary by region and configuration.
