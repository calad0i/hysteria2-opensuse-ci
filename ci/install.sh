#!/usr/bin/env bash
set -euo pipefail
zypper --non-interactive --gpg-auto-import-keys refresh
# The minimal image's busybox-gawk conflicts with rpm-build's GNU gawk.
if rpm -q busybox-gawk >/dev/null 2>&1; then
    zypper --non-interactive remove busybox-gawk
fi
zypper --non-interactive install --no-recommends \
    ca-certificates curl git python3 osc build go gcc rpm-build \
    obs-service-download_files obs-service-go_modules \
    tar gzip util-linux diffutils findutils cpio which
