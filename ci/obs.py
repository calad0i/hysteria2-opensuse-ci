#!/usr/bin/env python3
"""Small, fail-closed helpers for the Hysteria -> OBS release workflow."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UPSTREAM = "HyNetworks/hysteria"
API = "https://api.opensuse.org"
PUBLIC_API = API + "/public"
PACKAGE = "hysteria"
ROOT = Path(__file__).resolve().parent.parent


def project():
    value = os.environ.get("OBS_PROJECT", "home:calad")
    if not re.fullmatch(r"[A-Za-z0-9_.:+-]+", value):
        raise ValueError("Invalid OBS_PROJECT")
    return value


def fetch(url):
    headers = {"User-Agent": "hysteria2-opensuse-ci"}
    if urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def github(path):
    return json.loads(fetch(f"https://api.github.com/repos/{UPSTREAM}/{path}"))


def version_tuple(version):
    if not re.fullmatch(r"2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError(f"Expected a stable Hysteria 2 version, got {version!r}")
    return tuple(map(int, version.split(".")))


def release_version(release):
    tag = release["tag_name"]
    if release.get("draft") or release.get("prerelease") or not tag.startswith("app/v"):
        raise ValueError(f"Not a stable app release: {tag!r}")
    version = tag.removeprefix("app/v")
    version_tuple(version)
    return version


def spec_version(spec):
    matches = re.findall(r"^Version:\s*(\S+)\s*$", spec, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("Expected exactly one literal Version field")
    version_tuple(matches[0])
    return matches[0]


def needs_update(current, candidate, force=False):
    old, new = version_tuple(current), version_tuple(candidate)
    if new < old:
        return False  # Never downgrade, including forced/manual runs.
    return new > old or force


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(*args, cwd=None, env=None, capture=False):
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def osc_config(path):
    """Credentials are supplied only through OSC_USERNAME / OSC_PASSWORD."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # GitHub's openSUSE containers run as root and need no sudo installation.
    wrapper = "" if os.geteuid() == 0 else "sudo"
    path.write_text(
        f"[general]\napiurl = {API}\nsu-wrapper = {wrapper}\n\n"
        + "\n".join(
            f"[{url}]\nuser = anonymous\npass =\n"
            "credentials_mgr_class = osc.credentials.PlaintextConfigFileCredentialsManager\n"
            for url in (API, PUBLIC_API)
        )
    )
    path.chmod(0o600)


def osc(*args, cwd=None, public=False, capture=False):
    config = Path(os.environ.get("OSC_CONFIG", str(ROOT / ".work/oscrc"))).resolve()
    osc_config(config)
    return run("osc", "--config", str(config), "-A", PUBLIC_API if public else API,
               *args, cwd=cwd, capture=capture)


def require_credentials():
    if not os.environ.get("OSC_USERNAME") or not os.environ.get("OSC_PASSWORD"):
        raise ValueError("Set OBS_USERNAME (variable) and OBS_PASSWORD (secret) in GitHub Actions")


def detect(args):
    tag = args.tag
    if tag and not re.fullmatch(r"app/v2\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Tag must be app/v2.MINOR.PATCH")
    release = github("releases/tags/" + urllib.parse.quote(tag, safe="") if tag else "releases/latest")
    version = release_version(release)
    source_url = f"{PUBLIC_API}/source/{project()}/{PACKAGE}"
    directory = ET.fromstring(fetch(source_url))
    revision = directory.attrib["srcmd5"]
    spec = fetch(f"{source_url}/hysteria.spec?rev={revision}").decode()
    current = spec_version(spec)
    changed = needs_update(current, version, args.force)
    print(f"OBS: {current}; upstream: {version}; update: {str(changed).lower()}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"changed={str(changed).lower()}\nversion={version}\n")
    if not changed:
        return
    commit = github("commits/" + urllib.parse.quote(release["tag_name"], safe=""))
    sha = commit["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid upstream commit SHA")
    date = commit["commit"]["committer"]["date"]
    epoch = int(dt.datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())
    write_json(args.output, dict(project=project(), package=PACKAGE, version=version,
                                tag=release["tag_name"], commit=sha, epoch=epoch,
                                date=date, base_srcmd5=revision, previous_version=current))


def replace_once(text, pattern, replacement):
    result, count = re.subn(pattern, lambda _: replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected one spec match, found {count}: {pattern}")
    return result


def update_spec(spec, release):
    # Match known packaging constructs strictly: unexpected OBS edits need review.
    spec_version(spec)
    spec = replace_once(spec, r"^Version:.*$", f"Version:        {release['version']}")
    spec = replace_once(spec, r"^Release:.*$", "Release:        0")
    macro = f"%global upstream_commit {release['commit']}"
    if re.search(r"^%global upstream_commit .*", spec, re.MULTILINE):
        spec = replace_once(spec, r"^%global upstream_commit .*$", macro)
    else:
        spec = replace_once(spec, r"^Name:.*$", macro + "\n\nName:           hysteria")
    spec = replace_once(spec, r"^URL:.*$", f"URL:            https://github.com/{UPSTREAM}")
    spec = replace_once(spec, r"^Source0:.*$",
                        f"Source0:        https://github.com/{UPSTREAM}/archive/%{{upstream_commit}}.tar.gz#/v%{{version}}.tar.gz")
    spec = replace_once(spec, r"^Source1:.*$", "Source1:        vendor.tar.gz")
    spec = re.sub(r"^tar -xf %\{SOURCE1\} -C app\n", "", spec, flags=re.MULTILINE)
    spec = replace_once(spec, r"^%setup (?:-a 1 )?-n .*?$",
                        "%setup -n %{name}-%{upstream_commit}\ntar -xf %{SOURCE1} -C app")
    spec = re.sub(r"^mv go %\{_builddir\}\n\n", "", spec, flags=re.MULTILINE)
    offline = ("export GOPATH=%{_builddir}/go\n"
               "export GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local\n"
               "export GOFLAGS=-mod=vendor GOWORK=off")
    spec = re.sub(r"^export GOPROXY=.*\n|^export GOFLAGS=-mod=vendor(?: GOWORK=off)?\n",
                  "", spec, flags=re.MULTILINE)
    spec = replace_once(spec, r"^export GOPATH=.*$", offline)
    spec = replace_once(spec, r"^_date=.*$", f"_date={release['date']}")
    # go_modules vendors app/go.mod, including its local core/extras replacements.
    # Build inside app and keep the existing binary paths at the source root.
    for suffix in ("", "-avx"):
        pattern = (r"^(\s*)go (?:-C app )?build (.+) -o (?:\.\./)?%\{name\}"
                   + re.escape(suffix) + r" (?:\./app|\.)$")
        matches = list(re.finditer(pattern, spec, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"Expected one Go build command for hysteria{suffix}")
        match = matches[0]
        spec = (spec[:match.start()] + f"{match[1]}go -C app build {match[2]} "
                f"-o ../%{{name}}{suffix} ." + spec[match.end():])
    return spec


def go_requirement(archive, commit):
    versions = []
    with tarfile.open(archive) as source:
        for name in ("go.work", "app/go.mod", "core/go.mod", "extras/go.mod"):
            with source.extractfile(f"hysteria-{commit}/{name}") as stream:
                match = re.search(r"^go ([0-9]+\.[0-9]+(?:\.[0-9]+)?)$",
                                  stream.read().decode(), re.MULTILINE)
            if not match:
                raise ValueError(f"Missing Go version in {name}")
            versions.append(tuple(map(int, match[1].split("."))))
    return ".".join(map(str, max(versions)))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def services(args):
    from osc.core import Serviceinfo

    # Use osc's native executor for the package's _service. The CLI also POSTs
    # getprojectservices, which is unavailable through OBS's anonymous public API.
    service = Serviceinfo()
    service.read(ET.parse("_service").getroot())
    os.environ.update(GOWORK="off", GOTOOLCHAIN="local")
    raise SystemExit(service.execute(str(Path.cwd()), callmode="manual"))


def prepare(args):
    release = json.loads(args.release.read_text())
    package = args.package.resolve()
    package.parent.mkdir(parents=True, exist_ok=True)
    if not (package / ".osc").is_dir():
        osc("checkout", "--output-dir", str(package),
            release["project"], PACKAGE, public=True)
    baseline = ET.fromstring((package / ".osc/_files").read_text()).attrib["srcmd5"]
    if baseline != release["base_srcmd5"]:
        raise ValueError("Checkout does not match the detected OBS revision")
    # A commit archive is immutable even if upstream later moves its release tag.
    archive = package / f"v{release['version']}.tar.gz"
    spec_path = package / "hysteria.spec"
    spec_path.write_text(update_spec(spec_path.read_text(), release))
    services = ET.parse(ROOT / "_service")
    services.find("./service[@name='go_modules']/param[@name='archive']").text = archive.name
    services.write(package / "_service", encoding="unicode")
    # Fetch afresh even on a forced rebuild of a version previously archived by tag.
    archive.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="hysteria-services-") as temp:
        env = dict(os.environ, GOPATH=temp, GOMODCACHE=str(Path(temp) / "pkg/mod"),
                   GOWORK="off", GOTOOLCHAIN="local")
        run(sys.executable, str(ROOT / "ci/obs.py"), "services", cwd=package, env=env)
    requirement = go_requirement(archive, release["commit"])
    spec_path.write_text(replace_once(spec_path.read_text(), r"^BuildRequires:\s+golang\(API\).*$",
                                      f"BuildRequires:  golang(API) >= {requirement}"))
    (package / "hysteria.obsinfo").write_text(
        f"name: hysteria\nversion: {release['version']}\nmtime: {release['epoch']}\ncommit: {release['commit']}\n")
    timestamp = dt.datetime.fromtimestamp(release["epoch"], dt.timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
    changes = package / "hysteria.changes"
    changes.write_text(
        "-" * 67 + f"\n{timestamp} - Hysteria OBS CI <noreply@github.com>\n\n"
        f"- Update to {release['version']} ({release['tag']}).\n"
        f"  https://github.com/{UPSTREAM}/releases/tag/{release['tag']}\n"
        "- Refresh offline vendor sources with OBS source services.\n\n" + changes.read_text())
    # Remove only the old source archive, never unrelated downstream sources.
    previous = package / f"v{release['previous_version']}.tar.gz"
    if previous != archive:
        previous.unlink(missing_ok=True)
    release["go_version"] = requirement
    release["files"] = {p.name: sha256(p) for p in sorted(package.iterdir()) if p.is_file()}
    args.output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output / "package.tar.gz", "w:gz") as tar:
        for name in release["files"]:
            tar.add(package / name, arcname=name)
    write_json(args.output / "release.json", release)
    print(f"Prepared {release['tag']}; vendor.tar.gz: {(package / 'vendor.tar.gz').stat().st_size:,} bytes")


def verify_files(package, release):
    actual = {p.name: sha256(p) for p in package.iterdir() if p.is_file()}
    if actual != release["files"]:
        raise ValueError("Prepared source files do not match the manifest")


def unpack(args):
    from osc.core import Package

    release = json.loads((args.artifacts / "release.json").read_text())
    args.package.mkdir(parents=True, exist_ok=False)
    with tarfile.open(args.artifacts / "package.tar.gz") as tar:
        tar.extractall(args.package, filter="data")
    verify_files(args.package, release)
    # Initialize local osc metadata without another download or credentials.
    Package.init_package(API, release["project"], PACKAGE, str(args.package))


def publish(args):
    require_credentials()
    release = json.loads((args.artifacts / "release.json").read_text())
    package = args.package.resolve()
    package.parent.mkdir(parents=True, exist_ok=True)
    # Keep osc's numeric working-copy revision, then verify the tested contents.
    # A checkout by srcmd5 stores a hash as rev, which osc commit rejects as stale.
    osc("checkout", "--output-dir", str(package),
        release["project"], PACKAGE)
    current = ET.fromstring((package / ".osc/_files").read_text())
    if current.attrib["srcmd5"] != release["base_srcmd5"]:
        raise ValueError("OBS changed during the build; rerun to test the new baseline")
    with tempfile.TemporaryDirectory(prefix="hysteria-publish-") as temp:
        temp = Path(temp)
        with tarfile.open(args.artifacts / "package.tar.gz") as tar:
            tar.extractall(temp, filter="data")
        verify_files(temp, release)
        for path in package.iterdir():
            if path.is_file() and path.name not in release["files"]:
                path.unlink()
        for name in release["files"]:
            shutil.copyfile(temp / name, package / name)
    osc("addremove", cwd=package)
    osc("commit", "-m",
        f"Update to {release['tag']} ({release['commit'][:12]}); tested by GitHub Actions", cwd=package)
    print(f"Uploaded {release['tag']} to {release['project']}/{PACKAGE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("detect")
    check.add_argument("--tag", default="")
    check.add_argument("--force", action="store_true", help="Rebuild the same version; never downgrade")
    check.add_argument("--output", type=Path, default=Path(".work/release.json"))
    prep = commands.add_parser("prepare")
    prep.add_argument("--release", type=Path, default=Path(".work/release.json"))
    prep.add_argument("--package", type=Path, default=Path(".work/package"))
    prep.add_argument("--output", type=Path, default=Path("artifacts"))
    for name in ("unpack", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--artifacts", type=Path, default=Path("artifacts"))
        command.add_argument("--package", type=Path, default=Path(".work/package"))
    commands.add_parser("config")
    commands.add_parser("services", help="Run this checkout's manual OBS source services without authentication")
    args = parser.parse_args()
    if args.command == "config":
        osc_config(Path(os.environ["OSC_CONFIG"]))
    else:
        globals()[args.command](args)


if __name__ == "__main__":
    main()
