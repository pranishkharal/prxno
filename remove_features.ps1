$content = Get-Content 'd:\Auto Clips for Kick\main.py.restored.py' -Encoding UTF8

# 1. Remove webrtcvad import
$content = $content | Where-Object { $_ -notmatch '^import webrtcvad\s*$' }

# 2. Remove zoom and remove_silence from options dictionary
$newContent = @()
$inOptions = $false
foreach ($line in $content) {
    if ($line -match 'self\.options = \{') {
        $inOptions = $true
        $newContent += $line
        continue
    }
    if ($inOptions) {
        if ($line -match '"size": "9:16"') {
            $newContent += $line
            continue
        }
        if ($line -match '"zoom": False' -or $line -match '"remove_silence": True') {
            continue  # Skip these lines
        }
        if ($line -match '^\s*"enhance": "Off",?') {
            $newContent += $line
            continue
        }
        if ($line -match '^\s*"split_screen": False') {
            $newContent += $line
            $inOptions = $false
            continue
        }
        if ($line -match '^\s*\}') {
            $inOptions = $false
            $newContent += $line
            continue
        }
    }
    $newContent += $line
}
$content = $newContent

# 3. Remove zoom button
$startIdx = -1
$endIdx = -1
for ($i = 0; $i -lt $content.Count; $i++) {
    if ($content[$i] -match 'label="Slightly Zoomed"') {
        # Go back to find the start of the button decorator
        $startIdx = $i
        while ($startIdx -gt 0 -and $content[$startIdx] -notmatch '@discord\.ui\.button') {
            $startIdx--
        }
        if ($startIdx -gt 0 -and $content[$startIdx] -match '@discord\.ui\.button') {
            $startIdx--
        }
    }
    if ($startIdx -ge 0 -and $content[$i] -match 'await interaction\.response\.edit_message' -and $content[$i+1] -match 'view=self' -and $content[$i+2] -match '\)') {
        $endIdx = $i + 3
        break
    }
}
if ($startIdx -ge 0 -and $endIdx -ge 0) {
    $content = $content[0..($startIdx-1)] + $content[$endIdx..($content.Count-1)]
}

# 4. Remove silence button
$startIdx = -1
$endIdx = -1
for ($i = 0; $i -lt $content.Count; $i++) {
    if ($content[$i] -match 'label="Remove Non-Speech"') {
        $startIdx = $i
        while ($startIdx -gt 0 -and $content[$startIdx] -notmatch '@discord\.ui\.button') {
            $startIdx--
        }
        if ($startIdx -gt 0 -and $content[$startIdx] -match '@discord\.ui\.button') {
            $startIdx--
        }
    }
    if ($startIdx -ge 0 -and $content[$i] -match 'await interaction\.response\.edit_message' -and $content[$i+1] -match 'view=self' -and $content[$i+2] -match '\)') {
        $endIdx = $i + 3
        break
    }
}
if ($startIdx -ge 0 -and $endIdx -ge 0) {
    $content = $content[0..($startIdx-1)] + $content[$endIdx..($content.Count-1)]
}

# 5. Update summary method - remove zoom and remove_silence checks
$newContent = @()
foreach ($line in $content) {
    if ($line -match 'if self\.options\["zoom"\]' -or $line -match 'if self\.options\["remove_silence"\]') {
        # Skip this line and the next 2 lines (the append and blank line)
        $skip = 3
        while ($skip -gt 0 -and $content.IndexOf($line) + $skip -le $content.Count) {
            $skip--
        }
        continue
    }
    $newContent += $line
}
$content = $newContent

# 6. Remove zoom from edit_video function
$newContent = @()
$inEditVideo = $false
foreach ($line in $content) {
    if ($line -match 'def edit_video\(input_file') {
        $inEditVideo = $true
    }
    if ($inEditVideo) {
        if ($line -match 'zoom = options\["zoom"\]') {
            continue  # Skip this line
        }
        if ($line -match 'if zoom:' -or $line -match 'scale=iw\*1\.08:ih\*1\.08' -or $line -match 'crop=floor\(iw/1\.08/2\)') {
            continue  # Skip zoom-related lines
        }
    }
    $newContent += $line
}
$content = $newContent

# 7. Remove remove_silence logic from edit process
$newContent = @()
$skipSection = $false
foreach ($line in $content) {
    if ($line -match '# Remove non-speech only when enabled') {
        $skipSection = $true
        continue
    }
    if ($skipSection) {
        if ($line -match '# -{10,}' -and $content[$content.IndexOf($line)+1] -match '# -{10,}' -and $content[$content.IndexOf($line)+2] -match '# Final video editing') {
            # We've reached the end of the section to skip
            # Add the separator lines but not the content
            $newContent += $line
            $skipSection = $false
            continue
        }
        continue  # Skip all lines in this section
    }
    $newContent += $line
}
$content = $newContent

# 8. Remove remove_silence function
$startIdx = -1
$endIdx = -1
for ($i = 0; $i -lt $content.Count; $i++) {
    if ($content[$i] -match 'def remove_silence\(input_file, output_file\):') {
        $startIdx = $i
        # Go back to include the comment header
        while ($startIdx -gt 0 -and $content[$startIdx] -notmatch '^# -{10,}$') {
            $startIdx--
        }
    }
    if ($startIdx -ge 0 -and $content[$i] -match '^# -{10,}$' -and $content[$i+1] -match '^# AUTOMATIC OCR OVERLAY SYSTEM') {
        $endIdx = $i
        break
    }
}
if ($startIdx -ge 0 -and $endIdx -ge 0) {
    $content = $content[0..($startIdx-1)] + $content[$endIdx..($content.Count-1)]
}

# Write the modified file
$newContent | Set-Content 'd:\Auto Clips for Kick\main.py' -Encoding UTF8

Write-Host "Successfully updated main.py!"
Write-Host "- Removed webrtcvad import"
Write-Host "- Removed 'Slightly Zoomed' button and functionality"
Write-Host "- Removed 'Remove Non-Speech' button and speech detection"
Write-Host "- Updated options dictionary"
Write-Host "- Updated summary() method"
Write-Host "- Updated edit_video() function"
Write-Host "- Removed remove_silence() function"
