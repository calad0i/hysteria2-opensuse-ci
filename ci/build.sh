#!/usr/bin/env bash
set -euo pipefail

phase=$1
repository=$2
arch=$3
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace=$(dirname "$script_dir")
build_root=/var/tmp/hysteria-build
# osc appends .mount when --vm-type=chroot is explicitly selected.
chroot_root=$build_root/.mount
: "${OSC_CONFIG:?OSC_CONFIG must point outside the source package}"
: "${XDG_STATE_HOME:?XDG_STATE_HOME must point to ephemeral osc state}"

python3 "$script_dir/obs.py" config
cd "$workspace/.work/package"
common=(--skip-local-service-run --trust-all-projects --nopreinstallimage
    --vm-type=chroot --root "$build_root")

case "$phase" in
    preload)
        : "${OSC_USERNAME:?Set OBS_USERNAME in Actions variables}"
        : "${OSC_PASSWORD:?Set OBS_PASSWORD in Actions secrets}"
        # Preloading does not execute the upstream build. Remove session cookies
        # as well as configuration, even when a download fails.
        trap 'rm -f -- "$OSC_CONFIG"; rm -rf -- "${XDG_STATE_HOME:?}/osc"' EXIT
        osc --config "$OSC_CONFIG" -A https://api.opensuse.org build \
            "${common[@]}" --preload "$repository" "$arch" hysteria.spec
        ;;
    build)
        unset OSC_USERNAME OSC_PASSWORD
        # Only RPMs from this build may satisfy the acceptance test.
        rm -rf -- "$workspace/artifacts/rpms"
        mkdir -p "$workspace/artifacts/rpms"
        # Disable networking at the OS level, in addition to Go's offline flags.
        # pipefail ensures a failed osc build cannot be hidden by tee.
        {
            unshare --net osc --config "$OSC_CONFIG" -A https://api.opensuse.org build \
                "${common[@]}" --offline --clean --no-changelog \
                --keep-pkgs "$workspace/artifacts/rpms" "$repository" "$arch" hysteria.spec

            # Exercise the delivered payload, using the target's runtime libraries.
            # Completion/debug/source RPMs cannot satisfy the executable check.
            mkdir -p "$chroot_root/tmp"
            shopt -s nullglob globstar
            found=false
            for package in "$workspace/artifacts/rpms"/**/*.rpm; do
                case "$package" in
                    *.src.rpm|*.nosrc.rpm) continue ;;
                esac
                [[ $(rpm -qp --queryformat '%{NAME} %{ARCH}' "$package") == "hysteria $arch" ]] || continue
                found=true
                payload=$(mktemp -d "$chroot_root/tmp/hysteria-rpm.XXXXXX")
                trap 'rm -rf -- "$payload"' EXIT
                (
                    cd "$payload"
                    rpm2cpio "$package" | cpio -idm --quiet --no-absolute-filenames
                )
                if [[ ! -x "$payload/usr/bin/hysteria" ]]; then
                    printf 'Missing executable /usr/bin/hysteria in %s\n' "$package" >&2
                    exit 1
                fi
                printf 'Running /usr/bin/hysteria from %s\n' "$package"
                timeout 30s unshare --net chroot "$chroot_root" \
                    "${payload#"$chroot_root"}/usr/bin/hysteria" version
                rm -rf -- "$payload"
                trap - EXIT
            done
            if [[ $found != true ]]; then
                printf 'osc build produced no hysteria RPM for %s\n' "$arch" >&2
                exit 1
            fi
            printf 'PASS: osc build produced RPMs and the packaged Hysteria executable runs.\n'
        } 2>&1 | tee "$workspace/artifacts/build.log"
        ;;
    *) printf 'Unknown phase: %s\n' "$phase" >&2; exit 2 ;;
esac
