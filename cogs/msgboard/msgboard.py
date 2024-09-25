import logging
from datetime import datetime
import re

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands

from common import dataio

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

LOGS_EXPIRATION = 60*60*24*7 # 7 jours
CACHE_SAVE_INTERVAL = 60*30 # 30 minutes


class MsgBoard(commands.Cog):
    """Système de compilation des meilleurs messages."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        
        settings = dataio.DictTableBuilder(
            'settings',
            {
                'BoardChannelID': 0,
                'Threshold': 3,
                'VoteEmoji': '⭐',
                'MaxMessageAge': 60*60*24 # 24h
            })
        self.data.link(discord.Guild, settings)
        
        msgboard_logs = dataio.TableBuilder(
            '''CREATE TABLE IF NOT EXISTS msgboard_logs (
                message_id INTEGER PRIMARY KEY,
                copied_message_id INTEGER DEFAULT NULL,
                timestamp INTEGER
            )'''
        )
        self.data.link('global', msgboard_logs)
        
        self.__board_cache = self.load_cache()
        self.__last_cache_save = 0
        
    
    def cog_unload(self):
        self.save_cache()
        self.data.close_all()
        
    # Paramètres --------------------------------------------------------------
    
    def get_board_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel = guild.get_channel(self.data.get(guild).get_dict_value('settings', 'BoardChannelID', cast=int))
        if isinstance(channel, discord.TextChannel):
            return channel
        
    def get_threshold(self, guild: discord.Guild) -> int:
        return self.data.get(guild).get_dict_value('settings', 'Threshold', cast=int)
    
    def get_vote_emoji(self, guild: discord.Guild) -> str:
        return self.data.get(guild).get_dict_value('settings', 'VoteEmoji')
    
    def get_max_message_age(self, guild: discord.Guild) -> int:
        return self.data.get(guild).get_dict_value('settings', 'MaxMessageAge', cast=int)
    
    # Cache et logs -----------------------------------------------------------
    
    def add_message_to_cache(self, message_id: int, copied_message_id: int):
        self.__board_cache.append({'message_id': message_id, 'copied_message_id': copied_message_id, 'timestamp': datetime.now().timestamp()})
        
    def save_cache(self):
        self.__board_cache = [msg for msg in self.__board_cache if msg['timestamp'] > datetime.now().timestamp() - LOGS_EXPIRATION]
        self.data.get('global').executemany('INSERT OR REPLACE INTO msgboard_logs VALUES (?, ?, ?)', [(msg['message_id'], msg['copied_message_id'], msg['timestamp']) for msg in self.__board_cache])
        
    def load_cache(self):
        self.data.get('global').execute('DELETE FROM msgboard_logs WHERE timestamp < ?', datetime.now().timestamp() - LOGS_EXPIRATION)
        return self.data.get('global').fetchall('SELECT * FROM msgboard_logs') or []
    
    # Gestion du webhook -------------------------------------------------------
    
    async def get_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            if webhook.user == self.bot.user:
                return webhook
        if self.bot.user:
            return await channel.create_webhook(name=f'{self.bot.user.name} Webhook')
        return await channel.create_webhook(name='MsgBoard Webhook')
    
    async def send_copied_message(self, message: discord.Message):
        if not isinstance(message.guild, discord.Guild) or not isinstance(message.author, discord.Member):
            raise ValueError("Le message doit provenir d'un serveur.")
        
        board_channel = self.get_board_channel(message.guild)
        if not board_channel:
            return
        try:
            webhook = await self.get_webhook(board_channel)
        except discord.HTTPException as e:
            logger.error(f"Erreur lors de la création du webhook pour le salon de compilation : {e}")
            return
        
        jump_to_button = discord.ui.Button(label="Message d'origine", url=message.jump_url)
        jump_view = discord.ui.View()
        jump_view.add_item(jump_to_button)
        
        reply = message.reference.resolved if message.reference else None
        reply_content = ''
        if reply and isinstance(reply, discord.Message):
            reply_msg = await message.channel.fetch_message(reply.id)
            reply_content = f"> **{reply_msg.author.name}** · <t:{int(reply_msg.created_at.timestamp())}>"
            if reply_msg.content:
                reply_content += f"\n> {reply_msg.content}"
            if reply_msg.attachments:
                attachments_links = ' '.join([attachment.url for attachment in reply_msg.attachments])
                reply_content += f"\n> {attachments_links}"
                
        files, extra = [], []
        if message.attachments:
            files = [await attachment.to_file() for attachment in message.attachments if attachment.size < message.guild.filesize_limit]
            extra = [attachment.url for attachment in message.attachments if attachment.size >= message.guild.filesize_limit]
            
        content = f"{reply_content}\n{message.content}" if message.content else reply_content
        if extra:
            content += '\n' + ' '.join(extra)
        
        async with aiohttp.ClientSession() as session:
            try:
                await webhook.send(
                    content=content,
                    username=message.author.name,
                    avatar_url=message.author.display_avatar.url,
                    embeds=message.embeds,
                    files=files,
                    silent=True,
                    view=jump_view
                )
            except discord.HTTPException as e:
                logger.error(f"Erreur lors du repost du message {message.id} sur le salon de compilation : {e}")
        
    # EVENT ====================================================================
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        if not channel.permissions_for(channel.guild.me).read_message_history:
            return
        
        guild = channel.guild
        if not self.data.get(guild).get_dict_value('settings', 'BoardChannelID', cast=int):
            return
        
        reaction_emoji = payload.emoji.name
        if reaction_emoji != self.get_vote_emoji(guild):
            return
        message = await channel.fetch_message(payload.message_id)
        if not message:
            return
        if message.created_at.timestamp() < datetime.now().timestamp() - self.get_max_message_age(guild):
            return
        
        vote_emoji = self.get_vote_emoji(guild)
        votes_count = [reaction.count for reaction in message.reactions if str(reaction.emoji) == vote_emoji]
        if not votes_count:
            return
        votes_count = votes_count[0]
        
        if votes_count >= self.get_threshold(guild) and not any(msg['message_id'] == message.id for msg in self.__board_cache):
            self.add_message_to_cache(message.id, message.id)
            await self.send_copied_message(message)
            if self.__last_cache_save < datetime.now().timestamp() - CACHE_SAVE_INTERVAL:
                self.save_cache()
                self.__last_cache_save = datetime.now().timestamp()
                
            emoji = self.get_vote_emoji(guild)
            board_channel = self.get_board_channel(guild)
            if board_channel:
                text = f"### `{emoji}` {board_channel.mention} • Ce message a été ajouté au salon de compilation !"
            else:
                text = f"### `{emoji}` **Compilation de messages** • Ce message a été ajouté au salon de compilation !"
            await message.reply(text, mention_author=False, delete_after=30)
                
    # COMMANDES ================================================================
    
    config_group = app_commands.Group(name='config-msgboard', description="Configuration du système de compilation des meilleurs messages.", guild_only=True, default_permissions=discord.Permissions(manage_messages=True))
        
    @config_group.command(name='channel')
    @app_commands.rename(channel='salon')
    async def config_channel(self, interaction: Interaction, channel: discord.TextChannel | None):
        """Configure le salon de compilation des messages.

        :param channel: Salon de compilation des messages
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        guild = interaction.guild
        if not channel:
            self.data.get(guild).set_dict_value('settings', 'BoardChannelID', 0)
            return await interaction.response.send_message("**Salon de compilation supprimé** • Les messages ne seront plus compilés dessus.\n-# Vous pouvez supprimer le webhook lié si vous ne pensez pas utiliser d'autres fonctionnalités similaires sur ce salon textuel.", ephemeral=True)
        
        if not channel.permissions_for(guild.me).manage_webhooks:
            return await interaction.response.send_message("**Impossible de configurer le salon de compilation** • Je n'ai pas la permission de gérer les webhooks sur ce salon.", ephemeral=True)

        try:
            await self.get_webhook(channel)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"**Impossible de configurer le salon de compilation** • `{e}`", ephemeral=True)
        
        self.data.get(guild).set_dict_value('settings', 'BoardChannelID', channel.id)
        await interaction.response.send_message(f"**Salon de compilation configuré** • Les messages seront désormais compilés sur {channel.mention}.", ephemeral=True)
        
    @config_group.command(name='threshold')
    @app_commands.rename(threshold='seuil')
    async def config_threshold(self, interaction: Interaction, threshold: app_commands.Range[int, 1]):
        """Configure le seuil de votes pour enregistrer un message.
        
        :param threshold: Nombre de votes nécessaires
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        guild = interaction.guild
        self.data.get(guild).set_dict_value('settings', 'Threshold', threshold)
        await interaction.response.send_message(f"**Seuil de votes configuré** • Les messages nécessiteront désormais **{threshold}** votes (uniques) pour être compilés.", ephemeral=True)
        
    @config_group.command(name='emoji')
    async def config_emoji(self, interaction: Interaction, emoji: str):
        """Configure l'emoji de vote pour enregistrer un message.
        
        :param emoji: Emoji de vote
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        guild = interaction.guild
        if not re.match(r'(\u00a9|\u00ae|[\u2000-\u3300]|\ud83c[\ud000-\udfff]|\ud83d[\ud000-\udfff]|\ud83e[\ud000-\udfff])', emoji):
            return await interaction.response.send_message("**Erreur** · L'emoji doit être un emoji unicode de base.", ephemeral=True)
        self.data.get(guild).set_dict_value('settings', 'VoteEmoji', emoji)
        await interaction.response.send_message(f"**Emoji de vote configuré** • L'emoji à utiliser pour compiler un message est désormais {emoji}.", ephemeral=True)
    
    @config_group.command(name='max-age')
    @app_commands.rename(max_age='âge_max')
    async def config_max_age(self, interaction: Interaction, max_age: app_commands.Range[int, 1, 72]):
        """Configure l'âge maximal d'un message pour être compilé.
        
        :param max_age: Âge maximal en heures
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        guild = interaction.guild
        self.data.get(guild).set_dict_value('settings', 'MaxMessageAge', max_age * 3600)
        await interaction.response.send_message(f"**Âge maximal configuré** • Les messages de plus de **{max_age}** heures ne seront plus compilables.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(MsgBoard(bot))
