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

_QUEST_BG_PATH = os.path.join(_ASSET_DIR, "quest_banner_bg.png")
_QUEST_BG_TEMPLATE = None  # type: Optional[Image.Image]

def _get_quest_bg_template() -> Image.Image:
    global _QUEST_BG_TEMPLATE
    if _QUEST_BG_TEMPLATE is None:
        try:
            bg = Image.open(_QUEST_BG_PATH).convert("RGBA")
        except Exception:
            # 파일 없으면 rank_bg로 폴백
            bg = _get_bg_template()
        _QUEST_BG_TEMPLATE = bg
    return _QUEST_BG_TEMPLATE


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

def render_daily_quest_banner(
    *,
    display_name: str,
    pct_int: int,
    height: int = 70,
    reward_pct: int = 1,
) -> BytesIO:
    """
    채팅 한 줄 체감용 초슬림 배너 (아이콘 없음, 단일 행)
    레이아웃:
    [일일 퀘스트 성공!  경험치 1% 지급   |   서버 닉네임 님의   |   현재 경험치 37%]
    """
    bg = _get_quest_bg_template()
    w = bg.size[0]
    h = int(height)

    base = bg.crop((0, 0, w, min(h, bg.size[1]))).copy()
    if base.size[1] != h:
        img = Image.new("RGBA", (w, h), (245, 245, 245, 255))
        img.paste(base, (0, 0))
    else:
        img = base

    draw = ImageDraw.Draw(img)
    font = _get_font(16)

    x = 18
    max_w = w - (x * 2)

    title = "일일 퀘스트 성공!"
    reward = f"경험치 {reward_pct}% 지급"
    nick = f"{display_name} 님의"
    prog = f"현재 경험치 {max(0, min(100, int(pct_int)))}%"

    sep = "   |   "
    line = f"{title}  {reward}{sep}{nick}{sep}{prog}"

    safe_line = _ellipsize(draw, line, font, max_w)
    
    bbox = draw.textbbox((0, 0), safe_line, font=font)
    text_h = bbox[3] - bbox[1]
    y = (h - text_h) // 2 - bbox[1]
    
    draw.text((x, y), safe_line, font=font, fill=(0, 0, 0, 255))

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# =======================================================================

KST = pytz.timezone("Asia/Seoul")  # ← 추가

# =========================
# Attendance (출석) 메시지/보호권 설정
# =========================
ATTEND_MILESTONE_STREAKS = {3, 7, 14, 30, 50, 100}

ATTEND_MSG_ALREADY = [
    "📺 (속보) {mention} 오늘 출석, 이미 처리 완료",
    "🧾 (기록 확인) {mention} 오늘 도장: 찍힘",
    "🕰️ (리마인드) {mention} 오늘 건은 수령 완료 상태",
    "🚫 (제한 안내) {mention} 1일 1회 규정 적용 중",
    "🔒 (봉인됨) {mention} 오늘 출석 슬롯: 닫힘",
    "📌 (체크 완료) {mention} 오늘 항목: 완료 표시",
    "🎛️ (시스템) {mention} 중복 요청 감지: 처리 생략",
    "📷 (현장) {mention} 이미 찍힌 도장 화면 확보",
    "🗂️ (로그) {mention} 오늘자 출석 로그 존재",
    "📦 (수령 내역) {mention} 오늘 보상: 수령됨",
    "🧯 (과열 방지) {mention} 연타 방지 모드 작동",
    "🧊 (쿨다운) {mention} 오늘은 여기까지",
    "📎 (첨부) {mention} 오늘 출석 확인서 발급 완료",
    "🔔 (알림) {mention} 오늘 출석은 이미 끝난 이야기",
    "🧷 (고정) {mention} 오늘 체크는 더 이상 갱신되지 않음",
]

ATTEND_MSG_SUCCESS = [
    "🎉 (자막) {mention} 오늘도 무사 통과",
    "🥁 (효과음) {mention} 도장 “딱”",
    "🏁 (완료) {mention} 오늘 구간 클리어",
    "📌 (확정) {mention} 출석 처리 완료",
    "📈 (상승) {mention} 연속 기록 유지 중",
    "🔥 (유지력) {mention} 루틴이 꺼지지 않는다",
    "🧭 (정상 항로) {mention} 오늘도 경로 이탈 없음",
    "🧱 (적립) {mention} 한 칸 추가 적립",
    "🎬 (엔딩) {mention} 오늘의 출석, 깔끔한 마무리",
    "📣 (공지) {mention} 출석 완료 처리되었습니다",
    "🗃️ (저장) {mention} 오늘 기록 저장 완료",
    "🧲 (흡착) {mention} 습관이 또 붙었다",
    "🎯 (명중) {mention} 출석 타이밍 적중",
    "🛎️ (완료음) {mention} 처리 완료 신호",
    "📡 (송출) {mention} 출석 성공 신호 수신",
    "🧨 (기세) {mention} 연속 흐름 계속 간다",
    "🧽 (깔끔) {mention} 오늘도 정리정돈 완료",
    "🪪 (인증) {mention} 오늘 출석 인증 통과",
    "🔧 (정상 작동) {mention} 출석 모듈 이상 없음",
    "🎊 (장면 전환) {mention} 다음 출석은 내일로 넘어갑니다",
]

ATTEND_MSG_FIRST = [
    "🆕 (감지) {mention} 새로운 출석 기록 생성",
    "🎬 (오프닝) {mention} 1일차 장면 시작",
    "📍 (첫 체크) {mention} 오늘이 첫 도장입니다",
    "🗂️ (신규 등록) {mention} 출석 카드 발급 완료",
    "🚦 (출발) {mention} 이제부터 누적이 쌓입니다",
    "🧩 (첫 조각) {mention} 퍼즐 1칸 채움",
    "🏗️ (기초 공사) {mention} 기록의 바닥을 다졌습니다",
    "🎟️ (입장) {mention} 출석 루틴에 입장했습니다",
    "🧾 (초안 작성) {mention} 오늘부터 로그가 남습니다",
    "🔰 (스타트) {mention} 시작 마크 확인",
]

ATTEND_MSG_RESET = [
    "🧊 (알림) {mention} 연속 기록이 1일로 재설정됩니다",
    "📉 (변동) {mention} 연속 흐름이 끊겼습니다",
    "🪓 (컷) {mention} 콤보 종료, 오늘부터 다시 시작",
    "🧯 (진화) {mention} 불은 꺼졌고, 다시 붙이면 됩니다",
    "🕳️ (이탈) {mention} 연속 구간에서 벗어났습니다",
    "🧽 (리셋) {mention} 연속 수치 초기화 처리",
    "🔄 (재정렬) {mention} 연속 기록 1일차로 정렬",
    "📎 (참고) {mention} 누적은 유지, 연속만 리셋",
    "🪫 (방전) {mention} 연속 배터리 0%, 오늘부터 충전",
    "⛔ (중단) {mention} 연속 기록 중단 확인",
    "🧱 (재시작) {mention} 다시 한 칸부터 쌓습니다",
    "📌 (확정) {mention} 연속 끊김 상태로 출석 처리",
]

ATTEND_MSG_SHIELD_USED = [
    "🛡️ (방어 성공) {mention} 보호권 1장 소모, 연속 기록 유지",
    "🛡️ (처리) {mention} 보호권 사용 완료 — 흐름이 이어집니다",
    "🛡️ (복구) {mention} 연속 구간 복원 처리",
    "🛡️ (세이브) {mention} 기록이 보호되었습니다",
    "🛡️ (판정) {mention} 끊김 판정 무효 처리",
    "🛡️ (유지) {mention} 연속 {streak}일 유지 성공",
]

ATTEND_MSG_SHIELD_SKIPPED = [
    "✅ (확정) {mention} 보호권 미사용 — 연속은 1일부터 재시작",
    "📌 (처리) {mention} 출석은 완료, 연속 리셋 적용",
    "📉 (반영) {mention} 끊김 상태로 기록 저장",
    "🔄 (결정 반영) {mention} 미사용 선택, 연속 초기화",
    "🧊 (정리) {mention} 연속은 끊겼지만 누적은 유지됩니다",
    "🏁 (완료) {mention} 오늘 출석 처리 종료",
]

ATTEND_MSG_MILESTONE = [
    "🏅 (기록) {mention} 연속 {streak}일 달성",
    "🎖️ (인증) {mention} 연속 {streak}일 구간 진입",
    "📣 (특보) {mention} 연속 {streak}일, 축하 출력 송출",
    "🧱 (누적) {mention} 연속 {streak}일, 기반이 단단해졌습니다",
    "🎇 (이벤트) {mention} 연속 {streak}일 체크포인트 통과",
    "🏁 (분기점) {mention} 연속 {streak}일 구간 완료",
    "📌 (배지) {mention} 연속 {streak}일 표식 부착",
    "🥇 (랭크업) {mention} 연속 {streak}일, 컨디션 최상",
    "📈 (상향) {mention} 연속 {streak}일, 상승세 유지",
    "🎬 (하이라이트) {mention} 연속 {streak}일 장면 저장",
]

def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def normalize_attendance_record(ud: dict | None) -> dict:
    if not isinstance(ud, dict):
        ud = {}
    ud.setdefault("last_date", "")
    ud["total_days"] = max(0, _safe_int(ud.get("total_days", 0), 0))
    ud["streak"] = max(0, _safe_int(ud.get("streak", 0), 0))
    if not isinstance(ud.get("weekly"), dict):
        ud["weekly"] = {}
    if not isinstance(ud.get("monthly"), dict):
        ud["monthly"] = {}
    ud["shield_tokens"] = max(0, _safe_int(ud.get("shield_tokens", 0), 0))
    ud.setdefault("shield_grant_month", "")
    return ud

def ensure_monthly_shield_grant(ud: dict, now_kst: datetime) -> bool:
    """
    월 1회 보호권 1장 자동 지급(중복 방지).
    - ud["shield_grant_month"]가 이번달 키와 다르면 1장 지급
    """
    month_key = get_month_key_kst(now_kst)  # 기존 월 키 포맷과 통일 (YYYY-M)
    if ud.get("shield_grant_month") != month_key:
        ud["shield_tokens"] = max(0, _safe_int(ud.get("shield_tokens", 0), 0)) + 1
        ud["shield_grant_month"] = month_key
        return True
    return False

def _until_next_attendance(now_kst: datetime) -> tuple[int, int]:
    until = (now_kst.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)) - now_kst
    h, m = divmod(int(until.total_seconds() // 60), 60)
    return h, m

def _build_attendance_stats_line(total_days: int, streak: int, gain: int | None = None) -> str:
    if gain is None:
        return f"누적 {total_days}일 · 연속 {streak}일"
    return f"누적 {total_days}일 · 연속 {streak}일 · +{gain} XP"

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
DISCONNECT_LOG_CHANNEL_ID = 1506202471058509904
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

async def aget_attendance_user(uid: str) -> dict:
    return await asyncio.to_thread(get_attendance_user, uid)

async def aset_attendance_user(uid: str, data: dict):
    return await asyncio.to_thread(set_attendance_user, uid, data)

async def abulk_update_attendance(updates: dict):
    return await asyncio.to_thread(bulk_update_attendance, updates)

async def aget_user_exp(uid: str):
    def _get():
        raw = db.reference("exp_data").child(uid).get()

        # 1) 레코드 자체가 없으면 기본값
        if not isinstance(raw, dict):
            return {"exp": 0, "level": 1, "voice_minutes": 0}

        # 2) exp 보정 (없거나 타입 이상하면 0)
        exp = raw.get("exp", 0)
        try:
            exp = int(exp)
        except Exception:
            exp = 0
        if exp < 0:
            exp = 0

        # 3) 나머지 키도 기본값 보장
        vm = raw.get("voice_minutes", 0)
        try:
            vm = int(vm)
        except Exception:
            vm = 0
        if vm < 0:
            vm = 0

        lvl = raw.get("level", 1)
        try:
            lvl = int(lvl)
        except Exception:
            lvl = 1

        # 4) 반환값은 “항상 완전한 스키마”
        raw["exp"] = exp
        raw["voice_minutes"] = vm
        raw["level"] = lvl
        return raw

    return await asyncio.to_thread(_get)


async def aget_user_mission(uid: str, today: str):
    def _get():
        base = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}
        val = db.reference("mission_data").child(uid).get()
        return val or base
    return await asyncio.to_thread(_get)

# =========================
# Guild (server) config IO
# =========================

_GUILD_CONFIG_CACHE = {}          # guild_id(str) -> dict
_GUILD_CONFIG_CACHE_TS = {}       # guild_id(str) -> float
_GUILD_CONFIG_TTL = 30.0          # seconds

def _guild_cfg_ref(guild_id: int):
    return db.reference("guild_config").child(str(guild_id))

def _default_guild_config() -> dict:
    # 최소 스키마. 없으면 dict 합치기 쉬움.
    return {
        "channels": {},
        "roles": {},
        "voice": {
            "afk_channel_ids": [],
            "special_vc_category_ids": [],
        },
        "season_map": {},  # "봄": {"role_id":..., "channel_id":...}
        "features": {
            "season_voice_enabled": True,
        }
    }
    
_COUNT_SUFFIX_RE = re.compile(r"(\d+)명$")

def _replace_count_suffix(name: str, count: int):
    m = _COUNT_SUFFIX_RE.search(name or "")
    if not m:
        return None
    return name[:m.start(1)] + f"{count}명"


async def aget_guild_config(guild_id: int) -> dict:
    now = time.time()
    gid = str(guild_id)
    ts = _GUILD_CONFIG_CACHE_TS.get(gid, 0.0)
    if gid in _GUILD_CONFIG_CACHE and (now - ts) < _GUILD_CONFIG_TTL:
        return _GUILD_CONFIG_CACHE[gid]

    def _get():
        val = _guild_cfg_ref(guild_id).get() or {}
        base = _default_guild_config()
        # 얕은 병합(필요 키 보장)
        for k, v in base.items():
            if k not in val or not isinstance(val.get(k), type(v)):
                val[k] = v
        return val

    cfg = await asyncio.to_thread(_get)
    _GUILD_CONFIG_CACHE[gid] = cfg
    _GUILD_CONFIG_CACHE_TS[gid] = now
    return cfg

async def aset_guild_config_field(guild_id: int, path: str, value):
    # path 예: "channels/log_channel_id"
    def _set():
        ref = _guild_cfg_ref(guild_id)
        parts = [p for p in path.split("/") if p]
        node = ref
        for p in parts[:-1]:
            node = node.child(p)
        node.child(parts[-1]).set(value)

    await asyncio.to_thread(_set)
    # 캐시 무효화
    gid = str(guild_id)
    _GUILD_CONFIG_CACHE.pop(gid, None)
    _GUILD_CONFIG_CACHE_TS.pop(gid, None)

def _cfg_get(cfg: dict, *keys, default=None):
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

async def get_channel_from_cfg(guild: discord.Guild, cfg: dict, key: str, fallback_id: int | None):
    # key: "log_channel_id" 같은 단일 키를 channels에서 찾음
    ch_id = _cfg_get(cfg, "channels", key, default=None)
    if isinstance(ch_id, int):
        return guild.get_channel(ch_id)
    if isinstance(ch_id, str) and ch_id.isdigit():
        return guild.get_channel(int(ch_id))
    if fallback_id:
        return guild.get_channel(fallback_id)
    return None

async def get_role_from_cfg(guild: discord.Guild, cfg: dict, key: str, fallback_id: int | None):
    role_id = _cfg_get(cfg, "roles", key, default=None)
    if isinstance(role_id, int):
        return guild.get_role(role_id)
    if isinstance(role_id, str) and role_id.isdigit():
        return guild.get_role(int(role_id))
    if fallback_id:
        return guild.get_role(fallback_id)
    return None


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

def get_attendance_user(user_id: str) -> dict:
    """특정 유저 출석 데이터만 불러옵니다."""
    raw = db.reference(ATTENDANCE_DB_KEY).child(user_id).get()
    return raw if isinstance(raw, dict) else {}

def set_attendance_user(user_id: str, data: dict):
    """특정 유저 출석 데이터 저장"""
    try:
        db.reference(ATTENDANCE_DB_KEY).child(user_id).set(data)
    except Exception as e:
        print(f"❌ set_attendance_user 실패: {e}")

def bulk_update_attendance(updates: dict):
    """attendance_data 루트에 대해 update(부분 갱신)"""
    try:
        db.reference(ATTENDANCE_DB_KEY).update(updates)
    except Exception as e:
        print(f"❌ bulk_update_attendance 실패: {e}")

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
    for task in (voice_xp_task, reset_daily_missions, repeat_vc_mission_task, monthly_attendance_shield_task, inactive_user_log_task, voice_count_channel_task):
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

        # ✅ 신입(또는 스키마 깨진 유저) 최초 1회 저장
        # - exp/level/voice_minutes가 비정상/누락이어도 aget_user_exp에서 보정됨
        # - 여기서 DB에 “정상 스키마”로 박아두면 이후 이벤트에서 재발 방지됨
        try:
            await asave_user_exp(uid, user_data)
        except Exception:
            pass

        new_level = calculate_level(user_data.get("exp", 0))


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
@tasks.loop(time=dtime(hour=15, minute=5))
async def monthly_attendance_shield_task():
    """
    매월 1일(KST) 00:05에 모든 유저에게 출석 보호권 1장 자동 지급.
    - 각 유저 레코드의 shield_grant_month로 중복 방지 (재시작/중복 실행에도 안전)
    """
    try:
        now_kst = datetime.now(KST)
        if now_kst.day != 1:
            return

        data = await aget_attendance_data()
        if not isinstance(data, dict) or not data:
            return

        month_key = get_month_key_kst(now_kst)
        updates = {}

        for uid, ud in data.items():
            ud = normalize_attendance_record(ud)
            if ud.get("shield_grant_month") != month_key:
                ud["shield_tokens"] = ud.get("shield_tokens", 0) + 1
                ud["shield_grant_month"] = month_key
                updates[str(uid)] = ud

        if updates:
            await abulk_update_attendance(updates)
            print(f"🛡️ 월간 출석 보호권 지급 완료: {len(updates)}명 ({month_key})")
    except Exception as e:
        print(f"❌ monthly_attendance_shield_task error: {e!r}")
        
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
        cfg = await aget_guild_config(guild.id)
        afk_ids = _cfg_get(cfg, "voice", "afk_channel_ids", default=AFK_CHANNEL_IDS) or []
        sp_cat_ids = _cfg_get(cfg, "voice", "special_vc_category_ids", default=SPECIAL_VC_CATEGORY_IDS) or []

        afk_ids = [int(x) for x in afk_ids if str(x).isdigit()]
        sp_cat_ids = [int(x) for x in sp_cat_ids if str(x).isdigit()]

        # 보이스 + 스테이지 채널 모두 포함
        try:
            voice_like_channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        except Exception:
            voice_like_channels = list(guild.voice_channels)

        for vc in voice_like_channels:
            if vc.id in afk_ids:
                continue

            is_special = vc.category and vc.category.id in sp_cat_ids


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
                        cfg = await aget_guild_config(guild.id)
                        announce = await get_channel_from_cfg(guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL)
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
        cfg = await aget_guild_config(guild.id)
        afk_ids = _cfg_get(cfg, "voice", "afk_channel_ids", default=AFK_CHANNEL_IDS) or []
        afk_ids = [int(x) for x in afk_ids if str(x).isdigit()]

         # 보이스 + 스테이지 채널 모두 포함
        voice_like_channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        for vc in voice_like_channels:
            humans = [m for m in vc.members if not m.bot]

            # 🅰 AFK 채널은 미션 지급 제외 (이유 로그)
            if vc.id in afk_ids:
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

@tasks.loop(seconds=60)
async def voice_count_channel_task():
    for guild in bot.guilds:
        cfg = await aget_guild_config(guild.id)
        items = cfg.get("voice_count_channels", [])
        if not items:
            continue

        for it in items:
            role = guild.get_role(int(it["role_id"]))
            ch = guild.get_channel(int(it["channel_id"]))
            if not role or not ch:
                continue

            count = sum(1 for m in role.members if not m.bot)
            new_name = _replace_count_suffix(ch.name, count)
            if new_name and new_name != ch.name:
                await ch.edit(name=new_name)


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
        cfg = await aget_guild_config(message.guild.id)
        thread_ch_id = _cfg_get(cfg, "channels", "thread_role_channel_id", default=THREAD_ROLE_CHANNEL_ID)
        thread_role_id = _cfg_get(cfg, "roles", "thread_role_id", default=THREAD_ROLE_ID)

        if getattr(message.channel, "id", None) == int(thread_ch_id) and message.guild:
            role = message.guild.get_role(int(thread_role_id)) if thread_role_id else None
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
                # 유저 EXP에 바로 반영                (메모리 상)
                # === 보상: 다음 레벨 필요 XP(현재 레벨 구간)의 1% ===
                total_before = int(user_data.get("exp", 0))
                lvl_before = calculate_level(total_before)
                
                prev_thr = THRESHOLDS[lvl_before - 1] if (lvl_before - 1) < len(THRESHOLDS) else THRESHOLDS[-1]
                next_thr = THRESHOLDS[lvl_before] if lvl_before < len(THRESHOLDS) else THRESHOLDS[-1]
                need_xp = max(1, int(next_thr - prev_thr))
                
                reward_xp = int(round(need_xp * 0.01))
                reward_xp = max(10, min(reward_xp, 5000))  # 안전 클램프
                
                # EXP 반영
                user_data["exp"] = total_before + reward_xp
                user_data["level"] = calculate_level(user_data["exp"])
                user_data["last_activity"] = time.time()
                
                # 현재 레벨 구간 진행도 %
                lvl_after = int(user_data["level"])
                prev_thr2 = THRESHOLDS[lvl_after - 1] if (lvl_after - 1) < len(THRESHOLDS) else THRESHOLDS[-1]
                next_thr2 = THRESHOLDS[lvl_after] if lvl_after < len(THRESHOLDS) else THRESHOLDS[-1]
                cur_xp = max(0, int(user_data["exp"]) - int(prev_thr2))
                need_xp2 = max(1, int(next_thr2 - prev_thr2))
                pct_int = int(round((cur_xp / need_xp2) * 100))
                
                # 로그                
                cfg = await aget_guild_config(message.guild.id)
                log_ch = await get_channel_from_cfg(message.guild, cfg, "log_channel_id", LOG_CHANNEL_ID)
                if log_ch:
                    await log_ch.send(
                        f"[🧾 로그] {message.author.display_name} 님 텍스트 일일 퀘스트 완료! +{reward_xp}XP (1%)"
                    )


                # 배너 이미지 전송
                try:
                    buf = await asyncio.wait_for(
                        asyncio.to_thread(
                            render_daily_quest_banner,
                            display_name=message.author.display_name,
                            pct_int=pct_int,
                            height=40,
                            reward_pct=1,
                        ),
                        timeout=6,
                    )
                    await message.channel.send(
                        file=discord.File(fp=buf, filename="daily_quest.png"),
                    )
                except Exception:
                    await message.channel.send(
                        f"🎯 {message.author.mention} 일일 퀘스트 완료! 경험치 1% 지급 (현재 {pct_int}%)",
                    )
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

# =========================
# /설정 commands (admin only)
# =========================

@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="설정", description="서버별 봇 설정을 변경/조회합니다.")
@app_commands.describe(
    작업="view/set_channel/set_role/add_afk/remove_afk/toggle_season/set_season_map",
    종류="설정 종류(예: log, levelup, inactive_log, suggest_anon, suggest_real, thread_role_channel, thread_role)",
    채널="지정할 채널(해당 시)",
    역할="지정할 역할(해당 시)",
    계절="봄/여름/가을/겨울",
    음성채널="시즌 음성 채널",
    onoff="true/false"
)
@app_commands.choices(
    작업=[
        app_commands.Choice(name="보기", value="view"),
        app_commands.Choice(name="채널지정", value="set_channel"),
        app_commands.Choice(name="역할지정", value="set_role"),
        app_commands.Choice(name="AFK채널추가", value="add_afk"),
        app_commands.Choice(name="AFK채널제거", value="remove_afk"),
        app_commands.Choice(name="시즌기능ONOFF", value="toggle_season"),
        app_commands.Choice(name="시즌매핑지정", value="set_season_map"),
    ]
)
async def config_cmd(
    interaction: discord.Interaction,
    작업: str,
    종류: str = None,
    채널: discord.TextChannel | discord.VoiceChannel | discord.StageChannel = None,
    역할: discord.Role = None,
    계절: str = None,
    음성채널: discord.VoiceChannel | discord.StageChannel = None,
    onoff: str = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("DM에서는 사용할 수 없습니다.", ephemeral=True)

    gid = interaction.guild.id

    # 1) 보기
    if 작업 == "view":
        cfg = await aget_guild_config(gid)

        def fmt_id(v):
            return str(v) if v else "미설정"

        channels = cfg.get("channels", {})
        roles = cfg.get("roles", {})
        voice = cfg.get("voice", {})
        features = cfg.get("features", {})
        season_map = cfg.get("season_map", {})

        embed = discord.Embed(title="⚙️ 서버 설정", color=discord.Color.blurple())
        embed.add_field(name="채널", value=(
            f"퀘스트 로그 채널 지정: {fmt_id(channels.get('log_channel_id'))}\n"
            f"레벨업 공지 채널 지정: {fmt_id(channels.get('levelup_channel_id'))}\n"
            f"미접속 로그 채널 지정: {fmt_id(channels.get('inactive_log_channel_id'))}\n"
            f"건의함(익명) 채널 지정: {fmt_id(channels.get('suggest_anon_channel_id'))}\n"
            f"건의함(실명) 채널 지정: {fmt_id(channels.get('suggest_real_channel_id'))}\n"
            f"입장 첫 역할 채널 지정: {fmt_id(channels.get('thread_role_channel_id'))}"
        ), inline=False)

        embed.add_field(name="역할", value=(
            f"첫 채팅 시 자동부여 역할: {fmt_id(roles.get('thread_role_id'))}"
        ), inline=False)

        embed.add_field(name="음성", value=(
            f"잠수 채널 지정: {voice.get('afk_channel_ids', [])}\n"
            f"특수 채널 지정: {voice.get('special_vc_category_ids', [])}"
        ), inline=False)

        embed.add_field(name="기능", value=(
            f"역할 별 인원 표시방 지정: {features.get('season_voice_enabled', True)}"
        ), inline=False)

        # 시즌 매핑은 길어질 수 있으니 간단히
        sm_lines = []
        for k in ["봄", "여름", "가을", "겨울"]:
            v = season_map.get(k) or {}
            sm_lines.append(f"{k}: role={v.get('role_id','미설정')}, channel={v.get('channel_id','미설정')}")
        embed.add_field(name="시즌 매핑", value="\n".join(sm_lines), inline=False)

        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # 2) 채널 지정
    if 작업 == "set_channel":
        if not 종류 or not 채널:
            return await interaction.response.send_message("종류와 채널을 지정하세요.", ephemeral=True)

        key_map = {
            "log": "log_channel_id",
            "levelup": "levelup_channel_id",
            "inactive_log": "inactive_log_channel_id",
            "suggest_anon": "suggest_anon_channel_id",
            "suggest_real": "suggest_real_channel_id",
            "thread_role_channel": "thread_role_channel_id",
        }
        if 종류 not in key_map:
            return await interaction.response.send_message(f"알 수 없는 종류: {종류}", ephemeral=True)

        await aset_guild_config_field(gid, f"channels/{key_map[종류]}", int(채널.id))
        return await interaction.response.send_message(f"✅ 채널 설정 완료: {종류} = {채널.mention}", ephemeral=True)

    # 3) 역할 지정
    if 작업 == "set_role":
        if not 종류 or not 역할:
            return await interaction.response.send_message("종류와 역할을 지정하세요.", ephemeral=True)

        key_map = {
            "thread_role": "thread_role_id",
        }
        if 종류 not in key_map:
            return await interaction.response.send_message(f"알 수 없는 종류: {종류}", ephemeral=True)

        await aset_guild_config_field(gid, f"roles/{key_map[종류]}", int(역할.id))
        return await interaction.response.send_message(f"✅ 역할 설정 완료: {종류} = {역할.name}", ephemeral=True)

    # 4) AFK 채널 추가/제거
    if 작업 in ("add_afk", "remove_afk"):
        if not 채널:
            return await interaction.response.send_message("AFK로 지정할 음성/스테이지 채널을 선택하세요.", ephemeral=True)

        cfg = await aget_guild_config(gid)
        lst = cfg.get("voice", {}).get("afk_channel_ids", []) or []
        lst = [int(x) for x in lst if str(x).isdigit()]

        cid = int(채널.id)
        if 작업 == "add_afk" and cid not in lst:
            lst.append(cid)
        if 작업 == "remove_afk" and cid in lst:
            lst.remove(cid)

        await aset_guild_config_field(gid, "voice/afk_channel_ids", lst)
        return await interaction.response.send_message(f"✅ AFK 목록 업데이트: {lst}", ephemeral=True)

    # 5) 시즌 기능 ON/OFF
    if 작업 == "toggle_season":
        if onoff not in ("true", "false"):
            return await interaction.response.send_message("onoff는 true/false 중 하나여야 합니다.", ephemeral=True)
        val = (onoff == "true")
        await aset_guild_config_field(gid, "features/season_voice_enabled", val)
        return await interaction.response.send_message(f"✅ 시즌 음성 기능: {val}", ephemeral=True)

    # 6) 시즌 매핑 지정
    if 작업 == "set_season_map":
        if 계절 not in ("봄", "여름", "가을", "겨울") or not 역할 or not 음성채널:
            return await interaction.response.send_message("계절(봄/여름/가을/겨울), 역할, 음성채널을 모두 지정하세요.", ephemeral=True)

        await aset_guild_config_field(gid, f"season_map/{계절}", {"role_id": int(역할.id), "channel_id": int(음성채널.id)})
        return await interaction.response.send_message(f"✅ 시즌 매핑 설정: {계절}", ephemeral=True)

    return await interaction.response.send_message("알 수 없는 작업입니다.", ephemeral=True)



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
        guild = interaction.guild
        if guild:
            cfg = await aget_guild_config(guild.id)
            announce = await get_channel_from_cfg(guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL)
        else:
            announce = None
            
        if announce:
            await announce.send(
                f"🎉 {member.display_name} 님이 Lv.{new_level} 에 도달했습니다! 🎊",
                allowed_mentions=ALLOW_NO_PING
            )

    await asave_user_exp(uid, user_data)
    await interaction.response.send_message(f"✅ {member.mention}에게 경험치 {amount}XP 지급 완료!", ephemeral=True)

@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="인원채널_생성", description="역할 인원수를 표시하는 음성채널 생성")
async def create_count_channel(
    interaction: discord.Interaction,
    역할: discord.Role,
    제목: str = "임시 제목"
):
    guild = interaction.guild
    if not guild:
        return

    ch = await guild.create_voice_channel(f"{제목} : 0명")

    cfg = await aget_guild_config(guild.id)
    items = cfg.get("voice_count_channels", [])
    items.append({"role_id": 역할.id, "channel_id": ch.id})

    await aset_guild_config_field(guild.id, "voice_count_channels", items)

    await interaction.response.send_message(
        f"완료: {ch.mention}\n채널명 끝의 `n명`만 자동 갱신됩니다.",
        ephemeral=True
    )


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

# =========================
# Attendance 보호권 버튼 View
# =========================
class AttendanceShieldView(discord.ui.View):
    def __init__(self, *, uid: str, owner_user_id: int):
        super().__init__(timeout=60)
        self.uid = uid
        self.owner_user_id = owner_user_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_user_id:
            try:
                await interaction.response.send_message("이 버튼은 출석한 본인만 사용할 수 있습니다.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    async def _apply(self, interaction: discord.Interaction, *, use_shield: bool):
        await interaction.response.defer()

        uid = self.uid
        now = datetime.now(KST)
        today_str = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        week = get_week_key_kst(now)
        month = get_month_key_kst(now)

        ud = normalize_attendance_record(await aget_attendance_user(uid))

        # 월간 보호권 자동 지급(지연 지급; 월간 태스크 미동작/재시작 대비)
        ensure_monthly_shield_grant(ud, now)

        prev_last = ud.get("last_date", "")
        prev_streak = _safe_int(ud.get("streak", 0), 0)

        # 이미 오늘 출석 처리된 경우(중복 클릭/동시 처리 방지)
        if prev_last == today_str:
            h, m = _until_next_attendance(now)
            headline = random.choice(ATTEND_MSG_ALREADY).format(mention=interaction.user.mention)
            content = "\n".join([
                headline,
                _build_attendance_stats_line(ud["total_days"], ud["streak"]),
                f"다음 출석까지 {h}시간 {m}분",
            ])
            try:
                await interaction.message.edit(content=content, view=None)
            except Exception:
                pass
            return

        natural_continue = (prev_last == yesterday)
        is_first = (prev_last == "")

        # 보호권 사용 조건: 자연 연속이 아니고, 첫 출석도 아니고, use_shield가 True
        shield_tokens = _safe_int(ud.get("shield_tokens", 0), 0)
        shield_used = False
        if use_shield and (not natural_continue) and (not is_first) and shield_tokens > 0:
            ud["shield_tokens"] = shield_tokens - 1
            shield_used = True

        # streak 계산
        if is_first:
            new_streak = 1
        elif natural_continue or shield_used:
            new_streak = prev_streak + 1
        else:
            new_streak = 1

        # 출석 반영
        ud["streak"] = new_streak
        ud["last_date"] = today_str
        ud["total_days"] = _safe_int(ud.get("total_days", 0), 0) + 1
        ud.setdefault("weekly", {})[week] = _safe_int(ud["weekly"].get(week, 0), 0) + 1
        ud.setdefault("monthly", {})[month] = _safe_int(ud["monthly"].get(month, 0), 0) + 1

        # 경험치 지급
        gain = 100 + min(new_streak - 1, 10) * 10
        ue = await aget_user_exp(uid)
        prev_level = ue["level"]
        ue["exp"] += gain
        ue["level"] = calculate_level(ue["exp"])
        ue["last_activity"] = time.time()

        # 레벨업 알림(안전)
        if ue["level"] > prev_level:
            guild = interaction.guild
            if guild:
                cfg = await aget_guild_config(guild.id)
                announce = await get_channel_from_cfg(guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL)
                if announce:
                    try:
                        await announce.send(
                            f"🎉 {interaction.user.display_name} 님이 Lv.{ue['level']} 에 도달했습니다! 🎊",
                            allowed_mentions=ALLOW_NO_PING
                        )
                    except Exception:
                        pass

        # 저장
        await asave_user_exp(uid, ue)
        await aset_attendance_user(uid, ud)
        await update_role_and_nick(interaction.user, ue["level"])

        # 출력 멘트 구성
        if shield_used:
            headline = random.choice(ATTEND_MSG_SHIELD_USED).format(mention=interaction.user.mention, streak=new_streak)
        else:
            headline = random.choice(ATTEND_MSG_SHIELD_SKIPPED).format(mention=interaction.user.mention)

        lines = [headline]
        if new_streak in ATTEND_MILESTONE_STREAKS:
            lines.append(random.choice(ATTEND_MSG_MILESTONE).format(mention=interaction.user.mention, streak=new_streak))
        lines.append(_build_attendance_stats_line(ud["total_days"], ud["streak"], gain))

        content = "\n".join(lines)

        # 버튼 비활성 + 메시지 갱신
        try:
            await interaction.message.edit(content=content, view=None)
        except Exception:
            pass

    @discord.ui.button(label="🛡️ 보호권 사용(1장)", style=discord.ButtonStyle.success)
    async def use_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, use_shield=True)

    @discord.ui.button(label="그냥 진행", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, use_shield=False)

    async def on_timeout(self):
        # 시간 초과: 버튼만 비활성화
        try:
            for item in self.children:
                if hasattr(item, "disabled"):
                    item.disabled = True
            if self.message:
                await self.message.edit(content=self.message.content + "\n(시간 초과: 다시 /출석을 실행해주세요)", view=self)
        except Exception:
            pass

@bot.tree.command(name="출석", description="오늘의 출석을 기록합니다.")
async def attend(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week = get_week_key_kst(now)
    month = get_month_key_kst(now)

    # 유저 단위로만 로드 (전체 로드 방지)
    ud = normalize_attendance_record(await aget_attendance_user(uid))

    # 월간 보호권 자동 지급(지연 지급; 월간 태스크 미동작/재시작 대비)
    ensure_monthly_shield_grant(ud, now)

    prev_last = ud.get("last_date", "")

    # 이미 출석
    if prev_last == today_str:
        h, m = _until_next_attendance(now)
        headline = random.choice(ATTEND_MSG_ALREADY).format(mention=interaction.user.mention)
        msg = "\n".join([
            headline,
            _build_attendance_stats_line(ud["total_days"], ud["streak"]),
            f"다음 출석까지 {h}시간 {m}분",
        ])
        return await interaction.response.send_message(msg)

    # 자연 연속 여부 / 첫 출석 여부
    natural_continue = (prev_last == yesterday)
    is_first = (prev_last == "")

    # 연속이 끊길 상황 + 보호권 보유 시: 버튼으로 결정
    if (not is_first) and (not natural_continue) and _safe_int(ud.get("shield_tokens", 0), 0) > 0:
        tokens = _safe_int(ud.get("shield_tokens", 0), 0)
        content = "\n".join([
            f"{interaction.user.mention} (경고) 연속 출석이 끊길 상황입니다",
            f"보유 보호권: {tokens}장 / 이번 출석 소모: 1장",
            "보호권을 사용해 연속 기록을 유지할까요?",
        ])
        view = AttendanceShieldView(uid=uid, owner_user_id=interaction.user.id)
        await interaction.response.send_message(content, view=view)
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass
        return

    # 여기부터는 즉시 출석 처리 (첫 출석 / 자연 연속 / 보호권 없음)
    prev_streak = _safe_int(ud.get("streak", 0), 0)
    if is_first:
        new_streak = 1
        headline = random.choice(ATTEND_MSG_FIRST).format(mention=interaction.user.mention)
    elif natural_continue:
        new_streak = prev_streak + 1
        headline = random.choice(ATTEND_MSG_SUCCESS).format(mention=interaction.user.mention)
    else:
        new_streak = 1
        headline = random.choice(ATTEND_MSG_RESET).format(mention=interaction.user.mention)

    ud["streak"] = new_streak
    ud["last_date"] = today_str
    ud["total_days"] = _safe_int(ud.get("total_days", 0), 0) + 1
    ud.setdefault("weekly", {})[week] = _safe_int(ud["weekly"].get(week, 0), 0) + 1
    ud.setdefault("monthly", {})[month] = _safe_int(ud["monthly"].get(month, 0), 0) + 1

    # 경험치 지급
    gain = 100 + min(new_streak - 1, 10) * 10
    ue = await aget_user_exp(uid)
    prev_level = ue["level"]
    ue["exp"] += gain
    ue["level"] = calculate_level(ue["exp"])
    ue["last_activity"] = time.time()

    # 레벨업 알림(안전)
    if ue["level"] > prev_level:
        guild = interaction.guild
        if guild:
            cfg = await aget_guild_config(guild.id)
            announce = await get_channel_from_cfg(guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL)
            if announce:
                try:
                    await announce.send(
                        f"🎉 {interaction.user.display_name} 님이 Lv.{ue['level']} 에 도달했습니다! 🎊",
                        allowed_mentions=ALLOW_NO_PING
                    )
                except Exception:
                    pass

    await asave_user_exp(uid, ue)
    await aset_attendance_user(uid, ud)
    await update_role_and_nick(interaction.user, ue["level"])

    lines = [headline]
    if new_streak in ATTEND_MILESTONE_STREAKS:
        lines.append(random.choice(ATTEND_MSG_MILESTONE).format(mention=interaction.user.mention, streak=new_streak))
    lines.append(_build_attendance_stats_line(ud["total_days"], ud["streak"], gain))

    await interaction.response.send_message("\n".join(lines))

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
    
# =========================
# Attendance Admin Commands
# =========================
@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="출석보호권지급", description="특정 유저에게 출석 보호권을 지급/회수합니다.")
@app_commands.describe(member="대상 유저", amount="지급할 수량(음수면 회수)")
async def attendance_shield_grant(interaction: discord.Interaction, member: discord.Member, amount: int):
    uid = str(member.id)
    ud = normalize_attendance_record(await aget_attendance_user(uid))
    ud["shield_tokens"] = max(0, _safe_int(ud.get("shield_tokens", 0), 0) + _safe_int(amount, 0))
    await aset_attendance_user(uid, ud)
    await interaction.response.send_message(
        f"🛡️ {member.mention} 보호권 수량 변경 완료: 현재 {ud['shield_tokens']}장",
        ephemeral=True
    )

@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="출석수정", description="유저의 누적/연속 출석을 수정합니다.")
@app_commands.describe(
    member="대상 유저",
    total_days="누적 출석일(0 이상)",
    streak="연속 출석일(0 이상)",
    last_date="마지막 출석일(선택, YYYY-MM-DD)"
)
async def attendance_edit(interaction: discord.Interaction, member: discord.Member, total_days: int, streak: int, last_date: str | None = None):
    uid = str(member.id)
    ud = normalize_attendance_record(await aget_attendance_user(uid))

    ud["total_days"] = max(0, _safe_int(total_days, 0))
    ud["streak"] = max(0, _safe_int(streak, 0))

    if last_date is not None:
        ld = last_date.strip()
        if ld == "":
            ud["last_date"] = ""
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ld):
                return await interaction.response.send_message("❌ last_date 형식이 올바르지 않습니다. 예: 2026-02-24", ephemeral=True)
            try:
                datetime.strptime(ld, "%Y-%m-%d")
            except Exception:
                return await interaction.response.send_message("❌ last_date 값이 유효한 날짜가 아닙니다.", ephemeral=True)
            ud["last_date"] = ld

    await aset_attendance_user(uid, ud)
    await interaction.response.send_message(
        f"✅ {member.mention} 출석 수정 완료\n"
        f"- 누적: {ud['total_days']}일\n"
        f"- 연속: {ud['streak']}일\n"
        f"- 마지막 출석일: {ud.get('last_date','') or '(없음)'}",
        ephemeral=True
    )

@bot.tree.command(name="연결끊기", description="현재 음성방에 있는 유저의 음성 연결을 끊습니다.")
@app_commands.describe(
    대상="연결을 끊을 대상",
    사유="연결을 끊는 사유"
)
async def disconnect_voice(
    interaction: discord.Interaction,
    대상: discord.Member,
    사유: str
):
    if not interaction.guild:
        return await interaction.response.send_message(
            "DM에서는 사용할 수 없습니다.",
            ephemeral=True
        )

    사유 = (사유 or "").strip()

    if not 사유:
        return await interaction.response.send_message(
            "❌ 연결 끊는 사유를 입력해주세요.",
            ephemeral=True
        )

    if len(사유) > 500:
        return await interaction.response.send_message(
            "❌ 사유는 500자 이내로 입력해주세요.",
            ephemeral=True
        )

    if not 대상.voice or not 대상.voice.channel:
        return await interaction.response.send_message(
            f"❌ {대상.display_name} 님은 현재 음성방에 참여 중이 아닙니다.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    executor = interaction.user
    voice_channel = 대상.voice.channel
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    dm_sent = True
    try:
        dm_embed = discord.Embed(
            title="🔌 음성 연결이 종료되었습니다",
            description=(
                f"서버: {interaction.guild.name}\n"
                f"처리자: {executor.display_name}\n"
                f"사유: {사유}"
            ),
            color=discord.Color.orange()
        )
        dm_embed.set_footer(text=f"처리 시각: {now_str}")
        await 대상.send(embed=dm_embed)
    except Exception:
        dm_sent = False

    try:
        await 대상.move_to(
            None,
            reason=f"/연결끊기 사용자={executor}({executor.id}) 사유={사유[:300]}"
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ 봇에게 대상자의 음성 연결을 끊을 권한이 없습니다. "
            "봇 역할에 `멤버 이동` 권한이 있는지 확인해주세요.",
            ephemeral=True
        )
    except discord.HTTPException as e:
        return await interaction.followup.send(
            f"❌ 연결 끊기 처리 중 Discord 오류가 발생했습니다: {e}",
            ephemeral=True
        )
    except Exception as e:
        return await interaction.followup.send(
            f"❌ 연결 끊기 처리 중 오류가 발생했습니다: {type(e).__name__}",
            ephemeral=True
        )

    log_channel = interaction.guild.get_channel(DISCONNECT_LOG_CHANNEL_ID)

    if log_channel:
        log_embed = discord.Embed(
            title="🔌 음성 연결 끊기 기록",
            color=discord.Color.red()
        )
        log_embed.add_field(
            name="처리자",
            value=f"{executor.display_name} (`{executor.id}`)",
            inline=False
        )
        log_embed.add_field(
            name="대상자",
            value=f"{대상.display_name} (`{대상.id}`)",
            inline=False
        )
        log_embed.add_field(
            name="대상 음성방",
            value=f"{voice_channel.name} (`{voice_channel.id}`)",
            inline=False
        )
        log_embed.add_field(
            name="사유",
            value=사유,
            inline=False
        )
        log_embed.add_field(
            name="DM 전송",
            value="성공" if dm_sent else "실패",
            inline=True
        )
        log_embed.set_footer(text=f"처리 시각: {now_str}")

        try:
            await log_channel.send(embed=log_embed, allowed_mentions=ALLOW_NO_PING)
        except Exception:
            pass

    await interaction.followup.send(
        f"✅ {대상.display_name} 님의 음성 연결을 끊었습니다.\n"
        f"사유: {사유}\n"
        f"DM 전송: {'성공' if dm_sent else '실패'}",
        ephemeral=True
    )

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
