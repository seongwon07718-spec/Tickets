# -*- coding: utf-8 -*-
import os
import io
import re
import sqlite3
import traceback
import datetime as dt
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ========= 환경설정 =========
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 선택(빠른 동기화), 비우면 모든 길드 개별 동기화
DB_PATH = "ticketbot.db"

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

COLOR = discord.Color.from_rgb(0, 0, 0)

intents = discord.Intents.default()
intents.members = True  # 스태프 역할 판별/권한 반영
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= 공용 유틸 =========
def make_embed(title: str, desc: str = "", fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=COLOR)
    if fields:
        for n, v, i in fields:
            e.add_field(name=n, value=v, inline=i)
    e.timestamp = now_utc()
    return e

def slugify(label: str) -> str:
    s = label.lower().strip()
    s = s.replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "ticket"

def pemoji(s: str | None):
    if not s:
        return None
    try:
        return discord.PartialEmoji.from_str(s.strip())
    except Exception:
        return None

def db():
    return sqlite3.connect(DB_PATH)

# ========= DB 초기화/쿼리 =========
def init_db():
    conn = db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS guild_settings(
        guild_id INTEGER PRIMARY KEY,
        category_id INTEGER,
        support_role_id INTEGER,
        log_channel_id INTEGER,
        max_open_per_user INTEGER DEFAULT 1,
        cooldown_sec INTEGER DEFAULT 0,
        auto_close_min INTEGER DEFAULT 0,
        channel_name_fmt TEXT DEFAULT 'ticket-{type}-{user}',
        open_msg TEXT DEFAULT '티켓이 열렸습니다.',
        guide_msg TEXT DEFAULT '상담 내용을 구체적으로 남겨주세요. 스태프가 곧 도와드려요.',
        close_msg TEXT DEFAULT '티켓이 종료되었습니다.'
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ticket_types(
        guild_id INTEGER,
        value TEXT,
        label TEXT,
        description TEXT,
        emoji TEXT,
        ord INTEGER DEFAULT 0,
        PRIMARY KEY(guild_id, value)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tickets(
        ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        channel_id INTEGER,
        opener_id INTEGER,
        type_value TEXT,
        opened_at TEXT,
        last_activity_at TEXT,
        status TEXT, -- open/closed
        claimed_by INTEGER,
        reason TEXT
    )""")
    conn.commit(); conn.close()

# (이하 생략: DB 관련 함수들)

# ========= 자동 닫힘 루프 =========
@tasks.loop(minutes=2)
async def auto_close_loop():
    # 자동 닫힘 로직
    pass

@auto_close_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()

# ========= 슬래시 그룹 설정 =========
티켓설정 = app_commands.Group(name="티켓설정", description="티켓 설정(관리자)")
티켓메시지 = app_commands.Group(name="티켓메시지", description="티켓 문구 설정(관리자)")
티켓유형 = app_commands.Group(name="티켓유형", description="티켓 드롭다운 항목(관리자)")

# ---- 설정, 메시지, 유형 관련 함수들 ----

# (여기에 각 명령어를 정의하는 코드가 들어갑니다)

# ========= 봇 실행 전 초기화 =========
@bot.event
async def on_ready():
    init_db()

    # 슬래시 명령어 동기화
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        synced = await bot.tree.sync(guild=guild)
        print(f"Synchronized {len(synced)} commands to guild: {GUILD_ID}")
    else:
        synced = await bot.tree.sync()
        print(f"Synchronized {len(synced)} commands globally.")

    # 그룹 등록(중복 등록 예외 무시)
    for grp in (티켓설정, 티켓메시지, 티켓유형):
        try:
            bot.tree.add_command(grp)
        except Exception:
            pass

    # 현재 봇이 들어가 있는 길드 리스트
    try:
        if bot.guilds:
            print("[GUILDS]", ", ".join(f"{g.name}({g.id})" for g in bot.guilds))
        else:
            print("[GUILDS] 봇이 어떤 서버에도 초대되어 있지 않음")
    except Exception as e:
        print("guild list print error:", e)

    print(f"로그인 성공: {bot.user}")

# ========= 에러 핸들러(친절하게) =========
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    try:
        await ctx.reply("오류가 발생했어. 관리자에게 문의해줘.", mention_author=False)
    except:
        pass
    print("on_command_error:", error); print(traceback.format_exc())

# ========= 부트스트랩 =========
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 설정되지 않았습니다.")
    bot.run(TOKEN)
