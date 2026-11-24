"""Import the actual bot with all external I/O replaced, then run its entry points."""
import asyncio
import copy
import importlib.util
import os
from pathlib import Path
import socket
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import firebase_admin
from test_season_storage import MemoryDB, PRE
from test_season_transition import fixture
from season_transition import KST


class Clock(datetime):
    value = datetime(2026, 11, 30, 22, 59, 59, tzinfo=KST)
    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = []
        def start(p):
            self.stack.append(p)
            self.addCleanup(p.stop)
            return p.start()
        start(patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden in tests')))
        start(patch.dict(os.environ, {'FIREBASE_KEY_JSON': '{}', 'DISCORD_TOKEN': 'offline-test', 'DISCORD_GUILD_ID': '0'}))
        start(patch('dotenv.load_dotenv'))
        start(patch.object(firebase_admin, 'get_app', return_value=object()))
        start(patch('firebase_admin.db.reference', side_effect=AssertionError('Live DB forbidden')))
        spec = importlib.util.spec_from_file_location('_npc_offline_test', Path('main.py'))
        self.main = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.main
        spec.loader.exec_module(self.main)
        root = fixture()
        root['season_state'].pop('preseason_cleared_for')
        self.db = MemoryDB(root)
        self.main.db = self.db
        self.main.settlement_service.g['db'] = self.db
        self.main.datetime = Clock
        Clock.value = datetime(2026, 11, 30, 22, 59, 59, tzinfo=KST)
        start(patch('season_runtime.datetime', Clock))
        start(patch('season_policy.datetime', Clock))
        self.channel = SimpleNamespace(send=AsyncMock(), permissions_for=lambda m: SimpleNamespace(view_channel=True, send_messages=True, embed_links=True))
        self.guild = SimpleNamespace(id=1, me=object(), owner_id=1, members=[], get_channel=lambda i: self.channel)
        self.main.ensure_guild_member_cache_complete = AsyncMock(return_value=(True, ''))

    async def asyncTearDown(self):
        await self.main.bot.close()
        sys.modules.pop('_npc_offline_test', None)
        for p in reversed(self.stack):
            p.stop()

    def interaction(self):
        return SimpleNamespace(extras={}, guild=self.guild,
            user=SimpleNamespace(id=42, display_name='테스트', display_avatar=SimpleNamespace(url='https://example.com/a.png')),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_full_module_commands_serialize_and_expected_features_remain(self):
        commands = {c.name: c for c in self.main.bot.tree.get_commands()}
        for name in ('시즌관리', '시즌정보', '정보', '랭킹', '활동', '출석', '출석랭킹', '칭호관리', '연결끊기', '정산재개'):
            self.assertIn(name, commands)
        for name in ('현재시즌초기화', '다음시즌준비', '시즌보상설정'):
            self.assertNotIn(name, commands)
        for command in commands.values():
            payload = command.to_dict(self.main.bot.tree)
            self.assertLessEqual(len(payload['description']), 100)
        self.assertTrue(commands['시즌관리'].default_permissions.administrator)
        self.assertEqual(len(self.main.season_boundary_task.time), 2)
        self.assertEqual({t.hour for t in self.main.season_boundary_task.time}, {0, 23})
        for t in self.main.season_boundary_task.time:
            self.assertEqual(t.utcoffset().total_seconds(), 9 * 3600)

    async def test_actual_23_reset_00_open_and_late_commit_and_retry(self):
        m = self.main
        state = await m.aget_effective_season_state()
        self.assertEqual(m.season_policy.multiplier(state), 10)
        self.assertEqual(m.calculate_level(1381600), 1257)
        async with m.get_user_state_lock('42'):
            await m.afirebase_root_update_strict({'exp_data/42': {'exp': 99999}, 'mission_data/42': {'text_count': 29}})
        old_state = copy.deepcopy(m.season_policy.STATE.get())
        Clock.value = datetime(2026, 11, 30, 23, 0, tzinfo=KST)
        await m.process_season_start_if_needed(self.guild)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 0)
        state = await m.aget_effective_season_state()
        self.assertEqual(m.season_policy.multiplier(state), 0)
        m.season_policy.bind(old_state)
        with self.assertRaises(ValueError):
            await m.afirebase_root_update_strict({'exp_data/42': {'exp': 88888}})
        Clock.value = datetime(2026, 12, 1, 0, 0, tzinfo=KST)
        result = await m.process_season_start_if_needed(self.guild)
        self.assertTrue(result['processed'], result)
        state = await m.aget_effective_season_state()
        self.assertEqual(state['current_season_id'], '2026_winter')
        self.assertEqual(m.season_policy.multiplier(state), 1)
        self.assertEqual(m.calculate_level(1381600), 100)
        async with m.get_user_state_lock('42'):
            await m.afirebase_root_update_strict({'exp_data/42': {'exp': 30}})
        await m.process_season_start_if_needed(self.guild)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 30)
        self.assertNotIn('preseason_archives', self.db.root)
        self.channel.send.assert_awaited()

    async def test_actual_attendance_preseason_10x_then_locked_zero_and_retry(self):
        m = self.main
        m.update_role_and_nick = AsyncMock()
        m.get_channel_from_cfg = AsyncMock(return_value=None)
        self.db.root['exp_data']['42']['exp'] = 0
        await m.attend.callback(self.interaction())
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 12000)
        await m.attend.callback(self.interaction())
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 12000)
        # A different member can attend during the maintenance hour, at 0 XP.
        Clock.value = datetime(2026, 11, 30, 23, 30, tzinfo=KST)
        i = self.interaction(); i.user.id = 99
        await m.attend.callback(i)
        self.assertEqual(self.db.root.get('exp_data', {}).get('99', {}).get('exp', 0), 0)
        self.assertEqual(self.db.root[m.ATTENDANCE_DB_KEY]['99']['total_days'], 1)
        self.assertGreater(self.db.root['exp_data']['99']['last_activity'], 0)

    async def test_calendar_leap_year_and_original_dates(self):
        m = self.main
        for year, kind, end, pre in ((2026, 'spring', '2026-05-24', '2026-05-25'), (2026, 'summer', '2026-08-24', '2026-08-25'), (2026, 'fall', '2026-11-23', '2026-11-24'), (2027, 'winter', '2028-02-22', '2028-02-23')):
            dates = m._season_dates_for_type(year, kind)
            self.assertEqual(str(dates['regular_end']), end)
            self.assertEqual(str(dates['preseason_start']), pre)

    async def test_actual_chat_voice_and_mission_rewards_for_both_phases(self):
        m = self.main
        m.update_role_and_nick = AsyncMock()
        m.get_channel_from_cfg = AsyncMock(return_value=None)
        m.bot.process_commands = AsyncMock()
        member = SimpleNamespace(id=42, bot=False, display_name='테스트', mention='<@42>', roles=[], guild=self.guild)
        vc = SimpleNamespace(id=9999, category=None, members=[member])
        self.guild.voice_channels = [vc]
        self.guild.stage_channels = []
        self.guild.get_member = lambda uid: None
        message = SimpleNamespace(author=member, guild=self.guild, webhook_id=None, content='테스트 대화',
                                  channel=SimpleNamespace(id=9999, send=AsyncMock()))
        for pre, factor in ((True, 10), (False, 1)):
            Clock.value = datetime(2026, 11, 30 if pre else 20, 12, tzinfo=KST)
            state = dict(fixture()['season_state'], settled=pre, status='preseason' if pre else 'regular')
            self.db.root = {'season_state': state, 'exp_data': {'42': {'exp': 0}},
                'mission_data': {'42': {'date': Clock.value.strftime('%Y-%m-%d'), 'text': {'count': 29}, 'repeat_vc': {'minutes': 14}}}}
            m._last_user_ts.clear()
            with patch.object(m.random, 'randint', return_value=2):
                await m.on_message(message)
            self.assertEqual(self.db.root['exp_data']['42']['exp'], (2 + 300) * factor)
            self.assertTrue((await m.aget_user_mission('42', Clock.value.strftime('%Y-%m-%d')))['text']['completed'])
            with patch.object(type(m.bot), 'guilds', new=property(lambda _: [self.guild])):
                with patch.object(m.random, 'randint', return_value=2):
                    await m.voice_xp_task.coro()
                self.assertEqual(self.db.root['exp_data']['42']['exp'], (2 + 300 + 2) * factor)
                vc.members = [member] + [SimpleNamespace(id=n, bot=False) for n in range(43, 47)]
                # Only the first member has 14 minutes; others accrue one minute.
                await m.repeat_vc_mission_task.coro()
                self.assertEqual(self.db.root['exp_data']['42']['exp'], (2 + 300 + 2 + 150) * factor)
                vc.members = [member]

    async def test_manual_adjustments_not_multiplied_and_maintenance_rejects(self):
        m = self.main
        m.update_role_and_nick = AsyncMock()
        m.get_channel_from_cfg = AsyncMock(return_value=None)
        self.db.root['exp_data']['42']['exp'] = 0
        member = SimpleNamespace(id=42, mention='<@42>', display_name='테스트')
        await m.grant_xp.callback(self.interaction(), member, 100)
        await m.deduct_xp.callback(self.interaction(), member, 30)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 70)
        Clock.value = datetime(2026, 11, 30, 23, 0, tzinfo=KST)
        await m.grant_xp.callback(self.interaction(), member, 100)
        await m.deduct_xp.callback(self.interaction(), member, 30)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 70)

    async def test_actual_settlement_commits_official_record_once_and_starts_preseason(self):
        from season_safety import fingerprint
        m = self.main
        self.guild.get_member = lambda uid: None
        self.db.root['season_state']['settled'] = False
        self.db.root['season_rewards']['2026_fall'] = {'title_name': '가을 기록'}
        state = await m.aget_effective_season_state()
        signature = fingerprint(state, self.db.root['season_rewards']['2026_fall'], self.db.root['exp_data'])
        async with m.SEASON_GATE.exclusive():
            await m.settlement_service.commit(self.interaction(), signature)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 0)
        self.assertEqual(self.db.root['season_records']['2026_fall']['42']['final_exp'], 1381600)
        self.assertTrue(self.db.root['season_settlement']['core_done'])
        self.assertEqual(m.season_policy.multiplier(await m.aget_effective_season_state()), 10)
        self.db.root['exp_data']['42']['exp'] = 500
        with self.assertRaises(ValueError):
            await m.settlement_service.commit(self.interaction(), signature)
        await m.settlement_service.resume(self.guild)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 500)

    async def test_missing_preparation_recovers_after_admin_configuration(self):
        m = self.main
        self.db.root['season_state']['next_ready'] = False
        Clock.value = datetime(2026, 12, 1, 0, 10, tzinfo=KST)
        await m.process_season_start_if_needed(self.guild)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 1381600)
        self.assertEqual(self.db.root['season_state']['status'], 'locked')
        paths = len(self.db.paths)
        await m.process_season_start_if_needed(self.guild)
        self.assertNotIn('', self.db.paths[paths:])
        self.db.root['season_state']['next_ready'] = True
        result = await m.process_season_start_if_needed(self.guild)
        self.assertTrue(result['processed'], result)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 0)
        self.assertEqual(self.db.root['season_state']['current_season_id'], '2026_winter')

    async def test_brief_voice_visit_records_activity_during_maintenance(self):
        Clock.value = datetime(2026, 11, 30, 23, 30, tzinfo=KST)
        member = SimpleNamespace(id=99, bot=False)
        await self.main.record_voice_activity(member, SimpleNamespace(channel=None), SimpleNamespace(channel=object()))
        self.assertGreater(self.db.root['exp_data']['99']['last_activity'], 0)
        self.assertEqual(self.db.root['exp_data']['99'].get('exp', 0), 0)
