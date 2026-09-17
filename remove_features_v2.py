#!/usr/bin/env python3
"""
Comprehensive script to remove Slightly Zoomed and Remove Non-Speech features.
"""

import re

# Read the original file
with open('d:\\Auto Clips for Kick\\main.py.restored.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

output_lines = []
i = 0

while i < len(lines):
    line = lines[i]
    
    # Skip webrtcvad import
    if re.match(r'^import webrtcvad\s*$', line):
        print(f"Skipping webrtcvad import at line {i+1}")
        i += 1
        continue
    
    # Skip zoom and remove_silence in options dictionary
    if '"zoom": False,' in line:
        print(f"Skipping zoom option at line {i+1}")
        i += 1
        continue
    
    if '"remove_silence": True,' in line:
        print(f"Skipping remove_silence option at line {i+1}")
        i += 1
        continue
    
    # Skip zoom button
    if re.search(r'label="Slightly Zoomed"', line):
        print(f"Found zoom button at line {i+1}, removing button block...")
        # Go back to find start of decorator
        start = i
        while start > 0 and '@discord.ui.button' not in lines[start]:
            start -= 1
        if start > 0 and '@discord.ui.button' in lines[start]:
            start -= 1  # Include the decorator line
        
        # Find end of button function
        end = i
        while end < len(lines):
            if 'await interaction.response.edit_message' in lines[end]:
                # Find the closing parenthesis
                j = end
                paren_count = 0
                while j < len(lines):
                    paren_count += lines[j].count('(') - lines[j].count(')')
                    if paren_count <= 0 and ')' in lines[j]:
                        end = j + 1
                        break
                    j += 1
                break
            end += 1
        
        print(f"  Removing lines {start+1} to {end+1}")
        i = end
        continue
    
    # Skip silence button
    if re.search(r'label="Remove Non-Speech"', line):
        print(f"Found silence button at line {i+1}, removing button block...")
        start = i
        while start > 0 and '@discord.ui.button' not in lines[start]:
            start -= 1
        if start > 0 and '@discord.ui.button' in lines[start]:
            start -= 1
        
        end = i
        while end < len(lines):
            if 'await interaction.response.edit_message' in lines[end]:
                j = end
                paren_count = 0
                while j < len(lines):
                    paren_count += lines[j].count('(') - lines[j].count(')')
                    if paren_count <= 0 and ')' in lines[j]:
                        end = j + 1
                        break
                    j += 1
                break
            end += 1
        
        print(f"  Removing lines {start+1} to {end+1}")
        i = end
        continue
    
    # Skip zoom check in summary() method
    if 'if self.options["zoom"]:' in line and 'enabled.append("Zoom")' in lines[i+1]:
        print(f"Skipping zoom check in summary() at line {i+1}")
        i += 2  # Skip the if statement and the append line
        continue
    
    # Skip remove_silence check in summary() method
    if 'if self.options["remove_silence"]:' in line and 'enabled.append("Remove Non-Speech")' in lines[i+1]:
        print(f"Skipping remove_silence check in summary() at line {i+1}")
        i += 2
        continue
    
    # Skip zoom variable assignment in edit_video()
    if 'zoom = options["zoom"]' in line:
        print(f"Skipping zoom assignment in edit_video() at line {i+1}")
        i += 1
        continue
    
    # Skip zoom filter addition in edit_video()
    if re.search(r'if zoom:.*?filters\. append.*scale=iw\*1\.08:ih\*1\.08', line, re.DOTALL):
        print(f"Skipping zoom filter in edit_video() at line {i+1}")
        # Skip multiple lines for this filter
        j = i
        while j < len(lines) and ('filters.append' in lines[j] or 'scale=iw' in lines[j] or 'crop=floor' in lines[j] or lines[j].strip() == ''):
            j += 1
        i = j
        continue
    
    # Skip remove_silence logic block
    if '# Remove non-speech only when enabled.' in line:
        print(f"Found remove_silence logic block at line {i+1}, removing...")
        start = i - 1  # Include the comment line before
        end = i
        while end < len(lines):
            if '# Final video editing.' in lines[end]:
                end += 1
                break
            end += 1
        
        print(f"  Removing lines {start+1} to {end+1}")
        i = end
        continue
    
    # Skip remove_silence function
    if 'def remove_silence(input_file, output_file):' in line:
        print(f"Found remove_silence function at line {i+1}, removing function...")
        start = i
        # Go back to include the comment header
        while start > 0 and '# -' not in lines[start-1]:
            start -= 1
        
        end = i
        while end < len(lines):
            if '# AUTOMATIC OCR OVERLAY SYSTEM' in lines[end]:
                # Include the separator lines
                if end > 0 and '# -' in lines[end-1]:
                    end -= 1
                break
            end += 1
        
        print(f"  Removing lines {start+1} to {end+1}")
        i = end
        continue
    
    output_lines.append(line)
    i += 1

# Write the output
with open('d:\\Auto Clips for Kick\\main.py', 'w', encoding='utf-8') as f:
    f.writelines(output_lines)

print(f"\nSuccessfully processed main.py!")
print(f"Original lines: {len(lines)}")
print(f"Final lines: {len(output_lines)}")
print(f"Removed {len(lines) - len(output_lines)} lines")
