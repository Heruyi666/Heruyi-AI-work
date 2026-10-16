"""Demo database seed: creates a sample SQLite database with test data
and auto-registers it as a connection on first startup."""

import logging
from pathlib import Path

from sqlmodel import Session
from app.config import settings
from app.database import engine
from app.models.database import DatabaseConnection, DatabaseType, ConnectionStatus

logger = logging.getLogger(__name__)

DEMO_DB_NAME = "demo"
DEMO_CONNECTION_NAME = "Demo Database (SQLite)"


def _get_demo_db_path() -> Path:
    return Path(settings.db_path).parent / "demo.db"


def seed_demo_database() -> str:
    """Create demo SQLite database with sample tables and data.
    Returns the database file path.
    """
    import sqlite3

    db_path = _get_demo_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # --- Departments ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            budget REAL NOT NULL DEFAULT 0.0
        )
    """)

    # --- Employees ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            department_id INTEGER NOT NULL,
            salary REAL NOT NULL,
            hire_date TEXT NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        )
    """)

    # --- Products ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0
        )
    """)

    # --- Orders ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    # Only insert if tables are empty
    if conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO departments (id, name, budget) VALUES (?, ?, ?)",
            [
                (1, "Engineering", 5000000.0),
                (2, "Marketing", 2000000.0),
                (3, "Sales", 3500000.0),
                (4, "HR", 800000.0),
                (5, "Finance", 1200000.0),
            ],
        )

    if conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO employees (name, email, department_id, salary, hire_date) VALUES (?, ?, ?, ?, ?)",
            [
                ("Alice Wang", "alice@example.com", 1, 120000, "2020-03-15"),
                ("Bob Li", "bob@example.com", 1, 110000, "2019-07-01"),
                ("Carol Zhang", "carol@example.com", 1, 105000, "2021-01-10"),
                ("Dave Chen", "dave@example.com", 2, 95000, "2020-06-20"),
                ("Eve Liu", "eve@example.com", 2, 88000, "2022-02-14"),
                ("Frank Yang", "frank@example.com", 3, 130000, "2018-11-05"),
                ("Grace Wu", "grace@example.com", 3, 115000, "2019-09-18"),
                ("Henry Zhao", "henry@example.com", 3, 102000, "2021-05-22"),
                ("Ivy Sun", "ivy@example.com", 4, 75000, "2022-08-01"),
                ("Jack Ma", "jack@example.com", 5, 140000, "2018-03-30"),
                ("Kate Xu", "kate@example.com", 1, 108000, "2023-01-15"),
                ("Leo Huang", "leo@example.com", 5, 125000, "2020-12-01"),
            ],
        )

    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO products (name, category, price, stock) VALUES (?, ?, ?, ?)",
            [
                ("MacBook Pro 16", "Electronics", 2499.99, 50),
                ("iPhone 15 Pro", "Electronics", 999.99, 120),
                ("AirPods Pro", "Electronics", 249.99, 200),
                ("Mechanical Keyboard", "Accessories", 149.99, 300),
                ("USB-C Hub", "Accessories", 79.99, 500),
                ("4K Monitor 27\"", "Electronics", 599.99, 35),
                ("Wireless Mouse", "Accessories", 49.99, 250),
                ("Laptop Stand", "Accessories", 39.99, 180),
                ("Webcam HD", "Electronics", 89.99, 90),
                ("External SSD 1TB", "Electronics", 129.99, 75),
            ],
        )

    if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO orders (customer_name, product_id, quantity, amount, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("Client A Corp", 1, 2, 4999.98, "completed", "2026-01-10"),
                ("Startup XYZ", 2, 5, 4999.95, "completed", "2026-02-14"),
                ("Client A Corp", 3, 10, 2499.90, "completed", "2026-02-20"),
                ("BigCo Ltd", 1, 3, 7499.97, "pending", "2026-03-05"),
                ("Startup XYZ", 6, 4, 2399.96, "completed", "2026-03-12"),
                ("BigCo Ltd", 4, 15, 2249.85, "shipped", "2026-04-01"),
                ("Client A Corp", 10, 2, 259.98, "completed", "2026-04-15"),
                ("SmallBiz Inc", 2, 1, 999.99, "pending", "2026-04-20"),
                ("BigCo Ltd", 5, 20, 1599.80, "completed", "2026-05-08"),
                ("Startup XYZ", 7, 8, 399.92, "completed", "2026-05-18"),
                ("SmallBiz Inc", 6, 2, 1199.98, "shipped", "2026-06-01"),
                ("Client A Corp", 8, 6, 239.94, "completed", "2026-06-10"),
                ("BigCo Ltd", 9, 3, 269.97, "completed", "2026-06-22"),
                ("Startup XYZ", 10, 4, 519.96, "pending", "2026-07-03"),
                ("SmallBiz Inc", 3, 5, 1249.95, "completed", "2026-07-15"),
                ("Client A Corp", 1, 1, 2499.99, "shipped", "2026-08-01"),
            ],
        )

    conn.commit()
    conn.close()

    logger.info(f"Demo database seeded at: {db_path}")
    return str(db_path)


def register_demo_connection() -> None:
    """Register the demo SQLite connection in the metadata database if not present."""
    with Session(engine) as session:
        existing = session.get(DatabaseConnection, DEMO_DB_NAME)
        if existing is None:
            db_path = _get_demo_db_path()
            connection = DatabaseConnection(
                name=DEMO_DB_NAME,
                url=f"sqlite:///{db_path}",
                db_type=DatabaseType.SQLITE,
                description=DEMO_CONNECTION_NAME,
                status=ConnectionStatus.ACTIVE,
            )
            session.add(connection)
            session.commit()
            logger.info(f"Registered demo connection: {DEMO_DB_NAME}")
