"""Development-input validation; never imported by the client."""

from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]

def require_private_file(path: Path) -> None:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Input must be an existing absolute file path")
    if path.resolve().is_relative_to(REPOSITORY):
        raise ValueError("Keep session material outside the repository")
    if path.stat().st_mode & 0o077:
        raise ValueError("Input file must be private (chmod 600)")
