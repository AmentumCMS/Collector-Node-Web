#!/usr/bin/env python3
"""
Concurrent Verdaccio-to-Nexus npm importer.

Purpose:
  Import npm packages collected in a Verdaccio storage archive into a Nexus npm hosted repository.

Expected input example:
  collector-node-web-verdaccio-20260511-2310.tgz
    verdaccio/
      storage/
        write-file-atomic/
          package.json
          write-file-atomic-5.0.1.tgz

What this script does:
  1. Accepts either a Verdaccio directory or a .tgz/.tar.gz archive.
  2. Safely extracts archives to a temporary directory.
  3. Recursively finds npm package .tgz files.
  4. Reads package/package.json from inside each package tarball.
  5. Uses the embedded package name/version, not the filename.
  6. Checks whether name@version already exists in Nexus.
  7. Publishes missing packages with npm publish.
  8. Runs publish jobs concurrently.

Auth:
  Recommended:
    Use an .npmrc that already authenticates to Nexus.

  Example .npmrc:
    registry=https://nexus.example.com/repository/npm-hosted/
    always-auth=true
    //nexus.example.com/repository/npm-hosted/:_authToken=YOUR_TOKEN_HERE
    strict-ssl=false

  Or login once:
    npm login --registry "https://nexus.example.com/repository/npm-hosted/"

  Optional metadata check auth through env vars:
    NPM_TOKEN=xxxxx

  Or:
    NEXUS_USERNAME=xxxxx
    NEXUS_PASSWORD=xxxxx

Usage:
  python3 import-verdaccio-to-nexus.py \
    --input collector-node-web-verdaccio-20260511-2310.tgz \
    --registry https://nexus.example.com/repository/npm-hosted/ \
    --workers 8

Dry run:
  python3 import-verdaccio-to-nexus.py \
    --input collector-node-web-verdaccio-20260511-2310.tgz \
    --registry https://nexus.example.com/repository/npm-hosted/ \
    --workers 8 \
    --dry-run

Notes:
  - Start with --workers 8.
  - Increase to 12 or 16 if Nexus handles it well.
  - Decrease if Nexus returns 429, 5xx, or lock/contention errors.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import queue
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class PackageTarball:
    path: Path
    name: str
    version: str

    @property
    def spec(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass
class ImportResult:
    status: str
    package: str
    path: str
    message: str = ""


print_lock = threading.Lock()


def log(message: str) -> None:
    with print_lock:
        print(message, flush=True)


def safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    """
    Safely extract a tar archive and prevent path traversal.

    Blocks entries like:
      ../../evil
      /absolute/path
    """
    destination = destination.resolve()

    for member in tar.getmembers():
        member_path = (destination / member.name).resolve()

        if not str(member_path).startswith(str(destination)):
            raise ValueError(f"unsafe path in tar archive: {member.name}")

    tar.extractall(destination)


def prepare_input(input_path: Path) -> tuple[Path, Optional[tempfile.TemporaryDirectory]]:
    """
    Accepts:
      - directory
      - .tgz
      - .tar.gz

    Returns:
      - directory to scan
      - temp directory handle, if archive was extracted
    """
    input_path = input_path.resolve()

    if input_path.is_dir():
        return input_path, None

    if not input_path.is_file():
        raise FileNotFoundError(f"input does not exist: {input_path}")

    input_name = input_path.name.lower()
    if not (input_name.endswith(".tgz") or input_name.endswith(".tar.gz")):
        raise ValueError(f"input must be a directory, .tgz, or .tar.gz file: {input_path}")

    temp_dir = tempfile.TemporaryDirectory(prefix="verdaccio-import-")
    extract_root = Path(temp_dir.name)

    log(f"Extracting archive: {input_path}")
    log(f"Extract target: {extract_root}")

    try:
        with tarfile.open(input_path, "r:gz") as tar:
            safe_extract_tar(tar, extract_root)
    except Exception:
        temp_dir.cleanup()
        raise

    return extract_root, temp_dir


def is_probably_collector_archive(path: Path) -> bool:
    """
    Avoid accidentally treating a top-level collector archive as an npm package.
    After extraction, this usually does not matter, but it helps if someone points
    the script at a directory containing collector artifacts.
    """
    name = path.name.lower()

    collector_markers = [
        "verdaccio",
        "collector",
        "node-web",
        "node-collector",
    ]

    return any(marker in name for marker in collector_markers)


def discover_tarballs(root: Path, include_collector_named_tgz: bool) -> list[Path]:
    """
    Finds package tarballs.

    Verdaccio package folders usually look like:
      verdaccio/storage/write-file-atomic/write-file-atomic-5.0.1.tgz

    This function finds every .tgz and lets read_package_json_from_tgz decide
    whether it is a valid npm package tarball.
    """
    tarballs: list[Path] = []

    for path in root.rglob("*.tgz"):
        if not path.is_file():
            continue

        if not include_collector_named_tgz and is_probably_collector_archive(path):
            continue

        tarballs.append(path)

    return sorted(tarballs)


def read_package_json_from_tgz(path: Path) -> PackageTarball:
    """
    Reads package identity from the package tarball itself.

    Typical npm tarball content:
      package/package.json
      package/index.js
      package/LICENSE
      ...

    The script intentionally does not rely on the tarball filename.
    """
    try:
        with tarfile.open(path, "r:gz") as tar:
            package_json_member = None

            for member in tar.getmembers():
                normalized = member.name.replace("\\", "/").lstrip("./")

                if normalized == "package/package.json":
                    package_json_member = member
                    break

                if normalized.endswith("/package.json"):
                    package_json_member = member

            if package_json_member is None:
                raise ValueError("package.json not found inside tarball")

            extracted = tar.extractfile(package_json_member)
            if extracted is None:
                raise ValueError("could not extract package.json")

            data = json.loads(extracted.read().decode("utf-8", errors="replace"))

            name = data.get("name")
            version = data.get("version")

            if not name or not version:
                raise ValueError("package.json is missing name or version")

            if not isinstance(name, str) or not isinstance(version, str):
                raise ValueError("package name/version must be strings")

            return PackageTarball(
                path=path,
                name=name.strip(),
                version=version.strip(),
            )

    except Exception as exc:
        raise ValueError(f"{path}: failed reading package metadata: {exc}") from exc


def find_nearby_verdaccio_package_json(package_tgz: Path) -> Optional[Path]:
    """
    Verdaccio package folders may include a metadata package.json next to tarballs.

    Example:
      verdaccio/storage/write-file-atomic/package.json
      verdaccio/storage/write-file-atomic/write-file-atomic-5.0.1.tgz

    We do not use this as the source of truth for publishing because the tarball's
    embedded package/package.json is safer. This is only for diagnostics.
    """
    candidate = package_tgz.parent / "package.json"
    return candidate if candidate.is_file() else None


def load_packages(tarballs: list[Path]) -> tuple[list[PackageTarball], list[ImportResult]]:
    packages: list[PackageTarball] = []
    failures: list[ImportResult] = []
    seen: set[tuple[str, str]] = set()

    for tgz in tarballs:
        try:
            package = read_package_json_from_tgz(tgz)
            key = (package.name, package.version)

            if key in seen:
                failures.append(
                    ImportResult(
                        status="duplicate",
                        package=package.spec,
                        path=str(package.path),
                        message="duplicate tarball for same package name/version; ignored",
                    )
                )
                continue

            seen.add(key)
            packages.append(package)

        except Exception as exc:
            nearby_package_json = find_nearby_verdaccio_package_json(tgz)
            extra = ""

            if nearby_package_json:
                extra = f"; nearby Verdaccio metadata found at {nearby_package_json}"

            failures.append(
                ImportResult(
                    status="invalid",
                    package="unknown",
                    path=str(tgz),
                    message=f"{exc}{extra}",
                )
            )

    return packages, failures


def registry_package_url(registry: str, package_name: str) -> str:
    """
    Build NPM registry package metadata URL.

    Examples:
      lodash       -> https://nexus/repository/npm-hosted/lodash
      @scope/pkg   -> https://nexus/repository/npm-hosted/@scope%2Fpkg
    """
    registry = registry.rstrip("/")
    encoded = urllib.parse.quote(package_name, safe="@")
    return f"{registry}/{encoded}"


def build_auth_header() -> Optional[str]:
    """
    Used for direct HTTP metadata checks.

    npm publish itself still uses npm/.npmrc auth.
    """
    token = os.environ.get("NPM_TOKEN")
    if token:
        return f"Bearer {token}"

    username = os.environ.get("NEXUS_USERNAME")
    password = os.environ.get("NEXUS_PASSWORD")

    if username and password:
        raw = f"{username}:{password}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        return f"Basic {encoded}"

    return None


def exists_via_registry_metadata(
    package: PackageTarball,
    registry: str,
    timeout: int,
) -> Optional[bool]:
    """
    Direct registry metadata existence check.

    Returns:
      True  -> exact package version exists
      False -> package/version does not exist
      None  -> unable to determine; caller should fallback to npm view
    """
    url = registry_package_url(registry, package.name)

    headers = {
        "Accept": "application/vnd.npm.install-v1+json, application/json",
        "User-Agent": "verdaccio-nexus-importer/1.0",
    }

    auth = build_auth_header()
    if auth:
        headers["Authorization"] = auth

    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None

            metadata = json.loads(response.read().decode("utf-8", errors="replace"))
            versions = metadata.get("versions", {})

            if not isinstance(versions, dict):
                return None

            return package.version in versions

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False

        if exc.code in (401, 403):
            return None

        return None

    except Exception:
        return None


def exists_via_npm_view(
    package: PackageTarball,
    registry: str,
    timeout: int,
    npmrc: Optional[Path],
) -> bool:
    cmd = [
        "npm",
        "view",
        package.spec,
        "version",
        "--registry",
        registry,
        "--json",
    ]

    env = build_subprocess_env(npmrc)

    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )

    if proc.returncode == 0:
        return True

    combined = f"{proc.stdout}\n{proc.stderr}".lower()

    not_found_markers = [
        "e404",
        "404 not found",
        "not found",
    ]

    if any(marker in combined for marker in not_found_markers):
        return False

    raise RuntimeError(
        f"npm view failed for {package.spec}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


def package_exists(
    package: PackageTarball,
    registry: str,
    timeout: int,
    force_npm_view: bool,
    npmrc: Optional[Path],
) -> bool:
    if not force_npm_view:
        result = exists_via_registry_metadata(package, registry, timeout)
        if result is not None:
            return result

    return exists_via_npm_view(package, registry, timeout, npmrc)


def build_subprocess_env(npmrc: Optional[Path]) -> dict[str, str]:
    env = os.environ.copy()

    if npmrc:
        env["NPM_CONFIG_USERCONFIG"] = str(npmrc.resolve())

    return env


def npm_publish(
    package: PackageTarball,
    registry: str,
    timeout: int,
    ignore_scripts: bool,
    tag: Optional[str],
    npmrc: Optional[Path],
) -> None:
    cmd = [
        "npm",
        "publish",
        str(package.path),
        "--registry",
        registry,
    ]

    if ignore_scripts:
        cmd.append("--ignore-scripts")

    if tag:
        cmd.extend(["--tag", tag])

    env = build_subprocess_env(npmrc)

    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )

    if proc.returncode != 0:
        combined = f"{proc.stdout}\n{proc.stderr}".lower()

        already_exists_markers = [
            "cannot publish over previously published version",
            "previously published version",
            "forbidden - cannot publish over existing version",
            "e403",
        ]

        if any(marker in combined for marker in already_exists_markers):
            raise FileExistsError(proc.stderr.strip() or proc.stdout.strip())

        raise RuntimeError(
            f"npm publish failed for {package.spec}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


def import_one(
    package: PackageTarball,
    registry: str,
    dry_run: bool,
    check_timeout: int,
    publish_timeout: int,
    force_npm_view: bool,
    ignore_scripts: bool,
    tag: Optional[str],
    retries: int,
    npmrc: Optional[Path],
) -> ImportResult:
    for attempt in range(1, retries + 2):
        try:
            if package_exists(package, registry, check_timeout, force_npm_view, npmrc):
                return ImportResult(
                    status="skipped",
                    package=package.spec,
                    path=str(package.path),
                    message="already exists",
                )

            if dry_run:
                return ImportResult(
                    status="dry-run",
                    package=package.spec,
                    path=str(package.path),
                    message="would publish",
                )

            npm_publish(
                package=package,
                registry=registry,
                timeout=publish_timeout,
                ignore_scripts=ignore_scripts,
                tag=tag,
                npmrc=npmrc,
            )

            return ImportResult(
                status="published",
                package=package.spec,
                path=str(package.path),
                message="published successfully",
            )

        except FileExistsError as exc:
            return ImportResult(
                status="skipped",
                package=package.spec,
                path=str(package.path),
                message=f"already exists after publish race: {exc}",
            )

        except Exception as exc:
            if attempt <= retries:
                sleep_seconds = min(2 * attempt, 10)
                log(
                    f"Retrying {package.spec}; "
                    f"attempt {attempt}/{retries + 1} failed: {exc}"
                )
                time.sleep(sleep_seconds)
                continue

            return ImportResult(
                status="failed",
                package=package.spec,
                path=str(package.path),
                message=str(exc),
            )

    return ImportResult(
        status="failed",
        package=package.spec,
        path=str(package.path),
        message="unexpected retry loop exit",
    )


def writer_thread_func(
    output_path: Optional[Path],
    q: queue.Queue[Optional[ImportResult]],
) -> None:
    fh = None

    try:
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fh = output_path.open("w", encoding="utf-8")

        while True:
            item = q.get()

            if item is None:
                break

            if fh:
                fh.write(json.dumps(item.__dict__, sort_keys=True) + "\n")
                fh.flush()

    finally:
        if fh:
            fh.close()


def ensure_npm_available() -> None:
    if shutil.which("npm") is None:
        raise RuntimeError("npm was not found on PATH")


def normalize_registry(registry: str) -> str:
    registry = registry.strip()

    if not registry:
        raise ValueError("registry URL is empty")

    return registry.rstrip("/") + "/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Concurrent importer for Verdaccio npm tarballs into Nexus."
    )

    parser.add_argument(
        "--input",
        required=True,
        help="Verdaccio storage directory or collector .tgz/.tar.gz archive.",
    )

    parser.add_argument(
        "--registry",
        required=True,
        help=(
            "Nexus npm hosted registry URL, for example "
            "https://nexus.example.com/repository/npm-hosted/"
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent workers. Default: 8.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and report what would be published, but do not publish.",
    )

    parser.add_argument(
        "--force-npm-view",
        action="store_true",
        help="Use npm view for existence checks instead of direct registry metadata checks.",
    )

    parser.add_argument(
        "--check-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for existence checks. Default: 30.",
    )

    parser.add_argument(
        "--publish-timeout",
        type=int,
        default=300,
        help="Timeout in seconds for each npm publish. Default: 300.",
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retry count for transient failures. Default: 2.",
    )

    parser.add_argument(
        "--no-ignore-scripts",
        action="store_true",
        help="Do not pass --ignore-scripts to npm publish.",
    )

    parser.add_argument(
        "--tag",
        default=None,
        help="Optional npm dist-tag to apply during publish, for example latest.",
    )

    parser.add_argument(
        "--npmrc",
        default=None,
        help="Optional path to a specific .npmrc file to use for npm commands.",
    )

    parser.add_argument(
        "--jsonl-report",
        default="nexus-import-report.jsonl",
        help="Path to JSONL report file. Default: nexus-import-report.jsonl.",
    )

    parser.add_argument(
        "--include-collector-named-tgz",
        action="store_true",
        help=(
            "Do not skip .tgz files whose names look like collector/Verdaccio archives. "
            "Usually not needed."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        ensure_npm_available()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        input_path = Path(args.input).resolve()
        registry = normalize_registry(args.registry)
        npmrc = Path(args.npmrc).resolve() if args.npmrc else None

        if npmrc and not npmrc.is_file():
            raise FileNotFoundError(f".npmrc file not found: {npmrc}")

        scan_root, temp_dir = prepare_input(input_path)

    except Exception as exc:
        print(f"ERROR: failed preparing input: {exc}", file=sys.stderr)
        return 2

    result_queue: queue.Queue[Optional[ImportResult]] = queue.Queue()
    report_path = Path(args.jsonl_report).resolve() if args.jsonl_report else None

    writer = threading.Thread(
        target=writer_thread_func,
        args=(report_path, result_queue),
        daemon=True,
    )
    writer.start()

    counts: dict[str, int] = {
        "published": 0,
        "skipped": 0,
        "dry-run": 0,
        "failed": 0,
        "invalid": 0,
        "duplicate": 0,
    }

    try:
        tarballs = discover_tarballs(
            root=scan_root,
            include_collector_named_tgz=args.include_collector_named_tgz,
        )

        log(f"Input: {input_path}")
        log(f"Scan root: {scan_root}")
        log(f"Registry: {registry}")
        log(f"Workers: {args.workers}")
        log(f"Dry run: {args.dry_run}")
        log(f"Found {len(tarballs)} .tgz files")

        packages, preflight_results = load_packages(tarballs)

        log(f"Valid npm package tarballs: {len(packages)}")
        log(f"Preflight warnings/errors: {len(preflight_results)}")

        for result in preflight_results:
            counts[result.status] = counts.get(result.status, 0) + 1
            result_queue.put(result)

        max_workers = max(1, args.workers)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    import_one,
                    package,
                    registry,
                    args.dry_run,
                    args.check_timeout,
                    args.publish_timeout,
                    args.force_npm_view,
                    not args.no_ignore_scripts,
                    args.tag,
                    args.retries,
                    npmrc,
                ): package
                for package in packages
            }

            completed = 0
            total = len(future_map)

            for future in concurrent.futures.as_completed(future_map):
                completed += 1
                result = future.result()

                counts[result.status] = counts.get(result.status, 0) + 1
                result_queue.put(result)

                status = result.status.upper()

                if result.status in ("published", "failed", "dry-run"):
                    log(
                        f"[{completed}/{total}] {status:9} "
                        f"{result.package} - {result.message}"
                    )
                else:
                    log(f"[{completed}/{total}] {status:9} {result.package}")

    finally:
        result_queue.put(None)
        writer.join()

        if temp_dir is not None:
            log(f"Cleaning up temp directory: {temp_dir.name}")
            temp_dir.cleanup()

    log("")
    log("Summary:")
    for key in sorted(counts):
        log(f"  {key}: {counts[key]}")

    if report_path:
        log(f"Report: {report_path}")

    if counts.get("failed", 0) > 0 or counts.get("invalid", 0) > 0:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
