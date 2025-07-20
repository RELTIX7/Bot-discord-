
import discord
from discord.ext import commands, tasks
import sqlite3
import json
import asyncio
import random
import datetime
import re
import aiohttp
import os
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import urllib.parse
import re
from collections import deque

# Configuration du bot
intents = discord.Intents.default()
intents.message_content = True  # Nécessaire pour lire le contenu des messages
intents.members = True  # Nécessaire pour les événements de membres (optionnel)
bot = commands.Bot(command_prefix='!', intents=intents)

# Configuration pour la musique
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# Configuration Spotify (optionnel)
spotify_client_id = os.getenv('SPOTIFY_CLIENT_ID')
spotify_client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')

if spotify_client_id and spotify_client_secret:
    spotify = spotipy.Spotify(client_credentials_manager=SpotifyClientCredentials(
        client_id=spotify_client_id,
        client_secret=spotify_client_secret
    ))
else:
    spotify = None

# Variables globales pour la musique
music_queues = {}
voice_clients = {}

# Base de données SQLite
def init_db():
    conn = sqlite3.connect('bot_data.db', timeout=20.0)
    cursor = conn.cursor()
    
    # Table pour les niveaux
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_levels (
            user_id INTEGER,
            guild_id INTEGER,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 0,
            messages INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, guild_id)
        )
    ''')
    
    # Table pour les avertissements
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            timestamp TEXT
        )
    ''')
    
    # Table pour la configuration des serveurs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel INTEGER,
            goodbye_channel INTEGER,
            log_channel INTEGER,
            mute_role INTEGER,
            auto_role INTEGER,
            level_up_channel INTEGER,
            log_mention_user INTEGER
        )
    ''')
    
    # Ajouter la colonne log_mention_user si elle n'existe pas
    try:
        cursor.execute('ALTER TABLE guild_config ADD COLUMN log_mention_user INTEGER')
    except sqlite3.OperationalError:
        pass  # La colonne existe déjà
    
    # Table pour les rôles automatiques par niveau
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS level_roles (
            guild_id INTEGER,
            level INTEGER,
            role_id INTEGER,
            PRIMARY KEY (guild_id, level)
        )
    ''')
    
    conn.commit()
    conn.close()

# Initialiser la base de données
init_db()

# === CLASSES POUR LA MUSIQUE ===
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        
        if 'entries' in data:
            data = data['entries'][0]
        
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

class MusicPlayer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.queue = deque()
        self.current = None
        self.voice_client = None
        self.is_playing = False
        self.volume = 0.5

    async def add_to_queue(self, source):
        self.queue.append(source)
        if not self.is_playing:
            await self.play_next()

    async def play_next(self):
        if len(self.queue) == 0:
            self.is_playing = False
            return

        self.is_playing = True
        self.current = self.queue.popleft()
        
        if self.voice_client:
            self.voice_client.play(self.current, after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop))

def get_spotify_track_info(track_url):
    """Extrait les informations d'une piste Spotify"""
    if not spotify:
        return None
    
    try:
        track_id = track_url.split('/')[-1].split('?')[0]
        track = spotify.track(track_id)
        search_query = f"{track['artists'][0]['name']} {track['name']}"
        return search_query
    except:
        return None

@bot.event
async def on_ready():
    print(f'{bot.user} est connecté et prêt !')
    activity = discord.Game(name="!help | Bot Discord Français")
    await bot.change_presence(activity=activity)

# === SYSTÈME DE NIVEAUX ===
def calculate_level(xp):
    return int((xp / 100) ** 0.5)

def xp_for_level(level):
    return (level ** 2) * 100

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    # Liste des gros mots à détecter
    bad_words = [
        'pute', 'salope', 'con', 'merde', 'ptn', 'connard', 'connasse', 'fdp', 'fils de pute',
        'batard', 'bâtard', 'enculé', 'encule', 'putain', 'bordel', 'chier', 'merdique',
        'connerie', 'conneries', 'salaud', 'salopard', 'bite', 'couille', 'couilles',
        'cul', 'conne', 'débile', 'crétin', 'abruti', 'idiot', 'imbécile', 'nique',
        'niquer', 'baise', 'baiser', 'salop', 'merdier', 'emmerde', 'emmerder'
    ]
    
    # Vérifier si le message contient des gros mots
    message_lower = message.content.lower()
    contains_bad_word = any(bad_word in message_lower for bad_word in bad_words)
    
    if contains_bad_word and not message.content.startswith('!'):
        # Récupérer le rôle mute configuré
        try:
            conn = sqlite3.connect('bot_data.db', timeout=20.0)
            cursor = conn.cursor()
            cursor.execute('SELECT mute_role FROM guild_config WHERE guild_id = ?', (message.guild.id,))
            config = cursor.fetchone()
            conn.close()
        except sqlite3.OperationalError:
            print("❌ Erreur d'accès à la base de données pour la détection de gros mots")
            return
        
        if config and config[0]:
            mute_role = message.guild.get_role(config[0])
            if mute_role:
                try:
                    # Supprimer le message offensant
                    await message.delete()
                    
                    # Envoyer le GIF
                    gif_url = "https://cdn.discordapp.com/attachments/1225455414934634649/1379151581878026370/caption_2.gif?ex=68617980&is=68602800&hm=e1dec0735b3e8557caa9eec33b57b980854e29d4555a76d0021704c5ddfb551e&"
                    
                    embed = discord.Embed(
                        title="🚫 Langage inapproprié détecté !",
                        description=f"{message.author.mention} a utilisé un langage inapproprié et a été mute 10 minutes.",
                        color=0xff0000
                    )
                    embed.set_image(url=gif_url)
                    embed.set_footer(text="Respectez les règles du serveur !")
                    
                    await message.channel.send(embed=embed)
                    
                    # Muter la personne pour 10 minutes
                    await message.author.add_roles(mute_role, reason="Langage inapproprié - mute automatique 10min")
                    
                    # Log de l'action
                    await log_moderation_action(message.guild, "AUTO_MUTE", None, message.author, 
                                              f"Langage inapproprié détecté: {message.content[:50]}...", 
                                              "10 minutes", "Mute automatique par le système")
                    
                    # Démute automatique après 10 minutes
                    await asyncio.sleep(600)  # 10 minutes = 600 secondes
                    if mute_role in message.author.roles:
                        await message.author.remove_roles(mute_role, reason="Fin du mute automatique (langage inapproprié)")
                        
                        # Notification de fin de mute
                        embed_unmute = discord.Embed(
                            title="🔊 Mute automatique expiré",
                            description=f"Le mute de {message.author.mention} pour langage inapproprié a expiré.",
                            color=0x00ff00
                        )
                        await message.channel.send(embed=embed_unmute)
                        
                        await log_action(message.guild, "AUTO_UNMUTE", None, message.author,
                                       "Fin du mute automatique pour langage inapproprié")
                
                except Exception as e:
                    print(f"Erreur lors du mute automatique: {e}")
        
        return  # Ne pas traiter l'XP pour les messages avec gros mots
    
    # Système XP
    if not message.content.startswith('!'):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect('bot_data.db', timeout=20.0)
                cursor = conn.cursor()
                break
            except sqlite3.OperationalError as e:
                if attempt == max_retries - 1:
                    print(f"❌ Erreur base de données après {max_retries} tentatives: {e}")
                    return
                await asyncio.sleep(0.5)
        
        user_id = message.author.id
        guild_id = message.guild.id
        
        # Récupérer les données actuelles
        cursor.execute('SELECT xp, level, messages FROM user_levels WHERE user_id = ? AND guild_id = ?', 
                      (user_id, guild_id))
        result = cursor.fetchone()
        
        if result:
            current_xp, current_level, messages = result
            new_xp = current_xp + random.randint(15, 25)
            new_messages = messages + 1
        else:
            new_xp = random.randint(15, 25)
            current_level = 0
            new_messages = 1
        
        new_level = calculate_level(new_xp)
        
        # Mettre à jour la base de données
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO user_levels (user_id, guild_id, xp, level, messages)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, guild_id, new_xp, new_level, new_messages))
            
            # Level up
            if new_level > current_level:
                cursor.execute('SELECT level_up_channel FROM guild_config WHERE guild_id = ?', (guild_id,))
                config = cursor.fetchone()
                
                if config and config[0]:
                    channel = bot.get_channel(config[0])
                else:
                    channel = message.channel
                
                embed = discord.Embed(
                    title="🎉 LEVEL UP !",
                    description=f"{message.author.mention} vient d'atteindre le niveau **{new_level}** !",
                    color=0x00ff00
                )
                embed.set_thumbnail(url=message.author.avatar.url if message.author.avatar else None)
                await channel.send(embed=embed)
                
                # Log du level up
                await log_action(message.guild, "LEVEL_UP", message.author, None,
                                f"Niveau {new_level} atteint !",
                                f"XP total: {new_xp}")
                
                # Vérifier les rôles de niveau
                cursor.execute('SELECT role_id FROM level_roles WHERE guild_id = ? AND level = ?', 
                              (guild_id, new_level))
                role_result = cursor.fetchone()
                if role_result:
                    role = message.guild.get_role(role_result[0])
                    if role:
                        await message.author.add_roles(role)
                        await log_action(message.guild, "ROLE_ADD", None, message.author,
                                        f"Rôle de niveau ajouté: {role.name}",
                                        f"Niveau requis: {new_level}")
            
            conn.commit()
        except sqlite3.OperationalError as e:
            print(f"❌ Erreur lors de la mise à jour XP: {e}")
        finally:
            if 'conn' in locals():
                conn.close()
    
    await bot.process_commands(message)

# === COMMANDES DE NIVEAUX ===
@bot.command(name='rank')
async def rank(ctx, member: discord.Member = None):
    """Affiche le niveau d'un utilisateur"""
    if member is None:
        member = ctx.author
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT xp, level, messages FROM user_levels WHERE user_id = ? AND guild_id = ?', 
                  (member.id, ctx.guild.id))
    result = cursor.fetchone()
    
    if not result:
        embed = discord.Embed(
            title="❌ Erreur",
            description=f"{member.mention} n'a pas encore d'XP !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    xp, level, messages = result
    xp_needed = xp_for_level(level + 1) - xp
    
    embed = discord.Embed(
        title=f"📊 Niveau de {member.display_name}",
        color=0x3498db
    )
    embed.add_field(name="Niveau", value=f"**{level}**", inline=True)
    embed.add_field(name="XP", value=f"**{xp}**", inline=True)
    embed.add_field(name="Messages", value=f"**{messages}**", inline=True)
    embed.add_field(name="XP pour le niveau suivant", value=f"**{xp_needed}**", inline=False)
    embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
    
    conn.close()
    await ctx.send(embed=embed)

@bot.command(name='leaderboard', aliases=['lb', 'top'])
async def leaderboard(ctx):
    """Affiche le classement du serveur"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT user_id, xp, level FROM user_levels 
        WHERE guild_id = ? 
        ORDER BY xp DESC 
        LIMIT 10
    ''', (ctx.guild.id,))
    
    results = cursor.fetchall()
    
    if not results:
        embed = discord.Embed(
            title="❌ Classement vide",
            description="Aucun utilisateur n'a encore d'XP !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="🏆 Classement du serveur",
        color=0xffd700
    )
    
    for i, (user_id, xp, level) in enumerate(results, 1):
        user = ctx.guild.get_member(user_id)
        if user:
            medals = ["🥇", "🥈", "🥉"]
            medal = medals[i-1] if i <= 3 else f"**{i}.**"
            embed.add_field(
                name=f"{medal} {user.display_name}",
                value=f"Niveau {level} - {xp} XP",
                inline=False
            )
    
    conn.close()
    await ctx.send(embed=embed)

# === MODÉRATION ===
@bot.command(name='kick')
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="Aucune raison spécifiée"):
    """Expulse un membre"""
    try:
        await member.kick(reason=reason)
        embed = discord.Embed(
            title="👢 Membre expulsé",
            description=f"{member.mention} a été expulsé.\n**Raison:** {reason}",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        
        # Log détaillé
        await log_moderation_action(ctx.guild, "KICK", ctx.author, member, reason)
    except Exception as e:
        await ctx.send(f"❌ Erreur lors de l'expulsion : {e}")

@bot.command(name='ban')
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="Aucune raison spécifiée"):
    """Bannit un membre"""
    try:
        await member.ban(reason=reason)
        embed = discord.Embed(
            title="🔨 Membre banni",
            description=f"{member.mention} a été banni.\n**Raison:** {reason}",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        
        # Log détaillé
        await log_moderation_action(ctx.guild, "BAN", ctx.author, member, reason)
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du bannissement : {e}")

@bot.command(name='tempban')
@commands.has_permissions(ban_members=True)
async def tempban(ctx, member: discord.Member, duration: str, *, reason="Aucune raison spécifiée"):
    """Bannit temporairement un membre (1h, 1d, 1w)"""
    time_units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    
    # Parser la durée
    try:
        if duration[-1] in time_units:
            duration_seconds = int(duration[:-1]) * time_units[duration[-1]]
            duration_text = duration
        else:
            duration_seconds = int(duration) * 60  # Par défaut en minutes
            duration_text = f"{duration} minute(s)"
    except:
        await ctx.send("❌ Format de durée invalide ! Utilisez: 1h, 30m, 1d, etc.")
        return
    
    try:
        await member.ban(reason=f"Bannissement temporaire: {reason}")
        embed = discord.Embed(
            title="⏰ Bannissement temporaire",
            description=f"{member.mention} a été banni temporairement pour {duration_text}.\n**Raison:** {reason}",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        
        # Débannir automatiquement après la durée
        await asyncio.sleep(duration_seconds)
        try:
            await ctx.guild.unban(member)
            embed = discord.Embed(
                title="✅ Bannissement expiré",
                description=f"Le bannissement temporaire de **{member.name}** a expiré.",
                color=0x00ff00
            )
            await ctx.send(embed=embed)
        except:
            pass
        
        await log_moderation_action(ctx.guild, "TEMPBAN", ctx.author, member, reason, duration_text)
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du bannissement temporaire : {e}")

@bot.command(name='tempmute')
@commands.has_permissions(manage_roles=True)
async def tempmute(ctx, member: discord.Member, duration: str, *, reason="Aucune raison spécifiée"):
    """Rend muet temporairement un membre"""
    time_units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    
    # Parser la durée
    try:
        if duration[-1] in time_units:
            duration_seconds = int(duration[:-1]) * time_units[duration[-1]]
            duration_text = duration
        else:
            duration_seconds = int(duration) * 60  # Par défaut en minutes
            duration_text = f"{duration} minute(s)"
    except:
        await ctx.send("❌ Format de durée invalide ! Utilisez: 1h, 30m, 1d, etc.")
        return
    
    # Récupérer le rôle mute
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT mute_role FROM guild_config WHERE guild_id = ?', (ctx.guild.id,))
    config = cursor.fetchone()
    conn.close()
    
    if not config or not config[0]:
        await ctx.send("❌ Aucun rôle mute configuré !")
        return
    
    mute_role = ctx.guild.get_role(config[0])
    if not mute_role:
        await ctx.send("❌ Le rôle mute n'existe plus !")
        return
    
    try:
        await member.add_roles(mute_role, reason=reason)
        embed = discord.Embed(
            title="🔇 Mute temporaire",
            description=f"{member.mention} a été rendu muet pour {duration_text}.\n**Raison:** {reason}",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        
        # Démute automatique
        await asyncio.sleep(duration_seconds)
        if mute_role in member.roles:
            await member.remove_roles(mute_role, reason="Fin du mute temporaire")
            embed = discord.Embed(
                title="🔊 Mute expiré",
                description=f"Le mute temporaire de {member.mention} a expiré.",
                color=0x00ff00
            )
            await ctx.send(embed=embed)
        
        await log_moderation_action(ctx.guild, "TEMPMUTE", ctx.author, member, reason, duration_text)
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du mute temporaire : {e}")

@bot.command(name='search')
async def search_music(ctx, *, query):
    """Recherche une chanson sur YouTube"""
    try:
        embed = discord.Embed(
            title="🔍 Recherche en cours...",
            description=f"Recherche de : **{query}**",
            color=0xffff00
        )
        search_msg = await ctx.send(embed=embed)
        
        # Rechercher avec yt-dlp
        search_results = ytdl.extract_info(f"ytsearch5:{query}", download=False)
        
        if not search_results or 'entries' not in search_results:
            embed = discord.Embed(
                title="❌ Aucun résultat",
                description="Aucune chanson trouvée pour cette recherche.",
                color=0xff0000
            )
            await search_msg.edit(embed=embed)
            return
        
        embed = discord.Embed(
            title="🎵 Résultats de recherche",
            description=f"Résultats pour : **{query}**",
            color=0x3498db
        )
        
        for i, entry in enumerate(search_results['entries'][:5], 1):
            duration = str(datetime.timedelta(seconds=entry.get('duration', 0))) if entry.get('duration') else "Inconnue"
            embed.add_field(
                name=f"{i}. {entry['title'][:50]}...",
                value=f"**Durée:** {duration}\n**Chaîne:** {entry.get('uploader', 'Inconnue')[:30]}",
                inline=False
            )
        
        embed.set_footer(text="Utilisez !play <titre> pour jouer une chanson")
        await search_msg.edit(embed=embed)
        
    except Exception as e:
        embed = discord.Embed(
            title="❌ Erreur de recherche",
            description=f"Erreur : {str(e)}",
            color=0xff0000
        )
        await ctx.send(embed=embed)

@bot.command(name='add')
async def add_to_queue(ctx, *, query):
    """Ajoute une chanson à la file d'attente sans la jouer immédiatement"""
    if not ctx.author.voice:
        await ctx.send("❌ Vous devez être dans un salon vocal !")
        return
    
    if ctx.guild.id not in voice_clients:
        await join_voice(ctx)
    
    # Vérifier si c'est un lien Spotify
    if 'spotify.com' in query:
        if spotify:
            query = get_spotify_track_info(query)
            if not query:
                await ctx.send("❌ Impossible de récupérer les informations Spotify !")
                return
        else:
            await ctx.send("❌ Les identifiants Spotify ne sont pas configurés !")
            return
    
    try:
        source = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
        music_queues[ctx.guild.id].queue.append(source)
        
        embed = discord.Embed(
            title="➕ Ajouté à la file",
            description=f"**{source.title}** ajouté à la file d'attente\n**Position:** {len(music_queues[ctx.guild.id].queue)}",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Erreur lors de l'ajout : {e}")

@bot.command(name='clear-queue', aliases=['clearqueue'])
async def clear_queue(ctx):
    """Vide la file d'attente"""
    if ctx.guild.id not in music_queues:
        await ctx.send("❌ Aucune file d'attente active !")
        return
    
    queue_size = len(music_queues[ctx.guild.id].queue)
    music_queues[ctx.guild.id].queue.clear()
    
    embed = discord.Embed(
        title="🗑️ File d'attente vidée",
        description=f"{queue_size} chanson(s) supprimée(s) de la file d'attente",
        color=0xff9900
    )
    await ctx.send(embed=embed)

@bot.command(name='start-quiz', aliases=['startquiz'])
async def start_quiz(ctx):
    """Démarre un blind test musical"""
    embed = discord.Embed(
        title="🎵 Blind Test",
        description="Fonctionnalité en développement ! Bientôt disponible.",
        color=0xffff00
    )
    await ctx.send(embed=embed)

@bot.command(name='stop-quiz', aliases=['stopquiz'])
async def stop_quiz(ctx):
    """Arrête le blind test"""
    embed = discord.Embed(
        title="⏹️ Blind Test",
        description="Aucun blind test en cours.",
        color=0xff9900
    )
    await ctx.send(embed=embed)

@bot.command(name='warn')
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason="Aucune raison spécifiée"):
    """Donne un avertissement à un membre"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    timestamp = datetime.datetime.now().isoformat()
    cursor.execute('''
        INSERT INTO warnings (user_id, guild_id, moderator_id, reason, timestamp)
        VALUES (?, ?, ?, ?, ?)
    ''', (member.id, ctx.guild.id, ctx.author.id, reason, timestamp))
    
    # Compter les avertissements
    cursor.execute('SELECT COUNT(*) FROM warnings WHERE user_id = ? AND guild_id = ?', 
                  (member.id, ctx.guild.id))
    warn_count = cursor.fetchone()[0]
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="⚠️ Avertissement donné",
        description=f"{member.mention} a reçu un avertissement.\n**Raison:** {reason}\n**Total:** {warn_count} avertissement(s)",
        color=0xffff00
    )
    await ctx.send(embed=embed)
    
    # Log détaillé
    await log_moderation_action(ctx.guild, "WARN", ctx.author, member, reason, additional_info=f"Total d'avertissements: {warn_count}")
    
    # Message privé au membre
    try:
        dm_embed = discord.Embed(
            title="⚠️ Vous avez reçu un avertissement",
            description=f"**Serveur:** {ctx.guild.name}\n**Raison:** {reason}\n**Modérateur:** {ctx.author}",
            color=0xffff00
        )
        await member.send(embed=dm_embed)
    except:
        pass

@bot.command(name='warnings')
@commands.has_permissions(manage_messages=True)
async def warnings(ctx, member: discord.Member):
    """Affiche les avertissements d'un membre"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT reason, timestamp, moderator_id FROM warnings 
        WHERE user_id = ? AND guild_id = ?
        ORDER BY timestamp DESC
    ''', (member.id, ctx.guild.id))
    
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        embed = discord.Embed(
            title="✅ Aucun avertissement",
            description=f"{member.mention} n'a aucun avertissement.",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title=f"⚠️ Avertissements de {member.display_name}",
        description=f"**Total:** {len(results)} avertissement(s)",
        color=0xffff00
    )
    
    for i, (reason, timestamp, mod_id) in enumerate(results[:10], 1):
        moderator = ctx.guild.get_member(mod_id)
        mod_name = moderator.display_name if moderator else "Modérateur inconnu"
        date = datetime.datetime.fromisoformat(timestamp).strftime("%d/%m/%Y %H:%M")
        
        embed.add_field(
            name=f"Avertissement #{i}",
            value=f"**Raison:** {reason}\n**Modérateur:** {mod_name}\n**Date:** {date}",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.command(name='clear', aliases=['purge'])
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 10):
    """Supprime des messages"""
    if amount > 100:
        await ctx.send("❌ Vous ne pouvez pas supprimer plus de 100 messages à la fois.")
        return
    
    deleted = await ctx.channel.purge(limit=amount + 1)
    
    embed = discord.Embed(
        title="🗑️ Messages supprimés",
        description=f"{len(deleted) - 1} message(s) supprimé(s).",
        color=0x00ff00
    )
    msg = await ctx.send(embed=embed)
    await asyncio.sleep(3)
    await msg.delete()

# === SYSTÈME DE BIENVENUE ===
@bot.event
async def on_member_join(member):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT welcome_channel, auto_role FROM guild_config WHERE guild_id = ?', 
                  (member.guild.id,))
    config = cursor.fetchone()
    
    if config:
        welcome_channel_id, auto_role_id = config
        
        # Message de bienvenue
        if welcome_channel_id:
            channel = member.guild.get_channel(welcome_channel_id)
            if channel:
                embed = discord.Embed(
                    title="👋 Bienvenue !",
                    description=f"Bienvenue {member.mention} sur **{member.guild.name}** !\nNous sommes maintenant **{member.guild.member_count}** membres !",
                    color=0x00ff00
                )
                embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
                await channel.send(embed=embed)
        
        # Rôle automatique
        if auto_role_id:
            role = member.guild.get_role(auto_role_id)
            if role:
                await member.add_roles(role)
    
    conn.close()

@bot.event
async def on_member_remove(member):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT goodbye_channel FROM guild_config WHERE guild_id = ?', 
                  (member.guild.id,))
    config = cursor.fetchone()
    
    if config and config[0]:
        channel = member.guild.get_channel(config[0])
        if channel:
            embed = discord.Embed(
                title="👋 Au revoir !",
                description=f"**{member.display_name}** a quitté le serveur.\nNous sommes maintenant **{member.guild.member_count}** membres.",
                color=0xff0000
            )
            await channel.send(embed=embed)
    
    conn.close()

# === CONFIGURATION ===
@bot.command(name='setup')
@commands.has_permissions(administrator=True)
async def setup(ctx):
    """Configuration initiale du bot"""
    embed = discord.Embed(
        title="⚙️ Configuration du bot",
        description="Utilisez les commandes suivantes pour configurer le bot :",
        color=0x3498db
    )
    embed.add_field(name="!set welcome <#channel>", value="Définir le salon de bienvenue", inline=False)
    embed.add_field(name="!set goodbye <#channel>", value="Définir le salon d'au revoir", inline=False)
    embed.add_field(name="!set logs <#channel>", value="Définir le salon de logs", inline=False)
    embed.add_field(name="!set levelup <#channel>", value="Définir le salon des level up", inline=False)
    embed.add_field(name="!set autorole <@role>", value="Définir le rôle automatique", inline=False)
    embed.add_field(name="!levelrole add <niveau> <@role>", value="Ajouter un rôle de niveau", inline=False)
    
    await ctx.send(embed=embed)

@bot.group(name='set')
@commands.has_permissions(administrator=True)
async def set_config(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("❌ Veuillez spécifier une option de configuration. Utilisez `!setup` pour voir les options.")

@set_config.command(name='welcome')
async def set_welcome(ctx, channel: discord.TextChannel):
    """Définit le salon de bienvenue"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config (guild_id, welcome_channel)
        VALUES (?, ?)
    ''', (ctx.guild.id, channel.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Salon de bienvenue défini sur {channel.mention}",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@set_config.command(name='goodbye')
async def set_goodbye(ctx, channel: discord.TextChannel):
    """Définit le salon d'au revoir"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config (guild_id, goodbye_channel)
        VALUES (?, ?)
    ''', (ctx.guild.id, channel.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Salon d'au revoir défini sur {channel.mention}",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@set_config.command(name='logs')
async def set_logs(ctx, channel: discord.TextChannel):
    """Définit le salon de logs"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config (guild_id, log_channel)
        VALUES (?, ?)
    ''', (ctx.guild.id, channel.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Salon de logs défini sur {channel.mention}",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@set_config.command(name='levelup')
async def set_levelup(ctx, channel: discord.TextChannel):
    """Définit le salon des level up"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config (guild_id, level_up_channel)
        VALUES (?, ?)
    ''', (ctx.guild.id, channel.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Salon de level up défini sur {channel.mention}",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@set_config.command(name='autorole')
async def set_autorole(ctx, role: discord.Role):
    """Définit le rôle automatique"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config (guild_id, auto_role)
        VALUES (?, ?)
    ''', (ctx.guild.id, role.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Rôle automatique défini sur {role.mention}",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

# === SYSTÈME DE LOGS COMPLET ===
async def log_action(guild, action, moderator, target, reason, additional_info=None):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT log_channel, log_mention_user FROM guild_config WHERE guild_id = ?', (guild.id,))
    config = cursor.fetchone()
    
    if config and config[0]:
        channel = guild.get_channel(config[0])
        mention_user_id = config[1] if len(config) > 1 else None
        
        if channel:
            # Couleurs selon le type d'action
            colors = {
                'JOIN': 0x00ff00,
                'LEAVE': 0xff0000,
                'KICK': 0xff9900,
                'BAN': 0xff0000,
                'UNBAN': 0x00ff00,
                'MUTE': 0xff9900,
                'UNMUTE': 0x00ff00,
                'WARN': 0xffff00,
                'MESSAGE_DELETE': 0xff6666,
                'MESSAGE_EDIT': 0xffaa00,
                'ROLE_ADD': 0x00ffaa,
                'ROLE_REMOVE': 0xffaa66,
                'LEVEL_UP': 0x00ff00,
                'VOICE_JOIN': 0x00aaff,
                'VOICE_LEAVE': 0xaa66ff,
                'NICKNAME_CHANGE': 0xaaaaaa
            }
            
            embed = discord.Embed(
                title=f"📋 {action}",
                color=colors.get(action, 0x3498db),
                timestamp=datetime.datetime.now()
            )
            
            if moderator:
                embed.add_field(name="Utilisateur/Modérateur", value=moderator.mention if hasattr(moderator, 'mention') else str(moderator), inline=True)
            
            if target:
                embed.add_field(name="Cible", value=target.mention if hasattr(target, 'mention') else str(target), inline=True)
            
            if reason:
                embed.add_field(name="Détails", value=reason, inline=False)
            
            if additional_info:
                embed.add_field(name="Informations supplémentaires", value=additional_info, inline=False)
            
            embed.set_footer(text=f"ID: {target.id if hasattr(target, 'id') else (moderator.id if hasattr(moderator, 'id') else 'N/A')}")
            
            try:
                # Préparer le message avec mention si configurée
                content = ""
                if mention_user_id:
                    mention_user = guild.get_member(mention_user_id)
                    if mention_user:
                        content = mention_user.mention
                
                await channel.send(content=content, embed=embed)
            except:
                pass  # Ignorer les erreurs de logs
    
    conn.close()

# Événements de logs automatiques
@bot.event
async def on_member_join(member):
    # Message de bienvenue (code existant)
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT welcome_channel, auto_role FROM guild_config WHERE guild_id = ?', 
                  (member.guild.id,))
    config = cursor.fetchone()
    
    if config:
        welcome_channel_id, auto_role_id = config
        
        # Message de bienvenue
        if welcome_channel_id:
            channel = member.guild.get_channel(welcome_channel_id)
            if channel:
                embed = discord.Embed(
                    title="👋 Bienvenue !",
                    description=f"Bienvenue {member.mention} sur **{member.guild.name}** !\nNous sommes maintenant **{member.guild.member_count}** membres !",
                    color=0x00ff00
                )
                embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
                embed.add_field(name="Compte créé le", value=member.created_at.strftime("%d/%m/%Y"), inline=True)
                embed.add_field(name="ID", value=member.id, inline=True)
                await channel.send(embed=embed)
        
        # Rôle automatique
        if auto_role_id:
            role = member.guild.get_role(auto_role_id)
            if role:
                await member.add_roles(role)
                await log_action(member.guild, "ROLE_ADD", None, member, f"Rôle automatique : {role.name}")
    
    conn.close()
    
    # Log de l'arrivée
    await log_action(member.guild, "JOIN", None, member, 
                    f"Nouveau membre rejoint le serveur", 
                    f"Compte créé: {member.created_at.strftime('%d/%m/%Y')}")

@bot.event
async def on_member_remove(member):
    # Message d'au revoir (code existant)
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT goodbye_channel FROM guild_config WHERE guild_id = ?', 
                  (member.guild.id,))
    config = cursor.fetchone()
    
    if config and config[0]:
        channel = member.guild.get_channel(config[0])
        if channel:
            embed = discord.Embed(
                title="👋 Au revoir !",
                description=f"**{member.display_name}** a quitté le serveur.\nNous sommes maintenant **{member.guild.member_count}** membres.",
                color=0xff0000
            )
            await channel.send(embed=embed)
    
    conn.close()
    
    # Log du départ avec correction de la date
    if member.joined_at:
        # Convertir en datetime naive pour éviter l'erreur
        now = datetime.datetime.now()
        joined_at_naive = member.joined_at.replace(tzinfo=None)
        days_on_server = (now - joined_at_naive).days
        server_time = f"{days_on_server} jours"
    else:
        server_time = "Inconnu"
    
    await log_action(member.guild, "LEAVE", None, member, 
                    f"Membre a quitté le serveur",
                    f"Était sur le serveur depuis: {server_time}")

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    
    content = message.content[:100] + "..." if len(message.content) > 100 else message.content
    await log_action(message.guild, "MESSAGE_DELETE", message.author, None,
                    f"Message supprimé dans {message.channel.mention}",
                    f"Contenu: {content}")

@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content:
        return
    
    before_content = before.content[:50] + "..." if len(before.content) > 50 else before.content
    after_content = after.content[:50] + "..." if len(after.content) > 50 else after.content
    
    await log_action(before.guild, "MESSAGE_EDIT", before.author, None,
                    f"Message modifié dans {before.channel.mention}",
                    f"Avant: {before_content}\nAprès: {after_content}")

@bot.event
async def on_member_update(before, after):
    # Changement de pseudo
    if before.display_name != after.display_name:
        await log_action(after.guild, "NICKNAME_CHANGE", after, None,
                        f"Pseudo changé",
                        f"Ancien: {before.display_name}\nNouveau: {after.display_name}")
    
    # Changement de rôles
    added_roles = set(after.roles) - set(before.roles)
    removed_roles = set(before.roles) - set(after.roles)
    
    for role in added_roles:
        if role.name != "@everyone":
            await log_action(after.guild, "ROLE_ADD", None, after,
                            f"Rôle ajouté: {role.name}")
    
    for role in removed_roles:
        if role.name != "@everyone":
            await log_action(after.guild, "ROLE_REMOVE", None, after,
                            f"Rôle retiré: {role.name}")

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel != after.channel:
        if before.channel is None and after.channel:
            # Rejoint un salon vocal
            await log_action(member.guild, "VOICE_JOIN", member, None,
                            f"A rejoint le salon vocal: {after.channel.name}")
        elif before.channel and after.channel is None:
            # Quitte un salon vocal
            await log_action(member.guild, "VOICE_LEAVE", member, None,
                            f"A quitté le salon vocal: {before.channel.name}")
        elif before.channel and after.channel:
            # Change de salon vocal
            await log_action(member.guild, "VOICE_MOVE", member, None,
                            f"Salon vocal changé",
                            f"De: {before.channel.name}\nVers: {after.channel.name}")

@bot.event
async def on_guild_channel_create(channel):
    await log_action(channel.guild, "CHANNEL_CREATE", None, None,
                    f"Nouveau salon créé: {channel.name}",
                    f"Type: {channel.type}")

@bot.event
async def on_guild_channel_delete(channel):
    await log_action(channel.guild, "CHANNEL_DELETE", None, None,
                    f"Salon supprimé: {channel.name}",
                    f"Type: {channel.type}")

@bot.event
async def on_guild_role_create(role):
    await log_action(role.guild, "ROLE_CREATE", None, None,
                    f"Nouveau rôle créé: {role.name}",
                    f"Couleur: {role.color}")

@bot.event
async def on_guild_role_delete(role):
    await log_action(role.guild, "ROLE_DELETE", None, None,
                    f"Rôle supprimé: {role.name}")

@bot.event
async def on_member_ban(guild, member):
    # Essayer de récupérer qui a fait le ban via les audit logs
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=1):
            if entry.target.id == member.id:
                await log_action(guild, "BAN", entry.user, member,
                                f"Membre banni du serveur",
                                f"Raison: {entry.reason or 'Aucune raison'}")
                return
    except:
        pass
    
    await log_action(guild, "BAN", None, member,
                    f"Membre banni du serveur")

@bot.event
async def on_member_unban(guild, user):
    # Essayer de récupérer qui a fait le unban via les audit logs
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.unban, limit=1):
            if entry.target.id == user.id:
                await log_action(guild, "UNBAN", entry.user, user,
                                f"Membre débanni du serveur",
                                f"Raison: {entry.reason or 'Aucune raison'}")
                return
    except:
        pass
    
    await log_action(guild, "UNBAN", None, user,
                    f"Membre débanni du serveur")

# === NOUVEAUX ÉVÉNEMENTS DE LOGS COMPLETS ===

@bot.event
async def on_bulk_message_delete(messages):
    """Log pour suppression en masse de messages"""
    if not messages:
        return
    
    channel = messages[0].channel
    guild = channel.guild
    
    # Essayer de récupérer qui a fait la suppression
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.message_bulk_delete, limit=1):
            if entry.extra.channel.id == channel.id:
                await log_action(guild, "BULK_DELETE", entry.user, channel,
                                f"Suppression en masse dans {channel.mention}",
                                f"Messages supprimés: {len(messages)}")
                return
    except:
        pass
    
    await log_action(guild, "BULK_DELETE", None, channel,
                    f"Suppression en masse dans {channel.mention}",
                    f"Messages supprimés: {len(messages)}")

@bot.event
async def on_member_kick(guild, member):
    """Log pour les expulsions"""
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.kick, limit=1):
            if entry.target.id == member.id:
                await log_action(guild, "KICK", entry.user, member,
                                f"Membre expulsé du serveur",
                                f"Raison: {entry.reason or 'Aucune raison'}")
                return
    except:
        pass
    
    await log_action(guild, "KICK", None, member,
                    f"Membre expulsé du serveur")

@bot.event
async def on_guild_channel_update(before, after):
    """Log pour modifications de salons"""
    changes = []
    
    if before.name != after.name:
        changes.append(f"Nom: {before.name} → {after.name}")
    
    if hasattr(before, 'topic') and hasattr(after, 'topic') and before.topic != after.topic:
        changes.append(f"Sujet: {before.topic or 'Aucun'} → {after.topic or 'Aucun'}")
    
    if hasattr(before, 'slowmode_delay') and hasattr(after, 'slowmode_delay') and before.slowmode_delay != after.slowmode_delay:
        changes.append(f"Mode lent: {before.slowmode_delay}s → {after.slowmode_delay}s")
    
    if changes:
        try:
            async for entry in after.guild.audit_logs(action=discord.AuditLogAction.channel_update, limit=1):
                if entry.target.id == after.id:
                    await log_action(after.guild, "CHANNEL_UPDATE", entry.user, after,
                                    f"Salon modifié: {after.name}",
                                    "\n".join(changes))
                    return
        except:
            pass
        
        await log_action(after.guild, "CHANNEL_UPDATE", None, after,
                        f"Salon modifié: {after.name}",
                        "\n".join(changes))

@bot.event
async def on_guild_role_update(before, after):
    """Log pour modifications de rôles"""
    changes = []
    
    if before.name != after.name:
        changes.append(f"Nom: {before.name} → {after.name}")
    
    if before.color != after.color:
        changes.append(f"Couleur: {before.color} → {after.color}")
    
    if before.permissions != after.permissions:
        changes.append("Permissions modifiées")
    
    if before.mentionable != after.mentionable:
        changes.append(f"Mentionnable: {before.mentionable} → {after.mentionable}")
    
    if before.hoist != after.hoist:
        changes.append(f"Affiché séparément: {before.hoist} → {after.hoist}")
    
    if changes:
        try:
            async for entry in after.guild.audit_logs(action=discord.AuditLogAction.role_update, limit=1):
                if entry.target.id == after.id:
                    await log_action(after.guild, "ROLE_UPDATE", entry.user, None,
                                    f"Rôle modifié: {after.name}",
                                    "\n".join(changes))
                    return
        except:
            pass
        
        await log_action(after.guild, "ROLE_UPDATE", None, None,
                        f"Rôle modifié: {after.name}",
                        "\n".join(changes))

@bot.event
async def on_guild_emojis_update(guild, before, after):
    """Log pour modifications d'emojis"""
    added = set(after) - set(before)
    removed = set(before) - set(after)
    
    for emoji in added:
        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.emoji_create, limit=1):
                if entry.target.id == emoji.id:
                    await log_action(guild, "EMOJI_ADD", entry.user, None,
                                    f"Emoji ajouté: {emoji.name}",
                                    f"ID: {emoji.id}")
                    break
        except:
            await log_action(guild, "EMOJI_ADD", None, None,
                            f"Emoji ajouté: {emoji.name}",
                            f"ID: {emoji.id}")
    
    for emoji in removed:
        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.emoji_delete, limit=1):
                await log_action(guild, "EMOJI_DELETE", entry.user, None,
                                f"Emoji supprimé: {emoji.name}",
                                f"ID: {emoji.id}")
                break
        except:
            await log_action(guild, "EMOJI_DELETE", None, None,
                            f"Emoji supprimé: {emoji.name}",
                            f"ID: {emoji.id}")

@bot.event
async def on_guild_update(before, after):
    """Log pour modifications du serveur"""
    changes = []
    
    if before.name != after.name:
        changes.append(f"Nom: {before.name} → {after.name}")
    
    if before.icon != after.icon:
        changes.append("Icône modifiée")
    
    if before.banner != after.banner:
        changes.append("Bannière modifiée")
    
    if before.verification_level != after.verification_level:
        changes.append(f"Niveau de vérification: {before.verification_level} → {after.verification_level}")
    
    if before.explicit_content_filter != after.explicit_content_filter:
        changes.append(f"Filtre de contenu: {before.explicit_content_filter} → {after.explicit_content_filter}")
    
    if changes:
        try:
            async for entry in after.audit_logs(action=discord.AuditLogAction.guild_update, limit=1):
                await log_action(after, "GUILD_UPDATE", entry.user, None,
                                f"Serveur modifié",
                                "\n".join(changes))
                return
        except:
            pass
        
        await log_action(after, "GUILD_UPDATE", None, None,
                        f"Serveur modifié",
                        "\n".join(changes))

@bot.event
async def on_invite_create(invite):
    """Log pour création d'invitations"""
    try:
        async for entry in invite.guild.audit_logs(action=discord.AuditLogAction.invite_create, limit=1):
            if entry.target.code == invite.code:
                await log_action(invite.guild, "INVITE_CREATE", entry.user, None,
                                f"Invitation créée: {invite.code}",
                                f"Salon: {invite.channel.mention}\nExpire: {invite.expires_at or 'Jamais'}\nUtilisations max: {invite.max_uses or 'Illimitées'}")
                return
    except:
        pass
    
    await log_action(invite.guild, "INVITE_CREATE", invite.inviter, None,
                    f"Invitation créée: {invite.code}",
                    f"Salon: {invite.channel.mention}\nExpire: {invite.expires_at or 'Jamais'}\nUtilisations max: {invite.max_uses or 'Illimitées'}")

@bot.event
async def on_invite_delete(invite):
    """Log pour suppression d'invitations"""
    try:
        async for entry in invite.guild.audit_logs(action=discord.AuditLogAction.invite_delete, limit=1):
            await log_action(invite.guild, "INVITE_DELETE", entry.user, None,
                            f"Invitation supprimée: {invite.code}",
                            f"Salon: {invite.channel.mention}")
            return
    except:
        pass
    
    await log_action(invite.guild, "INVITE_DELETE", None, None,
                    f"Invitation supprimée: {invite.code}",
                    f"Salon: {invite.channel.mention}")

@bot.event
async def on_webhooks_update(channel):
    """Log pour modifications de webhooks"""
    try:
        async for entry in channel.guild.audit_logs(action=discord.AuditLogAction.webhook_create, limit=1):
            await log_action(channel.guild, "WEBHOOK_UPDATE", entry.user, channel,
                            f"Webhook modifié dans {channel.mention}",
                            f"Action: Création")
            return
    except:
        pass
    
    try:
        async for entry in channel.guild.audit_logs(action=discord.AuditLogAction.webhook_update, limit=1):
            await log_action(channel.guild, "WEBHOOK_UPDATE", entry.user, channel,
                            f"Webhook modifié dans {channel.mention}",
                            f"Action: Modification")
            return
    except:
        pass
    
    try:
        async for entry in channel.guild.audit_logs(action=discord.AuditLogAction.webhook_delete, limit=1):
            await log_action(channel.guild, "WEBHOOK_UPDATE", entry.user, channel,
                            f"Webhook modifié dans {channel.mention}",
                            f"Action: Suppression")
            return
    except:
        pass

@bot.event
async def on_thread_create(thread):
    """Log pour création de threads"""
    await log_action(thread.guild, "THREAD_CREATE", thread.owner, thread,
                    f"Thread créé: {thread.name}",
                    f"Dans: {thread.parent.mention}")

@bot.event
async def on_thread_delete(thread):
    """Log pour suppression de threads"""
    try:
        async for entry in thread.guild.audit_logs(action=discord.AuditLogAction.thread_delete, limit=1):
            if entry.target.id == thread.id:
                await log_action(thread.guild, "THREAD_DELETE", entry.user, None,
                                f"Thread supprimé: {thread.name}",
                                f"Était dans: {thread.parent.mention}")
                return
    except:
        pass
    
    await log_action(thread.guild, "THREAD_DELETE", None, None,
                    f"Thread supprimé: {thread.name}",
                    f"Était dans: {thread.parent.mention}")

@bot.event
async def on_reaction_add(reaction, user):
    """Log pour ajout de réactions (optionnel - peut être beaucoup)"""
    if user.bot:
        return
    
    # Seulement logger les réactions sur les messages importants ou dans certains salons
    # Vous pouvez personnaliser cette condition
    if reaction.message.pinned or reaction.count == 1:  # Premier à réagir ou message épinglé
        await log_action(reaction.message.guild, "REACTION_ADD", user, None,
                        f"Réaction ajoutée: {reaction.emoji}",
                        f"Message de: {reaction.message.author.mention}\nDans: {reaction.message.channel.mention}")

@bot.event
async def on_reaction_remove(reaction, user):
    """Log pour suppression de réactions"""
    if user.bot:
        return
    
    # Condition similaire à l'ajout
    if reaction.message.pinned:
        await log_action(reaction.message.guild, "REACTION_REMOVE", user, None,
                        f"Réaction supprimée: {reaction.emoji}",
                        f"Message de: {reaction.message.author.mention}\nDans: {reaction.message.channel.mention}")

# Logs spéciaux pour les actions de modération automatiques
async def log_moderation_action(guild, action_type, moderator, target, reason, duration=None, additional_info=None):
    """Log spécialement pour les actions de modération avec plus de détails"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT log_channel, log_mention_user FROM guild_config WHERE guild_id = ?', (guild.id,))
    config = cursor.fetchone()
    
    if config and config[0]:
        channel = guild.get_channel(config[0])
        mention_user_id = config[1] if len(config) > 1 else None
        
        if channel:
            # Couleurs spéciales pour la modération
            colors = {
                'WARN': 0xffff00,
                'KICK': 0xff9900,
                'BAN': 0xff0000,
                'TEMPBAN': 0xff6600,
                'MUTE': 0x666666,
                'TEMPMUTE': 0x999999,
                'UNMUTE': 0x00ff00,
                'UNBAN': 0x00ff00
            }
            
            embed = discord.Embed(
                title=f"🛡️ MODÉRATION - {action_type}",
                color=colors.get(action_type, 0xff0000),
                timestamp=datetime.datetime.now()
            )
            
            if moderator:
                embed.add_field(name="Modérateur", value=f"{moderator.mention}\n({moderator.name}#{moderator.discriminator})", inline=True)
            
            if target:
                embed.add_field(name="Cible", value=f"{target.mention if hasattr(target, 'mention') else target}\n(ID: {target.id if hasattr(target, 'id') else 'N/A'})", inline=True)
            
            if duration:
                embed.add_field(name="Durée", value=duration, inline=True)
            
            if reason:
                embed.add_field(name="Raison", value=reason, inline=False)
            
            if additional_info:
                embed.add_field(name="Informations supplémentaires", value=additional_info, inline=False)
            
            embed.set_footer(text=f"Action de modération • ID: {target.id if hasattr(target, 'id') else 'N/A'}")
            
            try:
                # Préparer le message avec mention si configurée
                content = ""
                if mention_user_id:
                    mention_user = guild.get_member(mention_user_id)
                    if mention_user:
                        content = mention_user.mention
                
                await channel.send(content=content, embed=embed)
            except:
                pass
    
    conn.close()

# === COMMANDES UTILITAIRES ===
@bot.command(name='userinfo', aliases=['ui'])
async def userinfo(ctx, member: discord.Member = None):
    """Affiche les informations d'un utilisateur"""
    if member is None:
        member = ctx.author
    
    embed = discord.Embed(
        title=f"👤 Informations de {member.display_name}",
        color=member.color
    )
    embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
    embed.add_field(name="Nom d'utilisateur", value=f"{member.name}#{member.discriminator}", inline=True)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Surnom", value=member.display_name, inline=True)
    embed.add_field(name="Compte créé le", value=member.created_at.strftime("%d/%m/%Y"), inline=True)
    embed.add_field(name="A rejoint le", value=member.joined_at.strftime("%d/%m/%Y"), inline=True)
    embed.add_field(name="Rôles", value=f"{len(member.roles) - 1} rôle(s)", inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='serverinfo', aliases=['si'])
async def serverinfo(ctx):
    """Affiche les informations du serveur"""
    guild = ctx.guild
    
    embed = discord.Embed(
        title=f"🏰 Informations de {guild.name}",
        color=0x3498db
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.add_field(name="Propriétaire", value=guild.owner.mention, inline=True)
    embed.add_field(name="Membres", value=guild.member_count, inline=True)
    embed.add_field(name="Créé le", value=guild.created_at.strftime("%d/%m/%Y"), inline=True)
    embed.add_field(name="Salons", value=len(guild.channels), inline=True)
    embed.add_field(name="Rôles", value=len(guild.roles), inline=True)
    embed.add_field(name="Boost", value=f"Niveau {guild.premium_tier}", inline=True)
    
    await ctx.send(embed=embed)

# === COMMANDES MUSICALES ===
@bot.command(name='join', aliases=['connect'])
async def join_voice(ctx):
    """Connecte le bot au salon vocal"""
    if not ctx.author.voice:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Vous devez être dans un salon vocal !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return

    channel = ctx.author.voice.channel
    
    if ctx.guild.id in voice_clients:
        await voice_clients[ctx.guild.id].move_to(channel)
    else:
        voice_clients[ctx.guild.id] = await channel.connect()
        music_queues[ctx.guild.id] = MusicPlayer(ctx)
        music_queues[ctx.guild.id].voice_client = voice_clients[ctx.guild.id]

    embed = discord.Embed(
        title="🎵 Connecté !",
        description=f"Connecté au salon vocal **{channel.name}**",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='leave', aliases=['disconnect'])
async def leave_voice(ctx):
    """Déconnecte le bot du salon vocal"""
    if ctx.guild.id not in voice_clients:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Le bot n'est pas connecté à un salon vocal !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return

    await voice_clients[ctx.guild.id].disconnect()
    del voice_clients[ctx.guild.id]
    
    if ctx.guild.id in music_queues:
        del music_queues[ctx.guild.id]

    embed = discord.Embed(
        title="👋 Déconnecté",
        description="Bot déconnecté du salon vocal",
        color=0xff9900
    )
    await ctx.send(embed=embed)

@bot.command(name='play', aliases=['p'])
async def play_music(ctx, *, query):
    """Joue de la musique depuis YouTube, Spotify ou un fichier"""
    if not ctx.author.voice:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Vous devez être dans un salon vocal !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return

    # Connecter le bot si pas déjà connecté
    if ctx.guild.id not in voice_clients:
        await join_voice(ctx)

    # Vérifier si c'est un lien Spotify
    if 'spotify.com' in query:
        if not spotify:
            embed = discord.Embed(
                title="❌ Erreur",
                description="Les identifiants Spotify ne sont pas configurés !",
                color=0xff0000
            )
            await ctx.send(embed=embed)
            return
        
        query = get_spotify_track_info(query)
        if not query:
            embed = discord.Embed(
                title="❌ Erreur",
                description="Impossible de récupérer les informations Spotify !",
                color=0xff0000
            )
            await ctx.send(embed=embed)
            return

    try:
        embed = discord.Embed(
            title="🔍 Recherche en cours...",
            description=f"Recherche de : **{query}**",
            color=0xffff00
        )
        search_msg = await ctx.send(embed=embed)

        # Si c'est un fichier local .mp3
        if query.endswith('.mp3') and not query.startswith('http'):
            if os.path.exists(query):
                source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(query))
                music_queues[ctx.guild.id].queue.append(source)
                
                embed = discord.Embed(
                    title="🎵 Ajouté à la file",
                    description=f"**{os.path.basename(query)}** ajouté à la file d'attente",
                    color=0x00ff00
                )
                await search_msg.edit(embed=embed)
            else:
                embed = discord.Embed(
                    title="❌ Erreur",
                    description="Fichier MP3 introuvable !",
                    color=0xff0000
                )
                await search_msg.edit(embed=embed)
                return
        else:
            # Recherche YouTube/URL
            source = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
            await music_queues[ctx.guild.id].add_to_queue(source)
            
            embed = discord.Embed(
                title="🎵 Ajouté à la file",
                description=f"**{source.title}** ajouté à la file d'attente",
                color=0x00ff00
            )
            if source.uploader:
                embed.add_field(name="Chaîne", value=source.uploader, inline=True)
            if source.duration:
                duration = str(datetime.timedelta(seconds=source.duration))
                embed.add_field(name="Durée", value=duration, inline=True)
            
            await search_msg.edit(embed=embed)

    except Exception as e:
        embed = discord.Embed(
            title="❌ Erreur",
            description=f"Erreur lors de la lecture : {str(e)}",
            color=0xff0000
        )
        await ctx.send(embed=embed)

@bot.command(name='pause')
async def pause_music(ctx):
    """Met en pause la musique"""
    if ctx.guild.id in voice_clients and voice_clients[ctx.guild.id].is_playing():
        voice_clients[ctx.guild.id].pause()
        embed = discord.Embed(
            title="⏸️ Musique en pause",
            color=0xffff00
        )
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Aucune musique n'est en cours de lecture !",
            color=0xff0000
        )
        await ctx.send(embed=embed)

@bot.command(name='resume')
async def resume_music(ctx):
    """Reprend la musique"""
    if ctx.guild.id in voice_clients and voice_clients[ctx.guild.id].is_paused():
        voice_clients[ctx.guild.id].resume()
        embed = discord.Embed(
            title="▶️ Musique reprise",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Aucune musique n'est en pause !",
            color=0xff0000
        )
        await ctx.send(embed=embed)

@bot.command(name='stop')
async def stop_music(ctx):
    """Arrête la musique et vide la file"""
    if ctx.guild.id in voice_clients:
        voice_clients[ctx.guild.id].stop()
        if ctx.guild.id in music_queues:
            music_queues[ctx.guild.id].queue.clear()
            music_queues[ctx.guild.id].is_playing = False
        
        embed = discord.Embed(
            title="⏹️ Musique arrêtée",
            description="File d'attente vidée",
            color=0xff0000
        )
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Aucune musique n'est en cours !",
            color=0xff0000
        )
        await ctx.send(embed=embed)

@bot.command(name='skip', aliases=['next'])
async def skip_music(ctx):
    """Passe à la musique suivante"""
    if ctx.guild.id in voice_clients and voice_clients[ctx.guild.id].is_playing():
        voice_clients[ctx.guild.id].stop()
        embed = discord.Embed(
            title="⏭️ Musique passée",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Aucune musique n'est en cours !",
            color=0xff0000
        )
        await ctx.send(embed=embed)

@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx):
    """Affiche la file d'attente"""
    if ctx.guild.id not in music_queues or len(music_queues[ctx.guild.id].queue) == 0:
        embed = discord.Embed(
            title="📋 File d'attente vide",
            description="Aucune musique en attente",
            color=0xffff00
        )
        await ctx.send(embed=embed)
        return

    queue = music_queues[ctx.guild.id].queue
    current = music_queues[ctx.guild.id].current
    
    embed = discord.Embed(
        title="📋 File d'attente",
        color=0x3498db
    )
    
    if current:
        embed.add_field(
            name="🎵 En cours",
            value=f"**{current.title}**",
            inline=False
        )
    
    for i, song in enumerate(list(queue)[:10], 1):
        title = getattr(song, 'title', 'Fichier local')
        embed.add_field(
            name=f"{i}.",
            value=f"**{title}**",
            inline=False
        )
    
    if len(queue) > 10:
        embed.add_field(
            name="...",
            value=f"Et {len(queue) - 10} autre(s) musique(s)",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.command(name='volume', aliases=['vol'])
async def set_volume(ctx, volume: int = None):
    """Ajuste le volume (0-100)"""
    if volume is None:
        if ctx.guild.id in music_queues:
            current_vol = int(music_queues[ctx.guild.id].volume * 100)
            embed = discord.Embed(
                title="🔊 Volume actuel",
                description=f"Volume : **{current_vol}%**",
                color=0x3498db
            )
            await ctx.send(embed=embed)
        return

    if volume < 0 or volume > 100:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Le volume doit être entre 0 et 100 !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return

    if ctx.guild.id in music_queues:
        music_queues[ctx.guild.id].volume = volume / 100
        if music_queues[ctx.guild.id].current:
            music_queues[ctx.guild.id].current.volume = volume / 100
        
        embed = discord.Embed(
            title="🔊 Volume ajusté",
            description=f"Volume défini à **{volume}%**",
            color=0x00ff00
        )
        await ctx.send(embed=embed)

@bot.command(name='nowplaying', aliases=['np'])
async def now_playing(ctx):
    """Affiche la musique en cours"""
    if ctx.guild.id not in music_queues or not music_queues[ctx.guild.id].current:
        embed = discord.Embed(
            title="❌ Aucune musique",
            description="Aucune musique n'est en cours de lecture",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return

    current = music_queues[ctx.guild.id].current
    embed = discord.Embed(
        title="🎵 En cours de lecture",
        description=f"**{current.title}**",
        color=0x00ff00
    )
    
    if hasattr(current, 'uploader') and current.uploader:
        embed.add_field(name="Chaîne", value=current.uploader, inline=True)
    
    if hasattr(current, 'duration') and current.duration:
        duration = str(datetime.timedelta(seconds=current.duration))
        embed.add_field(name="Durée", value=duration, inline=True)
    
    volume = int(music_queues[ctx.guild.id].volume * 100)
    embed.add_field(name="Volume", value=f"{volume}%", inline=True)
    
    await ctx.send(embed=embed)

# === MODÉRATION AVANCÉE ===
@bot.command(name='mute')
@commands.has_permissions(manage_roles=True)
async def mute(ctx, member: discord.Member, duration: int = None, *, reason="Aucune raison spécifiée"):
    """Rend muet un membre (durée en minutes)"""
    # Récupérer le rôle mute configuré
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT mute_role FROM guild_config WHERE guild_id = ?', (ctx.guild.id,))
    config = cursor.fetchone()
    conn.close()
    
    if not config or not config[0]:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Aucun rôle mute configuré ! Utilisez `!set muterole @role`",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    mute_role = ctx.guild.get_role(config[0])
    if not mute_role:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Le rôle mute configuré n'existe plus !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    try:
        await member.add_roles(mute_role, reason=reason)
        
        duration_text = f" pendant {duration} minute(s)" if duration else ""
        embed = discord.Embed(
            title="🔇 Membre rendu muet",
            description=f"{member.mention} a été rendu muet{duration_text}.\n**Raison:** {reason}",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        
        # Démute automatique si durée spécifiée
        if duration:
            await asyncio.sleep(duration * 60)
            if mute_role in member.roles:
                await member.remove_roles(mute_role, reason="Fin du mute automatique")
                embed = discord.Embed(
                    title="🔊 Mute expiré",
                    description=f"Le mute de {member.mention} a expiré.",
                    color=0x00ff00
                )
                await ctx.send(embed=embed)
        
        await log_moderation_action(ctx.guild, "MUTE", ctx.author, member, reason)
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du mute : {e}")

@bot.command(name='unmute')
@commands.has_permissions(manage_roles=True)
async def unmute(ctx, member: discord.Member):
    """Redonne la parole à un membre"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT mute_role FROM guild_config WHERE guild_id = ?', (ctx.guild.id,))
    config = cursor.fetchone()
    conn.close()
    
    if not config or not config[0]:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Aucun rôle mute configuré !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    mute_role = ctx.guild.get_role(config[0])
    if not mute_role or mute_role not in member.roles:
        embed = discord.Embed(
            title="❌ Erreur",
            description="Ce membre n'est pas muet !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    try:
        await member.remove_roles(mute_role, reason="Unmute par modérateur")
        embed = discord.Embed(
            title="🔊 Membre démuté",
            description=f"{member.mention} peut de nouveau parler.",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        await log_moderation_action(ctx.guild, "UNMUTE", ctx.author, member, "Unmute par modérateur")
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du unmute : {e}")

@bot.command(name='slowmode')
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int = 0):
    """Active/désactive le mode lent"""
    if seconds < 0 or seconds > 21600:  # Max 6 heures
        await ctx.send("❌ La durée doit être entre 0 et 21600 secondes (6 heures) !")
        return
    
    try:
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            embed = discord.Embed(
                title="⚡ Mode lent désactivé",
                description="Le mode lent a été désactivé dans ce salon.",
                color=0x00ff00
            )
        else:
            embed = discord.Embed(
                title="🐌 Mode lent activé",
                description=f"Mode lent défini à **{seconds}** seconde(s).",
                color=0xff9900
            )
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "SLOWMODE", ctx.author, ctx.channel, f"{seconds} secondes")
    except Exception as e:
        await ctx.send(f"❌ Erreur lors de la modification du slowmode : {e}")

@bot.command(name='lock')
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    """Verrouille le salon actuel"""
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
        embed = discord.Embed(
            title="🔒 Salon verrouillé",
            description="Ce salon a été verrouillé.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "LOCK", ctx.author, ctx.channel, "Salon verrouillé")
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du verrouillage : {e}")

@bot.command(name='unlock')
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    """Déverrouille le salon actuel"""
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
        embed = discord.Embed(
            title="🔓 Salon déverrouillé",
            description="Ce salon a été déverrouillé.",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "UNLOCK", ctx.author, ctx.channel, "Salon déverrouillé")
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du déverrouillage : {e}")

@bot.command(name='unban')
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    """Débannit un utilisateur par son ID"""
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        embed = discord.Embed(
            title="✅ Utilisateur débanni",
            description=f"**{user.name}#{user.discriminator}** a été débanni.",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "UNBAN", ctx.author, user, "Débannissement")
    except discord.NotFound:
        await ctx.send("❌ Utilisateur non trouvé ou pas banni !")
    except Exception as e:
        await ctx.send(f"❌ Erreur lors du débannissement : {e}")

# === GESTION AVANCÉE DES NIVEAUX ===
@bot.command(name='setlevel')
@commands.has_permissions(administrator=True)
async def set_level(ctx, member: discord.Member, level: int):
    """Définit le niveau d'un utilisateur"""
    if level < 0:
        await ctx.send("❌ Le niveau ne peut pas être négatif !")
        return
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    new_xp = xp_for_level(level)
    cursor.execute('''
        INSERT OR REPLACE INTO user_levels (user_id, guild_id, xp, level, messages)
        VALUES (?, ?, ?, ?, COALESCE((SELECT messages FROM user_levels WHERE user_id = ? AND guild_id = ?), 0))
    ''', (member.id, ctx.guild.id, new_xp, level, member.id, ctx.guild.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="📊 Niveau modifié",
        description=f"Le niveau de {member.mention} a été défini à **{level}** ({new_xp} XP).",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='addxp')
@commands.has_permissions(administrator=True)
async def add_xp(ctx, member: discord.Member, amount: int):
    """Ajoute de l'XP à un utilisateur"""
    if amount <= 0:
        await ctx.send("❌ Le montant doit être positif !")
        return
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT xp, level FROM user_levels WHERE user_id = ? AND guild_id = ?', 
                  (member.id, ctx.guild.id))
    result = cursor.fetchone()
    
    if result:
        current_xp, current_level = result
        new_xp = current_xp + amount
    else:
        current_level = 0
        new_xp = amount
    
    new_level = calculate_level(new_xp)
    
    cursor.execute('''
        INSERT OR REPLACE INTO user_levels (user_id, guild_id, xp, level, messages)
        VALUES (?, ?, ?, ?, COALESCE((SELECT messages FROM user_levels WHERE user_id = ? AND guild_id = ?), 0))
    ''', (member.id, ctx.guild.id, new_xp, new_level, member.id, ctx.guild.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="➕ XP ajouté",
        description=f"{amount} XP ajouté à {member.mention}.\nNouveau total : **{new_xp}** XP (niveau {new_level})",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='removexp')
@commands.has_permissions(administrator=True)
async def remove_xp(ctx, member: discord.Member, amount: int):
    """Retire de l'XP à un utilisateur"""
    if amount <= 0:
        await ctx.send("❌ Le montant doit être positif !")
        return
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT xp, level FROM user_levels WHERE user_id = ? AND guild_id = ?', 
                  (member.id, ctx.guild.id))
    result = cursor.fetchone()
    
    if not result:
        await ctx.send("❌ Cet utilisateur n'a pas d'XP !")
        return
    
    current_xp, current_level = result
    new_xp = max(0, current_xp - amount)
    new_level = calculate_level(new_xp)
    
    cursor.execute('''
        UPDATE user_levels SET xp = ?, level = ? WHERE user_id = ? AND guild_id = ?
    ''', (new_xp, new_level, member.id, ctx.guild.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="➖ XP retiré",
        description=f"{amount} XP retiré à {member.mention}.\nNouveau total : **{new_xp}** XP (niveau {new_level})",
        color=0xff9900
    )
    await ctx.send(embed=embed)

@bot.group(name='levelrole')
@commands.has_permissions(administrator=True)
async def level_role(ctx):
    """Gestion des rôles de niveau"""
    if ctx.invoked_subcommand is None:
        embed = discord.Embed(
            title="📋 Rôles de niveau",
            description="Commandes disponibles :\n`!levelrole add <niveau> <@role>`\n`!levelrole remove <niveau>`\n`!levelrole list`",
            color=0x3498db
        )
        await ctx.send(embed=embed)

@level_role.command(name='add')
async def levelrole_add(ctx, level: int, role: discord.Role):
    """Ajoute un rôle de niveau"""
    if level < 1:
        await ctx.send("❌ Le niveau doit être supérieur à 0 !")
        return
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('INSERT INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?)', 
                      (ctx.guild.id, level, role.id))
        conn.commit()
        embed = discord.Embed(
            title="✅ Rôle de niveau ajouté",
            description=f"Le rôle {role.mention} sera donné au niveau **{level}**.",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
    except sqlite3.IntegrityError:
        embed = discord.Embed(
            title="❌ Erreur",
            description=f"Un rôle est déjà configuré pour le niveau **{level}** !",
            color=0xff0000
        )
        await ctx.send(embed=embed)
    
    conn.close()

@level_role.command(name='remove')
async def levelrole_remove(ctx, level: int):
    """Supprime un rôle de niveau"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM level_roles WHERE guild_id = ? AND level = ?', 
                  (ctx.guild.id, level))
    
    if cursor.rowcount > 0:
        embed = discord.Embed(
            title="✅ Rôle de niveau supprimé",
            description=f"Le rôle du niveau **{level}** a été supprimé.",
            color=0x00ff00
        )
    else:
        embed = discord.Embed(
            title="❌ Erreur",
            description=f"Aucun rôle configuré pour le niveau **{level}** !",
            color=0xff0000
        )
    
    conn.commit()
    conn.close()
    await ctx.send(embed=embed)

@level_role.command(name='list')
async def levelrole_list(ctx):
    """Liste tous les rôles de niveau"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level', 
                  (ctx.guild.id,))
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        embed = discord.Embed(
            title="📋 Rôles de niveau",
            description="Aucun rôle de niveau configuré.",
            color=0xffff00
        )
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="📋 Rôles de niveau configurés",
        color=0x3498db
    )
    
    for level, role_id in results:
        role = ctx.guild.get_role(role_id)
        role_name = role.mention if role else "Rôle supprimé"
        embed.add_field(name=f"Niveau {level}", value=role_name, inline=True)
    
    await ctx.send(embed=embed)

# === UTILITAIRES DISCORD ===
@bot.command(name='avatar', aliases=['av'])
async def avatar(ctx, member: discord.Member = None):
    """Affiche l'avatar d'un utilisateur"""
    if member is None:
        member = ctx.author
    
    embed = discord.Embed(
        title=f"🖼️ Avatar de {member.display_name}",
        color=member.color
    )
    
    if member.avatar:
        embed.set_image(url=member.avatar.url)
        embed.add_field(name="Lien", value=f"[Télécharger]({member.avatar.url})", inline=False)
    else:
        embed.description = "Cet utilisateur n'a pas d'avatar personnalisé."
    
    await ctx.send(embed=embed)

@bot.command(name='ping')
async def ping(ctx):
    """Affiche la latence du bot"""
    latency = round(bot.latency * 1000)
    
    if latency < 100:
        color = 0x00ff00
        status = "Excellente"
    elif latency < 200:
        color = 0xffff00
        status = "Bonne"
    else:
        color = 0xff0000
        status = "Élevée"
    
    embed = discord.Embed(
        title="🏓 Pong !",
        description=f"**Latence :** {latency}ms\n**Status :** {status}",
        color=color
    )
    await ctx.send(embed=embed)

@bot.command(name='say')
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message):
    """Fait parler le bot"""
    await ctx.message.delete()
    await ctx.send(message)

@bot.command(name='embed')
@commands.has_permissions(manage_messages=True)
async def create_embed(ctx, title, *, description):
    """Crée un embed personnalisé"""
    embed = discord.Embed(
        title=title,
        description=description,
        color=0x3498db
    )
    embed.set_footer(text=f"Créé par {ctx.author.display_name}", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    
    await ctx.message.delete()
    await ctx.send(embed=embed)

# === CONFIGURATION AVANCÉE ===
@bot.command(name='config')
@commands.has_permissions(administrator=True)
async def show_config(ctx):
    """Affiche la configuration actuelle du serveur"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM guild_config WHERE guild_id = ?', (ctx.guild.id,))
    config = cursor.fetchone()
    
    cursor.execute('SELECT COUNT(*) FROM level_roles WHERE guild_id = ?', (ctx.guild.id,))
    level_roles_count = cursor.fetchone()[0]
    
    conn.close()
    
    embed = discord.Embed(
        title="⚙️ Configuration du serveur",
        color=0x3498db
    )
    
    if config:
        welcome_ch = ctx.guild.get_channel(config[1]) if config[1] else None
        goodbye_ch = ctx.guild.get_channel(config[2]) if config[2] else None
        log_ch = ctx.guild.get_channel(config[3]) if config[3] else None
        mute_role = ctx.guild.get_role(config[4]) if config[4] else None
        auto_role = ctx.guild.get_role(config[5]) if config[5] else None
        levelup_ch = ctx.guild.get_channel(config[6]) if config[6] else None
        
        embed.add_field(name="Salon de bienvenue", value=welcome_ch.mention if welcome_ch else "Non configuré", inline=True)
        embed.add_field(name="Salon d'au revoir", value=goodbye_ch.mention if goodbye_ch else "Non configuré", inline=True)
        embed.add_field(name="Salon de logs", value=log_ch.mention if log_ch else "Non configuré", inline=True)
        embed.add_field(name="Salon level up", value=levelup_ch.mention if levelup_ch else "Non configuré", inline=True)
        embed.add_field(name="Rôle automatique", value=auto_role.mention if auto_role else "Non configuré", inline=True)
        embed.add_field(name="Rôle mute", value=mute_role.mention if mute_role else "Non configuré", inline=True)
    else:
        embed.description = "Aucune configuration trouvée. Utilisez `!setup` pour commencer."
    
    embed.add_field(name="Rôles de niveau", value=f"{level_roles_count} configuré(s)", inline=True)
    embed.add_field(name="Préfixe", value="`!`", inline=True)
    
    # Afficher l'utilisateur à mentionner dans les logs
    if config and len(config) > 6 and config[7]:
        mention_user = ctx.guild.get_member(config[7])
        embed.add_field(name="Mention dans les logs", value=mention_user.mention if mention_user else "Utilisateur introuvable", inline=True)
    else:
        embed.add_field(name="Mention dans les logs", value="Non configuré", inline=True)
    
    await ctx.send(embed=embed)

@set_config.command(name='muterole')
async def set_mute_role(ctx, role: discord.Role):
    """Définit le rôle mute"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config (guild_id, mute_role)
        VALUES (?, ?)
    ''', (ctx.guild.id, role.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Rôle mute défini sur {role.mention}",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@set_config.command(name='logmention')
async def set_log_mention(ctx, member: discord.Member):
    """Définit l'utilisateur à mentionner dans tous les logs"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO guild_config 
        (guild_id, log_mention_user, welcome_channel, goodbye_channel, log_channel, mute_role, auto_role, level_up_channel)
        VALUES (?, ?, 
            COALESCE((SELECT welcome_channel FROM guild_config WHERE guild_id = ?), NULL),
            COALESCE((SELECT goodbye_channel FROM guild_config WHERE guild_id = ?), NULL),
            COALESCE((SELECT log_channel FROM guild_config WHERE guild_id = ?), NULL),
            COALESCE((SELECT mute_role FROM guild_config WHERE guild_id = ?), NULL),
            COALESCE((SELECT auto_role FROM guild_config WHERE guild_id = ?), NULL),
            COALESCE((SELECT level_up_channel FROM guild_config WHERE guild_id = ?), NULL)
        )
    ''', (ctx.guild.id, member.id, ctx.guild.id, ctx.guild.id, ctx.guild.id, ctx.guild.id, ctx.guild.id, ctx.guild.id))
    
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Configuration mise à jour",
        description=f"Mention des logs définie sur {member.mention}\nCet utilisateur sera mentionné à chaque log !",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

# === COMMANDES FUN ===
@bot.command(name='8ball')
async def eight_ball(ctx, *, question):
    """Boule magique 8"""
    responses = [
        "Oui, absolument !",
        "Non, certainement pas.",
        "Peut-être...",
        "Les signes pointent vers oui.",
        "Concentre-toi et redemande.",
        "Les perspectives ne sont pas bonnes.",
        "Oui, sans aucun doute.",
        "Mes sources disent non.",
        "C'est certain.",
        "Très douteux."
    ]
    
    embed = discord.Embed(
        title="🎱 Boule magique 8",
        description=f"**Question:** {question}\n**Réponse:** {random.choice(responses)}",
        color=0x8b00ff
    )
    await ctx.send(embed=embed)

@bot.command(name='dice', aliases=['roll'])
async def dice(ctx, sides: int = 6):
    """Lance un dé"""
    if sides < 2:
        await ctx.send("❌ Le dé doit avoir au moins 2 faces !")
        return
    
    result = random.randint(1, sides)
    embed = discord.Embed(
        title="🎲 Lancer de dé",
        description=f"Résultat : **{result}** (sur {sides})",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='coinflip', aliases=['flip'])
async def coinflip(ctx):
    """Lance une pièce"""
    result = random.choice(["Pile", "Face"])
    emoji = "🪙"
    
    embed = discord.Embed(
        title=f"{emoji} Lancer de pièce",
        description=f"Résultat : **{result}**",
        color=0xffd700
    )
    await ctx.send(embed=embed)

# === COMMANDE D'AIDE PERSONNALISÉE ===
@bot.remove_command('help')
@bot.command(name='help')
async def help_command(ctx, category=None):
    """Affiche l'aide"""
    if category is None:
        embed = discord.Embed(
            title="🤖 Aide du Bot Discord Français",
            description="Voici toutes les catégories de commandes disponibles :",
            color=0x3498db
        )
        embed.add_field(name="📊 Niveaux", value="`!help levels`", inline=True)
        embed.add_field(name="🛡️ Modération", value="`!help moderation`", inline=True)
        embed.add_field(name="⚙️ Configuration", value="`!help config`", inline=True)
        embed.add_field(name="🎵 Musique", value="`!help music`", inline=True)
        embed.add_field(name="🎮 Fun", value="`!help fun`", inline=True)
        embed.add_field(name="🔧 Utilitaires", value="`!help utils`", inline=True)
        embed.set_footer(text="Utilisez !help <catégorie> pour plus de détails")
        
    elif category.lower() in ['levels', 'niveau', 'niveaux']:
        embed = discord.Embed(
            title="📊 Commandes de niveaux",
            color=0x00ff00
        )
        embed.add_field(name="!rank [@utilisateur]", value="Affiche le niveau", inline=False)
        embed.add_field(name="!leaderboard", value="Classement du serveur", inline=False)
        embed.add_field(name="!setlevel <@utilisateur> <niveau>", value="Définir le niveau (Admin)", inline=False)
        embed.add_field(name="!addxp <@utilisateur> <montant>", value="Ajouter de l'XP (Admin)", inline=False)
        embed.add_field(name="!removexp <@utilisateur> <montant>", value="Retirer de l'XP (Admin)", inline=False)
        embed.add_field(name="!levelrole add/remove/list", value="Gérer les rôles de niveau", inline=False)
        
    elif category.lower() in ['moderation', 'mod']:
        embed = discord.Embed(
            title="🛡️ Commandes de modération",
            color=0xff0000
        )
        embed.add_field(name="!kick <@utilisateur> [raison]", value="Expulser un membre", inline=False)
        embed.add_field(name="!ban <@utilisateur> [raison]", value="Bannir un membre", inline=False)
        embed.add_field(name="!unban <ID>", value="Débannir un utilisateur", inline=False)
        embed.add_field(name="!mute <@utilisateur> [durée] [raison]", value="Rendre muet (durée en minutes)", inline=False)
        embed.add_field(name="!unmute <@utilisateur>", value="Redonner la parole", inline=False)
        embed.add_field(name="!warn <@utilisateur> [raison]", value="Donner un avertissement", inline=False)
        embed.add_field(name="!warnings <@utilisateur>", value="Voir les avertissements", inline=False)
        embed.add_field(name="!clear [nombre]", value="Supprimer des messages", inline=False)
        embed.add_field(name="!slowmode [secondes]", value="Mode lent (0 pour désactiver)", inline=False)
        embed.add_field(name="!lock / !unlock", value="Verrouiller/déverrouiller salon", inline=False)
        
    elif category.lower() in ['config', 'configuration']:
        embed = discord.Embed(
            title="⚙️ Commandes de configuration",
            color=0xffff00
        )
        embed.add_field(name="!setup", value="Aide de configuration", inline=False)
        embed.add_field(name="!config", value="Voir la configuration actuelle", inline=False)
        embed.add_field(name="!set welcome <#salon>", value="Salon de bienvenue", inline=False)
        embed.add_field(name="!set goodbye <#salon>", value="Salon d'au revoir", inline=False)
        embed.add_field(name="!set logs <#salon>", value="Salon de logs", inline=False)
        embed.add_field(name="!set levelup <#salon>", value="Salon de level up", inline=False)
        embed.add_field(name="!set autorole <@role>", value="Rôle automatique", inline=False)
        embed.add_field(name="!set muterole <@role>", value="Rôle mute", inline=False)
        embed.add_field(name="!set logmention <@utilisateur>", value="Mentionner dans tous les logs", inline=False)
        
    elif category.lower() in ['music', 'musique']:
        embed = discord.Embed(
            title="🎵 Commandes musicales",
            color=0x9932cc
        )
        embed.add_field(name="!join", value="Connecter le bot au salon vocal", inline=False)
        embed.add_field(name="!leave", value="Déconnecter le bot", inline=False)
        embed.add_field(name="!play <lien/recherche>", value="Jouer de la musique", inline=False)
        embed.add_field(name="!pause", value="Mettre en pause", inline=False)
        embed.add_field(name="!resume", value="Reprendre la lecture", inline=False)
        embed.add_field(name="!stop", value="Arrêter et vider la file", inline=False)
        embed.add_field(name="!skip", value="Passer à la suivante", inline=False)
        embed.add_field(name="!queue", value="Afficher la file d'attente", inline=False)
        embed.add_field(name="!volume [0-100]", value="Ajuster le volume", inline=False)
        embed.add_field(name="!nowplaying", value="Musique en cours", inline=False)
        
    elif category.lower() in ['fun', 'amusement']:
        embed = discord.Embed(
            title="🎮 Commandes fun",
            color=0x8b00ff
        )
        embed.add_field(name="!8ball <question>", value="Boule magique 8", inline=False)
        embed.add_field(name="!dice [faces]", value="Lancer un dé", inline=False)
        embed.add_field(name="!coinflip", value="Lancer une pièce", inline=False)
        
    elif category.lower() in ['utils', 'utilitaires']:
        embed = discord.Embed(
            title="🔧 Commandes utilitaires",
            color=0x3498db
        )
        embed.add_field(name="!userinfo [@utilisateur]", value="Infos utilisateur", inline=False)
        embed.add_field(name="!serverinfo", value="Infos du serveur", inline=False)
        embed.add_field(name="!avatar [@utilisateur]", value="Afficher l'avatar", inline=False)
        embed.add_field(name="!ping", value="Latence du bot", inline=False)
        embed.add_field(name="!say <message>", value="Faire parler le bot", inline=False)
        embed.add_field(name="!embed <titre> <description>", value="Créer un embed", inline=False)
        
    else:
        embed = discord.Embed(
            title="❌ Catégorie inconnue",
            description="Utilisez `!help` pour voir toutes les catégories.",
            color=0xff0000
        )
    
    await ctx.send(embed=embed)

# Gestion des erreurs
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(
            title="❌ Permissions insuffisantes",
            description="Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MemberNotFound):
        embed = discord.Embed(
            title="❌ Membre introuvable",
            description="Je n'ai pas pu trouver ce membre.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
    elif isinstance(error, commands.CommandNotFound):
        return  # Ignorer les commandes inconnues
    else:
        print(f"Erreur: {error}")

# Fonction pour traiter les commandes web
async def process_web_commands():
    """Traite les commandes provenant de l'interface web"""
    # Traiter les commandes musicales
    if os.path.exists('music_commands.txt'):
        try:
            with open('music_commands.txt', 'r') as f:
                commands = f.readlines()
            
            # Vider le fichier après lecture
            open('music_commands.txt', 'w').close()
            
            for command in commands:
                command = command.strip()
                if not command:
                    continue
                
                parts = command.split('|')
                cmd = parts[0]
                
                print(f"📡 Commande musicale web reçue: {cmd}")
                
                if cmd == 'CONNECT':
                    print("🔗 Commande de connexion vocale reçue")
                elif cmd == 'DISCONNECT':
                    print("🔌 Commande de déconnexion vocale reçue")
                elif cmd == 'PLAY' and len(parts) >= 3:
                    url, title = parts[1], parts[2]
                    print(f"🎵 Commande de lecture reçue: {title}")
                elif cmd == 'ADD' and len(parts) >= 3:
                    url, title = parts[1], parts[2]
                    print(f"➕ Commande d'ajout reçue: {title}")
                    
        except Exception as e:
            print(f"❌ Erreur lors du traitement des commandes musicales: {e}")
    
    # Traiter les commandes de messages
    if os.path.exists('message_commands.txt'):
        try:
            with open('message_commands.txt', 'r', encoding='utf-8') as f:
                commands = f.readlines()
            
            # Vider le fichier après lecture
            open('message_commands.txt', 'w').close()
            
            for command in commands:
                command = command.strip()
                if not command:
                    continue
                
                parts = command.split('|', 2)  # Limiter à 3 parties max
                cmd = parts[0]
                
                print(f"📡 Commande message web reçue: {cmd}")
                
                if cmd == 'SEND_MESSAGE' and len(parts) >= 3:
                    channel_id, content = parts[1], parts[2]
                    await send_message_to_channel(int(channel_id), content)
                elif cmd == 'SEND_EMBED' and len(parts) >= 3:
                    channel_id, embed_json = parts[1], parts[2]
                    try:
                        embed_data = json.loads(embed_json)
                        await send_embed_to_channel(int(channel_id), embed_data)
                    except json.JSONDecodeError:
                        print("❌ Erreur lors du parsing de l'embed JSON")
                    
        except Exception as e:
            print(f"❌ Erreur lors du traitement des commandes de messages: {e}")

async def send_message_to_channel(channel_id, content):
    """Envoie un message simple dans un salon Discord"""
    try:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(content)
            print(f"✅ Message envoyé dans #{channel.name}: {content[:50]}...")
        else:
            print(f"❌ Salon {channel_id} introuvable")
    except Exception as e:
        print(f"❌ Erreur lors de l'envoi du message: {e}")

async def send_embed_to_channel(channel_id, embed_data):
    """Envoie un embed dans un salon Discord"""
    try:
        channel = bot.get_channel(channel_id)
        if channel:
            embed = discord.Embed(
                title=embed_data.get('title', ''),
                description=embed_data.get('description', ''),
                color=int(embed_data.get('color', '3498db'), 16)
            )
            
            if embed_data.get('footer'):
                embed.set_footer(text=embed_data['footer'])
            
            if embed_data.get('image_url'):
                embed.set_image(url=embed_data['image_url'])
            
            await channel.send(embed=embed)
            print(f"✅ Embed envoyé dans #{channel.name}: {embed_data.get('title', 'Sans titre')}")
        else:
            print(f"❌ Salon {channel_id} introuvable")
    except Exception as e:
        print(f"❌ Erreur lors de l'envoi de l'embed: {e}")

# Tâche périodique pour vérifier les commandes web
@tasks.loop(seconds=2)
async def check_web_commands():
    """Vérifie les commandes web toutes les 2 secondes"""
    await process_web_commands()

@check_web_commands.before_loop
async def before_check_web_commands():
    await bot.wait_until_ready()

# Démarrer le bot
if __name__ == "__main__":
    print("🚀 Démarrage du bot...")
    print("📋 Fonctionnalités incluses:")
    print("   • Système de niveaux complet")
    print("   • Modération avancée")
    print("   • Messages de bienvenue/au revoir")
    print("   • Système de logs")
    print("   • Commandes fun")
    print("   • Configuration flexible")
    print("   • Interface web intégrée")
    print("\n⚠️  N'oubliez pas d'ajouter votre token Discord dans les Secrets !")
    print("   Nom de la variable: DISCORD_BOT_TOKEN")
    
    # Le token sera récupéré depuis les secrets Replit
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        print("❌ Token Discord manquant ! Ajoutez DISCORD_BOT_TOKEN dans les Secrets.")
    else:
        # Démarrer la tâche de vérification des commandes web après que le bot soit prêt
        @bot.event
        async def on_ready():
            print(f'{bot.user} est connecté et prêt !')
            activity = discord.Game(name="!help | Bot Discord Français")
            await bot.change_presence(activity=activity)
            # Démarrer la tâche de vérification maintenant
            if not check_web_commands.is_running():
                check_web_commands.start()
        
        bot.run(token)
