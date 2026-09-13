import tempfile
import unittest
from pathlib import Path

from backend import database as db
from backend.config import settings


class IkpAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = settings.database_path
        object.__setattr__(settings, 'database_path', Path(self.temp_dir.name) / 'consilium.db')
        db.init_db()

    def tearDown(self):
        object.__setattr__(settings, 'database_path', self.original_path)
        self.temp_dir.cleanup()

    def test_link_is_unique_per_inn_and_users_are_excluded_from_common_metrics(self):
        token = 'a' * 43
        first = db.provision_ikp_access('7707083893', 'ООО Ромашка', token)
        repeated = db.provision_ikp_access('7707083893', 'ООО Ромашка новое имя', 'b' * 43)
        self.assertEqual(first['id'], repeated['id'])
        self.assertEqual(repeated['access_token'], token)
        db.ensure_user('chel_ikp_user_0001', pending=True)
        db.record_ikp_access(token, 'chel_ikp_user_0001')
        db.record_ikp_access(token, 'chel_ikp_user_0001')
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT IS_STATS_USER(?)", ('chel_ikp_user_0001',)).fetchone()[0], 0)
        report = db.admin_ikp_report()
        self.assertEqual(report['summary'], {'links':1,'visits':2,'users':1,'active_users':0})
        self.assertEqual(report['links'][0]['company'], 'ООО Ромашка новое имя')
        self.assertEqual(report['users'][0]['visit_count'], 2)


if __name__ == '__main__':
    unittest.main()
