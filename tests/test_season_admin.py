import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from discord.ext import commands
from season_admin import install, SeasonPanel, SeasonInput

class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_preserves_public_commands(self):
        bot=commands.Bot(command_prefix='!',intents=discord.Intents.none())
        async def callback(i:discord.Interaction):pass
        for name in ('현재시즌초기화','다음시즌준비','시즌보상설정','시즌정보','활동','정산재개'):
            bot.tree.add_command(discord.app_commands.Command(name=name,description=name,callback=callback))
        install({'bot':bot})
        self.assertEqual({c.name for c in bot.tree.get_commands()},{'시즌관리','시즌정보','활동','정산재개'})
        command=bot.tree.get_command('시즌관리')
        self.assertTrue(command.default_permissions.administrator)
        self.assertTrue(command.guild_only)
        await bot.close()
    async def test_panel_blocks_unavailable_operations(self):
        panel=SeasonPanel({},42,{'first_season_started':True,'status':'regular','settled':False})
        self.assertTrue(panel.settle.disabled);self.assertTrue(panel.prepare.disabled)
        panel=SeasonPanel({},42,{'first_season_started':True,'status':'preseason','settled':True})
        self.assertTrue(panel.settle.disabled);self.assertFalse(panel.prepare.disabled)
    async def test_panel_owner_and_permission_enforced(self):
        panel=SeasonPanel({},42,{})
        for uid,admin in ((43,True),(42,False)):
            i=SimpleNamespace(guild=object(),user=SimpleNamespace(id=uid,guild_permissions=SimpleNamespace(administrator=admin)),response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await panel.interaction_check(i))
    async def test_modal_reuses_existing_callback(self):
        callback=AsyncMock();g={'season_reward_set':SimpleNamespace(callback=callback)}
        modal=SeasonInput(g,42,True);modal.name._value='겨울';modal.description._value='기록'
        i=SimpleNamespace(guild=object(),user=SimpleNamespace(id=42,guild_permissions=SimpleNamespace(administrator=True)))
        await modal.on_submit(i)
        callback.assert_awaited_once_with(i,칭호명='겨울',설명='기록')
    async def test_settle_uses_existing_confirmation(self):
        callback=AsyncMock();panel=SeasonPanel({'current_season_reset':SimpleNamespace(callback=callback)},42,{})
        i=object();await panel.settle.callback(i);callback.assert_awaited_once_with(i)
