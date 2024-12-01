import copy
import logging
import re
import cv2
import textwrap
from io import BytesIO
from typing import List, Optional, Tuple

import aiohttp
import colorgram
import discord
import numpy as np
from discord import Interaction, app_commands
from discord.components import SelectOption
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageChops

from common import dataio
from common.utils import pretty

logger = logging.getLogger(f'WNDR.{__name__.split(".")[-1]}')

DEFAULT_QUOTE_IMAGE_SIZE = (512, 512)
FLUSH_AFTER = 20

# UI --------------------------------------------------------------------------

class PotentialMessageSelect(discord.ui.Select):
    def __init__(self, view, placeholder: str, options: List[discord.SelectOption]):
        super().__init__(placeholder=placeholder, 
                         min_values=1,
                         max_values=min(len(options), 10), 
                         options=options)
        self.__view = view
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.__view.selected_messages = [m for m in self.__view.potential_messages if m.id in [int(v) for v in self.values]]
        self.options = [SelectOption(label=pretty.shorten_text(m.clean_content, 95), value=str(m.id), description=f'{m.author.name} · {m.created_at.strftime('%H:%M %d/%m/%y')}', default=str(m.id) in self.values) for m in self.__view.potential_messages]
        embed = await self.__view._get_embed()
        if not embed:
            return await interaction.followup.send("**Erreur** · Impossible de créer la prévisualisation du texte", ephemeral=True)
        await interaction.edit_original_response(view=self.__view, embed=embed)

class CitationView(discord.ui.View):
    """Menu permettant de sélectionner les messages à citer et de générer une image en fonction."""
    def __init__(self, cog: 'Quotes', initial_message: discord.Message):
        super().__init__(timeout=20)
        self.__cog = cog
        self.initial_message = initial_message
        self.potential_messages = []
        self.selected_messages = [initial_message]
        
        self.interaction : Interaction | None = None
        
    # Checks -------------------------------------------------------------------
        
    async def interaction_check(self, interaction: Interaction):
        if not self.interaction:
            return False
        if interaction.user != self.interaction.user:
            await interaction.response.send_message("**Action impossible** · Seul l'auteur du message initial peut utiliser ce menu", ephemeral=True, delete_after=10)
            return False
        return True
    
    async def on_timeout(self):
        new_view = discord.ui.View()
        message_url = self.selected_messages[0].jump_url
        new_view.add_item(discord.ui.Button(label="Message d'origine", url=message_url, style=discord.ButtonStyle.link))
        if self.interaction:
            if self.interaction.message and not self.interaction.message.attachments:
                try:
                    image = await self._get_image()
                    return await self.interaction.edit_original_response(attachments=[image], view=new_view, embed=None)
                except Exception as e:
                    pass
                await self.interaction.delete_original_response()
        self.stop()
        
    # Fonctions ----------------------------------------------------------------
    
    async def _get_embed(self) -> Optional[discord.Embed]:
        """Génère une prévisualisation du texte sélectionné."""
        if not self.selected_messages:
            return None
        
        # On détermine si on a un seul auteur ou plusieurs
        authors = set([m.author for m in self.selected_messages])
        if len(authors) == 1:
            # On crée juste un embed avec l'auteur, date et texte
            content = pretty.shorten_text('\n'.join([m.clean_content for m in self.selected_messages]), 800)
            message = self.selected_messages[0]
            embed = discord.Embed(description=content, color=message.author.color)
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
            channel_name = message.channel.name if isinstance(message.channel, (discord.TextChannel, discord.Thread)) else 'MP'
            embed.set_footer(text=f"#{channel_name} • {message.created_at.strftime('%H:%m %d/%m/%y')}")
            return embed
        else:
            embed = discord.Embed(title="**Prévisualisation** · Messages sélectionnés", color=pretty.DEFAULT_EMBED_COLOR)
            regrouped_messages = {}
            current_author = None
            group = 0
            for msg in sorted(self.selected_messages, key=lambda m: m.created_at):
                if msg.author != current_author:
                    group += 1
                    current_author = msg.author
                    regrouped_messages[group] = []
                regrouped_messages[group].append(msg)
            for _, msgs in regrouped_messages.items():
                author = msgs[0].author
                content = '\n'.join([pretty.shorten_text(m.clean_content, 100) for m in msgs])
                embed.add_field(name=f"{author.display_name} ({msgs[0].created_at.strftime('%d/%m/%y')})", value=content, inline=False)
            embed.set_footer(text=f"#{msgs[0].channel.name if isinstance(msgs[0].channel, (discord.TextChannel, discord.Thread)) else 'MP'}")
            return embed
        
    async def _get_image(self) -> discord.File:
        """Génère une image à partir des messages sélectionnés."""
        try:
            return await self.__cog.generate_quote(self.selected_messages)
        except Exception as e:
            logger.exception(e, exc_info=True)
            raise ValueError("Impossible de générer l'image de citation.")

    # Déroulement -------------------------------------------------------------
    
    async def start(self, interaction: Interaction):
        await interaction.response.defer()
        
        potential_messages = await self.__cog.fetch_following_messages(self.initial_message)
        if len(potential_messages) > 1: # Si on a plus d'un message, on affiche le menu
            self.potential_messages = potential_messages
            options = [SelectOption(label=pretty.shorten_text(m.clean_content, 95), value=str(m.id), description=f'{m.author.name} · {m.created_at.strftime("%H:%M %d/%m/%y")}', default=m in self.selected_messages) for m in potential_messages]
            self.add_item(PotentialMessageSelect(self, "Sélectionnez les messages à ajouter", options))
            
        embed = await self._get_embed()
        if not embed:
            return await interaction.followup.send("**Erreur** · Impossible de créer la prévisualisation du texte", ephemeral=True)
        await interaction.followup.send(view=self, embed=embed)
        self.interaction = interaction
        
    @discord.ui.button(label="Générer l'image", style=discord.ButtonStyle.green, row=1)
    async def save_quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        new_view = discord.ui.View()
        message_url = self.initial_message.jump_url
        new_view.add_item(discord.ui.Button(label="Message d'origine", url=message_url, style=discord.ButtonStyle.link))
        
        try:
            image = await self._get_image()
            await interaction.edit_original_response(attachments=[image], view=new_view, embed=None)
        except Exception as e:
            logger.exception(e, exc_info=True)
            await interaction.followup.send("**Erreur** · Impossible de générer l'image de citation", ephemeral=True)
            
        self.stop()
        
    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.red, row=1)
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer()
        if self.interaction:
            await self.interaction.delete_original_response()

# COG -------------------------------------------------------------------------

class Quotes(commands.Cog):
    """Créateur de citations et de compilations de messages."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        
        self.__assets = {}
        self.__user_backgrounds = {}
        
        self.ctx_create_quote = app_commands.ContextMenu(
            name='Créer une citation',
            callback=self.create_quote_callback)
        self.bot.tree.add_command(self.ctx_create_quote)
        
        self.__flush_countdown = 0
        
    def cog_unload(self):
        self.data.close_all()
        
    # Gestion des ressources ---------------------------------------------------
    
    def _get_raw_asset(self, uid: str):
        """Récupère une ressource brute."""
        return self.__assets.get(uid)
    
    # Icones 
    def fetch_icon(self, name: str, size: int = 64):
        """Récupère une icone."""
        uid = f"icon_{name}_{size}"
        if uid in self.__assets:
            return self.__assets[uid]
        path = dataio.COMMON_RESOURCES_PATH / 'images' / f"{name}.png"
        if not path.exists():
            raise ValueError(f"Impossible de trouver l'icone {name}.")
        icon = Image.open(path)
        icon = icon.resize((size, size))
        self.__assets[uid] = icon
        return icon
    
    # Fonts
    def fetch_font(self, name: str, size: int):
        """Récupère une police."""
        uid = f"font_{name}_{size}"
        if uid in self.__assets:
            return self.__assets[uid]
        path = dataio.COMMON_RESOURCES_PATH / 'fonts' / f"{name}.ttf"
        if not path.exists():
            raise ValueError(f"Impossible de trouver la police {name}.")
        font = ImageFont.truetype(str(path), size)
        self.__assets[uid] = font
        return font
    
    # Backgrounds
    async def get_user_background(self, user: discord.Member, blur_radius: int):
        """Crée le fond des citations pour un utilisateur et l'enregistre en cache."""
        # Le fond est une image de 512x512 pixels avec l'avatar flouté du membre
        if f'{user.guild.id}-{user.id}' in self.__user_backgrounds:
            return self.__user_backgrounds[f'{user.guild.id}-{user.id}-{blur_radius}']
        
        avatar = BytesIO(await user.display_avatar.read())
        avatar = Image.open(avatar).convert('RGBA').resize((512, 512))
        bg = copy.copy(avatar)
        bg = cv2.GaussianBlur(np.array(bg), (blur_radius, blur_radius), 0)
        bg = Image.fromarray(bg)
        self.__user_backgrounds[f'{user.guild.id}-{user.id}-{blur_radius}'] = bg
        return bg
        
    # Génération de citations -------------------------------------------------
    
    def _normalize_text(self, text: str) -> str:
        text = re.sub(r'<a?:(\w+):\d+>', r':\1:', text)
        text = re.sub(r'(\*|_|`|~|\\)', r'', text)
        return text
    
    def _add_gradientv2(self, image: Image.Image, gradient_magnitude=1.0, color: Tuple[int, int, int]=(0, 0, 0)):
        width, height = image.size

        gradient = Image.new('RGBA', (width, height), color)
        draw = ImageDraw.Draw(gradient)

        end_alpha = int(gradient_magnitude * 255)

        for y in range(height):
            alpha = int((y / height) * end_alpha)
            draw.line([(0, y), (width, y)], fill=(color[0], color[1], color[2], alpha))

        gradient_im = Image.alpha_composite(image.convert('RGBA'), gradient)
        return gradient_im
    
    def _round_corners(self, img: Image.Image, rad: int, *,
                    top_left: bool = True, top_right: bool = True, 
                    bottom_left: bool = True, bottom_right: bool = True) -> Image.Image:
        circle = Image.new('L', (rad * 2, rad * 2), 0)
        draw = ImageDraw.Draw(circle)
        draw.ellipse((0, 0, rad * 2, rad * 2), fill=255)
        w, h = img.size
        alpha = None
        if img.mode == 'RGBA':
            alpha = img.split()[3]
        mask = Image.new('L', img.size, 255)
        if top_left:
            mask.paste(circle.crop((0, 0, rad, rad)), (0, 0))
        if top_right:
            mask.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
        if bottom_left:
            mask.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
        if bottom_right:
            mask.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
        if alpha:
            img.putalpha(ImageChops.multiply(alpha, mask))
        else:
            img.putalpha(mask)
        return img

    def _add_gradient_dir(self, image: Image.Image, gradient_magnitude=1.0, color: Tuple[int, int, int]=(0, 0, 0), direction='bottom_to_top'):
        width, height = image.size
        gradient = Image.new('RGBA', (width, height), color)
        draw = ImageDraw.Draw(gradient)
        end_alpha = int(gradient_magnitude * 255)
        if direction == 'top_to_bottom':
            for y in range(height):
                alpha = int((y / height) * end_alpha)
                draw.line([(0, y), (width, y)], fill=(color[0], color[1], color[2], alpha))
        elif direction == 'bottom_to_top':
            for y in range(height, -1, -1):
                alpha = int(((height - y) / height) * end_alpha)
                draw.line([(0, y), (width, y)], fill=(color[0], color[1], color[2], alpha))
        elif direction == 'left_to_right':
            for x in range(width):
                alpha = int((x / width) * end_alpha)
                draw.line([(x, 0), (x, height)], fill=(color[0], color[1], color[2], alpha))
        elif direction == 'right_to_left':
            for x in range(width, -1, -1):
                alpha = int(((width - x) / width) * end_alpha)
                draw.line([(x, 0), (x, height)], fill=(color[0], color[1], color[2], alpha))

        gradient_im = Image.alpha_composite(image.convert('RGBA'), gradient)
        return gradient_im
    
    async def generate_single_author_image(self, bg: Image.Image, text: str, author_name: str, channel_name: str, date: str, *, size: tuple[int, int] = (512, 512)):
        """Crée une image de citation avec un avatar, un texte, un nom d'auteur et une date."""
        text = text.upper()
        w, h = size
        box_w, _ = int(w * 0.92), int(h * 0.72)
        
        image = copy.copy(bg)
        bg_color = colorgram.extract(bg.resize((30, 30)), 1)[0].rgb
        
        luminosity = (0.2126 * bg_color[0] + 0.7152 * bg_color[1] + 0.0722 * bg_color[2]) / 255
        text_size = int(h * 0.08)
        text_font = self.fetch_font("NotoBebasNeue", text_size)
        
        draw = ImageDraw.Draw(image)
        text_color = (255, 255, 255) if luminosity < 0.5 else (0, 0, 0)

        # Texte principal --------
        max_lines = len(text) // 60 + 2 if len(text) > 200 else 4
        wrap_width = int(box_w / (text_font.getlength("A") * 0.85))
        lines = textwrap.fill(text, width=wrap_width, max_lines=max_lines, placeholder="§")
        while lines[-1] == "§":
            text_size -= 2
            text_font = self.fetch_font("NotoBebasNeue", text_size)
            wrap_width = int(box_w / (text_font.getlength("A") * 0.85))
            lines = textwrap.fill(text, width=wrap_width, max_lines=max_lines, placeholder="§")
        draw.multiline_text((w / 2, h * 0.835), lines, font=text_font, spacing=1, align='center', fill=text_color, anchor='md')

        # Icone et lignes ---------
        icon_image = self.fetch_icon('quotemark_white', int(w * 0.06)) if luminosity < 0.5 else self.fetch_icon('quotemark_black', int(w * 0.06))
        icon_left = w / 2 - icon_image.width / 2
        image.paste(icon_image, (int(icon_left), int(h * 0.85 - icon_image.height / 2)), icon_image)

        author_font = self.fetch_font("NotoBebasNeue", int(h * 0.06))
        draw.text((w / 2,  h * 0.95), author_name, font=author_font, fill=text_color, anchor='md', align='center')

        draw.line((icon_left - w * 0.25, h * 0.85, icon_left - w * 0.02, h * 0.85), fill=text_color, width=1) # Ligne de gauche
        draw.line((icon_left + icon_image.width + w * 0.02, h * 0.85, icon_left + icon_image.width + w * 0.25, h * 0.85), fill=text_color, width=1) # Ligne de droite

        # Date -------------------
        date_font = self.fetch_font("NotoBebasNeue", int(h * 0.04))
        date_text = f"#{channel_name} • {date}"
        draw.text((w / 2, h * 0.9875), date_text, font=date_font, fill=text_color, anchor='md', align='center')
        return image
    
    async def generate_multiple_authors_image(self, messages: list[discord.Message]) -> Image.Image:
        """Génère une image avec plusieurs citations."""
        width = 1000
        
        ggsans = self.fetch_font("gg_sans", 40)
        ggsans_xs = self.fetch_font("gg_sans", 24)
        ggsans_semi = self.fetch_font("gg_sans_semi", 32)
        
        # On commence par regrouper les messages par auteur
        regrouped_messages = {} # On regroupe les messages qui se suivent avec le même auteur
        current_author = None
        group = 0
        for msg in messages:
            if msg.author != current_author:
                group += 1
                current_author = msg.author
                regrouped_messages[group] = []
            regrouped_messages[group].append(msg)
                
        # On génère les images
        images = []
        total_height = 0
        for _, msgs in regrouped_messages.items():
            full_text = ''
            for msg in msgs:
                content = self._normalize_text(msg.clean_content)
                if len(content) > 50:
                    content = '\n'.join(textwrap.wrap(content, 50))
                full_text += f"{content}\n"
            full_text = full_text[:-1]
            
            # On détermine la hauteur en fonction du nombre de lignes
            base_height = 200
            height = base_height + 40 * (full_text.count('\n') - 1 if full_text.count('\n') > 0 else 0)
            # On génère l'image
            img = Image.new('RGB', (width, height), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            
            # On ajoute le fond avec cv2 (on crop l'avatar avec pillow avant)
            disp_avatar = await self.get_user_background(msgs[0].author, 115)
            disp_avatar = self._add_gradient_dir(disp_avatar, 0.9, direction='right_to_left')
            bg = copy.copy(disp_avatar)
            text_color = (255, 255, 255)
            
            bg = bg.resize((width, width))
            if bg.height > height:
                # On crop pour avoir height en hauteur (milieu de l'image)
                bg = bg.crop((0, (bg.height - height) // 2, bg.width, (bg.height - height) // 2 + height))
            elif bg.height < height:
                # On resize pour avoir height en hauteur (milieu de l'image)
                bg = bg.resize((height, height), Image.Resampling.LANCZOS)
                bg = bg.crop(((bg.width - width) // 2, 0, (bg.width - width) // 2 + width, bg.height))
            img.paste(bg, (0, 0))
            
            # On ajoute l'avatar arrondi à gauche
            
            avatar = BytesIO(await msgs[0].author.display_avatar.with_size(256).read())
            avatar = Image.open(avatar).convert('RGBA').resize((240, 240))
            avatar = self._round_corners(avatar, 30)
            avatar = avatar.resize((120, 120), Image.Resampling.LANCZOS)
            img.paste(avatar, (40, 40), avatar)
            
            # On ajoute le nom de l'auteur
            if msgs[0].author.display_name.lower() == msgs[0].author.name.lower():
                draw.text((180, 30), msgs[0].author.display_name, text_color, font=ggsans)
            else:
                draw.text((180, 30), msgs[0].author.display_name, text_color, font=ggsans)
                draw.text((180 + ggsans.getlength(msgs[0].author.display_name) + 10, 44), f"@{msgs[0].author.name}", (text_color[0], text_color[1], text_color[2], 220), font=ggsans_xs)
            
            # On ajoute le texte en dessous
            draw.multiline_text((180, 80), full_text, text_color, font=ggsans_semi)
            
            total_height += height
            images.append(img)

        # On concatène les images
        final_img = Image.new('RGBA', (width, total_height), (0, 0, 0, 0))
        y = 0
        for img in images:
            final_img.paste(img, (0, y))
            y += img.height
        
        return final_img
    
    async def fetch_following_messages(self, starting_message: discord.Message, messages_limit: int = 15) -> list[discord.Message]:
        """Ajoute au message initial les messages suivants jusqu'à atteindre la limite de messages"""
        messages = [starting_message]
        async for message in starting_message.channel.history(limit=messages_limit * 2, after=starting_message):
            if not message.content or message.content.isspace():
                continue
            messages.append(message)
            if len(messages) >= messages_limit:
                break
        return messages
    
    async def generate_quote(self, messages: list[discord.Message]) -> discord.File:
        """Génère une image de citation à partir de messages."""
        if len(set([m.author for m in messages])) == 1:
            return await self.build_quote_image(messages)
        return await self.build_multiple_quote_image(messages)
    
    async def build_quote_image(self, messages: list[discord.Message]) -> discord.File:
        messages = sorted(messages, key=lambda m: m.created_at)
        base_message = messages[0]
        if not isinstance(base_message.author, discord.Member):
            raise ValueError("Le message de base doit être envoyé par un membre du serveur.")
        
        message_date = messages[0].created_at.strftime("%d.%m.%Y")
        if isinstance(messages[0].channel, (discord.DMChannel, discord.PartialMessageable)):
            message_channel_name = 'MP'
        else:
            message_channel_name = messages[0].channel.name if messages[0].channel.name else 'Inconnu'
        bg = await self.get_user_background(base_message.author, 10)
        bg = self._add_gradient_dir(bg, 0.7, direction='top_to_bottom')
        full_content = pretty.shorten_text(' '.join([self._normalize_text(m.content) for m in messages]), 800)
        author_name = f"@{base_message.author.name}" if not base_message.author.nick else f"{base_message.author.nick} (@{base_message.author.name})"
        try:
            image = await self.generate_single_author_image(bg, full_content, author_name, message_channel_name, message_date, size=DEFAULT_QUOTE_IMAGE_SIZE)
        except Exception as e:
            logger.exception(e, exc_info=True)
            raise ValueError("Impossible de générer l'image de citation.")
        
        with BytesIO() as buffer:
            image.save(buffer, format='PNG')
            buffer.seek(0)
            alt_text = pretty.shorten_text(full_content, 800)
            alt_text = f"\"{alt_text}\" - {author_name} [#{message_channel_name} • {message_date}]"
            return discord.File(buffer, filename='quote.png', description=alt_text)
        
    async def build_multiple_quote_image(self, messages: list[discord.Message]) -> discord.File:
        messages = sorted(messages, key=lambda m: m.created_at)
        base_message = messages[0]
        if not isinstance(base_message.author, discord.Member):
            raise ValueError("Le message de base doit être envoyé par un membre du serveur.")
        authors = set([m.author for m in messages])
        try:
            image = await self.generate_multiple_authors_image(messages)
        except Exception as e:
            logger.exception(e, exc_info=True)
            raise ValueError("Impossible de générer l'image de citation.")
        
        with BytesIO() as buffer:
            image.save(buffer, format='PNG')
            buffer.seek(0)
            return discord.File(buffer, filename='multiple_quote.png', description=f"Compilation de messages de {len(authors)} auteurs")
    
    # COMMANDES =================================================================
    
    @app_commands.command(name='quote')
    @app_commands.checks.cooldown(1, 600)
    async def fetch_inspirobot_quote(self, interaction: Interaction):
        """Obtenir une citation aléatoire de Inspirobot.me"""
        await interaction.response.defer()
        
        async def get_inspirobot_quote():
            async with aiohttp.ClientSession() as session:
                async with session.get('https://inspirobot.me/api?generate=true') as resp:
                    if resp.status != 200:
                        return None
                    return await resp.text()
                
        url = await get_inspirobot_quote()
        if url is None:
            return await interaction.followup.send("**Erreur** • Impossible d'obtenir une citation depuis Inspirobot.me.", ephemeral=True)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await interaction.followup.send("**Erreur** • Impossible d'obtenir une citation depuis Inspirobot.me.", ephemeral=True)
                data = BytesIO(await resp.read())
        
        await interaction.followup.send(file=discord.File(data, 'quote.png', description="Citation fournie par Inspirobot.me"))
        
    # Callback ------------------------------------------------------------------
    
    async def create_quote_callback(self, interaction: Interaction, message: discord.Message):
        """Callback pour la commande de génération de citation"""
        if not message.content or message.content.isspace():
            return await interaction.response.send_message("**Action impossible** · Le message est vide", ephemeral=True)
        if interaction.channel_id != message.channel.id:
            return await interaction.response.send_message("**Action impossible** · Le message doit être dans le même salon", ephemeral=True)
        try:
            view = CitationView(self, message)
            await view.start(interaction)
        except Exception as e:
            logger.exception(e)
            await interaction.followup.send(f"**Erreur dans l'initialisation du menu** · `{e}`", ephemeral=True)
            
        self.__flush_countdown -= 1
        if self.__flush_countdown <= 0:
            self.__user_backgrounds = {}
            self.__flush_countdown = FLUSH_AFTER

async def setup(bot):
    await bot.add_cog(Quotes(bot))
