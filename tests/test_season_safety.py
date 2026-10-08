import asyncio
import copy
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from season_safety import MutationGate, SeasonBusy, SettlementService, ConfirmView, fingerprint, settlement_payload


STATE = {'current_season_id': '2026_fall', 'current_season_name': '가을', 'status': 'preseason', 'first_season_started': True, 'settled': False}
REWARD = {'title_name': '가을의 기록'}
DATA = {'42': {'exp': 108900, 'level': 100, 'last_activity': 123, 'voice_minutes': 50}, '43': {'exp': 100}}
LEVEL = lambda xp: min(100, int(xp) // 1100 + 1)


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_commands_register_with_discord_library(self):
        import ast
        from pathlib import Path
        import discord
        from discord import app_commands
        from discord.ext import commands
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        names = {'current_season_reset', 'settlement_resume', 'settlement_backup', 'settlement_backup_cleanup'}
        nodes = [n for n in ast.parse(Path('main.py').read_text(encoding='utf-8')).body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
        env = {'bot': bot, 'discord': discord, 'app_commands': app_commands, 'season_operation_serialized': lambda: lambda f: f}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), env)
        self.assertEqual({c.name for c in bot.tree.get_commands()}, {'현재시즌초기화', '정산재개', '정산백업', '정산백업정리'})
        await bot.close()

    async def test_settlement_drains_writes_blocks_new_writes_and_allows_own_nested_work(self):
        gate = MutationGate()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def member():
            async with gate.member(asyncio.Lock()):
                entered.set()
                await finish.wait()
        write = asyncio.create_task(member())
        await entered.wait()
        settled = asyncio.Event()
        async def reset():
            async with gate.exclusive():
                self.assertTrue(finish.is_set())
                async with gate.member(asyncio.Lock()):
                    settled.set()
        task = asyncio.create_task(reset())
        await asyncio.sleep(0)
        with self.assertRaises(SeasonBusy):
            async with gate.member(asyncio.Lock()):
                pass
        self.assertFalse(settled.is_set())
        finish.set()
        await asyncio.gather(write, task)
        async with gate.member(asyncio.Lock()):
            self.assertEqual(gate.count, 1)

    async def test_cancelled_exclusive_releases_barrier(self):
        gate = MutationGate()
        with self.assertRaises(RuntimeError):
            async with gate.exclusive():
                raise RuntimeError()
        async with gate.member(asyncio.Lock()):
            pass


class PayloadTests(unittest.TestCase):
    def test_atomic_backup_reset_preserves_activity_and_no_attendance_mutation(self):
        values = settlement_payload(STATE, REWARD, DATA, {'42': {'date': 'today'}}, 1, 2, LEVEL)
        self.assertEqual(values['season_backup']['exp_data'], DATA)
        self.assertEqual(values['exp_data']['42']['exp'], 0)
        self.assertEqual(values['exp_data']['42']['last_activity'], 123)
        self.assertIsNone(values['mission_data'])
        self.assertNotIn('attendance_data', values)
        self.assertEqual(values['season_settlement']['token'], values['season_state/settlement_token'])
        self.assertEqual(DATA['42']['exp'], 108900)

    def test_preview_signature_checks_balance_reward_but_not_activity_stamp(self):
        expected = fingerprint(STATE, REWARD, DATA)
        data = copy.deepcopy(DATA)
        data['42']['last_activity'] = 456
        self.assertEqual(expected, fingerprint(STATE, REWARD, data))
        data['42']['exp'] -= 2000
        self.assertNotEqual(expected, fingerprint(STATE, REWARD, data))
        self.assertNotEqual(expected, fingerprint(STATE, {'title_name': '변경'}, DATA))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = {'season_state': copy.deepcopy(STATE), 'exp_data': copy.deepcopy(DATA), 'mission_data': {}}
        self.updates = []
        async def get(path):
            value = self.root
            for part in path.split('/'):
                if not isinstance(value, dict):
                    return None
                value = value.get(part)
            return copy.deepcopy(value)
        async def update(values):
            self.updates.append(copy.deepcopy(values))
            for path, value in values.items():
                parts = path.split('/')
                current = self.root
                for part in parts[:-1]:
                    current = current.setdefault(part, {})
                if value is None:
                    current.pop(parts[-1], None)
                else:
                    current[parts[-1]] = copy.deepcopy(value)
        self.get, self.update = get, update
        self.member = SimpleNamespace(id=42, bot=False, send=AsyncMock())
        self.channel = SimpleNamespace(send=AsyncMock())
        self.guild = SimpleNamespace(id=1, owner_id=99, members=[self.member], get_member=lambda uid: self.member if uid == 42 else None,
                                     get_channel=lambda uid: self.channel)
        self.g = dict(aget_effective_season_state=AsyncMock(side_effect=lambda: get('season_state')),
                      _get_season_reward=AsyncMock(return_value=REWARD), aload_exp_data=AsyncMock(side_effect=lambda: get('exp_data')),
                      ensure_guild_member_cache_complete=AsyncMock(return_value=(True, '')), calculate_level=LEVEL,
                      make_title_id=lambda sid: sid+'_lv100', logging=logging,
                      aget_user_exp=AsyncMock(side_effect=lambda uid: get('exp_data/'+uid)),
                      aget_user_titles=AsyncMock(return_value={'equipped': {'type': 'progress'}}),
                      update_role_and_nick=AsyncMock(return_value=True), SEASON_NOTICE_CHANNEL_ID=1, LOG_CHANNEL_ID=2,
                      aget_guild_config=AsyncMock(return_value={}), get_channel_from_cfg=AsyncMock(return_value=self.channel))
        # AsyncMock does not await a coroutine returned by a synchronous side effect.
        async def state(): return await get('season_state')
        async def data(): return await get('exp_data')
        async def exp(uid): return await get('exp_data/'+uid)
        self.g.update(aget_effective_season_state=state, aload_exp_data=data, aget_user_exp=exp)
        self.service = SettlementService(self.g)
        self.service.get, self.service.update = get, update
        self.interaction = SimpleNamespace(extras={}, guild=self.guild, user=SimpleNamespace(id=2), followup=SimpleNamespace(send=AsyncMock()))

    async def test_commit_once_resume_after_dm_failure_does_not_reset_or_reaward(self):
        self.member.send.side_effect = RuntimeError('DM blocked')
        with self.assertLogs(level='ERROR'):
            await self.service.commit(self.interaction, fingerprint(STATE, REWARD, DATA))
        self.assertEqual(self.root['exp_data']['42']['exp'], 0)
        self.assertEqual(self.root['season_settlement']['status'], 'pending')
        self.assertTrue(self.root['season_settlement']['core_done'])
        # Restart service, then resume only failed steps.
        self.root['exp_data']['42']['exp'] = 321
        self.member.send.side_effect = None
        replacement = SettlementService(self.g)
        replacement.get, replacement.update = self.get, self.update
        await replacement.resume(self.guild)
        self.assertEqual(self.root['exp_data']['42']['exp'], 321)
        self.assertEqual(self.root['season_settlement']['status'], 'complete')
        self.assertEqual(sum('exp_data' in values for values in self.updates), 1)
        self.g['update_role_and_nick'].assert_awaited_once()
        self.assertEqual(self.channel.send.await_count, 2)
        with self.assertRaises(ValueError):
            await replacement.commit(self.interaction, fingerprint(STATE, REWARD, DATA))

    async def test_lost_commit_ack_readback_continues_without_second_reset(self):
        async def uncertain(values):
            await self.update(values)
            if 'season_backup' in values:
                raise RuntimeError('lost ack')
        self.service.update = uncertain
        await self.service.commit(self.interaction, fingerprint(STATE, REWARD, DATA))
        self.assertEqual(sum('season_backup' in v for v in self.updates), 1)
        self.assertEqual(self.root['season_settlement']['status'], 'complete')

    async def test_stale_preview_does_not_write(self):
        self.root['exp_data']['42']['exp'] = 10
        with self.assertRaises(ValueError):
            await self.service.commit(self.interaction, fingerprint(STATE, REWARD, DATA))
        self.assertEqual(self.updates, [])

    async def test_nickname_failure_stays_pending_and_retries_without_reset(self):
        self.g['update_role_and_nick'].return_value = False
        with self.assertLogs(level='ERROR'):
            await self.service.commit(self.interaction, fingerprint(STATE, REWARD, DATA))
        self.assertFalse(self.root['season_settlement']['core_done'])
        self.g['update_role_and_nick'].return_value = True
        await self.service.resume(self.guild)
        self.assertTrue(self.root['season_settlement']['core_done'])
        self.assertEqual(sum('exp_data' in values for values in self.updates), 1)

    async def test_confirmation_owner_permissions_and_duplicate_click(self):
        run = AsyncMock()
        view = ConfirmView(2, run)
        def interaction(uid, admin=True):
            return SimpleNamespace(user=SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(administrator=admin)),
                                   response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                   followup=SimpleNamespace(send=AsyncMock()), message=SimpleNamespace(edit=AsyncMock()))
        await view.confirm(interaction(3))
        await view.confirm(interaction(2, False))
        run.assert_not_awaited()
        await view.confirm(interaction(2))
        await view.confirm(interaction(2))
        run.assert_awaited_once()
