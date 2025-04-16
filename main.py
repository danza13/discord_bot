#!/usr/bin/env python
# ──────────────────────────────────────────────────────────────
# Discord‑бот‑програвач «під ключ» (discord.py 2.5.x + yt‑dlp)
# Працює у Native Run‑time Render без apt‑get та без Docker
# ──────────────────────────────────────────────────────────────
#  ⚙  Встановіть залежності (requirements.txt):
#      discord.py[voice]==2.5.2
#      yt-dlp>=2025.04  # свіжа версія з патчем проти HTTP 429
#      PyNaCl>=1.5
#
#  🔑  У Render → Environment:
#      DISCORD_BOT_TOKEN   = <токен>
#      YTDLP_COOKIES_FILE  = youtube_cookies.txt   (необов’язково)
#      ENABLE_MSG_CONTENT  = false                 (true, якщо потрібні !префікс‑команди)
#
#  🛑  У Discord Dev Portal на сторінці бота увімкніть:
#      • MESSAGE CONTENT INTENT (лише коли потрібно) та натисніть Save.
#
#  🚀  Build Command:  pip install -r requirements.txt
# ──────────────────────────────────────────────────────────────

import asyncio
import os
import traceback
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ╭─╴ Конфігурація ────────────────────────────────────────────╮
MAX_DEFER_SECONDS = 2.5          # скільки «думаємо» до першої відповіді
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))  # миттєва реєстрація Slash‑команд
COOKIES_PATH = os.getenv("YTDLP_COOKIES_FILE")        # файл із cookie — прибирає 429
ENABLE_MSG_CONTENT = os.getenv("ENABLE_MSG_CONTENT", "false").lower() == "true"
# ╰─────────────────────────────────────────────────────────────╯


# ╭─╴ yt‑dlp: єдина точка входу ───────────────────────────────╮
async def extract_info(url_or_query: str) -> Optional[dict]:
    """Повертає InfoDict або None. Обробляє HTTP 429 та DRM."""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",          # кеш обходу rate‑limit
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
        "cookiesfrombrowser": "chrome" if COOKIES_PATH is None else None,
        "cookiefile": COOKIES_PATH,
        "ignoreerrors": True,
    }

    loop = asyncio.get_running_loop()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url_or_query, download=False)
            )
        except yt_dlp.utils.DownloadError as e:
            print(f"[yt‑dlp] помилка: {e}")
            return None

    if not info:
        return None

    # Перший елемент плейлиста / пошуку
    if "entries" in info:
        info = next((e for e in info["entries"] if e), None)

    # DRM — відкидаємо
    if info and info.get("drm") is True:
        return None
    return info


# ╭─╴ Модель треку та плеєр для кожного Guild ────────────────╮
class Song:
    def __init__(self, info: dict):
        self.title = info.get("title", "Untitled")
        self.duration = int(info.get("duration", 0))
        self.source_url = info["url"]
        self.webpage_url = info.get("webpage_url")

    def __str__(self) -> str:
        m, s = divmod(self.duration, 60)
        return f"{self.title} ({m}:{s:02d})"


class MusicPlayer:
    def __init__(self):
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.is_paused = False

    # утиліти
    def enqueue(self, song: Song) -> int:
        self.queue.append(song)
        return len(self.queue)

    def next(self):
        self.current = self.queue.pop(0) if self.queue else None

    def disconnect(self):
        if self.voice and self.voice.is_connected():
            coro = self.voice.disconnect()
            asyncio.create_task(coro)


players: Dict[int, MusicPlayer] = {}


def get_player(guild: discord.Guild) -> MusicPlayer:
    if guild.id not in players:
        players[guild.id] = MusicPlayer()
    return players[guild.id]


# ╭─╴ Intents та ініціалізація бота ───────────────────────────╮
intents = discord.Intents.default()
intents.message_content = ENABLE_MSG_CONTENT
bot = commands.Bot(command_prefix="!" if ENABLE_MSG_CONTENT else commands.when_mentioned, intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Увійшла як {bot.user} | Гільдї: {len(bot.guilds)}")
    try:
        if TEST_GUILD_ID:
            test = discord.Object(id=TEST_GUILD_ID)
            bot.tree.copy_global_to(guild=test)
            await bot.tree.sync(guild=test)
            print("🔄 Slash‑команди синхронізовані для TEST_GUILD_ID")
        else:
            synced = await bot.tree.sync()
            print(f"🔄 Slash‑команди глобально синхронізовані: {len(synced)}")
    except Exception as e:
        print("❌ Sync error:", e)


# ╭─╴ Головні Slash‑команди ──────────────────────────────────╮
@app_commands.command(name="play", description="Відтворити або додати трек")
@app_commands.describe(url="URL або пошуковий запит")
async def play_cmd(inter: discord.Interaction, url: str):
    await inter.response.send_message("⏳ Завантажую трек…")
    msg = await inter.original_response()

    # Перевірка Voice‑стану
    if not (vs := inter.user.voice) or not vs.channel:
        return await msg.edit(content="❌ Спочатку зайдіть у голосовий канал.")

    player = get_player(inter.guild)
    if not player.voice or not player.voice.is_connected():
        player.voice = await vs.channel.connect()
    elif player.voice.channel.id != vs.channel.id:
        await player.voice.move_to(vs.channel)

    info = await extract_info(url)
    if not info:
        return await msg.edit(content="❌ Не вдалося отримати аудіо (DRM, 429 чи неправильне посилання).")

    song = Song(info)
    # Якщо нічого не грає
    if not player.voice.is_playing() and not player.is_paused and player.current is None:
        player.current = song
        await _start_playback(player, inter)
        await msg.edit(content=f"▶️ Зараз грає: **{song}**")
    else:
        pos = player.enqueue(song)
        await msg.edit(content=f"➕ Додано в чергу під № {pos}: **{song.title}**")


async def _start_playback(player: MusicPlayer, inter: discord.Interaction):
    if not player.current:
        return

    src = discord.FFmpegPCMAudio(
        player.current.source_url,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        options="-vn",
    )

    def after(err):
        if err:
            print("⚠️ FFmpeg error:", err)
        asyncio.run_coroutine_threadsafe(_after_song(player, inter), bot.loop)

    player.voice.play(src, after=after)


async def _after_song(player: MusicPlayer, inter: discord.Interaction):
    player.next()
    if player.current:
        await _start_playback(player, inter)
    else:
        await asyncio.sleep(30)  # таймер авто‑відключення
        if player.voice and not player.voice.is_playing():
            await player.voice.disconnect()


@app_commands.command(description="Пропустити поточний трек")
async def skip(inter: discord.Interaction):
    player = get_player(inter.guild)
    if player.voice and player.voice.is_playing():
        player.voice.stop()
        await inter.response.send_message("⏭️ Трек пропущено", ephemeral=True)
    else:
        await inter.response.send_message("Зараз нічого не грає", ephemeral=True)


@app_commands.command(description="Пауза / відновлення")
async def pause(inter: discord.Interaction):
    player = get_player(inter.guild)
    if not player.voice:
        return await inter.response.send_message("Бот не в голосовому каналі", ephemeral=True)

    if player.voice.is_playing():
        player.voice.pause()
        player.is_paused = True
        await inter.response.send_message("⏸️ Пауза", ephemeral=True)
    elif player.is_paused:
        player.voice.resume()
        player.is_paused = False
        await inter.response.send_message("▶️ Продовжуємо", ephemeral=True)
    else:
        await inter.response.send_message("Немає чого ставити на паузу", ephemeral=True)


@app_commands.command(description="Зупинити та вийти з каналу")
async def stop(inter: discord.Interaction):
    player = get_player(inter.guild)
    player.queue.clear()
    player.current = None
    player.disconnect()
    await inter.response.send_message("⏹️ Відтворення зупинено і бот покинув канал", ephemeral=True)


@app_commands.command(description="Показати чергу")
async def queue(inter: discord.Interaction):
    player = get_player(inter.guild)
    desc = [f"**Зараз грає:** {player.current}" if player.current else "**Нічого не грає**"]

    if player.queue:
        desc.append("\n**Черга:**")
        desc += [f"{i+1}. {s}" for i, s in enumerate(player.queue)]
    else:
        desc.append("\nЧерга порожня.")

    await inter.response.send_message("\n".join(desc), ephemeral=True)


@app_commands.command(description="Видалити трек із черги")
@app_commands.describe(index="Номер у черзі, починаючи з 1")
async def remove(inter: discord.Interaction, index: int):
    player = get_player(inter.guild)
    if 1 <= index <= len(player.queue):
        song = player.queue.pop(index - 1)
        await inter.response.send_message(f"🗑️ Видалено: **{song.title}**", ephemeral=True)
    else:
        await inter.response.send_message("❌ Неправильний індекс", ephemeral=True)


# ╭─╴ Обробка виключень ───────────────────────────────────────╮
@bot.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)
    if inter.response.is_done():
        await inter.followup.send(f"⚠️ Помилка: {original}", ephemeral=True)
    else:
        await inter.response.send_message(f"⚠️ Помилка: {original}", ephemeral=True)


# ╭─╴ Запуск ───────────────────────────────────────────────────╮
if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не знайдено DISCORD_BOT_TOKEN у змінних середовища!")
    bot.run(token)
