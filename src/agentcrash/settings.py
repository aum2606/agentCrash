"""Local configuration from environment variables, falling back to the git-ignored ``.env``.

The database password is only ever placed inside a ``sqlalchemy.URL`` (whose repr hides it);
it is never formatted into strings, logs or error messages.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import URL
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class Settings:
    root: Path  # repository root: fixtures/, graders/, migrations/, .local/
    database_url: URL
    token_path: Path
    queue_cap: int = 100  # max queued runs (CONTRACTS section 3)
    create_rate_per_minute: int = 10

    @property
    def artifact_dir(self) -> Path:
        """Content-addressed export bundles; under the git-ignored ``.local/``."""
        return self.root / ".local" / "artifacts"


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (no quoting, no expansion); unknown shapes are skipped."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def load_settings(root: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    base = (root or Path(os.environ.get("AGENTCRASH_ROOT", "."))).resolve()
    merged = {**read_dotenv(base / ".env"), **(os.environ if env is None else env)}
    if "AGENTCRASH_DATABASE_URL" in merged:
        url = make_url(merged["AGENTCRASH_DATABASE_URL"])
        if url.drivername != "postgresql+psycopg":
            raise ValueError("AGENTCRASH_DATABASE_URL must use the postgresql+psycopg driver")
    else:
        password = merged.get("POSTGRES_PASSWORD", "")
        if not password:
            raise ValueError("POSTGRES_PASSWORD is not set (copy .env.example to .env)")
        url = URL.create(
            "postgresql+psycopg",
            username=merged.get("POSTGRES_USER", "agentcrash"),
            password=password,
            host="127.0.0.1",  # loopback only (ADR-009)
            port=int(merged.get("POSTGRES_HOST_PORT", "15432")),
            database=merged.get("POSTGRES_DB", "agentcrash"),
        )
    return Settings(
        root=base,
        database_url=url,
        token_path=Path(merged.get("AGENTCRASH_TOKEN_FILE", str(base / ".local" / "token"))),
    )
