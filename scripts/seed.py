"""Load the synthetic users into the private EShop database after startup."""

import csv
import sqlite3
from pathlib import Path


root = Path(__file__).resolve().parents[1]
database = root / ".runtime" / "backend" / "database.sqlite"
if not database.is_file():
    raise SystemExit(f"Missing fixture database: {database}")

with (root / "data" / "test-users.csv").open(encoding="utf-8", newline="") as file:
    fixtures = list(csv.DictReader(file))

with sqlite3.connect(database) as db:
    product_categories = dict(db.execute("SELECT id, category_id FROM products"))
    for user in fixtures:
        if product_categories.get(int(user["productId"])) != int(user["categoryId"]):
            raise SystemExit(f"Missing fixture product {user['productId']}")
        found = db.execute("SELECT id FROM users WHERE email = ?", (user["email"],)).fetchone()
        if found is None:
            db.execute("INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, 'user')",
                       ("HW05 synthetic user", user["email"], "InitialFixtureOnly1!"))

print(f"Ready: {len(fixtures)} synthetic users")

