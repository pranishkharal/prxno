#!/usr/bin/env python3
"""Remove zoom, silence, and auto caption features from main.py"""

import re

with open('d:\\Auto Clips for Kick\\main.py.restored.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Remove imports
content = re.sub(r'^import webrtcvad\s*\n', '', content, flags=re.MULTILINE)
content = re.sub(r'^from captioning import transcribe_and_caption\s*\n', '', content, flags=re.MULTILINE)
content = re.sub(r'^from word_caption import burn_word_captions\s*\n', '', content, flags=re.MULTILINE)

# Remove options
content = re.sub(r'"zoom": False,\s*\n', '', content)
content = re.sub(r'"remove_silence": True,\s*\n', '', content)
content = re.sub(r'"auto_captions": False,\s*\n', '', content)

# Remove buttons and their handlers
content = re.sub(r'    @discord\.ui\.button\(\s*\n        label="Slightly Zoomed".*?\n\s*\n', '', content, flags=re.DOTALL)
content = re.sub(r'    @discord\.ui\.button\(\s*\n        label="Remove Non-Speech".*?\n\s*\n', '', content, flags=re.DOTALL)
content = re.sub(r'    @discord\.ui\.button\(\s*\n        label="Auto Captions".*?\n\s*\n', '', content, flags=re.DOTALL)

# Remove checks in summary()
content = re.sub(r'        if self\.options\["zoom"\]:.*?enabled\.append\("Zoom"\)\s*\n\s*\n', '', content, flags=re.DOTALL)
content = re.sub(r'        if self\.options\["remove_silence"\]:.*?enabled\.append\("Remove Non-Speech"\)\s*\n\s*\n', '', content, flags=re.DOTALL)
content = re.sub(r'        if self\.options\.get\("auto_captions", False\):.*?enabled\.append\("Auto Captions"\)\s*\n\s*\n', '', content, flags=re.DOTALL)

# Remove zoom from edit_video
content = re.sub(r'    zoom = options\["zoom"\]\s*\n', '', content)
content = re.sub(r'        if zoom:.*?filters\. append.*scale=iw\*1\.08.*?\s*\n\s*\n', '', content, flags=re.DOTALL)

# Remove remove_silence logic
content = re.sub(r'            # -{10,}?\s*\n            # Remove non-speech only when enabled\.\s*\n            # -{10,}?\s*\n\s*self\.options\["remove_silence"\].*?working_file = silence_file.*?temp_files\. append\(silence_file\)\s*\n\s*\n', '', content, flags=re.DOTALL)

# Remove auto_captions logic  
content = re.sub(r'            # -{10,}?\s*\n\s*# Auto captions.*?' if self\.options\. get\("auto_captions".*? captioned_file = OUTPUT_DIR.*?\s*\n\s*\n', '', content, flags=re.DOTALL)

# Remove functions
content = re.sub(r'\n# -{10,}\s*\n# Remove silence.*?def remove_silence.*?# -{10,}\s*\n# AUTOMATIC OCR OVERLAY SYSTEM', '\n# -{10,}\n# AUTOMATIC OCR OVERLAY SYSTEM', content, flags=re.DOTALL)

with open('d:\\Auto Clips for Kick\\main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done!")
