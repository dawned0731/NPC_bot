import ast
import asyncio
import copy
import logging
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from attendance_ui import apply_recovery, recovery_offer, attendance_embed, recovery_view


def level(exp):
    return min(100, exp // 1100 + 1)


class RecoveryTests(unittest.TestCase):
    def record(self):
        old = {'last_date': '2026-10-03', 'streak': 20}
        return dict(last_date='2026-10-06', streak=1, total_days=21,
                    weekly={'week': 1}, monthly={'month': 1}, daily_gain=1200,
                    recovery=recovery_offer(old, '2026-10-06', 'fall'))

    def test_gap_cost_and_restore_without_backfilled_attendance(self):
        record = self.record()
        updated, xp = apply_recovery(record, {'exp': 5000}, '2026-10-06', 'fall', level)
        self.assertEqual(xp, {'exp': 1000, 'level': 1})
        self.assertEqual(updated['streak'], 21)
        for key in ('total_days', 'weekly', 'monthly', 'daily_gain'):
            self.assertEqual(updated[key], record[key])
        self.assertIsNone(updated['recovery'])
        with self.assertRaises(ValueError):
            apply_recovery(updated, xp, '2026-10-06', 'fall', level)

    def test_insufficient_balance_does_not_mutate(self):
        record = self.record()
        before = copy.deepcopy(record)
        with self.assertRaises(ValueError):
            apply_recovery(record, {'exp': 3999}, '2026-10-06', 'fall', level)
        self.assertEqual(before, record)
        self.assertEqual(apply_recovery(record, {'exp': 4000}, '2026-10-06', 'fall', level)[1]['exp'], 0)

    def test_expired_day_season_and_admin_invalidation(self):
        for day, season in [('2026-10-07', 'fall'), ('2026-10-06', 'winter')]:
            with self.assertRaises(ValueError):
                apply_recovery(self.record(), {'exp': 9000}, day, season, level)
        record = self.record()
        record['recovery'] = None
        with self.assertRaises(ValueError):
            apply_recovery(record, {'exp': 9000}, '2026-10-06', 'fall', level)

    def test_no_offer_for_first_consecutive_or_invalid_dates(self):
        for last in ['', 'invalid', '2026-10-05', '2026-10-06', '2026-10-07']:
            self.assertIsNone(recovery_offer({'last_date': last, 'streak': 20}, '2026-10-06', 'fall'))

    def test_embed(self):
        member = SimpleNamespace(display_name='테스트', display_avatar=SimpleNamespace(url='https://example.com/a.png'))
        embed = attendance_embed(member, self.record(), 5000, lambda xp: (5, 600, 1100, 600/1100), 100)
        self.assertIn('500 XP', embed.fields[2].value)
        self.assertIn('4,000 XP', embed.fields[3].value)


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_days_allowed_six_days_blocked_in_ui_and_server(self):
        for missed in (5, 6):
            record = RecoveryTests().record()
            record['recovery']['missed'] = missed
            record['recovery']['cost'] = missed * 2000
            if missed == 5:
                self.assertIsNotNone(recovery_view('42', record))
                updated, xp = apply_recovery(record, {'exp': 20000}, '2026-10-06', 'fall', level)
                self.assertEqual(xp['exp'], 10000)
            else:
                self.assertIsNone(recovery_view('42', record))
                with self.assertRaisesRegex(ValueError, '최대 5일'):
                    apply_recovery(record, {'exp': 20000}, '2026-10-06', 'fall', level)
                member = SimpleNamespace(display_name='테스트', display_avatar=SimpleNamespace(url='https://example.com/a.png'))
                embed = attendance_embed(member, record, 20000, lambda xp: (19, 200, 1100, 0.18), 100)
                self.assertIn('복구할 수 없어요', embed.fields[3].value)

    async def test_concurrent_duplicate_clicks_charge_once_and_restart_routes(self):
        # Execute the actual listener without importing main's Firebase/login startup.
        tree = ast.parse(Path('main.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'attendance_recovery_interaction')
        node.decorator_list = []
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        record = RecoveryTests().record()
        record['last_date'] = record['recovery']['date'] = now
        xp = {'exp': 9000}
        lock = asyncio.Lock()
        writes = []

        async def save(updates):
            await asyncio.sleep(0)
            writes.append(updates)
            record.clear()
            record.update(updates['attendance_data/42'])
            xp.clear()
            xp.update(updates['exp_data/42'])

        env = dict(discord=discord, datetime=datetime, KST=timezone.utc, logging=logging,
                   get_user_state_lock=lambda uid: lock,
                   normalize_attendance_record=lambda r: r,
                   aget_attendance_user=AsyncMock(side_effect=lambda uid: copy.deepcopy(record)),
                   aget_user_exp=AsyncMock(side_effect=lambda uid: copy.deepcopy(xp)),
                   aget_effective_season_state=AsyncMock(return_value={'current_season_id': 'fall'}),
                   apply_recovery=apply_recovery, calculate_level=level,
                   afirebase_root_update_strict=save, ATTENDANCE_DB_KEY='attendance_data',
                   update_role_and_nick=AsyncMock(), attendance_embed=lambda *a: discord.Embed(),
                   get_level_progress=None, SEASON_MAX_LEVEL=100)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py', 'exec'), env)

        def interaction(uid=42):
            return SimpleNamespace(data={'custom_id': f'attendance:recover:42:{now}'},
                                   user=SimpleNamespace(id=uid), guild=object(),
                                   response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                   followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
        intruder = interaction(43)
        await env['attendance_recovery_interaction'](intruder)
        self.assertEqual(len(writes), 0)
        intruder.response.send_message.assert_awaited_once()
        first, second = interaction(), interaction()
        await asyncio.gather(env['attendance_recovery_interaction'](first), env['attendance_recovery_interaction'](second))
        self.assertEqual(len(writes), 1)
        self.assertEqual(xp['exp'], 5000)
        self.assertEqual(record['streak'], 21)
        # A raw listener uses the persisted offer; no in-memory View registration is required.
        self.assertIsNone(recovery_view('42', record))
