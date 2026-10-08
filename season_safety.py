"""Single-process settlement barrier and bounded resumable settlement journal."""
import asyncio
import copy
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import discord
from runtime_safety import failure_message, mark_operation


class SeasonBusy(RuntimeError):
    def __init__(self):
        super().__init__('시즌 정산 중입니다. 잠시 후 다시 시도해주세요.')


class MutationGate:
    def __init__(self):
        self.owner = None
        self.count = 0
        self.idle = asyncio.Event()
        self.idle.set()

    @asynccontextmanager
    async def member(self, lock):
        nested = self.owner is asyncio.current_task()
        if self.owner is not None and not nested:
            raise SeasonBusy()
        if not nested:
            self.count += 1
            self.idle.clear()
        try:
            async with lock:
                yield
        finally:
            if not nested:
                self.count -= 1
                if self.count == 0:
                    self.idle.set()

    @asynccontextmanager
    async def exclusive(self):
        if self.owner is asyncio.current_task():
            yield
            return
        if self.owner is not None:
            raise SeasonBusy()
        self.owner = asyncio.current_task()
        try:
            await self.idle.wait()
            yield
        finally:
            self.owner = None


def fingerprint(state, reward, data):
    # Last-activity updates must not invalidate a preview, but balances must.
    balances = {str(uid): int(r.get('exp', 0)) for uid, r in data.items() if isinstance(r, dict)}
    raw = json.dumps([state.get('current_season_id'), state.get('status'), state.get('settled'), reward, balances], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def settlement_payload(state, reward, data, missions, guild_id, actor_id, level):
    sid = state['current_season_id']
    token = uuid.uuid4().hex
    stamp = datetime.now(timezone.utc).isoformat()
    reset = copy.deepcopy(data)
    records = {}
    for uid, record in data.items():
        if not isinstance(record, dict):
            continue
        xp = max(0, int(record.get('exp', 0)))
        final_level = level(xp)
        records[uid] = {'final_exp': xp, 'final_level': final_level, 'reached_100': final_level >= 100,
                        'reward_title': reward['title_name'], 'settled_at': stamp}
        reset[uid].update(exp=0, level=1, voice_minutes=0, last_text_xp_at=0)
    job = {'token': token, 'season_id': sid, 'guild_id': str(guild_id), 'season_name': state.get('current_season_name', sid),
           'reward': reward, 'created_at': stamp, 'status': 'pending', 'steps': {}, 'core_done': False}
    return {
        'season_backup': {'token': token, 'guild_id': str(guild_id), 'season_id': sid, 'created_at': stamp,
                          'exp_data': data, 'mission_data': missions, 'season_state': state},
        'season_settlement': job, f'season_records/{sid}': records,
        'exp_data': reset or None, 'mission_data': None,
        'season_state/settled': True, 'season_state/status': 'preseason', 'season_state/next_ready': False,
        'season_state/settled_by': str(actor_id), 'season_state/settled_at': stamp,
        'season_state/settlement_version': 2, 'season_state/settlement_token': token,
        'season_state/settlement_postprocess_pending': False, 'season_state/settlement_notice_pending': False,
    }


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id, callback, label='확인 후 정산 실행'):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.run = callback
        self.used = False
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        button.callback = self.confirm
        self.add_item(button)

    async def confirm(self, interaction):
        if interaction.user.id != self.owner_id or not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message('명령을 실행한 관리자만 확인할 수 있습니다.', ephemeral=True)
        if self.used:
            return await interaction.response.send_message('이미 처리한 요청입니다.', ephemeral=True)
        self.used = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.run(interaction)
        except Exception as error:
            message = str(error) if isinstance(error, (ValueError, SeasonBusy)) else failure_message(interaction, error)
            await interaction.followup.send(f'{message}\n다시 명령어를 실행해 현재 상태를 확인해주세요.', ephemeral=True)
        finally:
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
            self.stop()


class SettlementService:
    def __init__(self, globals_):
        self.g = globals_

    async def get(self, path):
        return await asyncio.to_thread(lambda: self.g['db'].reference(path).get())

    async def update(self, values):
        await self.g['afirebase_root_update_strict'](values)

    def now(self):
        return datetime.now(timezone.utc).isoformat()

    async def preview(self, interaction):
        await interaction.response.defer(ephemeral=True)
        state = await self.g['aget_effective_season_state']()
        if state.get('settled'):
            return await interaction.followup.send('이미 정산된 시즌입니다. 남은 작업은 `/정산재개`로 처리해주세요.', ephemeral=True)
        if not state.get('first_season_started') or state.get('status') == 'regular':
            return await interaction.followup.send('시작된 시즌의 프리시즌 또는 잠금 상태에서만 정산할 수 있습니다.', ephemeral=True)
        reward = await self.g['_get_season_reward'](state['current_season_id'])
        if not reward.get('title_name'):
            return await interaction.followup.send('먼저 시즌 보상 칭호를 설정해주세요.', ephemeral=True)
        data = await self.g['aload_exp_data']()
        signature = fingerprint(state, reward, data)
        winners = [uid for uid, r in data.items() if isinstance(r, dict) and self.g['calculate_level'](r.get('exp', 0)) >= 100]
        embed = discord.Embed(title='시즌 정산 미리보기', color=0xD9A65C,
                              description=f"{state.get('current_season_name')}\n아직 데이터를 변경하지 않았습니다.")
        embed.add_field(name='초기화 대상', value=f'{sum(isinstance(r, dict) for r in data.values())}명 · 경험치/레벨/음성 시간/일일 활동', inline=False)
        embed.add_field(name='Lv.100 달성자', value=f'{len(winners)}명\n' + (' '.join(f'<@{uid}>' for uid in winners[:15]) or '없음'), inline=False)
        embed.add_field(name='보상 칭호', value=reward['title_name'], inline=False)
        embed.set_footer(text='확인 유효시간 3분 · 직전 백업 하나만 유지 · 출석 기록/보유 칭호 유지')
        async def confirm(i):
            lock = self.g['get_season_operation_lock'](i.guild.id)
            if lock.locked():
                raise SeasonBusy()
            async with lock, self.g['SEASON_GATE'].exclusive():
                await self.commit(i, signature)
        await interaction.followup.send(embed=embed, view=ConfirmView(interaction.user.id, confirm), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def commit(self, interaction, signature):
        mark_operation(interaction, '정산 재검사', 'not_saved')
        state = await self.g['aget_effective_season_state']()
        reward = await self.g['_get_season_reward'](state['current_season_id'])
        data = await self.g['aload_exp_data']()
        if state.get('settled') or state.get('status') == 'regular' or signature != fingerprint(state, reward, data):
            raise ValueError('미리보기 이후 시즌/경험치/보상 설정이 바뀌었습니다. `/현재시즌초기화`를 다시 실행해주세요.')
        ok, error = await self.g['ensure_guild_member_cache_complete'](interaction.guild)
        if not ok:
            raise ValueError(f'서버원 목록을 확인하지 못했습니다: {error}')
        missions = await self.get('mission_data') or {}
        values = settlement_payload(state, reward, data, missions, interaction.guild.id, interaction.user.id, self.g['calculate_level'])
        token = values['season_settlement']['token']
        mark_operation(interaction, '정산과 백업 저장', 'saving')
        try:
            await self.update(values)
        except Exception:
            # Never claim rollback just because the acknowledgement was lost.
            saved = await self.get('season_state') or {}
            if saved.get('settlement_token') != token:
                raise RuntimeError('정산 저장 결과를 확인하지 못했습니다. 재실행 시 저장된 상태를 먼저 확인합니다.')
        mark_operation(interaction, '정산 후처리', 'saved')
        result = await self.resume(interaction.guild)
        await interaction.followup.send(f'경험치 초기화와 직전 백업 저장을 완료했습니다.\n{result}\n남은 작업: `/정산재개` · 백업 확인: `/정산백업`', ephemeral=True)

    async def resume(self, guild):
        job = await self.get('season_settlement') or {}
        if not job or job.get('guild_id') != str(guild.id):
            return '재개할 정산 기록이 없습니다.'
        state = await self.get('season_state') or {}
        if state.get('current_season_id') != job['season_id']:
            return '이전 시즌의 후처리는 현재 시즌에서 실행하지 않습니다.'
        ok, _ = await self.g['ensure_guild_member_cache_complete'](guild)
        if not ok:
            return '서버원 목록 확인 대기 중입니다. 초기화는 반복하지 않습니다.'
        records = await self.get(f"season_records/{job['season_id']}") or {}
        steps = job.setdefault('steps', {})
        errors = []
        async def step(key, action):
            if steps.get(key) == 'done':
                return
            try:
                await action()
                steps[key] = 'done'
            except Exception as error:
                steps[key] = 'failed'
                errors.append(key)
                self.g['logging'].exception('Settlement postprocess failed step=%s token=%s', key, job['token'])
            await self.update({f'season_settlement/steps/{key}': steps[key]})

        for uid, record in records.items():
            member = guild.get_member(int(uid))
            if not member or member.bot:
                continue
            if record.get('reached_100'):
                async def award(uid=uid):
                    sid = job['season_id']
                    title_id = self.g['make_title_id'](sid)
                    existing = await self.get(f'user_titles/{uid}/owned/{title_id}') or {}
                    title = dict(existing, title_name=job['reward']['title_name'], source_season_id=sid,
                                 acquired_at=existing.get('acquired_at') or self.now(), description=job['reward'].get('description', ''))
                    await self.update({f'user_titles/{uid}/owned/{title_id}': title,
                                       f'season_completion/{sid}/{uid}/reward_given': True,
                                       f'season_completion/{sid}/{uid}/title_id': title_id,
                                       f'season_completion/{sid}/{uid}/title_name': job['reward']['title_name']})
                await step(f'award_{uid}', award)
                if steps.get(f'award_{uid}') == 'done':
                    async def dm(uid=uid, member=member):
                        completion = await self.get(f"season_completion/{job['season_id']}/{uid}") or {}
                        if not completion.get('dm_sent'):
                            await member.send(f"🎉 `{job['season_name']}` 시즌 Lv.100 보상 칭호 `[ {job['reward']['title_name']} ]`을 받았습니다. `/칭호관리`에서 확인해주세요.")
                            await self.update({f"season_completion/{job['season_id']}/{uid}/dm_sent": True})
                    await step(f'dm_{uid}', dm)
        for member in guild.members:
            if member.bot or member.id == guild.owner_id:
                continue
            async def nickname(member=member):
                data = await self.g['aget_user_exp'](str(member.id))
                titles = await self.g['aget_user_titles'](str(member.id))
                if (titles.get('equipped') or {}).get('type', 'progress') != 'progress':
                    return
                if not await self.g['update_role_and_nick'](member, self.g['calculate_level'](data.get('exp', 0))):
                    raise RuntimeError('nickname update failed')
            await step(f'nick_{member.id}', nickname)
        async def notice():
            channel = guild.get_channel(int(state.get('season_notice_channel_id') or self.g['SEASON_NOTICE_CHANNEL_ID']))
            if channel is None:
                raise ValueError('시즌 공지 채널 없음')
            await channel.send(f"📢 `{job['season_name']}` 시즌 정산이 완료되었습니다. 프리시즌 동안 시즌 경험치 획득이 중단됩니다.", allowed_mentions=discord.AllowedMentions.none())
        await step('notice', notice)
        async def log():
            cfg = await self.g['aget_guild_config'](guild.id)
            channel = await self.g['get_channel_from_cfg'](guild, cfg, 'log_channel_id', self.g['LOG_CHANNEL_ID'])
            if channel is None:
                raise ValueError('정산 로그 채널 없음')
            await channel.send(f"🧾 `{job['season_name']}` 정산 · 초기화 {len(records)}명 · Lv.100 {sum(bool(r.get('reached_100')) for r in records.values())}명 · 직전 백업 보관 중", allowed_mentions=discord.AllowedMentions.none())
        await step('log', log)
        core_done = not any(key.startswith(('award_', 'nick_')) for key in errors)
        await self.update({'season_settlement/core_done': core_done,
                           'season_settlement/status': 'pending' if errors else 'complete',
                           'season_settlement/checked_at': self.now()})
        return f'후처리 미완료 {len(errors)}개입니다.' if errors else '칭호·닉네임·DM·공지 후처리를 완료했습니다.'

    async def auto_resume(self, guild):
        job = await self.get('season_settlement') or {}
        if job.get('guild_id') != str(guild.id) or job.get('status') != 'pending':
            return False
        async with self.g['SEASON_GATE'].exclusive():
            await self.resume(guild)
        job = await self.get('season_settlement') or {}
        return not job.get('core_done', False)
