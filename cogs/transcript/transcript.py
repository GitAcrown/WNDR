import io
import json
import logging
from pathlib import Path
import random
import re
import os
from collections import namedtuple
from datetime import datetime, timedelta
from typing import Any, Literal

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from moviepy.editor import VideoFileClip

from common import dataio
from common.utils import fuzzy, pretty

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

CURRENT_MONTH = lambda: datetime.now().strftime('%Y-%m')
    
class Transcript(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        
        usage = dataio.TableBuilder(
            '''CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER PRIMARY KEY,
                month TEXT,
                seconds_transcribed INTEGER DEFAULT 0
                )'''
        )
        self.data.link('global', usage)
        
        self.client = AsyncOpenAI(
            api_key=self.bot.config['OPENAI_API_KEY'], # type: ignore
        )
        
        self.create_audio_transcription = app_commands.ContextMenu(
            name="Transcription audio",
            callback=self.transcript_audio_callback)
        self.bot.tree.add_command(self.create_audio_transcription)
        
    def cog_unload(self):
        self.data.close_all()
        
    # Suivi de l'utilisation ---------------------------------------------------
    
    def get_usage(self, user_id: int, month: str = CURRENT_MONTH()):
        r = self.data.get('global').fetchone("SELECT * FROM usage WHERE user_id = ? AND month = ?", (user_id, month))
        return r or {'user_id': user_id, 'month': month, 'seconds_transcribed': 0}
    
    def update_usage(self, user_id: int, seconds: int, month: str = CURRENT_MONTH()):
        usage = self.get_usage(user_id, month)
        usage['seconds_transcribed'] += seconds
        self.data.get('global').execute("REPLACE INTO usage VALUES (:user_id, :month, :seconds_transcribed)", usage)
        
    # Utilitaires --------------------------------------------------------------    
        
    async def get_audio(self, message: discord.Message) -> io.BytesIO | None:
        """Récupère le fichier audio d'un message."""
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith('audio'):
                buffer = io.BytesIO()
                await attachment.save(buffer, seek_begin=True)
                return buffer
        return None
    
    async def get_audio_from_video(self, message: discord.Message) -> Path | None:
        """Récupère le fichier audio d'une vidéo."""
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith('video'):
                path = self.data.get_subfolder('temp', create=True) / f'{datetime.now().timestamp()}.mp4'
                await attachment.save(path)
                clip = VideoFileClip(str(path))
                audio = clip.audio
                if not audio:
                    return None
                audio_path = path.with_suffix('.wav')
                audio.write_audiofile(str(audio_path))
                clip.close()
                
                os.remove(str(path))
                return audio_path
        return None
    
    # Handlers -----------------------------------------------------------------
    
    async def audio_transcription(self, file: io.BytesIO | Path, model: str = 'whisper-1'):
        try:
            transcript = await self.client.audio.transcriptions.create(
                model=model,
                file=file
            )
        except Exception as e:
            logger.error(f"ERREUR OPENAI : {e}", exc_info=True)
            return None
        return transcript.text
    
    async def handle_audio_transcription(self, interaction: Interaction, message: discord.Message, transcript_author: discord.Member | discord.User):
        await interaction.response.defer()
        
        attach_type = message.attachments[0].content_type
        if not message.attachments or not attach_type:
            return await interaction.followup.send(f"**Erreur** × Aucun fichier supporté n'est attaché au message.", ephemeral=True)
        
        if attach_type.startswith('video'):
            file_or_buffer = await self.get_audio_from_video(message)
        elif attach_type.startswith('audio'):
            file_or_buffer = await self.get_audio(message)
        else:
            return await interaction.followup.send(f"**Erreur** × Le fichier audio n'a pas pu être récupéré.", ephemeral=True)
    
        if not file_or_buffer:
            return await interaction.followup.send(f"**Erreur** × Le fichier audio n'a pas pu être récupéré.", ephemeral=True)
        
        await interaction.followup.send(f"Transcription en cours de traitement pour le message de {message.author.mention}...", ephemeral=True)
        
        transcript = await self.audio_transcription(file_or_buffer)
        if not transcript:
            return await interaction.followup.send(f"**Erreur** × Aucun texte n'a pu être transcrit depuis ce fichier audio.")
        
        await interaction.delete_original_response()
        if file_or_buffer != io.BytesIO:
            os.remove(str(file_or_buffer))

        if len(transcript) > 1950:
            return await message.reply(f"**Transcription demandée par {transcript_author.mention}** :\n>>> {transcript[:1950]}...", mention_author=False)
        
        await message.reply(f"**Transcription demandée par {transcript_author.mention}** :\n>>> {transcript}", mention_author=False)
        
    async def transcript_audio_callback(self, interaction: Interaction, message: discord.Message):
        """Callback pour demander la transcription d'un message audio via le menu contextuel."""
        if interaction.channel_id != message.channel.id:
            return await interaction.response.send_message("**Action impossible** × Le message doit être dans le même salon", ephemeral=True)
        if not message.attachments:
            return await interaction.response.send_message("**Erreur** × Aucun fichier n'est attaché au message.", ephemeral=True)
        return await self.handle_audio_transcription(interaction, message, interaction.user)
    
async def setup(bot):
    await bot.add_cog(Transcript(bot))
