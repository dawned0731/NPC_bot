import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from activity_ui import build_activity, completion_embed, season_key
from onboarding import MEMBER_GUIDE, SERVER_INTRO


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
