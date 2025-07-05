import discord
from discord.ext import commands, tasks
import os
import json
import time
import random
import re
from datetime import datetime, timedelta, time as dtime, UTC
from dotenv import load_dotenv
from keep_alive import keep_alive

# ---- 설정 영역 ----
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# ---- 경로 및 상수 ----
EXP_PATH = "data/exp.json"
MISSION_PATH = "data/mission.json"
LOG_CHANNEL_ID = 1386685633136820248
COOLDOWN_SECONDS = 5
VOICE_COOLDOWN = 60
VOICE_MIN_XP = 20
VOICE_MAX_XP = 30
MAX_VOICE_MINUTES = 600
AFK_CHANNEL_IDS = [1386685633820495994]
LEVELUP_ANNOUNCE_CHANNEL = 1386685634462093332
TARGET_TEXT_CHANNEL_ID = 1386685633413775416
MISSION_EXP_REWARD = 100
MISSION_REQUIRED_MESSAGES = 30
REPEAT_VC_EXP_REWARD = 100
REPEAT_VC_REQUIRED_MINUTES = 15
REPEAT_VC_MIN_PEOPLE = 5

# ---- 유틸 ----
def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def calculate_level(exp):
    for level in range(1, 100):
        required = ((level * 30) + (level ** 2 * 7)) * 18
        if exp < required:
            return level
    return 99

def get_role_name_for_level(level):
    lower = ((level - 1) // 10) * 10 + 1
    upper = min(lower + 9, 99)
    return f"[ Lv. {lower} ~ {upper} ]"

def generate_nickname(base_name, level):
    clean_base = re.sub(r"\\s*\\[ Lv\\.?.? ?\\.?\\d+ ?~? ?\\d*? ?\\]", "", base_name)
    clean_base = re.sub(r"\\s*\\[ Lv \\. \\d+ \\]", "", clean_base).strip()
    new_nick = f"{clean_base} [ Lv . {level} ]"
    return new_nick if len(new_nick) <= 32 else clean_base[:32 - len(f" [ Lv . {level} ]")] + f" [ Lv . {level} ]"

# ---- 역할 부여 감지 ----
@bot.event
async def on_member_update(before, after):
    before_roles = set(r.id for r in before.roles)
    after_roles = set(r.id for r in after.roles)
    added_roles = after_roles - before_roles
    if 1386685631580733541 in added_roles:
        channel = bot.get_channel(1386685633413775416)
        if channel:
            await channel.send(f"{after.mention} 님 어서오세요! '사계절, 그 사이' 서버에 오신 것을 환영합니다!")

# ---- 미접속 인원 로그 태스크 ----
@tasks.loop(hours=24)
async def inactive_user_log_task():
    exp_data = load_json(EXP_PATH)
    now = datetime.now()
    threshold = now - timedelta(days=5)
    log_channel = bot.get_channel(1386685633136820247)
    if not log_channel:
        return
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            user_id = str(member.id)
            user_data = exp_data.get(user_id)
            if not user_data:
                continue
            last_ts = user_data.get("last_activity")
            if not last_ts:
                continue
            last_active = datetime.fromtimestamp(last_ts)
            if last_active < threshold:
                await log_channel.send(f"{member.display_name} 님 5일 미접 상태입니다.")

# ---- on_ready ----
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 가 온라인 상태입니다.")
    voice_xp_task.start()
    reset_daily_missions.start()
    repeat_vc_mission_task.start()
    inactive_user_log_task.start()

# ---- 일일 미션 초기화 ----
@tasks.loop(time=dtime(hour=0, minute=0))
async def reset_daily_missions():
    save_json(MISSION_PATH, {})
    print("🔁 일일 미션 초기화 완료")

# ---- 음성 경험치 태스크 ----
@tasks.loop(seconds=VOICE_COOLDOWN)
async def voice_xp_task():
    now_ts = time.time()
    exp_data = load_json(EXP_PATH)
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot or vc.id in AFK_CHANNEL_IDS:
                    continue
                user_id = str(member.id)
                user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
                if user_data.get("voice_minutes", 0) < MAX_VOICE_MINUTES:
                    gain = random.randint(VOICE_MIN_XP, VOICE_MAX_XP)
                    user_data["exp"] += gain
                    user_data["voice_minutes"] += 1
                    user_data["last_activity"] = now_ts
                    new_level = calculate_level(user_data["exp"])
                    if new_level != user_data.get("level", 1):
                        user_data["level"] = new_level
                        new_role = discord.utils.get(guild.roles, name=get_role_name_for_level(new_level))
                        for role in member.roles:
                            if role.name.startswith("[ Lv."):
                                await member.remove_roles(role)
                        if new_role:
                            try:
                                await member.add_roles(new_role)
                            except:
                                pass
                        try:
                            if member.id != guild.owner_id:
                                await member.edit(nick=generate_nickname(member.display_name, new_level))
                        except:
                            pass
                        channel = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
                        if channel:
                            await channel.send(f"🎉 {member.mention} 님이 Lv.{new_level} 에 도달했습니다! 🎊")
                    exp_data[user_id] = user_data
    save_json(EXP_PATH, exp_data)

# ---- 반복 VC 미션 ----
@tasks.loop(seconds=60)
async def repeat_vc_mission_task():
    mission_data = load_json(MISSION_PATH)
    exp_data = load_json(EXP_PATH)
    now = datetime.now(UTC).strftime("%Y-%m-%d")
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if vc.id in AFK_CHANNEL_IDS or len(vc.members) < REPEAT_VC_MIN_PEOPLE:
                continue
            for member in vc.members:
                if member.bot:
                    continue
                uid = str(member.id)
                user_m = mission_data.get(uid, {"date": now, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})
                if user_m.get("date") != now:
                    user_m = {"date": now, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}
                user_m["repeat_vc"]["minutes"] += 1
                if user_m["repeat_vc"]["minutes"] % REPEAT_VC_REQUIRED_MINUTES == 0:
                    user_exp = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
                    user_exp["exp"] += REPEAT_VC_EXP_REWARD
                    user_exp["last_activity"] = time.time()
                    exp_data[uid] = user_exp
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        await log_channel.send(f"[🧾 로그] {member.display_name} 님이 반복 VC 미션 완료! +{REPEAT_VC_EXP_REWARD}XP")
                mission_data[uid] = user_m
    save_json(MISSION_PATH, mission_data)
    save_json(EXP_PATH, exp_data)

# ---- 실행 ----
from keep_alive import keep_alive

keep_alive()
bot.run(TOKEN)
