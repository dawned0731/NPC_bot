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
from threading import Thread
import logging, sys


# ---- Firebase 초기화 ----
# 환경 변수에서 Firebase 키(JSON) 로드
load_dotenv()
firebase_key_json = os.getenv("FIREBASE_KEY_JSON")
# === fail-fast: Firebase 키 없으면 즉시 종료 ===
if not firebase_key_json:
    raise RuntimeError("FIREBASE_KEY_JSON 환경변수가 설정되어 있지 않습니다.")
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
EXEMPT_ROLE_IDS = [
    1391063915655331942,  # 예외 역할 : 관리자
    1410180795938771066,  # 예외 역할 : 추방 방지
]
# Discord 봇 토큰 및 슬래시 커맨드 동기화를 위한 길드 ID
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
# === fail-fast: 토큰 없으면 즉시 종료 ===
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되어 있지 않습니다.")

# ---- 역할별 인원수를 음성 채널 이름으로 실시간 반영 ----

SEASON_ROLE_CHANNEL_MAP = {
    "봄": (1386685631551246426, 1401854813356036196),
    "여름": (1386685631551246425, 1401854844628893718),
    "가을": (1386685631551246424, 1401854913117687889),
    "겨울": (1386685631551246423, 1401854945547915316),
}

async def update_season_voice_channels():
    for guild in bot.guilds:
        for season, (role_id, channel_id) in SEASON_ROLE_CHANNEL_MAP.items():
            role = guild.get_role(role_id)
            channel = guild.get_channel(channel_id)
            if role and channel:
                count = len(role.members)
                new_name = f"[{season}], 그 사이의 {count}명"
                if channel.name != new_name:
                    try:
                        await channel.edit(name=new_name)
                    except Exception as e:
                        print(f"❌ 채널 이름 변경 실패 ({season}): {e}")


# 로컬 데이터 디렉토리 생성
os.makedirs("data", exist_ok=True)

# 파일 및 채널, 쿨다운 등 상수 정의
EXP_PATH = "data/exp.json"
MISSION_PATH = "data/mission.json"
LOG_CHANNEL_ID = 1386685633136820248
INACTIVE_LOG_CHANNEL_ID = 1386685633136820247
LEVELUP_ANNOUNCE_CHANNEL = 1386685634462093332
TARGET_TEXT_CHANNEL_ID = 1386685633413775416
THREAD_ROLE_CHANNEL_ID = 1386685633413775416
THREAD_ROLE_ID = 1386685631580733541
COOLDOWN_SECONDS = 5
VOICE_COOLDOWN = 60
VOICE_MIN_XP = 10
VOICE_MAX_XP = 50
AFK_CHANNEL_IDS = [1386685633820495994]
MISSION_EXP_REWARD = 100
MISSION_REQUIRED_MESSAGES = 30
REPEAT_VC_EXP_REWARD = 100
REPEAT_VC_REQUIRED_MINUTES = 15
REPEAT_VC_MIN_PEOPLE = 5
SPECIAL_VC_CATEGORY_IDS = [1386685633820495991]
ATTENDANCE_DB_KEY = "attendance_data"
HIDDEN_QUEST_KEY = "hidden_quest_data"  # 히든 퀘스트 저장 키
quest_id = 1
QUEST_NAMES = {1: "아니시에이팅", 2: "감사한 마음", 3: "파푸 애호가"}

QUEST_CONDITIONS = {
    1: "메시지에 '아니'를 24시간 동안 50회 이상 포함하면 달성됩니다.",
    2: "메시지에 '감사합니다'를 24시간 동안 50회 이상 포함하면 달성됩니다.",
    3: "메시지에 '파푸'를 24시간 동안 45회 이상 포함하면 달성됩니다."
}  # 히든 퀘스트 이름 매핑

VALID_QUEST_IDS = {1, 2, 3}  # 사용할 히든퀘스트 번호 목록

# KST 타임존 객체
KST = pytz.timezone("Asia/Seoul")


# ---- Firebase 핸들링 함수 ----


# ---- Firebase 비동기 래퍼 (블로킹 방지) ----
import asyncio

async def aload_exp_data():
    return await asyncio.to_thread(load_exp_data)

async def asave_exp_data(data):
    return await asyncio.to_thread(save_exp_data, data)

async def asave_user_exp(user_id, user_data):
    return await asyncio.to_thread(save_user_exp, user_id, user_data)

async def aload_mission_data():
    return await asyncio.to_thread(load_mission_data)

async def asave_mission_data(data):
    return await asyncio.to_thread(save_mission_data, data)

async def asave_user_mission(user_id, user_mission):
    return await asyncio.to_thread(save_user_mission, user_id, user_mission)

async def aget_attendance_data():
    return await asyncio.to_thread(get_attendance_data)

async def aset_attendance_data(user_id, data):
    return await asyncio.to_thread(set_attendance_data, user_id, data)



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
# === 레벨 곡선: 5단계 등비(엔드게임 초하드) ===
from bisect import bisect_right

LEVEL_MAX = 99

# 각 항목: (start_level, end_level, start_delta, ratio, jump_from_prev_end)
# start_delta가 None이면 '직전 단계 마지막 Δ × jump'로 시작
STAGES = [
    (1,   5,  240,   1.040, 1.00),   # 튜토리얼(가볍게)
    (6,  10,  None,  1.045, 1.10),
    (11, 15,  None,  1.050, 1.10),
    (16, 20,  None,  1.056, 1.12),
    (21, 25,  None,  1.063, 1.12),
    (26, 30,  None,  1.071, 1.13),
    (31, 35,  None,  1.080, 1.13),
    (36, 40,  None,  1.090, 1.14),
    (41, 45,  None,  1.101, 1.15),
    (46, 50,  None,  1.113, 1.15),
    (51, 60,  None,  1.126, 1.16),   # 50→60 완만 상승
    (61, 70,  None,  1.140, 1.17),   # 60대 ‘벽’ 제거(미세 증가)
    (71, 85,  None,  1.155, 1.18),   # 고레벨 진입이지만 급점프 없음
    (86, 99,  None,  1.171, 1.22),   # 엔드게임: 꾸준히 가파르되 ‘절벽’은 아님
]
]

def _build_piecewise_geometric_deltas(stages, Lmax):
    """각 레벨 Δ(필요치) 생성. 반올림 후 단조증가 보정."""
    deltas = []
    prev_d = None
    for (a, b, start_d, r, jump) in stages:
        if start_d is None:
            start_d = int(round(prev_d * jump))
        for L in range(a, b + 1):
            if L == a:
                d = start_d
            else:
                d = int(round(d * r))
            if prev_d is not None and d <= prev_d:
                d = prev_d + 1  # 반올림으로 인한 비단조 방지
            deltas.append(d)
            prev_d = d
    if len(deltas) < Lmax:
        deltas += [deltas[-1]] * (Lmax - len(deltas))
    return deltas[:Lmax]

# Δ[1..99]
GEOM_DELTAS = _build_piecewise_geometric_deltas(STAGES, LEVEL_MAX)

# T[L] = Lv.L '진입' 임계 누적치 (T[0]=0, T[1]=Δ1, ...)
THRESHOLDS = [0]
for d in GEOM_DELTAS:
    THRESHOLDS.append(THRESHOLDS[-1] + d)

def calculate_level(exp: int) -> int:
    """T[L-1] <= exp < T[L] 이면 현재 레벨 L (1..99)"""
    idx = bisect_right(THRESHOLDS, exp) - 1
    return max(1, min(idx + 1, LEVEL_MAX))



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

def hidden_quest_txn(cur):
    # 처음 호출 시 기본 구조 생성
    if cur is None:
        cur = {
            "last_date": datetime.now(KST).strftime("%Y-%m-%d"),
            "counts": {},
            "timestamps": {},
            "completed": False,
            "winner": None
        }

    today = datetime.now(KST).strftime("%Y-%m-%d")
    if cur["last_date"] != today:
        cur["last_date"] = today
        cur["counts"] = {}
        cur["timestamps"] = {}

    return cur


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
    # 1) 커맨드 등록: 최초 1회만
    if not getattr(bot, "_commands_added", False):
        try:
            bot.tree.add_command(hidden_quest, override=True)
            bot._commands_added = True
        except Exception as e:
            print(f"[on_ready] add_command failed: {e!r}")

    # 2) 시즌 보이스 채널 업데이트 (예외 로깅)
    try:
        await update_season_voice_channels()
    except Exception as e:
        print(f"[on_ready] update_season_voice_channels error: {e!r}")


    print(f"✅ {bot.user} 온라인")
    logging.info(f"[ready] logged in as {bot.user} (id={bot.user.id})")
    await bot.change_presence(activity=discord.Game("부팅 점검 중"))
    
    # 3) 슬래시 커맨드 동기화: 최초 1회만
    if not getattr(bot, "_synced", False):
        try:
            synced = await bot.tree.sync()  # 전역 등록
            bot._synced = True
            print(f"🌐 전역 슬래시 커맨드 {len(synced)}개 동기화 완료")
        except Exception as e:
            print(f"❌ 슬래시 커맨드 동기화 실패: {e!r}")

    # 4) 백그라운드 태스크 안전 시작(중복 방지)
    for task in (voice_xp_task, reset_daily_missions, repeat_vc_mission_task, inactive_user_log_task):
        try:
            if not task.is_running():
                task.start()
        except Exception as e:
            print(f"[on_ready] task start error: {e!r}")


# ---- on_member_update: 환영 메시지 및 역할 동기화 ----
@bot.event
async def on_member_update(before, after):
    before_roles = set(r.id for r in before.roles)
    after_roles = set(r.id for r in after.roles)
    added = after_roles - before_roles
    
    if before_roles != after_roles:
        await update_season_voice_channels()

    # 특정 스레드 역할이 부여되면 환영 메시지
    if THREAD_ROLE_ID in added:
        channel = bot.get_channel(TARGET_TEXT_CHANNEL_ID)
        if channel:
            await channel.send(
                f"환영합니다 {after.mention} 님! '사계절, 그 사이' 서버입니다.\n"
                "프로필 우클릭 → 편집으로 닉네임을 변경할 수 있어요!"
            )

        # DB에서 경험치, 레벨 로드 후 역할/닉네임 동기화
        exp_data = await aload_exp_data()
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
    """5일 미접속 사용자 추방 + 로그"""
    exp_data = await aload_exp_data()
    threshold = datetime.now(KST) - timedelta(days=5)
    log_channel = bot.get_channel(INACTIVE_LOG_CHANNEL_ID)

    if not log_channel:
        return

    kicked = []  # 추방된 유저 기록

    for guild in bot.guilds:
        for member in guild.members:
            if member.bot or member.id == guild.owner_id:
                continue
            if any(r.id in EXEMPT_ROLE_IDS for r in member.roles):
                continue

            user = exp_data.get(str(member.id))
            if not user or not user.get("last_activity"):
                continue

            last_active = datetime.fromtimestamp(user["last_activity"], KST)
            if last_active < threshold:
                # DM 시도
                try:
                    embed = discord.Embed(
                        title="📢 사계절, 그 사이 서버 안내",
                        description=(
                            "안녕하세요, '사계절, 그 사이' 서버 관리자입니다.\n\n"
                            "최근 5일간 서버에 기록된 활동 내역이 없어,\n"
                            "공지해둔 규칙 사항에 따라 서버에서 추방 처리가 진행됩니다 !\n\n"
                            "개인 사정에 의해, 혹은 기록 누락 등 피치 못할 사정으로 추방되신 분들은\n"
                            "아래의 링크를 통해 언제든 다시 서버에 입장하실 수 있습니다.\n"
                            "앞으로 더 활발히 활동해 주시면 감사하겠습니다 !\n\n"
                            "👉 https://discord.gg/Npuxrkf38G\n\n"
                            "- '사계절, 그 사이' 서버장 새벽녘 (새벽녘#0001) -"
                        ),
                        color=0x3498db
                    )
                    await member.send(embed=embed)
                except:
                    await log_channel.send(f"❌ {member.display_name} 님에게 DM 전송 실패")

                # 추방
                try:
                    await member.kick(reason="5일 미접속 자동 추방")
                    await log_channel.send(f"👢 {member.display_name} 님이 5일간 미접속으로 추방되었습니다.")
                    kicked.append(member.display_name)
                except Exception as e:
                    await log_channel.send(f"❌ {member.display_name} 님 추방 실패: {e}")

    # ✅ 아무도 추방되지 않았을 경우에도 로그 남기기
    if not kicked:
        await log_channel.send("✅ 현재 5일 이상 미접속 중인 사용자가 없습니다.")

@tasks.loop(time=dtime(hour=15, minute=0))
async def reset_daily_missions():
    """일일 미션 데이터 초기화 (로컬 및 DB)"""
    try:
        # 로컬 파일 초기화
        save_json(MISSION_PATH, {})
        # Realtime DB의 mission_data 노드 초기화
        await asave_mission_data({})
        print("🔁 일일 미션 초기화 완료")
    except Exception as e:
        # 오류 발생 시 로그 채널에 알림하거나 콘솔에 에러 기록
        print(f"❌ 일일 미션 초기화 실패: {e}")


@tasks.loop(seconds=VOICE_COOLDOWN)
async def voice_xp_task():
    """음성 채널 경험치 태스크"""
    now_ts = time.time()
    exp_data = await aload_exp_data()

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

                await asave_user_exp(uid, user_data)


@tasks.loop(seconds=60)
async def repeat_vc_mission_task():
    """반복 VC 미션 보상 태스크"""
    mission_data = await aload_mission_data()
    exp_data = await aload_exp_data()
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

    await asave_mission_data(mission_data)
    # 로컬 JSON에도 백업
    try:
        save_json(MISSION_PATH, mission_data)
    except Exception as e:
        print(f"❌ 미션 로컬 백업 실패: {e}")
    await asave_exp_data(exp_data)


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
        exp_data = await aload_exp_data()
        uid = str(message.author.id)
        user_data = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
        now_ts = time.time()

        if now_ts - user_data.get("last_activity", 0) >= COOLDOWN_SECONDS:
            gain = random.randint(1, 30)
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

        await asave_user_exp(uid, user_data)

        # 4) 텍스트 미션 집계 (지정 채널만)
        mission_data = await aload_mission_data()
        exp_data = await aload_exp_data()
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
                await asave_exp_data(exp_data)

                log_ch = bot.get_channel(LOG_CHANNEL_ID)
                if log_ch:
                     await log_ch.send(f"[🧾 로그] {message.author.display_name} 님 텍스트 미션 완료! +{MISSION_EXP_REWARD}XP")
                await message.channel.send(f"🎯 {message.author.mention} 일일 미션 완료! +{MISSION_EXP_REWARD}XP 지급되었습니다.")
                user_m["text"]["completed"] = True

        mission_data[uid] = user_m
        await asave_user_mission(uid, user_m)

    except Exception as e:
        print(f"❌ on_message 처리 중 오류: {e}")

    # ---- 히든 퀘스트 진행 처리 ----
    # 메시지에 '아니' 키워드가 포함된 경우에만 트랜잭션 실행
    if "아니" in message.content:
        ref_hq = db.reference(f"{HIDDEN_QUEST_KEY}/1")
        def txn(cur):
            cur = hidden_quest_txn(cur)
            cnts = cur.get("counts", {})
            if not cur["completed"] and "아니" in message.content:
                uid = str(message.author.id)
                now = datetime.now(KST)
                ts_map = cur.get("timestamps", {})
                first_time_str = ts_map.get(uid)

                if not first_time_str:
                    ts_map[uid] = now.isoformat()
                    cur["timestamps"] = ts_map
                    cnts[uid] = 1                   # ✅ 첫 기록은 1로 시작
                else:
                    first_time = datetime.fromisoformat(first_time_str)
                    if now - first_time > timedelta(hours=24):
                        cur["timestamps"][uid] = now.isoformat()
                        cnts[uid] = 1               # ✅ 하루 경과했으면 리셋 후 1
                    else:
                        cnts[uid] = cnts.get(uid, 0) + 1

                cur["counts"] = cnts
                if cnts[uid] >= 50:
                    cur["completed"] = True
                    cur["winner"] = uid
                    cur["completed_at"] = datetime.now(KST).strftime("%Y. %-m. %-d %H:%M")
            return cur

        result = ref_hq.transaction(txn)
        if result.get("completed") and result.get("winner") == str(message.author.id):
            await message.channel.send(
                f"🎉 {message.author.mention}님, 히든 퀘스트 [아니시에이팅]을(를) 완료하셨습니다!"
            )

    # 메시지에 '감사합니다' 키워드가 포함된 경우에만 트랜잭션 실행
    if "감사합니다" in message.content:
        ref_hq = db.reference(f"{HIDDEN_QUEST_KEY}/2")
        def txn2(cur):
            cur = hidden_quest_txn(cur)
            cnts = cur.get("counts", {})
            if not cur["completed"] and "감사합니다" in message.content:
                uid = str(message.author.id)
                now = datetime.now(KST)
                ts_map = cur.get("timestamps", {})
                first_time_str = ts_map.get(uid)

                if not first_time_str:
                    ts_map[uid] = now.isoformat()
                    cur["timestamps"] = ts_map
                    cnts[uid] = 1                   # ✅ 첫 기록은 1로 시작
                else:
                    first_time = datetime.fromisoformat(first_time_str)
                    if now - first_time > timedelta(hours=24):
                        cur["timestamps"][uid] = now.isoformat()
                        cnts[uid] = 1               # ✅ 하루 경과했으면 리셋 후 1
                    else:
                        cnts[uid] = cnts.get(uid, 0) + 1

                cur["counts"] = cnts
                if cnts[uid] >= 50:
                    cur["completed"] = True
                    cur["winner"] = uid
                    cur["completed_at"] = datetime.now(KST).strftime("%Y. %-m. %-d %H:%M")
            return cur

        result = ref_hq.transaction(txn2)
        if result.get("completed") and result.get("winner") == str(message.author.id):
            await message.channel.send(
                f"🎉 {message.author.mention}님, 히든 퀘스트 [감사한 마음] 달성!"
            )

    # 메시지에 '파푸' 키워드가 포함된 경우에만 트랜잭션 실행
    if "파푸" in message.content:
        ref_hq = db.reference(f"{HIDDEN_QUEST_KEY}/3")
        def txn3(cur):
            cur = hidden_quest_txn(cur)
            cnts = cur.get("counts", {})
            if not cur["completed"] and "파푸" in message.content:
                uid = str(message.author.id)
                now = datetime.now(KST)
                ts_map = cur.get("timestamps", {})
                first_time_str = ts_map.get(uid)

                if not first_time_str:
                    ts_map[uid] = now.isoformat()
                    cur["timestamps"] = ts_map
                    cnts[uid] = 1
                else:
                    first_time = datetime.fromisoformat(first_time_str)
                    if now - first_time > timedelta(hours=24):
                        ts_map[uid] = now.isoformat()
                        cur["timestamps"] = ts_map
                        cnts[uid] = 1
                    else:
                        cnts[uid] = cnts.get(uid, 0) + 1

                cur["counts"] = cnts

                if cnts[uid] >= 45:
                    cur["completed"] = True
                    cur["winner"] = uid
                    cur["completed_at"] = datetime.now(KST).strftime("%Y. %-m. %-d %H:%M")
            return cur

        result = ref_hq.transaction(txn3)
        if result.get("completed") and result.get("winner") == str(message.author.id):
            await message.channel.send(
                f"🎉 {message.author.mention}님, 히든 퀘스트 [파푸 애호가] 달성!"
            )


# ---- 슬래시 관리자 명령어 ----

# ---- 히든 퀘스트 관리 커맨드 ----

hidden_quest = app_commands.Group(
    name="히든관리",
    description="히든 퀘스트 관리"
)

@hidden_quest.command(
    name="상태",
    description="지정한 히든퀘스트 상태 조회"
)
@app_commands.describe(
    번호="조회할 히든퀘스트 번호 (정수)"
)
@app_commands.default_permissions(administrator=True)
async def 상태(inter: discord.Interaction, 번호: int):
    if 번호 not in VALID_QUEST_IDS:
        return await inter.response.send_message(
            f"❌ 유효하지 않은 퀘스트 번호입니다. 사용 가능한 번호: {sorted(VALID_QUEST_IDS)}",
            ephemeral=True
        )

    key = f"{HIDDEN_QUEST_KEY}/{번호}"
    data = db.reference(key).get() or {}
    last_date = data.get("last_date", "-")
    completed = data.get("completed", False)
    winner = data.get("winner")
    my_count = data.get("counts", {}).get(str(inter.user.id), 0)

    name = QUEST_NAMES.get(번호, f"퀘스트 {번호}")
    msg = f"""🔎 히든 퀘스트 [{name}] 상태
📅 마지막 초기화: {last_date}
✅ 완료 여부: {'완료' if completed else '미완료'}
🏆 달성자: {f'<@{winner}>' if winner else '없음'}
📊 내 카운트: {my_count} / 50"""
    await inter.response.send_message(msg, ephemeral=True)

@hidden_quest.command(
    name="리셋",
    description="지정한 히든퀘스트 번호만 초기화합니다."
)
@app_commands.describe(
    번호="초기화할 히든퀘스트 번호 (정수)"
)
@app_commands.default_permissions(administrator=True)
async def 리셋(inter: discord.Interaction, 번호: int):
    if 번호 not in VALID_QUEST_IDS:
        return await inter.response.send_message(
            f"❌ 유효하지 않은 퀘스트 번호입니다. 사용 가능한 번호: {sorted(VALID_QUEST_IDS)}",
            ephemeral=True
        )

    key = f"{HIDDEN_QUEST_KEY}/{번호}"
    today = datetime.now(KST).strftime("%Y-%m-%d")
    db.reference(key).set({
        "last_date": today,
        "counts": {},
        "completed": False,
        "winner": None
    })
    await inter.response.send_message(
        f"🔄 히든 퀘스트 #{번호}를 초기화했습니다.",
        ephemeral=True
    )

    

# ---- 기타 슬래시 커맨드 핸들러 (/정보, /퀘스트, /랭킹, /출석, /출석랭킹) ----

# 건의함 기능 설정
SUGGEST_ANON_CHANNEL_ID = 1410186330083954689  # 익명 건의함 채널 ID
SUGGEST_REAL_CHANNEL_ID = 1410186411310710847  # 실명 건의함 채널 ID
OWNER_ID = 792661958549045249                  # 서버 오너(본인) ID

from discord import Embed

@bot.tree.command(name="건의함", description="건의사항을 관리자에게 전달합니다.")
@app_commands.describe(
    모드="익명 또는 실명 중 선택하세요.",
    내용="보낼 건의 내용을 작성하세요."
)
@app_commands.choices(
    모드=[
        app_commands.Choice(name="익명", value="익명"),
        app_commands.Choice(name="실명", value="실명"),
    ]
)
async def suggest(interaction: discord.Interaction, 모드: str, 내용: str):
    anon_ch = bot.get_channel(SUGGEST_ANON_CHANNEL_ID)
    real_ch = bot.get_channel(SUGGEST_REAL_CHANNEL_ID)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    # 내용 길이 제한 (임베드 안정성 보장)
    if len(내용) > 1000:
        return await interaction.response.send_message(
            "❌ 건의 내용은 **1000자 이내**로 작성해주세요.",
            ephemeral=True
        )

    # === 익명 모드 ===
    if 모드 == "익명":
        # 관리자 채널에 익명 건의 임베드 전송
        embed = Embed(
            title=f"📢 익명 건의 ({now_str})",
            description=f"알 수 없는 서버원 님이 아래와 같이 건의하셨습니다:\n\n{내용}",
            color=0x95a5a6
        )
        if anon_ch:
            await anon_ch.send(embed=embed)

        # 오너 DM 전송 (실제 유저 정보 포함)
        owner = bot.get_user(OWNER_ID)
        if owner:
            exp_data = await aload_exp_data()
            user = exp_data.get(str(interaction.user.id), {})
            last_ts = user.get("last_activity")
            if last_ts:
                last_dt = datetime.fromtimestamp(last_ts, KST)
                days_ago = (datetime.now(KST) - last_dt).days
                last_seen = f"{days_ago}일 전 ({last_dt.strftime('%Y-%m-%d %H:%M')})"
            else:
                last_seen = "기록 없음"

            dm_embed = Embed(
                title=f"📢 익명 건의 (내부 기록) [{now_str}]",
                color=0xe74c3c
            )
            dm_embed.add_field(name="서버 닉네임", value=interaction.user.display_name, inline=False)
            dm_embed.add_field(name="계정 닉네임", value=f"{interaction.user}", inline=False)
            dm_embed.add_field(name="서버 입장일", value=interaction.user.joined_at.strftime("%Y-%m-%d %H:%M"), inline=False)
            dm_embed.add_field(name="최근 활동", value=last_seen, inline=False)
            dm_embed.add_field(name="건의 내용", value=내용, inline=False)

            try:
                await owner.send(embed=dm_embed)
            except:
                pass  # 실패 시 기록 X, 조용히 무시

    # === 실명 모드 ===
    elif 모드 == "실명":
        embed = Embed(
            title=f"📢 실명 건의 ({now_str})",
            description=f"서버원 {interaction.user.display_name} 님이 아래와 같이 건의하셨습니다:\n\n{내용}",
            color=0x2ecc71
        )
        if real_ch:
            await real_ch.send(embed=embed)

    # 사용자에게 전송 완료 알림 (ephemeral)
    await interaction.response.send_message("✅ 건의가 정상적으로 전달되었습니다.", ephemeral=True)

@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="정보분석", description="서버원의 경험치 및 마지막 활동일 분석")
@app_commands.describe(member="분석할 서버원")
async def analyze_info(interaction: discord.Interaction, member: discord.Member):
    uid = str(member.id)
    exp_data = await aload_exp_data()
    user = exp_data.get(uid)

    if not user:
        return await interaction.response.send_message(f"{member.display_name}님의 정보가 존재하지 않습니다.", ephemeral=True)

    level = user.get("level", 1)
    exp = user.get("exp", 0)
    last_ts = user.get("last_activity")

    if last_ts:
        last_dt = datetime.fromtimestamp(last_ts, KST)
        elapsed = datetime.now(KST) - last_dt
        days_ago = elapsed.days
        last_seen = last_dt.strftime("%Y. %-m. %-d %H:%M")
    else:
        last_seen = "기록 없음"
        days_ago = "-"

    embed = discord.Embed(title=f"📊 {member.display_name}님의 활동 분석", color=discord.Color.orange())
    embed.add_field(name="레벨", value=f"Lv. {level} ({exp:,} XP)", inline=False)
    embed.add_field(name="마지막 활동 시각", value=last_seen, inline=False)
    embed.add_field(name="경과일", value=f"{days_ago}일 경과" if isinstance(days_ago, int) else days_ago, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="경험치지급", description="유저에게 경험치를 지급합니다.")
async def grant_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    exp_data = await aload_exp_data()
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

    await asave_user_exp(uid, user_data)
    await interaction.response.send_message(f"✅ {member.mention}에게 경험치 {amount}XP 지급 완료!", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="경험치차감", description="유저의 경험치를 차감합니다.")
async def deduct_xp(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: int
):
    # 데이터 로드
    exp_data = await aload_exp_data()
    uid = str(member.id)
    user_data = exp_data.get(uid, {"exp": 0, "level": 1})

    # 경험치 차감 및 레벨 재계산
    user_data["exp"] = max(0, user_data["exp"] - amount)
    user_data["level"] = calculate_level(user_data["exp"])

    # DB 저장
    await asave_user_exp(uid, user_data)

    # 역할·닉네임 변경 (데바운스 적용)
    await update_role_and_nick(member, user_data["level"])

    await interaction.response.send_message(f"✅ {member.mention}에게서 경험치 {amount}XP 차감 완료!", ephemeral=True)
# ---- 기타 슬래시 커맨드 핸들러 (/정보, /퀘스트, /랭킹, /출석, /출석랭킹) ----

  # 히든 퀘스트 목록 조회 명령어 (일반 사용자용)
@bot.tree.command(name="히든퀘스트", description="히든 퀘스트 목록을 확인합니다.")
async def hidden_quest_list(interaction: discord.Interaction):
    raw = db.reference(HIDDEN_QUEST_KEY).get()
    data = raw if isinstance(raw, dict) else {}
    lines = ["🕵️ 히든 퀘스트"]

    for qid in sorted(VALID_QUEST_IDS):
        q = data.get(str(qid), {})
        if q.get("completed"):
            name = QUEST_NAMES.get(qid, f"퀘스트 {qid}")
            winner = f"<@{q.get('winner')}>" if q.get("winner") else "알 수 없음"
            completed_at = q.get("completed_at", "알 수 없음")
            condition = QUEST_CONDITIONS.get(qid, "조건 비공개")
            lines.append(f"{qid}. {name}\n달성자: {winner}\n완료 시각: {completed_at}\n📘 조건: {condition}")
        else:
            lines.append(f"{qid}. ???")

    await interaction.response.send_message("\n\n".join(lines))

                                            
@bot.tree.command(name="정보", description="자신의 레벨 및 경험치 정보를 확인합니다.")
async def info(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    exp_data = await aload_exp_data()
    user = exp_data.get(uid, {"exp": 0, "level": 1, "voice_minutes": 0})
    current_exp = user["exp"]
    lvl = calculate_level(current_exp)
    if lvl != user["level"]:
        user["level"] = lvl
        await asave_user_exp(uid, user)
    # 새 등비 5단계 곡선 기준 진행도 계산
    left = THRESHOLDS[lvl - 1]
    right = THRESHOLDS[lvl] if lvl <= LEVEL_MAX else THRESHOLDS[-1]
    progress = max(0, current_exp - left)
    total = max(1, right - left)
    percent = (progress / total) * 100
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
    missions = await aload_mission_data()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    um = missions.get(uid, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})
    if um.get("date") != today:
        um = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

    text_count = um["text"]["count"]
    text_status = (
      f"진행도: {text_count} / {MISSION_REQUIRED_MESSAGES}\n"
      f"상태: {'✅ 완료' if um['text']['completed'] else '❌ 미완료'}"
    )
  
    vc_minutes = um["repeat_vc"]["minutes"]
    vc_rewards = vc_minutes // REPEAT_VC_REQUIRED_MINUTES
    vc_status = f"누적 참여: {vc_minutes}분\n보상 횟수: {vc_rewards}회 지급"

    # 출석 여부
    attendance_all = await aget_attendance_data()
    attendance = attendance_all.get(uid, {})
    attended = last_date == today
    attendance_status = f"상태: {'✅ 출석 완료' if attended else '❌ 출석 안됨'}"

    embed = discord.Embed(title="📜 퀘스트 현황", color=discord.Color.green())
    embed.add_field(name="🗨️ 텍스트 미션", value=text_status, inline=False)
    embed.add_field(name="📞 5인 이상 통화방 참여 미션", value=vc_status, inline=False)
    embed.add_field(name="🗓️ 출석", value=attendance_status, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="랭킹", description="경험치 랭킹을 확인합니다.")
async def ranking(interaction: discord.Interaction):
    exp_data = await aload_exp_data()
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
    data = await aget_attendance_data()
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
    gain = 100 + min(ud["streak"] - 1, 10) * 10
    expd = await aload_exp_data()
    ue = expd.get(uid,{"exp":0,"level":1,"voice_minutes":0})
    prev_level = ue["level"]
    ue["exp"] += gain
    ue["level"] = calculate_level(ue["exp"])
    ue["last_activity"] = time.time()

    if ue["level"] > prev_level:
        announce = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
        if announce:
            await announce.send(f"🎉 {interaction.user.mention} 님이 Lv.{ue['level']} 에 도달했습니다! 🎊")


    await asave_user_exp(uid, ue)
    await aset_attendance_data(uid, ud)
    await update_role_and_nick(interaction.user, ue["level"])
    first_attend = ud["total_days"] == 1
    streak_reset = ud["streak"] == 1 and ud["last_date"] != yesterday

    if first_attend:
        intro = "✨ 출석! 빛나는 하루 되세요!"
    elif streak_reset:
        intro = "😥 연속 출석이 끊겼습니다! 다시 1일부터 시작합니다."
    else:
        intro = random.choice([
            "🎉 출석 완료! 멋져요!",
            "🥳 계속 달려볼까요?",
            "🌞 좋은 하루의 시작이에요!",
            "💪 출석 성공! 오늘도 파이팅!"
        ])

    msg = (
      f"{intro}\n"
      f"누적 출석: {ud['total_days']}일\n"
      f"연속 출석: {ud['streak']}일\n"
      f"경험치: +{gain} XP"
      )
    await interaction.response.send_message(msg)

@bot.tree.command(name="출석랭킹", description="출석 랭킹을 확인합니다.")
async def attend_ranking(interaction: discord.Interaction):
    data = await aget_attendance_data()
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

# ---- Launcher (Flask thread) ----
def _start_flask():
    port = int(os.getenv("PORT", "10000"))
    threading.Thread(
        target=app.run,
        kwargs={"host": "0.0.0.0", "port": port, "use_reloader": False},
        daemon=True,
    ).start()

async def _safe_start():
    """
    디스코드 로그인 안전 실행:
    - 로그인/연결 전에 발생하는 예외만 백오프 재시도
    - 실행 후에는 timeout으로 세션을 끊지 않음 (중요)
    """
    base = 1800          # 30분
    max_backoff = 7200   # 2시간
    penalty = 0          # 연속 429 누적

    while True:
        try:
            print("[login] bot.start 진입")
            # ❌ timeout 제거: 실행 중에는 세션을 끊지 않는다
            await bot.start(TOKEN)
            print("[login] bot.start 정상 종료")
            break  # 정상 종료 시 루프 탈출

        except discord.HTTPException as e:
            # 로그인/연결 직전 단계의 HTTP 오류만 백오프
            status = getattr(e, "status", None)
            try:
                await bot.close()
            except Exception:
                pass

            if status == 429:
                penalty = min(penalty + 1, 3)                       # 0→1→2→3
                wait = min(base + penalty * 1800, max_backoff)       # 30→60→90→120
                wait = int(wait * random.uniform(0.95, 1.1))
                print(f"[login] 429/Cloudflare rate limit. backoff {wait}s")
                await asyncio.sleep(wait)
                continue

            wait = int(min(base, max_backoff) * random.uniform(0.5, 1.0))
            print(f"[login] HTTP {status}; backoff {wait}s: {e!r}")
            await asyncio.sleep(wait)

        except RuntimeError as e:
            # 드문 런타임 오류에 대해 보수적 백오프 후 재시도
            try:
                await bot.close()
            except Exception:
                pass
            wait = int(900 * random.uniform(0.8, 1.2))
            print(f"[login] RuntimeError; backoff {wait}s: {e!r}")
            await asyncio.sleep(wait)

        except Exception as e:
            # 알 수 없는 예외
            try:
                await bot.close()
            except Exception:
                pass
            wait = int(900 * random.uniform(0.8, 1.2))
            print(f"[login] unexpected; backoff {wait}s: {e!r}")
            await asyncio.sleep(wait)



# --- 강제 로깅 활성화 (INFO 이상 콘솔 출력)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

# discord 내부 로거 가시성 상승
logging.getLogger("discord").setLevel(logging.INFO)
logging.getLogger("discord.client").setLevel(logging.INFO)
logging.getLogger("discord.gateway").setLevel(logging.INFO)
logging.getLogger("discord.http").setLevel(logging.INFO)

if __name__ == "__main__":
    _start_flask()
    asyncio.run(_safe_start())

