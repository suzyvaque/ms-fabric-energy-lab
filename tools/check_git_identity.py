"""Pre-commit hook: the commit author email must be a GitHub no-reply address."""
import subprocess
import sys

email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True).stdout.strip()
if not email.endswith("@users.noreply.github.com"):
    print("git user.email is not a GitHub no-reply address. Fix with:\n"
          "  git config --local user.email <id>+<user>@users.noreply.github.com")
    sys.exit(1)
