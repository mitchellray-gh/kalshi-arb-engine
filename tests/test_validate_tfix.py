import os
import sys
import subprocess


def test_validate_tfix_runs_successfully():
    """Run the existing validate_tfix.py script as a smoke test.

    This ensures the T-bracket validation remains green. The script should exit 0 on success.
    """
    env = os.environ.copy()
    env.update({"DRY_RUN": "true", "CI": "true"})
    proc = subprocess.run([sys.executable, "validate_tfix.py"], env=env)
    assert proc.returncode == 0, "validate_tfix.py failed"
