import asyncio
import logging
import random
import discord
from discord import Interaction, app_commands
from discord.ext import commands

from common import dataio

logger = logging.getLogger(f'WANDR.{__name__.split(".")[-1]}')

class Misc(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
    # Compatibilité amoureuse ------------------------------
    
    @app_commands.command(name='love')
    @app_commands.guild_only()
    @app_commands.rename(user_a='utilisateur_1', user_b='utilisateur_2')
    async def comp_love(self, interation: Interaction, user_a: discord.Member, user_b: discord.Member | None = None):
        """Calculer la compatibilité amoureuse entre deux utilisateurs

        :param user_a: Premier utilisateur
        :param user_b: Deuxième utilisateur (par défaut l'auteur de la commande)
        """
        if not isinstance(interation.user, discord.Member):
            return await interation.response.send_message("**Erreur** × Vous devez être connecté à un serveur pour utiliser cette commande.", ephemeral=True)
        if not isinstance(interation.channel, discord.TextChannel | discord.Thread):
            return await interation.response.send_message("**Erreur** × Cette commande ne peut pas être utilisée dans un message privé.", ephemeral=True)
        
        if user_b is None:
            user_b = interation.user
        if user_a == user_b:
            return await interation.response.send_message("**Bizarre...** × Vous ne pouvez pas tester votre compatibilité avec vous-même...", ephemeral=True)
        
        names = [user_a.name.lower(), user_b.name.lower()]
        seed = ''.join(sorted(names))
        prc = random.Random(seed).randint(0, 100)
        await interation.response.defer()
        await asyncio.sleep(random.randint(1, 3))
        emoji = "❤️" if prc >= 50 else "💔"
        await interation.followup.send(f"# __Compabilité amoureuse__\n**{user_a.display_name}** et **{user_b.display_name}** sont compatibles à **{prc}%** {emoji}")
        
async def setup(bot):
    await bot.add_cog(Misc(bot))
