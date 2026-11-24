import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from PIL import Image
import discord
from season_notice import render,send,COLORS

class NoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_seasons_render_without_external_assets(self):
        dates=dict(regular_start='2026-09-01',regular_end='2026-11-23',preseason_start='2026-11-24',preseason_end='2026-11-30')
        for season in COLORS:
            for kind in ('regular','preseason','settled'):
                image=render(dict(current_season_type=season,current_season_id='2026_'+season,current_season_name='사계절의 이야기'*20),dates,{'title_name':'겨울의 기록'},kind)
                with Image.open(image) as png:self.assertEqual(png.size,(1200,440))
    async def test_missing_font_sends_existing_embed(self):
        channel=SimpleNamespace(send=AsyncMock());embed=discord.Embed(title='정산')
        def broken(*args):raise FileNotFoundError()
        await send(channel,embed,{}, {},{},renderer=broken)
        channel.send.assert_awaited_once()
        self.assertIs(channel.send.await_args.kwargs['embed'],embed)
        self.assertNotIn('file',channel.send.await_args.kwargs)
