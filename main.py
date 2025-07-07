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
firebase_key_json = os.getenv("FIREBASE_KEY_JSON")
try:
    firebase_key_dict = json.loads(firebase_key_json)
except json.decoder.JSONDecodeError:
    import ast
    firebase_key_dict = ast.literal_eval(firebase_key_json)
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
    
def save_user_exp(user_id, user_data):
    ref = db.reference("exp_data")
    ref.child(user_id).set(user_data)
    
def load_mission_data():
    ref = db.reference("mission_data")
    return ref.get() or {}

def save_mission_data(data):
    ref = db.reference("mission_data")
    ref.set(data)
    
def save_user_mission(user_id, user_mission):
    ref = db.reference("mission_data")
    ref.child(user_id).set(user_mission)
    
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
        # ---------- 여기부터 추가 ----------
        exp_data = load_exp_data()
        user_id = str(after.id)
        user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
        new_level = calculate_level(user_data["exp"])
        guild = after.guild
        role_id = get_role_name_for_level(new_level)
        new_role = guild.get_role(role_id) if role_id else None
        LEVEL_ROLE_IDS = [
            1386685631627006000,
            1386685631627005999,
            1386685631627005998,
            1386685631627005997,
            1386685631627005996,
        ]
        for role in after.roles:
            if role.id in LEVEL_ROLE_IDS:
                await after.remove_roles(role)
        if new_role:
            try:
                await after.add_roles(new_role)
            except:
                pass
        try:
            if after.id != guild.owner_id:
                await after.edit(nick=generate_nickname(after.display_name, new_level))
        except:
            pass
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


                    save_user_exp(user_id, user_data)


# ---- 반복 VC 미션 ----
@tasks.loop(seconds=60)
async def repeat_vc_mission_task():
    mission_data = load_mission_data()
    exp_data = load_exp_data()
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
                    user_exp["level"] = calculate_level(user_exp["exp"])
                    user_exp["last_activity"] = time.time()
                    exp_data[uid] = user_exp
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        await log_channel.send(f"[🧾 로그] {member.display_name} 님이 반복 VC 미션 완료! +{REPEAT_VC_EXP_REWARD}XP")
                mission_data[uid] = user_m
    save_mission_data(mission_data)
    save_exp_data(exp_data)

# ---- 메시지 이벤트 ----
@bot.event
async def on_message(message):
    if message.author.bot:
        return


    # ---- (정밀 패치) 특정 스레드 채팅 감지 시, 역할 자동 부여 ----
    if message.channel.id == 1389632514045251674:
        role_id = 1386685631580733541
        guild = message.guild
        member = message.author
        role = guild.get_role(role_id)
        if role and role not in member.roles:
            await member.add_roles(role)
        # 안내 메시지 없이 역할만 자동 부여

    exp_data = load_exp_data()
    user_id = str(message.author.id)
    user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
    now = time.time()
    last_time = user_data.get("last_activity", 0)
    if now - last_time >= COOLDOWN_SECONDS:
        gain = random.randint(1, 6)
        user_data["exp"] += gain
        user_data["last_activity"] = now
        print(f"[채팅] {message.author.display_name} +{gain}XP (총 {user_data['exp']}XP)")
        try:
            if message.author.id != message.guild.owner_id:
                await message.author.edit(nick=generate_nickname(message.author.display_name, user_data["level"]))
        except:
            pass
    new_level = calculate_level(user_data["exp"])
    if new_level != user_data["level"]:
        user_data["level"] = new_level
        guild = message.guild
        role_id = get_role_name_for_level(new_level)
        new_role = guild.get_role(role_id) if role_id else None
        LEVEL_ROLE_IDS = [
            1386685631627006000,
            1386685631627005999,
            1386685631627005998,
            1386685631627005997,
            1386685631627005996,
        ]
        for role in message.author.roles:
            if role.id in LEVEL_ROLE_IDS:
                await message.author.remove_roles(role)
        if new_role:
            try:
                await message.author.add_roles(new_role)
            except:
                pass

        try:
            if message.author.id != guild.owner_id:
                await message.author.edit(nick=generate_nickname(message.author.display_name, new_level))
        except:
            pass
        level_channel = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
        if level_channel:
            await level_channel.send(f"🎉 {message.author.mention} 님이 Lv.{new_level} 에 도달했습니다! 🎊")
    # === 기존: exp_data[user_id] = user_data
    # === 기존: save_exp_data(exp_data)
    # === 교체: 아래 한 줄
    save_user_exp(user_id, user_data)

    await bot.process_commands(message)
    # 텍스트 미션은 지정 채널에서만 집계
    if message.channel.id != TARGET_TEXT_CHANNEL_ID:
        return

    mission_data = load_mission_data()
    exp_data = load_exp_data()
    user_id = str(message.author.id)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    user_mission = mission_data.get(user_id, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})

    if user_mission["date"] != today:
        user_mission = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

    if not user_mission["text"]["completed"]:
        user_mission["text"]["count"] += 1
        if user_mission["text"]["count"] >= MISSION_REQUIRED_MESSAGES:
            user_exp = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
            user_exp["exp"] += MISSION_EXP_REWARD
            user_exp["level"] = calculate_level(user_exp["exp"])
            exp_data[user_id] = user_exp
            save_exp_data(exp_data)
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"[🧾 로그] {message.author.display_name} 님이 텍스트 일일 미션 완료! +{MISSION_EXP_REWARD}XP")
            await message.channel.send(f"🎯 {message.author.mention} 일일 미션 완료! +{MISSION_EXP_REWARD}XP 지급되었습니다.")
            user_mission["text"]["completed"] = True

    mission_data[user_id] = user_mission
    save_user_mission(user_id, user_mission)

    
    # ---- !경험치지급 / 차감 ----
@bot.command()
@commands.has_permissions(administrator=True)
async def 경험치지급(ctx, member: discord.Member, amount: int):
    exp_data = load_exp_data()
    user_id = str(member.id)
    user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
    previous_level = user_data["level"]
    user_data["exp"] += amount
    new_level = calculate_level(user_data["exp"])
    user_data["level"] = new_level

    if new_level > previous_level:
        guild = ctx.guild
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
    await ctx.send(f"✅ {member.mention}에게 경험치 {amount}XP 지급 완료!")
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(f"[🧾 로그] 관리자가 {member.display_name} 님에게 경험치 {amount}XP 지급")

@bot.command()
@commands.has_permissions(administrator=True)
async def 경험치차감(ctx, member: discord.Member, amount: int):
    exp_data = load_exp_data()
    user_id = str(member.id)
    user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
    user_data["exp"] = max(0, user_data["exp"] - amount)
    user_data["level"] = calculate_level(user_data["exp"])
    save_exp_data(exp_data)
    await ctx.send(f"✅ {member.mention}에게서 경험치 {amount}XP 차감 완료!")
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(f"[🧾 로그] 관리자가 {member.display_name} 님에게서 경험치 {amount}XP 차감")

# ---- !정보 ----
@bot.command()
async def 정보(ctx):
    user_id = str(ctx.author.id)
    exp_data = load_exp_data()
    user_data = exp_data.get(user_id, {"exp": 0, "level": 1, "voice_minutes": 0})
    current_exp = user_data["exp"]
    current_level = calculate_level(current_exp)
    next_level = current_level + 1

    # -- 누적 경험치 구간 산식 보정 --
    if current_level > 1:
        prev_required = ((current_level - 1) * 30) + ((current_level - 1) ** 2 * 7)
        prev_required *= 18
    else:
        prev_required = 0
    current_required = ((current_level * 30) + (current_level ** 2 * 7)) * 18

    remain_exp = max(0, current_required - current_exp)
    role_range = get_role_name_for_level(current_level)
    voice_minutes = user_data.get("voice_minutes", 0)

    delta = current_required - prev_required
    progress = current_exp - prev_required
    progress = max(0, progress)
    percent = (progress / delta) * 100 if delta > 0 else 0
    filled = int(percent / 5)
    empty = 20 - filled
    bar = "🟦" * filled + "⬜" * empty

    embed = discord.Embed(title=f"📊 {ctx.author.display_name}님의 정보", color=discord.Color.blue())
    embed.add_field(name="레벨", value=f"Lv. {current_level} (누적 경험치: {current_exp:,} XP)", inline=False)
    embed.add_field(name="경험치", value=f"{progress:,} / {delta:,} XP", inline=False)
    embed.add_field(name="경험치 진행도", value=f"{bar} ← {percent:.1f}%", inline=False)
    await ctx.send(embed=embed)


# ---- !퀘스트 ----
@bot.command()
async def 퀘스트(ctx):
    user_id = str(ctx.author.id)
    mission_data = load_mission_data()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    user_m = mission_data.get(user_id, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})

    if user_m.get("date") != today:
        user_m = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

    text_count = user_m["text"].get("count", 0)
    text_status = "✅ 완료" if user_m["text"].get("completed", False) else f"{text_count} / {MISSION_REQUIRED_MESSAGES} → 미완료"

    vc_minutes = user_m["repeat_vc"].get("minutes", 0)
    vc_rewards = vc_minutes // REPEAT_VC_REQUIRED_MINUTES
    vc_status = f"{vc_minutes}분 → {vc_rewards}회 보상 지급됨"

    embed = discord.Embed(title="📜 퀘스트 현황", color=discord.Color.green())
    embed.add_field(name="🗨️ 텍스트 미션 (일일)", value=text_status, inline=False)
    embed.add_field(name="🔁 반복 VC 미션 (누적)", value=vc_status, inline=False)
    await ctx.send(embed=embed)

# ---- !랭킹 ----
@bot.command()
async def 랭킹(ctx):
    exp_data = load_exp_data()
    sorted_data = sorted(exp_data.items(), key=lambda x: x[1].get("exp", 0), reverse=True)
    user_id = str(ctx.author.id)
    lines = []
    user_rank = None
    for i, (uid, data) in enumerate(sorted_data[:10], 1):
        try:
            member = await ctx.guild.fetch_member(int(uid))
            name = member.display_name
        except:
            name = "Unknown"
        lines.append(f"{i}위. {name} - Lv. {data.get('level', 1)} ({data.get('exp', 0)} XP)")
    for i, (uid, data) in enumerate(sorted_data, 1):
        if uid == user_id:
            user_rank = f"당신의 순위: {i}위 - Lv. {data.get('level', 1)} ({data.get('exp', 0)} XP)"
            break
    embed = discord.Embed(
        title="🏆 경험치 랭킹 (TOP 10)",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    if user_rank:
        embed.add_field(name="📍 현재 내 순위", value=user_rank, inline=False)
    await ctx.send(embed=embed)

# ---- 실행 ----
import threading
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_web():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_web).start()

bot.run(TOKEN)
