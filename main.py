# ------------ main.py ------------
import os
import discord

# 1. Зчитуємо токен зі змінної середовища
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не встановлений у середовищі!")

# 2. Налаштовуємо Intents
intents = discord.Intents.default()
intents.message_content = True      # щоб працювало on_message
intents.guilds = True                # щоб бот бачив сервери
intents.members = False              # якщо не потрібні події для юзерів

class MyClient(discord.Client):
    async def on_ready(self):
        print(f'Logged on as {self.user}!')

    async def on_message(self, message):
        # щоб бот не реагував на власні повідомлення
        if message.author == self.user:
            return
        print(f'Message from {message.author}: {message.content}')

# 3. Створюємо клієнта з передачею intents
client = MyClient(intents=intents)

# 4. Запускаємо
client.run(TOKEN)
