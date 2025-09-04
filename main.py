# -*- coding: utf-8 -*-
import os, io, re, sqlite3, traceback
import datetime as dt
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ========= 환경설정 =========
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 선택(빠른 동기화)
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
    # 단일 스레드 루프라 기본 연결로 충분
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

def get_settings(gid: int):
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT category_id, support_role_id, log_channel_id,
                          max_open_per_user, cooldown_sec, auto_close_min,
                          channel_name_fmt, open_msg, guide_msg, close_msg
                   FROM guild_settings WHERE guild_id=?""", (gid,))
    row = cur.fetchone(); conn.close()
    if not row:
        # 기본값
        return (None, None, None, 1, 0, 0,
                'ticket-{type}-{user}',
                '티켓이 열렸습니다.',
                '상담 내용을 구체적으로 남겨주세요. 스태프가 곧 도와드려요.',
                '티켓이 종료되었습니다.')
    return row

def upsert_settings(gid: int, **kwargs):
    current = list(get_settings(gid))
    keys = ["category_id","support_role_id","log_channel_id","max_open_per_user",
            "cooldown_sec","auto_close_min","channel_name_fmt","open_msg","guide_msg","close_msg"]
    # 없는 경우 삽입을 위해 None→기본값 세팅
    defaults = [None, None, None, 1, 0, 0, 'ticket-{type}-{user}',
                '티켓이 열렸습니다.',
                '상담 내용을 구체적으로 남겨주세요. 스태프가 곧 도와드려요.',
                '티켓이 종료되었습니다.']
    if not current or len(current) != len(keys):
        current = defaults[:]

    for i, k in enumerate(keys):
        if k in kwargs and kwargs[k] is not None:
            current[i] = kwargs[k]

    conn = db(); cur = conn.cursor()
    cur.execute("""INSERT INTO guild_settings(
        guild_id, category_id, support_role_id, log_channel_id,
        max_open_per_user, cooldown_sec, auto_close_min,
        channel_name_fmt, open_msg, guide_msg, close_msg
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(guild_id) DO UPDATE SET
        category_id=excluded.category_id,
        support_role_id=excluded.support_role_id,
        log_channel_id=excluded.log_channel_id,
        max_open_per_user=excluded.max_open_per_user,
        cooldown_sec=excluded.cooldown_sec,
        auto_close_min=excluded.auto_close_min,
        channel_name_fmt=excluded.channel_name_fmt,
        open_msg=excluded.open_msg,
        guide_msg=excluded.guide_msg,
        close_msg=excluded.close_msg
    """, (gid, *current))
    conn.commit(); conn.close()

def add_type(gid, value, label, desc, emoji, ord_):
    conn = db(); cur = conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO ticket_types(guild_id,value,label,description,emoji,ord)
                   VALUES(?,?,?,?,?,?)""", (gid, value, label, desc, emoji, ord_))
    conn.commit(); conn.close()

def del_type(gid, value) -> bool:
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM ticket_types WHERE guild_id=? AND value=?", (gid, value))
    ok = cur.rowcount > 0
    conn.commit(); conn.close()
    return ok

def list_types(gid):
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT value,label,description,emoji,ord
                   FROM ticket_types
                   WHERE guild_id=?
                   ORDER BY ord, label""", (gid,))
    rows = cur.fetchall(); conn.close(); return rows

def count_user_open(gid, uid) -> int:
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT COUNT(*)
                   FROM tickets
                   WHERE guild_id=? AND opener_id=? AND status='open'""", (gid, uid))
    n = cur.fetchone()[0]
    conn.close(); return n

def last_open_time(gid, uid):
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT opened_at
                   FROM tickets
                   WHERE guild_id=? AND opener_id=?
                   ORDER BY ticket_id DESC LIMIT 1""", (gid, uid))
    r = cur.fetchone(); conn.close()
    try:
        return dt.datetime.fromisoformat(r[0]) if r else None
    except Exception:
        return None

def ticket_from_channel(chid):
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT ticket_id,guild_id,channel_id,opener_id,type_value,
                          opened_at,last_activity_at,status,claimed_by,reason
                   FROM tickets WHERE channel_id=?""", (chid,))
    r = cur.fetchone(); conn.close(); return r

def update_ticket_activity(chid):
    conn = db(); cur = conn.cursor()
    cur.execute("""UPDATE tickets
                   SET last_activity_at=?
                   WHERE channel_id=?""", (now_utc().isoformat(), chid))
    conn.commit(); conn.close()

def close_ticket_record(chid):
    conn = db(); cur = conn.cursor()
    cur.execute("""UPDATE tickets
                   SET status='closed', last_activity_at=?
                   WHERE channel_id=?""", (now_utc().isoformat(), chid))
    conn.commit(); conn.close()

# ========= 모달(사유 입력) =========
class ReasonModal(discord.ui.Modal, title="티켓 사유 입력"):
    reason = discord.ui.TextInput(
        label="간단한 문의/구매 사유",
        placeholder="예) 로벅스 10,000 구매 문의",
        max_length=120, required=False
    )
    def __init__(self, parent_callback):
        super().__init__(timeout=120)
        self.parent_callback = parent_callback
    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.parent_callback(interaction, str(self.reason).strip())
        except Exception as e:
            print("ReasonModal error:", e)
            print(traceback.format_exc())
            if not interaction.response.is_done():
                await interaction.response.send_message("처리 중 오류가 발생했어. 잠시 후 다시 시도해줘.", ephemeral=True)

# ========= 버튼(닫기/담당) =========
class TicketOpsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="티켓 닫기", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_close(interaction)

    @discord.ui.button(label="담당하기", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_claim(interaction)

async def handle_claim(interaction: discord.Interaction):
    try:
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("텍스트 채널에서만 가능해.", ephemeral=True)

        row = ticket_from_channel(ch.id)
        if not row:
            return await interaction.response.send_message("티켓 정보가 없네. 관리자에게 문의!", ephemeral=True)
        _, gid, _, _, _, _, _, status, claimed_by, _ = row
        if status != "open":
            return await interaction.response.send_message("닫힌 티켓은 담당할 수 없어.", ephemeral=True)

        _, support_role_id, *_ = get_settings(gid)
        role_ok = False
        if support_role_id:
            role = interaction.guild.get_role(int(support_role_id))
            if role and role in getattr(interaction.user, "roles", []):
                role_ok = True
        if not role_ok:
            return await interaction.response.send_message("스태프만 담당할 수 있어.", ephemeral=True)

        if claimed_by and claimed_by != interaction.user.id:
            return await interaction.response.send_message("이미 다른 스태프가 담당 중이야.", ephemeral=True)

        conn = db(); cur = conn.cursor()
        cur.execute("""UPDATE tickets
                       SET claimed_by=?, last_activity_at=?
                       WHERE channel_id=?""",
                    (interaction.user.id, now_utc().isoformat(), ch.id))
        conn.commit(); conn.close()
        await interaction.response.send_message(embed=make_embed("담당자 지정", f"{interaction.user.mention} 님이 이 티켓을 담당합니다."), ephemeral=False)
    except Exception as e:
        print("handle_claim:", e); print(traceback.format_exc())
        if not interaction.response.is_done():
            await interaction.response.send_message("처리 중 오류가 발생했어.", ephemeral=True)

async def handle_close(interaction: discord.Interaction):
    try:
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("텍스트 채널에서만 가능해.", ephemeral=True)

        row = ticket_from_channel(ch.id)
        if not row:
            return await interaction.response.send_message("티켓 정보가 없네. 관리자에게 문의!", ephemeral=True)
        ticket_id, gid, _, opener_id, *_ = row
        category_id, support_role_id, log_channel_id, *_ , close_msg = get_settings(gid)

        # 권한: 개설자 또는 스태프
        is_staff = False
        if support_role_id:
            role = interaction.guild.get_role(int(support_role_id))
            if role and role in getattr(interaction.user, "roles", []):
                is_staff = True
        if interaction.user.id != opener_id and not is_staff:
            return await interaction.response.send_message("개설자 또는 스태프만 닫을 수 있어.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 트랜스크립트
        lines = []
        async for m in ch.history(limit=2000, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M")
            author = f"{m.author}({m.author.id})"
            content = m.content or ""
            if m.attachments:
                content += " " + " ".join(a.url for a in m.attachments)
            lines.append(f"[{ts}] {author}: {content}")
        transcript = "\n".join(lines) if lines else "내용 없음"
        file = discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"{ch.name}_transcript.txt")

        # 로그 채널 전송
        if log_channel_id:
            log_ch = interaction.guild.get_channel(int(log_channel_id))
            if isinstance(log_ch, discord.TextChannel):
                try:
                    await log_ch.send(embed=make_embed("티켓 종료", f"#{ch.name} (ID: {ticket_id})",
                                                       [("종료자", interaction.user.mention, True)]),
                                      file=file)
                except Exception as e:
                    print("log send:", e)

        try:
            await ch.send(embed=make_embed("티켓 종료", close_msg))
        except:
            pass

        close_ticket_record(ch.id)
        try:
            await ch.edit(name=f"closed-{ch.name}", reason="티켓 닫힘")
            await ch.set_permissions(interaction.guild.default_role, view_channel=False, send_messages=False)
        except:
            pass
        await ch.delete(reason="티켓 닫기")
    except Exception as e:
        print("handle_close:", e); print(traceback.format_exc())
        if not interaction.response.is_done():
            await interaction.response.send_message("종료 처리 중 오류가 발생했어.", ephemeral=True)

# ========= 드롭다운 → 모달 → 채널 생성 =========
class TicketTypeSelect(discord.ui.Select):
    def __init__(self, rows):
        options = []
        for v, label, desc, emoji, _ in rows:
            options.append(discord.SelectOption(
                label=label[:100],
                description=(desc or "")[:100],
                emoji=pemoji(emoji),
                value=v
            ))
        super().__init__(placeholder="선택하기", min_values=1, max_values=1,
                         options=options, custom_id="ticket_type_select")
        self.map = {v: (label, desc, emoji) for v, label, desc, emoji, _ in rows}

    async def callback(self, interaction: discord.Interaction):
        try:
            gid = interaction.guild_id
            (category_id, support_role_id, log_channel_id, max_open, cooldown_sec,
             auto_close_min, name_fmt, open_msg, guide_msg, close_msg) = get_settings(gid)

            guild = interaction.guild
            category = guild.get_channel(int(category_id)) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                return await interaction.response.send_message("카테고리가 설정되지 않았어. /티켓설정 카테고리 먼저!", ephemeral=True)

            # 동시 오픈 제한
            if max_open and count_user_open(gid, interaction.user.id) >= int(max_open):
                return await interaction.response.send_message(f"동시에 열 수 있는 티켓 수를 초과했어. (최대 {max_open})", ephemeral=True)

            # 쿨다운
            if cooldown_sec and (lt := last_open_time(gid, interaction.user.id)):
                remain = cooldown_sec - int((now_utc() - lt).total_seconds())
                if remain > 0:
                    return await interaction.response.send_message(f"{remain}초 뒤에 다시 시도해줘.", ephemeral=True)

            v = self.values[0]
            label, desc, _ = self.map.get(v, ("기타 문의", "", None))

            async def after_reason(inter: discord.Interaction, reason_text: str):
                # 채널명
                safe_user = re.sub(r'[^a-z0-9\-]', '', interaction.user.name.lower().replace(' ', '-')) or str(interaction.user.id)
                slug_type = slugify(label)
                temp_name = name_fmt.replace("{type}", slug_type).replace("{user}", safe_user).replace("{id}", "x")
                ch_name = slugify(temp_name)[:90]

                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                }
                if support_role_id:
                    role = guild.get_role(int(support_role_id))
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

                # 중복 티켓 방지
                for ch in category.text_channels:
                    if ch.topic and ch.topic.startswith(f"opener:{interaction.user.id}|"):
                        return await inter.response.send_message(f"이미 열린 티켓 있어: {ch.mention}", ephemeral=True)

                channel = await guild.create_text_channel(
                    ch_name, category=category, overwrites=overwrites,
                    topic=f"opener:{interaction.user.id}|type:{v}",
                    reason=f"티켓 생성: {label}"
                )

                # 기록
                conn = db(); cur = conn.cursor()
                now = now_utc().isoformat()
                cur.execute("""INSERT INTO tickets(guild_id,channel_id,opener_id,type_value,opened_at,last_activity_at,status,claimed_by,reason)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                            (gid, channel.id, interaction.user.id, v, now, now, "open", None, reason_text))
                conn.commit()
                ticket_id = cur.lastrowid
                conn.close()

                # {id} 지원
                if "{id}" in name_fmt:
                    new_name = name_fmt.replace("{type}", slug_type).replace("{user}", safe_user).replace("{id}", str(ticket_id))
                    new_name = slugify(new_name)[:90]
                    try:
                        await channel.edit(name=new_name)
                    except:
                        pass

                # 안내 메시지
                fields = [("유형", label, True), ("개설자", interaction.user.mention, True)]
                if reason_text:
                    fields.append(("사유", reason_text, False))
                fields.append(("안내", guide_msg, False))
                ping = guild.get_role(int(support_role_id)).mention if support_role_id and guild.get_role(int(support_role_id)) else None
                await channel.send(content=ping, embed=make_embed(open_msg, desc or "", fields), view=TicketOpsView())
                await inter.response.send_message(f"티켓 채널이 생성됐어: {channel.mention}", ephemeral=True)

            await interaction.response.send_modal(ReasonModal(after_reason))
        except Exception as e:
            print("TicketTypeSelect callback:", e); print(traceback.format_exc())
            if not interaction.response.is_done():
                await interaction.response.send_message("티켓 생성 중 오류가 발생했어.", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect(rows))

# ========= 자동 닫힘 루프 =========
@tasks.loop(minutes=2)
async def auto_close_loop():
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("""SELECT t.channel_id, t.guild_id, t.last_activity_at, g.auto_close_min, g.close_msg, g.log_channel_id
                       FROM tickets t JOIN guild_settings g ON t.guild_id=g.guild_id
                       WHERE t.status='open' AND g.auto_close_min>0""")
        rows = cur.fetchall(); conn.close()
        now = now_utc()
        for chid, gid, last_at, auto_min, close_msg, log_ch_id in rows:
            try:
                last = dt.datetime.fromisoformat(last_at)
            except Exception:
                continue
            if (now - last).total_seconds() >= auto_min * 60:
                guild = bot.get_guild(int(gid))
                ch = guild.get_channel(int(chid)) if guild else None
                if not isinstance(ch, discord.TextChannel):
                    continue
                # 로그
                if log_ch_id and guild:
                    logch = guild.get_channel(int(log_ch_id))
                    if isinstance(logch, discord.TextChannel):
                        try:
                            await logch.send(embed=make_embed("자동 종료", f"#{ch.name} (활동 {auto_min}분 없음)"))
                        except:
                            pass
                try:
                    await ch.send(embed=make_embed("자동 종료", close_msg))
                except:
                    pass
                close_ticket_record(ch.id)
                try:
                    await ch.edit(name=f"closed-{ch.name}", reason="자동 닫힘")
                    await ch.set_permissions(guild.default_role, view_channel=False, send_messages=False)
                except:
                    pass
                try:
                    await ch.delete(reason="자동 닫힘")
                except:
                    pass
    except Exception as e:
        print("auto_close_loop:", e); print(traceback.format_exc())

@auto_close_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()

# ========= 슬래시 그룹 =========
티켓설정 = app_commands.Group(name="티켓설정", description="티켓 설정(관리자)")
티켓메시지 = app_commands.Group(name="티켓메시지", description="티켓 문구 설정(관리자)")
티켓유형 = app_commands.Group(name="티켓유형", description="티켓 드롭다운 항목(관리자)")

# ---- 설정 ----
@티켓설정.command(name="카테고리", description="티켓 생성 카테고리 설정")
async def set_cat(interaction: discord.Interaction, 카테고리: discord.CategoryChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, category_id=카테고리.id)
    await interaction.response.send_message(embed=make_embed("카테고리 설정 완료", f"{카테고리.mention}"), ephemeral=True)

@티켓설정.command(name="역할", description="스태프 역할 설정")
async def set_role(interaction: discord.Interaction, 역할: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, support_role_id=역할.id)
    await interaction.response.send_message(embed=make_embed("스태프 역할 설정 완료", f"{역할.mention}"), ephemeral=True)

@티켓설정.command(name="로그채널", description="로그/트랜스크립트 채널 설정")
async def set_log(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, log_channel_id=채널.id)
    await interaction.response.send_message(embed=make_embed("로그 채널 설정 완료", f"{채널.mention}"), ephemeral=True)

@티켓설정.command(name="최대열림수", description="유저당 동시 오픈 제한(1~10)")
async def set_max_open(interaction: discord.Interaction, 개수: app_commands.Range[int, 1, 10]):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, max_open_per_user=int(개수))
    await interaction.response.send_message(embed=make_embed("최대 열림 수 설정", f"{개수}개"), ephemeral=True)

@티켓설정.command(name="쿨다운", description="티켓 재오픈 쿨다운(초)")
async def set_cooldown(interaction: discord.Interaction, 초: app_commands.Range[int, 0, 86400]):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, cooldown_sec=int(초))
    await interaction.response.send_message(embed=make_embed("쿨다운 설정", f"{초}초"), ephemeral=True)

@티켓설정.command(name="자동닫힘", description="활동 없을 때 자동 닫힘(분)")
async def set_autoclose(interaction: discord.Interaction, 분: app_commands.Range[int, 0, 1440]):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, auto_close_min=int(분))
    await interaction.response.send_message(embed=make_embed("자동 닫힘 설정", f"{분}분"), ephemeral=True)

@티켓설정.command(name="채널포맷", description="채널명 포맷: {type},{user},{id} 사용 가능")
async def set_namefmt(interaction: discord.Interaction, 포맷: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    # 길이/문자 제한은 생성 시 slugify로 보정됨
    upsert_settings(interaction.guild_id, channel_name_fmt=포맷[:64])
    await interaction.response.send_message(embed=make_embed("채널명 포맷 설정", f"`{포맷}`"), ephemeral=True)

# ---- 메시지 ----
@티켓메시지.command(name="열림문구", description="티켓 열릴 때 제목/상단 문구")
async def set_openmsg(interaction: discord.Interaction, 문구: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, open_msg=문구[:120])
    await interaction.response.send_message(embed=make_embed("열림 문구 설정", 문구), ephemeral=True)

@티켓메시지.command(name="안내문구", description="채널 첫 임베드의 안내 문구")
async def set_guidemsg(interaction: discord.Interaction, 문구: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, guide_msg=문구[:200])
    await interaction.response.send_message(embed=make_embed("안내 문구 설정", 문구), ephemeral=True)

@티켓메시지.command(name="닫힘문구", description="티켓 닫을 때 안내 문구")
async def set_closemsg(interaction: discord.Interaction, 문구: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(interaction.guild_id, close_msg=문구[:200])
    await interaction.response.send_message(embed=make_embed("닫힘 문구 설정", 문구), ephemeral=True)

# ---- 유형 ----
@티켓유형.command(name="추가", description="유형 추가/수정")
async def type_add(interaction: discord.Interaction, 라벨: str, 설명: str = "", 이모지: str = "", 값: str = "", 순서: int = 0):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    if not 값:
        값 = slugify(라벨)
    if not re.fullmatch(r"[a-z0-9-]{1,50}", 값):
        return await interaction.response.send_message("값은 영소문자/숫자/하이픈 1~50자로 해줘.", ephemeral=True)
    add_type(interaction.guild_id, 값, 라벨[:100], 설명[:100], (이모지 or None), int(순서))
    fields = [("값", 값, True), ("라벨", 라벨, True)]
    if 설명: fields.append(("설명", 설명, False))
    if 이모지: fields.append(("이모지", 이모지, True))
    fields.append(("정렬", str(순서), True))
    await interaction.response.send_message(embed=make_embed("유형 추가/수정", "", fields), ephemeral=True)

@티켓유형.command(name="삭제", description="유형 삭제")
async def type_del(interaction: discord.Interaction, 값: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    ok = del_type(interaction.guild_id, 값)
    if not ok:
        return await interaction.response.send_message("해당 값의 유형이 없어요.", ephemeral=True)
    await interaction.response.send_message(embed=make_embed("유형 삭제 완료", 값), ephemeral=True)

@티켓유형.command(name="목록", description="유형 목록 보기")
async def type_list(interaction: discord.Interaction):
    rows = list_types(interaction.guild_id)
    if not rows:
        return await interaction.response.send_message("등록된 유형이 없어요. /티켓유형 추가 먼저!", ephemeral=True)
    desc = ""
    for v, label, d, e, o in rows[:25]:
        desc += f"- [{o}] {e+' ' if e else ''}{label} (값: {v}) - {d or ''}\n"
    await interaction.response.send_message(embed=make_embed("유형 목록", desc or "(없음)"), ephemeral=True)

@티켓유형.command(name="프리셋", description="로블록스 5종 프리셋을 한 번에 등록합니다")
async def type_preset(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    presets = [
        ("item",   "로블록스 아이템 구매", "아이템 구매하고 싶어요.", "<:emoji_10:1411978370635399288>", 1),
        ("robux",  "로블록스 로벅스 구매", "로벅스 구매하고 싶어요.",   "<:emoji_11:1411978635480399963>", 2),
        ("partner","파트너 / 상단 문의",  "파트너 또는 상담하고 싶어요.", "<a:emoji_13:1411978711544238140>", 3),
        ("event",  "이벤트 관련",         "이벤트 관련 문의가 있어요.", "<a:emoji_12:1411978680653185055>", 4),
        ("other",  "기타 문의",           "기타 문의가 있어요.",       "<:emoji_14:1411978741504282685>", 5),
    ]
    for v, l, d, e, o in presets:
        add_type(interaction.guild_id, v, l, d, e, o)
    await interaction.response.send_message(embed=make_embed("프리셋 등록 완료", "로블록스 5종 항목이 등록됐어."), ephemeral=True)

# ---- 패널/운영 ----
@bot.tree.command(name="티켓패널", description="티켓 패널 게시(관리자)")
async def ticket_panel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    rows = list_types(interaction.guild_id)
    if not rows:
        return await interaction.response.send_message("유형이 없어요. /티켓유형 프리셋 또는 /티켓유형 추가 먼저!", ephemeral=True)
    cat, *_ = get_settings(interaction.guild_id)
    if not cat:
        return await interaction.response.send_message("카테고리 미설정. /티켓설정 카테고리 먼저!", ephemeral=True)
    await interaction.response.send_message(
        embed=make_embed("구매 & 문의", "아래 드롭다운에서 항목을 선택해줘."),
        view=TicketPanelView(rows)
    )

@bot.tree.command(name="티켓", description="티켓 채널 운영 명령")
@app_commands.describe(액션="참가자추가/참가자제거/이름변경/우선순위",
                       대상="유저(참가자 추가/제거일 때만)",
                       값="새이름 또는 우선순위(low|normal|high)")
async def ticket_ops(interaction: discord.Interaction, 액션: str, 대상: discord.Member = None, 값: str = ""):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("티켓 채널에서만 써줘.", ephemeral=True)
    row = ticket_from_channel(ch.id)
    if not row:
        return await interaction.response.send_message("티켓 채널이 아니야.", ephemeral=True)
    _, gid, _, opener_id, *_ = row
    _, support_role_id, *_ = get_settings(gid)
    is_staff = False
    if support_role_id:
        role = interaction.guild.get_role(int(support_role_id))
        if role and role in getattr(interaction.user, "roles", []):
            is_staff = True
    if not is_staff and interaction.user.id != opener_id:
        return await interaction.response.send_message("개설자 또는 스태프만 가능해.", ephemeral=True)

    if 액션 == "참가자추가":
        if not 대상:
            return await interaction.response.send_message("대상 유저를 지정해줘.", ephemeral=True)
        try:
            await ch.set_permissions(대상, view_channel=True, send_messages=True,
                                     read_message_history=True, attach_files=True)
            await interaction.response.send_message(embed=make_embed("참가자 추가", f"{대상.mention}"), ephemeral=False)
        except Exception:
            await interaction.response.send_message("권한 변경 실패.", ephemeral=True)

    elif 액션 == "참가자제거":
        if not 대상:
            return await interaction.response.send_message("대상 유저를 지정해줘.", ephemeral=True)
        try:
            await ch.set_permissions(대상, overwrite=None)
            await interaction.response.send_message(embed=make_embed("참가자 제거", f"{대상.mention}"), ephemeral=False)
        except Exception:
            await interaction.response.send_message("권한 변경 실패.", ephemeral=True)

    elif 액션 == "이름변경":
        if not 값:
            return await interaction.response.send_message("새 이름을 입력해줘.", ephemeral=True)
        new_name = slugify(값)[:90]
        try:
            await ch.edit(name=new_name, reason="티켓 이름변경")
            await interaction.response.send_message(embed=make_embed("이름 변경", f"#{new_name}"), ephemeral=False)
        except Exception:
            await interaction.response.send_message("이름 변경 실패.", ephemeral=True)

    elif 액션 == "우선순위":
        pr = (값 or "normal").lower()
        if pr not in ("low", "normal", "high"):
            return await interaction.response.send_message("우선순위는 low/normal/high 중 하나.", ephemeral=True)
        try:
            name = ch.name
            if name.startswith(("low-", "normal-", "high-")):
                name = pr + "-" + name.split("-", 1)[1]
            else:
                name = pr + "-" + name
            await ch.edit(name=name, reason="우선순위 변경")
            await interaction.response.send_message(embed=make_embed("우선순위", pr), ephemeral=False)
        except Exception:
            await interaction.response.send_message("우선순위 변경 실패.", ephemeral=True)
    else:
        await interaction.response.send_message("액션은 참가자추가/참가자제거/이름변경/우선순위 중 하나.", ephemeral=True)

# ---- 강제 동기화(관리자 전용) ----
@bot.tree.command(name="강제동기화", description="(관리자) 이 서버에 슬래시 명령을 즉시 동기화합니다")
async def 강제동기화(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=interaction.guild_id))
        await interaction.response.send_message(f"동기화 완료: {len(synced)}개", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"동기화 실패: {e}", ephemeral=True)

# ========= 활동 갱신(자동닫힘 근거) =========
@bot.event
async def on_message(message: discord.Message):
    try:
        await bot.process_commands(message)
        if message.guild and isinstance(message.channel, discord.TextChannel):
            row = ticket_from_channel(message.channel.id)
            if row and row[7] == "open":
                update_ticket_activity(message.channel.id)
    except Exception as e:
        print("on_message:", e)

# ========= 에러 핸들러(친절하게) =========
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    try:
        await ctx.reply("오류가 발생했어. 관리자에게 문의해줘.", mention_author=False)
    except:
        pass
    print("on_command_error:", error); print(traceback.format_exc())

# ========= 부트스트랩 =========
@bot.event
async def on_ready():
    init_db()
    # 그룹 등록(중복 등록 예외 무시)
    try:
        bot.tree.add_command(티켓설정)
    except Exception:
        pass
    try:
        bot.tree.add_command(티켓메시지)
    except Exception:
        pass
    try:
        bot.tree.add_command(티켓유형)
    except Exception:
        pass

    # 퍼시스턴트 뷰
    try:
        bot.add_view(TicketOpsView())
    except Exception:
        pass

    # 슬래시 동기화
    try:
        if GUILD_ID:
            await bot.tree.sync(guild=discord.Object(id=int(GUILD_ID)))
        else:
            await bot.tree.sync()
    except Exception as e:
        print("슬래시 동기화 오류:", e)

    if not auto_close_loop.is_running():
        auto_close_loop.start()

    print(f"로그인 성공: {bot.user}")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 설정되지 않았습니다.")
    bot.run(TOKEN)
