"""Bootstrap and control a local PostgreSQL instance without Docker.

The repository has to be reviewable on machines that have no Docker daemon, no
WSL and no system-wide PostgreSQL.  The ``pgserver`` project ships a complete
PostgreSQL build inside an ordinary wheel, so this script downloads that wheel
from the Aliyun PyPI mirror (reachable from mainland China, unlike
``ftp.postgresql.org``), extracts the binaries and drives a **real** cluster on
a private port.

Commands::

    uv run python scripts/local_postgres.py up       # initdb + start + seed
    uv run python scripts/local_postgres.py status   # is the server accepting?
    uv run python scripts/local_postgres.py psql     # interactive psql shell
    uv run python scripts/local_postgres.py stop     # graceful shutdown
    uv run python scripts/local_postgres.py reset    # stop + delete cluster

After ``up`` the script prints the ``DATABASES`` value to export, and you can
run the live-service tests with::

    PG_MCP_RUN_INTEGRATION=1 uv run pytest tests/integration
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: ``pgserver`` bundles a full PostgreSQL 16.2 build; we only need its binaries.
WHEEL_URL = (
    "https://mirrors.aliyun.com/pypi/packages/85/80/f6304274c1740c283bc7317ababceb3"
    "c23c8275ce4995f7379e17b49bc6d/pgserver-0.1.4-cp312-cp312-win_amd64.whl"
)

DEFAULT_PORT = 55432
SUPERUSER = "postgres"
PREFIX = "pgserver/pginstall/"
READONLY_ROLE = "readonly_user"


def _repo_root() -> Path:
    """Return the repository root (parent of ``scripts/``).

    Returns:
        Path: Absolute path to the project root.
    """
    return Path(__file__).resolve().parents[1]


def _pg_home(args: argparse.Namespace) -> Path:
    """Resolve the PostgreSQL home directory.

    Args:
        args: Parsed command line arguments (``--pg-home`` may be absent).

    Returns:
        Path: Directory that holds ``bin/`` and ``data/``.
    """
    fallback = Path.home() / "AppData" / "Local" / "pg-local"
    raw = vars(args).get("pg_home") or os.environ.get("PG_LOCAL_HOME")
    return Path(raw or fallback)


def _port(args: argparse.Namespace) -> int:
    """Resolve the TCP port to use.

    Args:
        args: Parsed command line arguments (``--port`` may be absent).

    Returns:
        int: Port number.
    """
    raw = vars(args).get("port")
    return int(raw) if raw else DEFAULT_PORT


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def run(
    executable: Path,
    args: Sequence[str],
    *,
    log: Path | None = None,
    check: bool = True,
) -> int:
    """Run a PostgreSQL helper executable.

    Args:
        executable: Absolute path of the executable.
        args: Command line arguments.
        log: When given, stdout/stderr are appended to this file instead of the
            console (keeps the console readable and gives ``initdb`` real file
            handles for its internal ``popen`` calls).
        check: Raise :class:`SystemExit` when the exit code is non-zero.

    Returns:
        int: Process exit code.
    """
    command = [str(executable), *args]
    printable = " ".join(command)
    if log is None:
        print(f"$ {printable}")
        completed = subprocess.run(command, check=False)  # noqa: S603
    else:
        print(f"$ {printable} > {log.name}")
        with open(log, "a", encoding="utf-8", errors="replace") as handle:
            completed = subprocess.run(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
    if check and completed.returncode != 0:
        raise SystemExit(f"command failed ({completed.returncode}): {printable}")
    return completed.returncode


def ensure_binaries(home: Path) -> Path:
    """Download and extract the bundled PostgreSQL build when it is missing.

    Args:
        home: Directory that will host ``bin/``, ``lib/`` and ``share/``.

    Returns:
        Path: Directory that contains ``initdb.exe``/``postgres.exe``/``psql.exe``.
    """
    bin_dir = home / "bin"
    if (bin_dir / "initdb.exe").exists():
        return bin_dir

    if sys.platform != "win32":
        raise SystemExit(
            "the bundled build is Windows-only; install PostgreSQL yourself and "
            "point PG_LOCAL_HOME at it"
        )

    home.mkdir(parents=True, exist_ok=True)
    wheel = home / "_cache" / "pgserver.whl"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {WHEEL_URL}")
    with urllib.request.urlopen(WHEEL_URL, timeout=600) as response:  # noqa: S310
        wheel.write_bytes(response.read())
    print(f"downloaded {wheel.stat().st_size / 1e6:.1f} MB -> extracting")

    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if not info.filename.startswith(PREFIX):
                continue
            relative = info.filename[len(PREFIX) :]
            if not relative:
                continue
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
    print(f"extracted PostgreSQL build -> {bin_dir}")
    return bin_dir


def wait_until_ready(bin_dir: Path, port: int, attempts: int = 40) -> bool:
    """Poll ``pg_isready`` until the server accepts connections.

    Args:
        bin_dir: Directory containing ``pg_isready.exe``.
        port: TCP port of the cluster.
        attempts: Number of polls before giving up.

    Returns:
        bool: ``True`` when the server is accepting connections.
    """
    for _ in range(attempts):
        code = run(bin_dir / "pg_isready.exe", ["-h", "127.0.0.1", "-p", str(port)], check=False)
        if code == 0:
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #

SECOND_DATABASE = "analytics"

SECOND_DATABASE_SQL = """
DROP TABLE IF EXISTS events CASCADE;
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    event_name VARCHAR(64) NOT NULL,
    revenue NUMERIC(12, 2) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO events (user_id, event_name, revenue) VALUES
    (1, 'signup', 0.00),
    (2, 'purchase', 199.00),
    (3, 'purchase', 89.50),
    (4, 'refund', -89.50),
    (5, 'purchase', 420.00);
COMMENT ON TABLE events IS 'Product analytics events';
"""

# Literal SQL (no interpolation) so the read-only role is created idempotently.
# Keep the role name in sync with ``READONLY_ROLE`` above.
READONLY_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'readonly_user') THEN
        CREATE ROLE readonly_user LOGIN;
    END IF;
END
$$;
GRANT CONNECT ON DATABASE blog_small TO readonly_user;
GRANT USAGE ON SCHEMA public TO readonly_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly_user;
"""


def seed(bin_dir: Path, home: Path, port: int) -> None:
    """Create the demo databases, load the fixture and add the read-only role.

    Args:
        bin_dir: Directory containing ``psql.exe``.
        home: PostgreSQL home (used for log files).
        port: TCP port of the cluster.
    """
    fixture = _repo_root() / "fixtures" / "01_small_db.sql"
    psql = bin_dir / "psql.exe"
    base = ["-h", "127.0.0.1", "-p", str(port), "-U", SUPERUSER, "-v", "ON_ERROR_STOP=1"]

    if fixture.exists():
        print(f"loading fixture {fixture.name}")
        run(psql, [*base, "-d", "postgres", "-f", str(fixture)], log=home / "seed.log", check=False)
    else:
        print(f"fixture not found, skipping: {fixture}")

    print(f"creating second database '{SECOND_DATABASE}' (multi-database routing)")
    run(
        psql,
        [*base, "-d", "postgres", "-c", f"CREATE DATABASE {SECOND_DATABASE}"],
        log=home / "seed.log",
        check=False,
    )
    run(
        psql,
        [*base, "-d", SECOND_DATABASE, "-c", SECOND_DATABASE_SQL],
        log=home / "seed.log",
        check=False,
    )

    print(f"creating read-only role '{READONLY_ROLE}' (verifies SET ROLE hardening)")
    run(psql, [*base, "-d", "blog_small", "-c", READONLY_SQL], log=home / "seed.log", check=False)


def print_connection_info(port: int) -> None:
    """Print the environment variables needed to point pg-mcp at the cluster.

    Args:
        port: TCP port of the cluster.
    """
    databases = (
        f'[{{"name":"blog_small","host":"127.0.0.1","port":{port},"user":"{SUPERUSER}",'
        f'"password":"","ssl_mode":"disable","min_pool_size":1,"max_pool_size":5}},'
        f'{{"name":"analytics","host":"127.0.0.1","port":{port},"user":"{SUPERUSER}",'
        f'"password":"","ssl_mode":"disable","min_pool_size":1,"max_pool_size":5}}]'
    )
    print()
    print("=" * 78)
    print("PostgreSQL is ready - export these before running the live tests:")
    print("=" * 78)
    if sys.platform == "win32":
        print(f"$env:DATABASES = '{databases}'")
        print('$env:PG_MCP_RUN_INTEGRATION = "1"')
    else:
        print(f"export DATABASES='{databases}'")
        print("export PG_MCP_RUN_INTEGRATION=1")
    print()
    print("uv run python scripts/local_postgres.py psql   # inspect manually")
    print("uv run pytest tests/integration -m integration # live-service tests")
    print("=" * 78)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_up(args: argparse.Namespace) -> int:
    """Initialise, start and seed the local cluster.

    Args:
        args: Parsed command line arguments.

    Returns:
        int: Process exit code.
    """
    home = _pg_home(args)
    bin_dir = ensure_binaries(home)
    port = _port(args)
    data = home / "data"
    sock = home / "sock"
    sock.mkdir(parents=True, exist_ok=True)

    if not (data / "PG_VERSION").exists():
        data.mkdir(parents=True, exist_ok=True)
        print(f"initialising cluster in {data}")
        run(
            bin_dir / "initdb.exe",
            [
                "-D",
                str(data),
                "-U",
                SUPERUSER,
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C",
            ],
            log=home / "initdb.log",
        )
    else:
        print(f"reusing existing cluster in {data}")

    if wait_until_ready(bin_dir, port, attempts=1):
        print(f"server already listening on port {port}")
    else:
        print(f"starting server on port {port}")
        run(
            bin_dir / "pg_ctl.exe",
            [
                "-D",
                str(data),
                "-l",
                str(home / "server.log"),
                "-o",
                f"-p {port} -k {sock} -c listen_addresses=127.0.0.1",
                "-w",
                "start",
            ],
            log=home / "pg_ctl.log",
            check=False,
        )

    if not wait_until_ready(bin_dir, port):
        print("server did not become ready; see logs:")
        print(f"  {home / 'pg_ctl.log'}")
        print(f"  {home / 'server.log'}")
        return 1

    run(bin_dir / "postgres.exe", ["--version"], check=False)
    seed(bin_dir, home, port)
    print_connection_info(port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Report whether the local cluster is accepting connections.

    Args:
        args: Parsed command line arguments.

    Returns:
        int: ``0`` when ready, ``1`` otherwise.
    """
    home = _pg_home(args)
    port = _port(args)
    bin_dir = home / "bin"
    if not (bin_dir / "pg_isready.exe").exists():
        print("PostgreSQL binaries are not installed yet - run 'up' first")
        return 1
    ready = wait_until_ready(bin_dir, port, attempts=1)
    print(f"port {port}: {'accepting connections' if ready else 'not running'}")
    return 0 if ready else 1


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the local cluster.

    Args:
        args: Parsed command line arguments.

    Returns:
        int: Process exit code.
    """
    home = _pg_home(args)
    bin_dir = home / "bin"
    data = home / "data"
    if not data.exists():
        print("no cluster to stop")
        return 0
    return run(bin_dir / "pg_ctl.exe", ["-D", str(data), "-m", "fast", "stop"], check=False)


def cmd_reset(args: argparse.Namespace) -> int:
    """Stop the cluster and delete its data directory.

    Args:
        args: Parsed command line arguments.

    Returns:
        int: Process exit code.
    """
    cmd_stop(args)
    home = _pg_home(args)
    data = home / "data"
    if data.exists():
        print(f"deleting {data}")
        shutil.rmtree(data)
    return 0


def cmd_psql(args: argparse.Namespace) -> int:
    """Open an interactive ``psql`` shell against the local cluster.

    Args:
        args: Parsed command line arguments.

    Returns:
        int: Process exit code.
    """
    home = _pg_home(args)
    bin_dir = home / "bin"
    database = vars(args).get("database") or "blog_small"
    return run(
        bin_dir / "psql.exe",
        ["-h", "127.0.0.1", "-p", str(_port(args)), "-U", SUPERUSER, "-d", database],
        check=False,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        argparse.ArgumentParser: Configured parser.
    """
    # Shared options accept ``pg-mcp up --port X`` and ``pg-mcp --port X up``.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--pg-home", default=argparse.SUPPRESS, help="directory holding bin/ and data/"
    )
    common.add_argument("--port", type=int, default=argparse.SUPPRESS, help="TCP port to listen on")
    common.add_argument(
        "--database", default=argparse.SUPPRESS, help="database for the 'psql' command"
    )

    parser = argparse.ArgumentParser(
        parents=[common], description=__doc__.splitlines()[0] if __doc__ else ""
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    handlers = {
        "up": cmd_up,
        "status": cmd_status,
        "stop": cmd_stop,
        "reset": cmd_reset,
        "psql": cmd_psql,
    }
    for name, handler in handlers.items():
        summary = (handler.__doc__ or name).splitlines()[0]
        subparsers.add_parser(name, parents=[common], help=summary).set_defaults(handler=handler)
    return parser


def main() -> int:
    """Run the command line interface.

    Returns:
        int: Process exit code.
    """
    args = build_parser().parse_args()
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
