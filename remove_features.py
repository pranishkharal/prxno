#!/usr/bin/env python3
"""
Script to remove Slightly Zoomed and Remove Non-Speech features from main.py
to improve processing speed.
"""

import re

def main():
    # Read the original file
    with open('d:\\Auto Clips for Kick\\main.py.restored.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 1. Remove webrtcvad import
    content = re.sub(r'^import webrtcvad\s*\n', '', content, flags=re.MULTILINE)
    
    # 2. Remove zoom from options dictionary
    content = re.sub(r'"zoom": False,\s*\n\s*"mirror": False,', '"mirror": False,', content)
    
    # 3. Remove remove_silence from options dictionary
    content = re.sub(r'"remove_silence": True,\s*\n', '', content)
    
    # 4. Remove zoom button
    content = re.sub(
        r'    @discord\.ui\.button\(\s*\n'
        r'        label="Slightly Zoomed",\s*\n'
        r'        style=discord\.ButtonStyle\.secondary\s*\n'
        r'    \)\s*\n'
        r'    async def zoom_button\(self, interaction, button\):\s*\n'
        r'\s*self\.options\["zoom"\] = not self\.options\["zoom"\]\s*\n'
        r'\s*button\.style = \(\s*\n'
        r'            discord\.ButtonStyle\.success\s*\n'
        r'            if self\.options\["zoom"\]:\s*\n'
        r'            else discord\.ButtonStyle\.secondary\s*\n'
        r'        \}\s*\n'
        r'\s*await interaction\.response\.edit_message\(\s*\n'
        r'            content=\(\s*\n'
        r'                "\*\*AUTO EDIT OPTIONS\*\*\n\n"\s*\n'
        r'                f"Selected: \*\*\{self\.summary\(\)\\}\*\*"\s*\n'
        r'            \),\s*\n'
        r'            view=self\s*\n'
        r'        \)\s*\n'
        r'\s*\n',
        '',
        content
    )
    
    # 5. Remove silence button
    content = re.sub(
        r'    @discord\.ui\.button\(\s*\n'
        r'        label="Remove Non-Speech",\s*\n'
        r'        style=discord\.ButtonStyle\.secondary\s*\n'
        r'    \)\s*\n'
        r'    async def silence_button\(self, interaction, button\):\s*\n'
        r'\s*self\.options\["remove_silence"\] = not self\.options\["remove_silence"\]\s*\n'
        r'\s*button\.style = \(\s*\n'
        r'            discord\.ButtonStyle\.success\s*\n'
        r'            if self\.options\["remove_silence"\]:\s*\n'
        r'            else discord\.ButtonStyle\.secondary\s*\n'
        r'        \}\s*\n'
        r'\s*await interaction\.response\.edit_message\(\s*\n'
        r'            content=\(\s*\n'
        r'                "\*\*AUTO EDIT OPTIONS\*\*\n\n"\s*\n'
        r'                f"Selected: \*\*\{self\.summary\(\)\\}\*\*"\s*\n'
        r'            \),\s*\n'
        r'            view=self\s*\n'
        r'        \)\s*\n'
        r'\s*\n',
        '',
        content
    )
    
    # 6. Update summary method - remove zoom
    content = re.sub(
        r'        if self\.options\["zoom"\]:\s*\n'
        r'            enabled\.append\("Zoom"\)\s*\n'
        r'\s*\n',
        '',
        content
    )
    
    # 7. Update summary method - remove remove_silence
    content = re.sub(
        r'        if self\.options\["remove_silence"\]:\s*\n'
        r'            enabled\.append\("Remove Non-Speech"\)\s*\n'
        r'\s*\n',
        '',
        content
    )
    
    # 8. Remove zoom from edit_video function
    content = re.sub(r'    zoom = options\["zoom"\]\s*\n', '', content)
    content = re.sub(
        r'        if zoom:\s*\n'
        r'            filters\.append\(\s*\n'
        r'                "scale=iw\*1\.08:ih\*1\.08,"\s*\n'
        r'                "crop=floor\(iw/1\.08/2\)\*2:floor\(ih/1\.08/2\)\*2"\s*\n'
        r'            \)\s*\n'
        r'\s*\n',
        '',
        content
    )
    
    # 9. Remove remove_silence logic from edit process
    content = re.sub(
        r'            # -{10,}\s*\n'
        r'            # Remove non-speech only when enabled\.\s*\n'
        r'            # -{10,}\s*\n'
        r'\s*\n'
        r'            if self\.options\["remove_silence"\]:\s*\n'
        r'\s*\n'
        r'                silence_file = OUTPUT_DIR / \(.*?\)?\s*\n'
        r'\s*\n'
        r'                await notify\(\s*\n'
        r'                    "Removing silent/non-speaking sections\.\.\."\s*\n'
        r'                \)\s*\n'
        r'\s*\n'
        r'                await run_encode_job\(\s*\n'
        r'                    remove_silence,\s*\n'
        r'                    working_file,\s*\n'
        r'                    silence_file\s*\n'
        r'                \)\s*\n'
        r'\s*\n'
        r'                working_file = silence_file\s*\n'
        r'                temp_files\.append\(silence_file\)\s*\n'
        r'\s*\n',
        '',
        content,
        flags=re.DOTALL
    )
    
    # 10. Remove the remove_silence function
    content = re.sub(
        r'\n# -{10,}\s*\n'
        r'# Remove silence from video \(voice activity detection\)\s*\n'
        r'# -{10,}\s*\n'
        r'\s*\n'
        r'def remove_silence\(input_file, output_file\):\s*.*?'
        r'\n# -{10,}\s*\n'
        r'# AUTOMATIC OCR OVERLAY SYSTEM\s*\n'
        r'# -{10,}\s*\n',
        '\n# -{10,}\n# AUTOMATIC OCR OVERLAY SYSTEM\n# -{10,}\n',
        content,
        flags=re.DOTALL
    )
    
    # Write the modified file
    with open('d:\\Auto Clips for Kick\\main.py', 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("Successfully updated main.py!")
    print("- Removed webrtcvad import")
    print("- Removed 'Slightly Zoomed' button and functionality")
    print("- Removed 'Remove Non-Speech' button and speech detection")
    print("- Updated options dictionary")
    print("- Updated summary() method")
    print("- Updated edit_video() function")
    print("- Removed remove_silence() function")

if __name__ == '__main__':
    main()
