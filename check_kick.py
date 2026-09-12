"""Check yt-dlp KICK extractor support"""
import yt_dlp

for name, cls in yt_dlp.extractor._extractors.items():
    if 'kick' in name.lower():
        print(f"{name}: {cls.IE_NAME}")
        if hasattr(cls, '_WORKING'):
            print(f"  Working: {cls._WORKING}")
        if hasattr(cls, '_TESTS'):
            print(f"  Tests: {len(cls._TESTS)}")