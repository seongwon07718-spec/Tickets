# -*- coding: utf-8 -*-
import os
import discord
from discord.ext import commands, tasks
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 비우면 들어가 있는 모든 서버에 per-guild 동기화

# 인텐트: 텍스트 응급 명령(!강제동기화) 쓰려면 message_content 필요
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== 기본 슬래시 명령(작동 확인용) =====
@bot.tree.command(name="핑", description="봇 응답 확인")
async def 핑(inter: discord.Interaction):
    await inter.response.send_message("퐁!", ephemeral=True)

@bot.tree.command(name="강제동기화", description="(관리자) 이 서버에 슬래시 명령을 즉시 동기화합니다")
async def 강제동기화_slash(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        return await inter.response.send_message("서버 관리 권한이 필요해.", ephemeral=True)
    synced = await bot.tree.sync(guild=discord.Object(id=inter.guild_id))
    await inter.response.send_message(f"동기화 완료: {len(synced)}개", ephemeral=True)

@bot.tree.command(name="점검", description="현재 서버의 등록된 명령 상태를 보여줍니다")
async def 점검(inter: discord.Interaction):
    cmds = await bot.tree.fetch_commands(guild=discord.Object(id=inter.guild_id))
    names = ", ".join(sorted(c.name for c in cmds)) or "(없음)"
    await inter.response.send_message(f"등록 명령 수: {len(cmds)}개\n목록: {names}", ephemeral=True)

# ===== 텍스트 응급 동기화(슬래시가 아예 안 뜰 때) =====
@bot.command(name="강제동기화")
@commands.has_guild_permissions(manage_guild=True)
async def 강제동기화_text(ctx: commands.Context):
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=ctx.guild.id))
        await ctx.reply(f"동기화 완료: {len(synced)}개", mention_author=False)
    except Exception as e:
        await ctx.reply(f"동기화 실패: {e}", mention_author=False)

# ===== 새 서버 들어오면 즉시 설치 =====
@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        # 글로벌→길드 복사 후 길드 싱크
        bot.tree.copy_global_to(guild=discord.Object(id=guild.id))
        synced = await bot.tree.sync(guild=discord.Object(id=guild.id))
        print(f"[AUTO SYNC] {guild.name}({guild.id}): {len(synced)}개")
    except Exception as e:
        print("[AUTO SYNC] 실패:", e)

# ===== “명령 0개” 길드 재동기화 루프(안전장치) =====
@tasks.loop(seconds=30)
async def resync_until_ok():
    try:
        for g in bot.guilds:
            try:
                cmds = await bot.tree.fetch_commands(guild=discord.Object(id=g.id))
                if len(cmds) == 0:
                    # 글로벌을 길드로 복사한 다음 길드 싱크 재시도
                    bot.tree.copy_global_to(guild=discord.Object(id=g.id))
                    synced = await bot.tree.sync(guild=discord.Object(id=g.id))
                    print(f"[RESYNC] {g.name}({g.id}) 재동기화: {len(synced)}개")
            except Exception as e:
                print(f"[RESYNC] {g.id} 실패: {e}")
    except Exception as e:
        print("resync loop err:", e)

@resync_until_ok.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

# ===== 부팅 루틴: 글로벌→길드 복사 → 길드 싱크 =====
@bot.event
async def on_ready():
    # 로컬 트리 상태
    local = bot.tree.get_commands()
    print(f"[TREE] 로컬 명령 수: {len(local)} -> {[c.name for c in local]}")

    # 1) 글로벌 싱크(전부 글로벌에 먼저 올려둠)
    try:
        synced_global = await bot.tree.sync()
        print(f"[SYNC][GLOBAL] 글로벌 동기화: {len(synced_global)}개")
    except Exception as e:
        print("[SYNC][GLOBAL] 예외:", e)

    # 2) 길드 싱크(글로벌을 길드로 복사한 뒤 per-guild)
    try:
        if GUILD_ID:
            gid = int(GUILD_ID)
            bot.tree.copy_global_to(guild=discord.Object(id=gid))
            synced = await bot.tree.sync(guild=discord.Object(id=gid))
            print(f"[SYNC][GUILD] 지정 길드({gid}): {len(synced)}개")
        else:
            total = 0
            for g in bot.guilds:
                bot.tree.copy_global_to(guild=discord.Object(id=g.id))
                synced = await bot.tree.sync(guild=discord.Object(id=g.id))
                print(f"[SYNC][GUILD] {g.name}({g.id}): {len(synced)}개")
                total += len(synced)
            print(f"[SYNC] 전체 길드 합계: {total}개")
    except Exception as e:
        print("[SYNC][GUILD] 예외:", e)

    if not resync_until_ok.is_running():
        resync_until_ok.start()

    print(f"[READY] 로그인 성공: {bot.user}")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 설정되지 않았습니다.")
    bot.run(TOKEN)
