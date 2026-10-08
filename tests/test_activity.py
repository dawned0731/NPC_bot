from runtime_safety import mark_operation, failure_message
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from activity_ui import build_activity, completion_embed, season_key
from onboarding import MEMBER_GUIDE, SERVER_INTRO
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import discord
import logging


class ActivityTests(unittest.TestCase):
    def render(self, minutes=39, completed=False, active=True, season='fall', attended=True):
        member = SimpleNamespace(display_avatar=SimpleNamespace(url='https://example.com/avatar.png'))
        return build_activity(member, '디키구',
                              {'text': {'count': 30 if completed else 18, 'completed': completed},
                               'repeat_vc': {'minutes': minutes}},
                              {'last_date': '2026-10-08', 'daily_gain': 1200} if attended else {},
                              '2026-10-08', 45500,
                              {'current_season_type': season, 'status': 'regular' if active else 'preseason'},
                              lambda xp: (42, 400, 1100, 400/1100))

    def test_requested_content_and_rewards(self):
        embed, path = self.render()
        self.assertEqual(embed.title, '사계절, 그 사이 · 오늘의 활동')
        self.assertEqual(embed.description, '디키구 님의 활동 일지')
        self.assertIn('+1,200 XP', embed.fields[0].value)
        self.assertIn('12회 더', embed.fields[1].value)
        self.assertIn('+300 XP', embed.fields[1].value)
        self.assertIn('9 / 15분', embed.fields[2].value)
        self.assertIn('**6분**', embed.fields[2].value)
        self.assertIn('+150 XP', embed.fields[2].value)
        self.assertIn('오늘 2회 지급', embed.fields[2].value)
        self.assertIn('700 XP', embed.fields[3].value)

    def test_voice_boundary_starts_next_cycle(self):
        for minutes in (0, 15, 30):
            embed, _ = self.render(minutes=minutes)
            self.assertIn('0 / 15분', embed.fields[2].value)
            self.assertIn(f'오늘 {minutes//15}회 지급', embed.fields[2].value)
            self.assertIn('**15분**', embed.fields[2].value)

    def test_completed_and_inactive_season(self):
        embed, _ = self.render(completed=True, active=False, attended=False)
        self.assertIn('모두 채웠어요', embed.fields[1].value)
        self.assertIn('/출석', embed.fields[0].value)
        self.assertIn('경험치 지급 기간이 아닙니다', embed.fields[4].value)

    def test_four_banners_exist_and_season_mapping(self):
        import struct
        for season in ('spring', 'summer', 'fall', 'winter'):
            _, path = self.render(season=season)
            self.assertEqual(path.stem, season)
            data = path.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack(">II", data[16:24])
            self.assertAlmostEqual(width/height, 3, places=1)
        self.assertEqual(season_key({'calendar': {'season_type': 'winter'}}), 'winter')
        self.assertEqual(season_key({}), 'spring')

    def test_rewards_and_command_registration(self):
        tree = ast.parse(Path('main.py').read_text(encoding='utf-8'))
        values = {n.targets[0].id: n.value.value for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant)}
        self.assertEqual(values['MISSION_EXP_REWARD'], 300)
        self.assertEqual(values['REPEAT_VC_EXP_REWARD'], 150)
        commands = []
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == 'command':
                        commands.extend(k.value.value for k in decorator.keywords if k.arg == 'name')
        self.assertIn('활동', commands)
        self.assertNotIn('퀘스트', commands)
        self.assertIn('/활동', MEMBER_GUIDE)
        self.assertNotIn('/퀘스트', MEMBER_GUIDE + SERVER_INTRO)
        self.assertIn('+300 XP', completion_embed('디키구', 300, {}).description)


class ActivityCommandTests(unittest.IsolatedAsyncioTestCase):
    async def run_command(self, fail_send=False, missing=False):
        tree = ast.parse(Path('main.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'activity')
        node.decorator_list = []
        opened = []

        async def send(**kwargs):
            if missing:
                self.assertNotIn('file', kwargs)
                return
            if 'file' not in kwargs:
                self.assertEqual(fail_send, 'http')
                self.assertIsNone(kwargs['embed'].image.url)
                return
            image = kwargs['file']
            opened.append(image)
            self.assertFalse(image.fp.closed)
            self.assertEqual(image.fp.read(8), b'\x89PNG\r\n\x1a\n')
            self.assertEqual(kwargs['embed'].image.url, 'attachment://activity-banner.png')
            if fail_send == 'http':
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'attachment denied')
            if fail_send:
                raise RuntimeError('simulated network failure')

        def builder(*args, **kwargs):
            embed, path = build_activity(*args, **kwargs)
            return embed, path.with_name('missing-test-banner.png') if missing else path

        env = dict(mark_operation=mark_operation, failure_message=failure_message, discord=discord, datetime=datetime, KST=timezone.utc, logging=logging,
                   aget_user_mission=AsyncMock(return_value={}),
                   aget_attendance_user=AsyncMock(return_value={}),
                   aget_user_exp=AsyncMock(return_value={'exp': 1200}),
                   aget_effective_season_state=AsyncMock(return_value={'current_season_type': 'fall', 'status': 'regular'}),
                   strip_title_suffix=lambda name: name, build_activity=builder,
                   get_level_progress=lambda exp: (2, 100, 1100, 100/1100),
                   MISSION_REQUIRED_MESSAGES=30, MISSION_EXP_REWARD=300,
                   REPEAT_VC_REQUIRED_MINUTES=15, REPEAT_VC_EXP_REWARD=150,
                   REPEAT_VC_MIN_PEOPLE=5, ATTENDANCE_EXP_REWARD=1200, SEASON_MAX_LEVEL=100)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py', 'exec'), env)
        interaction = SimpleNamespace(extras={}, user=SimpleNamespace(id=42, display_name='테스트',
                                      display_avatar=SimpleNamespace(url='https://example.com/a.png')),
                                      response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock(side_effect=send)))
        if fail_send is True:
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                await env['activity'](interaction)
        else:
            await env['activity'](interaction)
        self.assertEqual(interaction.followup.send.await_count, 2 if fail_send == 'http' else 1)
        if not missing:
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].fp.closed)

    async def test_real_banner_file_sends_and_closes(self):
        await self.run_command()

    async def test_file_closes_when_send_fails(self):
        await self.run_command(fail_send=True)

    async def test_missing_banner_still_sends_journal(self):
        with self.assertLogs(level='WARNING'):
            await self.run_command(missing=True)

    async def test_attachment_rejected_sends_embed_without_image(self):
        with self.assertLogs(level='WARNING'):
            await self.run_command(fail_send='http')
