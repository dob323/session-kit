#!/bin/sh
# PID 1 for a matrix container. Docker mounts the container's own cgroup tree
# read-only; systemd needs it writable to create its scopes. With a private
# cgroup namespace this remount exposes the container's own subtree and nothing
# of the host, which is why the container needs CAP_SYS_ADMIN but not
# --privileged and no host bind mounts.
mount -o remount,rw /sys/fs/cgroup 2>/dev/null ||
  echo 'install-matrix: cgroup remount failed; systemd will not start' >&2
exec /usr/lib/systemd/systemd
