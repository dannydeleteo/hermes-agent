"""Locate the official GitHub CLI, including the owner-installed portable copy."""
from hermes_platform.resolver import locate_command


def github_cli():
    from hermes_constants import get_default_hermes_root
    return locate_command("gh", known_dirs=(str(get_default_hermes_root() / "tools" / "github-cli"),))
