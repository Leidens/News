import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import re
from datetime import datetime, timedelta
import logging
import os
from dotenv import load_dotenv

# Charger les variables d'environnement
load_dotenv()

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TwitterMonitorBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!ww ', intents=intents)
        
        # Stockage en mémoire (pas de localStorage dans les bots Discord)
        self.monitored_accounts = {}  # {guild_id: {channel_id: [accounts]}}
        self.last_tweet_ids = {}      # {account: last_tweet_id}
        self.guild_settings = {}      # {guild_id: settings}
        
        # Liste des comptes officiels Wuthering Waves (pré-configurés)
        self.official_accounts = {
            'Wuthering_Waves_Global': '@Wuthering_Waves_Global',
            'Narushio_wuwa': '@Narushio_wuwa',
            'WutheringWavesOfficialDiscord': '@WutheringWavesOfficialDiscord'
        }
    
    async def on_ready(self):
        logger.info(f'{self.user} est connecté!')
        self.monitor_twitter.start()
    
    async def on_guild_join(self, guild):
        """Initialise les paramètres par défaut pour un nouveau serveur"""
        self.guild_settings[guild.id] = {
            'check_interval': 300,  # 5 minutes par défaut
            'notification_role': None,
            'embed_color': 0x00d4ff,  # Couleur Wuthering Waves
            'include_retweets': False,
            'filter_keywords': []
        }
    
    @commands.command(name='setup')
    @commands.has_permissions(administrator=True)
    async def setup_monitoring(self, ctx, account_handle: str, channel: discord.TextChannel = None):
        """
        Configure la surveillance d'un compte Twitter
        Usage: !ww setup @Wuthering_Waves_Global #news-channel
        """
        if channel is None:
            channel = ctx.channel
        
        # Nettoyer le handle (enlever @ si présent)
        account_handle = account_handle.replace('@', '')
        
        guild_id = ctx.guild.id
        channel_id = channel.id
        
        # Initialiser les structures si nécessaire
        if guild_id not in self.monitored_accounts:
            self.monitored_accounts[guild_id] = {}
        if channel_id not in self.monitored_accounts[guild_id]:
            self.monitored_accounts[guild_id][channel_id] = []
        
        # Vérifier si le compte n'est pas déjà surveillé
        if account_handle in self.monitored_accounts[guild_id][channel_id]:
            await ctx.send(f"❌ Le compte @{account_handle} est déjà surveillé dans {channel.mention}")
            return
        
        # Ajouter le compte à la surveillance
        self.monitored_accounts[guild_id][channel_id].append(account_handle)
        
        embed = discord.Embed(
            title="✅ Surveillance configurée",
            description=f"Le compte **@{account_handle}** sera maintenant surveillé dans {channel.mention}",
            color=0x00d4ff
        )
        embed.add_field(name="Intervalle de vérification", value="5 minutes", inline=True)
        embed.add_field(name="Prochaine vérification", value="Dans 5 minutes", inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='remove')
    @commands.has_permissions(administrator=True)
    async def remove_monitoring(self, ctx, account_handle: str, channel: discord.TextChannel = None):
        """
        Retire un compte de la surveillance
        Usage: !ww remove @Wuthering_Waves_Global #news-channel
        """
        if channel is None:
            channel = ctx.channel
        
        account_handle = account_handle.replace('@', '')
        guild_id = ctx.guild.id
        channel_id = channel.id
        
        try:
            self.monitored_accounts[guild_id][channel_id].remove(account_handle)
            await ctx.send(f"✅ Le compte @{account_handle} n'est plus surveillé dans {channel.mention}")
        except (KeyError, ValueError):
            await ctx.send(f"❌ Le compte @{account_handle} n'était pas surveillé dans {channel.mention}")
    
    @commands.command(name='list')
    async def list_monitored(self, ctx):
        """Affiche tous les comptes surveillés sur ce serveur"""
        guild_id = ctx.guild.id
        
        if guild_id not in self.monitored_accounts or not self.monitored_accounts[guild_id]:
            await ctx.send("❌ Aucun compte n'est actuellement surveillé sur ce serveur.")
            return
        
        embed = discord.Embed(
            title="📱 Comptes Twitter surveillés",
            color=0x00d4ff,
            timestamp=datetime.utcnow()
        )
        
        for channel_id, accounts in self.monitored_accounts[guild_id].items():
            if accounts:
                channel = self.get_channel(channel_id)
                channel_name = channel.mention if channel else f"Canal supprimé ({channel_id})"
                accounts_list = "\n".join([f"• @{account}" for account in accounts])
                embed.add_field(
                    name=f"#{channel.name if channel else 'Canal supprimé'}",
                    value=accounts_list,
                    inline=False
                )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='settings')
    @commands.has_permissions(administrator=True)
    async def configure_settings(self, ctx, setting: str = None, *, value: str = None):
        """
        Configure les paramètres du bot
        Usage: !ww settings interval 300
        Usage: !ww settings retweets true
        Usage: !ww settings role @News
        """
        guild_id = ctx.guild.id
        
        if guild_id not in self.guild_settings:
            await self.on_guild_join(ctx.guild)
        
        if setting is None:
            # Afficher les paramètres actuels
            settings = self.guild_settings[guild_id]
            embed = discord.Embed(
                title="⚙️ Paramètres actuels",
                color=0x00d4ff
            )
            embed.add_field(name="Intervalle (secondes)", value=settings['check_interval'], inline=True)
            embed.add_field(name="Inclure les retweets", value=settings['include_retweets'], inline=True)
            embed.add_field(name="Rôle de notification", value=settings['notification_role'] or "Aucun", inline=True)
            
            await ctx.send(embed=embed)
            return
        
        # Modifier un paramètre
        if setting.lower() == 'interval':
            try:
                interval = int(value)
                if interval < 60:
                    await ctx.send("❌ L'intervalle minimum est de 60 secondes")
                    return
                self.guild_settings[guild_id]['check_interval'] = interval
                await ctx.send(f"✅ Intervalle mis à jour: {interval} secondes")
            except ValueError:
                await ctx.send("❌ L'intervalle doit être un nombre en secondes")
        
        elif setting.lower() == 'retweets':
            self.guild_settings[guild_id]['include_retweets'] = value.lower() in ['true', '1', 'yes', 'oui']
            await ctx.send(f"✅ Retweets: {'Inclus' if self.guild_settings[guild_id]['include_retweets'] else 'Exclus'}")
        
        elif setting.lower() == 'role':
            if value.startswith('<@&') and value.endswith('>'):
                role_id = int(value[3:-1])
                role = ctx.guild.get_role(role_id)
                if role:
                    self.guild_settings[guild_id]['notification_role'] = role.id
                    await ctx.send(f"✅ Rôle de notification: {role.mention}")
                else:
                    await ctx.send("❌ Rôle introuvable")
            else:
                await ctx.send("❌ Merci de mentionner un rôle valide (@role)")
    
    @commands.command(name='test')
    @commands.has_permissions(administrator=True)
    async def test_monitoring(self, ctx, account_handle: str):
        """Teste la surveillance d'un compte (récupère le dernier tweet)"""
        account_handle = account_handle.replace('@', '')
        
        await ctx.send(f"🔍 Test de surveillance pour @{account_handle}...")
        
        # Simuler la récupération d'un tweet (à remplacer par votre API)
        tweet_data = await self.get_latest_tweet(account_handle)
        
        if tweet_data:
            await self.send_tweet_notification(ctx.channel, account_handle, tweet_data, is_test=True)
        else:
            await ctx.send(f"❌ Impossible de récupérer les tweets de @{account_handle}")
    
    async def get_latest_tweet(self, account_handle):
        """
        Récupère le dernier tweet d'un compte
        IMPORTANT: Cette fonction utilise un service externe pour éviter l'API Twitter payante
        """
        try:
            # Option 1: Utiliser un service gratuit comme Nitter
            nitter_url = f"https://nitter.net/{account_handle}/rss"
            async with aiohttp.ClientSession() as session:
                async with session.get(nitter_url) as resp:
                    if resp.status == 200:
                        # Parser le RSS (simulation)
                        # En réalité, vous devriez utiliser feedparser
                        return {
                            'id': '1950390998208860464',
                            'url': f'https://x.com/{account_handle}/status/1950390998208860464',
                            'text': 'Nouveau contenu Wuthering Waves!',
                            'created_at': datetime.utcnow(),
                            'author': account_handle
                        }
        except Exception as e:
            logger.error(f"Erreur lors de la récupération du tweet: {e}")
        
        return None
    
    async def send_tweet_notification(self, channel, account_handle, tweet_data, is_test=False):
        """Envoie une notification de nouveau tweet"""
        try:
            # Message simple avec le lien (Discord créera automatiquement l'embed)
            content = ""
            
            # Ajouter mention du rôle si configuré
            guild_settings = self.guild_settings.get(channel.guild.id, {})
            notification_role = guild_settings.get('notification_role')
            if notification_role and not is_test:
                role = channel.guild.get_role(notification_role)
                if role:
                    content = f"{role.mention} "
            
            # Message principal
            if is_test:
                content += f"🧪 **[TEST]** Nouveau tweet de @{account_handle}:\n{tweet_data['url']}"
            else:
                content += f"📱 Nouveau tweet de @{account_handle}:\n{tweet_data['url']}"
            
            await channel.send(content)
            
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi de notification: {e}")
    
    @tasks.loop(seconds=60)  # Vérification toutes les minutes
    async def monitor_twitter(self):
        """Boucle principale de surveillance des comptes Twitter"""
        for guild_id, channels in self.monitored_accounts.items():
            guild_settings = self.guild_settings.get(guild_id, {})
            check_interval = guild_settings.get('check_interval', 300)
            
            # Vérifier si c'est le moment de checker (selon l'intervalle configuré)
            current_time = datetime.utcnow()
            last_check = getattr(self, '_last_check', {})
            
            if guild_id not in last_check:
                last_check[guild_id] = current_time - timedelta(seconds=check_interval)
            
            if (current_time - last_check[guild_id]).total_seconds() < check_interval:
                continue
            
            # Mettre à jour le timestamp de dernière vérification
            last_check[guild_id] = current_time
            self._last_check = last_check
            
            for channel_id, accounts in channels.items():
                if not accounts:
                    continue
                
                channel = self.get_channel(channel_id)
                if not channel:
                    continue
                
                for account in accounts:
                    try:
                        tweet_data = await self.get_latest_tweet(account)
                        if tweet_data:
                            # Vérifier si c'est un nouveau tweet
                            last_id = self.last_tweet_ids.get(account)
                            if tweet_data['id'] != last_id:
                                await self.send_tweet_notification(channel, account, tweet_data)
                                self.last_tweet_ids[account] = tweet_data['id']
                        
                        # Petit délai pour éviter le spam
                        await asyncio.sleep(2)
                        
                    except Exception as e:
                        logger.error(f"Erreur lors de la surveillance de @{account}: {e}")
    
    @commands.command(name='help')
    async def help_command(self, ctx):
        """Affiche l'aide du bot"""
        embed = discord.Embed(
            title="🤖 Wuthering Waves Twitter Bot",
            description="Bot de surveillance des comptes Twitter officiels de Wuthering Waves",
            color=0x00d4ff
        )
        
        embed.add_field(
            name="📋 Commandes principales",
            value="""
            `!ww setup @compte #canal` - Configure la surveillance
            `!ww remove @compte #canal` - Retire la surveillance  
            `!ww list` - Liste les comptes surveillés
            `!ww settings` - Affiche les paramètres
            `!ww test @compte` - Test la surveillance
            """,
            inline=False
        )
        
        embed.add_field(
            name="⚙️ Configuration",
            value="""
            `!ww settings interval 300` - Intervalle en secondes
            `!ww settings retweets true` - Inclure les retweets
            `!ww settings role @News` - Rôle à mentionner
            """,
            inline=False
        )
        
        embed.add_field(
            name="📱 Comptes officiels suggérés",
            value="""
            @Wuthering_Waves_Global
            @Narushio_wuwa
            @WutheringWavesOfficialDiscord
            """,
            inline=False
        )
        
        embed.set_footer(text="Développé pour la communauté Wuthering Waves")
        
        await ctx.send(embed=embed)

# Point d'entrée du bot
if __name__ == "__main__":
    bot = TwitterMonitorBot()
    
    # Le token sera fourni par Render via les variables d'environnement
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        print("❌ ERREUR: Token Discord manquant!")
        print("Configurez la variable DISCORD_BOT_TOKEN dans Render")
        exit(1)
    
    print("🚀 Démarrage du bot...")
    bot.run(TOKEN)