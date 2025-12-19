import discord
from discord.ext import commands, tasks
from discord import app_commands


import os
import sys
import json
import time
import random
import re
import asyncio
import logging
import pytz
import aiohttp

from threading import Thread
from datetime import time as dtime
from datetime import datetime  # ← 추가
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import logging
logging.basicConfig(level=logging.INFO)


# =========================
# Rank card rendering (Pillow)
# =========================

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
_BG_PATH = os.path.join(_ASSET_DIR, "rank_bg.png")
_FONT_PATH = os.path.join(_ASSET_DIR, "fonts", "Donoun Medium.ttf")  # 네가 넣은 폰트명에 맞춤

_BG_TEMPLATE = None  # type: Optional[Image.Image]
_FONT_CACHE = {}     # size -> ImageFont.FreeTypeFont


def _get_bg_template() -> Image.Image:
    global _BG_TEMPLATE
    if _BG_TEMPLATE is None:
        bg = Image.open(_BG_PATH).convert("RGBA")
        _BG_TEMPLATE = bg
    return _BG_TEMPLATE


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    font = _FONT_CACHE.get(size)
    if font is None:
        font = ImageFont.truetype(_FONT_PATH, size)
        _FONT_CACHE[size] = font
    return font


def _format_int(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text

    ell = "…"
    lo, hi = 0, len(text)
    # 이진 탐색으로 최대 길이 찾기
    while lo < hi:
        mid = (lo + hi) // 2
        cand = text[:mid] + ell
        if draw.textlength(cand, font=font) <= max_width:
            lo = mid + 1
        else:
            hi = mid
    cut = max(0, lo - 1)
    return text[:cut] + ell


def _circle_crop(im: Image.Image, size: int) -> Image.Image:
    # 정사각으로 맞춘 뒤 원형 마스크
    im = im.convert("RGBA")
    w, h = im.size
    s = min(w, h)
    left = (w - s) // 2
    top = (h - s) // 2
    im = im.crop((left, top, left + s, top + s))

    resample = getattr(Image, "Resampling", None)
    if resample is not None:
        im = im.resize((size, size), resample=resample.LANCZOS)
    else:
        im = im.resize((size, size), resample=Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, size - 1, size - 1), fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def render_rank_card(
    *,
    display_name: str,
    level: int,
    total_xp: int,
    cur_xp: int,
    need_xp: int,
    pct: float,
    avatar_bytes: Optional[bytes] = None,
) -> BytesIO:
    """
    디스코드/DB와 무관한 순수 렌더러.
    - 입력: 가공된 수치 + 아바타 이미지 bytes
    - 출력: PNG(BytesIO)
    """
    bg = _get_bg_template()
    img = bg.copy()
    draw = ImageDraw.Draw(img)

    # ===== 레이아웃 (600x240 기준) =====
    AVATAR_SIZE = 96
    AVATAR_X, AVATAR_Y = 36, 72

    TEXT_X = 155
    NAME_Y = 60
    STAT_Y = 102
    XP_Y = 130

    BAR_X, BAR_Y = 150, 180
    BAR_W, BAR_H = 300, 22
    BAR_RADIUS = 11  # BAR_H//2

    # ===== 아바타 =====
    if avatar_bytes:
        try:
            av = Image.open(BytesIO(avatar_bytes))
            av = _circle_crop(av, AVATAR_SIZE)
            img.paste(av, (AVATAR_X, AVATAR_Y), av)
        except Exception:
            # 아바타 실패 시 회색 원으로 대체
            fallback = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (0, 0, 0, 0))
            fd = ImageDraw.Draw(fallback)
            fd.ellipse((0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), fill=(120, 120, 120, 255))
            img.paste(fallback, (AVATAR_X, AVATAR_Y), fallback)

    # ===== 폰트 =====
    font_name = _get_font(28)
    font_stat = _get_font(22)
    font_small = _get_font(18)

    # ===== 닉네임 =====
    name_max_w = 600 - TEXT_X - 30
    safe_name = _ellipsize(draw, display_name, font_name, name_max_w)
    draw.text((TEXT_X, NAME_Y), safe_name, font=font_name, fill=(0x05, 0x44, 0x6B, 255))

    # ===== 레벨 / XP =====
    draw.text((TEXT_X, STAT_Y), f"Lv. {int(level)}", font=font_stat, fill=(0xFF, 0xFF, 0xFF, 255))
    draw.text((TEXT_X, XP_Y), f"XP  {_format_int(total_xp)}", font=font_stat, fill=(0x9E, 0x9E, 0x9E, 255))

    # ===== 진행도 바 =====
    pct = _clamp01(float(pct))
    # 바 배경
    draw.rounded_rectangle(
        (BAR_X, BAR_Y, BAR_X + BAR_W, BAR_Y + BAR_H),
        radius=BAR_RADIUS,
        fill=(0xED, 0xF8, 0xFC, 255),
    )
    # 바 채움
    fill_w = int(BAR_W * pct)
    if fill_w > 0:
        draw.rounded_rectangle(
            (BAR_X, BAR_Y, BAR_X + fill_w, BAR_Y + BAR_H),
            radius=BAR_RADIUS,
            fill=(0x05, 0x44, 0x6B, 255),
        )

    # 진행도 텍스트
    # 예: "123 / 456 (27%)"
    pct_int = int(round(pct * 100))
    prog_text = f"{_format_int(cur_xp)} / {_format_int(need_xp)} ({pct_int}%)"
    draw.text((BAR_X, BAR_Y - 22), prog_text, font=font_small, fill=(60, 60, 60, 255))

    # ===== PNG 출력 =====
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# =======================================================================

KST = pytz.timezone("Asia/Seoul")  # ← 추가

SAFEGUARD_DISABLE_EXTERNAL_IO = os.getenv("SAFEGUARD_DISABLE_EXTERNAL_IO", "1") == "1"
SAFEGUARD_MIN_INTERVAL_GLOBAL = float(os.getenv("SAFEGUARD_MIN_INTERVAL_GLOBAL", "1.0"))  # 전역 처리 간 최소 간격(초)
SAFEGUARD_MIN_INTERVAL_PER_CHANNEL = float(os.getenv("SAFEGUARD_MIN_INTERVAL_PER_CHANNEL", "2.0"))  # 채널별
SAFEGUARD_MIN_INTERVAL_PER_USER = float(os.getenv("SAFEGUARD_MIN_INTERVAL_PER_USER", "2.0"))  # 유저별

# 외부 HTTP 동시성 제한 (필요 시 사용)
SAFEGUARD_EXTERNAL_IO_SEMAPHORE = asyncio.Semaphore(int(os.getenv("SAFEGUARD_EXTERNAL_IO_MAX_CONCURRENCY", "3")))

_last_global_ts = 0.0
_last_channel_ts = defaultdict(float)  # channel_id -> ts
_last_user_ts = defaultdict(float)     # user_id -> ts

load_dotenv()
firebase_key_json = os.getenv("FIREBASE_KEY_JSON")

# === fail-fast: Firebase 키 없으면 즉시 종료 ===
if not firebase_key_json:
    raise RuntimeError("FIREBASE_KEY_JSON 환경변수가 설정되어 있지 않습니다.")

# 1차 파싱: 환경변수 값이 (a) 원본 JSON 이거나 (b) JSON 문자열(tojson 결과)일 수 있음
try:
    v = json.loads(firebase_key_json)
except json.JSONDecodeError:
    raise RuntimeError("FIREBASE_KEY_JSON 값이 올바른 JSON 형식이 아닙니다.")

# 2차 처리: tojson로 넣은 경우(str)면 한 번 더 파싱해서 dict로 만든다
if isinstance(v, str):
    try:
        firebase_key_dict = json.loads(v)  # 최종 dict
    except json.JSONDecodeError:
        raise RuntimeError("FIREBASE_KEY_JSON 내부 문자열이 올바른 JSON이 아닙니다.")
elif isinstance(v, dict):
    firebase_key_dict = v
else:
    raise RuntimeError("FIREBASE_KEY_JSON는 JSON 객체여야 합니다.")

# Firebase Admin 초기화 (중복 방지)
# 이미 초기화되어 있으면 재사용, 없으면 한 번만 초기화
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://npc-bot-add0a-default-rtdb.firebaseio.com")
try:
    firebase_admin.get_app()  # 기본 앱 존재 여부 확인
except ValueError:
    cred = credentials.Certificate(firebase_key_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})


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

async def update_season_voice_channels(_bot: commands.Bot):
    for guild in _bot.guilds:
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
INACTIVE_KICK_DAYS = 30  # 원하는 기준일로
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

async def aget_user_exp(uid: str):
    def _get():
        return db.reference("exp_data").child(uid).get() or {"exp": 0, "level": 1, "voice_minutes": 0}
    return await asyncio.to_thread(_get)

async def aget_user_mission(uid: str, today: str):
    def _get():
        base = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}
        val = db.reference("mission_data").child(uid).get()
        return val or base
    return await asyncio.to_thread(_get)


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
    (1,   5,  200,   1.040, 1.00),   # 튜토리얼(가볍게)
    (6,  10,  None,  1.045, 1.10),
    (11, 15,  None,  1.050, 1.11),
    (16, 20,  None,  1.056, 1.12),
    (21, 25,  None,  1.063, 1.12),
    (26, 30,  None,  1.071, 1.13),
    (31, 35,  None,  1.080, 1.14),
    (36, 40,  None,  1.090, 1.15),
    (41, 45,  None,  1.101, 1.16),
    (46, 50,  None,  1.113, 1.17),
    (51, 55,  None,  1.126, 1.18),   # 50→60 완만 상승
    (56, 60,  None,  1.140, 1.19),   # 60대 ‘벽’ 제거(미세 증가)
    (61, 65,  None,  1.155, 1.20),   # 고레벨 진입이지만 급점프 없음
    (66, 70,  None,  1.171, 1.21),   # 엔드게임: 꾸준히 가파르되 ‘절벽’은 아님
    (71, 75,  None,  1.196, 1.22),   # 엔드게임: 꾸준히 가파르되 ‘절벽’은 아님
    (76, 80,  None,  1.213, 1.23),   # 엔드게임: 꾸준히 가파르되 ‘절벽’은 아님
    (81, 90,  None,  1.241, 1.24),   # 엔드게임: 꾸준히 가파르되 ‘절벽’은 아님
    (91, 99,  None,  1.270, 1.25),   # 엔드게임: 꾸준히 가파르되 ‘절벽’은 아님

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

# ─── 데바운스 적용 헬퍼 함수 추가 ────────────────────────────

async def update_role_and_nick(member: discord.Member, new_level: int):
    """
    역할·닉네임 변경을 5분에 한 번만 수행하도록 데바운스 처리합니다.
    """
    uid = member.id
    if uid in recent_role_updates:
        return  # 이미 5분 이내에 업데이트 했으므로 스킵

    recent_role_updates.add(uid)
    asyncio.get_event_loop().call_later(300, lambda: recent_role_updates.discard(uid))

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
# === [SAFEGUARD UTILS] ===
def _is_bot_message(message) -> bool:
    # 봇/웹훅은 무시
    if getattr(message.author, "bot", False):
        return True
    if getattr(message, "webhook_id", None):
        return True
    return False

def _is_low_value_context(message) -> bool:
    # DM, 스레드 등 필요 시 필터링
    try:
        if isinstance(message.channel, discord.DMChannel):
            return True
        # 스레드 필터링이 필요하면 아래 주석 해제
        # if isinstance(message.channel, discord.Thread):
        #     return True
    except Exception:
        pass
    return False

def _hit_cooldowns(message):
    """쿨다운을 위반하면 이유 문자열을 반환, 아니면 None"""
    global _last_global_ts
    now = time.time()

    # 전역 쿨다운
    if now - _last_global_ts < SAFEGUARD_MIN_INTERVAL_GLOBAL:
        return "global_cooldown"
    _last_global_ts = now

    # 채널 쿨다운
    ch_id = getattr(message.channel, "id", None)
    if ch_id is not None:
        if now - _last_channel_ts[ch_id] < SAFEGUARD_MIN_INTERVAL_PER_CHANNEL:
            return "channel_cooldown"
        _last_channel_ts[ch_id] = now

    # 유저 쿨다운
    user_id = getattr(message.author, "id", None)
    if user_id is not None:
        if now - _last_user_ts[user_id] < SAFEGUARD_MIN_INTERVAL_PER_USER:
            return "user_cooldown"
        _last_user_ts[user_id] = now

    return None

# ---- Discord Bot 초기화 (슬래시 전용) ---
intents = discord.Intents.all()

# --- AllowedMentions 공통 설정 (핑 방지용) ---
ALLOW_NO_PING = discord.AllowedMentions(
    everyone=False,     # @everyone 금지
    users=False,        # 유저 멘션 금지
    roles=False,        # 역할 멘션 금지
    replied_user=False  # 답장 대상 멘션 금지
)
# --- /END AllowedMentions 설정 ---

bot = commands.Bot(
    command_prefix=commands.when_mentioned,     # 프리픽스 명령어 비활성화
    help_command=None,      # 기본 도움말 명령어 비활성화
    intents=intents
)


# ---- on_ready ----
@bot.event
async def on_ready():

    # 2) 시즌 보이스 채널 업데이트 (예외 로깅)
    try:
        await update_season_voice_channels(bot)
    except Exception as e:
        print(f"[on_ready] update_season_voice_channels error: {e!r}")


    print(f"✅ {bot.user} 온라인")
    logging.info(f"[ready] logged in as {bot.user} (id={bot.user.id})")
    await bot.change_presence(activity=discord.Game("제가 오프라인이라면, 서버장에게 말해주세요!"))
    
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
        await update_season_voice_channels(bot)

    # 특정 스레드 역할이 부여되면 환영 메시지
    if THREAD_ROLE_ID in added:
        channel = bot.get_channel(TARGET_TEXT_CHANNEL_ID)
        if channel:
            await channel.send(
                f"환영합니다 {after.mention} 님! '사계절, 그 사이' 서버입니다.\n"
                "프로필 우클릭 → 편집으로 닉네임을 변경할 수 있어요!\n"
                "닉네임은 한글만 사용 가능합니다!"
            )

        # DB에서 경험치, 레벨 로드 후 역할/닉네임 동기화
        uid = str(after.id)
        user_data = await aget_user_exp(uid)
        new_level = calculate_level(user_data["exp"])

        # 역할/닉네임 동기화 (데바운스 적용 + 예외 내성)
        try:
            await update_role_and_nick(after, new_level)
        except Exception as e:
            logging.exception(f"[on_member_update] role/nick sync failed: {e}")


# ---- 백그라운드 태스크 정의 ----
@tasks.loop(hours=24)
async def inactive_user_log_task():
    """30일 미접속 사용자 추방 + 로그"""
    threshold = datetime.now(KST) - timedelta(days=INACTIVE_KICK_DAYS)
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

            user = await aget_user_exp(str(member.id))
            if not user or not user.get("last_activity"):
                continue

            last_active = datetime.fromtimestamp(user["last_activity"], KST)
            if last_active < threshold:
                # DM 시도
                try:
                    embed = discord.Embed(
                        title="📢 사계절, 그 사이 서버 안내",
                        description=(
                            "안녕하세요, '사계절, 그 사이' 서버 서버장입니다!\n\n"
                            f"최근 {INACTIVE_KICK_DAYS}일간 서버에 기록된 활동 내역이 없어,\n"
                            "공지해둔 규칙 사항에 따라 서버에서 추방 처리가 진행됩니다 !\n\n"
                            "개인 사정에 의해, 혹은 기록 누락 등 피치 못할 사정으로 추방되신 분들,\n"
                            "잠깐 다른 서버나 현생으로 인해 저희 서버를 깜박하셨던 분들 모두\n"
                            "아래의 링크를 통해 언제든 다시 서버에 입장하실 수 있습니다.\n\n"
                            "분명, 지나온 계절보다 앞으로 계절이 더 재밌을거에요.\n\n"
                            "👉 https://discord.gg/Npuxrkf38G\n\n"
                            "앞으로 더 발전하는 서버로 찾아뵙겠습니다 !\n\n"
                            "- '사계절, 그 사이' 서버장 새벽녘 (새벽녘#0001) -"
                        ),
                        color=0x3498db
                    )
                    await member.send(embed=embed)
                except:
                    await log_channel.send(f"❌ {member.display_name} 님에게 DM 전송 실패")

                # 추방
                try:
                    await member.kick(reason=f"{INACTIVE_KICK_DAYS}일 미접속 자동 추방")
                    await log_channel.send(f"👢 {member.display_name} 님이 {INACTIVE_KICK_DAYS}일간 미접속으로 추방되었습니다.")
                    kicked.append(member.display_name)
                except Exception as e:
                    await log_channel.send(f"❌ {member.display_name} 님 추방 실패: {e}")

    # ✅ 아무도 추방되지 않았을 경우에도 로그 남기기
    if not kicked:
        await log_channel.send(f"✅ 현재 {INACTIVE_KICK_DAYS}일 이상 미접속 중인 사용자가 없습니다.")

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

    for guild in bot.guilds:
        # 보이스 + 스테이지 채널 모두 포함
        try:
            voice_like_channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        except Exception:
            voice_like_channels = list(guild.voice_channels)

        for vc in voice_like_channels:
            if vc.id in AFK_CHANNEL_IDS:
                continue

            is_special = vc.category and vc.category.id in SPECIAL_VC_CATEGORY_IDS
            for member in vc.members:
                if member.bot:
                    continue
                try:
                    uid = str(member.id)
                    user_data = await aget_user_exp(uid)

                    # 안전 보정: 낡은 레코드 방어
                    user_data.setdefault("exp", 0)
                    user_data.setdefault("voice_minutes", 0)
                    user_data.setdefault("level", calculate_level(user_data.get("exp", 0)))

                    gain = random.randint(VOICE_MIN_XP, VOICE_MAX_XP)
                    if is_special:
                        gain = max(1, int(gain * 0.2))

                    user_data["exp"] += gain
                    if not is_special:
                        user_data["voice_minutes"] += 1

                    user_data["last_activity"] = now_ts
                    new_level = calculate_level(user_data["exp"])

                    if new_level != user_data.get("level", 1):
                        user_data["level"] = new_level

                        # 역할·닉네임 변경 (데바운스 적용)
                        await update_role_and_nick(member, new_level)

                        # 레벨업 알림 유지
                        announce = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
                        if announce:
                            await announce.send(
                                f"🎉 {member.display_name} 님이 Lv.{new_level} 에 도달했습니다! 🎊",
                                allowed_mentions=ALLOW_NO_PING
                            )

                    await asave_user_exp(uid, user_data)
                except Exception as e:
                    logging.exception(f"[voice_xp_task] uid={getattr(member, 'id', '?')} error: {e}")
                    continue

@voice_xp_task.error
async def voice_xp_task_error(error):
    logging.exception(f"[voice_xp_task] crashed: {error}")
    try:
        # 예외로 루프가 중지됐으면 재시작 시도
        if not voice_xp_task.is_running():
            voice_xp_task.start()
    except Exception as e2:
        logging.exception(f"[voice_xp_task] restart failed: {e2}")
        
@tasks.loop(seconds=60)
async def repeat_vc_mission_task():
    """반복 VC 미션 보상 태스크"""
    mission_data = await aload_mission_data()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    for guild in bot.guilds:
         # 보이스 + 스테이지 채널 모두 포함
        voice_like_channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        for vc in voice_like_channels:
            humans = [m for m in vc.members if not m.bot]

            # 🅰 AFK 채널은 미션 지급 제외 (이유 로그)
            if vc.id in AFK_CHANNEL_IDS:
                logging.debug(f"[repeat_vc_mission] skip AFK vc_id={vc.id}")
                continue

            # 🅱 인원 수 미달 시 미션 지급 제외 (이유 로그)
            if len(humans) < REPEAT_VC_MIN_PEOPLE:
                logging.debug(
                    f"[repeat_vc_mission] skip not enough people vc_id={vc.id} "
                    f"count={len(humans)}/{REPEAT_VC_MIN_PEOPLE}"
                )
                continue

            for member in humans:
                if member.bot:
                    continue

                uid = str(member.id)
                user_m = mission_data.get(uid, {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}})
                if user_m["date"] != today:
                    user_m = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

                user_m["repeat_vc"]["minutes"] += 1
                if user_m["repeat_vc"]["minutes"] % REPEAT_VC_REQUIRED_MINUTES == 0:
                    uexp = await aget_user_exp(uid)
                    uexp["exp"] += REPEAT_VC_EXP_REWARD
                    uexp["level"] = calculate_level(uexp["exp"])
                    uexp["last_activity"] = time.time()
                    await asave_user_exp(uid, uexp)

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


@bot.event
async def on_message(message):
    # === [SAFEGUARD IN on_message] ===
    try:
        if _is_bot_message(message):
            return
        if _is_low_value_context(message):
            return
        cd_reason = _hit_cooldowns(message)
        if cd_reason is not None:
            # print(f"[on_message] skipped due to {cd_reason}")
            return
    except Exception as e:
        print(f"[on_message] safeguard pre-check error: {e!r}")
        return
    # === [/SAFEGUARD IN on_message] ===

    try:
        # ✅ 메시지 전처리: 내용 없으면 빠르게 종료 (이모지/파일만 등의 케이스)
        text = (message.content or "").strip()
        if not text:
            return
        text_lower = text.lower()

        # 1) 특정 스레드 채팅 감지 시 역할 자동 부여 (권한/널 가드)
        if getattr(message.channel, "id", None) == THREAD_ROLE_CHANNEL_ID and message.guild:
            role = message.guild.get_role(THREAD_ROLE_ID)
            member = getattr(message, "author", None)
            if role and isinstance(member, discord.Member) and role not in member.roles:
                try:
                    await member.add_roles(role, reason="thread activity auto-assign")
                except discord.Forbidden:
                    logging.warning("[role] lacking permissions to add role")
                except Exception as e:
                    logging.exception(f"[role] add_roles error: {e}")

        # 2) 채팅 경험치 처리 로직
        uid = str(message.author.id)
        now_ts = time.time()
        user_data = await aget_user_exp(uid)

        if now_ts - user_data.get("last_activity", 0) >= COOLDOWN_SECONDS:
            gain = random.randint(1, 30)
            user_data["exp"] += gain
            user_data["last_activity"] = now_ts

        # 3) 레벨업 분기
        new_level = calculate_level(user_data["exp"])
        if new_level != user_data.get("level", 1):
            user_data["level"] = new_level
            await update_role_and_nick(message.author, new_level)


        # 4) 텍스트 미션 집계 (유저 단일 로드/저장)
        today = datetime.now(KST).strftime("%Y-%m-%d")
        user_m = await aget_user_mission(uid, today)

        if user_m.get("date") != today:
            user_m = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}

        if not user_m["text"]["completed"]:
            user_m["text"]["count"] += 1
            if user_m["text"]["count"] >= MISSION_REQUIRED_MESSAGES:
                # 유저 EXP에 바로 반영(메모리 상)
                user_data["exp"] += MISSION_EXP_REWARD
                user_data["level"] = calculate_level(user_data["exp"])
                user_data["last_activity"] = time.time()  # ← (정책 선택) 미션 완료도 활동으로 간주하려면 유지, 아니면 제거

                log_ch = bot.get_channel(LOG_CHANNEL_ID)
                if log_ch:
                    await log_ch.send(f"[🧾 로그] {message.author.display_name} 님 텍스트 미션 완료! +{MISSION_EXP_REWARD}XP")
                await message.channel.send(f"🎯 {message.author.mention} 일일 미션 완료! +{MISSION_EXP_REWARD}XP 지급되었습니다.")
                user_m["text"]["completed"] = True

        # (중요) 전체 저장 제거 → 유저 단위 저장만
        await asave_user_mission(uid, user_m)

        # ✅ 최종 EXP 저장 1회 (on_message 맨 끝에서 저장)
        await asave_user_exp(uid, user_data)

    except Exception as e:
        print(f"❌ on_message 처리 중 오류: {e}")

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
            user = await aget_user_exp(str(interaction.user.id))
            last_ts = user.get("last_activity")
            if last_ts:
                last_dt = datetime.fromtimestamp(last_ts, KST)
                days_ago = (datetime.now(KST) - last_dt).days
                last_seen = f"{days_ago}일 전 ({last_dt.strftime('%Y.%m.%d %H:%M')})"
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
    user = await aget_user_exp(uid)

    if not user:
        return await interaction.response.send_message(f"{member.display_name}님의 정보가 존재하지 않습니다.", ephemeral=True)

    level = user.get("level", 1)
    exp = user.get("exp", 0)
    last_ts = user.get("last_activity")

    if last_ts:
        last_dt = datetime.fromtimestamp(last_ts, KST)
        elapsed = datetime.now(KST) - last_dt
        days_ago = elapsed.days
        last_seen = last_dt.strftime("%Y. %m. %d %H:%M")
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
    uid = str(member.id)
    user_data = await aget_user_exp(uid)
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
            await ch_log.send(
                f"🎉 {member.display_name} 님이 Lv.{new_level} 에 도달했습니다! 🎊",
                allowed_mentions=ALLOW_NO_PING
            )

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
    uid = str(member.id)
    user_data = await aget_user_exp(uid)

    # 경험치 차감 및 레벨 재계산
    user_data["exp"] = max(0, user_data["exp"] - amount)
    user_data["level"] = calculate_level(user_data["exp"])

    # DB 저장
    await asave_user_exp(uid, user_data)

    # 역할·닉네임 변경 (데바운스 적용)
    await update_role_and_nick(member, user_data["level"])

    await interaction.response.send_message(f"✅ {member.mention}에게서 경험치 {amount}XP 차감 완료!", ephemeral=True)
# ---- 기타 슬래시 커맨드 핸들러 (/정보, /퀘스트, /랭킹, /출석, /출석랭킹) ----
                                            
@bot.tree.command(name="정보", description="내 정보를 이미지 카드로 확인합니다")
async def info(interaction: discord.Interaction):
    # defer부터 안전하게
    try:
        await interaction.response.defer()
    except discord.NotFound:
        # 10062 Unknown interaction: 이미 만료됨
        return
    except Exception:
        # defer 실패는 일단 종료
        return

    try:
        logging.info("[/정보] start")

        user = interaction.user
        uid = str(user.id)

        logging.info("[/정보] load exp (all)")
        all_exp = await aload_exp_data()          # ✅ 인자 없이
        exp_data = all_exp.get(uid) if all_exp else None

        if not exp_data:
            await interaction.followup.send("데이터가 없습니다.")
            return

        total_xp = int(exp_data.get("exp", 0))
        level = calculate_level(total_xp)

        if exp_data.get("level") != level:
            exp_data["level"] = level
            await asave_user_exp(uid, exp_data)  # 이 함수는 유저 단위 저장이 맞는지 기존 코드와 동일해야 함

        prev_thr = THRESHOLDS[level - 1] if (level - 1) < len(THRESHOLDS) else THRESHOLDS[-1]
        next_thr = THRESHOLDS[level] if level < len(THRESHOLDS) else THRESHOLDS[-1]
        cur_xp = max(0, total_xp - prev_thr)
        need_xp = max(1, next_thr - prev_thr)
        pct = cur_xp / need_xp

        logging.info("[/정보] fetch avatar")
        avatar_bytes = None
        try:
            avatar_url = user.display_avatar.replace(size=256).url
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(avatar_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    logging.info(f"[/정보] avatar resp={resp.status}")
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
        except Exception:
            logging.exception("[/정보] avatar fetch failed")
            avatar_bytes = None

        logging.info("[/정보] render image")
        buf = await asyncio.wait_for(
            asyncio.to_thread(
                render_rank_card,
                display_name=user.display_name,
                level=level,
                total_xp=total_xp,
                cur_xp=cur_xp,
                need_xp=need_xp,
                pct=pct,
                avatar_bytes=avatar_bytes,
            ),
            timeout=8,
        )

        logging.info("[/정보] send file")
        await interaction.followup.send(file=discord.File(fp=buf, filename="rank.png"))
        logging.info("[/정보] done")

    except asyncio.TimeoutError:
        logging.exception("[/정보] timeout")
        try:
            await interaction.followup.send("응답이 지연되어 중단했습니다. (타임아웃)")
        except Exception:
            pass
    except Exception as e:
        logging.exception("[/정보] error")
        try:
            await interaction.followup.send(f"처리 중 오류: {type(e).__name__}")
        except Exception:
            pass


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
    last_date = attendance.get("last_date")
    attended = (last_date == today)
    attendance_status = f"상태: {'✅ 출석 완료' if attended else '❌ 출석 안됨'}"

    embed = discord.Embed(title="📜 퀘스트 현황", color=discord.Color.green())
    embed.add_field(name="🗨️ 텍스트 미션", value=text_status, inline=False)
    embed.add_field(name="📞 5인 이상 통화방 참여 미션", value=vc_status, inline=False)
    embed.add_field(name="🗓️ 출석", value=attendance_status, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="랭킹", description="경험치 랭킹을 확인합니다.")
async def ranking(interaction: discord.Interaction):
    # 전체 EXP 데이터 1회 로드 (읽기 전용)
    data = await aload_exp_data()
    if not isinstance(data, dict):
        data = {}

    # 경험치 기준 상위 정렬
    sorted_users = sorted(
        data.items(),
        key=lambda x: x[1].get("exp", 0),
        reverse=True
    )

    # 상위 10명 라인 생성
    desc_lines = []
    for idx, (uid, u) in enumerate(sorted_users[:10], start=1):
        try:
            member = await interaction.guild.fetch_member(int(uid))
            name = member.display_name
        except:
            name = "Unknown"
        level = u.get("level", 1)
        exp = u.get("exp", 0)
        desc_lines.append(f"{idx}위. {name} - Lv. {level} ({exp:,} XP)")

    # 내 순위
    my_rank = None
    me = str(interaction.user.id)
    for idx, (uid, u) in enumerate(sorted_users, start=1):
        if uid == me:
            my_rank = f"당신의 순위: {idx}위 - Lv. {u.get('level',1)} ({u.get('exp',0):,} XP)"
            break

    # Embed
    embed = discord.Embed(
        title="🏆 경험치 랭킹",
        description="\n".join(desc_lines) if desc_lines else "랭킹 데이터가 없습니다.",
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
    prev_last = ud.get("last_date", "")

    if prev_last == today_str:
        until = (now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)) - now
        h, m = divmod(int(until.total_seconds()/60), 60)
        return await interaction.response.send_message(f"이미 출석 완료! 다음 출석까지 {h}시간 {m}분 남음.")
        
    ud["streak"] = ud["streak"] + 1 if prev_last == yesterday else 1
    ud["last_date"] = today_str
    ud["total_days"] += 1
    ud.setdefault("weekly", {})[week] = ud["weekly"].get(week,0)+1
    ud.setdefault("monthly", {})[month] = ud["monthly"].get(month,0)+1
    # 경험치 지급
    gain = 100 + min(ud["streak"] - 1, 10) * 10
    ue = await aget_user_exp(uid)
    prev_level = ue["level"]
    ue["exp"] += gain
    ue["level"] = calculate_level(ue["exp"])
    ue["last_activity"] = time.time()

    if ue["level"] > prev_level:
        announce = bot.get_channel(LEVELUP_ANNOUNCE_CHANNEL)
        if announce:
            await announce.send(
                f"🎉 {interaction.user.display_name} 님이 Lv.{ue['level']} 에 도달했습니다! 🎊",
                allowed_mentions=ALLOW_NO_PING
            )


    await asave_user_exp(uid, ue)
    await aset_attendance_data(uid, ud)
    await update_role_and_nick(interaction.user, ue["level"])
    first_attend = ud["total_days"] == 1
    streak_reset = (ud["streak"] == 1 and prev_last != yesterday)

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
from aiohttp import web

# ---- 실행 및 웹 서버 유지 (aiohttp, same event loop) ----
async def health(_request):
    return web.Response(text="Bot is running!")

_web_runner = None

async def start_web_app():
    global _web_runner
    try:
        app = web.Application()
        app.router.add_get("/", health)

        _web_runner = web.AppRunner(app)
        await _web_runner.setup()

        port = int(os.getenv("PORT", "10000"))
        site = web.TCPSite(_web_runner, host="0.0.0.0", port=port)
        await site.start()

        logging.info(f"[web] listening on 0.0.0.0:{port}")
    except Exception as e:
        logging.exception(f"[web] failed to start: {e}")
        # 웹이 죽어도 봇은 계속 켠다

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
            print("[login] bot.start returned unexpectedly. restarting soon.")
            try:
                await bot.close()
            except Exception:
                pass
            await asyncio.sleep(10)
            continue
            
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

# 프로그램 시작 시: 포트를 먼저 바인딩하고, 그 다음 디스코드 봇을 시작
async def _main():
    # 포트 바인딩(웹 서버) 먼저 시작 → Render의 포트 스캔 통과
    await start_web_app()
    # 이후 디스코드 로그인 루프 진입
    await _safe_start()

if __name__ == "__main__":
    asyncio.run(_main())
