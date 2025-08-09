import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import feedparser
import json
import os
import logging
from datetime import datetime, timedelta

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TwitterMonitorBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix="!ww ", intents=intents, help_command=None)

        # Stockage en mémoire (adapté pour Render)
        self.monitored_accounts = {}  # {guild_id: {channel_id: [accounts]}}
        self.last_tweet_ids = {}      # {account: last_tweet_id}
        self.guild_settings = {}      # {guild_id: settings}
        
        # Dictionnaire d'horodatage pour chaque guild
        self._last_check = {}
        
        # Comptes officiels Wuthering Waves (pré-configurés)
        self.official_accounts = {
            'Wuthering_Waves_Global': 'Compte officiel global',
            'Narushio_wuwa': 'Développeur Narushio',
            'WutheringWavesOfficialDiscord': 'Discord officiel'
        }

    async def on_ready(self):
        logger.info(f"{self.user} est connecté!")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synchronisé {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"Erreur lors de la synchronisation: {e}")
        
        if not self.monitor_twitter.is_running():
            self.monitor_twitter.start()

    async def on_guild_join(self, guild):
        """Initialise les paramètres par défaut pour un nouveau serveur"""
        self.guild_settings[guild.id] = {
            "check_interval": 300,  # 5 minutes
            "notification_role": None,
            "embed_color": 0x00d4ff,  # Couleur Wuthering Waves
            "include_retweets": False,
            "filter_keywords": []
        }
        logger.info(f"Bot ajouté au serveur: {guild.name}")

    async def on_command_error(self, ctx, error):
        """Gère les erreurs de commandes"""
        if isinstance(error, commands.CommandNotFound):
            await ctx.send(f"❌ Commande inconnue. Tapez `!ww aide` pour voir les commandes disponibles.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Vous n'avez pas les permissions nécessaires pour cette commande.")
        else:
            logger.error(f"Erreur de commande: {error}")
            await ctx.send(f"❌ Une erreur s'est produite. Vérifiez les logs.")

    # ------------------------------------------------------------------
    # API Twitter via Nitter
    # ------------------------------------------------------------------

    async def get_latest_tweet(self, handle: str) -> dict | None:
        """
        Récupère le dernier tweet via Nitter RSS.
        Méthode robuste qui évite l'API Twitter payante.
        """
        # Nitter instances (fallback si l'une ne marche pas)
        nitter_instances = [
            "https://nitter.net",
            "https://nitter.it",
            "https://nitter.privacydev.net"
        ]
        
        for instance in nitter_instances:
            url = f"{instance}/{handle}/rss"
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        
                        text = await resp.text()
                        feed = feedparser.parse(text)
                        
                        if not feed.entries:
                            continue
                            
                        entry = feed.entries[0]
                        
                        # Extraire l'ID du tweet depuis l'URL
                        tweet_id = None
                        if hasattr(entry, 'id') and entry.id:
                            tweet_id = entry.id.split("/")[-1]
                        elif hasattr(entry, 'link') and entry.link:
                            tweet_id = entry.link.split("/")[-1]
                        
                        if not tweet_id:
                            continue
                            
                        return {
                            "id": tweet_id,
                            "url": entry.link.replace("nitter", "x.com").replace("twitter.com", "x.com"),
                            "text": entry.title or entry.summary or "Nouveau tweet",
                            "created_at": datetime.utcnow(),
                            "author": handle,
                            "instance_used": instance
                        }
                        
            except Exception as e:
                logger.warning(f"Échec {instance} pour @{handle}: {e}")
                continue
        
        logger.error(f"Impossible de récupérer les tweets de @{handle} sur toutes les instances")
        return None

    async def send_tweet_notification(self, channel: discord.TextChannel, handle: str, tweet_data: dict, is_test=False):
        """Envoie une notification de nouveau tweet"""
        try:
            guild = channel.guild
            settings = self.guild_settings.get(guild.id, {})
            role_id = settings.get("notification_role")
            content = ""

            # Mention du rôle si configuré
            if role_id and not is_test:
                role = guild.get_role(role_id)
                if role:
                    content += f"{role.mention} "

            # Formatage du message
            emoji = "🧪" if is_test else "📱"
            test_prefix = "[TEST] " if is_test else ""
            
            content += f"{emoji} {test_prefix}Nouveau tweet de **@{handle}**:\n{tweet_data['url']}"
            
            # Ajouter des infos de debug en mode test
            if is_test:
                content += f"\n`Instance: {tweet_data.get('instance_used', 'Inconnue')}`"
                content += f"\n`ID: {tweet_data['id']}`"
            
            await channel.send(content)
            logger.info(f"Tweet notifié: @{handle} dans #{channel.name}")
            
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi de notification: {e}")

    # ------------------------------------------------------------------
    # Commandes
    # ------------------------------------------------------------------

    @commands.command(name="test_simple")
    async def test_simple(self, ctx):
        """Commande de test basique"""
        await ctx.send("✅ Le bot fonctionne ! Tapez `!ww aide` pour voir toutes les commandes.")

    @commands.command(name="setup")
    @commands.has_permissions(administrator=True)
    async def setup_monitoring(self, ctx, account_handle: str, channel: discord.TextChannel = None):
        """
        Configure la surveillance d'un compte Twitter
        Usage: !ww setup @Wuthering_Waves_Global #news-channel
        """
        if channel is None:
            channel = ctx.channel

        handle = account_handle.lstrip("@")
        
        # Test de connectivité au compte
        await ctx.send(f"🔍 Vérification du compte @{handle}...")
        test_tweet = await self.get_latest_tweet(handle)
        if not test_tweet:
            await ctx.send(f"❌ Impossible de trouver ou d'accéder au compte @{handle}. Vérifiez que le compte existe et est public.")
            return

        guild_id = ctx.guild.id
        chan_id = channel.id

        # Initialiser les structures
        if guild_id not in self.monitored_accounts:
            self.monitored_accounts[guild_id] = {}
        if chan_id not in self.monitored_accounts[guild_id]:
            self.monitored_accounts[guild_id][chan_id] = []
        if guild_id not in self.guild_settings:
            await self.on_guild_join(ctx.guild)

        # Vérifier les doublons
        if handle in self.monitored_accounts[guild_id][chan_id]:
            await ctx.send(f"❌ Le compte @{handle} est déjà surveillé dans {channel.mention}.")
            return

        # Ajouter à la surveillance
        self.monitored_accounts[guild_id][chan_id].append(handle)
        
        # Mémoriser le dernier tweet pour éviter le spam au démarrage
        self.last_tweet_ids[handle] = test_tweet["id"]

        embed = discord.Embed(
            title="✅ Surveillance configurée",
            description=f"Le compte **@{handle}** sera désormais surveillé dans {channel.mention}",
            color=0x00d4ff,
        )
        embed.add_field(name="Intervalle", value=f"{self.guild_settings[guild_id]['check_interval']} secondes", inline=True)
        embed.add_field(name="Dernier tweet détecté", value=f"`{test_tweet['id']}`", inline=True)
        embed.add_field(name="Instance utilisée", value=f"`{test_tweet.get('instance_used', 'Inconnue')}`", inline=True)
        
        await ctx.send(embed=embed)

    @commands.command(name="remove")
    @commands.has_permissions(administrator=True)
    async def remove_monitoring(self, ctx, account_handle: str, channel: discord.TextChannel = None):
        """
        Retire un compte de la surveillance
        Usage: !ww remove @Wuthering_Waves_Global #news-channel
        """
        if channel is None:
            channel = ctx.channel

        handle = account_handle.lstrip("@")
        guild_id = ctx.guild.id
        chan_id = channel.id

        try:
            self.monitored_accounts[guild_id][chan_id].remove(handle)
            # Nettoyer le dernier ID mémorisé
            if handle in self.last_tweet_ids:
                del self.last_tweet_ids[handle]
            await ctx.send(f"✅ Le compte @{handle} n'est plus surveillé dans {channel.mention}.")
        except (KeyError, ValueError):
            await ctx.send(f"❌ Le compte @{handle} n'était pas surveillé dans {channel.mention}.")

    @commands.command(name="list")
    async def list_monitored(self, ctx):
        """Affiche tous les comptes surveillés sur ce serveur"""
        guild_id = ctx.guild.id
        
        if guild_id not in self.monitored_accounts or not any(self.monitored_accounts[guild_id].values()):
            await ctx.send("❌ Aucun compte surveillé sur ce serveur.")
            return

        embed = discord.Embed(
            title="📱 Comptes Twitter surveillés",
            color=0x00d4ff,
            timestamp=datetime.utcnow()
        )
        
        total_accounts = 0
        for chan_id, accounts in self.monitored_accounts[guild_id].items():
            if not accounts:
                continue
                
            channel = self.get_channel(chan_id)
            channel_name = f"#{channel.name}" if channel else f"Canal supprimé ({chan_id})"
            accounts_list = "\n".join([f"• @{account}" for account in accounts])
            
            embed.add_field(name=channel_name, value=accounts_list, inline=False)
            total_accounts += len(accounts)
        
        embed.set_footer(text=f"Total: {total_accounts} compte(s) surveillé(s)")
        await ctx.send(embed=embed)

    @commands.command(name="settings")
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

        # Afficher les paramètres actuels
        if setting is None:
            settings = self.guild_settings[guild_id]
            embed = discord.Embed(title="⚙️ Paramètres actuels", color=0x00d4ff)
            embed.add_field(name="Intervalle de vérification", value=f"{settings['check_interval']} secondes", inline=True)
            embed.add_field(name="Inclure les retweets", value="Oui" if settings['include_retweets'] else "Non", inline=True)
            
            role = ctx.guild.get_role(settings['notification_role']) if settings['notification_role'] else None
            embed.add_field(name="Rôle de notification", value=role.mention if role else "Aucun", inline=True)
            
            embed.add_field(name="Commandes disponibles", value="""
            `!ww settings interval 300` - Changer l'intervalle (en secondes, min 60)
            `!ww settings retweets true/false` - Inclure les retweets
            `!ww settings role @MonRole` - Définir le rôle à mentionner
            """, inline=False)
            
            await ctx.send(embed=embed)
            return

        # Modifier un paramètre
        try:
            if setting.lower() == "interval":
                interval = int(value)
                if interval < 60:
                    await ctx.send("❌ L'intervalle minimum est de 60 secondes pour éviter le spam.")
                    return
                self.guild_settings[guild_id]["check_interval"] = interval
                await ctx.send(f"✅ Intervalle mis à jour: {interval} secondes")
                
            elif setting.lower() in ["retweets", "rt"]:
                include_rt = value.lower() in ("true", "1", "yes", "oui", "on")
                self.guild_settings[guild_id]["include_retweets"] = include_rt
                await ctx.send(f"✅ Retweets: {'Inclus' if include_rt else 'Exclus'}")
                
            elif setting.lower() == "role":
                if value.lower() in ["none", "aucun", "reset"]:
                    self.guild_settings[guild_id]["notification_role"] = None
                    await ctx.send("✅ Rôle de notification supprimé.")
                elif value.startswith("<@&") and value.endswith(">"):
                    try:
                        role_id = int(value[3:-1])
                        role = ctx.guild.get_role(role_id)
                        if role:
                            self.guild_settings[guild_id]["notification_role"] = role.id
                            await ctx.send(f"✅ Rôle de notification: {role.mention}")
                        else:
                            await ctx.send("❌ Rôle introuvable.")
                    except ValueError:
                        await ctx.send("❌ ID de rôle invalide.")
                else:
                    await ctx.send("❌ Mentionnez un rôle valide (@role) ou tapez 'none' pour supprimer.")
            else:
                await ctx.send(f"❌ Paramètre inconnu: `{setting}`. Tapez `!ww settings` pour voir les options.")
                
        except ValueError:
            await ctx.send("❌ Valeur invalide. Vérifiez le format de votre commande.")

    @commands.command(name="test")
    @commands.has_permissions(administrator=True)
    async def test_monitoring(self, ctx, account_handle: str):
        """Teste la surveillance d'un compte (récupère le dernier tweet)"""
        handle = account_handle.lstrip("@")
        
        await ctx.send(f"🔍 Test de surveillance pour @{handle}...")
        
        tweet = await self.get_latest_tweet(handle)
        if not tweet:
            await ctx.send(f"❌ Impossible de récupérer le dernier tweet de @{handle}. Vérifiez que le compte existe et est accessible.")
            return
            
        await self.send_tweet_notification(ctx.channel, handle, tweet, is_test=True)

    @commands.command(name="comptes")
    async def suggest_accounts(self, ctx):
        """Affiche les comptes officiels Wuthering Waves recommandés"""
        embed = discord.Embed(
            title="📱 Comptes officiels Wuthering Waves recommandés",
            description="Voici les principaux comptes à surveiller :",
            color=0x00d4ff
        )
        
        for handle, description in self.official_accounts.items():
            embed.add_field(
                name=f"@{handle}",
                value=f"{description}\n`!ww setup @{handle} #votre-canal`",
                inline=False
            )
        
        embed.set_footer(text="Utilisez !ww setup @compte #canal pour surveiller un compte")
        await ctx.send(embed=embed)

    @commands.command(name="status")
    async def bot_status(self, ctx):
        """Affiche le statut du bot et des surveillances actives"""
        guild_id = ctx.guild.id
        
        # Compter les surveillances
        total_accounts = 0
        total_channels = 0
        if guild_id in self.monitored_accounts:
            for accounts in self.monitored_accounts[guild_id].values():
                if accounts:
                    total_channels += 1
                    total_accounts += len(accounts)
        
        embed = discord.Embed(
            title="📊 Statut du Bot",
            color=0x00d4ff,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="🤖 Bot", value="En ligne", inline=True)
        embed.add_field(name="📱 Comptes surveillés", value=total_accounts, inline=True)
        embed.add_field(name="📺 Canaux configurés", value=total_channels, inline=True)
        
        settings = self.guild_settings.get(guild_id, {})
        embed.add_field(name="⏱️ Intervalle", value=f"{settings.get('check_interval', 300)}s", inline=True)
        embed.add_field(name="🔄 Retweets", value="Oui" if settings.get('include_retweets', False) else "Non", inline=True)
        embed.add_field(name="🏓 Surveillance", value="Active" if self.monitor_twitter.is_running() else "Inactive", inline=True)
        
        # Dernière vérification
        last_check = self._last_check.get(guild_id)
        if last_check:
            time_since = datetime.utcnow() - last_check
            embed.add_field(name="🕒 Dernière vérification", value=f"Il y a {int(time_since.total_seconds())}s", inline=True)
        
        await ctx.send(embed=embed)

    @commands.command(name="aide")
    async def help_command(self, ctx):
        """Affiche l'aide du bot"""
        embed = discord.Embed(
            title="🤖 Wuthering Waves Twitter Monitor",
            description="Bot de surveillance automatique des comptes Twitter officiels de Wuthering Waves",
            color=0x00d4ff
        )
        
        embed.add_field(
            name="📋 Commandes principales",
            value="""
            `!ww setup @compte #canal` - Surveiller un compte
            `!ww remove @compte #canal` - Arrêter la surveillance  
            `!ww list` - Voir les comptes surveillés
            `!ww test @compte` - Tester un compte
            `!ww comptes` - Comptes officiels suggérés
            """,
            inline=False
        )
        
        embed.add_field(
            name="⚙️ Configuration",
            value="""
            `!ww settings` - Voir les paramètres actuels
            `!ww settings interval 300` - Changer l'intervalle (secondes)
            `!ww settings retweets true` - Inclure les retweets
            `!ww settings role @News` - Rôle à mentionner
            """,
            inline=False
        )
        
        embed.add_field(
            name="📊 Informations",
            value="""
            `!ww status` - Statut du bot
            `!ww aide` - Afficher cette aide
            """,
            inline=False
        )
        
        embed.set_footer(text="Hébergé gratuitement sur Render • Développé pour la communauté WW")
        
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Tâche de surveillance
    # ------------------------------------------------------------------

    @tasks.loop(seconds=60)  # Vérification toutes les minutes
    async def monitor_twitter(self):
        """Boucle principale de surveillance des comptes Twitter"""
        now = datetime.utcnow()
        
        for guild_id, channels in self.monitored_accounts.items():
            # Vérifier si c'est le moment de checker selon l'intervalle configuré
            settings = self.guild_settings.get(guild_id, {})
            interval = settings.get("check_interval", 300)

            last_check = self._last_check.get(guild_id, now - timedelta(seconds=interval))
            if (now - last_check).total_seconds() < interval:
                continue

            # Mettre à jour le timestamp
            self._last_check[guild_id] = now
            logger.info(f"Vérification des tweets pour le serveur {guild_id}")

            for chan_id, accounts in channels.items():
                if not accounts:
                    continue

                channel = self.get_channel(chan_id)
                if not channel:
                    logger.warning(f"Canal {chan_id} introuvable, nettoyage recommandé")
                    continue

                for handle in accounts:
                    try:
                        tweet = await self.get_latest_tweet(handle)
                        if not tweet:
                            logger.warning(f"Pas de tweet récupéré pour @{handle}")
                            continue

                        # Vérifier si c'est un nouveau tweet
                        last_id = self.last_tweet_ids.get(handle)
                        if tweet["id"] != last_id:
                            # Filtrer les retweets si nécessaire
                            if not settings.get("include_retweets", False):
                                if tweet["text"].lower().startswith(("rt @", "retweet")):
                                    logger.info(f"Retweet ignoré de @{handle}: {tweet['id']}")
                                    self.last_tweet_ids[handle] = tweet["id"]  # Marquer comme vu
                                    continue

                            await self.send_tweet_notification(channel, handle, tweet)
                            self.last_tweet_ids[handle] = tweet["id"]
                            
                            # Anti-spam entre les notifications
                            await asyncio.sleep(2)

                    except Exception as e:
                        logger.error(f"Erreur lors de la surveillance de @{handle}: {e}")

                # Anti-spam entre les comptes
                await asyncio.sleep(1)

    @monitor_twitter.before_loop
    async def before_monitor_twitter(self):
        """Attendre que le bot soit prêt avant de commencer la surveillance"""
        await self.wait_until_ready()
        logger.info("Surveillance Twitter démarrée")

    @monitor_twitter.error
    async def monitor_twitter_error(self, error):
        """Gère les erreurs de la boucle de surveillance"""
        logger.error(f"Erreur dans la boucle de surveillance: {error}")
        # Redémarrer la boucle après une pause
        await asyncio.sleep(60)
        if not self.monitor_twitter.is_running():
            self.monitor_twitter.restart()

# ------------------------------------------------------------------
# Point d'entrée du bot
# ------------------------------------------------------------------

if __name__ == "__main__":
    bot = TwitterMonitorBot()
    
    # Configuration du token depuis les variables d'environnement
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("❌ ERREUR: Token Discord manquant!")
        logger.error("Ajoutez votre token dans la variable d'environnement DISCORD_BOT_TOKEN")
        raise RuntimeError("La variable d'environnement DISCORD_BOT_TOKEN est manquante.")
    
    logger.info("🚀 Démarrage du bot Wuthering Waves Twitter Monitor...")
    
    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"❌ Erreur critique lors du démarrage: {e}")
        raise
