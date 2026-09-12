PRESETS = {
    "TikTok": {
        "size": "9:16",
        "zoom": True,
        "mirror": False,
        "blur": True,
        "remove_silence": True,
        "overlay": True,
        "enhance": "Subtle",
        "split_screen": False,
        "auto_captions": True,
    },

    "Story": {
        "size": "9:16",
        "zoom": False,
        "mirror": False,
        "blur": True,
        "remove_silence": True,
        "overlay": True,
        "enhance": "Subtle",
        "split_screen": False,
        "auto_captions": True,
    },

    "Meme": {
        "size": "9:16",
        "zoom": True,
        "mirror": False,
        "blur": True,
        "remove_silence": True,
        "overlay": True,
        "enhance": "Vivid",
        "split_screen": False,
        "auto_captions": True,
    },

    "Clean": {
        "size": "9:16",
        "zoom": False,
        "mirror": False,
        "blur": False,
        "remove_silence": False,
        "overlay": False,
        "enhance": "Off",
        "split_screen": False,
        "auto_captions": False,
    },
}


def get_preset(name):
    preset = PRESETS.get(name)

    if not preset:
        return None

    return dict(preset)


def list_presets():
    return list(PRESETS.keys())
