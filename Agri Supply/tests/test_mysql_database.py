import unittest

import app as agri_app


class FakeCursor:
    def __init__(self, with_rows, rows=None, rowcount=0, lastrowid=None):
        self.with_rows = with_rows
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.closed = False

    def execute(self, query, args):
        self.query = query
        self.args = args

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.cursors = []

    def cursor(self, dictionary=False, buffered=False):
        cursor = FakeCursor(
            with_rows=True,
            rows=[{"user_id": 7, "email": "customer@example.com"}]
        )
        self.cursors.append(cursor)
        return cursor


class MySQLAdapterTests(unittest.TestCase):
    def test_execute_translates_placeholders_and_rows_support_sqlite_access(self):
        raw_connection = FakeConnection()
        db = agri_app.MySQLConnection(raw_connection)

        row = db.execute(
            "SELECT user_id, email FROM users WHERE user_id = ?", (7,)
        ).fetchone()

        cursor = raw_connection.cursors[0]
        self.assertEqual(
            cursor.query, "SELECT user_id, email FROM users WHERE user_id = %s"
        )
        self.assertEqual(cursor.args, (7,))
        self.assertEqual(row["email"], "customer@example.com")
        self.assertEqual(row[0], 7)
        self.assertTrue(cursor.closed)

    def test_begin_immediate_starts_a_mysql_transaction(self):
        raw_connection = FakeConnection()
        db = agri_app.MySQLConnection(raw_connection)

        db.execute("BEGIN IMMEDIATE")

        self.assertEqual(raw_connection.cursors[0].query, "START TRANSACTION")


if __name__ == "__main__":
    unittest.main()
