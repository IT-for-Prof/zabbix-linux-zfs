# Linux ZFS by Zabbix agent active

> Zabbix 8.0 template: OpenZFS pool state and disk errors on Linux, with stock agent keys only.

The template raises a problem when a ZFS pool loses a disk, fails or disappears. A separate trigger
fires on read, write and checksum errors on the disks of a pool that is still working. It needs no scripts, no
UserParameter and no sudo. The agent config gets one `AllowKey` line.

This file describes the English version, `template/linux_zfs_en.yaml`. The Russian
version of the same template is `template/linux_zfs.yaml`, described in
[README.md](README.md). The files share UUIDs and the template name, so importing either one updates
an installed template in place and keeps its history. A server runs one of the two.

**Author:** Konstantin Tyutyunnik / Константин Тютюнник · [itforprof.com](https://itforprof.com) · MIT

## Why

The official Zabbix 8.0 templates have no ZFS template. Proxmox VE by HTTP gets a ZFS-backed storage
as a directory, and its data has no pool state. If a disk drops out of a raidz2, no alert fires.

Community ZFS templates use UserParameter and `sudo zpool`. This one does not:
- the OpenZFS kernel module writes the pool state to `/proc/spl/kstat/zfs/<pool>/state`, which any user
  can read;
- `zpool status` on Linux also works without root: `/dev/zfs` has mode `0666`, and status requests
  need no privileges.

## What it collects

| Item | Key | Interval |
|---|---|---|
| Pool discovery | `vfs.dir.get[/proc/spl/kstat/zfs,,"^\$import$",dir,,0]` | 1h |
| Pool state (prototype) | `vfs.file.contents[/proc/spl/kstat/zfs/{#POOLNAME}/state]` | 1m |
| `zpool status -x` output | `system.run[/usr/sbin/zpool status -x]` | 5m |

`zpool import` creates the `$import` directory while it searches for pools, and discovery skips it.
If the agent cannot run `zpool status -x`, the preprocessing step "Check for not supported value" stores
the value `ZFS_NOT_COLLECTED` instead of the error. The "Matches regular expression" step sets the same marker
when the output does not look like a `zpool` answer: `system.run` returns text, not an exit code, so
`sh: …: not found` would otherwise be stored as an ordinary value. While the last value is the marker, open
problems stay open: both triggers of the item have a recovery expression. The item does not become unsupported, so the trigger on
that value fires: on an unsupported item, triggers go to the unknown state and raise no alert.

Unchanged `zpool status -x` output is stored once a day. The pool state is stored every minute: the
"no data" trigger watches that item, and it would count a discarded repeat as missing data.

## Triggers

| Trigger | Severity | Fires when |
|---|---|---|
| `ZFS {#POOLNAME}: pool is degraded` | Average | two values in a row are `DEGRADED`: a disk dropped out, the pool runs on its redundancy |
| `ZFS {#POOLNAME}: pool has failed` | High | two values in a row are neither `ONLINE` nor `DEGRADED`: `FAULTED`, `UNAVAIL`, `SUSPENDED` and others |
| `ZFS {#POOLNAME}: no data for {$ZFS.NODATA}` | High | the state file has been missing for 35 minutes: the pool was exported or not imported after a reboot |
| `ZFS: errors on an ONLINE pool` | Average | `zpool status -x` reports READ/WRITE/CKSUM errors or corrupted data on a pool that is still `ONLINE` |
| `ZFS: zpool status -x is not collected` | Average | for 10 minutes the agent cannot run `zpool status -x`: no `AllowKey` line, no `zpool`, or `DenyKey` forbids the command |

- **Two values in a row.** While a pool is imported after boot, it briefly shows `TRANSITIONING`.
  A single such value does not raise a trigger.
- **"No data" depends on `Linux: Zabbix agent is not available`.** While the agent is down, the template
  raises no problems: the 35-minute window is longer than the agent timeout (`{$AGENT.NODATA_TIMEOUT}`,
  30 minutes) plus the item interval. The other triggers evaluate the last values received, and those
  values do not change while the agent is down.
- **Errors and states do not overlap.** `zpool status -x` also prints degraded pools. So the error
  trigger needs both `state: ONLINE` and the line `One or more devices has experienced`, which starts
  both OpenZFS error statuses, `FAILING_DEV` and `CORRUPT_DATA`. A resilver, a non-native ashift and an
  errata notice also print `ONLINE`, but the trigger ignores them. Otherwise it would stay in problem
  state and hide the next real error.

## Requirements

- Zabbix 8.0.
- Zabbix agent 2 in active mode, tested on 8.0.0.
- OpenZFS on Linux, tested on 2.2.10 (Proxmox VE 8).
- `Linux by Zabbix agent active` linked to the host: its agent availability trigger suppresses
  "no data".
- The agent runs with an English locale, or `zpool` has no translation (OpenZFS packages ship none).
  The error trigger matches English text.

## Installation

1. Allow the command in the agent config and restart the agent:

   ```sh
   echo 'AllowKey=system.run[/usr/sbin/zpool status -x]' > /etc/zabbix/zabbix_agent2.d/zfs.conf
   systemctl restart zabbix-agent2
   ```

   The line allows exactly this command. If `zpool` is not in `/usr/sbin`, change the path in both the
   line and the item key: the key must match the `AllowKey` line byte for byte.

2. Test the keys as the agent user:

   ```sh
   cd /tmp && su -s /bin/sh zabbix -c "zabbix_agent2 -t 'system.run[/usr/sbin/zpool status -x]'"
   cd /tmp && su -s /bin/sh zabbix -c "zabbix_agent2 -t 'vfs.dir.get[/proc/spl/kstat/zfs,,\"^\\\$import\$\",dir,,0]'"
   ```

   On a healthy host the first command returns `all pools are healthy`, the second returns JSON with the
   pool directories.

3. Import the YAML (Data collection → Templates → Import) and link the template to the host.

The agent runs pool discovery once an hour. The server cannot start an active discovery, so the state
items appear within an hour of linking.

## Macros

| Macro | Value | Meaning |
|---|---|---|
| `{$ZFS.NODATA}` | `35m` | the "no data" window. It must be longer than `{$AGENT.NODATA_TIMEOUT}` plus the item interval, or the trigger fires before the agent availability dependency can suppress it |

## What it does not catch

- **Pool space.** `Linux by Zabbix agent active` tracks it on mounted datasets: `zfs` is in
  `{$VFS.FS.FSTYPE.MATCHES}`. A second trigger on the same fact would be a duplicate.
- **Hot spares.** They are not part of the pool state and `zpool status -x` does not check them. A failed spare
  shows up only in the disk's SMART.
- **Age of the last scrub.** There is no stock source without root.
- **Precision on a host with several pools.** The error trigger checks the whole `zpool status -x` output,
  not each pool. If one pool is degraded with disk errors while another is `ONLINE` and resilvering, the
  error trigger fires when it should not.

## Testing the template

```sh
python3 -m pytest -q tests
```

The tests check the "no data" dependency and its window, the severity for each OpenZFS pool state, the
error trigger against sample `zpool status -x` output, the trigger on lost collection, and that the Russian and English versions differ
only in their texts.

## License

MIT. See [LICENSE](LICENSE).
