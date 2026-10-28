import tempfile
import unittest
import io
import base64
import json
from datetime import date, timedelta
from pathlib import Path

from backend import database as db
from backend import chat_attachments
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

    def test_unattributed_new_user_selects_and_keeps_entry_route(self):
        previous_chel_id = db.current_chel_id()
        try:
            chel_id = 'chel_unattributed_entry_0001'
            db.ensure_user(chel_id, pending=True)
            db.set_current_chel_id(chel_id)
            self.assertTrue(db.entry_route_choice_required())
            selected = db.set_entry_route('other')
            self.assertEqual(selected['entry_route'], 'other')
            self.assertFalse(db.entry_route_choice_required())
            db.save_onboarding(status='appearance', font_size='large')
            self.assertEqual(db.get_onboarding()['entry_route'], 'other')
            selected = db.set_entry_route('results')
            self.assertEqual(selected['entry_route'], 'results')
            with self.assertRaisesRegex(ValueError, 'направление'):
                db.set_entry_route('unknown')
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_explicit_service_link_skips_entry_route_choice(self):
        previous_chel_id = db.current_chel_id()
        try:
            chel_id = 'chel_explicit_entry_0001'
            db.ensure_user(chel_id, pending=True)
            db.set_current_chel_id(chel_id)
            link = db.provision_ikp_access(
                '7707083893', 'ООО Ромашка', 'i' * 43,
            )
            db.record_ikp_access(link['access_token'], chel_id)
            self.assertFalse(db.entry_route_choice_required())
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_chat_guides_are_persisted_once_per_user_and_chat_type(self):
        previous_chel_id = db.current_chel_id()
        try:
            first_user = 'chel_chat_guides_user_0001'
            second_user = 'chel_chat_guides_user_0002'
            db.ensure_user(first_user, pending=True)
            db.ensure_user(second_user, pending=True)
            db.set_current_chel_id(first_user)

            self.assertEqual(db.current_user_chat_guides(), [])
            for guide_type in ('general', 'weight_control', 'masterclass_psychology'):
                completed = db.complete_chat_guide(guide_type)
                self.assertEqual(completed['guide_type'], guide_type)
            db.complete_chat_guide('general')
            self.assertEqual(
                set(db.current_user_chat_guides()),
                {'general', 'weight_control', 'masterclass_psychology'},
            )
            with self.assertRaisesRegex(ValueError, 'тип обучения'):
                db.complete_chat_guide('unknown')

            db.set_current_chel_id(second_user)
            self.assertEqual(db.current_user_chat_guides(), [])
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_unknown_entry_direction_screen_routes_all_four_choices(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / 'index.html').read_text(encoding='utf-8')
        script = (project_root / 'static' / 'app.js').read_text(encoding='utf-8')
        styles = (project_root / 'static' / 'styles.css').read_text(encoding='utf-8')
        main = (project_root / 'backend' / 'main.py').read_text(encoding='utf-8')

        self.assertIn('id="entryDirectionScreen"', index)
        self.assertIn('data-entry-route="masterclass"', index)
        self.assertIn('data-entry-route="medical_exam"', index)
        self.assertIn('data-entry-route="results"', index)
        self.assertIn('data-entry-route="other"', index)
        self.assertIn('Чем мы можем вам помочь?', index)
        self.assertIn('Выберите нужный раздел — мы сразу направим вас дальше.', index)
        self.assertIn("identity.entry_route_choice_required", script)
        self.assertIn("await api('/api/entry-route'", script)
        self.assertIn("onboarding.entry_route === 'other'", script)
        self.assertIn("onboarding.entry_route === 'results'", script)
        self.assertIn("await enterResultFlow({explicit:true})", script)
        self.assertIn("$('#entryDirectionScreen').classList.add('hidden');", script)
        self.assertIn("showEntryDirection();\n    $('#entryDirectionError').textContent", script)
        self.assertIn('.entry-direction-options', styles)
        self.assertIn('if path == "/api/entry-route":', main)

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
            self.assertIn('Я Вита — ИИ-помощник команды психологов', greeting)
            self.assertIn('Я не человек и не ставлю диагнозов', greeting)
            self.assertIn('первая 30-минутная сессия бесплатна', greeting)
            self.assertNotIn('по видео', greeting)
            self.assertIn('мысли причинить себе вред', greeting)
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

    def test_psychology_chat_is_available_without_masterclass(self):
        previous_chel_id = db.current_chel_id()
        try:
            db.ensure_user('chel_without_masterclass', pending=True)
            db.set_current_chel_id('chel_without_masterclass')
            conversation = db.create_or_get_masterclass_psychology_conversation()
            repeated = db.create_or_get_masterclass_psychology_conversation()
            self.assertEqual(conversation['id'], repeated['id'])
            self.assertEqual(conversation['dialog_type'], 'masterclass_psychology')
            self.assertEqual(conversation['active_agent'], 'psychologist')
            context = json.loads(conversation['context_summary'])
            self.assertEqual(context['user_goal'], 'получить психологическую поддержку')
            self.assertEqual(context['known_facts'], [])
            welcome = db.list_messages(conversation['id'])[0]
            self.assertEqual(welcome['content'], db.MASTERCLASS_PSYCHOLOGY_WELCOME)
            self.assertNotIn('masterclass_code', welcome['metadata'])

            db.ensure_user('chel_incomplete_masterclass', pending=True)
            db.set_current_chel_id('chel_incomplete_masterclass')

            token = 'r' * 43
            db.provision_masterclass_access(
                '7707083893', 'ООО Ромашка', 'masterclass_1', token,
            )
            db.record_masterclass_access(token, 'chel_incomplete_masterclass')
            attributed = db.create_or_get_masterclass_psychology_conversation()
            attributed_context = json.loads(attributed['context_summary'])
            self.assertIn('после мастер-класса', attributed_context['user_goal'])
            attributed_welcome = db.list_messages(attributed['id'])[0]
            self.assertEqual(
                attributed_welcome['metadata']['masterclass_code'], 'masterclass_1',
            )
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
        self.assertIn('Согласованный формат · 60 минут', script)
        self.assertIn('const unitPrice = 3500', script)
        self.assertIn("session_count:psychologySessionPackage().sessions", script)
        self.assertIn("sessionCount:psychologySessionPackage().sessions", script)
        self.assertIn('Сессий проведено', script)
        self.assertIn('Сессий предоплачено', script)
        dashboard = (project_root / 'dashboard.html').read_text(encoding='utf-8')
        self.assertIn('<option value="psychologist">Психолог</option>', dashboard)

    def test_psychology_chat_reset_deletes_context_but_preserves_sessions(self):
        previous_chel_id = db.current_chel_id()
        try:
            chel_id = 'chel_psychology_reset_0001'
            db.ensure_user(chel_id, pending=True)
            db.set_current_chel_id(chel_id)
            conversation = db.create_conversation(
                'Психологическая поддержка',
                dialog_type='masterclass_psychology', pinned=True,
            )
            db.add_message(
                conversation['id'], 'assistant', db.MASTERCLASS_PSYCHOLOGY_WELCOME,
                'psychologist',
            )
            attachment = chat_attachments.store({
                'name': 'note.txt', 'type': 'text/plain',
                'data_url': 'data:text/plain;base64,' + base64.b64encode(
                    b'private chat attachment',
                ).decode('ascii'),
            }, chel_id)
            db.add_message(
                conversation['id'], 'user', 'Старое сообщение', 'psychologist',
                {'attachments': [chat_attachments.public_metadata(attachment)]},
            )
            db.save_psychology_dialog_state(
                conversation['id'], {'stage': 'reflection', 'user_words': ['тревога']},
            )
            db.update_conversation(
                conversation['id'], active_agent='psychologist',
                context_summary='{"current_topic":"тревога"}',
                status='waiting_human', human_status='pending',
                human_ticket_id='TEST-RESET', human_channel='chat', ai_enabled=False,
            )
            now = db.utc_now()
            with db.connection() as conn:
                conn.execute(
                    """INSERT INTO psychology_sessions
                       (id,conversation_id,chel_id,session_number,request_number,
                        access_type,status,started_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,'requested',?,?,?)""",
                    (
                        'session-reset-preserved', conversation['id'], chel_id, 1,
                        '12345', 'free', now, now, now,
                    ),
                )
                conn.commit()

            reset = db.reset_masterclass_psychology_conversation(conversation['id'])

            self.assertIsNotNone(reset)
            self.assertEqual(reset['context_summary'], '{}')
            self.assertTrue(reset['ai_enabled'])
            self.assertEqual(reset['human_status'], 'none')
            self.assertEqual(len(reset['messages']), 1)
            self.assertEqual(reset['messages'][0]['content'], db.MASTERCLASS_PSYCHOLOGY_WELCOME)
            self.assertTrue(reset['messages'][0]['metadata']['conversation_reset'])
            self.assertEqual(len(db.list_message_deletions(conversation['id'])), 2)
            state = db.get_psychology_dialog_state(conversation['id'])
            self.assertEqual(state['message_index'], 0)
            self.assertEqual(
                db.psychology_session_summary(conversation['id'])['pending_session']['request_number'],
                '12345',
            )
            token = attachment['url'].rsplit('/', 1)[-1]
            self.assertFalse((chat_attachments.storage_dir() / f'{token}.json').exists())
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_psychology_chat_reset_rejects_other_dialogs_and_users(self):
        previous_chel_id = db.current_chel_id()
        try:
            owner = 'chel_psychology_reset_owner'
            db.ensure_user(owner, pending=True)
            db.set_current_chel_id(owner)
            general = db.create_conversation('Обычный диалог', dialog_type='general')
            psychology = db.create_conversation(
                'Психологическая поддержка', dialog_type='masterclass_psychology',
            )
            db.add_message(psychology['id'], 'user', 'Секрет', 'psychologist')
            self.assertIsNone(db.reset_masterclass_psychology_conversation(general['id']))

            stranger = 'chel_psychology_reset_stranger'
            db.ensure_user(stranger, pending=True)
            db.set_current_chel_id(stranger)
            self.assertIsNone(db.reset_masterclass_psychology_conversation(psychology['id']))

            db.set_current_chel_id(owner)
            self.assertEqual(db.list_messages(psychology['id'])[0]['content'], 'Секрет')
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_psychology_chat_reset_does_not_bypass_crisis_lock(self):
        previous_chel_id = db.current_chel_id()
        try:
            chel_id = 'chel_psychology_reset_crisis'
            db.ensure_user(chel_id, pending=True)
            db.set_current_chel_id(chel_id)
            conversation = db.create_conversation(
                'Психологическая поддержка', dialog_type='masterclass_psychology',
            )
            db.add_message(conversation['id'], 'user', 'Опасное сообщение', 'psychologist')
            db.update_conversation(
                conversation['id'], active_agent='psychologist', context_summary='{}',
                status='waiting_human', human_status='pending',
                human_ticket_id='URG-ABC123', human_channel='chat', ai_enabled=False,
            )

            reset = db.reset_masterclass_psychology_conversation(conversation['id'])

            self.assertFalse(bool(reset['ai_enabled']))
            self.assertEqual(reset['status'], 'waiting_human')
            self.assertEqual(reset['human_status'], 'pending')
            self.assertEqual(reset['human_ticket_id'], 'URG-ABC123')
            self.assertEqual(len(reset['messages']), 1)
        finally:
            db.set_current_chel_id(previous_chel_id)

    def test_psychology_chat_reset_controls_are_wired(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / 'index.html').read_text(encoding='utf-8')
        script = (project_root / 'static' / 'app.js').read_text(encoding='utf-8')
        main = (project_root / 'backend' / 'main.py').read_text(encoding='utf-8')
        self.assertIn('id="psychologyResetButton"', index)
        self.assertIn('id="psychologyResetModal"', index)
        self.assertIn('id="psychologyResetConfirm"', index)
        self.assertIn('/reset`,', script)
        self.assertIn('reset_masterclass_psychology_conversation', main)

    def test_masterclass_questionnaire_and_all_chat_guides_are_styled(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / 'index.html').read_text(encoding='utf-8')
        script = (project_root / 'static' / 'app.js').read_text(encoding='utf-8')
        styles = (project_root / 'static' / 'styles.css').read_text(encoding='utf-8')
        dashboard = (project_root / 'static' / 'dashboard.js').read_text(encoding='utf-8')
        main = (project_root / 'backend' / 'main.py').read_text(encoding='utf-8')

        self.assertIn('id="chatGuide"', index)
        self.assertIn('id="chatGuideRepeat"', index)
        self.assertIn('id="chatGuideClose"', index)
        self.assertIn('CHAT_GUIDE_SLIDES', script)
        self.assertIn('general: [', script)
        self.assertIn('weight_control: [', script)
        self.assertIn('masterclass_psychology: [', script)
        self.assertIn("target:'#psychologySessionsButton'", script)
        self.assertIn("target:'#weightToolsButton'", script)
        self.assertIn("target:'#capabilitiesButton'", script)
        self.assertIn("target:'#messageInput'", script)
        self.assertIn("target:'#attachButton'", script)
        self.assertIn("target:'#functionMenuButton'", script)
        self.assertGreaterEqual(script.count('maybeOpenChatGuide();'), 2)
        self.assertIn("'/api/chat-guides/complete'", script)
        self.assertIn('if path == "/api/chat-guides/complete":', main)
        self.assertIn('"chat_guides": db.current_user_chat_guides()', main)
        self.assertIn('masterclass-custom-select', script)
        self.assertIn("setOnboardingMeta('Мастер-класс ·\\nПсихология', 20)", script)
        self.assertIn('white-space:pre-line', styles)
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
        self.assertIn('.chat-guide-spotlight', styles)
        self.assertIn('.masterclass-profile-form>.onboarding-next', styles)
        self.assertIn('font-variant-numeric:tabular-nums', styles)
        self.assertIn('data-psychology-sessions-open', script)
        self.assertIn("entry.psychology_chat_started", script)
        self.assertIn("trackMasterclassScreen('masterclass_returning')", script)
        self.assertIn("trackMasterclassAction('self_check_started'", script)
        self.assertIn("trackMasterclassAction('self_check_completed'", script)
        self.assertIn("trackMasterclassAction('material_tracking_ready'", script)
        self.assertIn("trackMasterclassAction('download_guide'", script)
        self.assertIn("metricGroup('Вход в мастер-класс'", dashboard)
        self.assertIn("metricGroup(\n      'Материалы'", dashboard)
        self.assertIn("metricGroup(\n      'Самопроверка'", dashboard)
        self.assertIn("metricGroup(\n      'Чат с Витой'", dashboard)
        self.assertIn("['Не отвечали'", dashboard)
        self.assertIn("['Начали'", dashboard)
        self.assertIn("['Завершили'", dashboard)
        self.assertIn('Скачали гайд', dashboard)
        self.assertIn('Очисток диалога', dashboard)
        self.assertIn("['Пользователи'", dashboard)
        self.assertIn('data-new-dialog="psychology"', index)
        self.assertIn('function startPsychologyConversation()', script)
        self.assertIn("button.dataset.newDialog === 'psychology'", script)
        self.assertIn("/api/conversations/psychology", script)
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

    def test_psychologist_can_observe_every_vita_chat_before_handoff(self):
        psychologist = db.admin_create_staff(
            'Психолог-наблюдатель', 'psychologist.observer', '123456',
            role='psychologist',
        )
        user_id = 'chel_psychology_observer_0001'
        db.ensure_user(user_id, pending=True)
        previous_chel_id = db.current_chel_id()
        try:
            db.set_current_chel_id(user_id)
            vita_chat = db.create_conversation(
                'Психологическая поддержка',
                dialog_type='masterclass_psychology',
            )
            greeting = db.add_message(
                vita_chat['id'], 'assistant', 'Здравствуйте! Я Вита.', 'psychologist',
            )
            ordinary_chat = db.create_conversation('Обычный диалог')

            queue = db.manager_list_conversations(
                '', 'open', include_related=True,
                staff_role='psychologist', staff_id=psychologist['id'],
            )
            self.assertIn(vita_chat['id'], [item['id'] for item in queue])
            self.assertNotIn(ordinary_chat['id'], [item['id'] for item in queue])
            self.assertIsNotNone(
                db.manager_conversation_detail(vita_chat['id'], 'psychologist'),
            )
            self.assertIsNone(
                db.manager_conversation_detail(ordinary_chat['id'], 'psychologist'),
            )
            marked = db.manager_mark_conversation_read(
                vita_chat['id'], psychologist['id'], 'psychologist', greeting['id'],
            )
            self.assertEqual(marked['last_read_message_id'], greeting['id'])
            with self.assertRaisesRegex(ValueError, 'Диалог не найден'):
                db.manager_add_reply(
                    vita_chat['id'], 'Ответ до передачи',
                    psychologist['display_name'], 'psychologist',
                )

            manager_script = (
                Path(__file__).resolve().parents[1] / 'static' / 'manager.js'
            ).read_text(encoding='utf-8')
            self.assertIn("const observingVitaChat", manager_script)
            self.assertIn(
                "$('#managerReplyForm').classList.toggle('hidden', observingVitaChat)",
                manager_script,
            )
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
                f"ЗАЯВКА № {free_session['request_number']}",
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

            purchases = db.list_payment_orders()
            session_purchase = next(
                item for item in purchases if item['id'] == order['id']
            )
            self.assertEqual(session_purchase['order_type'], 'psychology_session')
            self.assertEqual(session_purchase['status'], 'succeeded')
            self.assertTrue(session_purchase['paid'])
            self.assertEqual(session_purchase['conversation_id'], conversation['id'])
            self.assertEqual(session_purchase['items'][0]['id'], 'psychology_session')

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
