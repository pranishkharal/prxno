"""
Review Interface - Discord UI for human review of top clip candidates.

Provides interactive Discord interface for users to:
- View top ranked clips with scores and reasoning
- Preview clip details (timestamps, hook, payoff, story)
- Select clips to edit: EDIT / REJECT / REVIEW
- Batch edit all top clips: EDIT ALL
- Customize edit options per clip
"""

import asyncio
import discord
from discord.ui import View, Button, Select
from discord import ButtonStyle, Interaction
from typing import Dict, List, Optional, Any, Callable
from pathlib import Path

from .job_manager import JobManager, VODJob, ClipCandidate, JobState
from .clip_ranker import RankingResult, RankedClip
from .metadata_generator import ClipMetadata
from .edit_planner import EditPlan, generate_ffmpeg_filter_plan
from .config import CONFIG


class ClipReviewView(View):
    """
    Main review interface showing top clips with action buttons.
    """

    def __init__(
        self,
        job: VODJob,
        ranking_result: RankingResult,
        metadata_map: Dict[str, ClipMetadata],
        job_manager: JobManager,
        on_edit: Callable,
        on_reject: Callable,
        on_edit_all: Callable,
        timeout: float = 1800  # 30 minutes
    ):
        super().__init__(timeout=timeout)
        self.job = job
        self.ranking_result = ranking_result
        self.metadata_map = metadata_map
        self.job_manager = job_manager
        self.on_edit = on_edit
        self.on_reject = on_reject
        self.on_edit_all = on_edit_all
        self.selected_clips: Dict[int, bool] = {}  # rank -> selected
        self.message: Optional[discord.Message] = None

        # Add clip selection buttons
        self._add_clip_buttons()

        # Add action row
        self._add_action_buttons()

    def _add_clip_buttons(self):
        """Add a button for each top clip."""
        for clip in self.ranking_result.top_clips:
            rank = clip.rank
            priority_emoji = {"high": "🔥", "normal": "⭐", "low": "📋"}.get(clip.review_priority, "⭐")
            duplicate_flag = " ⚠️ DUP" if clip.is_duplicate else ""

            btn = Button(
                label=f"#{rank} {priority_emoji} {clip.final_score:.0f}{duplicate_flag}",
                style=ButtonStyle.secondary,
                custom_id=f"clip_{rank}",
                row=min((rank - 1) // 5, 4)  # Max 5 per row, max row 4
            )
            btn.callback = self._make_clip_callback(rank)
            self.add_item(btn)

    def _make_clip_callback(self, rank: int):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.job.user_id:
                await interaction.response.send_message("This review panel belongs to someone else.", ephemeral=True)
                return

            clip = next(c for c in self.ranking_result.top_clips if c.rank == rank)
            metadata = self.metadata_map.get(f"rank_{rank}")

            # Show detailed clip view
            detail_view = ClipDetailView(
                job=self.job,
                clip=clip,
                metadata=metadata,
                job_manager=self.job_manager,
                on_edit=self.on_edit,
                on_reject=self.on_reject,
                parent_view=self
            )

            embed = self._build_clip_embed(clip, metadata)
            await interaction.response.edit_message(embed=embed, view=detail_view)

        return callback

    def _add_action_buttons(self):
        """Add action buttons at bottom."""
        row = min(len(self.ranking_result.top_clips) // 5 + 1, 4)

        # Edit All button
        edit_all_btn = Button(
            label="✂️ Edit All Top Clips",
            style=ButtonStyle.success,
            custom_id="edit_all",
            row=row
        )
        edit_all_btn.callback = self._edit_all_callback
        self.add_item(edit_all_btn)

        # Cancel button
        cancel_btn = Button(
            label="❌ Cancel",
            style=ButtonStyle.danger,
            custom_id="cancel",
            row=row
        )
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    async def _edit_all_callback(self, interaction: Interaction):
        if interaction.user.id != self.job.user_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return

        await interaction.response.defer()
        await self.on_edit_all(interaction, self.ranking_result.top_clips)

    async def _cancel_callback(self, interaction: Interaction):
        if interaction.user.id != self.job.user_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return

        self.job_manager.cancel_job(self.job.id)
        await interaction.response.edit_message(
            content="❌ VOD job cancelled.",
            embed=None,
            view=None
        )
        self.stop()

    def _build_clip_embed(self, clip: RankedClip, metadata: Optional[ClipMetadata]) -> discord.Embed:
        """Build embed for clip summary in list view."""
        m = clip.moment
        priority_color = {"high": 0xFF0000, "normal": 0xFFAA00, "low": 0x888888}.get(clip.review_priority, 0xFFAA00)

        embed = discord.Embed(
            title=f"Clip #{clip.rank} — Score: {clip.final_score:.1f}/100",
            color=priority_color
        )

        embed.add_field(
            name="⏱️ Timestamp",
            value=f"`{m.expanded_start:.1f}s - {m.expanded_end:.1f}s` ({m.duration:.1f}s)",
            inline=True
        )

        embed.add_field(
            name="📖 Story",
            value=f"{'Complete' if clip.story_analysis.has_complete_arc else 'Incomplete'} | {clip.story_analysis.pacing_rating.title()} pacing",
            inline=True
        )

        embed.add_field(
            name="🏷️ Tags",
            value=", ".join(clip.tags[:6]) or "—",
            inline=False
        )

        embed.add_field(
            name="🎯 Hook",
            value=m.hook_text[:100] + ("..." if len(m.hook_text) > 100 else ""),
            inline=False
        )

        embed.add_field(
            name="💰 Payoff",
            value=m.payoff_text[:100] + ("..." if len(m.payoff_text) > 100 else ""),
            inline=False
        )

        if metadata:
            embed.add_field(
                name="📝 Suggested Title",
                value=metadata.title[:100],
                inline=False
            )

        if clip.is_duplicate:
            embed.set_footer(text=f"⚠️ Duplicate of: {clip.duplicate_of}")

        return embed


class ClipDetailView(View):
    """
    Detailed view for a single clip with EDIT / REJECT / BACK actions.
    """

    def __init__(
        self,
        job: VODJob,
        clip: RankedClip,
        metadata: Optional[ClipMetadata],
        job_manager: JobManager,
        on_edit: Callable,
        on_reject: Callable,
        parent_view: ClipReviewView,
        timeout: float = 1800
    ):
        super().__init__(timeout=timeout)
        self.job = job
        self.clip = clip
        self.metadata = metadata
        self.job_manager = job_manager
        self.on_edit = on_edit
        self.on_reject = on_reject
        self.parent_view = parent_view
        self.edit_options = clip.moment.edit_recommendations.copy()

        self._add_action_buttons()
        self._add_edit_options()

    def _add_action_buttons(self):
        """Main action buttons."""
        # Edit button
        edit_btn = Button(
            label="✂️ Edit This Clip",
            style=ButtonStyle.success,
            custom_id="edit",
            row=0
        )
        edit_btn.callback = self._edit_callback
        self.add_item(edit_btn)

        # Reject button
        reject_btn = Button(
            label="🗑️ Reject",
            style=ButtonStyle.danger,
            custom_id="reject",
            row=0
        )
        reject_btn.callback = self._reject_callback
        self.add_item(reject_btn)

        # Back button
        back_btn = Button(
            label="◀️ Back to List",
            style=ButtonStyle.secondary,
            custom_id="back",
            row=0
        )
        back_btn.callback = self._back_callback
        self.add_item(back_btn)

        # Customize edit options
        customize_btn = Button(
            label="⚙️ Customize Edit",
            style=ButtonStyle.primary,
            custom_id="customize",
            row=1
        )
        customize_btn.callback = self._customize_callback
        self.add_item(customize_btn)

    def _add_edit_options(self):
        """Add edit option toggles based on recommendations."""
        # These are informational - actual options applied at edit time
        pass

    async def _edit_callback(self, interaction: Interaction):
        if interaction.user.id != self.job.user_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return

        await interaction.response.defer()
        await self.on_edit(interaction, self.job, self.clip, self.edit_options)

    async def _reject_callback(self, interaction: Interaction):
        if interaction.user.id != self.job.user_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return

        await interaction.response.defer()
        await self.on_reject(interaction, self.clip)

    async def _back_callback(self, interaction: Interaction):
        if interaction.user.id != self.job.user_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return

        # Return to main review view
        embed = self.parent_view._build_list_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)

    async def _customize_callback(self, interaction: Interaction):
        if interaction.user.id != self.job.user_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return

        customize_view = EditCustomizeView(
            job=self.job,
            clip=self.clip,
            current_options=self.edit_options,
            on_save=self._save_customization,
            on_cancel=lambda interaction, job, clip: interaction.edit_original_response(view=self)
        )
        await interaction.response.edit_message(view=customize_view)

    async def _save_customization(self, interaction: Interaction, job, clip, options: Dict[str, Any]):
        self.edit_options.update(options)
        await interaction.response.send_message("Edit options updated!", ephemeral=True)
        await interaction.edit_original_response(view=self)


class EditCustomizeView(View):
    """View for customizing edit options for a clip."""

    def __init__(
        self,
        job: VODJob,
        clip: RankedClip,
        current_options: Dict[str, Any],
        on_save: Callable,
        on_cancel: Callable,
        timeout: float = 300
    ):
        super().__init__(timeout=timeout)
        self.job = job
        self.clip = clip
        self.options = current_options.copy()
        self.on_save = on_save
        self.on_cancel = on_cancel

        self._add_option_controls()

    def _add_option_controls(self):
        """Add toggles for each edit option."""
        row = 0

        # Size select
        size_select = Select(
            placeholder="📐 Aspect Ratio",
            options=[
                discord.SelectOption(label="9:16 Vertical", value="9:16", default=self.options.get("size") == "9:16"),
                discord.SelectOption(label="1:1 Square", value="1:1", default=self.options.get("size") == "1:1"),
                discord.SelectOption(label="4:5 Portrait", value="4:5", default=self.options.get("size") == "4:5"),
                discord.SelectOption(label="4:3", value="4:3", default=self.options.get("size") == "4:3"),
                discord.SelectOption(label="Original", value="Original", default=self.options.get("size") == "Original"),
            ],
            custom_id="size",
            row=row
        )
        size_select.callback = self._make_option_callback("size")
        self.add_item(size_select)
        row += 1

        # Enhance select
        enhance_select = Select(
            placeholder="🎨 Enhancement",
            options=[
                discord.SelectOption(label="Off", value="Off", default=self.options.get("enhance") == "Off"),
                discord.SelectOption(label="Subtle", value="Subtle", default=self.options.get("enhance") == "Subtle"),
                discord.SelectOption(label="Vivid", value="Vivid", default=self.options.get("enhance") == "Vivid"),
            ],
            custom_id="enhance",
            row=row
        )
        enhance_select.callback = self._make_option_callback("enhance")
        self.add_item(enhance_select)
        row += 1

        # Toggle buttons
        toggles = [
            ("zoom", "🔍 Zoom", self.options.get("zoom", False)),
            ("mirror", "🪞 Mirror", self.options.get("mirror", False)),
            ("blur", "🌫️ Blur BG", self.options.get("blur", False)),
            ("remove_silence", "🔇 Remove Silence", self.options.get("remove_silence", True)),
            ("overlay", "🏷️ Overlay", self.options.get("overlay", True)),
            ("auto_captions", "📝 Auto Captions", self.options.get("auto_captions", False)),
            ("split_screen", "➗ Split Screen", self.options.get("split_screen", False)),
        ]

        for i, (key, label, enabled) in enumerate(toggles):
            btn = Button(
                label=f"{'✅' if enabled else '⬜'} {label}",
                style=ButtonStyle.success if enabled else ButtonStyle.secondary,
                custom_id=f"toggle_{key}",
                row=row
            )
            btn.callback = self._make_toggle_callback(key, label)
            self.add_item(btn)

            if (i + 1) % 3 == 0:
                row += 1

        row += 1

        # Save/Cancel
        save_row = min(row, 4)
        save_btn = Button(label="💾 Save", style=ButtonStyle.success, custom_id="save", row=save_row)
        save_btn.callback = self._save_callback
        self.add_item(save_btn)

        cancel_btn = Button(label="❌ Cancel", style=ButtonStyle.danger, custom_id="cancel", row=save_row)
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    def _make_option_callback(self, key: str):
        async def callback(interaction: Interaction):
            self.options[key] = interaction.data["values"][0]
            # Update the select to show new default
            await interaction.response.edit_message(view=self)
        return callback

    def _make_toggle_callback(self, key: str, label: str):
        async def callback(interaction: Interaction):
            self.options[key] = not self.options.get(key, False)
            # Update button label
            for item in self.children:
                if hasattr(item, 'custom_id') and item.custom_id == f"toggle_{key}":
                    item.label = f"{'✅' if self.options[key] else '⬜'} {label}"
                    item.style = ButtonStyle.success if self.options[key] else ButtonStyle.secondary
            await interaction.response.edit_message(view=self)
        return callback

    async def _save_callback(self, interaction: Interaction):
        await self.on_save(interaction, self.job, self.clip, self.options)

    async def _cancel_callback(self, interaction: Interaction):
        await self.on_cancel(interaction, self.job, self.clip)


async def start_review_session(
    interaction: Interaction,
    job: VODJob,
    ranking_result: RankingResult,
    metadata_map: Dict[str, ClipMetadata],
    job_manager: JobManager,
    edit_callback: Callable
):
    """
    Start the clip review session in Discord.

    Args:
        interaction: Discord interaction to respond to
        job: The VOD job
        ranking_result: Ranked clips from ClipRanker
        metadata_map: Generated metadata for each clip
        job_manager: Job manager instance
        edit_callback: Async function to call when user wants to edit clips
    """

    async def on_edit(interaction: Interaction, clip: RankedClip, options: Dict[str, Any]):
        await edit_callback(interaction, job, clip, options)

    async def on_reject(interaction: Interaction, clip: RankedClip):
        # Remove from selected
        job.deselect_clip(clip.moment_id if hasattr(clip, 'moment_id') else f"rank_{clip.rank}")
        await interaction.followup.send(f"❌ Rejected clip #{clip.rank}", ephemeral=True)

    async def on_edit_all(interaction: Interaction, clips: List[RankedClip]):
        # Select all for editing
        for clip in clips:
            job.select_clip(f"rank_{clip.rank}")
        await edit_callback(interaction, job, clips, {})

    view = ClipReviewView(
        job=job,
        ranking_result=ranking_result,
        metadata_map=metadata_map,
        job_manager=job_manager,
        on_edit=on_edit,
        on_reject=on_reject,
        on_edit_all=on_edit_all
    )

    # Build initial embed
    embed = view._build_list_embed()

    await interaction.response.edit_message(embed=embed, view=view)


# Helper to build the list embed
def build_review_list_embed(job: VODJob, ranking_result: RankingResult) -> discord.Embed:
    """Build the main list embed."""
    embed = discord.Embed(
        title=f"🎬 VOD Review: {job.vod_title or 'Untitled'}",
        description=(
            f"**Streamer:** {job.streamer_name}\n"
            f"**Duration:** {job.vod_duration/3600:.1f}h\n"
            f"**Candidates:** {ranking_result.total_candidates} | "
            f"**Top for Review:** {len(ranking_result.top_clips)} | "
            f"**Duplicates Removed:** {ranking_result.duplicates_removed}"
        ),
        color=0x00FF88
    )

    embed.add_field(
        name="Instructions",
        value=(
            "• Click a clip to view details and edit\n"
            "• **Edit All** processes all top clips with recommended settings\n"
            "• **Customize** lets you adjust options per clip\n"
            "• Clips marked ⚠️ DUP are duplicates"
        ),
        inline=False
    )

    return embed


# Monkey-patch the list embed builder
ClipReviewView._build_list_embed = lambda self: build_review_list_embed(self.job, self.ranking_result)