# VK operational source of truth

This directory mirrors the installed, non-secret VK Browser Node and production
pull/pipeline code as observed on 2026-09-24. The 22 deployed files below were
copied byte for byte; no server, timer, or GitHub state was changed while
preparing this local branch. Git `main` at
`27b9573ee8e499e0ec2699e2cfde94b7e10cb1ba` had none of these `ops/vk`
files. The application code and `VK_PIPELINE_SCHEDULER_DESIGN.md` already
existed in `main`.

The script name `ihc_export.py` is retained to preserve the proven forced-command
SSH protocol. It runs on the Timeweb Browser Node, not the retired IHC host.
Only source code and systemd units are here. No browser profile, session,
cookies, SSH private/public keys, `authorized_keys`, pinned host-key file,
runtime artifacts, SQLite database, or production environment secrets are
included.

## Installed inventory

All hashes are SHA-256 of the deployed file and of its matching file here.
Every listed operational file was absent from Git `main` before this sync.
`ops/vk/.gitattributes` pins LF checkout bytes even on Windows; executable
scripts carry Git executable bits. Restore their runtime owner/group/mode from
the installation runbook, not from the Git checkout alone.

| Host | Deployed path | Repository file | SHA-256 |
| --- | --- | --- | --- |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/reader.py` | `browser-node/reader.py` | `278bf3d3b4e97d8e5d8ede15321085122140280bf310b0ac5c8b45626058585b` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/reader_retention.py` | `browser-node/reader_retention.py` | `cf7619878167ba209e6f962e70191dbdd57df7e529d35ae5e71ca5f29a8ea220` |
| Browser Node | `/usr/local/lib/brucebet-vk-export/ihc_export.py` | `browser-node/ihc_export.py` | `5091141fd7f0a7778261b522abbf3d3608de6ed9e3128902c3ee17e174186601` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/kernel_oom_check.py` | `browser-node/kernel_oom_check.py` | `41cda425b2215c50d8a43bf064298ca4017941e608fa468c839909faeb556072` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/cdp_close.py` | `browser-node/cdp_close.py` | `1d0490a249fd53252daed8b40975aa34e9f1ef5066e48182b3ff47a220dfe4d7` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/cdp_session_probe.py` | `browser-node/cdp_session_probe.py` | `377ffc40ec29a2ec6a6ec21c6144159059f63e3d34ef7b6fd13e42d6a2a3aaa3` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/diagnostic.sh` | `browser-node/diagnostic.sh` | `99cd483f4e000406145484d13a660d1e206d08131856183adcfc2ff8c79e8b0c` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/start-manual-login.sh` | `browser-node/start-manual-login.sh` | `e8a9d6f62a82e65a143f7d77c437cc08e74d8bb8d0733ce3cae432511e111eb5` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/start-persistence.sh` | `browser-node/start-persistence.sh` | `6a67fb238394cb73ba5d937f4d9ee2d3332efddfda52bbc6b34eb9df43cb9328` |
| Browser Node | `/usr/local/lib/brucebet-vk-reader/stop-manual-gui.sh` | `browser-node/stop-manual-gui.sh` | `576938a3d893ad29b77488da5d86df67fecd641cce5341ad55b4bba58d9ecbe1` |
| Browser Node | `/etc/systemd/system/brucebet-vk-reader.service` | `systemd/browser-node/brucebet-vk-reader.service` | `222af108e62d6322858fc35c85a238fb5d9d0f33d9591e2e90ea2730bb9737b1` |
| Browser Node | `/etc/systemd/system/brucebet-vk-reader.service.d/10-retention.conf` | `systemd/browser-node/10-retention.conf` | `a5d02040398e87e323223c371ca2e3e943ffa64e2002f3573a84d99600f3e1ab` |
| Production | `/usr/local/lib/brucebet-vk-pull/timeweb_pull.py` | `production/timeweb_pull.py` | `2af1306af539503a11531dffe0772fc1b8e0550c9c4aeb9482992432a8b6c26d` |
| Production | `/usr/local/lib/brucebet-vk-pull/pull_retention.py` | `production/pull_retention.py` | `c80d18981c25ff4c5afdbb911be36300c8b1e7f7cb488ffec8be8de16bdb5430` |
| Production | `/usr/local/lib/brucebet-vk-pipeline/processor.py` | `production/processor.py` | `b674c4f9978b1c4098554433f6d5cb55956bb91aafb153fabc83262668a7faea` |
| Production | `/usr/local/lib/brucebet-vk-pipeline/pipeline.py` | `production/pipeline.py` | `4bc4342deb58d189d46ed5200d4684c8fec26fd09eb6b5152e88d229436719dc` |
| Production | `/usr/local/lib/brucebet-vk-pipeline/guarded_import.py` | `production/guarded_import.py` | `ebdf323f05e733bf4476912f5db10f3a11122d348266f6c32531cec02df6f29e` |
| Production | `/etc/systemd/system/brucebet-vk-pull.service` | `systemd/production/brucebet-vk-pull.service` | `3ab656b3f4dd4096f045a356b75c96c81a7f9609a2520fa3c1abb160f90cd4df` |
| Production | `/etc/systemd/system/brucebet-vk-pull.service.d/10-scheduler-guards.conf` | `systemd/production/10-scheduler-guards.conf` | `eaf74ac37f45d3389db1a5a9b4d21d9cc92a4bc56cb4f9fb44530d9194ad7844` |
| Production | `/etc/systemd/system/brucebet-vk-pull.timer` | `systemd/production/brucebet-vk-pull.timer` | `d128f769eabb7eceaf7ca251dccc3d436c4591d594485211c3773dea05f530bc` |
| Production | `/etc/systemd/system/brucebet-vk-pipeline.service` | `systemd/production/brucebet-vk-pipeline.service` | `f0491c45c1dfc79b8cd1f68071d4e2f66ac5311559c1370fb8d74434b2b9f374` |
| Production | `/etc/systemd/system/brucebet-vk-pipeline.timer` | `systemd/production/brucebet-vk-pipeline.timer` | `5fcf97694d38a66852a1ab4aa9c98156145d8bc47b93483712acdad02b3709f9` |

## Local candidate, not installed

| Target host | Proposed path | Repository file | SHA-256 |
| --- | --- | --- | --- |
| Browser Node | `/etc/systemd/system/brucebet-vk-reader.timer` | `systemd/browser-node/brucebet-vk-reader.timer` | `a342ccfc068c1f11a8ee61b6476e2bf54260544601f0082f12fddeea208aeaaa` |

This timer is a local source candidate only. It has not been installed,
enabled, or started on the Browser Node. Its calendar is `:00/:20/:40` with a
stable host-specific delay of at most 120 seconds. The existing service's
flock, resource guard, completeness checks, cleanup, and retention remain
unchanged.

## Recovery and enablement boundary

1. Restore non-secret files only after checksum review and a separate change
   approval. Provision the Browser Node user/profile and restricted SSH account
   separately; never commit their credentials. Pin the host key on production
   using the independently verified fingerprint. Keep SSH pull private key on
   production only.
2. The Browser Node has a manual `brucebet-vk-reader.service` and no installed
   reader timer. Production has pull/pipeline service and timer files, but both
   timers are disabled/inactive. Installed does not mean enabled.
3. The current pipeline unit fixes `BRUCEBET_VK_PIPELINE_ALLOW_IMPORT=0`; it
   reconciles against a DB copy and stops at the approval gate for new source.
   The guarded importer uses `notification_chat_ids=()` and does not send
   Telegram messages. Neither timer enablement nor live import is authorized
   by this repository snapshot.
4. The 2026-09-24 cutover proved 71 complete source records, 53 known forecast
   posts and 13 Round 5 posts (`3623`-`3635`). R5 was later recovered through
   a separately approved one-time historical recovery. Timer installation and
   observation-mode enablement still require separate approval.

`tests/test_vk_operational_contract.py` covers the source contracts without
shipping real VK comment bodies. The existing storage no-op regression remains
in `tests/test_storage_write_guards.py`.
