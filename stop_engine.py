"""Stop a running engine by reading its PID file."""
import os, signal, sys
from pathlib import Path

pid_file = Path(__file__).resolve().parent / "results" / "engine.pid"

if not pid_file.exists():
    print("  No engine.pid found — engine may not be running.")
    sys.exit(0)

pid = int(pid_file.read_text().strip())
print(f"  Sending SIGTERM to PID {pid} ...")

try:
    os.kill(pid, signal.SIGTERM)
    print(f"  ✓ Signal sent. Engine should shut down gracefully.")
except ProcessLookupError:
    print(f"  PID {pid} not found — engine already stopped.")
    pid_file.unlink(missing_ok=True)
except PermissionError:
    print(f"  ✗ Permission denied. Try running as Administrator.")
