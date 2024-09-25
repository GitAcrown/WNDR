import logging
import re
import json
import colorsys
from io import BytesIO
from typing import Iterable

import aiohttp
import colorgram
import discord
from discord import Interaction, app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from common import dataio
from common.utils import fuzzy, pretty

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

class AvatarPreviewSelectMenu(discord.ui.View):
    def __init__(self, initial_interaction: Interaction, previews: list[tuple[Image.Image, str]], *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.initial_interaction = initial_interaction
        self.previews = previews
        
        self.current_page = 0
        self.result = None
        
    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user != self.initial_interaction.user:
            await interaction.response.send_message("Seul l'utilisateur ayant lancé la commande peut utiliser ce menu.", ephemeral=True)
            return False
        return True
    
    def get_embed(self) -> discord.Embed:
        current_color = self.previews[self.current_page][1]
        em = discord.Embed(title=f"Preview • {current_color}", color=discord.Color(int(current_color[1:], 16)))
        em.set_image(url="attachment://preview.png")
        em.set_footer(text=f"Couleur extraite {self.current_page + 1}/{len(self.previews)}")
        return em
    
    async def on_timeout(self) -> None:
        self.stop()
        await self.initial_interaction.delete_original_response()
        
    async def start(self) -> None:
        with BytesIO() as buffer:
            self.previews[self.current_page][0].save(buffer, format='PNG')
            buffer.seek(0)
            await self.initial_interaction.followup.send(embed=self.get_embed(), file=discord.File(buffer, filename='preview.png', description="Preview"), view=self)

    async def update(self) -> None:
        with BytesIO() as buffer:
            self.previews[self.current_page][0].save(buffer, format='PNG')
            buffer.seek(0)
            await self.initial_interaction.edit_original_response(embed=self.get_embed(), attachments=[discord.File(buffer, filename='preview.png', description="Preview")])

    # Boutons ------------------------------------------------------------------
    
    @discord.ui.button(style=discord.ButtonStyle.grey, emoji=pretty.DEFAULT_ICONS_EMOJIS['back'])
    async def previous_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.current_page -= 1
        if self.current_page < 0:
            self.current_page = len(self.previews) - 1
        await self.update()
        
    @discord.ui.button(label='Annuler', style=discord.ButtonStyle.red)
    async def stop_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await interaction.delete_original_response()
        
    @discord.ui.button(label='Appliquer', style=discord.ButtonStyle.green)
    async def choose_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.result = self.previews[self.current_page][1]
        self.stop()
        await interaction.edit_original_response(view=None)
        
    @discord.ui.button(style=discord.ButtonStyle.grey, emoji=pretty.DEFAULT_ICONS_EMOJIS['next'])
    async def next_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.current_page += 1
        if self.current_page >= len(self.previews):
            self.current_page = 0
        await self.update()
        

class Colors(commands.Cog):
    """Système de distribution de rôles de couleur."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        
        settings = dataio.DictTableBuilder(
            'settings',
            {
                'Enabled': False, 
                'PlaceNewColorRole': 'AboveLowest'
            })
        self.data.link(discord.Guild, settings)
        
        self.__assets_cache = {}
        self._load_assets()
        
        self.__color_names = {}
        self._load_color_names()
        
        self.__reorg_counts : dict[int, int] = {}
        
    def cog_unload(self):
        self.data.close_all()
        
    # Utilitaires ------------------------------------------------
    
    def rgb_to_hsv(self, hex_color: str) -> tuple[float, float, float]:
        hex_color = hex_color.lstrip("#")
        lh = len(hex_color)
        r, g, b = (int(hex_color[i:i + lh // 3], 16) / 255.0 for i in range(0, lh, lh // 3))
        return colorsys.rgb_to_hsv(r, g, b)
    
    # Assets -----------------------------------------------------
    
    def _load_assets(self) -> None:
        self.__assets_cache = {
            'text_font': ImageFont.truetype(f'{dataio.COMMON_RESOURCES_PATH}/fonts/gg_sans_light.ttf', 18),
            'name_font': ImageFont.truetype(f'{dataio.COMMON_RESOURCES_PATH}/fonts/gg_sans.ttf', 18)
        }
        
    def get_asset(self, name: str):
        return self.__assets_cache.get(name)
    
    # Couleurs ---------------------------------------------------
    
    def _load_color_names(self) -> None:
        with open(f'{self.data.assets_path}/color_names_fr.json', 'r', encoding='utf-8') as f:
            raw_colors = json.load(f)
            
        self.__color_names = {i['name']: i['hex'] for i in raw_colors}
    
    # Contrôle du rôle utilisateur -----------------------------
    
    def fetch_all_color_roles(self, guild: discord.Guild) -> Iterable[discord.Role]:
        """Récupère tous les rôles de couleur du serveur"""
        return filter(lambda r: r.name.startswith("#") and len(r.name) == 7, guild.roles)
    
    def fetch_color_role(self, guild: discord.Guild, color: discord.Color) -> discord.Role | None:
        """Récupère le rôle de couleur correspondant à la couleur donnée si il existe"""
        return discord.utils.get(self.fetch_all_color_roles(guild), color=color)

    def get_current_member_color_role(self, member: discord.Member) -> discord.Role | None:
        """Récupère le rôle de couleur actuel du membre"""
        return discord.utils.find(lambda r: r in member.roles, self.fetch_all_color_roles(member.guild))
    
    def is_color_role_displayed(self, member: discord.Member) -> bool:
        """Vérifie si le rôle de couleur du membre est affiché comme couleur de pseudo"""
        return self.get_current_member_color_role(member) == f'#{member.color.value:06X}'
    
    def get_highest_color_role(self, guild: discord.Guild) -> discord.Role | None:
        """Récupère le rôle de couleur le plus haut dans la liste du serveur"""
        return max(self.fetch_all_color_roles(guild), key=lambda r: r.position, default=None)
    
    def get_lowest_color_role(self, guild: discord.Guild) -> discord.Role | None:
        """Récupère le rôle de couleur le plus bas dans la liste du serveur"""
        return min(self.fetch_all_color_roles(guild), key=lambda r: r.position, default=None)
    
    # Paramètres de rôles de couleur ---------------------------
    
    def is_enabled(self, guild: discord.Guild) -> bool:
        """Vérifie si le système de rôles de couleur est activé sur le serveur"""
        return self.data.get(guild).get_dict_value('settings', 'Enabled', cast=bool)
    
    def set_enabled(self, guild: discord.Guild, value: bool) -> None:
        """Active ou désactive le système de rôles de couleur sur le serveur"""
        self.data.get(guild).set_dict_value('settings', 'Enabled', value)
    
    def get_role_placing(self, guild: discord.Guild) -> str:
        """Récupère le paramètre de placement des rôles de couleur"""
        return self.data.get(guild).get_dict_value('settings', 'PlaceNewColorRole')
    
    # Création et recyclage de rôles ---------------------------
    
    async def give_color_role(self, member: discord.Member, color: discord.Color) -> discord.Role | None:
        """Donne un rôle de couleur à un membre en créant ou recyclant un rôle existant

        :param member: Membre à qui donner le rôle
        :param color: Couleur voulue pour le rôle
        :return: Le rôle donné ou None si la couleur est invalide
        """
        guild = member.guild
        
        # Remplacement de #000000 par #000001 pour éviter la transparence
        if color == discord.Color.default():
            color = discord.Color(1)
            
        # Si la couleur existe déjà, on la donne
        role = self.fetch_color_role(guild, color)
        if role:
            await member.add_roles(role, reason="Attribution de couleur")
            return role
        
        # Si le membre est le seul à posséder son rôle de couleur, on le recycle
        role = self.get_current_member_color_role(member)
        if role and len(role.members) == 1:
            await role.edit(name=f"#{color.value:06X}".upper(), color=color, reason="Recyclage de couleur")
            return role
        
        # Si il y a un rôle de couleur sans membre, on le recycle
        role = discord.utils.find(lambda r: not r.members, self.fetch_all_color_roles(guild))
        if role:
            await role.edit(name=f"#{color.value:06X}".upper(), color=color, reason="Recyclage de couleur")
            await member.add_roles(role)
            return role
        
        # Si il n'y a pas de rôle de couleur disponible, on en crée un
        role = await guild.create_role(name=f"#{color.value:06X}".upper(), color=color, reason="Création d'un rôle de couleur")
        await member.add_roles(role)
        
        # Placement du rôle dans la liste
        placing = self.get_role_placing(guild)
        if placing == 'AboveLowest':
            lowest = self.get_lowest_color_role(guild)
            if lowest:
                await role.edit(position=lowest.position + 1)
        elif placing == 'BelowHighest':
            highest = self.get_highest_color_role(guild)
            if highest:
                await role.edit(position=highest.position - 1)
                
        # On réorganise les rôles périodiquement
        if guild.id not in self.__reorg_counts:
            self.__reorg_counts[guild.id] = 0
        self.__reorg_counts[guild.id] += 1
        if self.__reorg_counts[guild.id] >= 10:
            await self.clear_color_roles(guild)
            await self.reorganize_color_roles(guild)
            self.__reorg_counts[guild.id] = 0
        
        return role
    
    async def remove_color_role(self, member: discord.Member) -> None:
        """Retire tous les rôles de couleur du membre"""
        colors = self.fetch_all_color_roles(member.guild)
        roles = [r for r in colors if r in member.roles]
        for role in roles:
            await member.remove_roles(role, reason="Retrait de couleur")
            if not role.members:
                await role.delete(reason="Suppression de rôle de couleur")
                
    async def reorganize_color_roles(self, guild: discord.Guild) -> None:
        """Réorganise les rôles de couleur du serveur"""
        color_roles = list(self.fetch_all_color_roles(guild))
        color_roles.sort(key=lambda r: self.rgb_to_hsv(r.name)) # Tri par teinte
        
        placing = self.get_role_placing(guild)
        if placing == 'AboveLowest':
            lowest = self.get_lowest_color_role(guild)
            if lowest:
                await guild.edit_role_positions({r: lowest.position + (i + 1) for i, r in enumerate(color_roles)})
                
        elif placing == 'BelowHighest':
            highest = self.get_highest_color_role(guild)
            if highest:
                await guild.edit_role_positions({r: highest.position - (i - 1) for i, r in enumerate(color_roles)})
                
    async def clear_color_roles(self, guild: discord.Guild) -> None:
        """Efface tous les rôles de couleur du serveur non attribués"""
        roles = self.fetch_all_color_roles(guild)
        roles = [r for r in roles if not r.members]
        for role in roles:
            await role.delete(reason="Suppression de rôle de couleur inutilisé")
                
    # Palette de couleurs --------------------------------------
    
    def draw_image_palette(self, img: str | BytesIO, n_colors: int = 5) -> Image.Image:
        """Dessine une palette de couleurs à partir d'une image."""
        try:
            image = Image.open(img).convert('RGB')
        except Exception as e:
            raise commands.CommandError("Impossible d'ouvrir l'image.")
        colors = colorgram.extract(image.resize((100, 100)), n_colors)

        image = ImageOps.contain(image, (500, 500), method=Image.Resampling.LANCZOS)
        iw, ih = image.size
        w, h = (iw + 100, ih)
        font = self.get_asset('text_font')
        palette = Image.new('RGB', (w, h), color='white')
        maxcolors = h // 30
        colors = colors[:maxcolors] if len(colors) > maxcolors else colors
        blockheight = h // len(colors)

        draw = ImageDraw.Draw(palette)
        for i, color in enumerate(colors):
            if i == len(colors) - 1:
                block = (iw, i * blockheight, iw + 100, h)
            else:
                block = (iw, i * blockheight, iw + 100, i * blockheight + blockheight)
            palette.paste(color.rgb, block)
            hex_color = f'#{color.rgb.r:02x}{color.rgb.g:02x}{color.rgb.b:02x}'.upper()
            text_color = 'white' if color.rgb[0] + color.rgb[1] + color.rgb[2] < 384 else 'black'
            draw.text((iw + 10, i * blockheight + 10), hex_color, font=font, fill=text_color)

        palette.paste(image, (0, 0))
        return palette
    
    async def draw_discord_emulation(self, member: discord.Member, *, limit: int = 3) -> list[tuple[Image.Image, str]]:
        """Dessine des simulations des couleurs des rôles depuis l'avatar du membre sur Discord.
        
        Renvoie une liste de tuples (image, couleur) où image est une image de prévisualisation et couleur est la couleur correspondante."""
        avatar = await member.display_avatar.with_size(128).with_format('png').read()
        avatar = Image.open(BytesIO(avatar)).convert('RGBA')
        colors = colorgram.extract(avatar, limit)

        mask = Image.new('L', avatar.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0) + avatar.size, fill=255)
        avatar.putalpha(mask)
        avatar = avatar.resize((46, 46), Image.Resampling.LANCZOS)
        
        versions = []
        for name_color in [c for c in colors if f'#{c.rgb.r:02x}{c.rgb.g:02x}{c.rgb.b:02x}' != '#000000']:
            images = []
            name_font = self.get_asset('name_font')
            content_font = self.get_asset('text_font')
            for bg_color in [(0, 0, 0), (54, 57, 63), (255, 255, 255)]:
                bg = Image.new('RGBA', (420, 75), color=bg_color)
                bg.paste(avatar, (13, 13), avatar)
                d = ImageDraw.Draw(bg)
                # Nom
                d.text((76, 10), member.display_name, font=name_font, fill=name_color.rgb)
                # Contenu
                txt_color = (255, 255, 255) if bg_color in [(54, 57, 63), (0, 0, 0)] else (0, 0, 0)
                d.text((76, 34), f"Prévisualisation de l'affichage du rôle", font=content_font, fill=txt_color)
                images.append(bg)
            
            full = Image.new('RGBA', (420, 75 * 3))
            full.paste(images[0], (0, 0))
            full.paste(images[1], (0, 75))
            full.paste(images[2], (0, 75 * 2))
            versions.append((full, f'#{name_color.rgb.r:02x}{name_color.rgb.g:02x}{name_color.rgb.b:02x}'.upper()))
            
        return versions
    
    # Triggers --------------------------------------------------
    
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not member.guild.me.guild_permissions.manage_roles:
            return
        user_roles = self.get_current_member_color_role(member)
        if not user_roles:
            return
        try:
            await self.remove_color_role(member)
        except Exception:
            pass # On ignore les erreurs de retrait de rôle vu que ça sera au pire récupéré par le nettoyage automatique
    
    # COMMANDES =================================================
    
    @app_commands.command(name='palette')
    @app_commands.rename(n_colors='nombre_couleurs', url='lien', image_file='image', user='utilisateur')
    async def create_palette(self, interaction: Interaction, n_colors: app_commands.Range[int, 3, 10] = 5, 
                             url: str | None = None, image_file: discord.Attachment | None = None, user: discord.Member | None = None) -> None:
        """Génère une palette de couleurs à partir d'une image fournie ou de la dernière image envoyée sur le salon

        :param n_colors: Nombre de couleurs à extraire [Par défaut: 5]
        :param url: URL de l'image à utiliser
        :param image_file: Fichier image à utiliser
        :param user: Utilisateur dont l'avatar sera utilisé
        """
        await interaction.response.defer()
        img = None
        if image_file:
            img = BytesIO(await image_file.read()) 
        elif url:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        img = BytesIO(await resp.read())
                    else:
                        await interaction.followup.send("**Erreur** • Impossible de télécharger l'image depuis l'URL.", ephemeral=True)
        elif user:
            img = BytesIO(await user.display_avatar.read())
        elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.DMChannel, discord.GroupChannel)):
            # On récupère la dernière image envoyée sur le salon (parmi les 15 derniers messages)
            async for message in interaction.channel.history(limit=15):
                if message.attachments:
                    img = BytesIO(await message.attachments[0].read())
                    break
    
        if not img:
            return await interaction.followup.send("**Erreur** • Aucune image n'a été fournie ni trouvée dans les 10 derniers messages.", ephemeral=True)
        
        try:
            palette = self.draw_image_palette(img, n_colors)
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.followup.send("**Erreur** • Impossible de générer la palette de couleurs.", ephemeral=True)
        
        with BytesIO() as buffer:
            palette.save(buffer, format='PNG')
            buffer.seek(0)
            await interaction.followup.send(file=discord.File(buffer, filename='palette.png', description="Image avec les couleurs extraites"))
    
    
    mycolor_group = app_commands.Group(name='mycolor', description="Gestion de la couleur de votre pseudo", guild_only=True)
    
    @mycolor_group.command(name='set')
    @app_commands.rename(color='couleur')
    async def set_mycolor(self, interaction: Interaction, color: str):
        """Obtenir une couleur de pseudo personnalisée
        
        :param color: Couleur en hexadécimal (#RRGGBB)
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        if not self.is_enabled(interaction.guild):
            return await interaction.response.send_message("**Erreur** • Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True)
        
        if not re.match(r'^#?([0-9a-fA-F]{6})$', color, re.IGNORECASE):
            return await interaction.response.send_message("**Erreur** • La couleur doit être en hexadécimal (#RRGGBB).", ephemeral=True)
        
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("**Erreur** • Je n'ai pas la permission de gérer les rôles.", ephemeral=True)
        
        try:
            role = await self.give_color_role(interaction.user, discord.Color(int(color.lstrip('#'), 16)))
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.response.send_message("**Erreur** • Impossible de donner le rôle de couleur. Demandez à un modérateur de vérifier mes permissions.", ephemeral=True)
        if not role:
            return await interaction.response.send_message("**Erreur** • Il y a eu une erreur dans la création ou la récupération de votre rôle de couleur.", ephemeral=True)
        
        await interaction.response.send_message(f"**Couleur définie** • Votre couleur de pseudo a été définie sur {role.mention}.", ephemeral=True)
        
    @mycolor_group.command(name='remove')
    async def remove_mycolor(self, interaction: Interaction):
        """Retirer vos rôles de couleur gérés par le bot"""	
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        if not self.is_enabled(interaction.guild):
            return await interaction.response.send_message("**Erreur** • Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True)
        
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("**Erreur** • Je n'ai pas la permission de gérer les rôles.", ephemeral=True)
        
        user_roles = self.get_current_member_color_role(interaction.user)
        if not user_roles:
            return await interaction.response.send_message("**Erreur** • Vous n'avez pas de rôle de couleur à retirer.", ephemeral=True)
        
        try:
            await self.remove_color_role(interaction.user)
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.response.send_message("**Erreur** • Impossible de retirer le(s) rôle(s) de couleur. Demandez à un modérateur de vérifier mes permissions.", ephemeral=True)
        
        await interaction.response.send_message("**Couleur retirée** • Vos rôles de couleur ont été retirés avec succès.", ephemeral=True)
        
    @mycolor_group.command(name='avatar')
    @app_commands.rename(user='utilisateur')
    async def avatar_mycolor(self, interaction: Interaction, user: discord.Member | None = None):
        """Choisir un rôle de couleur parmi les couleurs de l'avatar de l'utilisateur
        
        :param user: Utilisateur dont l'avatar sera utilisé (par défaut, vous)
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        if not self.is_enabled(interaction.guild):
            return await interaction.response.send_message("**Erreur** • Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True)
        
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("**Erreur** • Je n'ai pas la permission de gérer les rôles.", ephemeral=True)
        
        user = user or interaction.user
        previews = await self.draw_discord_emulation(user)
        if not previews:
            return await interaction.response.send_message("**Erreur** • Impossible de générer les prévisualisations de couleurs.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        view = AvatarPreviewSelectMenu(interaction, previews)
        await view.start()
        await view.wait()
        if not view.result:
            return await interaction.followup.send("**Annulé** • Aucune couleur n'a été sélectionnée.", ephemeral=True)
        
        color = discord.Color(int(view.result.lstrip('#'), 16))
        try:
            role = await self.give_color_role(user, color)
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.followup.send("**Erreur** • Impossible de donner le rôle de couleur. Demandez à un modérateur de vérifier mes permissions.", ephemeral=True)
        if not role:
            return await interaction.followup.send("**Erreur** • Il y a eu une erreur dans la création ou la récupération de votre rôle de couleur.", ephemeral=True)
        
        await interaction.followup.send(f"**Couleur définie** • Votre couleur de pseudo a été définie sur {role.mention}.", ephemeral=True)
        
    
    config_group = app_commands.Group(name='config-mycolor', description="Configuration des rôles de couleur", guild_only=True, default_permissions=discord.Permissions(manage_roles=True))
    
    @config_group.command(name='enable')
    @app_commands.rename(enabled='activer')
    async def enable_mycolor(self, interaction: Interaction, enabled: bool | None = None):
        """Activer ou désactiver le système de rôles de couleur sur le serveur
        
        :param enabled: Activer ou désactiver le système de rôles de couleur
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        if enabled is None:
            return await interaction.response.send_message(f"**Info** • Le système de rôles de couleur est actuellement **{'activé' if self.is_enabled(interaction.guild) else 'désactivé'}**.", ephemeral=True)
        
        self.set_enabled(interaction.guild, enabled)
        if enabled:
            await interaction.response.send_message(f"**Paramètre modifié** • Le système de rôles de couleur est maintenant **activé**\nVérifiez que mon rôle soit au dessus des rôles de couleur dans les paramètres du serveur, sans quoi je ne pourrais pas les modifier.", ephemeral=True)
        else:
            await interaction.response.send_message(f"**Paramètre modifié** • Le système de rôles de couleur est maintenant **désactivé**.", ephemeral=True)
        
    @config_group.command(name='place')
    @app_commands.rename(place='placement')
    @app_commands.choices(place=[app_commands.Choice(name='Au dessus du plus bas', value='AboveLowest'), app_commands.Choice(name='En dessous du plus haut', value='BelowHighest')])
    async def place_mycolor(self, interaction: Interaction, place: app_commands.Choice[str] | None = None):
        """Définir le placement des nouveaux rôles de couleur
        
        :param place: Placement des nouveaux rôles de couleur par rapport aux rôles existants
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        translated = {'AboveLowest': 'au dessus du plus bas', 'BelowHighest': 'en dessous du plus haut'}
        if place is None:
            return await interaction.response.send_message(f"**Info** • Les nouveaux rôles de couleur sont actuellement placés **{translated[self.get_role_placing(interaction.guild)]}** dans la liste.", ephemeral=True)
        
        self.data.get(interaction.guild).set_dict_value('settings', 'PlaceNewColorRole', place.value)
        await interaction.response.send_message(f"**Paramètre modifié** • Les nouveaux rôles de couleur seront maintenant placés **{translated[place.value]}**.", ephemeral=True)
        
    @config_group.command(name='cleanup')
    async def cleanup_mycolor(self, interaction: Interaction):
        """Lancer manuellement la réorganisation et le nettoyage des rôles de couleur"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        
        if not self.is_enabled(interaction.guild):
            return await interaction.response.send_message("**Erreur** • Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True)
        
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("**Erreur** • Je n'ai pas la permission de gérer les rôles.", ephemeral=True)
        if not interaction.guild.me.top_role.position > max(r.position for r in self.fetch_all_color_roles(interaction.guild)):
            return await interaction.response.send_message("**Erreur** • Mon rôle doit être au dessus des rôles de couleur pour pouvoir les gérer.", ephemeral=True)
        
        # Suppression des rôles de couleur sans membre
        try:
            await self.clear_color_roles(interaction.guild)
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.response.send_message("**Erreur** • Impossible de nettoyer les rôles de couleur.", ephemeral=True)
            
        # Réorganisation des rôles
        try:
            await self.reorganize_color_roles(interaction.guild)
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.response.send_message("**Erreur** • Impossible de réorganiser les rôles de couleur.", ephemeral=True)
        
        await interaction.response.send_message("**Nettoyage effectué** • Les rôles de couleur ont été réorganisés et nettoyés avec succès.", ephemeral=True)
        
    # Autocomplétion -------------------------------------------
    
    @set_mycolor.autocomplete('color')
    async def autocomplete_color(self, interaction: Interaction, current: str):
        r = fuzzy.finder(current, self.__color_names.keys(), key=lambda x: x)
        if not r:
            if re.match(r'^#?([0-9a-fA-F]{6})$', current, re.IGNORECASE):
                return [app_commands.Choice(name=f"Couleur personnalisée (#{current.replace('#', '')})", value=current)]
            return [app_commands.Choice(name=f"Couleur inconnue ou invalide", value="")]
        return [app_commands.Choice(name=f"{i} (#{self.__color_names[i]})", value=self.__color_names[i]) for i in r[:10]]

async def setup(bot):
    await bot.add_cog(Colors(bot))
