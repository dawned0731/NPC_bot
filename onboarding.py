"""Private-thread admission. Configuration and sessions are isolated per guild.

Component IDs are dispatched through on_interaction, so old messages still work
after a process restart without rebuilding an in-memory View registry.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from firebase_admin import db

LOG = logging.getLogger(__name__)
NO_PING = discord.AllowedMentions.none()
QUESTIONS = {"성별": "gender", "출생연도": "year", "관심사": "interests"}
LABELS = {"gender": "성별을 선택해주세요", "year": "출생연도를 선택해주세요",
          "interests": "관심사를 하나 이상 선택해주세요 (복수 선택 가능)"}
TERMINAL = {"done", "rejected"}


def default_config():
    return {"enabled": False, "lobby_id": 0, "member_role_id": 0, "log_id": 0,
            "questions": {
                "gender": {"male": {"label": "남자", "role_id": 0},
                           "female": {"label": "여자", "role_id": 0}},
                "year": {str(y): {"label": f"{str(y)[2:]}년생", "role_id": 0}
                         for y in range(2007, 1989, -1)},
                "interests": {}}, "introductions": {}}


def option_items(config, question):
    options = config["questions"].get(question) or {}
    return sorted(options.items(), key=lambda item: item[1]["label"], reverse=question == "year")


def managed_ids(config):
    return {int(item["role_id"]) for q in config["questions"].values()
            for item in (q or {}).values() if item.get("role_id")} | {int(config["member_role_id"])}


def desired_ids(config, answers, stage):
    if stage == "rejected":
        return set()
    result = set()
    for question, values in answers.items():
        for value in values:
            result.add(int(config["questions"][question][value]["role_id"]))
    if stage in {"tour", "done"}:
        if set(answers) != set(LABELS) or not all(answers.values()):
            raise ValueError("모든 질문을 완료해야 기본 역할을 지급할 수 있습니다.")
        result.add(int(config["member_role_id"]))
    return result


def choose(session, action, values):
    """Validate untrusted component values before constructing a transition."""
    stage = session["stage"]
    answers = copy.deepcopy(session.get("answers") or {})
    if action != stage:
        raise ValueError("이미 지난 단계입니다. 대기 채널의 이어하기 버튼을 사용해주세요.")
    if stage in LABELS:
        if not values or len(values) != len(set(values)):
            raise ValueError("선택지를 확인해주세요.")
        if stage != "interests" and len(values) != 1:
            raise ValueError("하나만 선택해주세요.")
        if stage == "year" and values == ["outside"]:
            return answers, "reject_confirm"
        valid = session["config"]["questions"][stage]
        if any(v not in valid for v in values):
            raise ValueError("등록되지 않은 선택지입니다.")
        answers[stage] = values
        return answers, {"gender": "year", "year": "interests", "interests": "tour"}[stage]
    if stage == "reject_confirm":
        if values == ["back"]:
            return answers, "year"
        if values == ["confirm"]:
            return {}, "rejected"
    raise ValueError("지원하지 않는 요청입니다.")


class FirebaseStore:
    async def get(self, path):
        return await asyncio.to_thread(lambda: db.reference("onboarding").child(path).get())

    async def put(self, path, value):
        await asyncio.to_thread(lambda: db.reference("onboarding").child(path).set(value))


class Onboarding:
    def __init__(self, bot, initialize_member, store=None):
        self.bot = bot
        self.initialize_member = initialize_member
        self.store = store or FirebaseStore()
        self.locks = {}

    def lock(self, guild_id, user_id):
        return self.locks.setdefault((guild_id, user_id), asyncio.Lock())

    async def config(self, guild_id):
        raw = await self.store.get(f"config/{guild_id}")
        if not raw:
            return default_config()
        config = {**default_config(), **raw}
        # Firebase drops empty dictionaries; do not resurrect deleted options.
        config["questions"] = raw.get("questions") or {}
        for key in LABELS:
            config["questions"].setdefault(key, {})
        config["introductions"] = raw.get("introductions") or {}
        return config

    async def enabled(self, guild_id):
        return bool((await self.config(guild_id)).get("enabled"))

    async def save_config(self, guild_id, config):
        await self.store.put(f"config/{guild_id}", config)

    async def session(self, guild_id, user_id):
        return await self.store.get(f"sessions/{guild_id}/{user_id}")

    async def save(self, member, session):
        session["updated_at"] = int(time.time())
        await self.store.put(f"sessions/{member.guild.id}/{member.id}", session)

    def role(self, guild, role_id):
        role = guild.get_role(int(role_id))
        if not role or role.is_default() or role.managed or role >= guild.me.top_role:
            raise ValueError(f"지급 가능한 역할이 아닙니다: {role_id}. 봇 역할을 더 위에 배치해주세요.")
        # Admission must never grant moderation or server-management powers.
        dangerous = ("administrator", "manage_guild", "manage_roles", "manage_channels",
                     "kick_members", "ban_members", "moderate_members", "manage_webhooks",
                     "manage_threads", "manage_messages", "mention_everyone")
        if any(getattr(role.permissions, name, False) for name in dangerous):
            raise ValueError(f"관리 권한이 있는 역할은 입장 선택지에 사용할 수 없습니다: {role.name}")
        return role

    def validate(self, guild, config):
        lobby = guild.get_channel(int(config.get("lobby_id", 0)))
        if not isinstance(lobby, discord.TextChannel) or lobby.type != discord.ChannelType.text:
            raise ValueError("대기 채널은 일반 텍스트 채널로 지정해주세요.")
        if guild.premium_tier < 2:
            raise ValueError("비공개 스레드를 사용하려면 서버 부스트 2레벨 이상이 필요합니다.")
        perms = lobby.permissions_for(guild.me)
        required = ("view_channel", "send_messages", "read_message_history",
                    "create_private_threads", "send_messages_in_threads", "manage_threads")
        if not all(getattr(perms, key) for key in required) or not guild.me.guild_permissions.manage_roles:
            raise ValueError("봇의 역할 관리 및 대기 채널의 메시지·비공개 스레드 생성/관리 권한을 확인해주세요.")
        everyone = lobby.permissions_for(guild.default_role)
        if not everyone.view_channel or not everyone.read_message_history:
            raise ValueError("신입(@everyone)이 대기 채널과 메시지 기록을 볼 수 있어야 합니다.")
        role_ids = [int(config.get("member_role_id", 0))]
        for key in LABELS:
            items = option_items(config, key)
            limit = 24 if key == "year" else 25
            if not 1 <= len(items) <= limit:
                raise ValueError(f"{key}: 선택지를 1~{limit}개 설정해주세요.")
            role_ids.extend(int(item.get("role_id", 0)) for _, item in items)
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("각 선택지와 기본 역할은 서로 다른 역할에 연결해야 합니다.")
        for role_id in role_ids:
            role = self.role(guild, role_id)
            overwrite = lobby.overwrites_for(role)
            if overwrite.view_channel is False or overwrite.read_message_history is False:
                raise ValueError(f"{role.name} 역할이 대기 채널 접근을 막습니다. 안내 완료까지 접근을 유지해주세요.")
        for channel in guild.channels:
            if channel.id != lobby.id and channel.permissions_for(guild.default_role).view_channel:
                raise ValueError(f"@everyone에게 다른 채널이 보입니다: {channel.name}. 입장 권한부터 설정해주세요.")
            if channel.id != lobby.id:
                for role_id in role_ids[1:]:
                    role = guild.get_role(role_id)
                    if channel.overwrites_for(role).view_channel is True:
                        raise ValueError(f"{role.name} 역할이 {channel.name}을 엽니다. 채널 열기는 기본 역할에만 허용해주세요.")
        intros = config.get("introductions") or {}
        if set(intros) != {"slot1", "slot2", "slot3", "slot4"}:
            raise ValueError("소개할 채널을 1~4번 모두 설정해주세요.")
        for item in intros.values():
            channel = guild.get_channel(int(item["channel_id"]))
            if not channel or not item.get("description"):
                raise ValueError("소개 채널 또는 설명이 없습니다.")
            if channel.overwrites_for(guild.get_role(role_ids[0])).view_channel is not True:
                raise ValueError(f"기본 역할에 {channel.name} 채널 보기 권한을 명시적으로 허용해주세요.")
        log = guild.get_channel(int(config.get("log_id", 0)))
        if not isinstance(log, discord.TextChannel) or not log.permissions_for(guild.me).send_messages:
            raise ValueError("봇이 메시지를 보낼 수 있는 비공개 기록 채널을 설정해주세요.")
        # Ordinary roles must not reveal the answers in the log channel.
        if log.permissions_for(guild.default_role).view_channel:
            raise ValueError("입장 기록 채널을 @everyone에게 숨겨주세요.")
        for role in guild.roles:
            if not role.permissions.administrator and log.overwrites_for(role).view_channel is True:
                if role not in guild.me.roles:
                    raise ValueError("기록 채널은 서버장과 봇 전용으로 설정해주세요.")
        for target, overwrite in log.overwrites.items():
            if isinstance(target, discord.Member) and target.id not in {guild.owner_id, guild.me.id}:
                if overwrite.view_channel is True:
                    raise ValueError("기록 채널의 다른 회원 전용 보기 허용을 제거해주세요.")

    async def audit(self, guild, config, content):
        channel = guild.get_channel(int(config.get("log_id", 0)))
        if channel:
            try:
                await channel.send(content[:1900], allowed_mentions=NO_PING)
            except discord.HTTPException:
                LOG.exception("Could not send admission audit guild=%s", guild.id)

    async def sync_roles(self, member, session, answers, stage):
        config = session["config"]
        desired = desired_ids(config, answers, stage)
        # Fetch fresh membership for retries and concurrent gateway events.
        member = await member.guild.fetch_member(member.id)
        if not member.guild.me.guild_permissions.manage_roles:
            raise ValueError("봇에게 역할 관리 권한이 없습니다.")
        additions = [self.role(member.guild, rid) for rid in desired]
        current = {r.id: r for r in member.roles}
        removals = [r for rid, r in current.items() if rid in managed_ids(config) and rid not in desired]
        # Remove the access role first when rejecting/resetting.
        removals.sort(key=lambda r: r.id != int(config["member_role_id"]))
        for role in removals:
            if role.managed or role >= member.guild.me.top_role:
                raise ValueError(f"회수할 역할의 위치를 확인해주세요: {role.name}")
            await member.remove_roles(role, reason="입장 안내 선택 변경/회수")
        gate = int(config["member_role_id"])
        for role in sorted(additions, key=lambda r: r.id == gate):
            if role.id == gate and stage in {"tour", "done"}:
                await self.initialize_member(member)
            if role.id not in current:
                await member.add_roles(role, reason="입장 안내 완료" if role.id == gate else "입장 안내 선택")
        actual = await member.guild.fetch_member(member.id)
        if ({r.id for r in actual.roles} & managed_ids(config)) != desired:
            raise ValueError("역할 변경 확인에 실패했습니다. 이어하기로 다시 시도해주세요.")

    async def apply_pending(self, member, session):
        pending = session.get("pending")
        if not pending:
            return
        answers = pending.get("answers") or {}
        try:
            await self.sync_roles(member, session, answers, pending["stage"])
        except ValueError as error:
            await self.audit(member.guild, session["config"], f"입장 역할 처리 실패: {member.id}\n{error}")
            raise
        session.update(answers=answers, stage=pending["stage"], pending=None,
                       revision=session["revision"] + 1)
        await self.save(member, session)
        if session["stage"] in {"tour", "rejected"}:
            details = []
            for key, values in (session.get("answers") or {}).items():
                details.append(f"{key}: " + ", ".join(session["config"]["questions"][key][v]["label"] for v in values))
            await self.audit(member.guild, session["config"],
                             f"입장 {'완료' if session['stage'] == 'tour' else '제한'}: {member.id}\n" + "\n".join(details))

    def view(self, member_id, session):
        view = discord.ui.View(timeout=None)
        stage = session["stage"]
        prefix = f"npcob:{member_id}:{session['revision']}:"
        if stage in LABELS:
            options = [discord.SelectOption(label=item["label"], value=key)
                       for key, item in option_items(session["config"], stage)]
            if stage == "year":
                options.append(discord.SelectOption(label="그 외 나이", value="outside"))
            view.add_item(discord.ui.Select(custom_id=prefix + stage, placeholder=LABELS[stage],
                                           options=options, min_values=1,
                                           max_values=len(options) if stage == "interests" else 1))
        elif stage == "reject_confirm":
            view.add_item(discord.ui.Button(label="다시 선택", custom_id=prefix + "back"))
            view.add_item(discord.ui.Button(label="확인", style=discord.ButtonStyle.danger,
                                           custom_id=prefix + "confirm"))
        elif stage == "tour":
            view.add_item(discord.ui.Button(label="안내 완료" if session.get("tour_index", 1) == 4 else "다음 채널",
                                           custom_id=prefix + "next", style=discord.ButtonStyle.primary))
        return view

    async def render(self, member, session):
        thread = self.bot.get_channel(session["thread_id"]) or await self.bot.fetch_channel(session["thread_id"])
        if not isinstance(thread, discord.Thread) or thread.type != discord.ChannelType.private_thread:
            raise ValueError("안내 스레드를 확인해주세요.")
        if thread.archived or thread.locked:
            await thread.edit(archived=False, locked=False)
        stage = session["stage"]
        content = LABELS.get(stage, "")
        if stage == "gender":
            content = "환영합니다! 성별 → 출생연도 → 관심사 선택을 마치면 서버 채널을 이용할 수 있어요.\n\n" + content
        elif stage == "reject_confirm":
            content = "허용 출생연도에 해당하지 않으면 입장할 수 없습니다. 확인하면 입장 절차에서 관리하는 역할을 회수합니다. 잘못 눌렀다면 다시 선택해주세요."
        elif stage == "rejected":
            content = "입장 가능한 출생연도 범위에 해당하지 않아 입장이 제한되었습니다. 입장 관련 역할을 회수했습니다. 잘못 선택했다면 서버장에게 문의해주세요."
        elif stage == "tour":
            index = str(session.get("tour_index", 1))
            intro = session["config"]["introductions"][f"slot{index}"]
            content = f"입장 준비가 끝났어요! 주요 채널을 소개합니다. ({index}/4)\n\n<#{intro['channel_id']}>\n{intro['description']}"
        elif stage == "done":
            content = "안내가 끝났습니다. 즐거운 서버 생활 되세요!\n\n" + "\n".join(
                f"<#{v['channel_id']}> — {v['description']}" for _, v in sorted(session["config"]["introductions"].items()))
        message = None
        if session.get("message_id"):
            try:
                message = await thread.fetch_message(session["message_id"])
            except discord.NotFound:
                pass
        view = self.view(member.id, session) if stage not in TERMINAL else None
        if message:
            await message.edit(content=content, view=view, allowed_mentions=NO_PING)
        else:
            message = await thread.send(content, view=view, allowed_mentions=NO_PING)
            session["message_id"] = message.id
            await self.save(member, session)
        if stage in TERMINAL:
            await thread.edit(locked=True, archived=True)
        return thread

    async def start(self, member):
        if member.bot:
            return None
        async with self.lock(member.guild.id, member.id):
            config = await self.config(member.guild.id)
            if not config["enabled"]:
                raise ValueError("현재 봇 입장 안내가 꺼져 있습니다.")
            session = await self.session(member.guild.id, member.id)
            joined_at = member.joined_at.isoformat() if member.joined_at else ""
            if session and session.get("joined_at", "") != joined_at:
                # Also handles a rejoin while the bot was offline.
                if session.get("thread_id"):
                    try:
                        old_thread = await self.bot.fetch_channel(session["thread_id"])
                        await old_thread.edit(locked=True, archived=True)
                    except discord.NotFound:
                        pass
                await self.store.put(f"sessions/{member.guild.id}/{member.id}", None)
                session = None
            if session and session["stage"] in TERMINAL:
                if session.get("pending"):
                    await self.apply_pending(member, session)
                # Retry final message/archive if the previous Discord call failed.
                return await self.render(member, session)
            if not session:
                if any(r.id == int(config["member_role_id"]) for r in member.roles):
                    raise ValueError("이미 기본 역할을 가진 회원입니다.")
                self.validate(member.guild, config)
                session = {"stage": "gender", "answers": {}, "revision": int(time.time() * 1000),
                           "config": copy.deepcopy(config), "thread_id": 0, "message_id": 0,
                           "tour_index": 1, "joined_at": joined_at}
                await self.save(member, session)
            thread = None
            if session.get("thread_id"):
                try:
                    thread = self.bot.get_channel(session["thread_id"]) or await self.bot.fetch_channel(session["thread_id"])
                except discord.NotFound:
                    pass
            if thread is None:
                lobby = member.guild.get_channel(int(session["config"]["lobby_id"]))
                if not isinstance(lobby, discord.TextChannel):
                    raise ValueError("대기 채널이 없습니다. 관리자에게 문의해주세요.")
                thread = await lobby.create_thread(name=f"입장 안내-{member.id}",
                                                   type=discord.ChannelType.private_thread,
                                                   invitable=False, auto_archive_duration=1440)
                session.update(thread_id=thread.id, message_id=0)
                await self.save(member, session)
            if thread.archived or thread.locked:
                await thread.edit(archived=False, locked=False)
            await thread.add_user(member)
            await thread.add_user(discord.Object(id=member.guild.owner_id))
            if not session.get("greeted"):
                await thread.send(f"{member.mention} 님, 이곳에서 입장 안내를 진행해주세요.",
                                  allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
                session["greeted"] = True
                await self.save(member, session)
            await self.apply_pending(member, session)
            return await self.render(member, session)

    async def on_join(self, member):
        if member.bot:
            return
        try:
            if not await self.enabled(member.guild.id):
                return
            await self.start(member)
        except Exception:
            LOG.exception("Admission start failed guild=%s member=%s", member.guild.id, member.id)
            try:
                await self.audit(member.guild, await self.config(member.guild.id),
                                 f"입장 안내 시작 실패: {member.id}. 권한 확인 후 /입장관리 이어하기를 사용해주세요.")
            except Exception:
                LOG.exception("Admission start failure could not be reported")

    async def on_interaction(self, interaction):
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        if interaction.type != discord.InteractionType.component or not custom_id.startswith("npcob:"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                raise ValueError("서버에서 사용해주세요.")
            if custom_id == "npcob:start":
                thread = await self.start(interaction.user)
                await interaction.followup.send(f"개인 입장 안내: {thread.mention}", ephemeral=True)
                return
            _, owner, revision, action = custom_id.split(":")
            if int(owner) != interaction.user.id:
                raise ValueError("이 질문은 해당 신입 본인만 답할 수 있습니다.")
            async with self.lock(interaction.guild.id, interaction.user.id):
                if not await self.enabled(interaction.guild.id):
                    raise ValueError("현재 입장 안내가 꺼져 있습니다.")
                session = await self.session(interaction.guild.id, interaction.user.id)
                if not session or interaction.channel_id != session["thread_id"]:
                    raise ValueError("현재 진행 중인 개인 스레드에서 사용해주세요.")
                if int(revision) != session["revision"] or session["stage"] in TERMINAL:
                    raise ValueError("이미 처리된 버튼입니다. 대기 채널에서 이어하기를 눌러주세요.")
                if session.get("pending"):
                    await self.apply_pending(interaction.user, session)
                elif session["stage"] == "tour" and action == "next":
                    index = session.get("tour_index", 1)
                    session.update(tour_index=min(4, index + 1), stage="done" if index == 4 else "tour",
                                   revision=session["revision"] + 1)
                    await self.save(interaction.user, session)
                else:
                    values = data.get("values") or []
                    if session["stage"] == "reject_confirm":
                        values, action = [action], "reject_confirm"
                    answers, stage = choose(session, action, values)
                    session["pending"] = {"answers": answers, "stage": stage}
                    await self.save(interaction.user, session)
                    await self.apply_pending(interaction.user, session)
                await self.render(interaction.user, session)
            await interaction.followup.send("반영했습니다. 스레드의 안내를 확인해주세요.", ephemeral=True)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            LOG.exception("Admission interaction failed")
            await interaction.followup.send("처리를 완료하지 못했습니다. 진행 내용은 저장되며, 대기 채널에서 이어하기를 눌러 다시 시도할 수 있습니다. 계속 실패하면 서버장에게 문의해주세요.", ephemeral=True)
            try:
                await self.audit(interaction.guild, await self.config(interaction.guild.id),
                                 f"입장 처리 실패: {interaction.user.id}. 봇 로그와 역할/스레드 권한을 확인해주세요.")
            except Exception:
                LOG.exception("Admission error reporting failed")

    async def excludes_message(self, message, config):
        if not config.get("enabled"):
            return False
        if message.channel.id == int(config["lobby_id"]):
            return True
        if isinstance(message.channel, discord.Thread):
            if message.channel.parent_id == int(config["lobby_id"]):
                return True
            session = await self.session(message.guild.id, message.author.id)
            return bool(session and session.get("thread_id") == message.channel.id)
        return False


class OnboardingCommands(commands.Cog):
    def __init__(self, service):
        self.service = service

    async def cog_app_command_error(self, interaction, error):
        interaction.extras["onboarding_error_handled"] = True
        original = getattr(error, "original", error)
        text = str(original) if isinstance(original, ValueError) else "설정을 처리하지 못했습니다. 관리자 권한과 봇 로그를 확인해주세요."
        if not isinstance(original, ValueError):
            LOG.error("Admission command failed: %r", original)
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="입장설정", description="개인 입장 안내의 기본 설정 및 켜기/끄기")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure(self, interaction: discord.Interaction,
                        작업: Literal["보기", "기본", "검사", "켜기", "끄기", "안내게시"],
                        대기채널: discord.TextChannel | None = None,
                        기본역할: discord.Role | None = None,
                        기록채널: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True)
        service = self.service
        async with service.lock(interaction.guild_id, "config"):
            config = await service.config(interaction.guild_id)
            result = "설정했습니다."
            if 작업 == "기본":
                if config["enabled"]:
                    raise ValueError("기본 설정 변경 전 안내를 꺼주세요. 진행 중인 신입은 이전 설정을 유지하므로 필요하면 /입장관리 재시작을 사용해주세요.")
                for key, value in (("lobby_id", 대기채널), ("member_role_id", 기본역할), ("log_id", 기록채널)):
                    if value is not None:
                        if key == "member_role_id":
                            service.role(interaction.guild, value.id)
                        config[key] = value.id
                await service.save_config(interaction.guild_id, config)
            elif 작업 in {"검사", "켜기"}:
                service.validate(interaction.guild, config)
                result = "설정과 권한 검사를 통과했습니다."
                if 작업 == "켜기":
                    config["enabled"] = True
                    await service.save_config(interaction.guild_id, config)
                    result += " 새 입장 안내를 켰습니다. /입장설정 안내게시로 이어하기 버튼을 게시해주세요."
            elif 작업 == "끄기":
                config["enabled"] = False
                await service.save_config(interaction.guild_id, config)
                result = "새 입장 안내를 껐습니다. 기존 첫 채팅 역할 지급 방식이 다시 동작합니다."
            elif 작업 == "안내게시":
                service.validate(interaction.guild, config)
                lobby = interaction.guild.get_channel(config["lobby_id"])
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(label="입장 절차 시작 / 이어하기", custom_id="npcob:start", style=discord.ButtonStyle.primary))
                await lobby.send("환영합니다! 아래 버튼을 누르면 본인의 비공개 입장 안내로 이동할 수 있습니다.", view=view)
                result = "대기 채널에 안내 버튼을 게시했습니다."
            else:
                lines = [f"입장 안내: {'켜짐' if config['enabled'] else '꺼짐'}",
                         f"대기 채널: <#{config['lobby_id']}>", f"기본 역할: <@&{config['member_role_id']}>",
                         f"기록 채널: <#{config['log_id']}>"]
                for key in LABELS:
                    items = option_items(config, key)
                    lines.append(f"{key}: {len(items)}개 (미연결 {sum(not v.get('role_id') for _, v in items)}개)")
                for pos, intro in sorted((config.get("introductions") or {}).items()):
                    lines.append(f"소개 {pos.removeprefix('slot')}: <#{intro['channel_id']}> — {intro['description']}")
                result = "\n".join(lines)
            await interaction.followup.send(result, ephemeral=True, allowed_mentions=NO_PING)

    @app_commands.command(name="입장선택지", description="선택지와 기존 서버 역할을 1대1로 연결합니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def options(self, interaction: discord.Interaction, 작업: Literal["등록", "삭제", "목록"],
                      질문: Literal["성별", "출생연도", "관심사"],
                      선택지: app_commands.Range[str, 1, 80] | None = None,
                      역할: discord.Role | None = None):
        await interaction.response.defer(ephemeral=True)
        service = self.service
        async with service.lock(interaction.guild_id, "config"):
            config = await service.config(interaction.guild_id)
            question = QUESTIONS[질문]
            options = config["questions"].get(question) or {}
            config["questions"][question] = options
            if 작업 == "목록":
                lines = [f"{item['label']} → " + (f"<@&{item['role_id']}>" if item.get("role_id") else "미연결")
                         for _, item in option_items(config, question)]
                if question == "year":
                    lines.append("그 외 나이 → 입장 제한 (고정)")
                text = "\n".join(lines) or "등록된 선택지가 없습니다."
                for offset in range(0, len(text), 1800):
                    await interaction.followup.send(text[offset:offset + 1800], ephemeral=True, allowed_mentions=NO_PING)
                return
            label = (선택지 or "").strip()
            if not label or label == "그 외 나이":
                raise ValueError("선택지 이름을 입력해주세요. '그 외 나이'는 시스템 고정 항목입니다.")
            key = next((key for key, item in options.items() if item["label"] == label), None)
            if 작업 == "삭제":
                if key is None:
                    raise ValueError("해당 선택지가 없습니다.")
                del options[key]
            else:
                if 역할 is None:
                    raise ValueError("서버에 있는 역할을 선택해주세요.")
                service.role(interaction.guild, 역할.id)
                if 역할.id == config["member_role_id"] or any(
                    item.get("role_id") == 역할.id for q, entries in config["questions"].items()
                    for k, item in (entries or {}).items() if not (q == question and k == key)
                ):
                    raise ValueError("이미 다른 선택지 또는 기본 역할에 연결된 역할입니다.")
                limit = 24 if question == "year" else 25
                if key is None and len(options) >= limit:
                    raise ValueError(f"선택지는 최대 {limit}개입니다.")
                options[key or uuid.uuid4().hex] = {"label": label, "role_id": 역할.id}
            if config["enabled"]:
                service.validate(interaction.guild, config)
            await service.save_config(interaction.guild_id, config)
            await interaction.followup.send("선택지를 저장했습니다. 새로 시작하는 신입부터 적용됩니다. 진행 중인 신입에게 적용하려면 /입장관리 재시작을 사용해주세요.", ephemeral=True)

    @app_commands.command(name="입장채널소개", description="입장 완료 후 소개할 채널 4개를 설정합니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def introduction(self, interaction: discord.Interaction, 순서: app_commands.Range[int, 1, 4],
                           채널: discord.TextChannel, 설명: app_commands.Range[str, 1, 250]):
        await interaction.response.defer(ephemeral=True)
        service = self.service
        async with service.lock(interaction.guild_id, "config"):
            config = await service.config(interaction.guild_id)
            # Non-numeric keys keep Firebase from returning this mapping as an array.
            config.setdefault("introductions", {})[f"slot{순서}"] = {"channel_id": 채널.id, "description": 설명}
            if config["enabled"]:
                service.validate(interaction.guild, config)
            await service.save_config(interaction.guild_id, config)
        await interaction.followup.send("소개를 저장했습니다. 새로 시작하는 신입부터 적용됩니다.", ephemeral=True)

    @app_commands.command(name="입장관리", description="특정 회원의 입장 안내를 이어가거나 초기화합니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def manage(self, interaction: discord.Interaction, 작업: Literal["이어하기", "재시작"], 대상: discord.Member):
        await interaction.response.defer(ephemeral=True)
        service = self.service
        if 대상.bot:
            raise ValueError("봇은 입장 안내 대상이 아닙니다.")
        if not await service.enabled(interaction.guild_id):
            raise ValueError("입장 안내를 먼저 켜주세요.")
        if 작업 == "재시작":
            async with service.lock(interaction.guild_id, 대상.id):
                old = await service.session(interaction.guild_id, 대상.id)
                if not old:
                    raise ValueError("입장 기록이 없는 회원입니다. 이어하기를 사용해주세요.")
                # Persist a reset intention; old components become unusable immediately.
                old.update(stage="rejected", revision=old["revision"] + 1,
                           pending={"answers": {}, "stage": "rejected"})
                await service.save(대상, old)
                await service.apply_pending(대상, old)
                if old.get("thread_id"):
                    try:
                        thread = await service.bot.fetch_channel(old["thread_id"])
                        await thread.edit(locked=True, archived=True)
                    except discord.NotFound:
                        pass
                await service.store.put(f"sessions/{interaction.guild_id}/{대상.id}", None)
            대상 = await interaction.guild.fetch_member(대상.id)
        thread = await service.start(대상)
        await interaction.followup.send(f"입장 안내: {thread.mention}", ephemeral=True)


async def install(bot, initialize_member):
    service = Onboarding(bot, initialize_member)
    bot.add_listener(service.on_join, "on_member_join")
    bot.add_listener(service.on_interaction, "on_interaction")
    await bot.add_cog(OnboardingCommands(service))
    return service
