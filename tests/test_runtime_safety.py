import ast
import asyncio
import copy
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

import discord
from runtime_safety import daily_mission, mark_operation, failure_message


def functions(env, *names):
    tree = ast.parse(Path('main.py').read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), env)
    return env


class DailyTests(unittest.TestCase):
    def test_new_day_replaces_old_record_without_history(self):
        old = {'date': '2026-10-08', 'text': {'count': 30, 'completed': True}, 'repeat_vc': {'minutes': 44}}
        result = daily_mission(old, '2026-10-09')
        self.assertEqual(result, {'date': '2026-10-09', 'text': {'count': 0, 'completed': False}, 'repeat_vc': {'minutes': 0}})
        self.assertEqual(old['text']['count'], 30)

    def test_same_day_keeps_progress_and_repairs_invalid_counts(self):
        result = daily_mission({'date': 'today', 'text': {'count': None}, 'repeat_vc': {'minutes': '15'}}, 'today')
        self.assertEqual(result['text']['count'], 0)
        self.assertEqual(result['repeat_vc']['minutes'], 15)

    def test_no_daily_global_delete_task(self):
        source = Path('main.py').read_text(encoding='utf-8')
        self.assertNotIn('reset_daily_missions', source)
        # Season settlement remains a separate, intentional reset.
        self.assertIn('"mission_data": None', source)

    def test_error_messages_distinguish_known_and_unknown_commit(self):
        i = SimpleNamespace(extras={}, command=SimpleNamespace(name='출석'), user=SimpleNamespace(id=1))
        for status, expected in [('not_saved', '저장 전에'), ('saving', '완료 여부'), ('saved', '기록은 저장됐지만'), ('read_only', '변경하지 않습니다')]:
            mark_operation(i, '단계', status)
            with self.assertLogs(level='ERROR') as logs:
                text = failure_message(i, RuntimeError('private detail'))
            self.assertIn(expected, text)
            self.assertNotIn('private detail', text)
            code = text.split('오류 번호: `')[1].split('`')[0]
            self.assertIn(code, logs.output[0])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lock = asyncio.Lock()
        self.clock = SimpleNamespace(value=datetime(2026, 10, 8, 23, 59, tzinfo=timezone.utc))
        clock = self.clock
        class Clock:
            @staticmethod
            def now(tz):
                return clock.value
        self.mission = {'date': '2026-10-08', 'text': {'count': 29, 'completed': False}, 'repeat_vc': {'minutes': 14}}
        self.xp = {'exp': 0, 'level': 1, 'voice_minutes': 0, 'last_activity': 0}
        self.writes = []

        async def save(updates):
            await asyncio.sleep(0)
            self.writes.append(copy.deepcopy(updates))
            if 'mission_data/42' in updates:
                self.mission = copy.deepcopy(updates['mission_data/42'])
            if 'exp_data/42' in updates:
                self.xp = copy.deepcopy(updates['exp_data/42'])
        self.env = dict(discord=discord, logging=logging, datetime=Clock, KST=timezone.utc,
                        time=SimpleNamespace(time=lambda: self.clock.value.timestamp()),
                        get_user_state_lock=lambda uid: self.lock,
                        aget_user_exp=AsyncMock(side_effect=lambda uid: copy.deepcopy(self.xp)),
                        aget_user_mission=AsyncMock(side_effect=lambda uid, day: daily_mission(self.mission, day)),
                        afirebase_root_update_strict=save, asave_user_exp=AsyncMock(),
                        aseason_xp_enabled=AsyncMock(return_value=True),
                        touch_member_activity=AsyncMock(), aget_guild_config=AsyncMock(return_value={}),
                        _cfg_get=lambda *a, default=None: default,
                        _safe_int=lambda n, default=0: int(n),
                        calculate_level=lambda xp: 1, get_level_progress_percent=lambda xp: 0,
                        get_channel_from_cfg=AsyncMock(return_value=None),
                        random=SimpleNamespace(randint=lambda *args: 10),
                        update_role_and_nick=AsyncMock(), maybe_award_level100=AsyncMock(),
                        LEVELUP_ANNOUNCE_CHANNEL=1, LOG_CHANNEL_ID=2, SEASON_MAX_LEVEL=100,
                        AFK_CHANNEL_IDS=[99], SPECIAL_VC_CATEGORY_IDS=[], VOICE_MIN_XP=10, VOICE_MAX_XP=50,
                        MISSION_REQUIRED_MESSAGES=30, MISSION_EXP_REWARD=300, COOLDOWN_SECONDS=5,
                        REPEAT_VC_MIN_PEOPLE=5, REPEAT_VC_REQUIRED_MINUTES=15, REPEAT_VC_EXP_REWARD=150,
                        aget_effective_season_state=AsyncMock(return_value={}), completion_embed=lambda *a: discord.Embed(),
                        strip_title_suffix=lambda name: name, ALLOW_NO_PING=discord.AllowedMentions.none(),
                        _is_bot_message=lambda m: False, _is_low_value_context=lambda m: False,
                        _hit_cooldowns=lambda m: None, onboarding_service=None,
                        THREAD_ROLE_CHANNEL_ID=999, THREAD_ROLE_ID=888)
        self.member = SimpleNamespace(id=42, bot=False, display_name='테스트')
        self.channel = SimpleNamespace(id=1, send=AsyncMock(), members=[self.member], category=None)
        self.guild = SimpleNamespace(id=1, voice_channels=[self.channel], stage_channels=[])
        self.env['bot'] = SimpleNamespace(guilds=[self.guild])
        self.message = SimpleNamespace(content='대화', guild=self.guild, channel=self.channel, author=self.member)
        functions(self.env, 'on_message', 'voice_xp_task', 'repeat_vc_mission_task')

    async def test_preseason_chat_records_activity_but_no_xp_or_mission(self):
        self.env['aseason_xp_enabled'].return_value = False
        await self.env['on_message'](self.message)
        self.env['touch_member_activity'].assert_awaited_once_with('42')
        self.assertEqual(self.writes, [])
        self.env['aget_user_exp'].assert_not_awaited()

    async def test_preseason_voice_records_humans_excludes_bots_and_afk(self):
        self.env['aseason_xp_enabled'].return_value = False
        self.channel.members.append(SimpleNamespace(id=43, bot=True))
        self.guild.voice_channels.append(SimpleNamespace(id=99, members=[SimpleNamespace(id=44, bot=False)]))
        await self.env['voice_xp_task']()
        self.env['touch_member_activity'].assert_awaited_once_with('42')
        self.env['asave_user_exp'].assert_not_awaited()

    async def test_regular_voice_still_awards_existing_xp(self):
        await self.env['voice_xp_task']()
        record = self.env['asave_user_exp'].await_args.args[1]
        self.assertEqual(record['exp'], 10)
        self.assertEqual(record['voice_minutes'], 1)

    async def test_concurrent_chat_completes_once(self):
        await asyncio.gather(self.env['on_message'](self.message), self.env['on_message'](self.message))
        self.assertEqual(self.xp['exp'], 310)
        self.assertTrue(self.mission['text']['completed'])
        self.assertEqual(self.mission['text']['count'], 30)

    async def test_voice_waiting_across_midnight_uses_new_day_under_lock(self):
        self.env['REPEAT_VC_MIN_PEOPLE'] = 1
        await self.lock.acquire()
        task = asyncio.create_task(self.env['repeat_vc_mission_task']())
        await asyncio.sleep(0)
        self.clock.value = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
        self.lock.release()
        await task
        self.assertEqual(self.mission['date'], '2026-10-09')
        self.assertEqual(self.mission['repeat_vc']['minutes'], 1)
        self.assertEqual(self.mission['text']['count'], 0)
        self.assertEqual(self.xp['exp'], 0)

    async def test_chat_and_voice_keep_both_updates_on_new_day(self):
        self.env['REPEAT_VC_MIN_PEOPLE'] = 1
        self.clock.value = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
        await asyncio.gather(self.env['on_message'](self.message), self.env['repeat_vc_mission_task']())
        self.assertEqual(self.mission['text']['count'], 1)
        self.assertEqual(self.mission['repeat_vc']['minutes'], 1)
        self.assertFalse(self.mission['text']['completed'])
        self.assertEqual(self.xp['exp'], 10)

    async def test_voice_reward_still_150(self):
        self.env['REPEAT_VC_MIN_PEOPLE'] = 1
        await self.env['repeat_vc_mission_task']()
        self.assertEqual(self.xp['exp'], 150)
        self.assertEqual(self.mission['repeat_vc']['minutes'], 15)

    async def test_touch_is_partial_throttled_and_failed_write_retries(self):
        env = functions(dict(self.env, _ACTIVITY_TOUCH_TS={}), 'touch_member_activity')
        env['afirebase_root_update_strict'] = AsyncMock(side_effect=[RuntimeError('database'), None, None])
        with self.assertRaises(RuntimeError):
            await env['touch_member_activity']('42')
        await env['touch_member_activity']('42')
        await env['touch_member_activity']('42')
        self.assertEqual(env['afirebase_root_update_strict'].await_count, 2)
        self.assertEqual(list(env['afirebase_root_update_strict'].await_args.args[0]), ['exp_data/42/last_activity'])
