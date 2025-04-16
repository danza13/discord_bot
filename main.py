# Файл: main.py

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands

import yt_dlp

# Переконайтесь, що у вас встановлено:
# pip install py-cord
# pip install yt-dlp
# pip install PyNaCl

# Інструмент для отримання аудіо потоку з посилання за допомогою yt-dlp
# Повертає (title, duration, source_url, webpage_url)
async def extract_info(url: str):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'default_search': 'auto',  # якщо користувач введе не URL, а просто запит
        'ignoreerrors': True
    }

    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
    
    if info is None:
        return None

    # Якщо користувач дав плейлист, беремо перший трек
    if 'entries' in info:
        info = info['entries'][0]

    # Витягуємо дані
    title = info.get('title', 'Untitled')
    duration = info.get('duration', 0)
    webpage_url = info.get('webpage_url', url)
    # Аудіо-потік
    url_audio = info['url']
    
    return (title, duration, url_audio, webpage_url)

class Song:
    def __init__(self, title, duration, source_url, webpage_url):
        self.title = title
        self.duration = duration
        self.source_url = source_url
        self.webpage_url = webpage_url

    def __str__(self):
        return f"{self.title} ({self.duration_str()})"

    def duration_str(self):
        # Перетворення тривалості у формат хв:сек
        m, s = divmod(self.duration, 60)
        return f"{int(m)}:{s:02d}"

class MusicPlayer:
    def __init__(self):
        self.queue = []
        self.current_song = None
        self.voice_client = None
        self.is_paused = False
    
    def add_to_queue(self, song: Song):
        self.queue.append(song)
        return len(self.queue)

    def skip_song(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    def stop(self):
        self.queue.clear()
        self.current_song = None
        if self.voice_client and self.voice_client.is_connected():
            asyncio.create_task(self.voice_client.disconnect())

    def pause(self):
        if self.voice_client:
            if self.voice_client.is_playing() and not self.is_paused:
                self.voice_client.pause()
                self.is_paused = True
            elif self.is_paused:
                self.voice_client.resume()
                self.is_paused = False

    def remove_song(self, index: int):
        if 0 <= index < len(self.queue):
            self.queue.pop(index)
            return True
        return False


# Мапа для кожного сервера (guild), щоб зберігати власну чергу 
# (можливо, знадобиться створювати окремі MusicPlayer для кожного guild)
music_players = {}


intents = discord.Intents.default()
intents.message_content = True  # Щоб бот міг читати вміст повідомлень, якщо треба
bot = commands.Bot(command_prefix="!", intents=intents)

# Реєструємо "Guild Commands" або "Global Commands"
# Для зручності зробимо так, щоб Slash-команди були доступні глобально
# Якщо ж потрібно робити лише для певних серверів, використовуйте: @bot.tree.command(guild=discord.Object(id=ВАШ_GUILD_ID))
@bot.event
async def on_ready():
    print(f"Бот запустився як {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Slash команди синхронізовані! {len(synced)} команд.")
    except Exception as e:
        print(f"Помилка синхронізації: {e}")

def get_player(guild_id: int) -> MusicPlayer:
    if guild_id not in music_players:
        music_players[guild_id] = MusicPlayer()
    return music_players[guild_id]


@bot.tree.command(name="play", description="Відтворити/додати пісню в чергу за посиланням (YouTube, SoundCloud тощо).")
@app_commands.describe(url="Посилання або пошуковий запит (для YouTube тощо).")
async def play_command(interaction: discord.Interaction, url: str):
    await interaction.response.defer()  # повідомимо Discord, що бот "думає"

    guild_id = interaction.guild_id
    player = get_player(guild_id)

    # 1. Отримати голосовий канал користувача
    voice_state = interaction.user.voice
    if not voice_state or not voice_state.channel:
        await interaction.followup.send("Спочатку потрібно зайти в голосовий канал!", ephemeral=True)
        return

    # 2. Перевірити, чи бот уже в голосовому каналі
    voice_client = player.voice_client
    if not voice_client or not voice_client.is_connected():
        # Підключитись до голосового каналу користувача
        player.voice_client = await voice_state.channel.connect()
    else:
        # Якщо бот підключений до іншого каналу, треба перевірити, чи це той самий
        if voice_client.channel.id != voice_state.channel.id:
            await voice_client.move_to(voice_state.channel)

    # 3. Завантажити інформацію про трек
    info = await extract_info(url)
    if info is None:
        await interaction.followup.send("Не вдалося знайти або відтворити дане посилання.", ephemeral=True)
        return

    title, duration, source_url, webpage_url = info
    new_song = Song(title, duration, source_url, webpage_url)

    # 4. Якщо нічого зараз не грає, граємо одразу
    if not player.voice_client.is_playing() and not player.is_paused:
        player.current_song = new_song
        await start_playing(interaction, player)
    else:
        # Інакше додаємо у чергу
        position = player.add_to_queue(new_song)
        await interaction.followup.send(f"Додано в чергу: {new_song}\nПозиція у черзі: {position}")
    

async def start_playing(interaction: discord.Interaction, player: MusicPlayer):
    """Функція, яка розпочинає відтворення треку та обробляє перехід до наступних."""
    if not player.current_song:
        return

    source = discord.FFmpegPCMAudio(player.current_song.source_url, 
                                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", 
                                    options="-vn")
    
    def after_playing(err):
        if err:
            print(f"Помилка відтворення: {err}")
        # Після закінчення поточного треку переходимо до наступного
        coro = play_next_song(interaction, player)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    player.voice_client.play(source, after=after_playing)

    # Надсилаємо повідомлення про поточну пісню
    await interaction.followup.send(
        f"Зараз грає: **{player.current_song.title}** "
        f"(тривалість: {player.current_song.duration_str()})\n"
        f"Джерело: {player.current_song.webpage_url}"
    )


async def play_next_song(interaction: discord.Interaction, player: MusicPlayer):
    if len(player.queue) == 0:
        # Пісень більше немає
        player.current_song = None
        return
    else:
        # Беремо наступну з черги
        player.current_song = player.queue.pop(0)
        await start_playing(interaction, player)


@bot.tree.command(name="skip", description="Пропустити поточний трек і відтворити наступний.")
async def skip_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    player = get_player(guild_id)

    if not player.voice_client or not player.voice_client.is_playing():
        await interaction.response.send_message("Наразі нічого не грає.", ephemeral=True)
        return

    player.skip_song()
    await interaction.response.send_message("Поточний трек пропущено!")


@bot.tree.command(name="pause", description="Поставити на паузу або відновити відтворення.")
async def pause_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    player = get_player(guild_id)

    if not player.voice_client or (not player.voice_client.is_playing() and not player.is_paused):
        await interaction.response.send_message("Зараз нічого не грає.", ephemeral=True)
        return

    player.pause()
    if player.is_paused:
        await interaction.response.send_message("Відтворення поставлено на паузу.")
    else:
        await interaction.response.send_message("Відтворення продовжено.")


@bot.tree.command(name="stop", description="Зупинити відтворення та вийти з голосового каналу.")
async def stop_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    player = get_player(guild_id)

    if not player.voice_client or not player.voice_client.is_connected():
        await interaction.response.send_message("Бот не в голосовому каналі.", ephemeral=True)
        return

    player.stop()
    await interaction.response.send_message("Бот вийшов з голосового каналу та зупинив відтворення.")


@bot.tree.command(name="queue", description="Показати поточну чергу відтворення.")
async def queue_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    player = get_player(guild_id)

    if player.current_song:
        now_playing = f"**Зараз грає:** {player.current_song.title}\n\n"
    else:
        now_playing = "**Зараз не грає жодна пісня.**\n\n"

    if len(player.queue) == 0:
        queue_str = "Черга порожня."
    else:
        queue_str = "Черга:\n"
        for i, song in enumerate(player.queue):
            queue_str += f"{i} — {song.title} ({song.duration_str()})\n"

    await interaction.response.send_message(now_playing + queue_str)


@bot.tree.command(name="remove", description="Видалити пісню з черги за номером.")
@app_commands.describe(index="Номер пісні в черзі (0, 1, 2...).")
async def remove_command(interaction: discord.Interaction, index: int):
    guild_id = interaction.guild_id
    player = get_player(guild_id)

    if len(player.queue) == 0:
        await interaction.response.send_message("Черга порожня, нема що видаляти.", ephemeral=True)
        return
    
    removed = player.remove_song(index)
    if removed:
        await interaction.response.send_message(f"Пісню під індексом {index} видалено з черги.")
    else:
        await interaction.response.send_message(f"Пісні з індексом {index} в черзі не існує.", ephemeral=True)


# Запуск бота
# Токен можна зберігати у змінній середовища DISCORD_BOT_TOKEN (в налаштуваннях Render, наприклад)
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("Не вказано токен бота. Задайте DISCORD_BOT_TOKEN у змінних середовища.")
    else:
        bot.run(TOKEN)
