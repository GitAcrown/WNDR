import io
import logging
from typing import Literal

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from common.utils import pretty

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

# Cog -------------------------------------------------------------------------
class Extract(commands.Cog):
    """Outils d'extraction de contenus Discord."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        self.ctx_export_text = app_commands.ContextMenu(
                name="Exporter du texte",
                callback=self.export_text_callback)
        self.bot.tree.add_command(self.ctx_export_text)
        
        # Message de départ / message d'arrivée pour chaque session d'extraction (clé = user:channel)
        self._export_sessions : dict[str, dict[str, discord.Message]] = {}
        
    # Sessions -----------------------------------------------------------------
    
    def get_export_session(self, user: discord.User | discord.Member, channel: discord.TextChannel | discord.Thread):
        """Récupère la session d'extraction de l'utilisateur dans le salon donné."""
        key = f"{user.id}:{channel.id}"
        return self._export_sessions.get(key)
        
    # Extraction de texte ------------------------------------------------------
    
    async def export_messages_between(self, start: discord.Message, end: discord.Message):
        """Extrait le texte entre deux messages."""
        messages = [start]
        async for message in start.channel.history(limit=None, after=start):
            messages.append(message)
            if message.id == end.id:
                break
        return messages
    
    def messages_to_text(self, messages: list[discord.Message], format: Literal['txt', 'pdf'] = 'txt'):
        """Convertit des messages en texte brut."""
        content = []
        for message in messages:
            content.append(f"{message.author.display_name} : {message.content}")
        return "\n".join(content)
    
    # Callbacks ----------------------------------------------------------------
    
    async def export_text_callback(self, interaction: Interaction, message: discord.Message):
        """Exporte le texte entre deux messages."""
        if not message.content:
            return await interaction.response.send_message("**Erreur** · Ce message ne contient pas de texte.", ephemeral=True)
        
        user = interaction.user
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("**Erreur** · Cette commande ne peut être utilisée que dans un salon texte ou un fil de discussion.", ephemeral=True)
        session = self.get_export_session(user, channel)
        
        msg_embed = discord.Embed(color=pretty.DEFAULT_EMBED_COLOR, description=message.content)
        msg_embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        msg_embed.set_footer(text=f"Message sélectionné dans #{channel.name}")
        
        # Définir le message de départ si ce n'est pas déjà fait
        if not session or 'start' not in session:
            self._export_sessions[f"{user.id}:{channel.id}"] = {'start': message}
            link_button = discord.ui.Button(label="Aller au message", url=message.jump_url)
            view = discord.ui.View()
            view.add_item(link_button)
            return await interaction.response.send_message(f"**Message de départ défini** · Vous pouvez maintenant sélectionner le message d'arrivée avec __la même commande contextuelle__.", embed=msg_embed, view=view, ephemeral=True)
        
        # Si le message de départ est le même que le message d'arrivée	
        if session['start'].id == message.id:
            return await interaction.response.send_message("**Erreur** · Le message de départ ne peut pas être le même que le message d'arrivée.", ephemeral=True)
        
        # Si il y a plus de 24h entre les deux messages
        if (message.created_at - session['start'].created_at).total_seconds() > 86400:
            del self._export_sessions[f"{user.id}:{channel.id}"]
            return await interaction.response.send_message("**Erreur** · Les deux messages doivent être envoyés dans un intervalle de moins de 24h. Le message de départ a été __réinitialisé__.", ephemeral=True)
        
        # Si le message d'arrivée est antérieur au message de départ on les inverse
        if session['start'].created_at > message.created_at:
            session['end'] = session['start']
            session['start'] = message
        
        await interaction.response.send_message(f"**Message d'arrivée défini** · Exportation en cours... Veuillez patienter", ephemeral=True)
        # Définir le message d'arrivée
        session['end'] = message
        messages = await self.export_messages_between(session['start'], session['end'])
        text = self.messages_to_text(messages)
        textfile = discord.File(io.BytesIO(text.encode()), filename="export.txt")

        del self._export_sessions[f"{user.id}:{channel.id}"]
        await interaction.edit_original_response(content="**Exportation terminée** · Voici le texte extrait entre les deux messages.", attachments=[textfile])
        
    # Commande -----------------------------------------------------------------
    
    @app_commands.command(name="exporter")
    @app_commands.rename(start_message_id='id_msg_départ', end_message_id='id_msg_arrivée')
    async def export_text_command(self, interaction: Interaction, start_message_id: int, end_message_id: int):
        """Exporte le texte entre deux messages.
        
        :param start_message_id: ID du message de départ.
        :param end_message_id: ID du message d'arrivée."""
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("**Erreur** · Cette commande ne peut être utilisée que dans un salon texte ou un fil de discussion.", ephemeral=True)
        
        try:
            start_message = await channel.fetch_message(start_message_id)
            end_message = await channel.fetch_message(end_message_id)
        except discord.NotFound:
            return await interaction.response.send_message("**Erreur** · Les messages spécifiés n'ont pas été trouvés.", ephemeral=True)
        
        # Si le message de départ est le même que le message d'arrivée
        if start_message.id == end_message.id:
            return await interaction.response.send_message("**Erreur** · Les deux messages ne peuvent pas être les mêmes.", ephemeral=True)
        
        # Plus de 24h entre les deux messages
        if (end_message.created_at - start_message.created_at).total_seconds() > 86400:
            return await interaction.response.send_message("**Erreur** · Les deux messages doivent être envoyés dans un intervalle de moins de 24h.", ephemeral=True)
        
        # Si le message d'arrivée est antérieur au message de départ on les inverse
        if start_message.created_at > end_message.created_at:
            start_message, end_message = end_message, start_message
        
        await interaction.response.send_message(f"**Messages trouvés** · Veuillez patienter pendant l'exportation...", ephemeral=True)
        text = await self.export_messages_between(start_message, end_message)
        text = self.messages_to_text(text)
        textfile = discord.File(io.BytesIO(text.encode()), filename="export.txt")
        await interaction.edit_original_response(content="**Exportation terminée** · Voici le texte extrait entre les deux messages.", attachments=[textfile])
        
async def setup(bot):
    await bot.add_cog(Extract(bot))