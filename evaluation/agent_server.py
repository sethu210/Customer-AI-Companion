"""
A Neuro SAN agent server for one evaluation run (journey, Guardian and assistant evals): ns run --server-only with
its own case and action stores, live mode; always stopped (with its children) on exit.
"""

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from evaluation.expectations import ROOT

NS_EXE = ROOT / ".venv" / "Scripts" / "ns.exe"
SERVER_PORT = 8080


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class AgentServer:
    """ns run --server-only with its own stores, live mode; always stopped (with its children) on exit."""

    def __init__(self, store_dir: Path, scripted: bool) -> None:
        self.store_dir = store_dir
        self.env = {**os.environ, "ACTION_MODE": "live", "ACTION_STORE_DIR": str(store_dir / "actions"),
                    "CASE_STORE_PATH": str(store_dir / "cases.db"), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        if not port_in_use(6006):
            self.env["PHOENIX_ENABLED"] = "false"
        if scripted:
            self.env.update(NS_LLM_CLASS="evaluation.scripted_llm.ScriptedSupportLlm", NS_LLM_MODEL="scripted",
                            CUSTOMER_DATA_BACKEND="mock")
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "AgentServer":
        self.store_dir.mkdir(parents=True, exist_ok=True)
        log = (self.store_dir / "server.log").open("a" if self.proc else "w", encoding="utf-8")  # a restart appends
        self.proc = subprocess.Popen([str(NS_EXE), "run", "--server-only", "--server-http-port", str(SERVER_PORT)],
                                     env=self.env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        deadline = time.monotonic() + 180
        while not port_in_use(SERVER_PORT):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"agent server did not start; see {self.store_dir / 'server.log'}")
            time.sleep(2)
        time.sleep(5)  # let the manifest load
        for k in ("ACTION_MODE", "ACTION_STORE_DIR", "CASE_STORE_PATH"):
            os.environ[k] = self.env[k]  # the conversation reads the same stores
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)

    def restart(self) -> None:
        """Stop and start again with the same stores (the Guardian evaluation's worker restart)."""
        self.__exit__()
        deadline = time.monotonic() + 30
        while port_in_use(SERVER_PORT) and time.monotonic() < deadline:
            time.sleep(1)
        self.__enter__()
