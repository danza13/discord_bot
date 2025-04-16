#!/usr/bin/env python
"""
Discord‑бот‑програвач   (discord.py 2.5.2  +  yt‑dlp 2024‑04‑09)

• Працює у Native Runtime Render без Docker та без apt‑get.
• Підтримує Slash‑команди (play / skip / pause / stop / queue / remove).
• За потреби можна ввімкнути префікс‑команди, установивши ENABLE_MSG_CONTENT=true
  та активувавши Message Content Intent у Developer Portal.

⚙ requirements.txt
    discord.py[voice]==2.5.2
    yt-dlp>=2024.04.09
    PyNaCl==1.5.0

🔑 Render → Environment
    DISCORD_BOT_TOKEN   = <токен>
    ENABLE_MSG_CONTENT  = false
    YTDLP_COOKIES_FILE  = youtube_cookies.txt   (необов'язково)
    TEST_GUILD_ID       = 0                    (ID сервера для миттєвої sync або 0)

🚀 Build Command
    pip install --upgrade pip && pip install -r requirements.txt
"""

import asyncio
import os
import traceback
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ─────────────────────────── Конфіг ──────────────────────────
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))
COOKIES_PATH = os.getenv("YTDLP_COOKIES_FILE")
ENABLE_MSG_CONTENT = os.getenv("ENABLE_MSG_CONTENT", "false").lower() == "true"
EXTRACT_TIMEOUT = 20          # сек. на yt‑dlp

# ──────────────────────── yt‑dlp wrapper ─────────────────────
async def extract_info(query: str) -> Optional[dict]:
    """Витягає інформацію про трек або повертає None."""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",  # зменшує шанс на 429
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
        "ignoreerrors": True,
    }

    loop = asyncio.get_running_loop()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False)),
                timeout=EXTRACT_TIMEOUT,
            )
        except (asyncio.TimeoutError, yt_dlp.utils.DownloadError):
            return None

    if not info:
        return None
    if "entries" in info:
        info = next((e for e in info["entries"] if e), None)
    if not info or info.get("drm"):
        return None
    return info

# ──────────────────────── Моделі даних ───────────────────────
class Song:
    def __init__(self, info: dict):
        self.title = info.get("title", "Untitled")
        self.duration = int(info.get("duration", 0))
        self.url = info["url"]
        self.page = info.get("webpage_url")

    def __str__(self):
        m, s = divmod(self.duration, 60)
        return f"{self.title} ({m}:{s:02d})"

class MusicPlayer:
    def __init__(self):
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.paused = False

    # helpers
    def enqueue(self, song: Song) -> int:
        self.queue.append(song)
        return len(self.queue)

    def next_song(self):
        self.current = self.queue.pop(0) if self.queue else None

    async def disconnect(self):
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect()

players: Dict[int, MusicPlayer] = {}

def get_player(guild: discord.Guild) -> MusicPlayer:
    if guild.id not in players:
        players[guild.id] = MusicPlayer()
    return players[guild.id]

# ───────────────────── Ініціалізація бота ────────────────────
intents = discord.Intents.default()
intents.message_content = ENABLE_MSG_CONTENT
bot = commands.Bot(
    command_prefix="!" if ENABLE_MSG_CONTENT else commands.when_mentioned,
    intents=intents,
    help_command=None,  # вимикаємо текстову !help, щоб не плутала
)

# ─────── Подія ready + швидка синхронізація Slash‑команд ─────
@bot.event
async def on_ready():
    print(f"✅ Онлайн як {bot.user}; сервера: {len(bot.guilds)}")
    try:
        if TEST_GUILD_ID:
            test = discord.Object(TEST_GUILD_ID)
            bot.tree.copy_global_to(guild=test)
            await bot.tree.sync(guild=test)
        else:
            await bot.tree.sync()
        print("🔄 Slash‑команди синхронізовані")
    except Exception as e:
        print("❌ Sync error:", e)

# ────────────────────── Slash‑команди бота ───────────────────
@bot.tree.command(name="play", description="Відтворити або додати трек")
@app_commands.describe(url="YouTube / SoundCloud посилання або пошук")
async def play_cmd(inter: discord.Interaction, url: str):
    await inter.response.defer(ephemeral=False, thinking=True)

    if not (state := inter.user.voice) or not state.channel:
        return await inter.followup.send("❌ Спершу під'єднайтесь до voice‑каналу", ephemeral=True)

    player = get_player(inter.guild)
    if not player.voice or not player.voice.is_connected():
        player.voice = await state.channel.connect()
    elif player.voice.channel.id != state.channel.id:
        await player.voice.move_to(state.channel)

    info = await extract_info(url)
    if not info:
        return await inter.followup.send("❌ Не вдалося завантажити аудіо (429/DRM/timeout)", ephemeral=True)

    song = Song(info)
    if not player.voice.is_playing() and not player.paused and player.current is None:
        player.current = song
        await start_playback(player, inter)
        await inter.followup.send(f"▶️ Зараз грає: **{song}**")
    else:
        pos = player.enqueue(song)
        await inter.followup.send(f"➕ Додано до черги під № {pos}: **{song.title}**")

async def start_playback(player: MusicPlayer, inter: discord.Interaction):
    if not player.current:
        return

    src = discord.FFmpegPCMAudio(
        player.current.url,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        options="-vn",
    )

    def _after(err):
        if err:
            print("FFmpeg error:", err)
        asyncio.run_coroutine_threadsafe(after_song(player, inter), bot.loop)

    player.voice.play(src, after=_after)

async def after_song(player: MusicPlayer, inter: discord.Interaction):
    player.next_song()
    if player.current:
        await start_playback(player, inter)
    else:
        await asyncio.sleep(30)  # auto‑leave
        if player.voice and not player.voice.is_playing():
            await player.disconnect()

@bot.tree.command(description="Пропустити поточний трек")
async def skip(inter: discord.Interaction):
    player = get_player(inter.guild)
    if player.voice and player.voice.is_playing():
        player.voice.stop()
        await inter.response.send_message("⏭️ Пропущено", ephemeral=True)
    else:
        await inter.response.send_message("Немає що пропускати", ephemeral=True)

@bot.tree.command(description="Пауза / відновлення")
async def pause(inter: discord.Interaction):
    player = get_player(inter.guild)
    if not player.voice:
        return await inter.response.send_message("Бот не у voice", ephemeral=True)
    if player.voice.is_playing():
        player.voice.pause()
        player.paused = True
        await inter.response.send_message("⏸️ Пауза", ephemeral=True)
    elif player.paused:
        player.voice.resume()
        player.paused = False
        await inter.response.send_message("▶️ Відновлено", ephemeral=True)
    else:
        await inter.response.send_message("Нічого не грає", ephemeral=True)

@bot.tree.command(description="Зупинити та вийти")
async def stop(inter: discord.Interaction):
    player = get_player(inter.guild)
    player.queue.clear()
    player.current = None
    await player.disconnect()
    await inter.response.send_message("⏹️ Зупинено", ephemeral=True)

@bot.tree.command(description="Показати чергу")
async def queue(inter: discord.Interaction):
    player = get_player(inter.guild)
    lines = [f"**Зараз:** {player.current}" if player.current else "**Нічого не грає**"]
    if player.queue:
        lines.append("\n**Черга:**")
        lines += [f"{i+1}. {s}" for i, s in enumerate(player.queue)]
    else:
        lines.append("\nЧерга порожня.")
    await inter.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(description="Видалити трек з черги")
@app_commands.describe(index="Починаючи з 1")
async def remove(inter: discord.Interaction, index: int):
    player = get_player(inter.guild)
    if 1 <= index <= len(player.queue):
        song = player.queue.pop(index - 1)
        await inter.response.send_message(f"🗑️ Видалено **{song.title}**", ephemeral=True)
    else:
        await inter.response.send_message("❌ Невірний індекс", ephemeral=True)

# ───────────────────── Обробка помилок ───────────────────────
@bot.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    orig = getattr(error, "original", error)
    traceback.print_exception(type(orig), orig, orig.__traceback__)
    msg = f"⚠️ Помилка: {orig}"
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)

# ───────────────────────── Запуск ────────────────────────────
if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN не знайдено")
    print("DEBUG TEST_GUILD_ID =", TEST_GUILD_ID)
    bot.run(token)
