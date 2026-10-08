"""Seasonal activity journal presentation (no writes or rewards on viewing)."""
from pathlib import Path
import discord

COLORS = {'spring': 0xDCA3AF, 'summer': 0x59BDB9, 'fall': 0xCE945B, 'winter': 0xA8AED6}
ASSETS = Path(__file__).resolve().parent / 'assets' / 'activity'


def season_key(state):
    key = state.get('current_season_type')
    if key not in COLORS:
        key = state.get('calendar', {}).get('season_type', 'spring')
    return key if key in COLORS else 'spring'


def bar(value, goal):
    filled = min(10, max(0, int(value * 10 / goal)))
    return '▰' * filled + '▱' * (10 - filled)


def build_activity(member, name, mission, attendance, today, xp, state, progress,
                   text_goal=30, text_reward=300, voice_goal=15, voice_reward=150,
                   voice_people=5, attendance_reward=1200, max_level=100):
    season = season_key(state)
    active = state.get('status') == 'regular'
    embed = discord.Embed(title='사계절, 그 사이 · 오늘의 활동',
                          description=f'{discord.utils.escape_markdown(name)} 님의 활동 일지',
                          color=COLORS[season])
    embed.set_thumbnail(url=member.display_avatar.url)
    attended = attendance.get('last_date') == today
    if attended:
        gain = attendance.get('daily_gain')
        attendance_text = '출석 완료' + (f' · **+{gain:,} XP 지급**' if gain else '')
    else:
        attendance_text = f'아직 출석하지 않았어요. `/출석`으로 기록해보세요.\n출석 보상 **+{attendance_reward:,} XP**'
    embed.add_field(name='부지런하게 출석하기', value=attendance_text, inline=False)
    text = mission.get('text') or {}
    count = max(0, int(text.get('count', 0)))
    completed = bool(text.get('completed'))
    detail = '오늘의 대화를 모두 채웠어요.' if completed else f'{max(0, text_goal-count)}회 더 이야기하면 완료예요.'
    embed.add_field(name='서버원과 대화하기', inline=False,
                    value=f'채팅 {text_goal}회 · 하루 한 번\n{bar(count, text_goal)} **{min(count, text_goal)} / {text_goal}**\n{detail}\n완료 보상 **+{text_reward:,} XP**')
    minutes = max(0, int((mission.get('repeat_vc') or {}).get('minutes', 0)))
    cycles, current = divmod(minutes, voice_goal)
    embed.add_field(name='사람이 많으면 경험치도 많이', inline=False,
                    value=f'{voice_people}명 이상인 음성방에서 {voice_goal}분 참여 · 반복 가능\n{bar(current, voice_goal)} **{current} / {voice_goal}분**\n다음 보상까지 **{voice_goal-current}분** · 보상 **+{voice_reward:,} XP**\n오늘 {cycles}회 지급')
    level, current_xp, needed, _ = progress(xp)
    embed.add_field(name=f'Lv.{level}', inline=False,
                    value='최고 레벨을 달성했어요.' if level >= max_level else f'다음 레벨까지 {needed-current_xp:,} XP')
    if not active:
        embed.add_field(name='시즌 안내', value='현재는 경험치 지급 기간이 아닙니다. 위 보상은 정규 시즌 기준이며, 출석 기록은 남길 수 있어요.', inline=False)
    embed.set_footer(text='매일 자정에 활동 일지가 새로 시작됩니다. · 한국 시간')
    return embed, ASSETS / f'{season}.png'


def completion_embed(name, reward, state):
    return discord.Embed(title='서버원과 대화하기 · 완료',
                         description=f'{discord.utils.escape_markdown(name)} 님, 오늘의 대화 기록을 채웠어요.\n**+{reward:,} XP 지급** · `/활동`에서 일지를 확인해보세요.',
                         color=COLORS[season_key(state)])
