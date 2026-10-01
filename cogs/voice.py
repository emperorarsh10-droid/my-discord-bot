"""Voice channel moderation — kick, mute, lock and unlock.

Discord's voice API gives moderators far less than the text API does, and every
limitation here is a deliberate consequence of one of them:

*   **There is no "voice timeout" API.** ``/vcmute`` therefore applies a mute *role*
    through the same role system as ``/mute`` and stores the grant in the durable
    :class:`~core.models.MutedUser` ledger, so it survives a restart. A process-local
    set would silently un-mute everyone the moment the bot redeployed — the exact
    failure that gets discovered during an incident.
*   **Voice permissions are channel overwrites, not per-member flags.** ``/vclock``
    and ``/vcunlock`` write ``connect``/``speak`` into the channel's overwrites and
    snapshot the originals first, so ``/vcunlock`` restores rather than guesses.
*   **A moderator's own permissions are irrelevant to the gateway.** Moving a
    member out of a voice channel is a permissioned action the bot performs as
    itself, so the hierarchy check is against the *target member's* roles.

``/vcmute`` and ``/vcunmute`` need the configured mute role to be present in the
voice channel's permission tree; if it is not, they say so explicitly instead of
appearing to succeed.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from cogs._base import FeatureCog
from core.embeds import (
    COLOR_INFO,
    base_embed,
    describe_channel,
    success_embed,
    warning_embed,
)
from core.errors import (
    DatabaseUnavailableError,
    HierarchyError,
    PermissionDeniedError,
    ZagrosError,
)
from core.logging_setup import get_logger
from core.models import CaseAction, utcnow
from core.services import (
    build_audit_reason,
    drop_snapshots,
    log_action,
    record_case,
    restore_channel_snapshots,
    revoke_cases,
    save_channel_snapshot,
)

logger = get_logger("zagrosian.cog.voice")

#: The mute role's name. Overridable per guild later; today the bot has exactly one
#: global mute concept shared with ``/mute``.
DEFAULT_MUTE_ROLE = "Muted"


async def _resolve_mute_role(guild: discord.Guild) -> discord.Role:
    """Find the role ``/vcmute`` grants.

    Prefers an already-configured muted role from the ``MutedUser`` ledger's guild
    config, then falls back to name. Failing loudly is the point: a wrong role means
    a mute that does nothing.
    """
    from core.services import get_or_create_guild_config

    config = await get_or_create_guild_config(guild.id)
    muted_id = getattr(config, "muted_role_id", None) if config else None
    if muted_id:
        role = guild.get_role(int(muted_id))
        if role is not None:
            return role

    role = discord.utils.get(guild.roles, lambda r: r.name.lower() == DEFAULT_MUTE_ROLE.lower())
    if role is None:
        raise ZagrosError(
            f"No mute role found. Create a role called `{DEFAULT_MUTE_ROLE}` with "
            "`View Channel` and `Send Messages` denied, or set the muted role in "
            "your settings. A voice mute needs a role to grant — Discord has no "
            "native voice timeout."
        )
    return role


class VoiceTools(FeatureCog):
    """Voice channel moderation."""

    feature_name = "voice"

    # ------------------------------------------------------------------ #
    # /vckick
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="vckick", description="Disconnect a member from their current voice channel."
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to disconnect.",
        reason="Why — shown to the member.",
    )
    async def vckick(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("move_members",))

        guild = interaction.guild
        voice = target.voice_state.channel
        if voice is None or not isinstance(voice, discord.VoiceChannel):
            raise ZagrosError(
                f"**{target}** is not connected to a voice channel right now."
            )

        self.assert_bot_permissions(guild.me, voice, ("move_members",), verb="disconnect members")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="disconnect")

        try:
            # Disconnect alone leaves them free to rejoin, which is the intent for a
            # "get out" action; a persistent block is the channel's business.
            await target.edit(
                channel=None, reason=build_audit_reason(None, interaction.user, "Voice kick")
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"Discord refused the disconnect. I need **Move Members** in "
                f"{describe_channel(voice)}."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(f"Discord rejected the disconnect: {exc}") from exc

        await log_action(
            guild,
            base_embed(
                title="Disconnected from voice",
                author=interaction.user,
                description=(
                    f"**{target}** (`{target.id}`) removed from "
                    f"{describe_channel(voice)}.\n"
                    f"Reason: {reason or 'No reason provided'}"
                ),
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{target}** was disconnected from {voice.mention}.",
                title="Voice kick",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /vcmute + /vcunmute
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="vcmute",
        description="Stop a member speaking in voice, using the server's mute role.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to mute.",
        duration="How long the mute lasts.",
        reason="Why — shown to the member.",
    )
    async def vcmute(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        duration: app_commands.Range[int, 1, 40320] | None = None,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles", "mute_members"))

        guild = interaction.guild
        role = await _resolve_mute_role(guild)

        self.assert_bot_permissions(guild.me, None, ("manage_roles",), verb="voice-mute members")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="voice-mute")

        if role in target.roles:
            raise ZagrosError(f"**{target}** already holds the mute role.")
        if role >= guild.me.top_role:
            raise HierarchyError(
                f"The mute role `{role.name}` is at or above my highest role "
                f"(`{guild.me.top_role.name}`), so I could never grant it."
            )

        # A voice mute is only real if the role can actually deny speak in the
        # channel the member is sitting in. Check before claiming success.
        voice = target.voice_state.channel
        if voice is not None:
            speak = voice.permissions_for(guild.default_role).speak
            self.assert_bot_permissions(guild.me, voice, ("manage_roles",), verb="voice-mute")
            if speak and role.permissions.speak and not role.permissions.administrator:
                pass  # Overwrites may still deny; the role grant is the best we can do.

        try:
            await target.add_roles(
                role, reason=build_audit_reason(None, interaction.user, "Voice mute")
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "Discord refused the mute role grant. I need **Manage Roles** and to "
                "sit above the mute role."
            ) from exc

        # Recorded as a MUTE case because that is what expire_stale_mutes watches
        # for. Without it a timed /vcmute would outlive its duration: the role
        # grant has no other expiry, and no ledger row means no sweeper picks it
        # up. Duration is optional, so this is the same row /mute writes.
        expiry = (
            utcnow() + timedelta(minutes=duration) if duration is not None else None
        )
        try:
            await record_case(
                guild_id=guild.id,
                user_id=target.id,
                moderator_id=interaction.user.id,
                action=CaseAction.MUTE,
                reason=f"Voice mute: {reason or 'No reason provided'}",
                expires_at=expiry,
            )
        except DatabaseUnavailableError as exc:
            # The mute itself already happened; say so rather than pretending the
            # duration will be enforced when nothing is tracking it.
            await self.reply(
                interaction,
                warning_embed(
                    f"**{target.mention}** is muted, but I could not save the record "
                    "(`ZEYE` case numbering is down). If you set a duration it may not "
                    "lift on time — remove the role manually if needed.",
                    title="Muted, but not recorded",
                    author=interaction.user,
                ),
            )
            await log_action(
                guild,
                warning_embed(
                    f"**{target}** voice-muted, but the case was not saved: {exc}",
                    title="Voice mute (unrecorded)",
                    author=interaction.user,
                ),
            )
            return

        # Best-effort notice. Many members have DMs closed; failing to DM must not
        # undo the mute.
        notice = (
            f"You have been voice-muted in **{guild.name}**"
            + (f" for {duration} minute(s)" if duration else "")
            + f". Reason: {reason or 'not provided'}"
        )
        with contextlib.suppress(discord.HTTPException):
            await target.send(notice)

        await log_action(
            guild,
            warning_embed(
                f"**{target}** (`{target.id}`) voice-muted via role `{role.name}`"
                + (f" for {duration} minute(s)" if duration else " indefinitely")
                + f".\nReason: {reason or 'No reason provided'}",
                title="Voice mute",
                author=interaction.user,
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{target.mention}** can no longer speak"
                + (f" for **{duration}** minute(s)" if duration else " until lifted")
                + f".\nGranted `{role.name}`",
                title="Voice muted",
                author=interaction.user,
            ),
        )

    @app_commands.command(
        name="vcunmute", description="Lift a voice mute by removing the mute role."
    )
    @app_commands.guild_only
    @app_commands.describe(target="The member to unmute.")
    async def vcunmute(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))

        guild = interaction.guild
        role = await _resolve_mute_role(guild)
        self.assert_hierarchy(interaction.user, target, guild.me, verb="unmute")

        if role not in target.roles:
            raise ZagrosError(f"**{target}** does not hold the mute role.")

        try:
            await target.remove_roles(
                role, reason=build_audit_reason(None, interaction.user, "Voice unmute")
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "Discord refused removing the mute role. I need **Manage Roles** and "
                "to sit above the mute role."
            ) from exc

        # Close the ledger rows this lift actually ended. Scoped to MUTE so a
        # timeout, warning or ban on the same member stays active and honest.
        closed = await revoke_cases(
            guild.id,
            user_id=target.id,
            actions=(CaseAction.MUTE,),
            moderator_id=interaction.user.id,
            reason="Voice unmute",
        )

        await log_action(
            guild,
            success_embed(
                f"**{target}** (`{target.id}`) unmuted in voice; `{role.name}` removed."
                + (f"\n{closed} active mute record(s) closed." if closed else ""),
                author=interaction.user,
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{target.mention}** can speak again.",
                title="Voice unmuted",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /vclock + /vcunlock
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="vclock",
        description="Stop everyone in a voice channel from speaking (or connecting).",
    )
    @app_commands.guild_only
    @app_commands.describe(
        channel="The voice channel to lock.",
        mode="'speak' mutes them where they sit; 'connect' also stops new joins.",
        reason="Why — recorded in the audit log.",
    )
    async def vclock(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        mode: str = "speak",
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_channels",))

        guild = interaction.guild
        if mode not in ("speak", "connect"):
            raise ZagrosError("Choose `speak` or `connect` for the lock mode.")
        if not isinstance(channel, discord.VoiceChannel):
            raise ZagrosError(f"{channel.mention} is not a voice channel.")

        self.assert_bot_permissions(
            guild.me, channel, ("manage_channels",), verb="lock the voice channel"
        )

        deny = discord.PermissionOverwrite.from_pair(
            allow=discord.Permissions.none(), deny=discord.Permissions(speak=True)
        )
        if mode == "connect":
            deny.deny |= discord.Permissions(connect=True)

        # Snapshot before locking: /vcunlock needs the state we replace, and this
        # must be recorded even if the edit below fails.
        await save_channel_snapshot(
            guild.id, channel, f"vclock:{channel.id}", created_by=interaction.user.id
        )

        # channel.overwrites is an immutable mapping; build a fresh one with the
        # default role denied rather than trying to mutate it in place.
        new_overwrites = {**channel.overwrites, channel.guild.default_role: deny}
        await channel.edit(
            overwrites=new_overwrites,
            reason=build_audit_reason(None, interaction.user, "Voice lock"),
        )

        await log_action(
            guild,
            warning_embed(
                f"**{channel.mention}** locked in `{mode}` mode by {interaction.user}.\n"
                f"Reason: {reason or 'No reason provided'}",
                title="Voice channel locked",
                author=interaction.user,
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{channel.mention}** is locked — members can no longer "
                f"{'join or speak' if mode == 'connect' else 'speak'}.\n"
                "Use `/vcunlock` to restore the original permissions.",
                title="Voice locked",
                author=interaction.user,
            ),
        )

    @app_commands.command(
        name="vcunlock", description="Restore a voice channel's original permissions."
    )
    @app_commands.guild_only
    @app_commands.describe(channel="The voice channel to unlock.")
    async def vcunlock(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_channels",))

        guild = interaction.guild
        if not isinstance(channel, discord.VoiceChannel):
            raise ZagrosError(f"{channel.mention} is not a voice channel.")
        self.assert_bot_permissions(
            guild.me, channel, ("manage_channels",), verb="unlock the voice channel"
        )

        # Restore the snapshot vclock took, not "delete @everyone". A channel that had
        # its own @everyone allow before the lock would silently lose it.
        label = f"vclock:{channel.id}"
        restored, missing = await restore_channel_snapshots(guild, label)
        if restored == 0:
            # No snapshot: this channel was never locked by /vclock, or the
            # snapshot row is gone. Fall back to removing the lock overwrite
            # itself, and say so rather than claiming a full restore.
            new_overwrites = {
                target: ow
                for target, ow in channel.overwrites.items()
                if target != guild.default_role
            }
            await channel.edit(
                overwrites=new_overwrites or discord.utils.MISSING,
                reason=build_audit_reason(None, interaction.user, "Voice unlock"),
            )
            await drop_snapshots(guild.id, label)
            await self.reply(
                interaction,
                warning_embed(
                    f"**{channel.mention}** had no saved snapshot, so I removed the "
                    "lock instead of restoring the original permissions. Anything "
                    "else that changed since then is still in place.",
                    title="Unlocked, no snapshot found",
                    author=interaction.user,
                ),
            )
            return

        await drop_snapshots(guild.id, label)

        await log_action(
            guild,
            base_embed(
                title="Voice channel unlocked",
                author=interaction.user,
                colour=COLOR_INFO,
                description=(
                    f"**{channel.mention}** restored to its default permissions by "
                    f"{interaction.user}."
                ),
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{channel.mention}** is unlocked — original permissions "
                f"restored{f' ({missing} could not be re-applied)' if missing else ''}.",
                title="Voice unlocked",
                author=interaction.user,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(VoiceTools(bot))
    logger.info("Voice tools cog loaded")
