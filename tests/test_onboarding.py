import asyncio
import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

from onboarding import (Onboarding, OnboardingCommands, _SettingsActions, option_items, choose, default_config, thread_name,
                        desired_ids, install, managed_ids, admission_log)


class MemoryStore:
    def __init__(self):
        self.data = {}

    async def get(self, path):
        return copy.deepcopy(self.data.get(path))

    async def put(self, path, value):
        self.data[path] = copy.deepcopy(value)


class Role:
    def __init__(self, ident):
        self.id = ident
        self.name = f"decorated-role-{ident}"
        self.managed = False
        self.permissions = discord.Permissions.none()

    def is_default(self):
        return self.id == 0

    def __ge__(self, other):
        return self.id >= other.id


def configured():
    return {"enabled": True, "lobby_id": 30, "log_id": 31, "member_role_id": 9,
            "questions": {"gender": {"male": {"label": "남자", "role_id": 1},
                                      "female": {"label": "여자", "role_id": 2}},
                          "year": {"2007": {"label": "07년생", "role_id": 3}},
                          "interests": {"game": {"label": "게임", "role_id": 4},
                                        "music": {"label": "음악", "role_id": 5}}},
            "introductions": {f"slot{i}": {"channel_id": 40 + i, "description": f"채널 {i}"}
                              for i in range(1, 5)}}


def session(stage="gender"):
    return {"config": configured(), "stage": stage, "answers": {}, "revision": 1,
            "thread_id": 50, "message_id": 51, "tour_index": 1, "joined_at": ""}


class SelectionTests(unittest.TestCase):
    def test_exact_existing_ids_and_multiple_interests(self):
        state = session()
        for action, values in [("gender", ["male"]), ("year", ["2007"]),
                               ("interests", ["game", "music"])]:
            answers, stage = choose(state, action, values)
            state.update(answers=answers, stage=stage)
        self.assertEqual(stage, "review")
        self.assertEqual(desired_ids(state["config"], answers, stage), {1, 3, 4, 5})
        self.assertEqual(desired_ids(state["config"], answers, "tour"), {1, 3, 4, 5, 9})

    def test_bad_values_and_skipping_stages_rejected(self):
        for action, values in [("year", ["2007"]), ("gender", ["unknown"]),
                               ("gender", ["male", "female"]), ("gender", []),
                               ("gender", ["male", "male"])]:
            with self.subTest(action=action, values=values), self.assertRaises(ValueError):
                choose(session(), action, values)

    def test_outside_age_requires_confirmation_and_cannot_go_back(self):
        state = session("year")
        state["answers"] = {"gender": ["female"]}
        answers, stage = choose(state, "year", ["outside"])
        self.assertEqual(stage, "reject_confirm")
        self.assertEqual(desired_ids(state["config"], answers, stage), {2})
        state.update(answers=answers, stage=stage)
        with self.assertRaises(ValueError):
            choose(state, "reject_confirm", ["back"])
        answers, stage = choose(state, "reject_confirm", ["confirm"])
        self.assertEqual(desired_ids(state["config"], answers, stage), set())

    def test_basic_role_never_granted_early(self):
        with self.assertRaises(ValueError):
            desired_ids(configured(), {"gender": ["male"]}, "tour")
        self.assertNotIn(9, desired_ids(configured(), {"gender": ["male"]}, "year"))

    def test_default_disabled_and_19_age_options(self):
        config = default_config()
        self.assertFalse(config["enabled"])
        self.assertEqual(len(config["questions"]["year"]) + 1, 19)
        self.assertTrue(all(not item["role_id"] for item in config["questions"]["year"].values()))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.roles = {i: Role(i) for i in range(10)}
        self.member = MagicMock(spec=discord.Member)
        self.member.id = 100
        self.member.bot = False
        self.member.joined_at = None
        self.member.roles = [self.roles[0], self.roles[8]]  # unrelated role
        self.member.mention = "<@100>"
        self.member.display_name = "신입"
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.id = 200
        self.guild.owner_id = 300
        self.guild.me = SimpleNamespace(id=400, top_role=Role(99),
                                        guild_permissions=SimpleNamespace(manage_roles=True))
        self.guild.get_role.side_effect = self.roles.get
        self.member.guild = self.guild
        self.guild.fetch_member = AsyncMock(return_value=self.member)
        self.operations = []

        async def add(role, **kwargs):
            self.operations.append(("add", role.id))
            if role not in self.member.roles:
                self.member.roles.append(role)

        async def remove(role, **kwargs):
            self.operations.append(("remove", role.id))
            self.member.roles = [r for r in self.member.roles if r.id != role.id]

        self.member.add_roles = AsyncMock(side_effect=add)
        self.member.remove_roles = AsyncMock(side_effect=remove)
        self.bot = MagicMock()
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: self.bot.get_channel(channel_id))
        self.init = AsyncMock()
        self.service = Onboarding(self.bot, self.init, self.store)
        self.service.audit = AsyncMock()
        self.service.render = AsyncMock()
        await self.service.save_config(200, configured())

    async def test_basic_role_is_last_and_preserves_unrelated_roles(self):
        answers = {"gender": ["male"], "year": ["2007"], "interests": ["music", "game"]}
        await self.service.sync_roles(self.member, session(), answers, "tour")
        self.assertEqual(self.operations[-1], ("add", 9))
        self.assertEqual({r.id for r in self.member.roles}, {0, 1, 3, 4, 5, 8, 9})
        self.init.assert_awaited_once()

    async def test_failure_before_gate_then_reboot_retries_saved_intent(self):
        state = session("interests")
        state["pending"] = {"answers": {"gender": ["male"], "year": ["2007"],
                                        "interests": ["game"]}, "stage": "tour"}
        await self.service.save(self.member, state)
        self.init.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            await self.service.apply_pending(self.member, state)
        self.assertNotIn(9, {r.id for r in self.member.roles})
        saved = await self.service.session(200, 100)
        self.assertEqual(saved["stage"], "interests")
        rebooted = Onboarding(self.bot, AsyncMock(), self.store)
        rebooted.audit = AsyncMock()
        await rebooted.apply_pending(self.member, saved)
        self.assertEqual((await rebooted.session(200, 100))["stage"], "tour")
        self.assertIn(9, {r.id for r in self.member.roles})
        self.assertEqual(self.operations.count(("add", 1)), 1)

    async def test_rejection_removes_only_managed_roles_gate_first(self):
        self.member.roles += [self.roles[1], self.roles[3], self.roles[9]]
        state = session("reject_confirm")
        # Firebase omits empty dictionaries (including empty pending.answers).
        state["pending"] = {"stage": "rejected"}
        await self.service.apply_pending(self.member, state)
        self.assertEqual(self.operations[0], ("remove", 9))
        self.assertEqual({r.id for r in self.member.roles}, {0, 8})
        self.assertEqual(state["stage"], "rejected")

    async def test_role_renamed_still_uses_same_id(self):
        self.roles[3].name = "✧··· * 07 * ···✧"
        await self.service.sync_roles(self.member, session(), {"year": ["2007"]}, "interests")
        self.assertIn(self.roles[3], self.member.roles)

    async def test_missing_or_unsafe_role_blocks(self):
        self.roles[1].permissions.administrator = True
        with self.assertRaises(ValueError):
            await self.service.sync_roles(self.member, session(), {"gender": ["male"]}, "year")
        self.roles.pop(1)
        with self.assertRaises(ValueError):
            await self.service.sync_roles(self.member, session(), {"gender": ["male"]}, "year")
        self.member.add_roles.assert_not_awaited()

    def interaction(self, ident="npcob:100:1:gender", values=None):
        interaction = MagicMock(spec=discord.Interaction)
        interaction.data = {"custom_id": ident, "values": values or ["male"]}
        interaction.type = discord.InteractionType.component
        interaction.guild = self.guild
        interaction.guild_id = 200
        interaction.user = self.member
        interaction.channel_id = 50
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_duplicate_click_advances_once(self):
        await self.service.save(self.member, session())
        one, two = self.interaction(), self.interaction()
        await asyncio.gather(self.service.on_interaction(one), self.service.on_interaction(two))
        self.assertEqual((await self.service.session(200, 100))["stage"], "year")
        self.assertEqual(self.operations.count(("add", 1)), 1)
        self.assertEqual(self.service.render.await_count, 1)

    async def test_other_person_wrong_thread_stale_and_disabled_blocked(self):
        await self.service.save(self.member, session())
        cases = [self.interaction("npcob:101:1:gender"), self.interaction("npcob:100:0:gender")]
        wrong_thread = self.interaction()
        wrong_thread.channel_id = 999
        cases.append(wrong_thread)
        for interaction in cases:
            await self.service.on_interaction(interaction)
        config = configured()
        config["enabled"] = False
        await self.service.save_config(200, config)
        await self.service.on_interaction(self.interaction())
        self.member.add_roles.assert_not_awaited()

    async def test_snapshot_survives_admin_remapping(self):
        state = session()
        await self.service.save(self.member, state)
        config = configured()
        config["questions"]["gender"]["male"]["role_id"] = 6
        await self.service.save_config(200, config)
        await self.service.on_interaction(self.interaction())
        self.assertIn(self.roles[1], self.member.roles)
        self.assertNotIn(self.roles[6], self.member.roles)

    async def test_view_limits_and_custom_ids_survive_rebuild(self):
        state = session("year")
        state["config"] = default_config()
        view = self.service.view(100, state)
        self.assertEqual(len(view.children[0].options), 19)
        self.assertTrue(view.is_persistent())
        state["stage"] = "interests"
        state["config"] = configured()
        self.assertEqual(self.service.view(100, state).children[0].max_values, 2)

    async def test_four_channel_tour_ends_only_after_four_clicks(self):
        state = session("tour")
        await self.service.save(self.member, state)
        for rev in range(1, 5):
            await self.service.on_interaction(self.interaction(f"npcob:100:{rev}:next"))
            saved = await self.service.session(200, 100)
            self.assertEqual(saved["stage"], "done" if rev == 4 else "tour")
        self.member.add_roles.assert_not_awaited()

    async def test_admin_role_mapping_and_duplicate_rejection(self):
        config = configured()
        config["enabled"] = False
        await self.service.save_config(200, config)
        cog = _SettingsActions(self.service)
        interaction = self.interaction()
        await cog.options.callback(cog, interaction, "등록", "출생연도", "07년생", self.roles[6])
        saved = await self.service.config(200)
        self.assertEqual(saved["questions"]["year"]["2007"]["role_id"], 6)
        with self.assertRaises(ValueError):
            await cog.options.callback(cog, interaction, "등록", "관심사", "중복", self.roles[6])

    async def test_config_and_sessions_are_guild_scoped(self):
        await self.service.save(self.member, session())
        self.assertIsNone(await self.service.session(201, 100))
        self.assertFalse((await self.service.config(201))["enabled"])

    async def test_firebase_empty_config_collections_do_not_restore_deleted_options(self):
        await self.store.put("config/200", {"enabled": False, "member_role_id": 9})
        config = await self.service.config(200)
        self.assertEqual(config["questions"], {"gender": {}, "year": {}, "interests": {}})
        self.assertEqual(config["introductions"], {})

    async def test_lobby_and_personal_thread_excluded_from_xp(self):
        await self.service.save(self.member, session())
        message = SimpleNamespace(channel=SimpleNamespace(id=30), guild=self.guild, author=self.member)
        self.assertTrue(await self.service.excludes_message(message, configured()))
        thread = MagicMock(spec=discord.Thread)
        thread.id = 50
        thread.name = thread_name(self.member)
        message.channel = thread
        self.assertTrue(await self.service.excludes_message(message, configured()))
        thread.id = 99
        self.assertFalse(await self.service.excludes_message(message, configured()))

    async def test_install_registers_clear_admin_commands_without_network(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        try:
            await install(bot, AsyncMock())
            self.assertEqual({cmd.name for cmd in bot.tree.get_commands()},
                             {"입장도움말", "입장현황", "입장검사", "입장기본설정", "입장역할연결", "입장선택지삭제", "입장채널소개", "입장안내게시", "입장시작", "입장중지", "입장이어하기", "입장재시작"})
            for cmd in bot.tree.get_commands():
                self.assertTrue(cmd.default_permissions.administrator)
                self.assertTrue(cmd.checks)
            for name in ("입장검사", "입장현황", "입장도움말", "입장안내게시", "입장시작", "입장중지"):
                self.assertEqual(bot.tree.get_command(name).parameters, [])
            self.assertEqual([p.name for p in bot.tree.get_command("입장역할연결").parameters],
                             ["질문", "선택지", "역할", "순서"])
            self.assertEqual([p.name for p in bot.tree.get_command("입장기본설정").parameters],
                             ["대기채널", "기본역할", "기록채널"])
        finally:
            await bot.close()

    def private_thread(self):
        thread = MagicMock(spec=discord.Thread)
        thread.id = 50
        thread.name = thread_name(self.member)
        thread.type = discord.ChannelType.private_thread
        thread.archived = False
        thread.locked = False
        thread.add_user = AsyncMock()
        thread.send = AsyncMock(return_value=SimpleNamespace(id=51))
        thread.edit = AsyncMock()
        message = SimpleNamespace(edit=AsyncMock())
        thread.fetch_message = AsyncMock(return_value=message)
        return thread

    async def test_deleted_thread_restarts_all_stages_and_clears_roles(self):
        for stage in ('year', 'review', 'tour', 'done', 'rejected'):
            with self.subTest(stage=stage):
                old = session(stage)
                old['answers'] = {'gender': ['male'], 'year': ['2007'], 'interests': ['game']}
                old['pending'] = {'answers': old['answers'], 'stage': 'tour'}
                old['greeted'] = True
                await self.service.save(self.member, old)
                self.member.roles = [self.roles[i] for i in (0, 1, 3, 4, 8, 9)]
                replacement = self.private_thread()
                replacement.id = 60
                lobby = MagicMock(spec=discord.TextChannel)
                lobby.create_thread = AsyncMock(return_value=replacement)
                self.guild.get_channel.return_value = lobby
                self.service.validate = MagicMock()
                self.bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(
                    SimpleNamespace(status=404, reason='Not Found'), 'Unknown Channel'))
                # Even a stale cached object must not hide the deletion.
                self.bot.get_channel.return_value = self.private_thread()
                await self.service.start(self.member)
                saved = await self.service.session(200, 100)
                self.assertEqual(saved['stage'], 'gender')
                self.assertEqual(saved['answers'], {})
                self.assertEqual(saved['thread_id'], 60)
                self.assertNotIn('pending', saved)
                self.assertGreater(saved['revision'], old['revision'])
                self.assertEqual({r.id for r in self.member.roles}, {0, 8})
                lobby.create_thread.assert_awaited_once()
                replacement.send.assert_awaited_once()

    async def test_thread_access_failure_does_not_reset_progress(self):
        old = session('review')
        await self.service.save(self.member, old)
        self.bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'Missing Access'))
        with self.assertRaises(discord.Forbidden):
            await self.service.start(self.member)
        self.assertEqual(await self.service.session(200, 100), old)
        self.member.remove_roles.assert_not_awaited()

    async def test_admission_logs_are_one_line_with_korean_join_time(self):
        self.member.joined_at = datetime(2026, 9, 21, 16, 30, tzinfo=timezone.utc)
        self.member.display_name = '새/회원\n이름'
        self.assertEqual(admission_log(self.member, True), '⭕ 100/새·회원 이름/2026-09-22 01:30:00')
        self.assertEqual(admission_log(self.member, False), '❌ 100/새·회원 이름/2026-09-22 01:30:00')
        for stage in ('tour', 'rejected'):
            state = session()
            state['pending'] = {'stage': stage, 'answers': {'gender': ['male'], 'year': ['2007'], 'interests': ['game']}}
            await self.service.apply_pending(self.member, state)
            self.assertEqual(self.service.audit.await_args.args[2], admission_log(self.member, stage == 'tour'))

    async def test_duplicate_starts_create_one_private_thread_and_add_owner(self):
        thread = self.private_thread()
        lobby = MagicMock(spec=discord.TextChannel)
        lobby.create_thread = AsyncMock(return_value=thread)
        self.guild.get_channel.return_value = lobby
        self.bot.get_channel.return_value = thread
        self.service.validate = MagicMock()
        await asyncio.gather(self.service.start(self.member), self.service.start(self.member))
        lobby.create_thread.assert_awaited_once()
        kwargs = lobby.create_thread.await_args.kwargs
        self.assertEqual(kwargs["type"], discord.ChannelType.private_thread)
        self.assertFalse(kwargs["invitable"])
        self.assertIn(300, [call.args[0].id for call in thread.add_user.await_args_list])
        self.assertEqual(thread.send.await_count, 1)  # one mention, not one per retry

    async def test_render_four_channel_summary_then_lock_and_archive(self):
        thread = self.private_thread()
        self.bot.get_channel.return_value = thread
        state = session("done")
        await Onboarding.render(self.service, self.member, state)
        message = await thread.fetch_message(51)
        kwargs = message.edit.await_args.kwargs
        self.assertIsNone(kwargs["view"])
        self.assertTrue(all(f"<#{41+i}>" in kwargs["content"] for i in range(4)))
        thread.edit.assert_awaited_once_with(locked=True, archived=True)

    async def test_resume_archived_thread_after_restart_without_new_thread(self):
        thread = self.private_thread()
        thread.archived = True
        self.bot.get_channel.return_value = thread
        state = session("year")
        state["greeted"] = True
        await self.service.save(self.member, state)
        await self.service.start(self.member)
        thread.edit.assert_awaited_once_with(archived=False, locked=False)
        self.service.render.assert_awaited_once()
        self.guild.get_channel.assert_not_called()

    async def test_rejoin_while_offline_does_not_resume_old_completed_answers(self):
        old = session("done")
        old["joined_at"] = "old membership"
        await self.service.save(self.member, old)
        thread = self.private_thread()
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        self.bot.get_channel.return_value = thread
        lobby = MagicMock(spec=discord.TextChannel)
        lobby.create_thread = AsyncMock(return_value=thread)
        self.guild.get_channel.return_value = lobby
        self.service.validate = MagicMock()
        await self.service.start(self.member)
        saved = await self.service.session(200, 100)
        self.assertEqual(saved["stage"], "gender")
        self.assertFalse(saved["answers"])
        self.member.add_roles.assert_not_awaited()

    def valid_guild(self):
        guild = self.guild
        guild.premium_tier = 2
        guild.default_role = self.roles[0]
        guild.me.roles = []
        guild.roles = list(self.roles.values())
        lobby = MagicMock(spec=discord.TextChannel)
        lobby.id, lobby.name, lobby.type = 30, "입장", discord.ChannelType.text
        full = discord.Permissions.all()
        lobby.permissions_for.return_value = full
        log = MagicMock(spec=discord.TextChannel)
        log.id, log.name, log.overwrites = 31, "기록", {}
        log.permissions_for.side_effect = lambda who: full if who is guild.me else discord.Permissions.none()
        log.overwrites_for.return_value = discord.PermissionOverwrite()
        channels = {30: lobby, 31: log}
        for i in range(1, 5):
            channel = MagicMock(spec=discord.TextChannel)
            channel.id, channel.name = 40 + i, f"소개{i}"
            channel.permissions_for.return_value = discord.Permissions.none()
            channel.overwrites_for.side_effect = lambda role: discord.PermissionOverwrite(view_channel=True) if role.id == 9 else discord.PermissionOverwrite()
            channels[channel.id] = channel
        guild.channels = list(channels.values())
        guild.get_channel.side_effect = channels.get
        return channels

    async def test_preflight_blocks_public_channels_early_access_and_missing_roles(self):
        channels = self.valid_guild()
        config = configured()
        self.service.validate(self.guild, config)
        channels[41].permissions_for.return_value = discord.Permissions.all()
        with self.assertRaisesRegex(ValueError, "다른 채널"):
            self.service.validate(self.guild, config)
        channels[41].permissions_for.return_value = discord.Permissions.none()
        channels[41].overwrites_for.side_effect = lambda role: discord.PermissionOverwrite(view_channel=True)
        with self.assertRaisesRegex(ValueError, "채널 열기"):
            self.service.validate(self.guild, config)
        self.valid_guild()
        self.roles.pop(3)
        with self.assertRaisesRegex(ValueError, "서버에 없습니다"):
            self.service.validate(self.guild, config)

    async def test_inspection_collects_all_missing_mappings_without_hierarchy_advice(self):
        self.valid_guild()
        config = configured()
        config["questions"]["gender"]["male"]["role_id"] = 0
        config["questions"]["year"]["2007"]["role_id"] = 12345
        config["introductions"].pop("slot3")
        original = copy.deepcopy(config)
        errors = self.service.inspect(self.guild, config)
        self.assertEqual(len(errors), 3)
        combined = "\n".join(errors)
        self.assertIn("성별 / 남자", combined)
        self.assertIn("출생연도 / 07년생", combined)
        self.assertIn("채널 소개 3번", combined)
        self.assertNotIn("위로", combined)
        self.assertEqual(config, original)

    async def test_hierarchy_and_managed_role_errors_are_distinct(self):
        self.roles[1].managed = True
        with self.assertRaisesRegex(ValueError, "연동 서비스"):
            self.service.role(self.guild, 1)
        self.roles[1].managed = False
        self.guild.me.top_role = Role(1)
        with self.assertRaisesRegex(ValueError, "최상위 역할"):
            self.service.role(self.guild, 1)

    async def test_check_reports_all_errors_in_bounded_pages(self):
        self.valid_guild()
        config = default_config()
        config['questions']['interests'] = {f'item{i}': {'label': f'관심사 {i}', 'role_id': 0} for i in range(25)}
        await self.service.save_config(200, config)
        interaction = self.interaction()
        cog = OnboardingCommands(self.service)
        await cog.check.callback(cog, interaction)
        pages = [c.args[0] for c in interaction.followup.send.await_args_list]
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 1800 for page in pages))
        report = "\n".join(pages)
        self.assertIn("07년생", report)
        self.assertIn("90년생", report)
        self.assertNotIn("위로", report)

    async def test_default_and_custom_display_order_preserved_after_relink(self):
        config = default_config()
        self.assertEqual([item['label'] for _, item in option_items(config, 'gender')], ['남자', '여자'])
        years = [item['label'] for _, item in option_items(config, 'year')]
        self.assertEqual(years[:3], ['07년생', '06년생', '05년생'])
        self.assertEqual(years[-1], '90년생')
        config = configured()
        config['enabled'] = False
        await self.service.save_config(200, config)
        cog = OnboardingCommands(self.service)
        interaction = self.interaction()
        await cog.link.callback(cog, interaction, '관심사', '음악', self.roles[5], 1)
        await cog.link.callback(cog, interaction, '관심사', '음악', self.roles[6])
        saved = await self.service.config(200)
        self.assertEqual(option_items(saved, 'interests')[0][1]['label'], '음악')
        self.assertEqual(saved['questions']['interests']['music']['order'], 1)
        self.assertEqual(saved['lobby_id'], config['lobby_id'])
        self.assertEqual(saved['introductions'], config['introductions'])

    async def test_thread_name_uses_user_nickname_and_korean_join_date(self):
        self.member.joined_at = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
        self.member.display_name = '가을'
        self.assertEqual(thread_name(self.member), '입장 : 가을/2026-09-22')
        self.member.display_name = '긴이름/' * 100
        name = thread_name(self.member)
        self.assertLessEqual(len(name), 100)
        self.assertEqual(name.count('/'), 1)
        self.assertTrue(name.endswith('/2026-09-22'))

    async def test_normal_selection_silently_updates_existing_message(self):
        await self.service.save(self.member, session())
        interaction = self.interaction()
        await self.service.on_interaction(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=False)
        interaction.followup.send.assert_not_awaited()
        self.service.render.assert_awaited_once()

    async def test_interests_are_draft_until_confirmed_and_gate_waits_for_review(self):
        state = session('interests')
        state['answers'] = {'gender': ['male'], 'year': ['2007']}
        await self.service.save(self.member, state)
        interaction = self.interaction('npcob:100:1:interests', ['game', 'music'])
        await self.service.on_interaction(interaction)
        saved = await self.service.session(200, 100)
        self.assertEqual(saved['stage'], 'interests')
        self.assertEqual(saved['draft_interests'], ['game', 'music'])
        self.member.add_roles.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        confirm = self.interaction('npcob:100:1:confirm_interests')
        await self.service.on_interaction(confirm)
        saved = await self.service.session(200, 100)
        self.assertEqual(saved['stage'], 'review')
        self.assertEqual(saved['answers']['interests'], ['game', 'music'])
        self.assertNotIn(9, {role.id for role in self.member.roles})
        confirm.followup.send.assert_not_awaited()
        await self.service.on_interaction(self.interaction('npcob:100:2:complete'))
        self.assertEqual((await self.service.session(200, 100))['stage'], 'tour')
        self.assertIn(9, {role.id for role in self.member.roles})

    async def test_draft_can_change_clear_and_survives_process_restart(self):
        state = session('interests')
        state['answers'] = {'gender': ['male'], 'year': ['2007']}
        await self.service.save(self.member, state)
        await self.service.on_interaction(self.interaction('npcob:100:1:interests', ['game']))
        await self.service.on_interaction(self.interaction('npcob:100:1:interests', ['music']))
        reboot = Onboarding(self.bot, self.init, self.store)
        reboot.render = AsyncMock()
        reboot.audit = AsyncMock()
        saved = await reboot.session(200, 100)
        view = reboot.view(100, saved)
        select = view.children[0]
        self.assertEqual([option.value for option in select.options if option.default], ['music'])
        self.assertFalse(next(item for item in view.children if item.custom_id.endswith('confirm_interests')).disabled)
        empty = self.interaction('npcob:100:1:interests')
        empty.data['values'] = []
        await reboot.on_interaction(empty)
        saved = await reboot.session(200, 100)
        self.assertTrue(next(item for item in reboot.view(100, saved).children if item.custom_id.endswith('confirm_interests')).disabled)
        await reboot.on_interaction(self.interaction('npcob:100:1:confirm_interests'))
        self.assertEqual((await reboot.session(200, 100))['stage'], 'interests')

    async def test_edit_gender_from_review_replaces_role_and_keeps_other_answers(self):
        state = session('review')
        state['answers'] = {'gender': ['male'], 'year': ['2007'], 'interests': ['game']}
        self.member.roles += [self.roles[1], self.roles[3], self.roles[4]]
        await self.service.save(self.member, state)
        await self.service.on_interaction(self.interaction('npcob:100:1:edit_gender'))
        await self.service.on_interaction(self.interaction('npcob:100:2:gender', ['female']))
        saved = await self.service.session(200, 100)
        self.assertEqual(saved['stage'], 'review')
        self.assertEqual(saved['answers'], {'gender': ['female'], 'year': ['2007'], 'interests': ['game']})
        self.assertEqual({r.id for r in self.member.roles}, {0, 2, 3, 4, 8})

    async def test_edit_year_from_tour_revokes_access_until_confirmed_again(self):
        state = session('tour')
        state['answers'] = {'gender': ['male'], 'year': ['2007'], 'interests': ['game']}
        self.member.roles += [self.roles[1], self.roles[3], self.roles[4], self.roles[9]]
        await self.service.save(self.member, state)
        await self.service.on_interaction(self.interaction('npcob:100:1:edit_year'))
        self.assertNotIn(9, {r.id for r in self.member.roles})
        await self.service.on_interaction(self.interaction('npcob:100:2:year', ['2007']))
        self.assertEqual((await self.service.session(200, 100))['stage'], 'review')
        self.assertNotIn(9, {r.id for r in self.member.roles})

    async def test_outside_age_has_contact_mention_and_no_back_button(self):
        thread = self.private_thread()
        self.bot.get_channel.return_value = thread
        state = session('reject_confirm')
        await Onboarding.render(self.service, self.member, state)
        message = await thread.fetch_message(state['message_id'])
        content = message.edit.await_args.kwargs['content']
        self.assertIn('\n\n입장 관련 문의는 <@300>', content)
        self.assertEqual([item.custom_id for item in self.service.view(100, state).children],
                         ['npcob:100:1:confirm'])

    async def test_question_messages_keep_common_intro_separate(self):
        thread = self.private_thread()
        self.bot.get_channel.return_value = thread
        for stage in ('gender', 'year', 'interests', 'review'):
            state = session(stage)
            await Onboarding.render(self.service, self.member, state)
            message = await thread.fetch_message(state['message_id'])
            content = message.edit.await_args.kwargs['content']
            self.assertTrue(content.startswith('환영합니다! 성별 → 출생연도 → 관심사 선택을 마치면 서버 채널을 이용할 수 있어요.\n\n'))

    async def test_edit_interest_replaces_confirmed_roles_only_after_confirm(self):
        state = session('review')
        state['answers'] = {'gender': ['male'], 'year': ['2007'], 'interests': ['game']}
        self.member.roles += [self.roles[1], self.roles[3], self.roles[4]]
        await self.service.save(self.member, state)
        await self.service.on_interaction(self.interaction('npcob:100:1:edit_interests'))
        await self.service.on_interaction(self.interaction('npcob:100:2:interests', ['music']))
        self.assertIn(4, {r.id for r in self.member.roles})
        await self.service.on_interaction(self.interaction('npcob:100:2:confirm_interests'))
        self.assertEqual({r.id for r in self.member.roles}, {0, 1, 3, 5, 8})

    async def test_completed_rejoin_reuses_thread_and_greets_without_old_answers(self):
        old = session('done')
        old['joined_at'] = '2026-09-20T00:00:00+00:00'
        old['answers'] = {'gender': ['male'], 'year': ['2007'], 'interests': ['game']}
        await self.service.save(self.member, old)
        self.member.joined_at = datetime(2026, 9, 22, tzinfo=timezone.utc)
        # Also remove roles restored by another integration on rejoin.
        self.member.roles += [self.roles[1], self.roles[9]]
        thread = self.private_thread()
        thread.archived = thread.locked = True
        self.bot.get_channel.return_value = thread
        self.service.validate = MagicMock()
        await self.service.start(self.member)
        saved = await self.service.session(200, 100)
        self.assertEqual(saved['thread_id'], old['thread_id'])
        self.assertEqual(saved['stage'], 'gender')
        self.assertFalse(saved['answers'])
        self.assertFalse(saved['message_id'])
        self.assertGreater(saved['revision'], old['revision'])
        self.assertEqual({r.id for r in self.member.roles}, {0, 8})
        self.assertIn('이전에 들어오신 기록', thread.send.await_args.args[0])
        thread.edit.assert_any_await(archived=False, locked=False)
        self.guild.get_channel.assert_not_called()
        await self.service.start(self.member)
        thread.send.assert_awaited_once()

    async def test_start_fetches_membership_instead_of_component_payload(self):
        old = session('year')
        old['joined_at'] = '2026-09-22T00:00:00+00:00'
        old['greeted'] = True
        await self.service.save(self.member, old)
        stale = MagicMock(spec=discord.Member)
        stale.id, stale.bot, stale.guild, stale.joined_at = 100, False, self.guild, None
        self.member.joined_at = datetime(2026, 9, 22, tzinfo=timezone.utc)
        thread = self.private_thread()
        self.bot.get_channel.return_value = thread
        await self.service.start(stale)
        self.assertEqual((await self.service.session(200, 100))['stage'], 'year')
        thread.send.assert_not_awaited()

    async def test_leave_marker_restarts_old_session_even_without_timestamp(self):
        old = session('rejected')
        await self.service.save(self.member, old)
        await self.service.on_leave(self.member)
        self.assertTrue((await self.service.session(200, 100))['left_at'])
        self.service.validate = MagicMock()
        self.bot.get_channel.return_value = self.private_thread()
        await self.service.start(self.member)
        saved = await self.service.session(200, 100)
        self.assertEqual(saved['stage'], 'gender')
        self.assertFalse(saved.get('left_at'))

    async def test_departed_member_cannot_submit_old_question(self):
        old = session('gender')
        old['left_at'] = 1
        await self.service.save(self.member, old)
        await self.service.on_interaction(self.interaction())
        self.member.add_roles.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
