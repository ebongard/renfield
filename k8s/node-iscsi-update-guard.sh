#!/usr/bin/env bash
# Keep `unattended-upgrades` from restarting iscsid under a running Longhorn.
#
# WHY (incident 2026-09-11): apt-daily-upgrade.service ran on k8s-gpu-1 and
# k8s-gpu-2 and restarted iscsid.service (06:19:39 and 06:17:44 UTC). Longhorn
# attaches EVERY volume through that iSCSI initiator, so the restart tore down
# the sessions; Longhorn marked replicas faulted seconds later (06:20:15). With
# `longhorn-pg` running numberOfReplicas=1 there was no second copy, so the
# renfield-pg-3 instance died in BOTH namespaces. Its CNPG HA replication slot
# then pinned WAL on the primary (max_slot_wal_keep_size was unset = unlimited),
# the dedicated 2Gi WAL volume filled in ~10 hours, and both primaries hit
# "PANIC: could not write to file pg_wal/xlogtemp: No space left on device".
# One unattended package upgrade took down two production databases.
#
# It also leaves a SECOND booby trap: Longhorn's already-running engine
# processes cache the iscsid PID namespace path (/host/proc/<pid>/ns/mnt). After
# the restart that path is stale, so any later iSCSI operation on an engine that
# predates the restart fails with
#   nsenter: cannot open /host/proc/<pid>/ns/mnt: No such file or directory
# — which is why the recovery's WAL volume expansion hung until the affected
# engine was recreated.
#
# Longhorn's own docs call this out: the iSCSI daemon must not be restarted
# while volumes are attached. Security updates stay on; only this one package is
# held and pulled in deliberately during a maintenance window (see below).
#
# Run as root on every Longhorn storage node:
#   ssh <node> 'sudo bash -s' < k8s/node-iscsi-update-guard.sh
#
# To update open-iscsi later, do it DELIBERATELY, one node at a time:
#   1. kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
#   2. apt-mark unhold open-iscsi && apt install -y open-iscsi && apt-mark hold open-iscsi
#   3. reboot the node (cleanest way to get consistent iSCSI + Longhorn state)
#   4. kubectl uncordon <node>, wait for Longhorn volumes healthy before the next node
set -euo pipefail

CONF=/etc/apt/apt.conf.d/52-longhorn-iscsi-hold

echo "== $(hostname) =="

# 1) dpkg-level hold: no automatic upgrade path can pull a new open-iscsi in.
apt-mark hold open-iscsi >/dev/null
echo "apt-mark hold: $(apt-mark showhold | tr '\n' ' ')"

# 2) unattended-upgrades blacklist: belt and suspenders. The hold alone already
#    stops it, but the blacklist makes the intent visible in the u-u config and
#    survives someone clearing holds wholesale.
cat > "$CONF" <<'CONFEOF'
// See k8s/node-iscsi-update-guard.sh in the renfield repo.
// Restarting iscsid tears down every attached Longhorn volume (incident
// 2026-09-11: two production Postgres clusters lost). Upgrade this package
// deliberately, with the node drained, never from the nightly job.
Unattended-Upgrade::Package-Blacklist {
    "open-iscsi";
};
CONFEOF
echo "geschrieben: $CONF"

# 3) Prove the config still parses — a broken apt.conf breaks ALL apt runs.
apt-config dump >/dev/null && echo "apt-config: ok"
echo -n "blacklist wirksam: "
apt-config dump 2>/dev/null | grep -c 'Package-Blacklist::.*open-iscsi' || true

echo "iscsid laeuft seit: $(systemctl show iscsid -p ActiveEnterTimestamp --value)"
