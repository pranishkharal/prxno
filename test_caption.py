"""Test caption generation with the actual transcript."""
from captioning import pick_template_category, generate_caption_template, generate_caption

transcript = "I'm scared, bro. Stop. Francisco, when I get scared, I'm scared. No. Francisco, when I get scared, I'm scared. No. No. Frankie, what's that you?"

# Test template category detection
category = pick_template_category(transcript)
print(f"Detected category: {category}")

# Test template caption
caption = generate_caption_template(transcript, 'bluesclues124')
print(f"Template caption: {caption}")

# Test full caption generation
full_caption = generate_caption(transcript, 'bluesclues124')
print(f"Full caption: {full_caption}")