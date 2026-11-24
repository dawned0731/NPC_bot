"""Owner-bound season panel reusing existing administrator callbacks."""
import discord
from discord import app_commands


class SeasonPanel(discord.ui.View):
    def __init__(self, g, owner, state):
        super().__init__(timeout=300)
        self.g, self.owner = g, owner
        self.settle.disabled = bool(state.get('settled')) or not state.get('first_season_started') or state.get('status') == 'regular'
        self.settle.label = '현재 시즌 정산 완료' if state.get('settled') else '현재 시즌 정산'
        self.prepare.disabled = not state.get('settled')

    async def interaction_check(self, i):
        if i.guild is None or i.user.id != self.owner or not i.user.guild_permissions.administrator:
            await i.response.send_message('이 패널을 실행한 관리자만 사용할 수 있습니다.', ephemeral=True)
            return False
        return True

    async def on_error(self, i, error, item):
        self.g['logging'].exception('Season panel failed', exc_info=error)
        message='처리 결과를 확인하지 못했습니다. 시즌 상태를 다시 확인해주세요.'
        if i.response.is_done():
            await i.followup.send(message, ephemeral=True)
        else:
            await i.response.send_message(message, ephemeral=True)

    @discord.ui.button(label='현재 시즌 정산', style=discord.ButtonStyle.danger)
    async def settle(self, i, button):
        # Existing preview includes a separate destructive-operation confirmation.
        await self.g['current_season_reset'].callback(i)

    @discord.ui.button(label='다음 시즌 준비', style=discord.ButtonStyle.primary)
    async def prepare(self, i, button):
        await i.response.send_modal(SeasonInput(self.g, self.owner, False))

    @discord.ui.button(label='시즌 보상 설정', style=discord.ButtonStyle.primary)
    async def reward(self, i, button):
        await i.response.send_modal(SeasonInput(self.g, self.owner, True))

    @discord.ui.button(label='시즌 상태 및 공지 확인', row=1)
    async def status(self, i, button):
        await show_panel(self.g, i)


class SeasonInput(discord.ui.Modal):
    def __init__(self, g, owner, reward):
        super().__init__(title='시즌 보상 설정' if reward else '다음 시즌 준비')
        self.g, self.owner, self.reward = g, owner, reward
        self.name=discord.ui.TextInput(label='보상 칭호명' if reward else '다음 시즌 이름', max_length=20)
        self.add_item(self.name)
        self.description=discord.ui.TextInput(label='보상 설명', style=discord.TextStyle.paragraph, required=False, max_length=300)
        if reward:
            self.add_item(self.description)

    async def on_submit(self, i):
        if i.guild is None or i.user.id != self.owner or not i.user.guild_permissions.administrator:
            return await i.response.send_message('입력창을 연 관리자만 저장할 수 있습니다.', ephemeral=True)
        if self.reward:
            await self.g['season_reward_set'].callback(i, 칭호명=self.name.value, 설명=self.description.value)
        else:
            await self.g['next_season_prepare'].callback(i, 시즌이름=self.name.value)

    async def on_error(self, i, error):
        self.g['logging'].exception('Season input failed', exc_info=error)
        if i.response.is_done():
            await i.followup.send('저장 결과를 확인하지 못했습니다. 시즌 상태를 다시 확인해주세요.', ephemeral=True)
        else:
            await i.response.send_message('입력을 처리하지 못했습니다.', ephemeral=True)


async def show_panel(g, i):
    if i.guild is None or not i.user.guild_permissions.administrator:
        return await i.response.send_message('서버 관리자만 사용할 수 있습니다.', ephemeral=True)
    await i.response.defer(ephemeral=True)
    state=await g['aget_effective_season_state']()
    sid=state.get('current_season_id','')
    cal=state.get('calendar') or g['get_calendar_season_info']()
    # Use the stored season's dates, even while the calendar has moved on.
    year,kind=sid.split('_',1)
    dates=g['_season_dates_for_type'](int(year),kind)
    next_id=state.get('next_season_id') or cal['next_season_id']
    reward=await g['_get_season_reward'](next_id)
    current_reward=await g['_get_season_reward'](sid)
    embed=discord.Embed(title='시즌 관리', color=0x87A9CF,
        description=f"**{state.get('current_season_name','미설정')}**\n{g['season_status_label'](state.get('status'))} · `{sid}`")
    embed.add_field(name='시즌 일정 · 한국 시간',value=f"정규: {dates['regular_start']} ~ {dates['regular_end']}\n프리시즌: {dates['preseason_start']} ~ {dates['preseason_end']}",inline=False)
    embed.add_field(name='현재 시즌 정산',value='완료' if state.get('settled') else '미완료')
    embed.add_field(name='현재 Lv.100 보상',value=current_reward.get('title_name') or '미설정')
    embed.add_field(name='다음 시즌 준비',value=('완료' if state.get('next_ready') else '미완료')+f" · {state.get('next_season_name') or '이름 미설정'}",inline=False)
    embed.add_field(name='다음 시즌 보상',value=reward.get('title_name') or '미설정')
    embed.add_field(name='공지 상태',value=f"시작 공지: {'전송 완료' if state.get('start_notice_sent_for')==sid else '미전송 또는 대기'}\n대기 중 시즌: {state.get('season_start_notice_pending_for') or '없음'}",inline=False)
    embed.set_footer(text='정산은 미리보기 후 별도 확인이 필요합니다. 첫 시즌 시작·정산 복구 명령은 유지됩니다.')
    await i.followup.send(embed=embed, view=SeasonPanel(g,i.user.id,state), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def install(g):
    @g['bot'].tree.command(name='시즌관리',description='시즌 정산·준비·보상과 공지 상태를 관리합니다.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def season_management(i: discord.Interaction):
        await show_panel(g,i)
    for name in ('현재시즌초기화','다음시즌준비','시즌보상설정'):
        g['bot'].tree.remove_command(name)
