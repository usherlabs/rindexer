#!/usr/bin/env python3
"""Qualify the FIET caller-owned embedded rindexer lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_manifest(rpc_url: str, health_port: int) -> str:
    return f"""name: fiet-embedded-lifecycle-probe
project_type: no-code
config: {{}}
global:
  health_port: {health_port}
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


def classify_run(
    *, signal_name: str, exit_code: int, output: str, health_bound: bool, force_timeout: bool
) -> tuple[dict[str, Any], list[str]]:
    expected_exit = 42 if force_timeout else 0
    expected_drain = "timeout" if force_timeout else "complete"
    errors: list[str] = []
    if exit_code != expected_exit:
        errors.append(f"{signal_name} exit {exit_code}, expected {expected_exit}")
    if f"FIET_EMBEDDED_CALLER_SIGNAL={signal_name}" not in output:
        errors.append(f"caller did not observe {signal_name}")
    if f"FIET_EMBEDDED_DRAIN={expected_drain}" not in output:
        errors.append(f"expected drain outcome {expected_drain} was absent")
    if not force_timeout and "FIET_EMBEDDED_ENGINE=complete" not in output:
        errors.append("embedded engine completion was absent")
    if health_bound:
        errors.append("embedded rindexer bound its configured health port")
    if "Received SIGTERM signal gracefully shutting down" in output or "Received SIGINT" in output:
        errors.append("engine-owned process signal handler was observed")

    return (
        {
            "signal": signal_name,
            "force_timeout": force_timeout,
            "exit_code": exit_code,
            "expected_exit_code": expected_exit,
            "caller_signal_observed": f"FIET_EMBEDDED_CALLER_SIGNAL={signal_name}" in output,
            "drain_outcome": expected_drain,
            "engine_completion_observed": "FIET_EMBEDDED_ENGINE=complete" in output,
            "engine_signal_handler_observed": "signal gracefully shutting down" in output,
            "health_listener_observed": health_bound,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "output_tail": output[-2000:],
        },
        errors,
    )


def _allocate_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _port_bound(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_anvil(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 20
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Anvil exited early with {process.returncode}")
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}",
                data=body,
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if json.load(response).get("result") == "0x7a69":
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.1)
    raise TimeoutError("Anvil did not become ready")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.kill()
    process.wait(timeout=5)


def _run_probe(
    binary: Path,
    manifest: Path,
    health_port: int,
    signal_name: str,
    force_timeout: bool,
) -> tuple[dict[str, Any], list[str]]:
    command = [str(binary), str(manifest)]
    if force_timeout:
        command.append("--force-timeout")
    process = subprocess.Popen(
        command,
        cwd=manifest.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "RUST_LOG": "info"},
    )
    health_bound = False
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output, _ = process.communicate()
                return classify_run(
                    signal_name=signal_name,
                    exit_code=process.returncode,
                    output=output,
                    health_bound=health_bound,
                    force_timeout=force_timeout,
                )
            health_bound = health_bound or _port_bound(health_port)
            time.sleep(0.1)

        os.kill(process.pid, getattr(signal, signal_name))
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _stop(process)
            output, _ = process.communicate()
            observation, errors = classify_run(
                signal_name=signal_name,
                exit_code=process.returncode,
                output=output,
                health_bound=health_bound,
                force_timeout=force_timeout,
            )
            errors.append("probe exceeded its ten-second finalization deadline")
            return observation, errors
        return classify_run(
            signal_name=signal_name,
            exit_code=process.returncode,
            output=output,
            health_bound=health_bound,
            force_timeout=force_timeout,
        )
    finally:
        _stop(process)


def qualify(binary: Path, anvil: Path, repo_root: Path) -> tuple[dict[str, Any], list[str]]:
    anvil_port = _allocate_port()
    health_port = _allocate_port()
    anvil_process = subprocess.Popen(
        [str(anvil), "--silent", "--port", str(anvil_port), "--chain-id", "31337"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_for_anvil(anvil_process, anvil_port)
        with tempfile.TemporaryDirectory(prefix="fiet-embedded-lifecycle-") as directory:
            manifest = Path(directory) / "rindexer.yaml"
            manifest.write_text(build_manifest(f"http://127.0.0.1:{anvil_port}", health_port))
            cases = [("SIGTERM", False), ("SIGINT", False), ("SIGTERM", True)]
            runs: list[dict[str, Any]] = []
            errors: list[str] = []
            for signal_name, force_timeout in cases:
                observation, case_errors = _run_probe(
                    binary, manifest, health_port, signal_name, force_timeout
                )
                runs.append(observation)
                errors.extend(case_errors)
    finally:
        _stop(anvil_process)

    receipt = {
        "schema_version": "fiet-rindexer-embedded-lifecycle-qualification/v1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_under_test": {
            "commit": subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
            ).strip(),
            "tree": subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"], text=True
            ).strip(),
        },
        "binary": {"path": str(binary), "sha256": _sha256(binary)},
        "configured_health_port": health_port,
        "runs": runs,
        "errors": errors,
        "verdict": "pass" if not errors else "fail",
    }
    return receipt, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--anvil", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    receipt, errors = qualify(
        args.binary.resolve(), args.anvil.resolve(), args.repo_root.resolve()
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    for error in errors:
        print(error)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
