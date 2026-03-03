"""
run_engine.py — Autonomous launcher for the Kalshi arb engine.

Features:
  • Auto-restart on crash (max 10 retries, exponential backoff)
  • File + console logging with rotation
  • Windows Task Scheduler integration (--install-task / --uninstall-task)
  • Graceful shutdown on Ctrl+C or SIGTERM
  • Health heartbeat file for external monitoring

Usage:
  python run_engine.py                     # Run engine (default: dry-run)
  python run_engine.py --live              # Run engine with DRY_RUN=false
  python run_engine.py --install-task      # Install Windows scheduled task
  python run_engine.py --uninstall-task    # Remove Windows scheduled task
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Ensure local imports work
ENGINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE_DIR))
os.chdir(ENGINE_DIR)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_RESTARTS       = 10         # max consecutive crashes before giving up
BACKOFF_BASE_SEC   = 5          # restart delay: base * 2^failures
BACKOFF_MAX_SEC    = 300         # cap at 5 minutes between restarts
HEARTBEAT_FILE     = ENGINE_DIR / "results" / "heartbeat.txt"
PID_FILE           = ENGINE_DIR / "results" / "engine.pid"
TASK_NAME          = "KalshiArbEngine"


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_file: str = "kalshi_arb.log", level: str = "INFO") -> None:
    """Set up rotating file logger + console output."""
    log_dir = ENGINE_DIR / "results"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / log_file

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file: 5 MB × 5 backups = 25 MB max
    fh = RotatingFileHandler(str(log_path), maxBytes=5_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def write_heartbeat(status: str = "alive") -> None:
    """Write a heartbeat file for external health monitors."""
    try:
        HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
        HEARTBEAT_FILE.write_text(
            f"{datetime.now(timezone.utc).isoformat()} {status} pid={os.getpid()}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def write_pid() -> None:
    """Write PID file so external tools can find & stop the engine."""
    try:
        PID_FILE.parent.mkdir(exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Signal handling ───────────────────────────────────────────────────────────

_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logging.getLogger(__name__).info("Shutdown signal received (sig=%s)", signum)
    raise KeyboardInterrupt


# ── Windows Task Scheduler ────────────────────────────────────────────────────

def install_scheduled_task() -> None:
    """Register a Windows Task Scheduler task to auto-start the engine on login."""
    python = sys.executable
    script = str(ENGINE_DIR / "run_engine.py")
    cmd = f'"{python}" "{script}" --live'

    # schtasks XML for a logon-triggered task
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Kalshi Arb Engine — autonomous trading bot</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python}</Command>
      <Arguments>"{script}" --live</Arguments>
      <WorkingDirectory>{ENGINE_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    xml_path = ENGINE_DIR / "task_config.xml"
    xml_path.write_text(xml, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
        capture_output=True, text=True,
    )
    xml_path.unlink(missing_ok=True)

    if result.returncode == 0:
        print(f"  ✓ Task '{TASK_NAME}' installed. Engine will auto-start on login.")
        print(f"    To start now: schtasks /Run /TN {TASK_NAME}")
        print(f"    To check:     schtasks /Query /TN {TASK_NAME}")
    else:
        print(f"  ✗ Failed to install task: {result.stderr.strip()}")
        print("    Try running as Administrator.")
        sys.exit(1)


def uninstall_scheduled_task() -> None:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  ✓ Task '{TASK_NAME}' removed.")
    else:
        print(f"  ✗ Failed: {result.stderr.strip()}")


# ── Engine runner with auto-restart ───────────────────────────────────────────

def run_engine(live: bool = False) -> None:
    """Run the trading engine with crash recovery."""
    logger = logging.getLogger("launcher")

    # Load config
    from dotenv import load_dotenv
    load_dotenv(ENGINE_DIR / ".env")

    if live:
        os.environ["DRY_RUN"] = "false"
        logger.warning("LIVE MODE — DRY_RUN=false — real orders will be placed!")
    else:
        os.environ.setdefault("DRY_RUN", "true")
        logger.info("DRY-RUN mode — no real orders will be placed")

    from engine.config import load_config
    cfg = load_config()
    cfg.validate()

    setup_logging(cfg.log_file, cfg.log_level)
    write_pid()

    logger.info("=" * 60)
    logger.info("ENGINE STARTING  env=%s  dry_run=%s  pid=%d", cfg.env, cfg.dry_run, os.getpid())
    logger.info("=" * 60)

    failures = 0
    while failures < MAX_RESTARTS and not _shutdown:
        try:
            write_heartbeat("starting")

            from engine.trading_engine import TradingEngine
            engine = TradingEngine(cfg)

            # Heartbeat thread
            import threading
            def heartbeat_loop():
                while not _shutdown:
                    write_heartbeat("alive")
                    time.sleep(60)
            hb = threading.Thread(target=heartbeat_loop, daemon=True)
            hb.start()

            engine.run()
            break  # clean exit (shouldn't reach here — run() is infinite)

        except KeyboardInterrupt:
            logger.info("Shutdown requested — stopping engine")
            write_heartbeat("stopped")
            break

        except Exception as exc:
            failures += 1
            delay = min(BACKOFF_BASE_SEC * (2 ** (failures - 1)), BACKOFF_MAX_SEC)
            logger.error(
                "Engine crashed (attempt %d/%d): %s\n%s",
                failures, MAX_RESTARTS, exc, traceback.format_exc(),
            )
            write_heartbeat(f"crashed (attempt {failures})")

            if failures >= MAX_RESTARTS:
                logger.critical("Max restarts reached (%d). Giving up.", MAX_RESTARTS)
                write_heartbeat("dead")
                break

            logger.info("Restarting in %ds ...", delay)
            time.sleep(delay)

            # Re-import to get fresh state
            import importlib
            import engine.trading_engine
            importlib.reload(engine.trading_engine)

    remove_pid()
    logger.info("Engine shut down.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Kalshi Arb Engine — autonomous launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
QUICK START:
  1. Copy .env.example → .env
  2. Add your API key and private key path
  3. python run_engine.py              (dry-run test)
  4. python run_engine.py --live       (real trading)

DEPLOY AS SERVICE:
  5. python run_engine.py --install-task   (auto-start on login)

MONITOR:
  - Logs:      results/kalshi_arb.log
  - Heartbeat: results/heartbeat.txt
  - PID:       results/engine.pid
  - Positions: results/positions.json
  - P&L:       python main.py --report
""",
    )
    p.add_argument("--live", action="store_true",
                   help="Run in LIVE mode (DRY_RUN=false). Real money!")
    p.add_argument("--install-task", action="store_true",
                   help="Install Windows Task Scheduler task to auto-start on login")
    p.add_argument("--uninstall-task", action="store_true",
                   help="Remove the Windows Task Scheduler task")
    args = p.parse_args()

    if args.install_task:
        install_scheduled_task()
        return
    if args.uninstall_task:
        uninstall_scheduled_task()
        return

    # Set up signal handlers
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows
        signal.signal(signal.SIGBREAK, _handle_signal)

    # Initial logging (before config load)
    setup_logging()
    run_engine(live=args.live)


if __name__ == "__main__":
    main()
