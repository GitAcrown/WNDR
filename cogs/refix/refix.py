import asyncio
import logging
import re

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from datetime import datetime

from pytz import utc

from common import dataio
from common.utils import fuzzy, pretty

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

LINK_FIXERS = {
    'twitter.com': {
        'search': r'https?://(?:www\.)?twitter\.com/',
        'replace': [
            'https://fixupx.com/',
            'https://vxtwitter.com/'
        ]
    },
    'x.com': {
        'search': r'https?://(?:www\.)?x\.com/',
        'replace': [
            'https://fixupx.com/',
            'https://vxtwitter.com/'
        ]
    },
    'tiktok.com': {
        'search': r'https?://(?:www\.)?tiktok\.com/',
        'replace': [
            'https://vm.vxtiktok.com/',
            'https://fixtiktok.com/'
        ]
    },
    'vm.tiktok.com': {
        'search': r'https?://(?:www\.)?vm\.tiktok\.com/',
        'replace': [
            'https://vm.vxtiktok.com/',
            'https://fixtiktok.com/'
        ]
    }
}

# UI --------------------------------------------------------------------------

class FixLinkMenu(discord.ui.View):
    """Menu permettant de changer de correcteur de lien."""
    def __init__(self, link_message: discord.Message, fixed_links: list[str]):
        super().__init__(timeout=20)
        self.link_message = link_message
        self.replacement_message = None
        self.fixed_links = fixed_links
        self._current = 0
        
    async def start(self):
        self.replacement_message = await self.link_message.reply(self.fixed_links[self._current], view=self, mention_author=False)
        await asyncio.sleep(0.1)
        try:
            await self.link_message.edit(suppress=True)
        except discord.HTTPException:
            pass
    
    @discord.ui.button(label="Changer d'intég.", style=discord.ButtonStyle.blurple)
    async def switch(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        self._current = (self._current + 1) % len(self.fixed_links)
        if self.replacement_message:
            await self.replacement_message.edit(content=self.fixed_links[self._current], allowed_mentions=discord.AllowedMentions.none())
        
    @discord.ui.button(label='Rétablir', style=discord.ButtonStyle.danger)
    async def delete(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if self.replacement_message:
            await self.replacement_message.delete()
            try:
                await self.link_message.edit(suppress=False)
            except discord.HTTPException:
                pass
        self.stop()
            
    async def on_timeout(self):
        if self.replacement_message:
            await self.replacement_message.edit(view=None, allowed_mentions=discord.AllowedMentions.none())
        self.stop()

# Cog -------------------------------------------------------------------------
class ReFix(commands.Cog):
    """Outils de correction de liens."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)

        enabled_fixers = dataio.TableBuilder(
            '''CREATE TABLE IF NOT EXISTS enabled_fixers (
                guild_id INTEGER,
                label TEXT,
                enabled BOOLEAN DEFAULT 1,
                PRIMARY KEY (guild_id, label)
                )'''
        )
        self.data.link('fixers', enabled_fixers)
        
        settings = dataio.DictTableBuilder(
            'settings',
            {
                'auto_fix': False # Ne pas attendre la réaction pour corriger automatiquement les liens
            }
        )
        self.data.link(discord.Guild, settings)
        self.__fixed = []
        
    def cog_unload(self):
        self.data.close_all()
        
    # Paramètres ----------------------------------------------------------------
    
    def get_auto_fix(self, guild: discord.Guild):
        return self.data.get(guild).get_dict_value('settings', 'auto_fix', cast=bool)
    
    def set_auto_fix(self, guild: discord.Guild, value: bool):
        self.data.get(guild).set_dict_value('settings', 'auto_fix', value)  
        
    # Gestion des corrections de liens ------------------------------------------
    
    def get_fixers(self, guild_id: int):
        r = self.data.get('fixers').fetchall("SELECT * FROM enabled_fixers WHERE guild_id = ?", guild_id)
        available_fixers = {fixer: True for fixer in LINK_FIXERS.keys()}
        for fixer in r:
            available_fixers[fixer['label']] = fixer['enabled']
        return [{'label': label, 'enabled': enabled} for label, enabled in available_fixers.items()]

    def get_fixer(self, guild_id: int, label: str) -> bool:
        r = self.data.get('fixers').fetchone("SELECT * FROM enabled_fixers WHERE guild_id = ? AND label = ?", guild_id, label)
        return r['enabled'] if r else True # Par défaut, les fixers sont tous activés
        
    def set_fixer(self, guild_id: int, label: str, enabled: bool):
        self.data.get('fixers').execute("REPLACE INTO enabled_fixers VALUES (?, ?, ?)", guild_id, label, enabled)
        
    # Utils ---------------------------------------------------------------------
    
    def get_label_from_url(self, url: str):
        url = re.sub(r'^(https?://)?(www\.)?', '', url)
        url = re.sub(r'\/.*$', '', url)
        if not re.match(r'^[a-zA-Z0-9\-\.]+$', url):
            return None
        return url.lower()
    
    def get_fixers_from_label(self, label: str):
        return [fixer for fixer in LINK_FIXERS if fixer in label]
    
    # Events --------------------------------------------------------------------
    
    # Quand le bot détecte un lien qui peut être corrigé, il ajoute une réaction pour proposer de le corriger
    # Si l'utilisateur réagit avec l'emoji, le bot envoie un menu pour choisir le correcteur
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        to_delete = False
        for url in re.findall(r'https?://[^\s]+', message.content):
            label = self.get_label_from_url(url)
            if not label:
                continue
            if not self.get_fixer(message.guild.id, label):
                continue
            fixers = self.get_fixers_from_label(label)
            if not fixers:
                continue
            for fixer in fixers:
                patterns = LINK_FIXERS[fixer]['search']
                if re.search(patterns, url):
                    to_delete = True
                    await message.add_reaction('🔗')
                    break
        if to_delete:
            await asyncio.sleep(60)
            await message.clear_reaction('🔗')
                
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot:
            return
        if not reaction.message.guild:
            return 
        if reaction.emoji != '🔗':
            return
        if (datetime.now(utc) - reaction.message.created_at).total_seconds() > 600: # Si le message a plus de 10 minutes, on ne fait rien
            return
        if reaction.message.id in self.__fixed:
            return
        links = re.findall(r'https?://[^\s]+', reaction.message.content)
        if not links:
            return
        url = links[0]
        label = self.get_label_from_url(url)
        if not label:
            return
        if not self.get_fixer(reaction.message.guild.id, label):
            return
        fixers = self.get_fixers_from_label(label)
        if not fixers:
            return
        fixed_links = []
        for fixer in fixers:
            patterns = LINK_FIXERS[fixer]['search']
            if re.search(patterns, url):
                fixed_links.extend([re.sub(patterns, replace, url) for replace in LINK_FIXERS[fixer]['replace']])
        if not fixed_links:
            return
        self.__fixed.append(reaction.message.id)
        try:
            await reaction.clear()
        except discord.HTTPException:
            pass
        menu = FixLinkMenu(reaction.message, fixed_links)
        await menu.start()
        

    # Commandes ------------------------------------------------------------------
    
    fix_group = app_commands.Group(name='linkfix', description='Gestion des correcteurs de liens.', guild_only=True, default_permissions=discord.Permissions(manage_messages=True))
    
    @fix_group.command(name='list')
    async def fix_list(self, interaction: Interaction):
        """Liste les correcteurs de liens activés"""
        if not interaction.guild_id:
            return await interaction.response.send_message('Cette commande ne peut être utilisée que sur un serveur.', ephemeral=True)
        fixers = self.get_fixers(interaction.guild_id)
        if not fixers:
            await interaction.response.send_message('**Liste vide** × Aucun correcteur de lien activé.', ephemeral=True)
            return
        embed = discord.Embed(title='Correcteurs de liens activés', color=pretty.DEFAULT_EMBED_COLOR)
        txt = '\n'.join([f'`{fixer["label"]}` → ' + ('Activé' if fixer['enabled'] else 'Désactivé') for fixer in fixers])
        embed.description = txt
        embed.set_footer(text=f"Certains liens peuvent posséder plusieurs correcteurs\nUtilisez '/linkfix set' pour activer/désactiver un correcteur")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    @fix_group.command(name='set')
    @app_commands.rename(label='lien', enabled='activer')
    async def fix_set(self, interaction: Interaction, label: str, enabled: bool):
        """Active ou désactive un correcteur de lien
        
        :param label: Le type de lien à corriger
        :param enabled: Activer ou désactiver le correcteur"""
        if not interaction.guild_id:
            return await interaction.response.send_message('Cette commande ne peut être utilisée que sur un serveur.', ephemeral=True)
        if not self.get_fixers_from_label(label):
            return await interaction.response.send_message('**Erreur** × Ce correcteur de lien n\'existe pas.', ephemeral=True)
        self.set_fixer(interaction.guild_id, label, enabled)
        await interaction.response.send_message(f'**Modifié** • Correcteur de lien `{label}` {'activé' if enabled else 'désactivé'} avec succès.', ephemeral=True)
        
    @fix_set.autocomplete('label')
    async def fix_autocomplete_label(self, interaction: Interaction, current: str):
        labels = LINK_FIXERS.keys()
        r = fuzzy.finder(current, labels)
        return [app_commands.Choice(name=label, value=label) for label in r][:10]
        
async def setup(bot):
    await bot.add_cog(ReFix(bot))