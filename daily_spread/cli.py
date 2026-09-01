import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .config import ROOT, Settings


class CLIError(RuntimeError):
    pass


def resolve_binary() -> str:
    override = os.getenv("ALPACA_CLI_PATH", "").strip()
    if override and Path(override).exists():
        return str(Path(override).resolve())

    for candidate in ("alpaca.exe", "alpaca"):
        local = ROOT / "tools" / candidate
        if local.exists():
            return str(local.resolve())

    found = shutil.which("alpaca")
    if found:
        return found

    raise CLIError(
        "alpaca CLI not found. Place the binary at tools/alpaca(.exe), put it on PATH, "
        "or set ALPACA_CLI_PATH")


class AlpacaCLI:
    def __init__(self, settings: Settings, timeout: int = 60):
        self.binary = resolve_binary()
        self.timeout = timeout
        self.env = dict(os.environ)
        self.env["ALPACA_API_KEY"] = settings.alpaca_key
        self.env["ALPACA_SECRET_KEY"] = settings.alpaca_secret
        self.env.pop("ALPACA_LIVE_TRADE", None)

    def run(self, args: List[str]) -> Dict:
        command = [self.binary] + args + ["--quiet"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    env=self.env, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise CLIError(f"alpaca {' '.join(args)} timed out after {self.timeout}s")

        stdout = result.stdout.strip()

        if result.returncode != 0:
            detail = stdout or result.stderr.strip()
            raise CLIError(f"alpaca {' '.join(args[:3])} failed: {detail[:300]}")

        if not stdout:
            return {}

        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            raise CLIError(f"alpaca {' '.join(args[:3])} returned non JSON: {stdout[:200]}")

    def version(self) -> str:
        try:
            result = subprocess.run([self.binary, "--version"], capture_output=True,
                                    text=True, env=self.env, timeout=15)
            return result.stdout.strip()[:120]
        except Exception:
            return "unknown"

    def account(self) -> Dict:
        return self.run(["account", "get"])

    def clock(self) -> Dict:
        return self.run(["clock"])

    def positions(self) -> List[Dict]:
        payload = self.run(["position", "list"])
        return payload if isinstance(payload, list) else payload.get("positions", [])

    def orders(self, status: str = "open", limit: int = 50) -> List[Dict]:
        payload = self.run(["order", "list", "--status", status, "--limit", str(limit)])
        return payload if isinstance(payload, list) else payload.get("orders", [])

    def get_order(self, order_id: str) -> Dict:
        return self.run(["order", "get", order_id])

    def submit_mleg(self, legs: List[Dict], qty: int, limit_price: float,
                    dry_run: bool = False) -> Dict:
        if not 2 <= len(legs) <= 4:
            raise CLIError(f"multi leg orders take 2 to 4 legs, got {len(legs)}")

        args = [
            "order", "submit",
            "--order-class", "mleg",
            "--qty", str(qty),
            "--type", "limit",
            "--limit-price", f"{limit_price:.2f}",
            "--time-in-force", "day",
            "--legs", json.dumps(legs),
        ]
        if dry_run:
            args.append("--dry-run")
        return self.run(args)

    @staticmethod
    def leg(symbol: str, side: str, intent: str, ratio: int = 1) -> Dict:
        return {
            "symbol": symbol,
            "ratio_qty": str(ratio),
            "side": side,
            "position_intent": intent,
        }
