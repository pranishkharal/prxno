import asyncio
import json
import os
import time
from pathlib import Path


PRANIXX_KICK_USERNAME = "pranixx"

PRANIXX_KICK_URL = "https://kick.com/pranixx"
PRANIXX_YOUTUBE_URL = (
    "https://www.youtube.com/@PranishKharal/live"
)

DEFAULT_DISCORD_CHANNEL = 1552969582816399451

STATE_FILE = (
    Path(__file__).resolve().parent
    / "pranixx_live_state.json"
)



def get_notify_channel_id():
    raw = os.getenv(
        "PRXNO_LIVE_NOTIFY_CHANNEL",
        str(DEFAULT_DISCORD_CHANNEL),
    ).strip()

    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DISCORD_CHANNEL


def get_poll_seconds():
    raw = os.getenv(
        "PRXNO_LIVE_POLL_SECONDS",
        "30",
    ).strip()

    try:
        return max(15, int(raw))
    except ValueError:
        return 30


def load_state():
    default = {
        "kick": False,
        "youtube": False,
        "initialized": False,
    }

    try:
        if not STATE_FILE.exists():
            return default

        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            return default

        return {
            "kick": bool(data.get("kick", False)),
            "youtube": bool(data.get("youtube", False)),
            "initialized": bool(
                data.get("initialized", False)
            ),
        }

    except Exception as error:
        print(
            f"PRANIXX LIVE: state load failed: {error}"
        )
        return default


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    try:
        temp_file.write_text(
            json.dumps(
                state,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp_file.replace(STATE_FILE)

    except Exception as error:
        print(
            f"PRANIXX LIVE: state save failed: {error}"
        )


async def fetch_kick_live(session):
    endpoint = (
        "https://kick.com/api/v2/channels/"
        f"{PRANIXX_KICK_USERNAME}"
    )

    try:
        async with session.get(endpoint) as response:

            if response.status != 200:
                print(
                    "PRANIXX LIVE: KICK HTTP "
                    f"{response.status}"
                )
                return None

            payload = await response.json(
                content_type=None
            )

            if not isinstance(payload, dict):
                return None

            livestream = payload.get(
                "livestream"
            )

            if not isinstance(
                livestream,
                dict,
            ):
                return {
                    "live": False,
                    "platform": "KICK",
                    "url": PRANIXX_KICK_URL,
                }

            return {
                "live": True,
                "platform": "KICK",
                "url": PRANIXX_KICK_URL,
                "title": str(
                    livestream.get("session_title")
                    or livestream.get("title")
                    or "Pranixx is live on KICK"
                ),
                "viewers": (
                    livestream.get("viewer_count")
                    or livestream.get("viewers")
                ),
                "started_at": (
                    livestream.get("started_at")
                    or livestream.get("created_at")
                ),
            }

    except asyncio.CancelledError:
        raise

    except Exception as error:
        print(
            f"PRANIXX LIVE: KICK check failed: {error}"
        )
        return None


async def fetch_youtube_live():
    """
    Check Pranish Kharal's YouTube channel for an
    active livestream.

    yt-dlp exposes live_status values such as:
    is_live, is_upcoming, was_live, not_live.
    Only is_live is treated as LIVE.
    """

    try:
        import yt_dlp

        def extract():
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "ignoreerrors": True,
                "extract_flat": False,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(
                    PRANIXX_YOUTUBE_URL,
                    download=False,
                )

        info = await asyncio.to_thread(extract)

        if not isinstance(info, dict):
            return {
                "live": False,
                "platform": "YouTube",
                "url": PRANIXX_YOUTUBE_URL,
            }

        live_status = info.get(
            "live_status"
        )

        is_live = (
            live_status == "is_live"
            or info.get("is_live") is True
        )

        if not is_live:
            return {
                "live": False,
                "platform": "YouTube",
                "url": PRANIXX_YOUTUBE_URL,
                "live_status": live_status,
            }

        return {
            "live": True,
            "platform": "YouTube",
            "url": (
                info.get("webpage_url")
                or PRANIXX_YOUTUBE_URL
            ),
            "title": str(
                info.get("title")
                or "Pranixx is live on YouTube"
            ),
            "viewers": info.get(
                "concurrent_view_count"
            ),
            "started_at": (
                info.get("release_timestamp")
                or info.get("timestamp")
            ),
        }

    except asyncio.CancelledError:
        raise

    except Exception as error:
        print(
            "PRANIXX LIVE: YouTube check failed: "
            f"{error}"
        )
        return None


async def announce_live(
    client,
    info,
):
    channel_id = get_notify_channel_id()

    channel = client.get_channel(
        channel_id
    )

    if channel is None:
        try:
            channel = await client.fetch_channel(
                channel_id
            )
        except Exception as error:
            print(
                "PRANIXX LIVE: Discord channel "
                f"{channel_id} unavailable: {error}"
            )
            return False

    platform = info.get(
        "platform",
        "Platform",
    )

    title = str(
        info.get(
            "title",
            "Pranixx is live!",
        )
    ).strip()

    url = info.get("url", "")

    viewers = info.get("viewers")

    if isinstance(viewers, int):
        viewer_line = (
            f"👀 Viewers: **{viewers:,}**\n"
        )
    else:
        viewer_line = ""

    game = str(
        info.get("game") or ""
    ).strip()

    game_line = (
        f"🎮 Category: **{game}**\n"
        if game
        else ""
    )

    message = (
        "🔴 **PRANIXX IS LIVE!**\n\n"
        f"📺 Platform: **{platform}**\n"
        f"📝 **{title}**\n"
        f"{game_line}"
        f"{viewer_line}\n"
        f"🔗 {url}"
    )

    try:
        await channel.send(message)

        print(
            "PRANIXX LIVE: notification sent for "
            f"{platform}."
        )

        return True

    except Exception as error:
        print(
            "PRANIXX LIVE: Discord send failed: "
            f"{error}"
        )
        return False


async def monitor_pranixx_live(client):
    import aiohttp

    poll_seconds = get_poll_seconds()
    state = load_state()

    print(
        "PRANIXX LIVE: monitor started."
    )
    print(
        "PRANIXX LIVE: KICK = "
        f"{PRANIXX_KICK_USERNAME}"
    )
    print(
        "PRANIXX LIVE: YouTube = "
        "@PranishKharal"
    )
    print(
        "PRANIXX LIVE: Discord channel = "
        f"{get_notify_channel_id()}"
    )
    print(
        "PRANIXX LIVE: poll interval = "
        f"{poll_seconds}s"
    )

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(PRXNO Pranixx Live Monitor)"
        )
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:

        while True:
            cycle_started = time.monotonic()

            try:
                kick_info = (
                    await fetch_kick_live(
                        session
                    )
                )

                youtube_info = (
                    await fetch_youtube_live()
                )

                checks = {
                    "kick": kick_info,
                    "youtube": youtube_info,
                }

                for key, info in checks.items():

                    if info is None:
                        continue

                    is_live = bool(
                        info.get("live")
                    )

                    was_live = bool(
                        state.get(key, False)
                    )

                    # First successful pass establishes
                    # the baseline without sending a
                    # notification.
                    if not state.get(
                        "initialized",
                        False,
                    ):
                        state[key] = is_live
                        continue

                    # Notify only on OFFLINE -> LIVE.
                    if is_live and not was_live:

                        sent = await announce_live(
                            client,
                            info,
                        )

                        if not sent:
                            # Keep the old offline state
                            # so the next successful cycle
                            # retries the notification.
                            continue

                    state[key] = is_live

                    print(
                        "PRANIXX LIVE:",
                        info.get("platform"),
                        "LIVE"
                        if is_live
                        else "offline",
                    )

                if not state.get(
                    "initialized",
                    False,
                ):
                    state["initialized"] = True

                save_state(state)

            except asyncio.CancelledError:
                raise

            except Exception as error:
                print(
                    "PRANIXX LIVE: monitor cycle "
                    f"error: {error}"
                )

            elapsed = (
                time.monotonic()
                - cycle_started
            )

            await asyncio.sleep(
                max(
                    5.0,
                    poll_seconds - elapsed,
                )
            )
