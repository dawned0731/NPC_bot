import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import time
import random
import re
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db
import pytz
import asyncio
from datetime import time as dtime



# ---- Firebase 초기화 ----
# 환경 변수에서 Firebase 키(JSON) 로드
load_dotenv()
firebase_key_json = os.getenv("FIREBASE_KEY_JSON")
try:
    firebase_key_dict = json.loads(firebase_key_json)
except json.decoder.JSONDecodeError:
    import ast
    firebase_key_dict = ast.literal_eval(firebase_key_json)

# Realtime Database URL 설정 및 초기화
FIREBASE_DB_URL = "https://npc-bot-add0a-default-rtdb.firebaseio.com"
cred = credentials.Certificate(firebase_key_dict)
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_DB_URL
})

# ---- 설정 영역 ----
# Discord 봇 토큰 및 슬래시 커맨드 동기화를 위한 길드 ID
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))

# 로컬 데이터 디렉토리 생성
os.makedirs("data", exist_ok=True)

# 파일 및 채널, 쿨다운 등 상수 정의
EXP_PATH = "data/exp.json"
MISSION_PATH = "data/mission.json"
LOG_CHANNEL_ID = 1386685633136820248
LEVELUP_ANNOUNCE_CHANNEL = 1386685634462093332
TARGET_TEXT_CHANNEL_ID = 1386685633413775416
THREAD_ROLE_CHANNEL_ID = 1389632514045251674
THREAD_ROLE_ID = 1386685631580733541
COOLDOWN_SECONDS = 5
VOICE_COOLDOWN = 60
VOICE_MIN_XP = 20
VOICE_MAX_XP = 30
AFK_CHANNEL_IDS = [1386685633820495994]
MISSION_EXP_REWARD = 100
MISSION_REQUIRED_MESSAGES = 30
REPEAT_VC_EXP_REWARD = 100
REPEAT_VC_REQUIRED_MINUTES = 15
REPEAT_VC_MIN_PEOPLE = 5
SPECIAL_VC_CATEGORY_IDS = [1386685633820495991]
ATTENDANCE_DB_KEY = "attendance_data"

# KST 타임존 객체
KST = pytz.timezone("Asia/Seoul")


# ---- Firebase 핸들링 함수 ----
def load_exp_data():
    """사용자 경험치 데이터를 Realtime DB에서 가져옵니다."""
    return db.reference("exp_data").get() or {}


def save_exp_data(data):
    """전체 경험치 데이터를 Realtime DB에 저장합니다."""
    try:
        db.reference("exp_data").set(data)
    except Exception as e:
        print(f"❌ save_exp_data 실패: {e}")

def save_user_exp(user_id, user_data):
    """특정 사용자 경험치 데이터를 Realtime DB에 저장합니다."""
    try:
        db.reference("exp_data").child(user_id).set(user_data)
    except Exception as e:
        print(f"❌ save_user_exp 실패: {e}")

def load_mission_data():
    """일일 미션 데이터 로드"""
    return db.reference("mission_data").get() or {}


def save_mission_data(data):
    """전체 미션 데이터를 저장"""
    try:
        db.reference("mission_data").set(data)
    except Exception as e:
        print(f"❌ save_mission_data 실패: {e}")

def save_user_mission(user_id, user_mission):
    """특정 사용자 미션 데이터 저장"""
    try:
        db.reference("mission_data").child(user_id).set(user_mission)
    except Exception as e:
        print(f"❌ save_user_mission 실패: {e}")

def get_attendance_data():
    """출석 데이터를 불러옵니다."""
    return db.reference(ATTENDANCE_DB_KEY).get() or {}


def set_attendance_data(user_id, data):
    """출석 데이터 저장"""
    try:
        db.reference(ATTENDANCE_DB_KEY).child(user_id).set(data)
    except Exception as e:
        print(f"❌ set_attendance_data 실패: {e}")

def load_json(path):
    """로컬 JSON 파일 로드 (없으면 빈 dict)"""
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)


def save_json(path, data):
    """로컬 JSON 파일 저장"""
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


# ---- 유틸 함수 ----
def calculate_level(exp):
    """누적 경험치로부터 레벨을 계산"""
    for lvl in range(1, 100):
        required = ((lvl * 30) + (lvl ** 2 * 7)) * 18
        if exp < required:
            return lvl
    return 99


# 레벨별 역할 ID 리스트
ROLE_IDS = [
    1386685631627006000,
    1386685631627005999,
    1386685631627005998,
    1386685631627005997,
    1386685631627005996,
]

def get_role_for_level(level):
    """레벨 범위에 따라 역할 ID 반환"""
    if level <= 24:
        return ROLE_IDS[0]
    elif level <= 49:
        return ROLE_IDS[1]
    elif level <= 74:
        return ROLE_IDS[2]
    elif level <= 98:
        return ROLE_IDS[3]
    else:
        return ROLE_IDS[4]


def generate_nickname(base, level):
    """기존 닉네임에서 레벨 태그를 제거하고 새롭게 추가"""
    clean = re.sub(r"\s*\[ Lv.*?\]", '', base).strip()
    tag = f" [ Lv . {level} ]"
    nickname = clean + tag
    return nickname[:32]
from datetime import datetime, timedelta

def get_week_key_kst(dt: datetime) -> str:
    """
    주 단위 키를 ISO 형식으로 반환합니다. 
    예: 2025년 7월 22일 → '2025-W29'
    """
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"

def get_month_key_kst(dt: datetime) -> str:
    """
    월 단위 키를 'YYYY-M' 형식으로 반환합니다.
    예: 2025년 7월 → '2025-7'
    """
    # 한 자리 월에는 앞에 ‘0’을 붙이지 않음
    return f"{dt.year}-{dt.month}"


# 최근 역할·닉네임 업데이트한 유저를 추적해 rate-limit 방지
recent_role_updates: set[int] = set()


# ─── 데바운스 적용 헬퍼 함수 추가 ────────────────────────────

async def update_role_and_nick(member: discord.Member, new_level: int):
    """
    역할·닉네임 변경을 5분에 한 번만 수행하도록 데바운스 처리합니다.
    """
    uid = member.id
    if uid in recent_role_updates:
        return  # 이미 5분 이내에 업데이트 했으므로 스킵

    recent_role_updates.add(uid)
    asyncio.get_event_loop().call_later(300, recent_role_updates.remove, uid)

    # 1) 기존 레벨 역할 제거
    for role in member.roles:
        if role.id in ROLE_IDS:
            try:
                await member.remove_roles(role)
            except:
                pass

    # 2) 새 역할 부여
    role_id = get_role_for_level(new_level)
    new_role = member.guild.get_role(role_id)
    if new_role:
        try:
            await member.add_roles(new_role)
        except:
            pass

    # 3) 닉네임 업데이트
    if member.id != member.guild.owner_id:
        try:
            await member.edit(nick=generate_nickname(member.display_name, new_level))
        except:
            pass
# ────────────────────────────────────────────────────────────

 # ---- Discord Bot 초기화 (슬래시 전용) ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=None,     # 프리픽스 명령어 비활성화
    help_command=None,      # 기본 도움말 명령어 비활성화
    intents=intents
)


# ---- on_ready ----
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 온라인")
    # 슬래시 커맨드 동기화
    try:
        synced = await bot.tree.sync()
        print(f"🌐 전역 슬래시 커맨드 {len(synced)}개 동기화 완료")
            await bot.tree.sync()
        print("🔄 슬래시 커맨드 동기화 완료")
    except Exception as e:
        # 환경 변수 누락 혹은 429 에러 등 처리
        print(f"❌ 슬래시 커맨드 동기화 실패: {e}")

    # 백그라운드 태스크 시작
    voice_xp_task.start()
    reset_daily_missions.start()
    repeat_vc_mission_task.start()
    inactive_user_log_task.start()


# ---- on_member_update: 환영 메시지 및 역할 동기화 ----
@bot.event
async def on_member_update(before, after):
    before_roles = set(r.id for r in before.roles)
    after_roles = set(r.id for r in after.roles)
    added = after_roles - before_roles

    # 특정 스레드 역할이 부여되면 환영 메시지
    if THREAD_ROLE_ID in added:
        channel = bot.get_channel(TARGET_TEXT_CHANNEL_ID)
        if channel:
            await channel.send(
                f"환영합니다 {after.mention} 님! '사계절, 그 사이' 서버입니다.\n"
                "프로필 우클릭 → 편집으로 닉네임을 변경할 수 있어요!"
            )

        # DB에서 경험치, 레벨 로드 후 역할/닉네임 동기화
        exp_data = load_exp_data()
        uid = str(after.id)
        user_data = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
        new_level = calculate_level(user_data["exp"])
        
        # 기존 레벨 역할 제거
        for role in after.roles:
            if role.id in ROLE_IDS:
                await after.remove_roles(role)
        
        # 새 역할 부여
        role_id = get_role_for_level(new_level)
        new_role = after.guild.get_role(role_id)
        if new_role:
            await after.add_roles(new_role)
        
        # 닉네임 갱신
        if after.id != after.guild.owner_id:
            await after.edit(nick=generate_nickname(after.display_name, new_level))


# ---- 백그라운드 태스크 정의 ----
@tasks.loop(hours=24)
async def inactive_user_log_task():
    """5일 미접속 사용자 로그"""
    exp_data = load_exp_data()
    # UTC 타임스탬프 기준 5일 전
    threshold = datetime.now(KST) - timedelta(days=5)
    log_channel = bot.get_channel(LOG_CHANNEL_ID)

    if not log_channel:
        return

    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            user = exp_data.get(str(member.id))
            if user and user.get("last_activity"):
                # UTC 기준 timestamp 변환
                last_active = datetime.fromtimestamp(user["last_activity"], KST)
                if last_active < threshold:
                    await log_channel.send(f"{member.display_name} 님 5일 미접 상태입니다.")


@tasks.loop(time=dtime(hour=15, minute=0))

async def reset_daily_missions():
    """일일 미션 데이터 초기화 (로컬 및 DB)"""
    try:
        # 로컬 파일 초기화
        save_json(MISSION_PATH, {})
        # Realtime DB의 mission_data 노드 초기화
        db.reference("mission_data").set({})
        print("🔁 일일 미션 초기화 완료")
    except Exception as e:
        # 오류 발생 시 로그 채널에 알림하거나 콘솔에 에러 기록
        print(f"❌ 일일 미션 초기화 실패: {e}")


@tasks.loop(seconds=VOICE_COOLDOWN)
async def voice_xp_task():
    """음성 채널 경험치 태스크"""
    now_ts = time.time()
    exp_data = load_exp_data()

    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if vc.id in AFK_CHANNEL_IDS:
                continue

            is_special = vc.category and vc.category.id in SPECIAL_VC_CATEGORY_IDS
            for member in vc.members:
                if member.bot:
                    continue

                uid = str(member.id)
                user_data = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
                gain = random.randint(VOICE_MIN_XP, VOICE_MAX_XP)
                if is_special:
                    gain = max(1, int(gain * 0.2))

                user_data["exp"] += gain
                if not is_special:
                    user_data["voice_minutes"] += 1

                user_data["last_activity"] = now_ts
                new_level = calculate_level(user_data["exp"])

                if new_level != user_data["level"]:
                    user_data["level"] = new_level

                    # 역할·닉네임 변경 (데바운스 적용)
                    await update_role_and_nick(member, new_level)

                    # 레벨업 알림 유지
                    announce = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
                    if announce:
                        await announce.send(f"🎉 {member.mention} 님이 Lv.{new_level} 에 도달했습니다! 🎊")

                save_user_exp(uid, user_data)


@tasks.loop(seconds=60)
async def repeat_vc_mission_task():
    """반복 VC 미션 보상 태스크"""
    mission_data = load_mission_data()
    exp_data = load_exp_data()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if vc.id in AFK_CHANNEL_IDS or len(vc.members) < REPEAT_VC_MIN_PEOPLE:
                continue

            for member in vc.members:
                if member.bot:
                    continue

                uid = str(member.id)
                user_m = mission_data.get(uid, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})
                if user_m["date"] != today:
                    user_m = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

                user_m["repeat_vc"]["minutes"] += 1
                if user_m["repeat_vc"]["minutes"] % REPEAT_VC_REQUIRED_MINUTES == 0:
                    uexp = exp_data.get(uid, {"exp": 0, "level": 1})
                    uexp["exp"] += REPEAT_VC_EXP_REWARD
                    uexp["level"] = calculate_level(uexp["exp"])
                    uexp["last_activity"] = time.time()
                    exp_data[uid] = uexp

                    log = bot.get_channel(LOG_CHANNEL_ID)
                    if log:
                        await log.send(f"[🧾 로그] {member.display_name} 님이 반복 VC 미션 완료! +{REPEAT_VC_EXP_REWARD}XP")

                mission_data[uid] = user_m

    save_mission_data(mission_data)
    # 로컬 JSON에도 백업
    try:
        save_json(MISSION_PATH, mission_data)
    except Exception as e:
        print(f"❌ 미션 로컬 백업 실패: {e}")
    save_exp_data(exp_data)


@bot.event
async def on_message(message):
    try:
        if message.author.bot:
            return

        # 1) 특정 스레드 채팅 감지 시 역할 자동 부여
        if message.channel.id == THREAD_ROLE_CHANNEL_ID:
            role = message.guild.get_role(THREAD_ROLE_ID)
            if role and role not in message.author.roles:
                await message.author.add_roles(role)

        # 2) 채팅 경험치 처리 로직
        exp_data = load_exp_data()
        uid = str(message.author.id)
        user_data = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
        now_ts = time.time()

        if now_ts - user_data.get("last_activity", 0) >= COOLDOWN_SECONDS:
            gain = random.randint(1, 6)
            user_data["exp"] += gain
            user_data["last_activity"] = now_ts
            try:
                if message.author.id != message.guild.owner_id:
                    await message.author.edit(nick=generate_nickname(message.author.display_name, user_data["level"]))
            except:
                pass

        # 3) 레벨업 분기
        new_level = calculate_level(user_data["exp"])
        if new_level != user_data["level"]:
            user_data["level"] = new_level
            await update_role_and_nick(message.author, new_level)

        save_user_exp(uid, user_data)

        # 4) 텍스트 미션 집계 (지정 채널만)
        if message.channel.id == TARGET_TEXT_CHANNEL_ID:
            mission_data = load_mission_data()
            exp_data = load_exp_data()
            uid = str(message.author.id)
            today = datetime.now(KST).strftime("%Y-%m-%d")
            user_m = mission_data.get(uid, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})

            if user_m["date"] != today:
                user_m = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

            if not user_m["text"]["completed"]:
                user_m["text"]["count"] += 1
                if user_m["text"]["count"] >= MISSION_REQUIRED_MESSAGES:
                    ue = exp_data.get(uid, {"exp": 0, "level": 1})
                    ue["exp"] += MISSION_EXP_REWARD
                    ue["level"] = calculate_level(ue["exp"])
                    exp_data[uid] = ue
                    save_exp_data(exp_data)

                    log_ch = bot.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        await log_ch.send(f"[🧾 로그] {message.author.display_name} 님 텍스트 미션 완료! +{MISSION_EXP_REWARD}XP")
                    await message.channel.send(f"🎯 {message.author.mention} 일일 미션 완료! +{MISSION_EXP_REWARD}XP 지급되었습니다.")
                    user_m["text"]["completed"] = True

            mission_data[uid] = user_m
            save_user_mission(uid, user_m)
            try:
                missions = load_mission_data()
                save_json(MISSION_PATH, missions)
            except Exception as e:
                print(f"❌ 미션 로컬 백업 실패: {e}")

    except Exception as e:
        print(f"❌ on_message 에러: {e}")


# ---- 슬래시 관리자 명령어 ----
@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="경험치지급", description="유저에게 경험치를 지급합니다.")
async def grant_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    exp_data = load_exp_data()
    uid = str(member.id)
    user_data = exp_data.get(uid, {"exp": 0, "level": 1})
    prev_level = user_data["level"]
    user_data["exp"] += amount
    new_level = calculate_level(user_data["exp"])
    user_data["level"] = new_level

    if new_level > prev_level:
        # 역할·닉네임 변경 (데바운스 적용)
        await update_role_and_nick(member, new_level)
        # 레벨업 알림
        ch_log = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
        if ch_log:
            await ch_log.send(f"🎉 {member.mention} 님이 Lv.{new_level} 에 도달했습니다! 🎊")

    save_user_exp(uid, user_data)
    await interaction.response.send_message(f"✅ {member.mention}에게 경험치 {amount}XP 지급 완료!")


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="경험치차감", description="유저의 경험치를 차감합니다.")
async def deduct_xp(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: int
):
    # 데이터 로드
    exp_data = load_exp_data()
    uid = str(member.id)
    user_data = exp_data.get(uid, {"exp": 0, "level": 1})

    # 경험치 차감 및 레벨 재계산
    user_data["exp"] = max(0, user_data["exp"] - amount)
    user_data["level"] = calculate_level(user_data["exp"])

    # DB 저장
    save_user_exp(uid, user_data)

    # 역할·닉네임 변경 (데바운스 적용)
    await update_role_and_nick(member, user_data["level"])

    await interaction.response.send_message(
        f"✅ {member.mention}에게서 경험치 {amount}XP 차감 완료!"
    )
# ---- 기타 슬래시 커맨드 핸들러 (/정보, /퀘스트, /랭킹, /출석, /출석랭킹) ----

@bot.tree.command(name="정보", description="자신의 레벨 및 경험치 정보를 확인합니다.")
async def info(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    exp_data = load_exp_data()
    user = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
    current_exp = user["exp"]
    lvl = calculate_level(current_exp)
    # 이전 !정보 임베드 로직 그대로 사용
    if lvl > 1:
        prev_req = ((lvl - 1) * 30 + (lvl - 1) ** 2 * 7) * 18
    else:
        prev_req = 0
    curr_req = ((lvl * 30) + lvl ** 2 * 7) * 18
    progress = max(0, current_exp - prev_req)
    total = curr_req - prev_req
    percent = (progress / total) * 100 if total else 0
    filled = int(percent / 5)
    bar = "🟦" * filled + "⬜" * (20 - filled)
    embed = discord.Embed(title=f"📊 {interaction.user.display_name}님의 정보", color=discord.Color.blue())
    embed.add_field(name="레벨", value=f"Lv. {lvl} (누적: {current_exp:,} XP)", inline=False)
    embed.add_field(name="경험치", value=f"{progress:,} / {total:,} XP", inline=False)
    embed.add_field(name="진행도", value=f"{bar} ← {percent:.1f}%", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="퀘스트", description="일일 및 반복 VC 퀘스트 현황을 확인합니다.")
async def quest(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    missions = load_mission_data()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    um = missions.get(uid, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})
    if um.get("date") != today:
        um = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}
    text_count = um["text"]["count"]
    text_status = "✅ 완료" if um["text"]["completed"] else f"{text_count} / {MISSION_REQUIRED_MESSAGES} → 미완료"
    vc_minutes = um["repeat_vc"]["minutes"]
    vc_rewards = vc_minutes // REPEAT_VC_REQUIRED_MINUTES
    vc_status = f"{vc_minutes}분 → {vc_rewards}회 보상 지급"
    embed = discord.Embed(title="📜 퀘스트 현황", color=discord.Color.green())
    embed.add_field(name="🗨️ 텍스트 미션", value=text_status, inline=False)
    embed.add_field(name="🔁 반복 VC 미션", value=vc_status, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="랭킹", description="경험치 랭킹을 확인합니다.")
async def ranking(interaction: discord.Interaction):
    exp_data = load_exp_data()
    # 경험치 기준 상위 10명 정렬
    sorted_users = sorted(exp_data.items(), key=lambda x: x[1].get("exp", 0), reverse=True)
    
    desc_lines = []
    for idx, (uid, data) in enumerate(sorted_users[:10], start=1):
        try:
            member = await interaction.guild.fetch_member(int(uid))
            name = member.display_name
        except:
            name = "Unknown"
        desc_lines.append(f"{idx}위. {name} - Lv. {data.get('level',1)} ({data.get('exp',0)} XP)")
    
    # 내 순위 찾기
    my_rank = None
    for idx, (uid, data) in enumerate(sorted_users, start=1):
        if uid == str(interaction.user.id):
            my_rank = f"당신의 순위: {idx}위 - Lv. {data.get('level',1)} ({data.get('exp',0)} XP)"
            break

    # Embed 생성
    embed = discord.Embed(
        title="🏆 경험치 랭킹",
        description="\n".join(desc_lines),
        color=discord.Color.gold()
    )
    if my_rank:
        embed.add_field(name="📍 내 순위", value=my_rank, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="출석", description="오늘의 출석을 기록합니다.")
async def attend(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week = get_week_key_kst(now)
    month = get_month_key_kst(now)
    data = get_attendance_data()
    ud = data.get(uid, {"last_date":"","total_days":0,"streak":0,"weekly":{},"monthly":{}})
    if ud["last_date"] == today_str:
        until = (now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)) - now
        h, m = divmod(int(until.total_seconds()/60), 60)
        return await interaction.response.send_message(f"이미 출석 완료! 다음 출석까지 {h}시간 {m}분 남음.")
    ud["streak"] = ud["streak"] + 1 if ud["last_date"] == yesterday else 1
    ud["last_date"] = today_str
    ud["total_days"] += 1
    ud.setdefault("weekly", {})[week] = ud["weekly"].get(week,0)+1
    ud.setdefault("monthly", {})[month] = ud["monthly"].get(month,0)+1
    # 경험치 지급
    gain = min(200, 100+(min(ud["streak"],10)-1)*10)
    expd = load_exp_data()
    ue = expd.get(uid,{"exp":0,"level":1,"voice_minutes":0})
    ue["exp"] += gain
    ue["level"] = calculate_level(ue["exp"])
    save_user_exp(uid, ue)
    set_attendance_data(uid, ud)
    msg = f"🎉 누적 {ud['total_days']}일 / 연속 {ud['streak']}일 출석! +{gain}XP"
    await interaction.response.send_message(msg)

@bot.tree.command(name="출석랭킹", description="출석 랭킹을 확인합니다.")
async def attend_ranking(interaction: discord.Interaction):
    data = get_attendance_data()
    # 총 출석일, 연속 출석일 순으로 정렬
    ranked = sorted(
        data.items(),
        key=lambda x: (-x[1].get("total_days", 0), -x[1].get("streak", 0))
    )

    # 상위 10명 라인 생성
    lines = []
    for idx, (uid, ud) in enumerate(ranked[:10], start=1):
        try:
            member = await interaction.guild.fetch_member(int(uid))
            name = member.display_name
        except:
            name = "Unknown"
        lines.append(f"{idx}위. {name} - 누적 {ud.get('total_days', 0)}일 / 연속 {ud.get('streak', 0)}일")

    # 내 순위 찾기
    my_rank = None
    for idx, (uid, ud) in enumerate(ranked, start=1):
        if uid == str(interaction.user.id):
            my_rank = f"당신의 순위: {idx}위"
            break

    # Embed 생성 (description에 "\n".join 사용)
    embed = discord.Embed(
        title="🏅 출석 랭킹",
        description="\n".join(lines),
        color=discord.Color.blue()
    )
    if my_rank:
        embed.add_field(name="📍 내 순위", value=my_rank, inline=False)

    await interaction.response.send_message(embed=embed)

# ---- 실행 및 웹 서버 유지 ----
import threading
from flask import Flask
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

# Flask 웹 서버를 별도 스레드로 실행
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=10000)).start()

# Discord Bot 실행
bot.run(TOKEN)
