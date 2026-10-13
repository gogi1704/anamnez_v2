import tempfile
import unittest
import io
from pathlib import Path

from backend import database as db
from backend.config import settings
from backend.masterclass_materials import build_masterclass_practices_pdf


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

    def test_masterclass_practices_are_a_real_downloadable_pdf(self):
        from pypdf import PdfReader

        pdf = build_masterclass_practices_pdf('ООО Ромашка')
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertGreater(len(pdf), 20_000)
        extracted = '\n'.join(
            page.extract_text() or ''
            for page in PdfReader(io.BytesIO(pdf)).pages
        )
        self.assertIn('5 практик', extracted)
        self.assertIn('Заземление 5–4–3–2–1', extracted)
        self.assertIn('ООО Ромашка', extracted)

    def test_masterclass_link_tracks_unique_users_and_continuation(self):
        token = 'm' * 43
        first = db.provision_masterclass_access(
            '7707083893', 'ООО Ромашка', 'masterclass_1', token,
        )
        repeated = db.provision_masterclass_access(
            '7707083893', 'ООО Ромашка новое имя', 'masterclass_1', 'n' * 43,
        )
        other_class = db.provision_masterclass_access(
            '7707083893', 'ООО Ромашка новое имя', 'masterclass_2', 'o' * 43,
        )
        self.assertEqual(first['id'], repeated['id'])
        self.assertEqual(repeated['access_token'], token)
        self.assertNotEqual(first['id'], other_class['id'])
        db.ensure_user('chel_masterclass_0001', pending=True)
        db.record_masterclass_access(token, 'chel_masterclass_0001')
        db.record_masterclass_access(token, 'chel_masterclass_0001')
        previous_chel_id = db.current_chel_id()
        try:
            db.set_current_chel_id('chel_masterclass_0001')
            entry = db.current_masterclass_access()
            self.assertEqual(entry['masterclass_name'], 'Мастер-класс 1')
            db.mark_current_masterclass_continued()
            completed = db.mark_current_masterclass_profile_completed()
            self.assertTrue(completed['profile_completed_at'])
            psychology = db.create_or_get_masterclass_psychology_conversation()
            repeated_psychology = db.create_or_get_masterclass_psychology_conversation()
            self.assertEqual(psychology['id'], repeated_psychology['id'])
            self.assertEqual(psychology['dialog_type'], 'masterclass_psychology')
            self.assertEqual(psychology['active_agent'], 'psychologist')
            self.assertEqual(psychology['pinned'], 1)
            self.assertIn(
                'отдельный чат психологической поддержки',
                db.list_messages(psychology['id'])[0]['content'],
            )
        finally:
            db.set_current_chel_id(previous_chel_id)
        report = db.admin_masterclass_report()
        first_row = next(item for item in report['enterprises'] if item['masterclass_code'] == 'masterclass_1')
        self.assertEqual(first_row['company'], 'ООО Ромашка новое имя')
        self.assertEqual((first_row['visit_count'], first_row['users'], first_row['continued_users']), (2, 1, 1))

    def test_psychology_session_lifecycle_and_paid_balance(self):
        token = 's' * 43
        chel_id = 'chel_psychology_sessions_0001'
        db.provision_masterclass_access(
            '7707083893', 'ООО Ромашка', 'masterclass_1', token,
        )
        db.ensure_user(chel_id, pending=True)
        db.record_masterclass_access(token, chel_id)
        previous_chel_id = db.current_chel_id()
        try:
            db.set_current_chel_id(chel_id)
            db.mark_current_masterclass_profile_completed()
            conversation = db.create_or_get_masterclass_psychology_conversation()

            initial = db.psychology_session_summary(conversation['id'])
            self.assertTrue(initial['free_available'])
            self.assertEqual(initial['total_started'], 0)
            self.assertEqual(initial['remaining_paid'], 0)

            free_session = db.request_psychology_session(conversation['id'])
            self.assertEqual(free_session['access_type'], 'free')
            self.assertEqual(free_session['status'], 'requested')
            self.assertTrue(free_session['created'])
            self.assertEqual(
                db.request_psychology_session(conversation['id'])['id'],
                free_session['id'],
            )

            manager_detail = db.manager_conversation_detail(conversation['id'], 'manager')
            self.assertEqual(manager_detail['psychology_session']['id'], free_session['id'])
            completed_session = db.manager_complete_psychology_session(
                conversation['id'], 'Тестовый менеджер', 'manager',
            )
            self.assertEqual(completed_session['status'], 'completed')
            self.assertIsNotNone(completed_session['completed_at'])
            self.assertIsNone(
                db.manager_conversation_detail(conversation['id'], 'manager')['psychology_session'],
            )
            with self.assertRaisesRegex(ValueError, 'Активная заявка'):
                db.manager_complete_psychology_session(
                    conversation['id'], 'Тестовый менеджер', 'manager',
                )
            self.assertIsNone(
                db.manager_complete_psychology_session(
                    conversation['id'], 'Тестовый врач', 'doctor',
                ),
            )
            with db.connection() as connection:
                action = connection.execute(
                    """SELECT action FROM manager_actions
                       WHERE conversation_id=? ORDER BY id DESC LIMIT 1""",
                    (conversation['id'],),
                ).fetchone()
            self.assertEqual(action['action'], 'complete_psychology_session')
            completed = db.psychology_session_summary(conversation['id'])
            self.assertEqual(completed['completed_count'], 1)
            self.assertFalse(completed['free_available'])
            with self.assertRaisesRegex(ValueError, 'Купите следующую сессию'):
                db.request_psychology_session(conversation['id'])

            order = db.create_psychology_session_payment_order(conversation['id'])
            self.assertEqual(order['order_type'], 'psychology_session')
            self.assertEqual(order['amount'], '2000.00')
            with db.connection() as connection:
                connection.execute(
                    """UPDATE payment_orders
                       SET status='succeeded',paid=1,paid_at=?,updated_at=?
                       WHERE id=?""",
                    ('2026-10-13T10:00:00+00:00', '2026-10-13T10:00:00+00:00', order['id']),
                )
                connection.commit()

            paid_balance = db.psychology_session_summary(conversation['id'])
            self.assertEqual(paid_balance['purchased_count'], 1)
            self.assertEqual(paid_balance['remaining_paid'], 1)
            paid_session = db.request_psychology_session(conversation['id'])
            self.assertEqual(paid_session['access_type'], 'paid')
            self.assertEqual(paid_session['session_number'], 2)
            self.assertEqual(
                db.psychology_session_summary(conversation['id'])['remaining_paid'],
                0,
            )
        finally:
            db.set_current_chel_id(previous_chel_id)


if __name__ == '__main__':
    unittest.main()
