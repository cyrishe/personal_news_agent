from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply or drop PNA user/profile schema.")
    parser.add_argument("--target", choices=["sqlite", "mysql"], default="sqlite")
    parser.add_argument("--drop", action="store_true")
    parser.add_argument("--database-url", help="Override database URL. Defaults to .env settings.")
    args = parser.parse_args()

    if args.target == "sqlite":
        db_url = args.database_url or settings.database_url
        sql_path = ROOT / "sql" / ("drop_pna_tables_sqlite.sql" if args.drop else "pna_schema_sqlite.sql")
        apply_sqlite(db_url, sql_path)
    else:
        db_url = args.database_url or settings.stock_agent_db_url
        if not db_url:
            raise SystemExit("MySQL target requires PNA_USER_DB_URL or SIMPLE_BI_PLATFORM_DB_URL in .env")
        sql_path = ROOT / "sql" / ("drop_pna_tables_mysql.sql" if args.drop else "pna_schema_mysql.sql")
        apply_mysql(db_url, sql_path)
    print(f"applied {sql_path.name} to {args.target}")


def apply_sqlite(db_url: str, sql_path: Path) -> None:
    if not db_url.startswith("sqlite:///"):
        raise SystemExit("SQLite URL must start with sqlite:///")
    db_path = Path(db_url.removeprefix("sqlite:///"))
    with sqlite3.connect(db_path) as conn:
        conn.executescript(sql_path.read_text(encoding="utf-8"))


def apply_mysql(db_url: str, sql_path: Path) -> None:
    try:
        import pymysql
    except ImportError as exc:
        raise SystemExit("PyMySQL is required for MySQL schema application") from exc
    parsed = urlparse(db_url)
    if parsed.scheme not in {"mysql+pymysql", "mysql"}:
        raise SystemExit("MySQL URL must use mysql+pymysql:// or mysql://")
    conn = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=(parsed.path or "/").lstrip("/"),
        charset=parse_qs(parsed.query).get("charset", ["utf8mb4"])[0],
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            for statement in split_sql(sql_path.read_text(encoding="utf-8")):
                cursor.execute(statement)
    finally:
        conn.close()


def split_sql(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    cleaned = "\n".join(lines)
    return [part.strip() for part in cleaned.split(";") if part.strip()]


if __name__ == "__main__":
    main()
