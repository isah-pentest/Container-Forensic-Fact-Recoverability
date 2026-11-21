# Series F deviations record

No deviation from the frozen plan, schedule, code or run procedure: 26 of 26 runs in schedule order, no repeats, no stop rule, no controller error (session exit 0).

Procedural notes (qualification, before the freeze; no effect on any counted run):
1. RUNBOOK_F step 1: `g1_cluster_down.sh` was not run, because no cluster existed on frx-hv.
2. The kind network did not exist on the new host, so a temporary cluster `frx-netinit` was created and deleted to let kind create its own network (gateway 172.18.0.1, as the runbook assumes). The study cluster `frx` was created afterwards with k = 1.
3. The audit reference receiver was stopped and restarted once, before the cluster existed (append mode; nothing had been received).
4. The commit message of the series-F code commit was edited by the author before `git am`. The tree (e99e10d585728129530d084c3774df1366077643) is identical to the tested tree.
5. cifs-utils was installed on frx-hv. The package manager reported a pending kernel 7.0.0-42; frx-hv was not rebooted, and every run used 7.0.0-38-generic (fingerprint 0a4024d7…).
