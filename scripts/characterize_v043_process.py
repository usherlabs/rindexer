#!/usr/bin/env python3
"""Characterize untouched v0.43 process lifecycle behavior.

This probe intentionally runs the canonical CLI without ``--watch`` against a
local Anvil. It records the embedded health listener and the engine-owned Unix
signal/exit behavior that FIET must not inherit after the embedded split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CANONICAL_COMMIT = "4f441289b83855c357239d2729fb725a56c3060b"
CANONICAL_TREE = "80a2698f6be13949d84d920b01c02125af598d09"
DEFAULT_HEALTH_PORT = 8080
SIGNALS = ("SIGTERM", "SIGINT", "SIGQUIT")


def build_manifest(rpc_url: str) -> str:
    """Return a live native-transfer manifest that relies on health defaults."""
    return f"""name: fiet-v043-process-probe
project_type: no-code
config: {{}}
networks:
  - name: anvil
    chain_id: 31337
    rpc: {rpc_url}
storage:
  csv:
    enabled: true
native_transfers:
  networks:
    - network: anvil
contracts: []
"""


def classify_signal_run(
    *,
    signal_name: str,
    exit_code: int,
    output: str,
    health_payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    handler_seen = f"Received {signal_name}" in output
    graceful_seen = f"Graceful shutdown completed for {signal_name}" in output
    health_seen = "Starting health server on port 8080" in output
    health_healthy = health_payload.get("status") == "healthy"
    indexing_running = health_payload.get("indexing", {}).get("is_running") is True
    watch_disabled = "Hot-reload:" not in output

    errors: list[str] = []
    if exit_code != 0:
        errors.append(f"{signal_name} process exit was {exit_code}, expected 0")
    if not handler_seen:
        errors.append(f"{signal_name} handler log was not observed")
    if not graceful_seen:
        errors.append(f"{signal_name} graceful completion log was not observed")
    if not health_seen:
        errors.append("default health listener log was not observed")
    if not health_healthy or not indexing_running:
        errors.append("health endpoint did not report a running healthy indexer")
    if not watch_disabled:
        errors.append("hot-reload activated even though --watch was omitted")

    return (
        {
            "signal": signal_name,
            "exit_code": exit_code,
            "default_health_listener_observed": health_seen,
            "health_payload": health_payload,
            "engine_signal_handler_observed": handler_seen,
            "graceful_completion_observed": graceful_seen,
            "engine_forced_zero_exit_observed": exit_code == 0 and handler_seen and graceful_seen,
            "watch_disabled_observed": watch_disabled,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        },
        errors,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), *args], text=True
    ).strip()


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _post_rpc(port: int, method: str) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=1) as response:
        return json.load(response)


def _wait_for_anvil(process: subprocess.Popen[str], port: int, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Anvil exited before readiness with {process.returncode}")
        try:
            response = _post_rpc(port, "eth_chainId")
            if response.get("result") == "0x7a69":
                return
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise TimeoutError("Anvil did not become ready")


def _get_health(process: subprocess.Popen[str], timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError(
                f"rindexer exited before health readiness with {process.returncode}: {output}"
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{DEFAULT_HEALTH_PORT}/health", timeout=1
            ) as response:
                payload = json.load(response)
            if payload.get("status") == "healthy":
                return payload
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise TimeoutError("rindexer health endpoint did not become ready")


def _wait_for_port_release(port: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_listening(port):
            return True
        time.sleep(0.05)
    return False


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_signal(binary: Path, rpc_url: str, signal_name: str) -> tuple[dict[str, Any], list[str]]:
    with tempfile.TemporaryDirectory(prefix="fiet-rindexer-v043-process-") as directory:
        project_path = Path(directory)
        (project_path / "rindexer.yaml").write_text(build_manifest(rpc_url))
        process = subprocess.Popen(
            [str(binary), "start", "indexer"],
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "RUST_LOG": "info"},
        )
        try:
            health_payload = _get_health(process)
            os.kill(process.pid, getattr(signal, signal_name))
            try:
                output, _ = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=5)
                observation, errors = classify_signal_run(
                    signal_name=signal_name,
                    exit_code=process.returncode,
                    output=output,
                    health_payload=health_payload,
                )
                errors.append(f"{signal_name} shutdown exceeded 20 seconds")
                return observation, errors

            observation, errors = classify_signal_run(
                signal_name=signal_name,
                exit_code=process.returncode,
                output=output,
                health_payload=health_payload,
            )
            observation["health_port_released_after_exit"] = _wait_for_port_release(
                DEFAULT_HEALTH_PORT
            )
            if not observation["health_port_released_after_exit"]:
                errors.append("default health port remained bound after process exit")
            return observation, errors
        finally:
            _terminate(process)


def characterize(binary: Path, anvil: Path, repo_root: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if _port_listening(DEFAULT_HEALTH_PORT):
        raise RuntimeError(
            f"default health port {DEFAULT_HEALTH_PORT} is already occupied; refusing misattribution"
        )

    anvil_port = _allocate_port()
    anvil_process = subprocess.Popen(
        [str(anvil), "--silent", "--port", str(anvil_port), "--chain-id", "31337"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_for_anvil(anvil_process, anvil_port)
        rpc_url = f"http://127.0.0.1:{anvil_port}"
        runs = []
        for signal_name in SIGNALS:
            observation, run_errors = _run_signal(binary, rpc_url, signal_name)
            runs.append(observation)
            errors.extend(run_errors)
    finally:
        _terminate(anvil_process)

    runtime_paths = ("Cargo.toml", "Cargo.lock", "core/Cargo.toml", "core/src")
    runtime_diff = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--quiet", CANONICAL_COMMIT, "--", *runtime_paths],
        check=False,
    )
    if runtime_diff.returncode != 0:
        errors.append("runtime paths are not byte-identical to canonical v0.43.0")

    try:
        binary_identity = str(binary.relative_to(repo_root))
    except ValueError:
        binary_identity = str(binary)

    receipt = {
        "schema_version": "fiet-rindexer-v043-process-characterization/v1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_under_test": {
            "canonical_commit": CANONICAL_COMMIT,
            "canonical_root_tree": CANONICAL_TREE,
            "observed_head": _git(repo_root, "rev-parse", "HEAD"),
            "observed_head_tree": _git(repo_root, "rev-parse", "HEAD^{tree}"),
            "runtime_paths_byte_identical_to_canonical": runtime_diff.returncode == 0,
        },
        "binary": {
            "path": binary_identity,
            "sha256": _sha256(binary),
        },
        "environment": {
            "os": platform.platform(),
            "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
            "cargo": subprocess.check_output(["cargo", "--version"], text=True).strip(),
            "anvil_version": subprocess.check_output([str(anvil), "--version"], text=True).splitlines()[0],
            "anvil_chain_id": 31337,
            "health_port": DEFAULT_HEALTH_PORT,
        },
        "invocation": {
            "arguments": ["start", "indexer"],
            "watch_argument_present": False,
        },
        "runs": runs,
        "source_bound_observations": {
            "start_details_requires_watch": True,
            "health_listener_spawned_when_indexing_details_is_some": True,
            "unix_handlers": list(SIGNALS),
            "shutdown_handler_calls_process_exit_zero": True,
            "drain_failure_cannot_control_process_exit": True,
        },
        "verdict": "pass" if not errors else "fail",
        "errors": errors,
    }
    return receipt, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--anvil", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    binary = args.binary.resolve()
    anvil = args.anvil.resolve()
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}")
    if not anvil.is_file():
        parser.error(f"Anvil does not exist: {anvil}")

    receipt, errors = characterize(binary, anvil, args.repo_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if errors:
        for error in errors:
            print(error)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
