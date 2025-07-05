import discord
from discord.ext import commands, tasks
import os
import json
import time
import random
import re
from datetime import datetime, timedelta, time as dtime, UTC
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db

# ---- Firebase 초기화 ----
load_dotenv()
firebase_key_json = os.getenv("FIREBASE_KEY_JSON")
firebase_key_dict = json.loads(firebase_key_json)
FIREBASE_DB_URL = "https://npc-bot-add0a-default-rtdb.firebaseio.com"
cred = credentials.Certificate(firebase_key_dict)
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_DB_URL
})

# ---- 설정 영역 ----
os.makedirs("data", exist_ok=True)
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

# ---- Firebase 핸들링 함수 ----
def load_exp_data():
    ref = db.reference("exp_data")
    return ref.get() or {}

def save_exp_data(data):
    ref = db.reference("exp_data")
    ref.set(data)

def load_mission_data():
    ref = db.reference("mission_data")
    return ref.get() or {}

def save_mission_data(data):
    ref = db.reference("mission_data")
    ref.set(data)

# ---- 유틸 ----
def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def calculate_level(exp):
    for level in range(1, 100):
        required = ((level * 30) + (level ** 2 * 7)) * 18
        if exp < required:
            return level
    return 99

def get_role_name_for_level(level):
    if level >= 1 and level <= 24:
        return 1386685631627006000
    elif level >= 25 and level <= 49:
        return 1386685631627005999
    elif level >= 50 and level <= 74:
        return 1386685631627005998
    elif level >= 75 and level <= 98:
        return 1386685631627005997
    elif level == 99:
        return 1386685631627005996
    return None

def generate_nickname(base_name, level):
    clean_base = re.sub(r"\s*\[ Lv\.?.? ?\.?\d+ ?~? ?\d*? ?\]", "", base_name)
    clean_base = re.sub(r"\s*\[ Lv \. \d+ \]", "", clean_base).strip()
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
            await channel.send(
            f"""환영합니다 {after.mention} 님! '사계절, 그 사이' 서버입니다.

저희 서버는 직접 닉네임을 변경할 수 있어요 !
프로필 우클릭-프로필-프로필 편집.

한글로만 구성된 닉네임으로 부탁드릴게요 !"""
        )
# ---- 미접속 인원 로그 태스크 ----
@tasks.loop(hours=24)
async def inactive_user_log_task():
    exp_data = load_exp_data()
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
    exp_data = load_exp_data()
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot or vc.id in AFK_CHANNEL_IDS:
                    continue
                user_id = str(member.id)
                user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
                if user_data.get("voice_minutes", 0) < MAX_VOICE_MINUTES:
                    gain = random.randint(VOICE_MIN_XP, VOICE_MAX_XP)
                    print(f"[음성] {member.display_name} +{gain}XP (총 {user_data['exp']}XP)")
                    user_data["exp"] += gain
                    user_data["voice_minutes"] += 1
                    user_data["last_activity"] = now_ts
                    new_level = calculate_level(user_data["exp"])
                    if new_level != user_data.get("level", 1):
                        user_data["level"] = new_level
                        role_id = get_role_name_for_level(new_level)
                        new_role = guild.get_role(role_id) if role_id else None
                        LEVEL_ROLE_IDS = [
                            1386685631627006000,
                            1386685631627005999,
                            1386685631627005998,
                            1386685631627005997,
                            1386685631627005996,
                        ]
                        for role in member.roles:
                            if role.id in LEVEL_ROLE_IDS:
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
                        save_exp_data(exp_data)
