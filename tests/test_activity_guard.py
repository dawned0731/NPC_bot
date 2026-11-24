import unittest
from activity_guard import inactive_candidate

class ActivityGuardTests(unittest.TestCase):
    def test_old_activity_is_eligible(self):
        self.assertTrue(inactive_candidate(100, 200))
    def test_missing_or_invalid_never_kicks(self):
        for value in (None, 0, "bad", float("nan"), float("inf")):
            self.assertFalse(inactive_candidate(value, 200))
    def test_recent_evidence_protects_member(self):
        for extra in ({"recent_activity": 201}, {"joined_at": 201}, {"in_voice": True}):
            self.assertFalse(inactive_candidate(100, 200, **extra))
    def test_boundary_is_not_inactive(self):
        self.assertFalse(inactive_candidate(200, 200))


class InactiveTaskTests(unittest.IsolatedAsyncioTestCase):
    async def run_task(self, *, recent=0, joined=None, voice=None, refresh=None):
        import ast
        import logging
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import discord
        now=datetime.now(timezone.utc)
        old=(now-timedelta(days=40)).timestamp()
        member=SimpleNamespace(id=42,bot=False,roles=[],display_name='member',joined_at=joined,voice=voice,send=AsyncMock(),kick=AsyncMock())
        channel=SimpleNamespace(send=AsyncMock())
        guild=SimpleNamespace(id=1,owner_id=1,members=[member])
        read=AsyncMock(side_effect=[{'last_activity':old}, {'last_activity':old if refresh is None else refresh}])
        env=dict(INACTIVE_AUTO_KICK_ENABLED=True,INACTIVE_KICK_DAYS=30,datetime=datetime,timedelta=timedelta,KST=timezone.utc,bot=SimpleNamespace(guilds=[guild]),logging=logging,aget_guild_config=AsyncMock(return_value={}),get_channel_from_cfg=AsyncMock(return_value=channel),INACTIVE_LOG_CHANNEL_ID=1,EXEMPT_ROLE_IDS=[],aget_user_exp=read,_safe_float=lambda value,default:float(value),discord=discord,inactive_candidate=inactive_candidate,_ACTIVITY_OBSERVED_TS={'42':recent})
        node=next(n for n in ast.parse(Path('main.py').read_text(encoding='utf-8')).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='inactive_user_log_task');node.decorator_list=[]
        exec(compile(ast.Module(body=[node],type_ignores=[]),'main.py','exec'),env)
        await env['inactive_user_log_task']()
        return member
    async def test_recent_local_activity_does_not_send_false_warning(self):
        import time
        member=await self.run_task(recent=time.time())
        member.send.assert_not_awaited();member.kick.assert_not_awaited()
    async def test_voice_member_does_not_receive_warning(self):
        from types import SimpleNamespace
        member=await self.run_task(voice=SimpleNamespace(channel=object()))
        member.send.assert_not_awaited();member.kick.assert_not_awaited()
    async def test_activity_during_dm_prevents_kick(self):
        import time
        member=await self.run_task(refresh=time.time())
        member.send.assert_awaited_once();member.kick.assert_not_awaited()
    async def test_inactive_member_still_processed(self):
        member=await self.run_task()
        member.kick.assert_awaited_once()
