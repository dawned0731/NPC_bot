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
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from firebase_admin import db

LOG = logging.getLogger(__name__)
NO_PING = discord.AllowedMentions.none()
QUESTIONS = {"성별": "gender", "출생연도": "year", "관심사": "interests", "계절": "season"}
SEASONS = {"spring": "봄", "summer": "여름", "autumn": "가을", "winter": "겨울"}
LABELS = {"gender": "성별을 선택해주세요", "year": "출생연도를 선택해주세요",
          "interests": "관심사를 하나 이상 선택해주세요 (복수 선택 가능)", "season": "마음에 드는 계절을 골라주세요."}
TERMINAL = {"done", "rejected"}
KST = timezone(timedelta(hours=9))


def membership_stamp(member):
    joined = member.joined_at
    return joined.astimezone(timezone.utc).isoformat() if joined else ""


def korean_nickname(name):
    name = unicodedata.normalize("NFC", name)
    def allowed(char):
        kind = unicodedata.category(char)
        return ("HANGUL" in unicodedata.name(char, "") or char.isdecimal()
                or kind[0] in {"P", "S"} or kind == "Zs") and not (
                    kind[0] in {"L", "M"} and "HANGUL" not in unicodedata.name(char, ""))
    return bool(name.strip()) and all(allowed(char) for char in name)


def admission_log(member, success, joined_at="", reason="", returning=False, previous_joined_at=""):
    joined = member.joined_at
    if not joined and joined_at:
        try:
            joined = datetime.fromisoformat(joined_at)
        except ValueError:
            pass
    joined = joined or datetime.now(timezone.utc)
    nickname = " ".join(member.display_name.replace("/", "·").split())
    line = f"{'⭕' if success else '❌'} {member.id}/{nickname}/{joined.astimezone(KST):%Y-%m-%d %H:%M:%S}"
    if not success:
        line += " | " + " ".join((reason or "처리 오류").split())[:100]
    if success and returning:
        try:
            previous = datetime.fromisoformat(previous_joined_at).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            previous = "기록 없음"
        line += f" | 재입장 · 이전 입장: {previous}"
    return line


def thread_name(member):
    joined = member.joined_at or datetime.now(timezone.utc)
    day = joined.astimezone(KST).strftime("%Y-%m-%d")
    prefix, suffix = "입장 : ", f"/{day}"
    nickname = " ".join(member.display_name.replace("/", "·").split()) or "새 회원"
    return prefix + nickname[:100 - len(prefix) - len(suffix)] + suffix


def is_new_membership(member, session):
    current, previous = membership_stamp(member), session.get("joined_at", "")
    if session.get("left_at"):
        return True
    if current and previous:
        try:
            return datetime.fromisoformat(current) != datetime.fromisoformat(previous)
        except ValueError:
            return current != previous
    return False


def default_config():
    return {"enabled": False, "lobby_id": 0, "member_role_id": 0, "log_id": 0,
            "questions": {
                "gender": {"male": {"label": "남자", "role_id": 0},
                           "female": {"label": "여자", "role_id": 0}},
                "year": {str(y): {"label": f"{str(y)[2:]}년생", "role_id": 0}
                         for y in range(2007, 1989, -1)},
                "interests": {}, "season": {key: {"label": label, "role_id": 0}
                                              for key, label in SEASONS.items()}}, "introductions": {}}


def option_items(config, question):
    options = config["questions"].get(question) or {}
    def order(entry):
        key, item = entry
        if "order" in item:
            return (0, int(item["order"]), item["label"])
        if question == "gender":
            return (1, {"male": 0, "female": 1}.get(key, 2), item["label"])
        if question == "season":
            return (0, list(SEASONS).index(key), item["label"])
        if question == "year":
            digits = "".join(c for c in item["label"] if c.isdigit())
            year = int(digits) if digits else 0
            if len(digits) == 2:
                year += 2000 if year < 50 else 1900
            return (1, -year, item["label"])
        return (1, 0, item["label"])
    return sorted(options.items(), key=order)


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
        if stage == "done":
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
        next_stage = {"gender": "year", "year": "interests", "interests": "nickname", "season": "review"}[stage]
        if session.get("edit_return") == "review":
            next_stage = "review"
        return answers, next_stage
    if stage == "reject_confirm":
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
        config["questions"].setdefault("season", default_config()["questions"]["season"])
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
        if not role_id or str(role_id) == "0":
            raise ValueError("역할이 아직 연결되지 않았습니다. /입장역할연결에서 기존 역할을 선택해주세요.")
        role = guild.get_role(int(role_id))
        if not role:
            raise ValueError(f"설정된 역할(ID {role_id})이 서버에 없습니다. 삭제되었다면 기존 역할을 다시 연결해주세요.")
        if role.is_default():
            raise ValueError("@everyone은 자동 지급할 역할로 선택할 수 없습니다.")
        if role.managed:
            raise ValueError(f"'{role.name}'은 봇/연동 서비스가 관리하는 역할이라 직접 지급할 수 없습니다.")
        if role >= guild.me.top_role:
            raise ValueError(f"'{role.name}'이 봇의 최상위 역할 '{guild.me.top_role.name}'보다 높거나 같습니다. 이 역할보다 봇 역할을 위로 옮겨주세요.")
        # Admission must never grant moderation or server-management powers.
        dangerous = ("administrator", "manage_guild", "manage_roles", "manage_channels",
                     "kick_members", "ban_members", "moderate_members", "manage_webhooks",
                     "manage_threads", "manage_messages", "mention_everyone")
        if any(getattr(role.permissions, name, False) for name in dangerous):
            raise ValueError(f"관리 권한이 있는 역할은 입장 선택지에 사용할 수 없습니다: {role.name}")
        return role

    def inspect(self, guild, config):
        """Collect every actionable problem without stopping at the first failure."""
        errors = []
        lobby = guild.get_channel(int(config.get("lobby_id", 0)))
        if not isinstance(lobby, discord.TextChannel) or lobby.type != discord.ChannelType.text:
            errors.append("[기본 설정] 일반 텍스트 대기 채널을 /입장기본설정으로 지정해주세요.")
            lobby = None
        if guild.premium_tier < 2:
            errors.append("[서버] 비공개 스레드에는 부스트 2레벨 이상이 필요합니다.")
        if not guild.me.guild_permissions.manage_roles:
            errors.append("[봇 권한] 역할 관리 권한이 없습니다. 역할 위치와는 별도 설정입니다.")
        if lobby:
            required = {"view_channel": "채널 보기", "send_messages": "메시지 보내기",
                        "read_message_history": "메시지 기록 보기", "create_private_threads": "비공개 스레드 생성",
                        "send_messages_in_threads": "스레드에서 메시지 보내기", "manage_threads": "스레드 관리"}
            perms = lobby.permissions_for(guild.me)
            missing = [label for key, label in required.items() if not getattr(perms, key)]
            if missing:
                errors.append(f"[대기 채널] 봇에게 필요한 권한: {', '.join(missing)}")
            everyone = lobby.permissions_for(guild.default_role)
            if not everyone.view_channel or not everyone.read_message_history:
                errors.append("[대기 채널] @everyone에게 채널 보기와 메시지 기록 보기를 허용해주세요.")
        entries = [("기본 회원 역할", int(config.get("member_role_id", 0)))]
        for label, key in QUESTIONS.items():
            items = option_items(config, key)
            limit = 24 if key == "year" else 25
            if not 1 <= len(items) <= limit:
                errors.append(f"[{label}] /입장역할연결로 선택지를 1~{limit}개 설정해주세요.")
            entries.extend((f"{label} / {item['label']}", int(item.get("role_id", 0))) for _, item in items)
        seen, roles = {}, {}
        for label, role_id in entries:
            if role_id and role_id in seen:
                errors.append(f"[{label}] '{seen[role_id]}'와 같은 역할을 사용합니다. 항목마다 다른 역할을 연결해주세요.")
            if role_id:
                seen[role_id] = label
            try:
                role = self.role(guild, role_id)
                roles[role_id] = role
            except ValueError as error:
                errors.append(f"[{label}] {error}")
                continue
            if lobby:
                overwrite = lobby.overwrites_for(role)
                if overwrite.view_channel is False or overwrite.read_message_history is False:
                    errors.append(f"[{label}] '{role.name}' 역할이 대기 채널 접근을 막습니다.")
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel) or (lobby and channel.id == lobby.id):
                continue
            if channel.permissions_for(guild.default_role).view_channel:
                errors.append(f"[채널 접근] @everyone에게 다른 채널 '{channel.name}'이 보입니다. 채널 보기를 거부해주세요.")
            for _, role_id in entries[1:]:
                role = roles.get(role_id)
                if role:
                    if channel.overwrites_for(role).view_channel is True:
                        errors.append(f"[채널 접근] '{role.name}'이 '{channel.name}'을 엽니다. 채널 열기는 기본 역할에만 허용해주세요.")
        intros = config.get("introductions") or {}
        gate = roles.get(int(config.get("member_role_id", 0)))
        for index in range(1, 5):
            item = intros.get(f"slot{index}") or {}
            channel = guild.get_channel(int(item.get("channel_id", 0)))
            if not isinstance(channel, discord.TextChannel) or not item.get("description"):
                errors.append(f"[채널 소개 {index}번] /입장채널소개로 채널과 설명을 지정해주세요.")
            elif gate and channel.overwrites_for(gate).view_channel is not True:
                errors.append(f"[채널 소개 {index}번] 기본 역할에 '{channel.name}' 채널 보기를 명시적으로 허용해주세요.")
        log = guild.get_channel(int(config.get("log_id", 0)))
        if not isinstance(log, discord.TextChannel):
            errors.append("[기록 채널] /입장기본설정으로 비공개 기록 채널을 지정해주세요.")
        else:
            perms = log.permissions_for(guild.me)
            if not perms.view_channel or not perms.send_messages:
                errors.append("[기록 채널] 봇에게 채널 보기와 메시지 보내기를 허용해주세요.")
            if log.permissions_for(guild.default_role).view_channel:
                errors.append("[기록 채널] @everyone에게 숨겨주세요.")
            for role in guild.roles:
                if not role.permissions.administrator and log.overwrites_for(role).view_channel is True and role not in guild.me.roles:
                    errors.append(f"[기록 채널] '{role.name}' 역할의 보기 허용을 제거해주세요. 서버장과 봇 전용입니다.")
            for target, overwrite in log.overwrites.items():
                if isinstance(target, discord.Member) and target.id not in {guild.owner_id, guild.me.id} and overwrite.view_channel is True:
                    errors.append(f"[기록 채널] 회원 {target.id}의 개별 보기 허용을 제거해주세요.")
        return list(dict.fromkeys(errors))

    def validate(self, guild, config):
        errors = self.inspect(guild, config)
        if errors:
            preview = "\n".join(errors)[:1400]
            raise ValueError(f"설정에서 {len(errors)}개 문제를 찾았습니다.\n{preview}\n\n/입장검사로 전체 결과를 확인해주세요.")

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
        if stage == "done" and not korean_nickname(member.display_name):
            raise ValueError("서버 닉네임을 한글로 변경한 뒤 안내 완료를 눌러주세요.")
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
            if role.id == gate and stage == "done":
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
        if pending["stage"] in {"tour", "done"} and not answers.get("season"):
            session["answers"] = answers
            await self.begin_season(member, session)
            return
        try:
            await self.sync_roles(member, session, answers, pending["stage"])
        except ValueError as error:
            LOG.warning("Admission role processing failed member=%s: %s", member.id, error)
            await self.audit(member.guild, session["config"], admission_log(member, False, session.get("joined_at", ""), reason=str(error)))
            raise
        session.update(answers=answers, stage=pending["stage"], pending=None,
                       revision=session["revision"] + 1)
        if session["stage"] in {"review", "tour", "rejected"}:
            session.pop("edit_return", None)
        if session["stage"] in {"nickname", "review"}:
            session.pop("draft_interests", None)
        await self.save(member, session)
        if session["stage"] in {"done", "rejected"}:
            await self.audit(member.guild, session["config"],
                             admission_log(member, session["stage"] == "done", session.get("joined_at", ""),
                                           reason="허용 출생연도 범위 밖", returning=session.get("returning", False),
                                           previous_joined_at=session.get("previous_joined_at", "")))

    async def begin_season(self, member, session):
        if not session["config"]["questions"].get("season") or any(
                not item.get("role_id") for item in session["config"]["questions"]["season"].values()):
            config = await self.config(member.guild.id)
            session["config"]["questions"]["season"] = copy.deepcopy(config["questions"]["season"])
        options = session["config"]["questions"]["season"]
        if set(options) != set(SEASONS):
            raise ValueError("관리자가 봄·여름·가을·겨울 역할을 모두 연결해야 합니다.")
        for item in options.values():
            self.role(member.guild, int(item.get("role_id", 0)))
        role_ids = [int(item.get("role_id", 0)) for entries in session["config"]["questions"].values()
                    for item in (entries or {}).values()]
        role_ids.append(int(session["config"]["member_role_id"]))
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("계절 역할이 기존 입장 역할과 중복됩니다. 관리자에게 역할 연결 확인을 요청해주세요.")
        session["pending"] = {"stage": "season", "answers": session.get("answers") or {}}
        session["tour_index"] = 1
        await self.save(member, session)
        await self.apply_pending(member, session)

    def view(self, member_id, session):
        view = discord.ui.View(timeout=None)
        stage = session["stage"]
        prefix = f"npcob:{member_id}:{session['revision']}:"
        if stage == "season":
            for key, label in SEASONS.items():
                view.add_item(discord.ui.Button(label=label, custom_id=prefix + "season_" + key,
                                               style=discord.ButtonStyle.primary, row=0))
        elif stage in LABELS:
            selected = (session.get("draft_interests") or []) if stage == "interests" else (session.get("answers") or {}).get(stage, [])
            options = [discord.SelectOption(label=item["label"], value=key, default=key in selected)
                       for key, item in option_items(session["config"], stage)]
            if stage == "year":
                options.append(discord.SelectOption(label="그 외 나이", value="outside"))
            view.add_item(discord.ui.Select(custom_id=prefix + stage, placeholder=LABELS[stage],
                                           options=options, min_values=0 if stage == "interests" else 1, row=0,
                                           max_values=len(options) if stage == "interests" else 1))
            if stage == "interests":
                view.add_item(discord.ui.Button(label="관심사 선택 확인", custom_id=prefix + "confirm_interests",
                                               style=discord.ButtonStyle.primary, disabled=not selected, row=1))
        elif stage == "reject_confirm":
            view.add_item(discord.ui.Button(label="확인", style=discord.ButtonStyle.danger,
                                           custom_id=prefix + "confirm"))
        elif stage == "tour":
            view.add_item(discord.ui.Button(label="안내 완료" if session.get("tour_index", 1) == 4 else "다음 채널",
                                           custom_id=prefix + "next", style=discord.ButtonStyle.primary))
        elif stage == "nickname":
            view.add_item(discord.ui.Button(label="진행하기", custom_id=prefix + "check_nickname",
                                           style=discord.ButtonStyle.primary))
        elif stage == "review":
            view.add_item(discord.ui.Button(label="채널 안내 시작", custom_id=prefix + "complete",
                                           style=discord.ButtonStyle.success, row=0))
        editable = {"year": ["gender"], "interests": ["gender", "year"],
                    "nickname": ["gender", "year", "interests"],
                    "review": list(LABELS), "tour": list(LABELS)}
        names = {value: key for key, value in QUESTIONS.items()}
        for question in editable.get(stage, []):
            view.add_item(discord.ui.Button(label=f"{names[question]} 다시 선택", custom_id=prefix + "edit_" + question, row=1))
        return view

    async def render(self, member, session):
        thread = self.bot.get_channel(session["thread_id"]) or await self.bot.fetch_channel(session["thread_id"])
        if not isinstance(thread, discord.Thread) or thread.type != discord.ChannelType.private_thread:
            raise ValueError("안내 스레드를 확인해주세요.")
        if thread.archived or thread.locked:
            await thread.edit(archived=False, locked=False)
        stage = session["stage"]
        content = LABELS.get(stage, "")
        if stage == "reject_confirm":
            content = f"허용 출생연도에 해당하지 않으면 입장할 수 없습니다. 확인하면 입장 절차에서 관리하는 역할을 회수합니다.\n\n입장 관련 문의는 <@{member.guild.owner_id}> 님에게 직접 연락해주세요."
        elif stage == "rejected":
            content = f"입장 가능한 출생연도 범위에 해당하지 않아 입장이 제한되었습니다. 입장 관련 역할을 회수했습니다.\n\n입장 관련 문의는 <@{member.guild.owner_id}> 님에게 직접 연락해주세요."
        elif stage == "interests":
            selected = session.get("draft_interests") or []
            names = [session["config"]["questions"]["interests"][key]["label"] for key in selected]
            content += "\n여러 항목을 고른 뒤 아래 **관심사 선택 확인** 버튼을 눌러주세요.\n\n현재 선택: " + (", ".join(names) or "없음")
        elif stage == "nickname":
            content = "**서버 닉네임은 한글로만 사용할 수 있어요.**\n한글 닉네임으로 변경한 뒤 **진행하기**를 눌러주세요. 이미 한글이면 바로 진행할 수 있어요.\n\n현재 서버 닉네임: " + discord.utils.escape_markdown(member.display_name)
        elif stage == "review":
            content = "**선택한 정보를 확인해주세요.**\n"
            for label, question in QUESTIONS.items():
                values = (session.get("answers") or {}).get(question, [])
                names = [session["config"]["questions"][question][key]["label"] for key in values]
                content += f"\n{label}: {', '.join(names) or '미선택'}"
            content += "\n\n잘못 선택했다면 아래에서 다시 선택할 수 있어요. 맞으면 **채널 안내 시작**을 눌러주세요."
        elif stage == "tour":
            index = str(session.get("tour_index", 1))
            intro = session["config"]["introductions"][f"slot{index}"]
            content = f"주요 채널을 소개합니다. 네 채널을 확인하고 안내 완료를 누르면 서버 채널을 이용할 수 있어요.\n\n({index}/4) <#{intro['channel_id']}>\n{intro['description']}"
        elif stage == "done":
            content = "안내가 끝났습니다. 즐거운 서버 생활 되세요!\n\n" + "\n".join(
                f"<#{v['channel_id']}> — {v['description']}" for _, v in sorted(session["config"]["introductions"].items()))
        if stage in {*LABELS, "nickname", "review"}:
            content = "환영합니다! 성별 → 출생연도 → 관심사 선택을 마치면 서버 채널을 이용할 수 있어요.\n\n" + content
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
            # Component payloads can omit joined_at; always use Discord's latest member.
            member = await member.guild.fetch_member(member.id)
            config = await self.config(member.guild.id)
            if not config["enabled"]:
                raise ValueError("현재 봇 입장 안내가 꺼져 있습니다.")
            session = await self.session(member.guild.id, member.id)
            joined_at = membership_stamp(member)
            thread = None
            missing_thread = False
            if session and session.get("thread_id"):
                try:
                    # Check Discord directly: a cached thread may already have been deleted.
                    thread = await self.bot.fetch_channel(session["thread_id"])
                except discord.NotFound:
                    missing_thread = True
            # Recover records created by older versions even if their join timestamp was missing.
            missing_completed_role = session and session["stage"] == "done" and not any(
                r.id == int(session["config"]["member_role_id"]) for r in member.roles)
            if session and (is_new_membership(member, session) or missing_completed_role or missing_thread):
                self.validate(member.guild, config)
                previous = session
                rejoined = is_new_membership(member, previous)
                session = {"stage": "gender", "answers": {},
                           "revision": max(previous["revision"] + 1, int(time.time() * 1000)),
                           "config": copy.deepcopy(config), "thread_id": 0 if missing_thread else previous.get("thread_id", 0),
                           "message_id": 0, "tour_index": 1, "joined_at": joined_at,
                           "returning": rejoined or previous.get("returning", False),
                           "previous_joined_at": previous.get("joined_at", "") if rejoined else previous.get("previous_joined_at", ""),
                           "cleanup_config": previous.get("cleanup_config") or previous["config"]}
                # Persist the reset before external calls, so a failed retry never resumes completion.
                await self.save(member, session)
            if session and session.get("cleanup_config"):
                await self.sync_roles(member, {"config": session["cleanup_config"]}, {}, "rejected")
                session.pop("cleanup_config", None)
                await self.save(member, session)
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
            if thread is None:
                lobby = member.guild.get_channel(int(session["config"]["lobby_id"]))
                if not isinstance(lobby, discord.TextChannel):
                    raise ValueError("대기 채널이 없습니다. 관리자에게 문의해주세요.")
                thread = await lobby.create_thread(name=thread_name(member),
                                                   type=discord.ChannelType.private_thread,
                                                   invitable=False, auto_archive_duration=1440)
                session.update(thread_id=thread.id, message_id=0)
                await self.save(member, session)
            if thread.archived or thread.locked:
                await thread.edit(archived=False, locked=False)
            if thread.name != thread_name(member):
                await thread.edit(name=thread_name(member))
            await thread.add_user(member)
            await thread.add_user(discord.Object(id=member.guild.owner_id))
            if not session.get("greeted"):
                greeting = ("이전에 들어오신 기록이 확인돼요. 다시 만나 반갑습니다! 새로운 마음으로 다시 시작할 수 있도록 정보를 한 번 더 여쭤볼게요."
                            if session.get("returning") else "이곳에서 입장 안내를 진행해주세요.")
                await thread.send(f"{member.mention} 님, {greeting}",
                                  allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
                session["greeted"] = True
                await self.save(member, session)
            await self.apply_pending(member, session)
            if session["stage"] == "tour" and not (session.get("answers") or {}).get("season"):
                await self.begin_season(member, session)
            return await self.render(member, session)

    async def on_leave(self, member):
        if member.bot:
            return
        try:
            async with self.lock(member.guild.id, member.id):
                session = await self.session(member.guild.id, member.id)
                if session:
                    # A delayed leave event must not mark a newer admission as departed.
                    stamp = membership_stamp(member)
                    if stamp and session.get("joined_at") and is_new_membership(member, session):
                        return
                    session["left_at"] = int(time.time())
                    await self.save(member, session)
        except Exception:
            LOG.exception("Could not record departure guild=%s member=%s", member.guild.id, member.id)

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
                                 admission_log(member, False, reason="입장 안내 시작 오류 · 권한/설정 확인"))
            except Exception:
                LOG.exception("Admission start failure could not be reported")

    async def on_interaction(self, interaction):
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        if interaction.type != discord.InteractionType.component or not custom_id.startswith("npcob:"):
            return
        is_start = custom_id == "npcob:start"
        # Component updates acknowledge silently; only the lobby needs a private thread link.
        await interaction.response.defer(ephemeral=is_start, thinking=is_start)
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
                member = await interaction.guild.fetch_member(interaction.user.id)
                if is_new_membership(member, session):
                    raise ValueError("재입장한 회원입니다. 대기 채널의 시작 / 이어하기 버튼을 눌러 새 안내를 시작해주세요.")
                if int(revision) != session["revision"] or session["stage"] in TERMINAL:
                    # Duplicate/old clicks are harmless and must not clutter the thread.
                    return
                if session.get("pending"):
                    await self.apply_pending(interaction.user, session)
                elif action.startswith("edit_"):
                    target = action.removeprefix("edit_")
                    allowed = {"year": {"gender"}, "interests": {"gender", "year"},
                               "nickname": set(LABELS), "review": set(LABELS), "tour": set(LABELS)}
                    if target not in allowed.get(session["stage"], set()):
                        raise ValueError("현재 단계에서는 해당 항목을 수정할 수 없습니다.")
                    if session["stage"] in {"review", "tour"}:
                        session["edit_return"] = "review"
                    if target == "interests":
                        session["draft_interests"] = (session.get("answers") or {}).get("interests", [])
                    session["tour_index"] = 1
                    session["pending"] = {"answers": session.get("answers") or {}, "stage": target}
                    await self.save(member, session)
                    await self.apply_pending(member, session)
                elif session["stage"] == "interests" and action == "interests":
                    values = data.get("values") or []
                    if values:
                        choose(session, "interests", values)  # validate before persisting a draft
                    session["draft_interests"] = values
                    # Keep this revision until confirmation, allowing repeated draft selection.
                    await self.save(member, session)
                elif session["stage"] == "interests" and action == "confirm_interests":
                    answers, stage = choose(session, "interests", session.get("draft_interests") or [])
                    session["pending"] = {"answers": answers, "stage": stage}
                    await self.save(member, session)
                    await self.apply_pending(member, session)
                elif session["stage"] == "nickname" and action == "check_nickname":
                    if not korean_nickname(member.display_name):
                        await self.render(member, session)
                        raise ValueError("서버 닉네임을 한글로만 변경한 뒤 진행하기를 눌러주세요.")
                    await self.begin_season(member, session)
                elif session["stage"] == "season" and action.startswith("season_"):
                    answers, stage = choose(session, "season", [action.removeprefix("season_")])
                    session["pending"] = {"answers": answers, "stage": stage}
                    await self.save(member, session)
                    await self.apply_pending(member, session)
                elif session["stage"] == "review" and action == "complete":
                    if not korean_nickname(member.display_name):
                        session.update(stage="nickname", revision=session["revision"] + 1)
                        await self.save(member, session)
                        await self.render(member, session)
                        return
                    if not (session.get("answers") or {}).get("season"):
                        await self.begin_season(member, session)
                        await self.render(member, session)
                        return
                    answers = session.get("answers") or {}
                    desired_ids(session["config"], answers, "tour")  # require every role question
                    session["pending"] = {"answers": answers, "stage": "tour"}
                    await self.save(member, session)
                    await self.apply_pending(member, session)
                elif session["stage"] == "tour" and action == "next":
                    if not (session.get("answers") or {}).get("season"):
                        await self.begin_season(member, session)
                        await self.render(member, session)
                        return
                    index = session.get("tour_index", 1)
                    if index == 4:
                        if not korean_nickname(member.display_name):
                            session["pending"] = {"answers": session.get("answers") or {}, "stage": "nickname"}
                        else:
                            session["pending"] = {"answers": session.get("answers") or {}, "stage": "done"}
                        await self.save(member, session)
                        await self.apply_pending(member, session)
                    else:
                        session.update(tour_index=index + 1, revision=session["revision"] + 1)
                        await self.save(member, session)
                else:
                    values = data.get("values") or []
                    if session["stage"] == "reject_confirm":
                        values, action = [action], "reject_confirm"
                    answers, stage = choose(session, action, values)
                    session["pending"] = {"answers": answers, "stage": stage}
                    await self.save(interaction.user, session)
                    await self.apply_pending(interaction.user, session)
                await self.render(member, session)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            LOG.exception("Admission interaction failed")
            await interaction.followup.send("처리를 완료하지 못했습니다. 진행 내용은 저장되며, 대기 채널에서 이어하기를 눌러 다시 시도할 수 있습니다. 계속 실패하면 서버장에게 문의해주세요.", ephemeral=True)
            try:
                await self.audit(interaction.guild, await self.config(interaction.guild.id),
                                 admission_log(interaction.user, False, reason="입장 처리 오류 · 봇 실행 로그 확인"))
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


class _SettingsActions:
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
                    raise ValueError("기본 설정 변경 전 안내를 꺼주세요. 진행 중인 신입은 이전 설정을 유지하므로 필요하면 /입장재시작을 사용해주세요.")
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
                    result += " 새 입장 안내를 켰습니다. /입장안내게시로 이어하기 버튼을 게시해주세요."
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
                      질문: Literal["성별", "출생연도", "관심사", "계절"],
                      선택지: app_commands.Range[str, 1, 80] | None = None,
                      역할: discord.Role | None = None,
                      순서: int | None = None):
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
            if question == "season" and (label not in SEASONS.values() or 작업 == "삭제"):
                raise ValueError("계절은 봄·여름·가을·겨울 고정 항목입니다. 역할 연결만 변경할 수 있습니다.")
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
                item = {**(options.get(key) or {}), "label": label, "role_id": 역할.id}
                if 순서 is not None:
                    item["order"] = 순서
                options[key or uuid.uuid4().hex] = item
            if config["enabled"] and question != "season":
                service.validate(interaction.guild, config)
            await service.save_config(interaction.guild_id, config)
            await interaction.followup.send("선택지를 저장했습니다. 새로 시작하는 신입부터 적용됩니다. 진행 중인 신입에게 적용하려면 /입장재시작을 사용해주세요.", ephemeral=True)

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


async def send_report(interaction, lines):
    """Keep long audits readable and below Discord's per-message limit."""
    page = ""
    for line in lines:
        if page and len(page) + len(line) + 1 > 1800:
            await interaction.followup.send(page, ephemeral=True, allowed_mentions=NO_PING)
            page = ""
        page += line + "\n"
    if page:
        await interaction.followup.send(page, ephemeral=True, allowed_mentions=NO_PING)


class OnboardingCommands(commands.Cog):
    """One task per command; no irrelevant parameters or action selectors."""
    def __init__(self, service):
        self.service = service
        self.actions = _SettingsActions(service)

    async def cog_app_command_error(self, interaction, error):
        await self.actions.cog_app_command_error(interaction, error)

    @app_commands.command(name="입장도움말", description="처음 설정할 때: 1~6단계 순서와 사용할 명령어를 보여줍니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def help(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await send_report(interaction, [
            "**입장 안내 설정 순서**",
            "1️⃣ `/입장기본설정` — 대기 채널, 기본 회원 역할, 기록 채널을 지정합니다.",
            "2️⃣ `/입장역할연결` — 질문 → 선택지 → 기존 역할 순서로 연결합니다. 선택지는 추천 목록에서 고를 수 있습니다.",
            "3️⃣ `/입장채널소개` — 소개 순서 1~4번에 채널과 설명을 지정합니다.",
            "4️⃣ `/입장검사` — 추가 선택 없이 모든 설정을 한 번에 검사합니다.",
            "5️⃣ `/입장안내게시` — 대기 채널에 시작·이어하기 버튼을 게시합니다.",
            "6️⃣ `/입장시작` — 자동 입장 안내를 켭니다.", "",
            "**확인과 수정**",
            "`/입장현황` — 채널, 역할 연결, 소개를 실제 표시 순서대로 모두 확인합니다.",
            "`/입장선택지삭제` — 사용하지 않을 선택지를 삭제합니다.",
            "`/입장이어하기` — 특정 신입의 안내를 재개합니다.",
            "`/입장재시작` — 해당 신입의 입장 역할을 회수하고 현재 설정으로 다시 시작합니다.",
            "`/입장중지` — 새 안내를 끄고 기존 첫 채팅 역할 지급 방식으로 돌아갑니다.", "",
            "성별·출생연도는 하나, 관심사는 복수 선택입니다. '그 외 나이'는 자동으로 추가됩니다.",
            "역할연결의 선택지에 기존 이름을 고르면 수정, 새 이름을 입력하면 추가됩니다.",
            "순서를 지정하면 작은 번호부터 표시됩니다. 기본 출생연도 순서는 07년생 → 90년생입니다.",
            "저장한 설정은 유지됩니다. 진행 중인 신입에게 변경을 적용하려면 /입장재시작을 사용하세요."])

    @app_commands.command(name="입장현황", description="현재 채널·역할 연결·소개 4개를 설정 순서대로 모두 확인합니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def overview(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = await self.service.config(interaction.guild_id)
        def channel_label(ident):
            channel = interaction.guild.get_channel(int(ident or 0))
            return channel.mention if channel else "⚠ 미설정 또는 삭제된 채널"
        def role_label(ident):
            role = interaction.guild.get_role(int(ident or 0))
            return f"{role.name} (<@&{role.id}>)" if role else "⚠ 역할 미연결 또는 삭제됨"
        lines = [f"**입장 안내: {'켜짐' if config['enabled'] else '꺼짐'}**", "**1. 기본 설정**",
                 f"대기 채널: {channel_label(config['lobby_id'])}",
                 f"기본 회원 역할: {role_label(config['member_role_id'])}",
                 f"기록 채널: {channel_label(config['log_id'])}", "", "**2. 선택지 → 기존 역할**"]
        for label, question in QUESTIONS.items():
            lines.append(f"**{label}**")
            items = option_items(config, question)
            if not items:
                lines.append("⚠ 선택지가 없습니다.")
            for index, (_, item) in enumerate(items, 1):
                lines.append(f"{index}. {item['label']} → {role_label(item.get('role_id'))}")
            if question == "year":
                lines.append("마지막. 그 외 나이 → 입장 제한 (고정)")
        lines.extend(["", "**3. 채널 소개**"])
        for index in range(1, 5):
            intro = (config.get("introductions") or {}).get(f"slot{index}") or {}
            lines.append(f"{index}. {channel_label(intro.get('channel_id'))} — {intro.get('description', '설명 미설정')}")
        lines.extend(["", "다음: `/입장검사`로 전체 설정을 검사해주세요."])
        await send_report(interaction, lines)

    @app_commands.command(name="입장검사", description="입력 항목 없이 모든 채널·역할·선택지·권한을 한 번에 검사합니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = await self.service.config(interaction.guild_id)
        errors = self.service.inspect(interaction.guild, config)
        if errors:
            lines = [f"**전체 검사 결과: 수정할 항목 {len(errors)}개**",
                     "아래 항목을 수정한 뒤 /입장검사를 다시 실행해주세요.", ""]
            lines.extend(f"{index}. {error}" for index, error in enumerate(errors, 1))
        else:
            lines = ["✅ **전체 검사 통과**", "기본 채널·역할, 모든 선택지, 소개 4개, 봇 권한을 확인했습니다.",
                     "다음: `/입장안내게시` → `/입장시작` 순서로 실행해주세요."]
        await send_report(interaction, lines)

    @app_commands.command(name="입장기본설정", description="1단계: 대기 채널 → 기본 회원 역할 → 비공개 기록 채널 지정")
    @app_commands.describe(대기채널="신입에게 처음 보이는 일반 텍스트 채널", 기본역할="모든 질문 완료 후 지급할 기존 회원 역할",
                           기록채널="서버장과 봇이 입장 결과를 확인할 비공개 채널")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def basic(self, interaction: discord.Interaction, 대기채널: discord.TextChannel,
                    기본역할: discord.Role, 기록채널: discord.TextChannel):
        await self.actions.configure.callback(self.actions, interaction, "기본", 대기채널, 기본역할, 기록채널)

    @app_commands.command(name="입장역할연결", description="2단계: 선택지에 기존 역할 연결. 같은 선택지면 수정, 새 이름이면 추가")
    @app_commands.describe(질문="성별 → 출생연도 → 관심사 순서로 설정하세요", 선택지="추천 목록에서 선택하거나 새 선택지 이름 입력",
                           역할="선택 시 지급할 기존 서버 역할", 순서="선택 사항: 작은 번호부터 표시 (예: 1, 2, 3)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def link(self, interaction: discord.Interaction, 질문: Literal["성별", "출생연도", "관심사", "계절"],
                   선택지: app_commands.Range[str, 1, 80], 역할: discord.Role,
                   순서: app_commands.Range[int, 1, 25] | None = None):
        await self.actions.options.callback(self.actions, interaction, "등록", 질문, 선택지, 역할, 순서)

    async def suggest(self, interaction, current):
        question = QUESTIONS.get(getattr(interaction.namespace, "질문", ""))
        if not question or not interaction.guild_id or not interaction.user.guild_permissions.administrator:
            return []
        config = await self.service.config(interaction.guild_id)
        return [app_commands.Choice(name=f"{item['label']} · {'연결됨' if item.get('role_id') else '미연결'}", value=item['label'])
                for _, item in option_items(config, question) if current.casefold() in item['label'].casefold()][:25]

    @link.autocomplete("선택지")
    async def suggest_link(self, interaction: discord.Interaction, current: str):
        return await self.suggest(interaction, current)

    @app_commands.command(name="입장선택지삭제", description="사용하지 않을 선택지 삭제. '그 외 나이'는 삭제할 수 없습니다")
    @app_commands.describe(질문="삭제할 선택지가 속한 질문", 선택지="삭제할 기존 선택지를 추천 목록에서 선택")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def delete(self, interaction: discord.Interaction, 질문: Literal["성별", "출생연도", "관심사"],
                     선택지: app_commands.Range[str, 1, 80]):
        await self.actions.options.callback(self.actions, interaction, "삭제", 질문, 선택지)

    @delete.autocomplete("선택지")
    async def suggest_delete(self, interaction: discord.Interaction, current: str):
        return await self.suggest(interaction, current)

    @app_commands.command(name="입장채널소개", description="3단계: 소개할 채널을 1번부터 4번까지 순서대로 지정합니다")
    @app_commands.describe(순서="소개 순서: 1, 2, 3, 4", 채널="신입에게 소개할 기존 채팅 채널", 설명="이 채널에서 무엇을 하는지 짧게 설명")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def introduction(self, interaction: discord.Interaction, 순서: app_commands.Range[int, 1, 4],
                           채널: discord.TextChannel, 설명: app_commands.Range[str, 1, 250]):
        await self.actions.introduction.callback(self.actions, interaction, 순서, 채널, 설명)

    @app_commands.command(name="입장안내게시", description="5단계: 전체 검사 후 대기 채널에 시작·이어하기 버튼 게시")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def publish(self, interaction: discord.Interaction):
        await self.actions.configure.callback(self.actions, interaction, "안내게시")

    @app_commands.command(name="입장시작", description="6단계: 전체 검사 후 신입 자동 입장 안내를 켭니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def enable(self, interaction: discord.Interaction):
        await self.actions.configure.callback(self.actions, interaction, "켜기")

    @app_commands.command(name="입장중지", description="새 안내를 끕니다. 기존 첫 채팅 역할 지급 방식이 다시 동작합니다")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def disable(self, interaction: discord.Interaction):
        await self.actions.configure.callback(self.actions, interaction, "끄기")

    @app_commands.command(name="입장이어하기", description="신입의 저장된 단계부터 안내를 이어갑니다")
    @app_commands.describe(대상="안내를 이어갈 신입")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def resume(self, interaction: discord.Interaction, 대상: discord.Member):
        await self.actions.manage.callback(self.actions, interaction, "이어하기", 대상)

    @app_commands.command(name="입장재시작", description="대상자의 입장 역할을 회수하고 현재 설정으로 처음부터 안내합니다")
    @app_commands.describe(대상="입장 역할을 회수하고 다시 안내할 회원")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def restart(self, interaction: discord.Interaction, 대상: discord.Member):
        await self.actions.manage.callback(self.actions, interaction, "재시작", 대상)


async def install(bot, initialize_member):
    service = Onboarding(bot, initialize_member)
    bot.add_listener(service.on_join, "on_member_join")
    bot.add_listener(service.on_leave, "on_member_remove")
    bot.add_listener(service.on_interaction, "on_interaction")
    await bot.add_cog(OnboardingCommands(service))
    return service
