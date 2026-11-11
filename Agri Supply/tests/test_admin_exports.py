import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as agri_app


class FakeSMTP:
    sent_messages = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        self.username = username
        self.password = password

    def send_message(self, message):
        self.sent_messages.append(message)


class AdminExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        database_path = Path(self.temp_dir.name) / "test.db"
        self._create_database(database_path)

        self.original_config = {
            key: agri_app.app.config[key]
            for key in (
                "TESTING", "DATABASE", "DB_TYPE", "SECRET_KEY", "MAIL_SERVER", "MAIL_PORT",
                "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER",
                "MAIL_USE_TLS", "MAIL_USE_SSL", "PUBLIC_URL",
            )
        }
        self.addCleanup(self._restore_config)
        agri_app.app.config.update(
            TESTING=True,
            DATABASE=str(database_path),
            DB_TYPE="sqlite",
            SECRET_KEY="test-secret",
            MAIL_SERVER="smtp.example.com",
            MAIL_PORT=587,
            MAIL_USERNAME="shop@example.com",
            MAIL_PASSWORD="mail-test-secret",
            MAIL_DEFAULT_SENDER="shop@example.com",
            MAIL_USE_TLS=True,
            MAIL_USE_SSL=False,
            PUBLIC_URL="https://shop.example.com",
        )
        self.client = agri_app.app.test_client()

    def _create_database(self, database_path):
        db = sqlite3.connect(database_path)
        db.executescript(
            (Path(agri_app.BASE_DIR) / "database" / "schema.sql").read_text(
                encoding="utf-8"
            )
        )
        product_id = db.execute(
            "SELECT product_id FROM products ORDER BY product_id LIMIT 1"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO users
               (full_name, email, password_hash, role, email_verified)
               VALUES ('Admin', 'admin@example.com', ?, 'admin', 1)""",
            (agri_app.generate_password_hash("admin-password"),)
        )
        db.execute(
            """INSERT INTO users
               (full_name, email, password_hash, role, email_verified)
               VALUES ('Customer', 'customer@example.com', ?, 'customer', 1)""",
            (agri_app.generate_password_hash("customer-password"),)
        )
        db.execute(
            "INSERT INTO customers (user_id) VALUES (2)"
        )
        for reference, quantity, unit_price, total, status, order_date in (
            ("PAID-1", 3, 10, 30, "paid", "2026-11-05 08:00:00"),
            ("PAID-2", 1, 2.5, 2.5, "paid", "2026-11-05 10:00:00"),
            ("PAID-3", 4, 7.25, 29, "paid", "2026-11-04 10:00:00"),
            ("UNPAID", 2, 100, 200, "pending", "2026-11-05 11:00:00"),
        ):
            db.execute(
                """INSERT INTO transactions
                   (order_reference, customer_id, product_id, quantity, unit_price,
                    total_amount, payment_method, payment_status, order_date)
                   VALUES (?, 1, ?, ?, ?, ?, 'cash_on_delivery', ?, ?)""",
                (
                    reference, product_id, quantity, unit_price, total,
                    status, order_date
                )
            )
        db.commit()
        db.close()

    def _restore_config(self):
        agri_app.app.config.update(self.original_config)

    def test_paid_sales_csv_aggregates_daily_and_excludes_unpaid_orders(self):
        self.client.post(
            "/login",
            data={"email": "admin@example.com", "password": "admin-password"}
        )

        response = self.client.get("/admin/reports/export.csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertEqual(
            response.headers["Content-Disposition"],
            'attachment; filename="agri-supply-sales.csv"'
        )
        self.assertEqual(
            list(csv.reader(io.StringIO(response.get_data(as_text=True)))),
            [
                ["Sales date", "Items sold", "Paid sales (PHP)"],
                ["2026-11-05", "4", "32.50"],
                ["2026-11-04", "4", "29.00"],
            ]
        )

    def test_csv_export_is_not_available_to_non_admins(self):
        response = self.client.get("/admin/reports/export.csv")
        self.assertEqual(response.status_code, 302)

        self.client.post(
            "/login",
            data={"email": "customer@example.com", "password": "customer-password"}
        )
        response = self.client.get("/admin/reports/export.csv")
        self.assertEqual(response.status_code, 403)

    def test_test_email_cli_uses_configured_smtp(self):
        runner = agri_app.app.test_cli_runner()
        FakeSMTP.sent_messages.clear()
        with patch.object(agri_app.smtplib, "SMTP", FakeSMTP):
            result = runner.invoke(
                args=["test-email", "--email", "recipient@example.com"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Test email sent to recipient@example.com.", result.output)
        self.assertEqual(len(FakeSMTP.sent_messages), 1)
        self.assertEqual(FakeSMTP.sent_messages[0]["To"], "recipient@example.com")
        self.assertIn("email delivery is configured", FakeSMTP.sent_messages[0].get_content())
        self.assertNotIn(
            "mail-test-secret",
            FakeSMTP.sent_messages[0].get_content()
        )

    def test_test_email_cli_reports_missing_smtp_configuration(self):
        agri_app.app.config["MAIL_SERVER"] = ""
        runner = agri_app.app.test_cli_runner()

        result = runner.invoke(
            args=["test-email", "--email", "recipient@example.com"]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Email delivery is not configured", result.output)


if __name__ == "__main__":
    unittest.main()
