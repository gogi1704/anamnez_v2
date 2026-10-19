import tempfile
import unittest
import io
from datetime import date, timedelta
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
            self.assertFalse(entry['is_test'])
            db.mark_current_masterclass_continued()
            completed = db.mark_current_masterclass_profile_completed()
            self.assertTrue(completed['profile_completed_at'])
            psychology = db.create_or_get_masterclass_psychology_conversation()
            repeated_psychology = db.create_or_get_masterclass_psychology_conversation()
            self.assertEqual(psychology['id'], repeated_psychology['id'])
            self.assertEqual(psychology['dialog_type'], 'masterclass_psychology')
            self.assertEqual(psychology['active_agent'], 'psychologist')
            self.assertEqual(psychology['pinned'], 1)
            greeting = db.list_messages(psychology['id'])[0]['content']
            self.assertEqual(greeting, db.MASTERCLASS_PSYCHOLOGY_WELCOME)
            self.assertIn('Я Вита, помощник-психолог', greeting)
            self.assertIn('без ограничений по времени', greeting)
            self.assertIn('Первая консультация бесплатна', greeting)
            self.assertTrue(db.current_masterclass_access()['psychology_chat_started'])
        finally:
            db.set_current_chel_id(previous_chel_id)
        report = db.admin_masterclass_report()
        first_row = next(item for item in report['enterprises'] if item['masterclass_code'] == 'masterclass_1')
        self.assertEqual(first_row['company'], 'ООО Ромашка новое имя')
        self.assertEqual((first_row['visit_count'], first_row['users'], first_row['continued_users']), (2, 1, 1))

    def test_metric2_masterclass_link_completes_only_test_payment_locally(self):
        token = 't' * 43
        link = db.provision_masterclass_access(
            '', 'Тестовая ветка Метрики 2.0', 'masterclass_1', token,
            is_test=True,
        )
        self.assertTrue(link['is_test'])
        chel_id = 'chel_metric2_masterclass_test'
        db.ensure_user(chel_id, pending=True)
        db.record_masterclass_access(token, chel_id)
        previous_chel_id = db.current_chel_id()
        try:
            db.set_current_chel_id(chel_id)
            self.assertTrue(db.current_masterclass_access()['is_test'])
            payment_token = db.masterclass_test_payment_token()
            self.assertTrue(payment_token)
            self.assertTrue(db.verify_masterclass_test_payment_token(payment_token))
            self.assertFalse(db.verify_masterclass_test_payment_token('invalid'))
            db.mark_current_masterclass_profile_completed()
            conversation = db.create_or_get_masterclass_psychology_conversation()
            order = db.create_psychology_session_payment_order(conversation['id'], 1)
            completed = db.complete_test_psychology_session_payment(order['id'])
            self.assertEqual(completed['status'], 'succeeded')
            self.assertTrue(completed['paid'])
            self.assertTrue(completed['test'])
            self.assertFalse(completed['confirmation_url'])
            summary = db.psychology_session_summary(conversation['id'])
            self.assertEqual(summary['purchased_count'], 1)
            self.assertEqual(summary['remaining_paid'], 1)
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_existing_metric2_masterclass_link_is_migrated_to_test_mode(self):
        token = 'q' * 43
        legacy = db.provision_masterclass_access(
            '', 'Тестовая ветка Метрики 2.0', 'masterclass_1', token,
        )
        self.assertFalse(legacy['is_test'])
        db.init_db()
        with db.connection() as conn:
            row = conn.execute(
                "SELECT is_test FROM masterclass_access_links WHERE id=?",
                (legacy['id'],),
            ).fetchone()
        self.assertTrue(row['is_test'])

    def test_psychology_chat_cannot_be_created_outside_completed_masterclass(self):
        previous_chel_id = db.current_chel_id()
        try:
            db.ensure_user('chel_without_masterclass', pending=True)
            db.set_current_chel_id('chel_without_masterclass')
            with self.assertRaisesRegex(ValueError, 'мини-анкету мастер-класса'):
                db.create_or_get_masterclass_psychology_conversation()

            token = 'r' * 43
            db.provision_masterclass_access(
                '7707083893', 'ООО Ромашка', 'masterclass_1', token,
            )
            db.record_masterclass_access(token, 'chel_without_masterclass')
            with self.assertRaisesRegex(ValueError, 'мини-анкету мастер-класса'):
                db.create_or_get_masterclass_psychology_conversation()

            db.mark_current_masterclass_profile_completed()
            conversation = db.create_or_get_masterclass_psychology_conversation()
            self.assertEqual(conversation['dialog_type'], 'masterclass_psychology')
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_psychology_session_purchase_interface_has_one_option(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / 'index.html').read_text(encoding='utf-8')
        script = (project_root / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="psychologySessionsBottomClose"', index)
        self.assertIn('id="psychologySessionBuyButton"', index)
        self.assertIn('id="psychologyPackagesModal"', index)
        self.assertNotIn("Array.from({length:7}", script)
        self.assertIn('Онлайн-встреча · 60 минут', script)
        self.assertIn('const unitPrice = 3500', script)
        self.assertIn("session_count:psychologySessionPackage().sessions", script)
        self.assertIn("sessionCount:psychologySessionPackage().sessions", script)
        self.assertIn('Сессий проведено', script)
        self.assertIn('Сессий предоплачено', script)
        dashboard = (project_root / 'dashboard.html').read_text(encoding='utf-8')
        self.assertIn('<option value="psychologist">Психолог</option>', dashboard)

    def test_masterclass_questionnaire_and_psychology_chat_guide_are_styled(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / 'index.html').read_text(encoding='utf-8')
        script = (project_root / 'static' / 'app.js').read_text(encoding='utf-8')
        styles = (project_root / 'static' / 'styles.css').read_text(encoding='utf-8')

        self.assertIn('id="psychologyChatGuide"', index)
        self.assertIn('id="psychologyGuideRepeat"', index)
        self.assertIn('id="psychologyGuideClose"', index)
        self.assertIn('PSYCHOLOGY_CHAT_GUIDE_SLIDES', script)
        self.assertIn("target:'#psychologySessionsButton'", script)
        self.assertIn("target:'#messageInput'", script)
        self.assertIn("target:'#attachButton'", script)
        self.assertIn("target:'#functionMenuButton'", script)
        self.assertIn('maybeOpenPsychologyChatGuide();', script)
        self.assertIn('masterclass-custom-select', script)
        self.assertNotIn('<select name="smoking"', script)
        self.assertIn('Перед материалом · 7 коротких пунктов', script)
        for number, label in (
            (1, 'Как к вам обращаться?'),
            (2, 'Пол'),
            (3, 'Возраст'),
            (4, 'Рост, см'),
            (5, 'Вес, кг'),
            (6, 'Курение'),
            (7, 'Алкоголь'),
        ):
            self.assertIn(f'<i>{number}</i>{label}', script)
        self.assertNotIn('Перед материалом · 5 коротких пунктов', script)
        self.assertIn('.masterclass-select-options', styles)
        self.assertIn('.masterclass-custom-select.open-up', styles)
        self.assertIn('position:relative;top:auto;right:auto;bottom:auto;left:auto', styles)
        self.assertIn("select.classList.toggle('open-up'", script)
        self.assertIn('MASTERCLASS_BURNOUT_DETAILS', script)
        self.assertIn('Сохранение ресурса', script)
        self.assertIn('Потребность в специалисте', script)
        self.assertIn("name:'Вита'", script)
        self.assertIn('.psychology-guide-spotlight', styles)
        self.assertIn('.masterclass-profile-form>.onboarding-next', styles)
        self.assertIn('font-variant-numeric:tabular-nums', styles)
        self.assertIn('data-psychology-sessions-open', script)
        self.assertIn("entry.psychology_chat_started", script)
        self.assertIn("trackMasterclassScreen('masterclass_returning')", script)
        self.assertIn("state.identity?.psychology_conversation_id", script)
        self.assertIn("function isIkpEntryUrl()", script)
        self.assertIn('id="psychologySessionRequestManager"', index)
        self.assertIn('Позвать менеджера в чат', index)
        self.assertIn("button.dataset.mode = 'link_messenger'", script)
        self.assertIn("openMessengerLinkModal({source:'psychology_session_request'})", script)
        self.assertIn("'/api/psychology-sessions/manager-help'", script)
        self.assertNotIn('psychology_payment_required', script)
        self.assertNotIn("'psychology_support'", script)
        self.assertNotIn('Временный чат с психологом', script)
        self.assertIn(
            '#psychologySessionRequestConfirm { grid-column:1/-1; grid-row:1; }',
            styles,
        )

    def test_psychologist_uses_and_closes_the_main_psychology_chat(self):
        user_id = 'chel_psychology_main_help_0001'
        db.ensure_user(user_id, pending=True)
        previous_chel_id = db.current_chel_id()
        try:
            db.set_current_chel_id(user_id)
            source = db.create_conversation(
                'Психологическая поддержка',
                dialog_type='masterclass_psychology',
            )
            request = db.add_message(
                source['id'], 'user',
                'Хочу согласовать проведение сессии без мессенджера.',
                'psychologist',
            )
            confirmed, created = db.confirm_human_chat(
                source['id'], 'H-123456', recipient_role='psychologist',
            )
            self.assertTrue(created)
            self.assertEqual(confirmed['dialog_type'], 'masterclass_psychology')
            self.assertFalse(bool(confirmed['ai_enabled']))
            db.add_message(
                source['id'], 'assistant',
                'Ожидайте ответа психолога в этом чате.',
                'psychologist',
            )
            waiting_message, _ = db.add_user_message_waiting_for_manager(
                source['id'], 'Мне удобно вечером.',
            )
            self.assertGreater(waiting_message['id'], request['id'])
            reply = db.manager_add_reply(
                source['id'], 'Хорошо, согласуем время.',
                'Тестовый психолог', 'psychologist',
            )
            self.assertEqual(reply['metadata']['staff_role'], 'psychologist')
            self.assertTrue(any(
                item['id'] == source['id']
                and item['dialog_type'] == 'masterclass_psychology'
                for item in db.list_conversations()
            ))
            self.assertIsNotNone(
                db.manager_conversation_detail(source['id'], 'psychologist'),
            )

            closed = db.manager_close_conversation(
                source['id'], 'Тестовый психолог', 'psychologist',
            )
            self.assertEqual(closed['human_status'], 'closed')
            self.assertTrue(bool(closed['ai_enabled']))
            self.assertIsNotNone(db.get_conversation(source['id']))
            contents = [item['content'] for item in db.list_messages(source['id'])]
            self.assertIn('Мне удобно вечером.', contents)
            self.assertIn('Хорошо, согласуем время.', contents)
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_psychology_session_lifecycle_and_paid_balance(self):
        token = 's' * 43
        chel_id = 'chel_psychology_sessions_0001'
        psychologist = db.admin_create_staff(
            'Тестовый психолог', 'psychologist.sessions', '123456',
            telegram_id='79990001122', max_id='79990002233',
            role='psychologist',
        )
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
            self.assertRegex(free_session['request_number'], r'^\d{5}$')
            self.assertEqual(
                db.request_psychology_session(conversation['id'])['id'],
                free_session['id'],
            )

            saved_conversation = db.get_conversation(conversation['id'])
            self.assertEqual(saved_conversation['human_recipient_role'], 'psychologist')
            self.assertEqual(saved_conversation['human_ticket_id'], free_session['request_number'])
            self.assertIsNone(db.manager_conversation_detail(conversation['id'], 'manager'))
            psychologist_detail = db.manager_conversation_detail(
                conversation['id'], 'psychologist',
            )
            self.assertEqual(psychologist_detail['psychology_session']['id'], free_session['id'])
            matching_queue = db.manager_list_conversations(
                free_session['request_number'], 'all', staff_role='psychologist',
            )
            self.assertEqual([item['id'] for item in matching_queue], [conversation['id']])
            self.assertEqual(matching_queue[0]['dialog_type'], 'masterclass_psychology')
            self.assertEqual(db.enqueue_manager_notifications(
                'psychology_session_requested', conversation['id'],
                message_id=int(free_session['request_number']),
                recipient_role='psychologist', request_kind='psychology_session',
                request_number=free_session['request_number'],
            ), 2)
            notification = db.claim_manager_notifications('telegram')[0]
            self.assertEqual(notification['recipient_id'], psychologist['telegram_id'])
            self.assertEqual(notification['payload']['recipient_role'], 'psychologist')
            self.assertEqual(
                notification['payload']['request_number'], free_session['request_number'],
            )
            self.assertIn(
                f"Заявка № {free_session['request_number']}",
                notification['payload']['title'],
            )
            self.assertIn(
                f"Номер заявки: {free_session['request_number']}",
                notification['payload']['body'],
            )
            max_notification = db.claim_manager_notifications('max')[0]
            self.assertEqual(max_notification['recipient_id'], psychologist['max_id'])
            self.assertEqual(
                max_notification['payload']['request_number'],
                free_session['request_number'],
            )
            self.assertEqual(max_notification['payload'], notification['payload'])
            self.assertEqual(db.enqueue_manager_notifications(
                'psychology_crisis', conversation['id'],
                message_id=9915506, recipient_role='psychologist',
            ), 2)
            crisis_notification = db.claim_manager_notifications('telegram')[0]
            self.assertEqual(crisis_notification['event_type'], 'psychology_crisis')
            self.assertEqual(crisis_notification['recipient_id'], psychologist['telegram_id'])
            self.assertEqual(
                crisis_notification['payload']['recipient_role'], 'psychologist',
            )
            self.assertIn('ИИ остановлен', crisis_notification['payload']['body'])
            with self.assertRaisesRegex(ValueError, 'только психолог'):
                db.manager_send_psychology_session_payment_instruction(
                    conversation['id'], 'Тестовый менеджер', 'manager',
                )
            payment_instruction = db.manager_send_psychology_session_payment_instruction(
                conversation['id'], 'Тестовый психолог', 'psychologist',
            )
            self.assertEqual(
                payment_instruction['metadata']['action'],
                'psychology_session_payment_instruction',
            )
            self.assertEqual(payment_instruction['metadata']['staff_role'], 'psychologist')
            self.assertIn('одну 60-минутную сессию за 3 500 ₽', payment_instruction['content'])
            self.assertIn('«Открыть меню сессий»', payment_instruction['content'])
            meeting_day = date.today() + timedelta(days=30)
            meeting_day_value = meeting_day.isoformat()
            invitation = db.manager_send_psychology_session_invitation(
                conversation['id'], 'Тестовый психолог', 'psychologist',
                'https://meet.example.ru/session-15507', meeting_day_value, '15:30',
            )
            self.assertEqual(
                invitation['metadata']['action'], 'psychology_session_invitation',
            )
            self.assertEqual(
                invitation['metadata']['meeting_url'],
                'https://meet.example.ru/session-15507',
            )
            self.assertEqual(
                invitation['metadata']['scheduled_at'][:10], meeting_day_value,
            )
            self.assertIn('15:30 (мск)', invitation['content'])
            with self.assertRaisesRegex(ValueError, 'https://'):
                db.manager_send_psychology_session_invitation(
                    conversation['id'], 'Тестовый психолог', 'psychologist',
                    'javascript:alert(1)', meeting_day_value, '15:30',
                )
            completed_session = db.manager_complete_psychology_session(
                conversation['id'], 'Тестовый психолог', 'psychologist',
            )
            self.assertEqual(completed_session['status'], 'completed')
            self.assertIsNotNone(completed_session['completed_at'])
            completion_messages = [
                item for item in db.list_messages(conversation['id'])
                if item['metadata'].get('action') == 'psychology_session_completed'
            ]
            self.assertEqual(len(completion_messages), 1)
            self.assertIn('Сессия с психологом проведена', completion_messages[0]['content'])
            self.assertEqual(
                completion_messages[0]['metadata']['request_number'],
                completed_session['request_number'],
            )
            self.assertTrue(bool(db.get_conversation(conversation['id'])['ai_enabled']))
            self.assertIsNone(
                db.manager_conversation_detail(
                    conversation['id'], 'psychologist',
                )['psychology_session'],
            )
            with self.assertRaisesRegex(ValueError, 'Активная заявка'):
                db.manager_complete_psychology_session(
                    conversation['id'], 'Тестовый психолог', 'psychologist',
                )
            self.assertIsNone(
                db.manager_complete_psychology_session(
                    conversation['id'], 'Тестовый менеджер', 'manager',
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
            with self.assertRaisesRegex(ValueError, 'Купите сессию'):
                db.request_psychology_session(conversation['id'])

            package = db.psychology_session_package(1)
            self.assertEqual((package['unit_price'], package['total_price']), (3500, 3500))
            order = db.create_psychology_session_payment_order(conversation['id'], 1)
            self.assertEqual(order['order_type'], 'psychology_session')
            self.assertEqual(order['amount'], '3500.00')
            self.assertEqual(order['items'][0]['sessions'], 1)
            self.assertIn('60 минут', order['items'][0]['name'])
            provider_payment = {
                'id': '2f3e4567-89ab-4cde-8012-3456789abcde',
                'status': 'pending', 'paid': False, 'test': False,
                'amount': {'value': '3500.00', 'currency': 'RUB'},
                'metadata': {'order_id': order['id']},
                'confirmation': {
                    'confirmation_url': 'https://yoomoney.ru/pay/psychology-test',
                },
            }
            db.attach_yookassa_payment(order['id'], provider_payment)
            paid_order = db.apply_yookassa_status(order['id'], {
                **provider_payment, 'status': 'succeeded', 'paid': True,
            })
            self.assertTrue(paid_order['paid'])
            self.assertEqual(paid_order['status'], 'succeeded')
            self.assertFalse(paid_order['test'])

            paid_balance = db.psychology_session_summary(conversation['id'])
            self.assertEqual(paid_balance['purchased_count'], 1)
            self.assertEqual(paid_balance['remaining_paid'], 1)
            self.assertEqual(db.enqueue_manager_notifications(
                'psychology_payment_succeeded', conversation['id'],
                message_id=9915507, recipient_role='psychologist',
                amount_kopecks=350000,
            ), 2)
            payment_notification = db.claim_manager_notifications('telegram')[0]
            self.assertEqual(
                payment_notification['event_type'], 'psychology_payment_succeeded',
            )
            self.assertIn('3 500 ₽', payment_notification['payload']['body'])
            updated_psychologist = db.admin_update_staff(
                psychologist['id'], notify_successful_payments=False,
            )
            self.assertFalse(updated_psychologist['notify_successful_payments'])
            self.assertEqual(db.enqueue_manager_notifications(
                'psychology_payment_succeeded', conversation['id'],
                message_id=9915508, recipient_role='psychologist',
                amount_kopecks=350000,
            ), 0)
            paid_session = db.request_psychology_session(conversation['id'])
            self.assertEqual(paid_session['access_type'], 'paid')
            self.assertEqual(paid_session['session_number'], 2)
            self.assertEqual(db.enqueue_manager_notifications(
                'psychology_session_requested', conversation['id'],
                message_id=int(paid_session['request_number']),
                recipient_role='psychologist', request_kind='psychology_session',
                request_number=paid_session['request_number'],
            ), 2)
            self.assertEqual(
                db.psychology_session_summary(conversation['id'])['remaining_paid'],
                1,
            )
            with db.connection() as connection:
                credit_use = connection.execute(
                    """SELECT payment_order_id,credit_number
                       FROM psychology_session_credit_uses WHERE session_id=?""",
                    (paid_session['id'],),
                ).fetchone()
            self.assertEqual(credit_use['payment_order_id'], order['id'])
            self.assertEqual(credit_use['credit_number'], 1)
            db.manager_complete_psychology_session(
                conversation['id'], 'Тестовый психолог', 'psychologist',
            )
            self.assertEqual(
                db.psychology_session_summary(conversation['id'])['remaining_paid'], 0,
            )
            with db.connection() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM psychology_session_credit_uses WHERE payment_order_id=?",
                    (order['id'],),
                ).fetchone()[0], 1)
                self.assertIsNotNone(connection.execute(
                    "SELECT fulfillment_at FROM payment_orders WHERE id=?",
                    (order['id'],),
                ).fetchone()['fulfillment_at'])
            with self.assertRaisesRegex(ValueError, 'одну сессию'):
                db.create_psychology_session_payment_order(conversation['id'], 2)
        finally:
            db.set_current_chel_id(previous_chel_id)


if __name__ == '__main__':
    unittest.main()
