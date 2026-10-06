"""Attendance presentation and recovery rules; no database or login side effects."""
from datetime import date
import discord

RECOVERY_COST_PER_DAY = 2000
MAX_RECOVERY_DAYS = 5


def recovery_offer(record, today, season_id):
    try:
        missed = (date.fromisoformat(today) - date.fromisoformat(record['last_date'])).days - 1
    except (ValueError, KeyError, TypeError):
        return None
    if missed < 1 or record.get('streak', 0) < 1:
        return None
    return {'date': today, 'season_id': season_id, 'missed': missed,
            'cost': missed * RECOVERY_COST_PER_DAY, 'restore_streak': record['streak'] + 1}


def apply_recovery(record, exp_record, today, season_id, calculate_level):
    offer = record.get('recovery')
    if (not offer or offer.get('date') != today or record.get('last_date') != today
            or offer.get('season_id') != season_id):
        raise ValueError('이미 복구했거나 기한이 지난 버튼입니다. /출석으로 현재 기록을 확인해주세요.')
    if not 1 <= offer.get('missed', 0) <= MAX_RECOVERY_DAYS:
        raise ValueError('연속 출석은 빠진 날이 최대 5일일 때만 복구할 수 있어요.')
    balance = max(0, int(exp_record.get('exp', 0)))
    cost = offer['cost']
    if balance < cost:
        raise ValueError(f'복구에 {cost:,} XP가 필요합니다. 현재 {balance:,} XP로, {cost - balance:,} XP가 부족해요.')
    updated = dict(record, streak=offer['restore_streak'], recovery=None,
                   recovered_cost=cost)
    xp = dict(exp_record, exp=balance - cost, level=calculate_level(balance - cost))
    return updated, xp


def attendance_embed(member, record, exp, progress, max_level, *, already=False):
    level, current, needed, pct = progress(exp)
    embed = discord.Embed(title='오늘의 출석' if already else '출석 완료',
                          description='오늘의 출석을 기록했어요.', color=0x80B99A)
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='출석 기록', value=f"누적 {record['total_days']:,}일 · 연속 {record['streak']:,}일", inline=False)
    gain = record.get('daily_gain')
    reward = '기존 출석 기록입니다.' if gain is None else (f'+{gain:,} XP' if gain else '경험치 지급 기간이 아닙니다.')
    embed.add_field(name='오늘 받은 경험치', value=reward, inline=False)
    filled = min(10, max(0, int(pct * 10)))
    bar = '▰' * filled + '▱' * (10 - filled)
    detail = '최고 레벨 달성' if level >= max_level else f'{bar} {current:,} / {needed:,} XP\n다음 레벨까지 {needed - current:,} XP'
    embed.add_field(name=f'현재 레벨 · Lv.{level}', value=f'{detail}\n보유 경험치 {exp:,} XP', inline=False)
    offer = record.get('recovery')
    if offer and offer['missed'] > MAX_RECOVERY_DAYS:
        embed.add_field(name='연속 출석 안내', inline=False,
                        value=f"빠진 날이 {offer['missed']}일이라 복구할 수 없어요.\n복구는 최대 5일까지 가능합니다. 오늘부터 새롭게 이어가요.")
    elif offer:
        embed.add_field(name='연속 출석 복구', inline=False,
                        value=f"빠진 날 {offer['missed']}일 · 비용 {offer['cost']:,} XP\n복구 후 연속 {offer['restore_streak']}일\n경험치를 사용하면 레벨이 내려갈 수 있어요.\n오늘 자정 전까지 복구할 수 있어요. 누락한 날의 보상은 지급되지 않습니다.")
    elif record.get('recovered_cost'):
        embed.add_field(name='연속 출석 복구 완료', value=f"{record['recovered_cost']:,} XP를 사용했어요.", inline=False)
    embed.set_footer(text='다음 출석은 한국 시간 자정부터 가능합니다.')
    return embed


def recovery_view(uid, record):
    offer = record.get('recovery')
    if not offer or not 1 <= offer.get('missed', 0) <= MAX_RECOVERY_DAYS:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=f"{offer['cost']:,} XP로 복구하기",
                  style=discord.ButtonStyle.secondary,
                  custom_id=f"attendance:recover:{uid}:{offer['date']}"))
    return view
