# Evidence from real switches

Output from live runs on the Google Cloud lab: Nexus 9000v 10.4(8) and vJunos-switch 26.2R1.7 in containerlab. Lines are unedited; files are trimmed to the relevant run, the Ansible file to the Cisco plays, and file paths are shortened. Times are US Eastern (EDT).

GitHub shows `.html` files as source code. Download one and open it in a browser to see the report the pipeline produced; the `.log` next to it has the same run as plain text.

| # | Files | Run | What it shows |
|---|---|---|---|
| 1 | [01-full-fabric-deploy.log](01-full-fabric-deploy.log), [report](01-full-fabric-deploy-report.html) | Oct 10, 9:22 PM · 3 Cisco + 3 Juniper (spine2 on Junos) | Plan touches only the 3 new Junos devices; spine2, then canary leaf3, then leaf4, each polled until BGP and VTEP peers converge; **fabric-wide check: all 6 devices healthy** |
| 2 | [02-canary-rollback.log](02-canary-rollback.log), [report](02-canary-rollback-report.html) | Oct 11, 1:18 PM · 4 Cisco + 2 Juniper (current design) | Greenfield deploy of all 6: spines confirmed, Cisco canary leaf1 converges and is confirmed, **Junos canary leaf3 times out on commit → rolled back, rollout halted, leaf2 and leaf4 never touched** |
| 3 | [03-ping-matrix.txt](03-ping-matrix.txt) | Oct 11, 2:46 AM · 3 Cisco + 3 Juniper | 16-check data-plane test from the hosts. **All 9 checks between deployed leaves pass**, including Cisco ↔ Juniper over L2 VXLAN and routed over the L3VNI; the 7 failures all involve leaf4, which had not finished deploying |
| 4 | [04-ansible-check-cisco.txt](04-ansible-check-cisco.txt) | Oct 11 · Cisco switches | Ansible (`cisco.nxos` collection) against the live Cisco switches: `changed=0` on spine1, leaf1 and leaf2, so Ansible agrees with what the pipeline deployed |

Run 2 is the safety design working as intended: the pipeline could not confirm the Juniper canary, so it rolled it back and stopped before the batch wave. The slow commit is the lab's limit (see *Lab limits* in the main README), not a fabric fault.
