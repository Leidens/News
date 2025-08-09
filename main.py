import os
import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
import feedparser
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ----------------------------------------------
# Configuration & logging
# ----------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("twitter_monitor")

load_dotenv()  # charge les variables d'environnement depuis .env


class TwitterMonitorBot(commands.Bot):
    """
    Bot Discord dédié à la surveillance de comptes Twitter.
    Utilise le flux RSS de Nitter (https://nitter.net) pour éviter l'API officielle.
    """

    def __init__(self, *, command_prefix: str = "!ww ", intents: discord.Intents | None = None):
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.guilds = True

        super().__init__(command_prefix=command_prefix, intents=intents, help_command=None)

        # ------------------------------------------------------------------
        # Stockage en mémoire (pas de persistance – à adapter si besoin)
        # ------------------------------------------------------------------
        self.monitored_accounts: dict[int, dict[int, set[str]]] = {}  # guild_id -> channel_id -> {accounts}
        self.last_tweet_ids: dict[str, str] = {}                       # account_handle -> tweet_id
        self.guild_settings: dict[int, dict] = {}                     # guild_id -> settings

        # ------------------------------------------------------------------
        # Accès aux comptes officiels (pour l’aide)
        # ------------------------------------------------------------------
        self.official_accounts = {
            "Wuthering_Waves_Global": "@Wuthering_Waves_Global",
            "Narushio_wuwa": "@Narushio_wuwa",
            "WutheringWavesOfficialDiscord": "@WutheringWavesOfficialDiscord",
        }

    # -------------------------------------------------------------
    #  Bot lifecycle
    # -------------------------------------------------------------
    async def on_ready(self):
        log.info(f"{self.user} connecté !")
        try:
            synced = await self.tree.sync()
            log.info(f"Synchronisé {len(synced)} slash commands.")
        except Exception as e:
            log.error("Erreur de synchronisation des slash commands", exc_info=e)

        if not self.monitor_twitter.is_running():
            self.monitor_twitter.start()

    async def on_guild_join(self, guild: discord.Guild):
        """Initialise les paramètres par défaut pour un nouveau serveur."""
        self.guild_settings[guild.id] = {
            "check_interval": 300,          # secondes
            "include_retweets": False,
            "notification_role": None,      # rôle à mentionner
            "embed_color": 0x00d4ff,
        }

    async def on_command_error(self, ctx: commands.Context, error):
        """Gestion globale des erreurs de commandes."""
        if isinstance(error, commands.CommandNotFound):
            await ctx.send(
                f"❌ Commande inconnue. Tapez `{self.command_prefix}aide` pour voir les commandes disponibles."
            )
        else:
            log.error("Erreur de commande", exc_info=error)
            await ctx.send(f"❌ Une erreur s'est produite : {error}")

    # -------------------------------------------------------------
    #  Commandes
    # -------------------------------------------------------------

    @commands.command(name="test_simple")
    async def test_simple(self, ctx: commands.Context):
        """Commande de test basique."""
        await ctx.send("✅ Le bot fonctionne ! Tapez `!ww aide` pour voir toutes les commandes.")

    @commands.command(name="setup")
    @commands.has_permissions(administrator=True)
    async def setup_monitoring(
        self,
        ctx: commands.Context,
        account_handle: str,
        channel: discord.TextChannel | None = None,
    ):
        """
        Configure la surveillance d’un compte Twitter.
        Usage : !ww setup @Wuthering_Waves_Global #news
        """
        if channel is None:
            channel = ctx.channel

        account_handle = account_handle.lstrip("@")

        guild_id, channel_id = ctx.guild.id, channel.id
        self.monitored_accounts.setdefault(guild_id, {}).setdefault(channel_id, set())

        if account_handle in self.monitored_accounts[guild_id][channel_id]:
            await ctx.send(
                f"❌ Le compte @{account_handle} est déjà surveillé dans {channel.mention}"
            )
            return

        self.monitored_accounts[guild_id][channel_id].add(account_handle)

        embed = discord.Embed(
            title="✅ Surveillance configurée",
            description=f"Le compte **@{account_handle}** sera maintenant surveillé dans {channel.mention}",
            color=0x00d4ff,
        )
        embed.add_field(name="Intervalle de vérification", value="5 minutes", inline=True)
        embed.add_field(name="Prochaine vérification", value="Dans 5 minutes", inline=True)

        await ctx.send(embed=embed)

    @commands.command(name="remove")
    @commands.has_permissions(administrator=True)
    async def remove_monitoring(
        self,
        ctx: commands.Context,
        account_handle: str,
        channel: discord.TextChannel | None = None,
    ):
        """Retire un compte de la surveillance."""
        if channel is None:
            channel = ctx.channel

        account_handle = account_handle.lstrip("@")
        guild_id, channel_id = ctx.guild.id, channel.id

        try:
            self.monitored_accounts[guild_id][channel_id].remove(account_handle)
            await ctx.send(f"✅ Le compte @{account_handle} n'est plus surveillé dans {channel.mention}")
        except (KeyError, ValueError):
            await ctx.send(
                f"❌ Le compte @{account_handle} n'était pas surveillé dans {channel.mention}"
            )

    @commands.command(name="list")
    async def list_monitored(self, ctx: commands.Context):
        """Affiche tous les comptes surveillés sur ce serveur."""
        guild_id = ctx.guild.id
        if not self.monitored_accounts.get(guild_id):
            await ctx.send("❌ Aucun compte n'est actuellement surveillé sur ce serveur.")
            return

        embed = discord.Embed(
            title="📱 Comptes Twitter surveillés",
            color=0x00d4ff,
            timestamp=datetime.utcnow(),
        )

        for channel_id, accounts in self.monitored_accounts[guild_id].items():
            if not accounts:
                continue
            channel = self.get_channel(channel_id)
            channel_name = channel.mention if channel else f"Canal supprimé ({channel_id})"
            accounts_list = "\n".join([f"• @{acct}" for acct in sorted(accounts)])
            embed.add_field(name=channel_name, value=accounts_list, inline=False)

        await ctx.send(embed=embed)

    @commands.command(name="settings")
    @commands.has_permissions(administrator=True)
    async def configure_settings(self, ctx: commands.Context, setting: str | None = None, *, value: str | None = None):
        """Configure les paramètres du bot."""
        guild_id = ctx.guild.id
        self.guild_settings.setdefault(guild_id, {
            "check_interval": 300,
            "include_retweets": False,
            "notification_role": None,
            "embed_color": 0x00d4ff,
        })

        settings = self.guild_settings[guild_id]

        if setting is None:
            # Afficher les paramètres actuels
            embed = discord.Embed(title="⚙️ Paramètres actuels", color=0x00d4ff)
            embed.add_field(name="Intervalle (s)", value=settings["check_interval"], inline=True)
            embed.add_field(
                name="Inclure retweets",
                value=str(settings["include_retweets"]),
                inline=True,
            )
            role = ctx.guild.get_role(settings["notification_role"]) if settings["notification_role"] else None
            embed.add_field(name="Rôle de notification", value=role.mention if role else "Aucun", inline=True)
            await ctx.send(embed=embed)
            return

        # Modification d’un paramètre
        setting = setting.lower()
        if setting == "interval":
            try:
                interval = int(value)
                if interval < 60:
                    raise ValueError("Intervalle trop court")
                settings["check_interval"] = interval
                await ctx.send(f"✅ Intervalle mis à jour : {interval} secondes.")
            except Exception as e:
                await ctx.send(f"❌ Erreur : {e}")

        elif setting == "retweets":
            val = value.lower() in {"true", "1", "yes", "oui"}
            settings["include_retweets"] = val
            await ctx.send(f"✅ Retweets : {'Inclus' if val else 'Exclus'}.")

        elif setting == "role":
            if not (value and value.startswith("<@&") and value.endswith(">")):
                await ctx.send("❌ Merci de mentionner un rôle valide (@role).")
                return
            role_id = int(value[3:-1])
            role_obj = ctx.guild.get_role(role_id)
            if role_obj is None:
                await ctx.send("❌ Rôle introuvable.")
                return
            settings["notification_role"] = role_id
            await ctx.send(f"✅ Rôle de notification : {role_obj.mention}")

        else:
            await ctx.send(f"❌ Paramètre inconnu : `{setting}`.")

    @commands.command(name="test")
    @commands.has_permissions(administrator=True)
    async def test_monitoring(self, ctx: commands.Context, account_handle: str):
        """Teste la surveillance d'un compte (récupère le dernier tweet)."""
        account_handle = account_handle.lstrip("@")
        await ctx.send(f"🔍 Test de surveillance pour @{account_handle}…")

        tweet_data = await self.get_latest_tweet(account_handle)
        if not tweet_data:
            await ctx.send(f"❌ Impossible de récupérer les tweets de @{account_handle}.")
            return

        await self.send_tweet_notification(ctx.channel, account_handle, tweet_data, is_test=True)

    @commands.command(name="aide")
    async def help_command(self, ctx: commands.Context):
        """Affiche l'aide du bot."""
        embed = discord.Embed(
            title="🤖 Wuthering Waves Twitter Bot",
            description="Bot de surveillance des comptes Twitter officiels de Wuthering Waves.",
            color=0x00d4ff,
        )

        embed.add_field(
            name="📋 Commandes principales",
            value="""
`!ww setup @compte #canal` - Configure la surveillance
`!ww remove @compte #canal` - Retire la surveillance  
`!ww list` - Liste les comptes surveillés
`!ww settings` - Affiche les paramètres actuels
`!ww test @compte` - Test de surveillance
""",
            inline=False,
        )
        embed.add_field(
            name="⚙️ Configuration avancée",
            value="""
`!ww settings interval 300` - Intervalle en secondes (minimum 60)
`!ww settings retweets true` - Inclure les retweets
`!ww settings role @News` - Rôle à mentionner lors d’une notification
""",
            inline=False,
        )
        embed.add_field(
            name="📱 Comptes officiels suggérés",
            value="\n".join(self.official_accounts.values()),
            inline=False,
        )
        embed.set_footer(text="Développé pour la communauté Wuthering Waves")
        await ctx.send(embed=embed)

    # -------------------------------------------------------------
    #  Core logic
    # -------------------------------------------------------------

    async def get_latest_tweet(self, account_handle: str) -> dict | None:
        """
        Récupère le dernier tweet d’un compte via Nitter RSS.
        Retourne un dictionnaire contenant id, url, text, created_at et author.
        """
        nitter_url = f"https://nitter.net/{account_handle}/rss"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(nitter_url) as resp:
                    if resp.status != 200:
                        log.warning(f"RSS {nitter_url} retourné {resp.status}")
                        return None
                    raw = await resp.text()

            feed = feedparser.parse(raw)
            if not feed.entries:
                log.debug(f"Aucun tweet trouvé pour @{account_handle}.")
                return None

            entry = feed.entries[0]  # le plus récent
            tweet_id = entry.id.split("/")[-1]
            tweet_url = entry.link
            tweet_text = entry.title  # title contient le texte du tweet
            created_at = datetime.strptime(entry.published, "%a, %d %b %Y %H:%M:%S %z")
            return {
                "id": tweet_id,
                "url": tweet_url,
                "text": tweet_text,
                "created_at": created_at,
                "author": account_handle,
            }

        except Exception as e:
            log.error(f"Erreur lors de la récupération du tweet pour @{account_handle}", exc_info=e)
            return None

    async def send_tweet_notification(
        self, channel: discord.TextChannel, account_handle: str, tweet_data: dict, *, is_test=False
    ):
        """Envoie une notification dans Discord."""
        try:
            # Mention de rôle si configuré
            guild_settings = self.guild_settings.get(channel.guild.id, {})
            role_id = guild_settings.get("notification_role")
            mention = ""
            if role_id and not is_test:
                role_obj = channel.guild.get_role(role_id)
                if role_obj:
                    mention = f"{role_obj.mention} "

            # Titre
            title = "🧪 [TEST] Nouveau tweet" if is_test else "📱 Nouveau tweet"

            embed = discord.Embed(
                title=title,
                description=f"[@{account_handle}]({tweet_data['url']})\n\n{tweet_data['text']}",
                color=guild_settings.get("embed_color", 0x00d4ff),
                timestamp=tweet_data["created_at"],
            )
            embed.set_footer(text="Twitter via Nitter")

            await channel.send(mention + embed.to_dict()["title"], embed=embed)
        except Exception as e:
            log.error(f"Erreur lors de l'envoi d'une notification", exc_info=e)

    # -------------------------------------------------------------
    #  Monitoring loop
    # -------------------------------------------------------------

    @tasks.loop(seconds=60.0)  # vérifie chaque minute (le check_interval décide du délai réel)
    async def monitor_twitter(self):
        """Boucle principale de surveillance."""
        now = datetime.utcnow()
        for guild_id, channels in self.monitored_accounts.items():
            settings = self.guild_settings.get(guild_id, {})
            interval_sec = settings.get("check_interval", 300)

            # Vérifie si l'intervalle est respecté
            last_check_key = f"_last_checked_{guild_id}"
            if getattr(self, last_check_key, None):
                elapsed = (now - getattr(self, last_check_key)).total_seconds()
                if elapsed < interval_sec:
                    continue

            # Met à jour le timestamp de la dernière vérification
            setattr(self, last_check_key, now)

            for channel_id, accounts in channels.items():
                channel = self.get_channel(channel_id)
                if not channel:
                    continue  # canal inexistant (ex: supprimé depuis)

                for account in accounts:
                    try:
                        tweet_data = await self.get_latest_tweet(account)
                        if not tweet_data:
                            continue

                        last_id = self.last_tweet_ids.get(account)
                        if tweet_data["id"] == last_id:
                            continue  # pas de nouveau tweet

                        # Vérification du filtre retweets
                        include_retweets = settings.get("include_retweets", False)
                        is_retweet = tweet_data["text"].startswith("RT ")
                        if not include_retweets and is_retweet:
                            self.last_tweet_ids[account] = tweet_data["id"]
                            continue

                        await self.send_tweet_notification(channel, account, tweet_data)
                        self.last_tweet_ids[account] = tweet_data["id"]

                    except Exception as e:
                        log.error(f"Erreur de surveillance pour @{account}", exc_info=e)

                # Petite pause entre les comptes pour éviter le spam
                await asyncio.sleep(2)


# -------------------------------------------------------------
#  Entrée principale
# -------------------------------------------------------------
if __name__ == "__main__":
    bot = TwitterMonitorBot()

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "❌ Impossible de démarrer le bot : DISCORD_BOT_TOKEN manquant dans les variables d'environnement."
        )

    try:
        bot.run(token)
    except KeyboardInterrupt:
        log.info("Arrêt du bot demandé (Ctrl+C).")

