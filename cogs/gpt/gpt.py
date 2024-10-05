import io
import json
import logging
import random
import re
from collections import namedtuple
from datetime import datetime, timedelta
from typing import Any, Literal

import discord
import tiktoken
import unidecode
from discord import Interaction, app_commands
from discord.ext import commands
from openai import AsyncOpenAI

from common import dataio
from common.utils import fuzzy, pretty

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

AUTO_CACHE_SAVE_DELAY = 300 # secondes
HISTORY_COMPLETION_EXPIRATION = 60 * 60 * 24 # 24 heures
MAX_CHATBOT_PER_GUILD = 5
MAX_CHATBOT_IMMUNE_GUILDS = [328632789836496897] # Serveurs où les chatbots peuvent être créés sans limite
GPT_COMPLETION = namedtuple('GPTCompletion', ['text', 'finish_reason'])
GPT_USAGE = namedtuple('GPTUsage', ['prompt_tokens', 'completion_tokens'])

def check_botreset(interaction: Interaction):
    return interaction.user.id == 172376505354158080 or interaction.permissions.manage_messages
        
class ChatbotSelectionMenu(discord.ui.View):
    def __init__(self, chatbots: list['Chatbot'], view_author: discord.User | discord.Member, start_at: int = 0):
        super().__init__(timeout=60)
        self.chatbots = chatbots
        self.__chatbot_embeds = [chatbot.embed for chatbot in chatbots]
        self.view_author = view_author
        self.selected_chatbot = None
        
        self.index = start_at
        self.initial_interaction: Interaction
        
    async def interaction_check(self, interaction: Interaction):
        if interaction.user == self.view_author:
            return True
        await interaction.response.send_message("Vous n'avez pas la permission d'utiliser ce menu", ephemeral=True)
        
    async def on_timeout(self) -> None:
        if self.initial_interaction is not None:
            await self.initial_interaction.edit_original_response(view=None)
        self.stop()
        
    async def start(self, interaction: Interaction):
        self.initial_interaction = interaction
        await interaction.followup.send(embed=self.__chatbot_embeds[self.index], view=self)
        
    @discord.ui.button(label='←', style=discord.ButtonStyle.blurple)
    async def previous_button(self, interaction: Interaction, button: discord.ui.Button):
        self.index -= 1
        if self.index < 0:
            self.index = len(self.__chatbot_embeds) - 1
        await interaction.response.edit_message(embed=self.__chatbot_embeds[self.index], view=self)
        
    @discord.ui.button(label='×', style=discord.ButtonStyle.red)
    async def stop_button(self, interaction: Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=None)
        
    @discord.ui.button(label='Utiliser', style=discord.ButtonStyle.green)
    async def use_button(self, interaction: Interaction, button: discord.ui.Button):
        self.selected_chatbot = self.chatbots[self.index]
        self.stop()
        await interaction.response.edit_message(view=None)
            
    @discord.ui.button(label='→', style=discord.ButtonStyle.blurple)
    async def next_button(self, interaction: Interaction, button: discord.ui.Button):
        self.index += 1
        if self.index >= len(self.__chatbot_embeds):
            self.index = 0
        await interaction.response.edit_message(embed=self.__chatbot_embeds[self.index], view=self)

class Chatbot:
    def __init__(self,
                 id: int,
                 name: str,
                 system_prompt: str,
                 temperature: float = 0.9,
                 max_completion: int = 500,
                 context_size: int = 8000,
                 vision_detail: str = 'auto',
                 author_id: int = 0,
                 guild_id: int = 0):
        self.id = id
        self.name = name
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_completion = max_completion
        self.context_size = context_size    
        self.vision_detail = vision_detail
        self.author_id = author_id
        self.guild_id = guild_id
        
    def __str__(self):  
        return self.name
    
    def __repr__(self):
        return f"<Chatbot {self.name} ({self.id})>"
    
    @property
    def embed(self):
        color = random.Random(self.id).randint(0, 0xFFFFFF)
        embed = discord.Embed(title=f"**CHATBOT** · *{self.name}*", color=color)
        embed.add_field(name="Instructions", value=pretty.codeblock(self.system_prompt, 'yaml'), inline=False)
        temp_text = f"{self.temperature} (!)" if self.temperature > 1.4 else f"{self.temperature:2f}"
        embed.add_field(name="Température", value=pretty.codeblock(temp_text), inline=True)
        embed.add_field(name="Long. réponses", value=pretty.codeblock(f'{self.max_completion} tokens'), inline=True)
        embed.add_field(name="Taille contexte", value=pretty.codeblock(f'{self.context_size} tokens'), inline=True)
        details_trad = {'auto': 'Automatique', 'low': 'Basse qualité', 'high': 'Haute qualité'}
        embed.add_field(name="Détail analyse des images", value=pretty.codeblock(details_trad.get(self.vision_detail, '?')), inline=True)
        embed.set_footer(text=f"ID : {self.id}")
        return embed

class ChatSession:
    def __init__(self,
                 cog: 'GPT',
                 guild: discord.Guild,
                 chatbot: Chatbot):
        self.__cog = cog
        self.guild = guild
        self.chatbot = chatbot
        
        self._history = self._load_history()
        self.__last_save = datetime.now()
        
    # Chargement et sauvegarde de l'historique
        
    def _load_history(self):
        r = self.__cog.data.get(self.guild).fetchall("SELECT * FROM sessions WHERE chatbot_id = ?", self.chatbot.id)
        if not r:
            return []
        return [{'timestamp': r['timestamp'], 'payload': json.loads(r['payload'])} for r in r]
    
    def _save_history(self):
        self.__cog.data.get(self.guild).execute("DELETE FROM sessions WHERE chatbot_id = ?", self.chatbot.id)
        now = datetime.now().timestamp()
        self._history = [h for h in self._history if now - h['timestamp'] < HISTORY_COMPLETION_EXPIRATION]
        if not self._history:
            return
        self.__cog.data.get(self.guild).executemany("INSERT INTO sessions VALUES (?, ?, ?)", [(self.chatbot.id, h['timestamp'], json.dumps(h['payload'])) for h in self._history])
        
    def _maybe_save(self):
        if (datetime.now() - self.__last_save).total_seconds() > AUTO_CACHE_SAVE_DELAY:
            self._save_history()
            self.__last_save = datetime.now()
        
    def __del__(self):
        self._save_history()
        
    # Gestion de l'historique
    
    def add_completion_message(self, role: Literal['system', 'assistant', 'user'], text: str, name: str | None = None) -> dict[str, Any]:
        """Ajoute un message de complétion à l'historique."""
        payload = {
            'role': role,
            'content': [
                {'type': 'text', 'text': text}
            ]
        }
        if name:
            payload['name'] = name
        self._history.append({'timestamp': datetime.now().timestamp(), 'payload': payload})
        self._maybe_save()
        return payload
        
    def add_vision_message(self, role: Literal['system', 'assistant', 'user'], text: str, image_url: str, name: str | None = None) -> dict[str, Any]:
        """Ajoute un message de vision à l'historique."""
        payload = {
            'role': role,
            'content': [
                {'type': 'text', 'text': text},
                {'type': 'image_url', 'image_url': {'url': image_url, 'detail': self.chatbot.vision_detail}}
            ]
        }
        if name:
            payload['name'] = name
        self._history.append({'timestamp': datetime.now().timestamp(), 'payload': payload})
        self._maybe_save()
        return payload
        
    def remove_message(self, timestamp: float | datetime):
        """Supprime un message de l'historique."""
        if isinstance(timestamp, datetime):
            timestamp = timestamp.timestamp()
        self._history = [h for h in self._history if h['timestamp'] != timestamp]
        self._maybe_save()
        
    def clear_history(self, since: datetime | None = None):
        """Supprime tout l'historique."""
        if since:
            self._history = [h for h in self._history if h['timestamp'] < since.timestamp()]
        else:
            self._history = []
        self._maybe_save()
        
    # Contexte de conversation
    
    def get_context(self):
        tokenizer = tiktoken.get_encoding('cl100k_base')
        system_payload = {'role': 'system', 'content': [{'type': 'text', 'text': self.chatbot.system_prompt}]}
        if not self._history:
            return [system_payload]
        full_ctx = []
        ctx_size = len(tokenizer.encode(str(self.chatbot.system_prompt)))
        for m in self._history[::-1]: # On parcourt l'historique à l'envers (du plus récent au plus ancien)
            content_size = 0
            for content in m['payload']['content']:
                if content['type'] == 'text':
                    content_size += len(tokenizer.encode(content['text']))
                elif content['type'] == 'image_url':
                    content_size += 170 # Max de tokens pour une image (vu qu'en mode auto on ne peut pas prédire en avance le nb exact de tokens utilisés)
            if m['payload']['role'] == 'system':
                continue
            if ctx_size + content_size > self.chatbot.context_size:
                break
            ctx_size += content_size
            full_ctx.append(m['payload'])
        if full_ctx:
            full_ctx = [system_payload] + full_ctx[::-1]
        else:
            full_ctx = [system_payload] + [self._history[-1]['payload']]
        return full_ctx
    
    # Completion
    
    async def get_completion(self, text: str, image_url: str | None = None, name: str | None = None) -> tuple[GPT_COMPLETION, GPT_USAGE] | None:
        if name:
            name = ''.join([c for c in unidecode.unidecode(name) if c.isalnum() or c.isspace()]).rstrip()
            name = re.sub(r"[^a-zA-Z0-9_-]", "", name[:32])
        
        if image_url:
            self.add_vision_message('user', text, image_url, name)
        else:
            self.add_completion_message('user', text, name)
        context = self.get_context()
        gptclient = self.__cog.client
        try:
            completion = await gptclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=context, # type: ignore
                max_tokens=self.chatbot.max_completion,
                temperature=self.chatbot.temperature
            )
        except Exception as e:
            logger.error(f"ERREUR OPENAI : {e}", exc_info=True)
            return None 
        
        answer = completion.choices[0].message.content if completion.choices else None
        if not answer:
            return None
        self.add_completion_message('assistant', answer, name)
        comp = GPT_COMPLETION(answer, completion.choices[0].finish_reason)
        if completion.usage:
            usage = GPT_USAGE(completion.usage.prompt_tokens, completion.usage.completion_tokens)   
        else:
            usage = GPT_USAGE(0, 0)
        return comp, usage
    

class GPT(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        
        # GLOBAL
        chatbots = dataio.TableBuilder(
            '''CREATE TABLE IF NOT EXISTS chatbots (
                id INTEGER PRIMARY KEY,
                name TEXT,
                system_prompt TEXT,
                temperature REAL DEFAULT 0.9,
                max_completion INTEGER DEFAULT 512,
                context_size INTEGER DEFAULT 4096,
                vision_detail TEXT DEFAULT "auto",
                author_id INTEGER,
                guild_id INTEGER
                )'''
        )
        users = dataio.TableBuilder(
            '''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tracking_month INTEGER,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                banned BOOLEAN DEFAULT FALSE
                )'''
        )
        self.data.link('global', chatbots, users)
        
        # GUILDS
        sessions = dataio.TableBuilder(
            '''CREATE TABLE IF NOT EXISTS sessions (
                chatbot_id INTEGER,
                timestamp REAL,
                payload TEXT,
                PRIMARY KEY (chatbot_id, timestamp)
                )'''
        )
        self.data.link(discord.Guild, sessions)
        
        self.client = AsyncOpenAI(
            api_key=self.bot.config['OPENAI_API_KEY'], # type: ignore
        )
        self.__sessions_cache : dict[int, ChatSession] = {}
        
        self.__last_nochatbot_alert : dict[int, datetime] = {}
        
    async def cog_unload(self):
        await self.client.close()
        self.save_all_sessions()
        self.data.close_all()
        
    # Gestion des sessions
    
    def create_chat_session(self, guild: discord.Guild, chatbot: Chatbot) -> ChatSession:
        """Crée une nouvelle session de chat pour une guilde."""
        if guild.id not in self.__sessions_cache:
            self.__sessions_cache[guild.id] = ChatSession(self, guild, chatbot)
        return self.__sessions_cache[guild.id]
    
    def get_chat_session(self, guild: discord.Guild) -> ChatSession | None:
        """Récupère la session de chat actuelle pour une guilde."""
        if guild.id in self.__sessions_cache:
            return self.__sessions_cache[guild.id]
        elif chatbot := self.get_last_chatbot_used(guild):
            return self.create_chat_session(guild, chatbot)
        return None
    
    def get_last_chatbot_used(self, guild: discord.Guild) -> Chatbot | None:
        """Récupère le dernier chatbot utilisé dans une guilde."""
        r = self.data.get(guild).fetch("SELECT chatbot_id FROM sessions ORDER BY timestamp DESC LIMIT 1")
        if not r:
            return None
        return self.get_chatbot(r['chatbot_id'])
    
    def remove_chat_session(self, guild: discord.Guild, chatbot: Chatbot):
        """Retire une session de chat de la cache."""
        if guild.id in self.__sessions_cache:
            self.__sessions_cache[guild.id]._save_history()
            del self.__sessions_cache[guild.id]
            
    def save_all_sessions(self):
        """Sauvegarde toutes les sessions de chat."""
        for _, session in self.__sessions_cache.items():
            session._save_history()
            
    # Gestion des chatbots
    
    def get_chatbot(self, chatbot_id: int) -> Chatbot | None:
        """Récupère un chatbot par son ID."""
        r = self.data.get('global').fetch("SELECT * FROM chatbots WHERE id = ?", chatbot_id)
        if not r:
            return None
        return Chatbot(**r)
    
    def get_chatbots(self, author_id: int | None = None) -> list[Chatbot]:
        """Récupère tous les chatbots."""   
        if author_id:
            r = self.data.get('global').fetchall("SELECT * FROM chatbots WHERE author_id = ?", author_id)
        else:
            r = self.data.get('global').fetchall("SELECT * FROM chatbots")
        return [Chatbot(**row) for row in r]

    def create_chatbot(self, name: str, system_prompt: str, temperature: float = 0.9, max_completion: int = 512, context_size: int = 4096, vision_detail: str = 'auto', author_id: int = 0, guild_id: int = 0) -> Chatbot | None:
        """Créer un nouveau chatbot."""
        if name.lower() in [b.name.lower() for b in self.get_chatbots()]:
            return None
        r = self.data.get('global').evaluate('INSERT INTO chatbots VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *', name, system_prompt, temperature, max_completion, context_size, vision_detail, author_id, guild_id)
        return Chatbot(**r)
    
    def update_chatbot(self, chatbot_id: int, **kwargs) -> Chatbot | None:
        """Met à jour les paramètres d'un chatbot."""
        r = self.data.get('global').evaluate('UPDATE chatbots SET ' + ', '.join([f'{k} = ?' for k in kwargs.keys()]) + ' WHERE id = ? RETURNING *', *kwargs.values(), chatbot_id)
        if not r:
            return None
        return Chatbot(**r)
    
    def delete_chatbot(self, chatbot_id: int):
        """Supprime un chatbot."""
        self.data.get('global').execute('DELETE FROM chatbots WHERE id = ?', chatbot_id)
        
    # Gestion des utilisateurs
    
    def get_user(self, user_id: int) -> dict[str, Any] | None:
        """Récupère les données d'un utilisateur par son ID."""
        r = self.data.get('global').fetch("SELECT * FROM users WHERE user_id = ?", user_id)
        if not r:
            return None
        if r['tracking_month'] != int(f'{datetime.now().year}{datetime.now().month}'):
            self.data.get('global').execute('UPDATE users SET tracking_month = ?, prompt_tokens = 0, completion_tokens = 0 WHERE user_id = ?', int(f'{datetime.now().year}{datetime.now().month}'), user_id)
        return r
    
    def get_users(self) -> list[dict[str, Any]]:
        """Récupère tous les utilisateurs."""
        r = self.data.get('global').fetchall("SELECT * FROM users")
        return r
    
    def update_usage(self, user_id: int, prompt_tokens: int = 0, completion_tokens: int = 0):
        """Met à jour les statistiques d'utilisation d'un utilisateur."""
        self.data.get('global').execute('INSERT OR IGNORE INTO users VALUES (?, 0, 0, 0, FALSE)', user_id)
        self.data.get('global').execute('UPDATE users SET prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ? WHERE user_id = ?', prompt_tokens, completion_tokens, user_id)
    
    def ban_user(self, user_id: int):
        """Bannit un utilisateur."""
        self.data.get('global').execute('UPDATE users SET banned = TRUE WHERE user_id = ?', user_id)
        
    def unban_user(self, user_id: int):
        """Débannit un utilisateur."""
        self.data.get('global').execute('UPDATE users SET banned = FALSE WHERE user_id = ?', user_id)
        
    # Déroulement des sessions
    
    async def handle_mention_message(self, prompt_message: discord.Message, *, mentioned_message: discord.Message | None = None, override: bool = False) -> discord.Message | None:
        """Gérer la demande de complétion d'un message (mention du bot)."""
        bot_user = self.bot.user
        if not bot_user:
            await prompt_message.reply(f"**Erreur** × Le bot n'est pas connecté.", delete_after=10)
            return None
        if not prompt_message.guild:
            await prompt_message.reply(f"**Erreur** × Je ne peux pas répondre en dehors d'un salon textuel de serveur.", delete_after=10)
            return None
        
        chatsession = self.get_chat_session(prompt_message.guild)
        if not chatsession:
            last_alert = self.__last_nochatbot_alert.get(prompt_message.guild.id)
            if not last_alert or (datetime.now() - last_alert).total_seconds() > 120:
                await prompt_message.reply(f"**Erreur** × Aucun chatbot n'est actuellement configuré pour ce serveur.\n***Utilisez `/chatbot` pour définir le chatbot à utiliser pour ses réponses***", delete_after=20)
                self.__last_nochatbot_alert[prompt_message.guild.id] = datetime.now()
            return
        
        # Si le seul contenu du message est la mention du bot : on affiche ses informations
        if prompt_message.content == bot_user.mention:
            return await prompt_message.reply(content="**Chatbot actuellement chargé sur le serveur :**", embed=chatsession.chatbot.embed, mention_author=False, delete_after=30)
        
        tracking = self.get_user(prompt_message.author.id)
        if tracking and tracking['banned']:
            return
        
        channel = prompt_message.channel
        
        if mentioned_message:
            content = f"[CONTEXTE :] @{mentioned_message.author.name} : {mentioned_message.content}\n\n[DEMANDE :] {prompt_message.content}"
        else:
            content = prompt_message.content
        if not content:
            return None
        if content.startswith('?'):
            return None
        content = content.replace(bot_user.mention, '').strip()
        
        image_url = None
        if prompt_message.attachments:
            image_urls = [attachment.url for attachment in prompt_message.attachments if attachment.content_type and attachment.content_type.startswith('image')]
            if image_urls:
                image_url = image_urls[0]
        elif mentioned_message and mentioned_message.attachments:    
            image_urls = [attachment.url for attachment in mentioned_message.attachments if attachment.content_type and attachment.content_type.startswith('image')]
            if image_urls:
                image_url = image_urls[0]
        elif re.match(r'^https?://', content):
            if content.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                image_url = content
        
        if bot_user.mentioned_in(prompt_message) or override:
            async with channel.typing():
                result = await chatsession.get_completion(content, image_url, name=prompt_message.author.name)
                if not result:
                    await prompt_message.reply(f"**Erreur OpenAI** × Impossible de générer une réponse.\nEssayez `/chatbotreset` si l'erreur persiste.", delete_after=15)
                    return None
                
                completion, usage = result
                if completion.finish_reason != 'stop':
                    text = completion.text + ' [...]'
                else:
                    text = completion.text

                if usage.completion_tokens > 0 or usage.prompt_tokens > 0:
                    self.update_usage(prompt_message.author.id, usage.prompt_tokens, usage.completion_tokens)
                
                answer = await prompt_message.reply(text, mention_author=False, suppress_embeds=True, allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False, replied_user=True))
                return answer
        return None
    
    async def handle_context_message(self, target_message: discord.Message, context: str, context_author: discord.Member | discord.User):
        """Gérer la demande de complétion d'un message tiers (utilisation du menu contextuel)."""
        bot_user = self.bot.user
        if not bot_user:
            await target_message.reply(f"**Erreur** × Le bot n'est pas connecté.", delete_after=10)
            return None
        if not target_message.guild:
            await target_message.reply(f"**Erreur** × Je ne peux pas répondre en dehors d'un salon textuel de serveur.", delete_after=10)
            return None
        
        chatbot = self.get_chat_session(target_message.guild)
        if not chatbot:
            last_alert = self.__last_nochatbot_alert.get(target_message.guild.id)
            if not last_alert or (datetime.now() - last_alert).total_seconds() > 120:
                await target_message.reply(f"**Erreur** × Aucun chatbot n'est actuellement configuré pour ce serveur.\n***Utilisez `/chatbot` pour définir le chatbot à utiliser pour ses réponses***", delete_after=20)
                self.__last_nochatbot_alert[target_message.guild.id] = datetime.now()
            return
        
        tracking = self.get_user(target_message.author.id)
        if tracking and tracking['banned']:
            return
        
        channel = target_message.channel

        if target_message.content:
            content = f"[CONTEXTE :] @{target_message.author} : {target_message.content}\n\n[DEMANDE :] {context}"
            if not content:
                return None
            content = content.replace(bot_user.mention, '').strip()
        else:
            content = context

        image_url = None
        if target_message.attachments:
            image_urls = [attachment.url for attachment in target_message.attachments if attachment.content_type and attachment.content_type.startswith('image')]
            if image_urls:
                image_url = image_urls[0]
        elif re.match(r'^https?://', content):
            if content.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                image_url = content 
        
        async with channel.typing():
            result = await chatbot.get_completion(content, image_url, name=context_author.name)
            if not result:
                await target_message.channel.send(f"{context_author.mention} **Erreur OpenAI** × Impossible de générer une réponse.", delete_after=10)
                return None
            
            completion, usage = result
            if completion.finish_reason != 'stop':
                text = completion.text + ' [...]'
            else:
                text = completion.text

            text = f"[{context_author.mention} : *{context}*] {text}"
                
            if usage.completion_tokens > 0 or usage.prompt_tokens > 0:
                self.update_usage(context_author.id, usage.prompt_tokens, usage.completion_tokens)
            
            answer = await target_message.reply(text, mention_author=False, suppress_embeds=True, allowed_mentions=discord.AllowedMentions(users=[context_author], roles=False, everyone=False, replied_user=False))
            return answer
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Répondre automatiquement aux messages des utilisateurs avec ChatGPT"""
        if message.author.bot:
            return
        if not message.guild:
            return
        if not message.content or message.content[0] in ('?', '!', '.', '/'):
            return
        if message.mention_everyone:
            return
        if self.bot.user and not self.bot.user.mentioned_in(message):
            return
        
        mentioned_message = None
        if message.reference and message.reference.message_id:
            ref_message = await message.channel.fetch_message(message.reference.message_id)
            if ref_message and not ref_message.author.bot:  
                mentioned_message = ref_message
            
        await self.handle_mention_message(message, mentioned_message=mentioned_message)
        
    # COMMANDES ---------------------------------------------------------------
    
    @app_commands.command(name='chatbot')
    @app_commands.rename(chatbot_id='chatbot')
    async def chatbot_select(self, interaction: Interaction, chatbot_id: int | None = None):
        """Permet de choisir un chatbot à utiliser sur le serveur.
        
        :param chatbot_id: Chatbot à utiliser (optionnel)"""
        chatbots = self.get_chatbots()
        if not chatbots:
            return await interaction.response.send_message("**Erreur** × Aucun chatbot n'est actuellement configuré.", ephemeral=True)
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        await interaction.response.defer()
        view = ChatbotSelectionMenu(chatbots, interaction.user, start_at=chatbot_id - 1 if chatbot_id else 0)
        await view.start(interaction)
        await view.wait()
        if view.selected_chatbot:
            # S'il y a déjà une session de chat, on la retire
            pre_text = "**Session de chatbot créée**"
            if session := self.get_chat_session(interaction.guild):
                self.remove_chat_session(interaction.guild, session.chatbot)
                pre_text = "**Session de chatbot remplacée**"
            self.create_chat_session(interaction.guild, view.selected_chatbot)
            await interaction.edit_original_response(content=f"{pre_text} · `{view.selected_chatbot.name}` a été chargé sur ce serveur.", view=None, embed=None)
            
    @app_commands.check(check_botreset)
    @app_commands.command(name='chatbotreset')
    @app_commands.rename(since='depuis')
    @app_commands.choices(since=[
        app_commands.Choice(name='Tout', value='all'), 
        app_commands.Choice(name='Dernière heure', value='hour'),
        app_commands.Choice(name='Dernier jour', value='day'),
        app_commands.Choice(name='Dernière semaine', value='week')])
    async def chatbot_reset(self, interaction: Interaction, since: str = 'all'):
        """Efface la mémoire du chatbot actuellement chargé.
        
        :param since: Supprimer l'historique depuis une certaine date (optionnel)"""  
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        if session := self.get_chat_session(interaction.guild):
            if since == 'all':
                session.clear_history()
                await interaction.response.send_message("**Mémoire effacée** × L'historique de la session de chat a été supprimé.")
            else:
                since_dt = datetime.now() - {'hour': timedelta(hours=1), 'day': timedelta(days=1), 'week': timedelta(weeks=1)}[since]
                session.clear_history(since_dt)
            await interaction.response.send_message("**Mémoire effacée** × L'historique de la session de chat a été supprimé depuis la date spécifiée.")
        else:
            await interaction.response.send_message("**Erreur** × Aucun chatbot n'est actuellement chargé sur ce serveur.", ephemeral=True)
    
    # GESTION DES CHATBOTS ----------------------------------------------------
    
    chatbot_manage = app_commands.Group(name='chatbot-manager', description='Gestion des chatbots', default_permissions=discord.Permissions(), guild_only=True)
    
    @chatbot_manage.command(name='create')
    @app_commands.rename(name='nom', system_prompt='instructions', temperature='température', max_completion='longueur_messages', context_size='taille_contexte', vision_detail='détails_vision')
    @app_commands.choices(vision_detail=[app_commands.Choice(name='Automatique', value='auto'),
                                         app_commands.Choice(name='Basse qualité', value='low'),
                                         app_commands.Choice(name='Haute qualité', value='high')])
    async def chatbot_create(self, interaction: Interaction, name: str, system_prompt: str, temperature: app_commands.Range[float, 0.1, 2.0] = 0.9, max_completion: app_commands.Range[int, 1, 1024] = 500, context_size: app_commands.Range[int, 1, 64000] = 8000, vision_detail: str = 'auto'):
            """Créer un nouveau chatbot (global).
            
            :param name: Nom du chatbot
            :param system_prompt: Instructions système du chatbot
            :param temperature: Température de génération (par défaut 0.9)
            :param max_completion: Longueur maximale des réponses (par défaut 500 tokens)
            :param context_size: Taille maximale du contexte (par défaut 8000 tokens)
            :param vision_detail: Détail d'analyse des images (par défaut 'auto')"""
            if not isinstance(interaction.guild, discord.Guild):
                return await interaction.response.send_message("**Erreur** × Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
            if len(name) > 32:
                return await interaction.response.send_message("**Erreur** × Nom du chatbot trop long.", ephemeral=True)
            if len(system_prompt) > 2000:
                return await interaction.response.send_message("**Erreur** × Instructions système trop longues.", ephemeral=True)
            if vision_detail not in ('auto', 'low', 'medium', 'high'):
                return await interaction.response.send_message("**Erreur** × Détail de la vision invalide.", ephemeral=True)
            
            nb_chatbots_in_guild = len(self.get_chatbots())
            if nb_chatbots_in_guild >= MAX_CHATBOT_PER_GUILD and interaction.guild.id not in MAX_CHATBOT_IMMUNE_GUILDS:
                return await interaction.response.send_message(f"**Erreur** × Vous avez atteint la limite de {MAX_CHATBOT_PER_GUILD} chatbots créés par serveur.", ephemeral=True)
            
            chatbot = self.create_chatbot(name, system_prompt, temperature, max_completion, context_size, vision_detail, interaction.user.id, interaction.guild.id)
            if not chatbot:
                return await interaction.response.send_message("**Erreur** × Un chatbot avec ce nom existe déjà.", ephemeral=True)
            await interaction.response.send_message(f"**Succès** · Le chatbot `{chatbot.name}` a été créé avec succès.", embed=chatbot.embed, ephemeral=True)
        
    @chatbot_manage.command(name='update')
    @app_commands.rename(chatbot_id='chatbot', key='clé', new_value='nouv_valeur')
    async def chatbot_update(self, interaction: Interaction, chatbot_id: int, key: str, new_value: str):
        """Modifier un chatbot (global).

        :param chatbot_id: ID du chatbot
        :param key: Clé à modifier
        :param new_value: Nouvelle valeur"""
        norm_table = {
            'name': {'type': str, 'check': lambda v: len(v) <= 32},
            'system_prompt': {'type': str, 'check': lambda v: len(v) <= 2000},
            'temperature': {'type': float, 'check': lambda v: 0.1 <= v <= 2.0},
            'max_completion': {'type': int, 'check': lambda v: 1 <= v <= 1024},
            'context_size': {'type': int, 'check': lambda v: 1 <= v <= 64000},
            'vision_detail': {'type': str, 'check': lambda v: v in ('auto', 'low', 'medium', 'high')}
        }
        if key not in norm_table:
            return await interaction.response.send_message("**Erreur** × Clé invalide.", ephemeral=True)
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        chatbot = self.get_chatbot(chatbot_id)
        if not chatbot:
            return await interaction.response.send_message("**Erreur** × Chatbot introuvable.", ephemeral=True)

        norm = norm_table[key]
        new_value = norm['type'](new_value)
        if not norm['check'](new_value):
            return await interaction.response.send_message(f"**Erreur** × Valeur invalide pour `{key}`.", ephemeral=True)
        
        chatbot = self.update_chatbot(chatbot_id, **{key: new_value})
        if not chatbot:
            return await interaction.response.send_message("**Erreur** × Impossible de mettre à jour le chatbot.", ephemeral=True)
        
        await interaction.response.send_message(f"**Succès** · Le chatbot `{chatbot.name}` a été mis à jour avec succès.", ephemeral=True)
    
    @chatbot_manage.command(name='delete')
    @app_commands.rename(chatbot_id='chatbot')
    async def chatbot_delete(self, interaction: Interaction, chatbot_id: int):
        """Supprimer un chatbot (global).

        :param chatbot_id: ID du chatbot"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        chatbot = self.get_chatbot(chatbot_id)
        if not chatbot:
            return await interaction.response.send_message("**Erreur** × Chatbot introuvable.", ephemeral=True)
        
        self.delete_chatbot(chatbot_id)
        await interaction.response.send_message(f"**Succès** · Le chatbot `{chatbot.name}` a été supprimé avec succès.", ephemeral=True)
        
    @chatbot_select.autocomplete('chatbot_id')
    @chatbot_update.autocomplete('chatbot_id')
    @chatbot_delete.autocomplete('chatbot_id')
    async def chatbot_id_autocomplete(self, interaction: discord.Interaction, current: str):
        if not isinstance(interaction.guild, discord.Guild):
            return []
        chatbots = self.get_chatbots()
        r = fuzzy.finder(current, chatbots, key=lambda c: c.name)
        return [app_commands.Choice(name=chatbot.name, value=chatbot.id) for chatbot in r][:20]
    
    # STATISTIQUES ------------------------------------------------------------
    
    @app_commands.command(name='chatstats')
    @app_commands.rename(user='utilisateur')
    async def chat_stats(self, interaction: Interaction, user: discord.User | None = None):
        """Affiche les statistiques d'utilisation des chatbots."""
        u = user or interaction.user
        tracking = self.get_user(u.id)
        if not tracking:
            return await interaction.response.send_message("**Aucune donnée** × Aucune donnée n'est disponible pour cet utilisateur.", ephemeral=True)
        
        # Coût de tokens pour les demandes (input) = 0.150$ pour 1 million de tokens
        input_cost = tracking['prompt_tokens'] * (0.150 / 1000000)
        # Coût de tokens pour les réponses (output) = 0.60$ pour 1 million de tokens 
        output_cost = tracking['completion_tokens'] * (0.60 / 1000000)
        total_cost = input_cost + output_cost
        
        embed = discord.Embed(title=f"Statistiques d'utilisation · ***{u}***", color=pretty.DEFAULT_EMBED_COLOR)
        embed.add_field(name='Demandes | Coût', value=f"{tracking['prompt_tokens']} tokens | ${input_cost:.4f}", inline=False)
        embed.add_field(name='Réponses* | Coût', value=f"{tracking['completion_tokens']} tokens | ${output_cost:.4f}", inline=False)
        embed.add_field(name='Coût total estimé', value=f"= ${total_cost:.4f}", inline=False)
        embed.set_thumbnail(url=u.display_avatar.url)
        embed.set_footer(text=f"Statistiques réinitialisées chaque 1er du mois | (*) Comprend la transcription audio")
        await interaction.response.send_message(embed=embed)
        
    # GESTION DES UTILISATEURS ------------------------------------------------
    
    @commands.command(name='gptblock', hidden=True)
    @commands.is_owner()
    async def block_user(self, ctx: commands.Context, user: discord.User):
        """Bannir un utilisateur du service de chatbot"""
        self.ban_user(user.id)
        await ctx.send(f"**Utilisateur banni** · L'utilisateur {user} a été banni du service de chatbot.")
    
    @commands.command(name='gptunblock', hidden=True)
    @commands.is_owner()
    async def unblock_user(self, ctx: commands.Context, user: discord.User):
        """Débannir un utilisateur du service de chatbot"""
        self.unban_user(user.id)
        await ctx.send(f"**Utilisateur débanni** · L'utilisateur {user} a été débanni du service de chatbot.")
    
async def setup(bot):
    await bot.add_cog(GPT(bot))
