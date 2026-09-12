with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

if "from captioning import" not in content:
    marker = "import asyncio"
    content = content.replace(
        marker,
        marker + "\nfrom captioning import transcribe_and_caption",
        1
    )
    print("Added captioning import.")
else:
    print("Import already present, skipping.")

caption_view_code = """
class CaptionView(discord.ui.View):
    \"\"\"
    Shown alongside a finished edited clip. Nothing here runs until
    the user clicks the button, so a captioning failure never
    affects video delivery.
    \"\"\"

    def __init__(self, output_file, streamer_hint):
        super().__init__(timeout=None)
        self.output_file = output_file
        self.streamer_hint = streamer_hint
        self.used = False

    @discord.ui.button(label="Generate Caption", emoji="\\U0001F4DD", style=discord.ButtonStyle.primary)
    async def generate_caption_button(self, interaction, button):
        if self.used:
            await interaction.response.send_message(
                "This caption has already been generated.",
                ephemeral=True
            )
            return

        self.used = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send("Generating caption, this may take a minute...")

        try:
            caption_text = await run_encode_job(
                transcribe_and_caption,
                self.output_file,
                self.streamer_hint
            )
            await interaction.followup.send(f"**Caption:**\\n{caption_text}")
        except Exception as caption_error:
            print("Caption generation failed:", caption_error)
            await interaction.followup.send(
                "Sorry, caption generation failed for this clip."
            )


"""

marker2 = "async def start_kick_edit(message, kick_url):"
if "class CaptionView(discord.ui.View):" not in content:
    content = content.replace(marker2, caption_view_code + marker2, 1)
    print("Inserted CaptionView class.")
else:
    print("CaptionView already present, skipping.")

old_tail = (
    "                    filename=\"edited_kick_clip.mp4\"\n"
    "                )\n"
    "            )"
)
new_tail = (
    "                    filename=\"edited_kick_clip.mp4\"\n"
    "                ),\n"
    "                view=CaptionView(output_file, self.url_streamer_hint)\n"
    "            )"
)

count = content.count(old_tail)
content = content.replace(old_tail, new_tail, 1)
print(f"Matched send-block tail {count} time(s); replaced first occurrence.")

with open("main.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done.")
