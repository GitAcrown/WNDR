import asyncio
from hmac import new
import logging
from os import system
import re
from typing import Iterable
from regex import F
import unidecode
from openai import AsyncOpenAI
from datetime import datetime, timedelta

import discord
import tiktoken
from discord import Interaction, app_commands
from discord.ext import commands

from common import dataio
from common.utils import fuzzy, pretty, interface

logger = logging.getLogger(f'WANDR.{__name__.split(".")[-1]}')

DEFAULT_PRESET = {
    'name': 'Basique',
    'system_prompt': "Tu es un chatbot serviable qui répond à toutes les questions qu'on te pose de manière pertinente en utilisant un langage naturel, fluide et en étant le plus synthétique possible. Tu utilises un ton amical et familier pour t'adresser à l'utilisateur.",
    'temperature': 0.8,
    'max_completion': 256,
    'context_size': 1024,
    'author_id': 0
}

CONSUM_LEVELS = {
    'A': {'score_max': 5000, 'label': 'Très faible'},
    'B': {'score_max': 10000, 'label': 'Faible'},
    'C': {'score_max': 15000, 'label': 'Moyenne'},
    'D': {'score_max': 20000, 'label': 'Élevée'},
    'E': {'score_max': 50000, 'label': 'Très élevée'}
}

class KeepNameOrEdit(discord.ui.View):
    """Crée un menu pour garder le nom généré ou éditer le nom d'un preset"""
    def __init__(self, *, timeout: float = 60.0, generated_name: str, author: discord.Member | None = None):
        super().__init__(timeout=timeout)
        self.generated_name = generated_name
        self.author = author
        self.value = None
        
    async def start(self, interaction: Interaction[discord.Client]):
        await interaction.response.send_message(f"**Nom généré pour vous** · Voici le nom généré pour votre preset de chatbot : `{self.generated_name}`\nSouhaitez-vous le **conserver** ou **l'éditer** ?", ephemeral=True, view=self)
    
    @discord.ui.button(label='Conserver', style=discord.ButtonStyle.green)
    async def keep_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = self.generated_name
        self.stop()
        
    @discord.ui.button(label='Éditer', style=discord.ButtonStyle.gray)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditNameModal(self.generated_name)
        await interaction.response.send_modal(modal)
        if await modal.wait():
            self.value = self.generated_name
            self.stop()
        self.value = modal.new_name.value
        self.stop()
        
    async def on_timeout(self) -> None:
        self.value = self.generated_name
        self.stop()
        
    async def interaction_check(self, interaction: Interaction[discord.Client]) -> bool:
        if self.author:
            if interaction.user.id == self.author.id:
                return True
            await interaction.response.send_message("Seul l'auteur du message peut choisir de garder ou d'éditer le nom généré.", ephemeral=True, delete_after=10)
            return False
        return True
        
class EditNameModal(discord.ui.Modal):
    def __init__(self, generated_name: str):
        super().__init__(title='Renommer le preset', timeout=60.0)
        
        self.new_name = discord.ui.TextInput(label='Nouveau nom', min_length=3, max_length=32, placeholder=generated_name)
        self.add_item(self.new_name)
        
    async def on_submit(self, interaction) -> None:
        await interaction.response.defer()
        self.stop()

class ContinueButtonView(discord.ui.View):
    """Ajoute un bouton pour continuer une complétion de message"""
    def __init__(self, *, timeout: float = 90.0, author: discord.Member | None = None):
        super().__init__(timeout=timeout)
        self.author = author
        self.value = None
        
    @discord.ui.button(label='Demander de continuer', style=discord.ButtonStyle.gray)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        
    async def on_timeout(self) -> None:
        self.value = False
        self.stop()
        
    async def interaction_check(self, interaction: Interaction[discord.Client]) -> bool:
        if self.author:
            if interaction.user.id == self.author.id:
                return True
            await interaction.response.send_message("Seul l'auteur du message peut continuer la complétion.", ephemeral=True, delete_after=10)
            return False
        return True

class Chatbot:
    def __init__(self, 
                 cog: 'Robot', 
                 guild: discord.Guild, 
                 preset_id: int):
        self.__cog = cog
        self.guild = guild
        self.preset_id = preset_id
        
        self.__load_preset()
        self._history = self.__load_history()
        
        self.clear_history_before(datetime.now().timestamp() - 60*60*24*30)
        
    def __str__(self):
        return self.name
    
    def __repr__(self):
        return f'<Chatbot {self.name}>'
    
    @property
    def author(self) -> discord.Member | None:
        return self.guild.get_member(self.author_id)
    
    # GESTION DES DONNÉES
    
    def __load_preset(self):
        r = self.__cog.data.get(self.guild).fetch('''SELECT * FROM presets WHERE id = ?''', self.preset_id)
        if not r:
            raise ValueError(f'Preset {self.preset_id} not found')
        self.name : str = r['name']
        self.system_prompt : str = r['system_prompt']
        self.temperature : float = r['temperature']
        self.max_completion : int = r['max_completion']
        self.context_size : int = r['context_size']
        self.author_id : int = r['author_id']
        
    def __load_history(self):
        return self.__cog.data.get(self.guild).fetchall('''SELECT * FROM history WHERE preset_id = ?''', self.preset_id) 
    
    # GESTION DE L'HISTORIQUE
    
    def add_message(self, role: str, content: str, username: str, channel_id: int):
        msg_data = {
            'preset_id': self.preset_id,
            'timestamp': datetime.now().timestamp(),
            'role': role,
            'content': content,
            'username': username,
            'channel_id': channel_id
        }
        self._history.append(msg_data)
        self.__cog.data.get(self.guild).execute('''INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)''', *msg_data.values())
        
    def remove_message(self, timestamp: float):
        self._history = [m for m in self._history if m['timestamp'] != timestamp]
        self.__cog.data.get(self.guild).execute('''DELETE FROM history WHERE preset_id = ? AND timestamp = ?''', self.preset_id, timestamp)
        
    def clear_history(self):
        self._history = []
        self.__cog.data.get(self.guild).execute('''DELETE FROM history WHERE preset_id = ?''', (self.preset_id))
        
    def clear_history_before(self, timestamp: float):
        self._history = [m for m in self._history if m['timestamp'] >= timestamp]
        self.__cog.data.get(self.guild).execute('''DELETE FROM history WHERE preset_id = ? AND timestamp < ?''', self.preset_id, timestamp)
        
    # CONTEXTE
    
    def _sanitize_messages(self, messages: Iterable[dict]) -> Iterable[dict[str, str]]:
        sanitized = []
        for m in messages:
            if 'username' in m and m['role'] == 'user':
                sanitized.append({'role': m['role'], 'content': m['content'], 'name': m['username']})
            else:
                sanitized.append({'role': m['role'], 'content': m['content']})
        return sanitized
    
    def get_context(self):
        tokenizer = tiktoken.get_encoding('cl100k_base')
        system = [{'role': 'system', 'content': self.system_prompt}]
        if not self._history:   
            return system
        ctx = []
        ctx_size = len(tokenizer.encode(str(self.system_prompt)))
        for msg in self._history[::-1]:
            content_size = len(tokenizer.encode(str(msg['content'])))
            if msg['role'] == 'system':
                continue
            if ctx_size + content_size >= self.context_size:
                break
            ctx_size += content_size
            ctx.append(msg)
        if ctx:
            ctx = system + ctx[::-1]
        else:
            ctx = system + [self._history[-1]]
        return self._sanitize_messages(ctx)
        
    # COMPLETIONS
    
    async def complete(self, prompt: str, channel: discord.TextChannel | discord.Thread, username: str = 'none') -> dict[str, str] | None:
        if username:
            username = ''.join([c for c in unidecode.unidecode(username) if c.isalnum() or c.isspace()]).rstrip()
            username = re.sub(r"[^a-zA-Z0-9_-]", "", username)
            
        self.add_message('user', prompt, username, channel.id)
        ctx = self.get_context()
        if not ctx:
            return None
        client = self.__cog.client
        try:
            completion = await client.chat.completions.create(
                model='gpt-3.5-turbo',
                messages=ctx, # type: ignore
                max_tokens=self.max_completion,
                temperature=self.temperature
            )
        except Exception as e:
            logger.error(f'Erreur OpenAI : {e}', exc_info=True)
            return None
        
        payload = {}
        response = completion.choices[0].message.content if completion.choices else None
        if response:
            self.add_message('assistant', response, 'assistant', channel.id)
            payload['response'] = response
            payload['finish_reason'] = completion.choices[0].finish_reason
        else:
            return None
        if completion.usage:
            payload['input_usage'] = completion.usage.prompt_tokens 
            payload['output_usage'] = completion.usage.completion_tokens
        return payload

class Robot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        
        presets = dataio.BuildTable(
            '''CREATE TABLE IF NOT EXISTS presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                system_prompt TEXT,
                temperature REAL DEFAULT 0.8,
                max_completion INTEGER DEFAULT 256,
                context_size INTEGER DEFAULT 1024,
                author_id INTEGER
                )''',
            default_values=[DEFAULT_PRESET]
        )
        history = dataio.BuildTable(
            '''CREATE TABLE IF NOT EXISTS history (
                preset_id INTEGER,
                timestamp REAL,
                role TEXT,
                content TEXT,
                username TEXT,
                channel_id INTEGER,
                PRIMARY KEY (preset_id, timestamp),
                FOREIGN KEY (preset_id) REFERENCES presets(id) ON DELETE CASCADE
                )'''
        )
        self.data.register(discord.Guild, presets, history)
        
        usage_tracking = dataio.BuildTable(
            '''CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER PRIMARY KEY,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                month INTEGER,
                banned INTEGER DEFAULT 0
                )'''
        )
        self.data.register('global', usage_tracking)
        
        self.client = AsyncOpenAI(
            api_key=self.bot.config['OPENAI_API_KEY'], # type: ignore
        )
        
        self.__sessions : dict[int, Chatbot] = {}
        self.__live_sessions : dict[int, datetime] = {}
        
    def cog_unload(self):
        self.data.close_all()   
        
    # ----- Gestion des presets -----
    
    def get_preset(self, guild: discord.Guild, preset_id: int):
        """Obtenir le chatbot associé à un preset"""
        try:
            return Chatbot(self, guild, preset_id)
        except ValueError:
            return None
        
    def get_presets(self, guild: discord.Guild):
        """Obtenir la liste des presets"""
        return self.data.get(guild).fetchall('''SELECT * FROM presets''')
    
    def create_preset(self, guild: discord.Guild, preset_data: dict):
        """Créer un nouveau preset"""
        self.data.get(guild).execute('''INSERT INTO presets VALUES (NULL, ?, ?, ?, ?, ?, ?)''', *preset_data.values())
        
    def update_preset(self, guild: discord.Guild, preset_id: int, preset_data: dict):
        """Mettre à jour un preset"""
        self.data.get(guild).execute('''UPDATE presets SET name = ?, system_prompt = ?, temperature = ?, max_completion = ?, context_size = ?, author_id = ? WHERE id = ?''', *preset_data.values(), preset_id)
        
    def delete_preset(self, guild: discord.Guild, preset_id: int):
        """Supprimer un preset"""
        self.data.get(guild).execute('''DELETE FROM presets WHERE id = ?''', preset_id)
        
    def get_preset_embed(self, preset: dict, live_mode: bool = False):
        """Créer un embed pour un preset"""
        if not live_mode:
            embed = discord.Embed(title=f'***{preset['name']}***', color=pretty.DEFAULT_EMBED_COLOR)
        else:
            embed = discord.Embed(title=f'***{preset["name"]}*** (Mode live)', color=pretty.DEFAULT_EMBED_COLOR)
        embed.add_field(name='Instructions du preset', value=pretty.codeblock(preset['system_prompt'], 'yaml'), inline=False)
        if preset['temperature'] > 1.4:
            embed.add_field(name='Température', value=pretty.codeblock(f'{preset["temperature"]} (!)'), inline=True)
        else:
            embed.add_field(name='Température', value=pretty.codeblock(preset['temperature']), inline=True)
        embed.add_field(name='Long. réponses', value=pretty.codeblock(f'max. {preset["max_completion"]} tokens'), inline=True)
        embed.add_field(name='Taille contexte', value=pretty.codeblock(f'max. {preset["context_size"]} tokens'), inline=True)
        
        # Déterminer le niveau de consommation de tokens
        tokenizer = tiktoken.get_encoding('cl100k_base')
        sys_prompt_len = len(tokenizer.encode(preset['system_prompt']))
        estim = self.get_consumption_estimate(preset['max_completion'], preset['context_size'], sys_prompt_len)
        level = next((k for k, v in CONSUM_LEVELS.items() if estim <= v['score_max']), 'E')
        embed.add_field(name='Consom. estimée*', value=f'**{level}** (*{CONSUM_LEVELS[level]["label"]}*)', inline=True)
        
        author = self.bot.get_user(preset['author_id'])
        footer = ''
        if author:
            footer = f'Auteur : {author} | '
        footer += '(*) Basée sur les paramètres et non sur les données réelles'
        embed.set_footer(text=footer)
        return embed
        
    # ----- Gestion des sessions -----
    
    def get_session(self, channel: discord.TextChannel | discord.Thread):
        """Obtenir la session associée à un salon"""
        return self.__sessions.get(channel.id)
    
    def fetch_last_session(self, channel: discord.TextChannel | discord.Thread):
        """Récupère le dernier chatbot chargé dans un salon"""
        history = self.data.get(channel.guild).fetchall('''SELECT * FROM history WHERE channel_id = ?''', channel.id)
        if not history:
            return None
        last_preset_id = history[-1]['preset_id']
        return self.get_preset(channel.guild, last_preset_id)
    
    def attach_chatbot(self, channel: discord.TextChannel | discord.Thread, chatbot: Chatbot):
        """Attacher un chatbot à un salon"""
        self.__sessions[channel.id] = chatbot
        
    def detach_chatbot(self, channel: discord.TextChannel | discord.Thread):
        """Détacher un chatbot d'un salon"""
        self.__sessions.pop(channel.id, None)
        
    def turn_on_live(self, thread: discord.Thread):
        """Activer le mode live pour un thread"""
        self.__live_sessions[thread.id] = datetime.now()
        
    def turn_off_live(self, thread: discord.Thread):
        """Désactiver le mode live pour un thread"""
        self.__live_sessions.pop(thread.id, None)
        
    # ----- Tracking des tokens -----
    
    def get_usage(self, user: discord.User | discord.Member) -> dict:
        """Obtenir les informations de tracking d'un utilisateur"""
        r = self.data.get('global').fetch('''SELECT * FROM usage WHERE user_id = ?''', user.id)
        if r:
            if r['month'] != datetime.now().month:
                self.data.get('global').execute('''UPDATE usage SET input_tokens = 0, output_tokens = 0, month = ? WHERE user_id = ?''', datetime.now().month, user.id)
                return self.get_usage(user)
            return r
        return {'user_id': user.id, 'input_tokens': 0, 'output_tokens': 0, 'month': datetime.now().month, 'banned': 0}
    
    def update_usage(self, user: discord.User | discord.Member, input_tokens: int, output_tokens: int):
        """Mettre à jour les informations de tracking d'un utilisateur"""
        self.data.get('global').execute('''INSERT OR IGNORE INTO usage VALUES (?, ?, ?, ?, ?)''', user.id, input_tokens, output_tokens, datetime.now().month, 0)
        self.data.get('global').execute('''UPDATE usage SET input_tokens = ?, output_tokens = ? WHERE user_id = ?''', input_tokens, output_tokens, user.id)
        
    def clear_usage(self):
        """Efface toutes les données de tracking des utilisateurs sauf du mois en cours"""
        self.data.get('global').execute('''DELETE FROM usage WHERE month != ?''', datetime.now().month)     
        
    def ban_user(self, user: discord.User | discord.Member):
        """Bannir un utilisateur du service"""
        self.data.get('global').execute('''UPDATE usage SET banned = 1 WHERE user_id = ?''', user.id)
        
    def unban_user(self, user: discord.User | discord.Member):
        """Débannir un utilisateur du service"""
        self.data.get('global').execute('''UPDATE usage SET banned = 0 WHERE user_id = ?''', user.id)
        
    def get_consumption_estimate(self, answer_lenght: int, context_size: int, system_prompt_lenght: int):
        """Estimer la consommation de tokens"""
        total_tokens = 0
        context = system_prompt_lenght
        med_msg_len = 0.8 * answer_lenght
        for _ in range(10):
            context = min(context_size, context + med_msg_len)
            total_tokens += context + answer_lenght
        return total_tokens
        
    # ----- Discussions -----
    
    async def handle_completion(self, message: discord.Message, *, completion_message: discord.Message | None = None, override_mention: bool = False) -> bool:
        """Gérer la demande de complétion d'un message"""
        bot_user = self.bot.user
        if not bot_user:
            await message.reply("**Erreur** × .", mention_author=True, delete_after=10)
            return False
        if not isinstance(message.channel, discord.TextChannel | discord.Thread):
            await message.reply("**Erreur** × Je ne peux pas discuter dans ce type de salon.", mention_author=True, delete_after=10)
            return False
        if not isinstance(message.author, discord.Member):    
            return False  
        if message.author.bot:
            return False
        
        chatbot = self.get_session(message.channel)
        if not chatbot:
            chatbot = self.fetch_last_session(message.channel)
            if not chatbot:
                if bot_user.mentioned_in(message):
                    await message.reply("**Aucun chatbot** × Aucun preset n'a déjà été chargé dans ce salon. Utilisez `/chatbot load` pour charger un chatbot.", mention_author=True, delete_after=10)
                return False
            self.attach_chatbot(message.channel, chatbot)
        
        tracking = self.get_usage(message.author)
        if tracking and tracking['banned']:
            await message.reply("**Inaccessible** × Vous avez été banni du service de chatbot.", mention_author=True, delete_after=10) 
            return False
        
        channel = message.channel
        content = message.content
        if not content:
            return False
        if content.startswith('?'): # Ignore les commandes
            return False
        # On retire la mention du bot
        content = content.replace(bot_user.mention, '').strip()
        
        completion = None
        if completion_message:
            content = 'suite'
            async with channel.typing():
                completion = await chatbot.complete(content, channel, message.author.name)
        elif bot_user.mentioned_in(message) or override_mention:
            async with channel.typing():
                completion = await chatbot.complete(content, channel, message.author.name)
                
        if completion:
            response = completion['response']
            is_finished = completion['finish_reason'] == 'stop'
            usage = {'input_usage': completion['input_usage'], 'output_usage': completion['output_usage']}
            user_data = self.get_usage(message.author)
            if usage and user_data:
                input_tokens = user_data['input_tokens'] + usage['input_usage']
                output_tokens = user_data['output_tokens'] + usage['output_usage']
                self.update_usage(message.author, input_tokens, output_tokens)
                
            if is_finished:
                await message.reply(response, 
                                    mention_author=False, 
                                    suppress_embeds=True, 
                                    allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False, replied_user=True))
                return True
            
            view = ContinueButtonView(timeout=120, author=message.author)
            resp = await message.reply(response, 
                                       view=view,
                                       mention_author=False, 
                                       suppress_embeds=True, 
                                       allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False, replied_user=True))
            await view.wait()
            await resp.edit(view=None)
            if view.value:
                await self.handle_completion(message, completion_message=resp, override_mention=override_mention)
            return True
        return False
    
    # LISTENERS ================================================================
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Répondre automatiquement aux messages des utilisateurs avec ChatGPT"""
        channel = message.channel
        if message.author.bot:
            return
        if channel.id in self.__live_sessions: # Mode live activé
            if message.content.startswith('?'):
                return
            if message.content.startswith('.'):
                return
            if datetime.now() - self.__live_sessions[channel.id] > timedelta(minutes=10):
                return self.turn_off_live(channel) # type: ignore
            self.__live_sessions[channel.id] = datetime.now() 
            return await self.handle_completion(message, override_mention=True)
        await self.handle_completion(message)
        
    # GESTION DES CHATBOTS =====================================================
    
    chatbot_cmds = app_commands.Group(name='chatbot', description='Gestion des presets de chatbots', guild_only=True)
    
    @chatbot_cmds.command(name='info')
    async def chatbot_info(self, interaction: Interaction, channel: discord.TextChannel | discord.Thread | None = None):
        """Afficher les informations du chatbot chargé dans un salon
        
        :param channel: Salon à consulter (optionnel)
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée en dehors d'un serveur.", ephemeral=True)
        
        chan = channel or interaction.channel
        if not isinstance(chan, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("**Erreur** × Je ne peux pas charger de chatbot dans ce type de salon.", ephemeral=True)
        chatbot = self.get_session(chan)
        if not chatbot:
            chatbot = self.fetch_last_session(chan)
            if not chatbot:
                return await interaction.response.send_message(f"**Aucun chatbot** × Aucun preset de chatbot n'a été chargé dans le salon {chan.mention}.", ephemeral=True)

        live_mode = False
        if chan.id in self.__live_sessions:
            live_mode = True
        embed = self.get_preset_embed({
            'name': chatbot.name,
            'system_prompt': chatbot.system_prompt,
            'temperature': chatbot.temperature,
            'max_completion': chatbot.max_completion,
            'context_size': chatbot.context_size,
            'author_id': chatbot.author_id
        }, live_mode)
        await interaction.response.send_message(f"**Info. chatbot** · Chargé sur {chan.mention} :", embed=embed)
    
    @chatbot_cmds.command(name='load')
    @app_commands.rename(preset_id='identifiant')
    async def chatbot_load(self, interaction: Interaction, preset_id: int, live_mode: bool = False):
        """Charger un preset de chatbot dans un salon
        
        :param preset_id: Identifiant du preset à charger
        :param live_mode: Activer le mode live (réponses automatiques)
        """
        guild = interaction.guild
        
        if not isinstance(guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée en dehors d'un serveur.", ephemeral=True)
        
        preset = self.get_preset(guild, preset_id)
        if not preset:
            return await interaction.response.send_message("**Erreur** × Le preset spécifié n'existe pas.", ephemeral=True)
        
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("**Erreur** × Je ne peux pas charger de chatbot dans ce type de salon.", ephemeral=True)
        
        # On regarde s'il y a déjà un chatbot chargé dans ce salon
        chatbot = self.get_session(channel)
        replacing = False
        if chatbot:
            replacing = True
    
        if live_mode:
            if isinstance(channel, discord.Thread):
                self.turn_on_live(channel)
            else:
                return await interaction.response.send_message("**Erreur** × Le mode live n'est disponible que dans les threads.", ephemeral=True)
        
        self.attach_chatbot(channel, preset)
        if live_mode:
            return await interaction.response.send_message(f"**Chatbot {'chargé' if not replacing else 'remplacé'}** · Le preset de chatbot `{preset.name}` a été chargé dans ce thread en mode live.\nLe **mode live** vous permet de recevoir des réponses automatiques, il se désactivera automatiquement après 10 minutes sans activité. Vous pourrez le réactiver avec la même commande.")
        await interaction.response.send_message(f"**Chatbot {'chargé' if not replacing else 'remplacé'}** · Le preset de chatbot `{preset.name}` a été chargé dans ce salon.")
    
    @chatbot_cmds.command(name='create')
    @app_commands.rename(system_prompt='initialisation', temperature='température', answer_length='longueur_réponse', context_size='taille_contexte')
    async def chatbot_create(self, interaction: Interaction, system_prompt: str, temperature: app_commands.Range[float, 0.1, 2.0] = 0.8, answer_length: app_commands.Range[int, 1, 1024] = 256, context_size: app_commands.Range[int, 1, 2048] = 1024):
        """Créer un nouveau preset de chatbot
        
        :param system_prompt: Instructions d'initialisation du chatbot
        :param temperature: Niveau de créativité du chatbot (0.1 - 2.0)
        :param answer_length: Longueur maximale des réponses (1 - 1024 tokens)
        :param context_size: Taille du contexte (1 - 2048 tokens)
        """
        guild = interaction.guild
        
        if not isinstance(guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée en dehors d'un serveur.", ephemeral=True)
        
        # Vérifications -----
        tokenizer = tiktoken.get_encoding('cl100k_base')    
        if len(tokenizer.encode(system_prompt)) > context_size:
            return await interaction.response.send_message("**Instructions trop longues** × La *taille du contexte* est trop petite pour contenir les instructions d'initialisation.", ephemeral=True)
        
        # Récupération d'un nom (en utilisant GPT)
        name = f'Chatbot_{len(self.get_presets(guild)) + 1}'
        prompt = "En 3 mots maximum et une longueur maximale de 32 caractères, tu dois nommer un preset de chatbot utilisant ces instructions d'initialisation : " + system_prompt
        try:
            completion = await self.client.chat.completions.create(
            model='gpt-3.5-turbo',
            messages=[{'role': 'system', 'content': prompt}],
            max_tokens=32,
            temperature=0.6
        )
        except Exception as e:
            logger.error(f'Erreur OpenAI : {e}', exc_info=True)
            return await interaction.response.send_message("**Erreur** × Impossible de générer un nom pour le preset.", ephemeral=True)
        if completion.choices:
            name = completion.choices[0].message.content or name
        if name in [p['name'] for p in self.get_presets(guild)]:
            name = f'{name}_{len(self.get_presets(guild)) + 1}'
        view = KeepNameOrEdit(generated_name=name, author=interaction.user)
        await view.start(interaction)
        await view.wait()
        if not view.value:
            return await interaction.followup.send("**Annulé** · La création du preset a été annulée.", ephemeral=True)
        
        # Création du preset
        preset_data = {
            'name': view.value,
            'system_prompt': system_prompt,
            'temperature': temperature,
            'max_completion': answer_length,
            'context_size': context_size,
            'author_id': interaction.user.id
        }
        self.create_preset(guild, preset_data)
        embed = self.get_preset_embed(preset_data)
        await interaction.edit_original_response(content=f"**Preset créé** · Le preset de chatbot `{view.value}` a été créé avec succès.", embed=embed, view=None)

    @chatbot_cmds.command(name='delete')
    @app_commands.rename(preset_id='identifiant')
    async def chatbot_delete(self, interaction: Interaction, preset_id: int):
        """Supprimer un preset de chatbot
        
        :param preset_id: Identifiant du preset à supprimer
        """
        guild = interaction.guild
        
        if not isinstance(guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée en dehors d'un serveur.", ephemeral=True)
        
        preset = self.get_preset(guild, preset_id)
        if not preset:
            return await interaction.response.send_message("**Erreur** × Le preset spécifié n'existe pas.", ephemeral=True)
        preset_data = {
            'name': preset.name,
            'system_prompt': preset.system_prompt,
            'temperature': preset.temperature,
            'max_completion': preset.max_completion,
            'context_size': preset.context_size,
            'author_id': preset.author_id
        }
        
        if interaction.user.guild_permissions.manage_guild or interaction.user.id == preset.author_id:
            await interaction.response.defer(ephemeral=True)
            confview = interface.ConfirmationView(users=[interaction.user])
            await interaction.followup.send(f"**Confirmation** · Êtes vous sûr de vouloir supprimer le preset de chatbot `{preset.name}` ?", ephemeral=True, view=confview, embed=self.get_preset_embed(preset_data))
            await confview.wait()
            if not confview.value:
                await interaction.edit_original_response(content="Suppression annulée.")
                await asyncio.sleep(10)
                await interaction.delete_original_response()
            
            self.delete_preset(guild, preset_id)
            return await interaction.edit_original_response(content=f"**Preset supprimé** · Le preset de chatbot `{preset.name}` a été supprimé avec succès.", view=None, embed=None)
        return await interaction.response.send_message("**Autorisation insuffisante** × Vous n'avez pas la permission de supprimer ce preset.", ephemeral=True)
        
    @chatbot_load.autocomplete('preset_id')
    @chatbot_delete.autocomplete('preset_id')
    async def chatbot_id_autocomplete(self, interaction: discord.Interaction, current: str):
        if not isinstance(interaction.guild, discord.Guild):
            return []
        presets = self.get_presets(interaction.guild)
        r = fuzzy.finder(current, presets, key=lambda x: x['name'])
        return [app_commands.Choice(name=p['name'], value=p['id']) for p in r][:10]
    
    @chatbot_cmds.command(name='list')
    async def chatbot_list(self, interaction: Interaction):
        """Lister les presets de chatbots disponibles"""
        guild = interaction.guild
        
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée en dehors d'un serveur.", ephemeral=True)
        
        presets = self.get_presets(guild)
        if not presets:
            return await interaction.response.send_message("**Aucun preset** × Aucun preset de chatbot n'a été créé.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        embeds = []
        for preset in presets:
            embed = self.get_preset_embed(preset)
            embed.set_footer(text=f'Page {len(embeds) + 1}/{len(presets)} · ID: {preset["id"]} · Auteur: {self.bot.get_user(preset["author_id"])} | (*) Basée sur les paramètres et non sur les données réelles')
            embeds.append(embed)
            
        view = interface.EmbedPaginatorMenu(embeds=embeds, users=[interaction.user], loop=True)
        await view.start(interaction)
        
    # STATISTIQUES =============================================================
    
    chatstats_cmds = app_commands.Group(name='chatstats', description='Statistiques d\'utilisation du chatbot', guild_only=True)  
    
    @chatstats_cmds.command(name='usage')
    @app_commands.rename(user='utilisateur')
    async def chatstats_usage(self, interaction: Interaction, user: discord.User | None = None):
        """Afficher les statistiques d'utilisation du chatbot
        
        :param user: Utilisateur à consulter (optionnel)
        """
        u = user or interaction.user
        tracking = self.get_usage(u)
        if not tracking:
            return await interaction.response.send_message("**Aucune donnée** × Aucune donnée n'est disponible pour cet utilisateur.", ephemeral=True)
        
        # Coût de tokens pour les demandes (input) = 0.50$ pour 1 million de tokens
        input_cost = tracking['input_tokens'] * (0.50 / 1000000)
        # Coût de tokens pour les réponses (output) = 1.50$ pour 1 million de tokens
        output_cost = tracking['output_tokens'] * (1.50 / 1000000)
        total_cost = input_cost + output_cost
        
        embed = discord.Embed(title=f"Statistiques d'utilisation · ***{u}***", color=pretty.DEFAULT_EMBED_COLOR)
        embed.add_field(name='Demandes | Coût', value=f"{tracking['input_tokens']} tokens | ${input_cost:.4f}", inline=False)
        embed.add_field(name='Réponses | Coût', value=f"{tracking['output_tokens']} tokens | ${output_cost:.4f}", inline=False)
        embed.add_field(name='Coût total estimé', value=f"= ${total_cost:.4f}", inline=False)
        embed.set_thumbnail(url=u.display_avatar.url)
        embed.set_footer(text=f"Statistiques réinitialisées chaque 1er du mois")
        await interaction.response.send_message(embed=embed)
        
    @chatstats_cmds.command(name='top')
    async def chatstats_top(self, interaction: Interaction, top: app_commands.Range[int, 3, 30] = 10):
        """Afficher le top des utilisateurs les plus actifs
        
        :param top: Nombre d'utilisateurs à afficher (3 - 30)"""
        guild = interaction.guild
        
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée en dehors d'un serveur.", ephemeral=True)
        
        self.clear_usage()
        
        # classer par tokens d'entrée + sortie
        users = self.data.get('global').fetchall('''SELECT * FROM usage WHERE banned = 0 ORDER BY input_tokens + output_tokens DESC LIMIT ?''', top)
        if not users:
            return await interaction.response.send_message("**Aucune donnée** × Aucune donnée n'est disponible pour le moment.", ephemeral=True)
        users = [user for user in users if guild.get_member(user['user_id'])]
        
        embed = discord.Embed(title=f"Statistiques d'utilisation · Top {top}", color=pretty.DEFAULT_EMBED_COLOR)
        text = []
        for i, user in enumerate(users, start=1):
            u = guild.get_member(user['user_id'])
            if not u:
                continue
            total_tokens = user['output_tokens'] + user['input_tokens']
            text.append(f"{i}. {u.name} - {total_tokens}")
            
        total_input = sum(user['input_tokens'] for user in users)
        total_output = sum(user['output_tokens'] for user in users)
        total_cost = (total_input * 0.50 + total_output * 1.50) / 1000000
        embed.description = pretty.codeblock('\n'.join(text), 'yaml')
        embed.set_footer(text=f"Coût total estimé ce mois-ci : ${total_cost:.4f}")
        await interaction.response.send_message(embed=embed)

    # BLOCKLIST ================================================================
    
    @commands.command(name='gptblock', hidden=True)
    @commands.is_owner()
    async def block_user(self, ctx: commands.Context, user: discord.User):
        """Bannir un utilisateur du service de chatbot"""
        self.ban_user(user)
        await ctx.send(f"**Utilisateur banni** · L'utilisateur {user} a été banni du service de chatbot.")
    
    @commands.command(name='gptunblock', hidden=True)
    @commands.is_owner()
    async def unblock_user(self, ctx: commands.Context, user: discord.User):
        """Débannir un utilisateur du service de chatbot"""
        self.unban_user(user)
        await ctx.send(f"**Utilisateur débanni** · L'utilisateur {user} a été débanni du service de chatbot.")
    
async def setup(bot):
    await bot.add_cog(Robot(bot))
