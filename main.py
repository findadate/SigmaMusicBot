import json
import os
import asyncio
import random
import time
import aiohttp
from aiohttp import web
import subprocess
import sys
import logging
import warnings
import yt_dlp
import traceback
from datetime import datetime

# Change working directory to the script's directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Suppress warnings and logs for a cleaner terminal
warnings.filterwarnings("ignore", category=DeprecationWarning)

from highrise import BaseBot
from highrise.models import SessionMetadata, User, Position, AnchorPosition, CurrencyItem, Item

# Base directory for the bot
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_path(filename):
    return os.path.join(BASE_DIR, filename)

# ──────────────────────────────────────────────
# Embedded HTTP Relay Server (aiohttp)
# FFmpeg PUTs audio here; Highrise GETs /radio
# ──────────────────────────────────────────────
class StreamRelay:
    def __init__(self, port: int = 3000):
        self.port = port
        self.listeners: set = set()
        self._runner = None

    async def handle_push(self, request: web.Request) -> web.Response:
        """Receive audio stream from FFmpeg via HTTP PUT."""
        try:
            async for chunk in request.content.iter_any():
                dead = set()
                for resp in list(self.listeners):
                    try:
                        await resp.write(chunk)
                    except Exception:
                        dead.add(resp)
                self.listeners -= dead
        except Exception as e:
            print(f"[RELAY] Push error: {e}")
        # Close all listeners when the source ends
        for resp in list(self.listeners):
            try:
                await resp.write_eof()
            except Exception:
                pass
        self.listeners.clear()
        return web.Response(text="OK")

    async def handle_radio(self, request: web.Request) -> web.StreamResponse:
        """Serve audio stream to Highrise (or any HTTP listener)."""
        resp = web.StreamResponse(headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        self.listeners.add(resp)
        print(f"[RELAY] New listener connected. Total: {len(self.listeners)}")
        try:
            # Keep connection open until client disconnects
            while not request.transport.is_closing():
                await asyncio.sleep(1)
        except Exception:
            pass
        finally:
            self.listeners.discard(resp)
            print(f"[RELAY] Listener disconnected. Total: {len(self.listeners)}")
        return resp

    async def start(self):
        app = web.Application()
        app.router.add_put("/stream/push", self.handle_push)
        app.router.add_get("/radio", self.handle_radio)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        print(f"[RELAY] ✅ Relay server started on port {self.port}")
        print(f"[RELAY] 📻 Radio endpoint: http://0.0.0.0:{self.port}/radio")


class MyBot(BaseBot):
    def load_config(self):
        try:
            with open(get_path("config.json"), "r") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Critical error: Could not load config.json: {e}")
            config = {}
        if os.environ.get("BOT_TOKEN"):
            config["bot_token"] = os.environ["BOT_TOKEN"]
        if os.environ.get("ROOM_ID"):
            config["room_id"] = os.environ["ROOM_ID"]
        if os.environ.get("RADIO_PASSWORD"):
            config["radio_password"] = os.environ["RADIO_PASSWORD"]
        if os.environ.get("RADIO_HOST"):
            config["radio_host"] = os.environ["RADIO_HOST"]
        if os.environ.get("RADIO_PORT"):
            config["radio_port"] = int(os.environ["RADIO_PORT"])
        if os.environ.get("RADIO_MOUNT"):
            config["radio_mount"] = os.environ["RADIO_MOUNT"]
        if os.environ.get("RADIO_USERNAME"):
            config["radio_username"] = os.environ["RADIO_USERNAME"]
        if os.environ.get("RADIO_BITRATE"):
            config["radio_bitrate"] = int(os.environ["RADIO_BITRATE"])
        return config

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        self.bot_id = session_metadata.user_id
        self.song_queue = []
        self.user_requests = {}
        self.current_song = None
        self.song_stats = None
        self.start_time = time.time()
        
        # Immediate initialization to prevent race conditions during async sleep
        self.conv_ids = {}
        self.user_id_to_name = {}
        self.webapi = None
        self.stream_process = None
        self.silence_process = None
        self.relay = None
        self._stream_starting = False   # prevents multiple simultaneous stream starts
        self._autodj_searching = False  # prevents multiple simultaneous AutoDJ searches
        
        # Load configuration
        config = self.load_config()
        self.autodj = config.get("autodj", True)

        # Start embedded relay if configured (for Wispbyte / no-Express hosting)
        if config.get("use_embedded_relay", False):
            relay_port = int(config.get("relay_port", 3000))
            self.relay = StreamRelay(port=relay_port)
            await self.relay.start()

        # Start silence immediately so Highrise radio stays connected from the start
        await self._start_silence()

        print("[BOT] YouTube-only mode: all songs fetched via yt-dlp")

        self.default_english_songs = [
            "Shape of You - Ed Sheeran", "Blinding Lights - The Weeknd", "Stay - Justin Bieber",
            "Levitating - Dua Lipa", "Watermelon Sugar - Harry Styles", "Peaches - Justin Bieber",
            "Someone You Loved - Lewis Capaldi", "Bad Guy - Billie Eilish", "Perfect - Ed Sheeran",
            "Believer - Imagine Dragons", "Memories - Maroon 5", "Say You Won't Let Go - James Arthur",
            "Dance Monkey - Tones and I", "Shallow - Lady Gaga", "Senorita - Shawn Mendes",
            "Closer - The Chainsmokers", "Love Yourself - Justin Bieber", "7 Rings - Ariana Grande"
        ]
        
        print("Bot is alive and connected to the room!")
        
        # Wait for the bot to fully enter the room before performing actions
        await asyncio.sleep(10)
        
        try:
            config = self.load_config()
            bot_pos = config.get("bot_position")
            if bot_pos:
                pos = Position(bot_pos["x"], bot_pos["y"], bot_pos["z"], bot_pos["facing"])
                await self.highrise.walk_to(pos)
            
            # Load saved outfit
            bot_out_data = config.get("bot_outfit")
            if bot_out_data:
                try:
                    outfit = [Item(type=i["type"], amount=i.get("amount", 1), id=i["id"], 
                                    account_bound=i.get("account_bound", i.get("accountBound", False)), 
                                    active_palette=i.get("active_palette", i.get("palette", 0))) 
                                for i in bot_out_data]
                    await self.highrise.set_outfit(outfit)
                    print("Bot outfit loaded.")
                except Exception as e:
                    print(f"Error applying outfit: {e}")
        except Exception as e:
            print(f"Startup position/outfit error: {e}")

        # await self.highrise.chat("Hello there! I'm online.")
        
        try:
            if os.path.exists(get_path("conv_ids.json")):
                with open(get_path("conv_ids.json"), "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.conv_ids.update(data)
        except: pass

        # Start random looping emotes for the bot character
        self.emote_loop_task = asyncio.create_task(self.emote_loop())
        self.auto_dj_task = asyncio.create_task(self.auto_dj_loop())
        self.promo_slots_task = asyncio.create_task(self.promo_slots_loop())
        self.auto_invite_task = asyncio.create_task(self.auto_invite_loop())
        self.song_announcer_task = asyncio.create_task(self.song_announcer_loop())
        self.hindi_promo_task = asyncio.create_task(self.hindi_promo_loop())

    async def song_announcer_loop(self):
        """Announces the current song every 60 seconds if a song is playing."""
        while True:
            try:
                if hasattr(self, "song_queue") and len(self.song_queue) > 0:
                    if hasattr(self, "get_room_np_message"):
                        msg = self.get_room_np_message()
                    elif hasattr(self, "get_np_message"):
                        msg = self.get_np_message()
                    else:
                        song = self.song_queue[0]
                        song_name = song.get('song', 'Unknown')
                        requester = song.get('user', 'AutoDJ')
                        msg = f"🎶 Now Playing: {song_name} (Requested by @{requester})"
                    await self.highrise.chat(msg)
            except Exception as e:
                print(f"Error in song_announcer_loop: {e}")
            await asyncio.sleep(60)

    async def stop_stream(self):
        """Kills the current streaming process if it is running."""
        process = self.stream_process
        self.stream_process = None  # Detach immediately to avoid race conditions
        try:
            if process and process.returncode is None:
                process.terminate()
                await asyncio.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                print("Stream process stopped.")
        except Exception as e:
            print(f"Error stopping stream: {e}")
        # Restart silence to keep Highrise connected between songs
        await asyncio.sleep(0.3)
        await self._start_silence()

    def _get_relay_push_url(self):
        """Returns the correct local relay URL based on config."""
        config = self.load_config()
        if config.get("use_embedded_relay", False):
            port = int(config.get("relay_port", 3000))
            return f"http://localhost:{port}/stream/push"
        return "http://localhost:8080/api/stream/push"

    def _silence_command(self):
        """FFmpeg command that generates valid MP3 silence and PUTs it to the relay."""
        return [
            "ffmpeg", "-loglevel", "quiet",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-vn", "-acodec", "libmp3lame", "-ab", "96k",
            "-ar", "44100", "-ac", "2",
            "-f", "mp3", "-method", "PUT",
            self._get_relay_push_url()
        ]

    async def _start_silence(self):
        """Start a silence FFmpeg so Highrise radio doesn't disconnect between songs."""
        await self._stop_silence()
        try:
            self.silence_process = subprocess.Popen(
                self._silence_command(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print("[SILENCE] 🔇 Silence stream started (keeps Highrise connected).")
        except Exception as e:
            print(f"[SILENCE] Failed to start silence: {e}")

    async def _stop_silence(self):
        """Kill the silence FFmpeg process."""
        proc = self.silence_process
        self.silence_process = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                await asyncio.sleep(0.3)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    async def get_direct_audio_url(self, youtube_url: str):
        """Uses yt-dlp to get the direct audio stream URL."""
        try:
            # Added more robust arguments: no-check-certificate, no-cache-dir, and android player client
            # The player-client=android often bypasses some bot detection
            command = [
                sys.executable, "-m", "yt_dlp",
                "-g",
                "-f", "bestaudio/best",
                "--no-check-certificate",
                "--no-cache-dir",
                "--no-warnings",
                "--extractor-args", "youtube:player-client=android",
                youtube_url
            ]
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                return stdout.decode().strip()
            else:
                err_msg = stderr.decode()
                print(f"yt-dlp error: {err_msg}")
                # Fallback without extractor args if it failed because of them
                if "extractor-args" in err_msg:
                    command = [sys.executable, "-m", "yt_dlp", "-g", "-f", "bestaudio/best", youtube_url]
                    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await process.communicate()
                    if process.returncode == 0:
                        return stdout.decode().strip()
                return None

        except Exception as e:
            print(f"Error getting direct URL: {e}")
            return None

    async def play_song_to_icecast(self, song_info):
        """Starts a new ffmpeg process to stream a song to Icecast. Returns True if started, False otherwise."""
        yt_link = song_info.get("link")
        song_title = song_info.get("song", "Unknown Song")
        if not yt_link:
            print(f"[STREAM] ❌ No YouTube link for '{song_title}', skipping.")
            return False

        print(f"[STREAM] ▶ Starting: '{song_title}'")
        print(f"[STREAM] 🔗 YouTube link: {yt_link}")

        # Stop silence + any existing stream first
        await self._stop_silence()
        await asyncio.sleep(0.3)
        process = self.stream_process
        self.stream_process = None
        if process and process.poll() is None:
            try:
                process.terminate()
                await asyncio.sleep(0.3)
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass

        print(f"[STREAM] 🔍 Fetching direct audio URL via yt-dlp...")
        direct_url = await self.get_direct_audio_url(yt_link)
        if direct_url:
            print(f"[STREAM] ✅ Direct URL obtained ({len(direct_url)} chars)")
        else:
            print(f"[STREAM] ❌ yt-dlp FAILED to get direct URL for '{song_title}'")
            return False

        try:
            config = self.load_config()

            # Build icecast URL from separate fields (preferred) or fall back to icecast_url
            radio_host     = config.get("radio_host", "")
            radio_port     = config.get("radio_port", 80)
            radio_username = config.get("radio_username", "source")
            radio_password = config.get("radio_password", "")
            radio_mount    = config.get("radio_mount", "/stream")
            radio_bitrate  = config.get("radio_bitrate", 64)

            if radio_host and radio_password:
                icecast_url = f"icecast://{radio_username}:{radio_password}@{radio_host}:{radio_port}{radio_mount}"
            else:
                icecast_url = config.get("icecast_url", "")

            if not icecast_url:
                print("[STREAM ERROR] No radio server details in config.json. Set radio_host, radio_port, radio_password, radio_mount.")
                return False

            title      = song_info.get("song", "Unknown Song")
            safe_title = "".join([c if ord(c) < 128 else " " for c in title]).replace('"', "'")
            bitrate_str = f"{radio_bitrate}k"

            local_relay_url = self._get_relay_push_url()
            command = [
                "ffmpeg",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-re",
                "-i", direct_url,
                # --- Output 1: Caster.fm Icecast ---
                "-vn", "-acodec", "libmp3lame", "-ab", bitrate_str,
                "-ar", "44100", "-ac", "2",
                "-content_type", "audio/mpeg",
                "-ice_name", safe_title,
                "-ice_description", "Sigma Music Bot",
                "-ice_genre", "Various",
                "-legacy_icecast", "1",
                "-f", "mp3",
                icecast_url,
                # --- Output 2: Local HTTPS relay (for Highrise) ---
                "-vn", "-acodec", "libmp3lame", "-ab", bitrate_str,
                "-ar", "44100", "-ac", "2",
                "-f", "mp3",
                "-method", "PUT",
                local_relay_url
            ]

            print(f"[STREAM] 🌐 Connecting → {radio_host}:{radio_port}{radio_mount} @ {bitrate_str}")
            print(f"[STREAM] 🌐 Local relay → {local_relay_url}")
            print(f"[STREAM] 🎵 Song: {title}")
            print(f"[STREAM] 📡 Icecast URL: icecast://source:***@{radio_host}:{radio_port}{radio_mount}")

            proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )
            self.stream_process = proc

            # Wait briefly and check if ffmpeg immediately exited (connection error)
            print(f"[STREAM] ⏳ Waiting 3s to verify connection...")
            await asyncio.sleep(3)
            # Use local ref in case self.stream_process was changed by another coroutine
            if proc.poll() is not None:
                stderr_output = proc.stderr.read().decode("utf-8", errors="ignore")
                if "401" in stderr_output or "Unauthorized" in stderr_output or "uthentication" in stderr_output:
                    print("[STREAM ERROR] Authentication Failed — Check radio_password in config.json")
                elif "Connection refused" in stderr_output or "Connection reset" in stderr_output:
                    print("[STREAM ERROR] Connection Refused — Check radio_host and radio_port in config.json")
                elif "403" in stderr_output or "Forbidden" in stderr_output:
                    print("[STREAM ERROR] Access Forbidden — Wrong mount point or server rejected stream")
                elif "Name or service not known" in stderr_output or "nodename" in stderr_output:
                    print(f"[STREAM ERROR] DNS Error — Hostname '{radio_host}' not found. Check radio_host.")
                elif stderr_output.strip():
                    print(f"[STREAM ERROR] FFmpeg exited immediately:\n{stderr_output[-600:]}")
                else:
                    print("[STREAM ERROR] FFmpeg exited immediately — unknown error")
                self.stream_process = None
                await self._start_silence()
                return False

            # FFmpeg is running — stream is live!
            print(f"[STREAM] ✅ Audio streaming LIVE to Caster.fm!")
            print(f"[STREAM] 📻 Listeners can tune in at: http://{radio_host}:{radio_port}{radio_mount}")
            # Monitor ffmpeg stderr for future errors in background
            asyncio.create_task(self._monitor_stream_stderr())
            return True

        except Exception as e:
            print(f"[STREAM ERROR] Failed to start stream process: {e}")
            import traceback
            traceback.print_exc()
            await self._start_silence()
            return False

    async def _monitor_stream_stderr(self):
        """Reads ffmpeg stderr in background — logs errors and data-sent stats."""
        try:
            if not self.stream_process or not self.stream_process.stderr:
                return
            loop = asyncio.get_event_loop()
            start_time = time.time()
            last_audio_log = 0
            while self.stream_process and self.stream_process.poll() is None:
                line = await loop.run_in_executor(None, self.stream_process.stderr.readline)
                if not line:
                    break
                decoded = line.decode("utf-8", errors="ignore").strip()
                if not decoded:
                    continue
                elapsed = time.time() - start_time
                # First 15 seconds: print everything so we can see connection handshake
                if elapsed < 15:
                    print(f"[FFMPEG] {decoded}")
                # After 15s: print errors AND audio data stats (kB sent)
                elif any(kw in decoded for kw in [
                    "Error", "error", "failed", "refused", "401", "403",
                    "forbidden", "reset", "Broken", "pipe", "auth"
                ]):
                    print(f"[FFMPEG ERROR] {decoded}")
                elif "audio:" in decoded and "KiB" in decoded:
                    # Print data-sent stats every 60 seconds
                    if time.time() - last_audio_log >= 60:
                        print(f"[FFMPEG DATA] {decoded}")
                        last_audio_log = time.time()
            # Print final exit summary
            exit_code = self.stream_process.poll() if self.stream_process else None
            print(f"[FFMPEG] Stream process finished with exit code {exit_code}")
        except Exception as ex:
            print(f"[FFMPEG MONITOR ERROR] {ex}")

    async def auto_dj_loop(self):
        """Manages the music queue and the Icecast streaming process."""
        while True:
            await asyncio.sleep(2)
            try:
                # 1. Check if the current stream has finished or died
                if self.stream_process is not None:
                    if self.stream_process.poll() is not None:
                        print(f"Stream process finished naturally with code {self.stream_process.returncode}.")
                        self.stream_process = None
                        self._stream_starting = False
                        # Remove the song that just finished
                        if len(self.song_queue) > 0:
                            self.song_queue.pop(0)

                # If a stream is already being started, skip this iteration
                if self._stream_starting:
                    continue

                # 2. If no stream running and songs in queue → start next song
                if self.stream_process is None and len(self.song_queue) > 0:
                    self._stream_starting = True
                    current_song = self.song_queue[0]
                    song_name = current_song.get("song", "Unknown")
                    duration = current_song.get("duration", 210)

                    print(f"Auto DJ: Preparing next song '{song_name}'")
                    self.set_song_stats(song_name, time.time(), duration)

                    success = await self.play_song_to_icecast(current_song)
                    self._stream_starting = False

                    if success:
                        await asyncio.sleep(2)
                        try:
                            msg = self.get_room_np_message()
                            await self.highrise.chat(msg)
                        except Exception:
                            pass
                    else:
                        print(f"Auto DJ: Skipping '{song_name}' due to playback failure.")
                        try:
                            await self.highrise.chat(f"⚠️ Error: Could not play '{song_name}'. Skipping...")
                        except Exception:
                            pass
                        if len(self.song_queue) > 0:
                            self.song_queue.pop(0)
                    continue

                # 3. AutoDJ: queue empty + no stream + not already searching
                if (self.autodj
                        and self.stream_process is None
                        and len(self.song_queue) == 0
                        and not self._autodj_searching):
                    self._autodj_searching = True
                    random_song = random.choice(self.default_english_songs)
                    print(f"AutoDJ: Queue empty, picking random English song: {random_song}")

                    yt_title, yt_link, yt_duration = await self.search_youtube_song(random_song)
                    self._autodj_searching = False

                    if yt_title:
                        self.song_queue.append({
                            "user": "AutoDJ",
                            "song": yt_title,
                            "duration": yt_duration,
                            "link": yt_link
                        })
                        print(f"AutoDJ: Added '{yt_title}' to queue.")
                    else:
                        print(f"AutoDJ: Could not find '{random_song}'. Waiting 30s...")
                        await asyncio.sleep(30)

            except Exception as e:
                self._stream_starting = False
                self._autodj_searching = False
                print(f"Error in auto_dj_loop: {e}")

    async def promo_slots_loop(self):
        """Broadcasts a promo message every 11 minutes."""
        promo_msg = (
            "🔔 Ding Ding!\n"
            "Tip 💛 1 Gold → Get 🎰 1 Music Slot Spin!\n"
            "Feel the bass 🎶\n"
            "Feel the vibe 🔥\n"
            "Let the slot music take control 💎✨"
        )
        while True:
            try:
                await self.highrise.chat(promo_msg)
            except Exception:
                pass
            await asyncio.sleep(660) # 11 minutes

    async def hindi_promo_loop(self):
        """Broadcasts Hindi tracks promo every 7 minutes."""
        msg = "🎵 MAUKA TOO GOOD! 😎 Vibing to the best Hindi tracks 🔥! Type -play [song name] to request your anthem! 💖"
        while True:
            try:
                await self.highrise.chat(msg)
            except Exception:
                pass
            await asyncio.sleep(420) # 7 minutes

    async def auto_invite_loop(self):
        """Sends room invites every 45 minutes."""
        while True:
            try:
                # Load config for room_id
                try:
                    with open(get_path("config.json"), "r") as f:
                        config = json.load(f)
                except:
                    config = {}
                
                room_id = config.get("room_id", "Unknown")
                if room_id == "Unknown":
                    continue

                jokes = [
                    "Why did the music teacher need a ladder? To reach the high notes! 🥁",
                    "Why do music bots make terrible liars? Because you can always read their pitch! 🤖🎶",
                    "What does a robot DJ eat for a snack? Micro-chips and salsa! 🍟🎵",
                    "Why did the VIP go to jail? Because he got caught in a jam! 🍓🚓",
                    "What happens when you play Beethoven backwards? He decomposes. 🎹💀",
                    "How do you make a bandstand? Take away their chairs! 🪑🎺",
                    "What is a ghost's favorite kind of music? Rhythm and BOO-s! 👻🎧",
                    "What kind of music do balloons hate? Pop music! 🎈💥"
                ]
                music_msgs = [
                    "Hey! We're vibing to some great music right now and you're invited! 🎶",
                    "The DJ is dropping the hottest tracks in the room! Come request your favorite song. 🎧",
                    "Need a break? Join our chill music session and relax with us! 🎵",
                    "Non-stop bangers playing right now! You definitely don't want to miss this. 🎸"
                ]
                vip_msgs = [
                    "Experience our custom song queue and get exclusive VIP perks! 💎",
                    "Unlock premium VIP access, skip the queue, and show off your golden status! ✨",
                    "Special roles and commands are ready for our VIP members. Don't miss out! 👑",
                    "Get VIP now to permanently secure your exclusive room benefits! 💰"
                ]

                response = await self.highrise.get_conversations()
                conversations = []
                if hasattr(response, "conversations"):
                    conversations = response.conversations
                elif isinstance(response, tuple) and len(response) > 0:
                    conversations = response[0]
                
                if not conversations:
                    continue

                for conv in conversations:
                    joke = random.choice(jokes)
                    m_msg = random.choice(music_msgs)
                    v_msg = random.choice(vip_msgs)
                    
                    colors = ["#FF69B4", "#00FFFF", "#32CD32", "#FF4500", "#9370DB", "#FFD700", "#00FA9A", "#1E90FF", "#FF6347", "#DA70D6", "#FF1493", "#7FFF00", "#00BFFF", "#FF8C00", "#FFA500"]
                    c1 = random.choice(colors)
                    c2 = random.choice(colors)
                    c3 = random.choice(colors)
                    c4 = random.choice(colors)
                    
                    invite_msg = (f"<{c1}>✨ ━━ 🎵 𝐉𝐎𝐈𝐍 𝐓𝐇𝐄 𝐏𝐀𝐑𝐓𝐘! 🎵 ━━ ✨\n\n"
                                  f"<{c2}>{m_msg}\n"
                                  f"<{c3}>{v_msg}\n\n"
                                  f"<{c1}>Joke of the moment:\n"
                                  f"<{c4}>{joke}\n\n"
                                  f"<{c2}>Click the Enter Room button and join the party! 🎉")
                    try:
                        await self.highrise.send_message(conv.id, invite_msg)
                        await asyncio.sleep(0.5)
                        await self.highrise.send_message(conv.id, "Click below to join!", message_type="invite", room_id=room_id)
                        await asyncio.sleep(0.5)
                    except:
                        pass
            except:
                pass
            await asyncio.sleep(2700) # 45 minutes

    async def emote_loop(self):
        try:
            with open(get_path("botemotes.json"), "r") as f:
                emotes = json.load(f)
                
            # Remove duplicated emotes for the bot's random pool
            emote_pool = []
            seen_ids = set()
            for key, val in emotes.items():
                if val["id"] not in seen_ids:
                    emote_pool.append(val)
                    seen_ids.add(val["id"])
            
            while True:
                if not emote_pool:
                    await asyncio.sleep(10)
                    continue
                
                emote = random.choice(emote_pool)
                emote_id = emote.get("id")
                # Ensure a minimum sleep duration
                duration = max(emote.get("duration", 5.0), 3.0) 
                
                try:
                    await self.highrise.send_emote(emote_id, self.bot_id)
                except Exception as e:
                    pass
                
                await asyncio.sleep(duration + 0.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Emote loop exception: {e}")

    def is_privileged(self, username, config):
        """Check if a user is an Owner, Moderator, or VIP."""
        owners = config.get("owners", [])
        mods = config.get("moderators", [])
        try:
            with open(get_path("vip_users.json"), "r") as f:
                vip_users = json.load(f)
        except Exception:
            vip_users = {}
            
        modified = False
        import time
        for u in list(vip_users.keys()):
            vip_data = vip_users[u]
            if isinstance(vip_data, dict) and vip_data.get("type") == "temp":
                timestamp = vip_data.get("timestamp", 0)
                if time.time() - timestamp > 86400:  # 24 hours in seconds
                    del vip_users[u]
                    modified = True
        
        if modified:
            try:
                with open(get_path("vip_users.json"), "w") as f:
                    json.dump(vip_users, f, indent=4)
            except Exception:
                pass
                
        return username in owners or username in mods or username in vip_users

    def is_subscribed(self, username):
        """Check if a user is subscribed to the bot."""
        try:
            if not os.path.exists(get_path("subscribed_users.json")):
                return False
            with open(get_path("subscribed_users.json"), "r") as f:
                subs = json.load(f)
            return username in subs
        except: return False

    def get_slots(self, user_id, username):
        """Get the current slot amount for a user."""
        try:
            with open(get_path("slots.json"), "r") as f:
                slots = json.load(f)
        except Exception:
            slots = {}
        
        if user_id not in slots:
            slots[user_id] = {"username": username, "amount": 0}
            with open(get_path("slots.json"), "w") as f:
                json.dump(slots, f, indent=4)
        
        return slots[user_id]["amount"]
    

    async def send_user_dm(self, user_id, message_text):
        """Send a DM to a specific user via conversation history, falls back to whisper."""
        try:
            # Split message into chunks to avoid Highrise character limits (approx 2000 for DMs)
            chunks = [message_text[i:i+1800] for i in range(0, len(message_text), 1800)]
            
            str_user_id = str(user_id)
            conv_id = getattr(self, "conv_ids", {}).get(str_user_id)
            
            # If we don't have a cached ID, try to find it once
            if not conv_id:
                try:
                    response = await self.highrise.get_conversations()
                    conversations = []
                    if hasattr(response, "conversations"):
                        conversations = response.conversations
                    elif isinstance(response, tuple) and len(response) > 0:
                        conversations = response[0]
                    
                    for conv in conversations:
                        if hasattr(conv, "member_ids") and conv.member_ids and str_user_id in conv.member_ids:
                            conv_id = conv.id
                            self.conv_ids[str_user_id] = conv_id
                            try:
                                with open(get_path("conv_ids.json"), "w") as f:
                                    json.dump(self.conv_ids, f)
                            except: pass
                            break
                except Exception as e:
                    print(f"Error fetching conversations: {e}")

            if conv_id:
                try:
                    for chunk in chunks:
                        await self.highrise.send_message(conv_id, chunk)
                        await asyncio.sleep(0.3)
                    return
                except Exception as e:
                    print(f"Failed to send DM via conv_id: {e}")

            # Fallback to Whisper if DM failed or no conv_id found
            # Whispers are much shorter (often 250-300 chars)
            whisper_chunks = [message_text[i:i+250] for i in range(0, len(message_text), 250)]
            try:
                for chunk in whisper_chunks:
                    await self.highrise.send_whisper(user_id, chunk)
                    await asyncio.sleep(0.3)
            except Exception as e:
                print(f"Whisper fallback failed as well: {e}")
        except Exception as e:
            print(f"Global error in send_user_dm: {e}")

    async def send_room_chunks(self, message_text, chunk_size=250):
        """Send a message to room chat, splitting into chunks if necessary (safely)."""
        lines = message_text.split("\n")
        current_chunk = ""
        
        for line in lines:
            # If the line itself is too long, we must split it (rare but possible)
            if len(line) > chunk_size:
                if current_chunk:
                    await self.highrise.chat(current_chunk.strip())
                    await asyncio.sleep(0.5)
                    current_chunk = ""
                
                # Split long line into parts
                for i in range(0, len(line), chunk_size):
                    await self.highrise.chat(line[i:i+chunk_size])
                    await asyncio.sleep(0.5)
                continue

            if len(current_chunk) + len(line) + 1 > chunk_size:
                await self.highrise.chat(current_chunk.strip())
                await asyncio.sleep(0.5)
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
        
        if current_chunk:
            await self.highrise.chat(current_chunk.strip())
            
    async def send_dm_id_chunks(self, conversation_id, message_text, chunk_size=1800):
        """Send a message to a specific DM conversation ID, splitting into chunks."""
        chunks = [message_text[i:i+chunk_size] for i in range(0, len(message_text), chunk_size)]
        for chunk in chunks:
            await self.highrise.send_message(conversation_id, chunk)
            await asyncio.sleep(0.4)

    async def broadcast_dm(self, message_text):
        """Send a DM to everyone in the bot's conversation history."""
        try:
            response = await self.highrise.get_conversations()
            conversations = []
            if hasattr(response, "conversations"):
                conversations = response.conversations
            elif isinstance(response, tuple) and len(response) > 0:
                conversations = response[0]
            
            for conv in conversations:
                try:
                    await self.highrise.send_message(conv.id, message_text)
                    await asyncio.sleep(0.5) # Rate limit protection
                except Exception as e:
                    print(f"Failed to broadcast to {conv.id}: {e}")
        except Exception as e:
            print(f"Error in broadcast_dm fetch: {e}")

    def consume_slot(self, user_id, username):
        """Consume 1 slot for a user."""
        try:
            with open(get_path("slots.json"), "r") as f:
                slots = json.load(f)
            
            user_id = str(user_id)
            if user_id in slots and slots[user_id]["amount"] > 0:
                slots[user_id]["amount"] -= 1
                with open(get_path("slots.json"), "w") as f:
                    json.dump(slots, f, indent=4)
                return True
        except Exception:
            pass
        return False

    def set_song_stats(self, song_name: str, start_time: float, duration: int):
        try:
            with open(get_path("song_stats_persist.json"), "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
            
        import random
        song_key = song_name.lower().strip()
        song_data = data.get(song_key, {"likes": [], "dislikes": [], "listens": random.randint(3, 10), "duration": duration})
        song_data["listens"] += 1
        song_data["duration"] = duration # Update duration in case it changed
        data[song_key] = song_data
        
        try:
            with open(get_path("song_stats_persist.json"), "w") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass
            
        self.song_stats = {
            "likes": set(song_data["likes"]),
            "dislikes": set(song_data["dislikes"]),
            "listens": song_data["listens"],
            "start_time": start_time,
            "duration": duration
        }

    def _persist_like_dislike(self, song_name: str):
        try:
            with open(get_path("song_stats_persist.json"), "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
        song_key = song_name.lower().strip()
        
        # Get current lists from set
        likes_list = list(self.song_stats.get("likes", set()))
        dislikes_list = list(self.song_stats.get("dislikes", set()))
        listens_count = self.song_stats.get("listens", 1)
        duration = self.song_stats.get("duration", 210)
        
        song_data = data.get(song_key, {"likes": [], "dislikes": [], "listens": listens_count, "duration": duration})
        song_data["likes"] = likes_list
        song_data["dislikes"] = dislikes_list
        song_data["duration"] = duration
        data[song_key] = song_data
        try:
            with open(get_path("song_stats_persist.json"), "w") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    async def delayed_np_message(self):
        await asyncio.sleep(2)
        song_msg = self.get_room_np_message()
        await self.highrise.chat(song_msg)

    def get_room_np_message(self) -> str:
        """Simple, stylish 'Now Playing' format for the room chat."""
        if not hasattr(self, "song_queue") or len(self.song_queue) == 0:
            return "No song is currently playing!"

        if not hasattr(self, "song_stats") or not self.song_stats:
            duration = self.song_queue[0].get("duration", 210)
            self.set_song_stats(self.song_queue[0]["song"], time.time(), duration)

        current = self.song_queue[0]
        song_name = current["song"]
        requester = current['user']
        duration = self.song_stats.get("duration", 210)
        
        mins_total = duration // 60
        secs_total = duration % 60
        
        parts = song_name.split("-", 1)
        title = parts[1].strip() if len(parts) == 2 else song_name
        
        likes = len(self.song_stats.get("likes", set()))
        listens = self.song_stats.get("listens", 0)

        # Truncate title to avoid Highrise 256-char message limit
        title_short = (title[:38] + "..") if len(title) > 40 else title

        return (f"<#FFD700>🎵 Now Playing:\n"
                f"<#cdaa54>{title_short}\n"
                f"<#e2ce9d>▷ ({mins_total}:{secs_total:02d}) | 👤 @{requester}\n"
                f"<#e2ce9d>❤️ {likes} likes | 🎧 {listens} listens\n"
                f"<#B8860B>-like / -dislike / -np")

    def get_np_message(self) -> str:
        if not hasattr(self, "song_queue"):
            self.song_queue = []
            
        if getattr(self, "current_song", None) and len(self.song_queue) == 0:
            self.song_queue.append({"song": self.current_song, "user": "DJ_Bot"})
            self.current_song = None

        if len(self.song_queue) == 0:
            return ("🎵 No song is currently playing!\n\n"
                    "The queue is empty. Use -play [song name] to request a song! 🎶")

        if not hasattr(self, "song_stats") or not self.song_stats:
            self.set_song_stats(self.song_queue[0]["song"], time.time(), 210)

        duration = self.song_stats.get("duration", 210)
        current = self.song_queue[0]
        song_name = current["song"]
        requester = f"@{current['user']}"
            
        parts = song_name.split("-", 1)
        if len(parts) == 2:
            title = parts[1].strip()
        else:
            title = song_name

        start_time = self.song_stats.get("start_time", time.time())
        elapsed = time.time() - start_time
        if elapsed < 0:
            elapsed = 0

        progress_pct = int((elapsed / duration) * 100)
        if progress_pct > 100:
            progress_pct = 100
            
        total_bars = 10
        filled_bars = int((progress_pct / 100) * total_bars)
        empty_bars = total_bars - filled_bars
        progress_bar = "━" * filled_bars + "◉" + "─" * empty_bars

        mins_elapsed = int(elapsed // 60)
        secs_elapsed = int(elapsed % 60)
        mins_total = int(duration // 60)
        secs_total = int(duration % 60)
        
        likes = len(self.song_stats.get("likes", set()))
        dislikes = len(self.song_stats.get("dislikes", set()))
        listens = self.song_stats.get("listens", 0)

        colors = ["#FF69B4", "#00FFFF", "#32CD32", "#FF4500", "#9370DB", "#FFD700", "#00FA9A", "#1E90FF", "#FF6347", "#DA70D6", "#FF1493", "#7FFF00", "#00BFFF", "#FF8C00", "#FFA500"]
        c1 = random.choice(colors)
        c2 = random.choice(colors)
        c3 = random.choice(colors)

        return (f"<{c1}>✨ ━━ 🎧 𝐍𝐎𝐖 𝐏𝐋𝐀𝐘𝐈𝐍𝐆 ━━ ✨\n\n"
                f"<{c2}>🎶 𝙎𝙤𝙣𝙜: {title}\n"
                f"<{c3}>👤 𝙍𝙚𝙦𝙪𝙚𝙨𝙩: {requester}\n\n"
                f"<{c3}>{mins_elapsed:02d}:{secs_elapsed:02d} <#FADBD8>{progress_bar} <{c3}>{mins_total:02d}:{secs_total:02d} ({progress_pct}%)\n"
                f"<{c1}>⟲       ◁         ‖         ▷       ⟳\n\n"
                f"<{c2}>❤️ {likes}   |   👎 {dislikes}   |   🎧 {listens} Listens")

    def get_formatted_queue(self, detailed=True) -> str:
        """Generates a stylishly formatted queue list. Detailed=True for DMs, False for compact room view."""
        if not hasattr(self, "song_queue") or len(self.song_queue) <= 1:
            return "<#B8860B>✨ The song queue is currently empty! ✨"

        q_msg = "\n<#B8860B>✨ ━━ 🎧 𝐐𝐔𝐄𝐔𝐄 𝐋𝐈𝐒𝐓 ━━ ✨\n\n"
        
        # Load persistent stats for historical data
        try:
            with open(get_path("song_stats_persist.json"), "r") as f:
                all_stats = json.load(f)
        except: all_stats = {}

        # Only show up to 10 songs to prevent overflow
        display_queue = self.song_queue[1:11] 

        for i, song_info in enumerate(display_queue):
            song_name = song_info['song']
            user_name = song_info['user']
            song_key = song_name.lower().strip()
            duration = song_info.get("duration", 210)
            mins = duration // 60
            secs = duration % 60
            
            # Get title only (strip singer if format is Singer - Song)
            parts = song_name.split("-", 1)
            title = parts[1].strip() if len(parts) == 2 else song_name
            pos = f"{i + 1}."
            
            if detailed:
                # Detailed format for DMs
                stats = all_stats.get(song_key, {})
                likes = len(stats.get("likes", []))
                listens = stats.get("listens", 0)
                q_msg += f"{pos} ▶️ <#cdaa54>{title}\n"
                q_msg += f"   <#e2ce9d>⏱️{mins}:{secs:02d} | 👤@{user_name}  | ❤️ {likes} | 🎧 {listens}\n\n"
            else:
                # Compact format for Room/Whisper (prevents huge bubbles)
                q_msg += f"{pos} <#cdaa54>{title} <#e2ce9d>({mins}:{secs:02d}) @{user_name}\n"
            
        if len(self.song_queue) > 11:
            q_msg += f"\n<#B8860B>...and {len(self.song_queue) - 11} more! Type -q in DMs for full list. 📩"
            
        return q_msg

    async def search_youtube_song(self, query: str):
        """
        ULTIMATE SEARCH ENGINE: 
        Uses yt-dlp directly for a single-step search.
        Returns (title, link, duration_seconds).
        """
        if not query or not isinstance(query, str):
            return None, None, 0
            
        target_query = query.strip()
        
        # 1. Check if it's already a URL
        is_url = "://" in target_query
        search_term = target_query if is_url else f"ytsearch1:{target_query}"

        print(f"Search Manager: Searching for '{target_query}' (Single Stage)")
        
        try:
            ytdlp_cmd = [
                sys.executable, "-m", "yt_dlp",
                "--get-title", "--get-id", "--get-duration",
                "--no-warnings", "--no-check-certificate",
                "--format", "bestaudio/best",
                search_term
            ]
            proc = await asyncio.create_subprocess_exec(
                *ytdlp_cmd, 
                stdout=asyncio.subprocess.PIPE, 
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().split('\n')
            
            if len(lines) >= 3:
                yt_t = lines[0]
                yt_id = lines[1]
                yt_dur = lines[2]
                
                # Convert duration string to seconds
                seconds = 210
                try:
                    p = str(yt_dur).split(':')
                    if len(p) == 2: seconds = int(p[0])*60 + int(p[1])
                    elif len(p) == 3: seconds = int(p[0])*3600 + int(p[1])*60 + int(p[2])
                    else: seconds = int(yt_dur)
                except: pass
                
                video_url = target_query if is_url else f"https://www.youtube.com/watch?v={yt_id}"
                print(f"Search Manager: Found '{yt_t}'")
                return yt_t, video_url, seconds
                
        except Exception as e:
            print(f"Search Manager: Search failed: {e}")

        return None, None, 0

    async def on_user_join(self, user: User, position: Position | AnchorPosition) -> None:
        print(f"{user.username} joined the room.")
        await self.highrise.chat(f"🎉 @{user.username} just joined the party! 🎉 Type -sub to unlock exclusive perks and enjoy the music 🎵!")
        
        # Give 5 free slots to new users
        try:
            try:
                with open(get_path("slots.json"), "r") as f:
                    slots = json.load(f)
            except Exception:
                slots = {}
            
            user_id = str(user.id)
            if user_id not in slots:
                slots[user_id] = {"username": user.username, "amount": 5}
                with open(get_path("slots.json"), "w") as f:
                    json.dump(slots, f, indent=4)
                await self.highrise.send_whisper(user.id, "🎁 Welcome! You've been given 5 FREE slots to play music. Enjoy! 🎵")
        except Exception as e:
            print(f"Error in on_user_join slots: {e}")

    async def on_tip(self, sender: User, receiver: User, tip: CurrencyItem | str) -> None:
        """Called when a tip is sent in the room."""
        if receiver.id != self.bot_id:
            return
            
        try:
            with open("balances.json", "r") as f:
                balances = json.load(f)
        except Exception:
            balances = {}
            
        user_id = str(sender.id)
        if user_id not in balances:
            balances[user_id] = {"username": sender.username, "gold": 0}
            
        # Support different SDK versions where tip might be an object with .amount or just an int/string
        amount = getattr(tip, "amount", 0)
        if not amount and isinstance(tip, int):
            amount = tip
            
        balances[user_id]["gold"] += amount
        balances[user_id]["username"] = sender.username
        
        with open("balances.json", "w") as f:
            json.dump(balances, f, indent=4)
        
        # Awarding slots on tip has been disabled. Users must now use -buyslot.
        await self.highrise.send_whisper(sender.id, f"💎 Thank you for the {amount} gold tip! Your new gold balance is {balances[user_id]['gold']}. Use -buyslot to convert gold to music slots.")


    async def on_chat(self, user: User, message: str) -> None:
        """Called when a user sends a message in the room."""
        try:
            try:
                with open(get_path("config.json"), "r") as f:
                    config = json.load(f)
                    owners = config.get("owners", [])
            except Exception:
                config = {}
                owners = []

            self.user_id_to_name[str(user.id)] = user.username
            msg = message.lower().strip()
        
            # Block Checker: If you are in blocked_users, you can't use anything.
            # Note: Owners are immune to blocking for safety.
            if msg.startswith("-") and user.username in config.get("blocked_users", []) and user.username not in owners:
                if not msg.startswith("-unblock"): # Safety redundancy: Unblock is always checked below if you have perms
                    await self.highrise.send_whisper(user.id, "🚫 You are BLOCKED from using bot commands.")
                    return
            
            if msg == "-ping":
                await self.highrise.chat(f"Pong! {user.username}")
    
            elif msg == "-sub":
                try:
                    if os.path.exists(get_path("subscribed_users.json")):
                        with open(get_path("subscribed_users.json"), "r") as f:
                            subs = json.load(f)
                    else:
                        subs = []
                    
                    if user.username not in subs:
                        subs.append(user.username)
                        with open(get_path("subscribed_users.json"), "w") as f:
                            json.dump(subs, f, indent=4)
                        await self.highrise.chat(f"✅ @{user.username}, you are now SUBSCRIBED! All music commands are UNLOCKED for you. 🎉")
                        
                        # Proactively try to establish a DM conversation so future updates work
                        welcome_dm = (
                            f"🌟 <#B8860B>𝐇𝐞𝐥𝐥𝐨 @{user.username}! 🌟\n\n"
                            f"🎉 You have successfully subscribed to the Music Bot! You can now use all commands like -np, -q, and -play in the room or right here in DMs.\n\n"
                            f"🎶 Enjoy the music!"
                        )
                        await self.send_user_dm(user.id, welcome_dm)
                    else:
                        await self.highrise.send_whisper(user.id, "You are already subscribed!")
                except Exception as e:
                    print(f"Error in -sub: {e}")
    
            elif msg == "-unsub":
                try:
                    if os.path.exists(get_path("subscribed_users.json")):
                        with open(get_path("subscribed_users.json"), "r") as f:
                            subs = json.load(f)
                        if user.username in subs:
                            subs.remove(user.username)
                            with open(get_path("subscribed_users.json"), "w") as f:
                                json.dump(subs, f, indent=4)
                            await self.highrise.chat(f"❌ @{user.username}, you have UNSUBSCRIBED. Music commands are now LOCKED. Type -sub to unlock again.")
                        else:
                            await self.highrise.send_whisper(user.id, "You are not currently subscribed.")
                    else:
                        await self.highrise.send_whisper(user.id, "You are not currently subscribed.")
                except Exception as e:
                    print(f"Error in -unsub: {e}")
    
            elif msg.startswith("-play "):
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 Music commands are LOCKED! Type -sub to unlock all commands. 🔓")
                    return
                song_query = message[6:].strip()
                if not song_query:
                    await self.highrise.send_whisper(user.id, "Please specify a song name. Example: -play Despacito")
                    return
    
                blocked_users = config.get("blocked_users", [])
                if user.username in blocked_users:
                    await self.highrise.send_whisper(user.id, "You are blocked from requesting songs.")
                    return
    
                # Slot Check
                paid_mode = config.get("paid_mode", False)
                if paid_mode and not self.is_privileged(user.username, config):
                    slots = self.get_slots(user.id, user.username)
                    if slots <= 0:
                        await self.highrise.send_whisper(user.id, "❌ You don't have enough slots! Tip 1 gold for 1 slot or check -myslots.")
                        return
                    # We consume the slot only if search succeeds
                
                # Search Search
                await self.highrise.send_whisper(user.id, f"🔍 Searching for '{song_query}'...")
                yt_title, yt_link, yt_duration = await self.search_youtube_song(song_query)
                
                # Search result handled
                if not yt_title:
                    await self.highrise.send_whisper(user.id, "❌ Sorry, I couldn't find that song. Please try another song or use more specific keywords.")
                    return
    
                if paid_mode and not self.is_privileged(user.username, config):
                    self.consume_slot(user.id, user.username)
    
                user_req_count = self.user_requests.get(user.username, 0)
                self.song_queue.append({
                    "user": user.username, 
                    "song": yt_title, 
                    "duration": yt_duration,
                    "link": yt_link
                })
                
                # The actual starting is now handled by auto_dj_loop process monitoring
                # We just need to trigger the stats if it was the first song
                if len(self.song_queue) == 1:
                    pass # Let auto_dj_loop detect it and start the stream
                self.user_requests[user.username] = user_req_count + 1
                queue_pos = len(self.song_queue) - 1
                pos_text = f"𝑷𝒐𝒔𝒊𝒕𝒊𝒐𝒏: {queue_pos}" if queue_pos > 0 else "𝙉𝙤𝙬 𝙋𝙡𝙖𝙮𝙞𝙣𝙜"
                play_msg = (f"<#B8860B>✨ 🎵 𝐀𝐃𝐃𝐄𝐃 𝐓𝐎 𝐐𝐔𝐄𝐔𝐄 🎵 ✨\n"
                            f"<#cdaa54>🎧 𝙎𝙤𝙣𝙜: {yt_title}\n"
                            f"<#e2ce9d>👤 𝙍𝙚𝙦𝙪𝙚𝙨𝙩: @{user.username} ({pos_text})")
                await self.highrise.chat(play_msg)
                print(f"[{time.strftime('%H:%M:%S')}] Success: @{user.username} added '{yt_title}' to queue.")
    
            elif msg == "-bal":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 Economy commands are LOCKED! Type -sub to unlock. 🔓")
                    return
                try:
                    with open("balances.json", "r") as f:
                        balances = json.load(f)
                except Exception:
                    balances = {}
                    
                user_id = str(user.id)
                user_data = balances.get(user_id, {"gold": 0})
                gold = user_data["gold"]
                await self.highrise.send_whisper(user.id, f"Your current balance is: {gold} gold.")
    
            elif msg == "-stats" or msg == "-uptime":
                uptime = time.time() - self.start_time
                hours, rem = divmod(uptime, 3600)
                minutes, seconds = divmod(rem, 60)
                uptime_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
                
                status_msg = (f"<#00FF00>🤖 ━━ 𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐔𝐒 ━━ 🤖\n\n"
                            f"🟢 𝙎𝙩𝙖𝙩𝙪𝙨: 24/7 Online Mode (Smooth)\n"
                            f"⏱️ 𝙐𝙥𝙩𝙞𝙢𝙚: {uptime_str}\n"
                            f"🎶 𝙌𝙪𝙚𝙪𝙚: {len(self.song_queue)} songs")
                await self.highrise.chat(status_msg)
    
            elif msg == "-np":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                    return
                song_msg = self.get_np_message()
                # Send the now playing info via the conversation ID based DM system
                await self.send_user_dm(user.id, song_msg)
                # Add a room confirmation if it's the first time or for clarity
                # (Uncomment the line below if a room confirmation is needed)
                # await self.highrise.chat(f"@{user.username} I've sent the Now Playing info to your DMs! 📩")
    
            elif msg == "-next":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                    return
                queue = getattr(self, "song_queue", [])
                if len(queue) > 1:
                    next_song = queue[1]
                    next_msg = (f"<#B8860B>✨ ━━ 🎧 𝐍𝐄𝐗𝐓 𝐒𝐎𝐍𝐆 ━━ ✨\n\n"
                                f"🎶 𝙎𝙤𝙣𝙜: {next_song['song']}\n"
                                f"👤 𝙍𝙚𝙦𝙪𝙚𝙨𝙩: @{next_song['user']}")
                else:
                    next_msg = "<#B8860B>✨ There is no song queued after the current one! ✨"
                await self.send_user_dm(user.id, next_msg)
    
            elif msg == "-q":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                    return
                
                # Detailed queue sent ONLY to DMs as requested
                detailed_q = self.get_formatted_queue(detailed=True)
                await self.send_user_dm(user.id, detailed_q)
            elif msg in ["-myq", "-mq"]:
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                    return
                user_songs = []
                if getattr(self, "song_queue", None) and len(self.song_queue) > 0:
                    for i, song_info in enumerate(self.song_queue):
                        if song_info.get('user') == user.username or f"Gift to {user.username}" in song_info.get('user', ''):
                            d = song_info.get('duration', random.randint(180, 240))
                            mins, secs = divmod(d, 60)
                            duration_str = f"{mins:02d}:{secs:02d}"
                            user_songs.append({
                                "name": song_info.get('song', 'Unknown Song'),
                                "pos": i + 1,
                                "duration": duration_str
                            })
                
                if user_songs:
                    count = len(user_songs)
                    my_q_msg = f"🎵 Your Song Requests ({count}):\n\n"
                    for idx, s in enumerate(user_songs):
                        my_q_msg += f"{idx+1}. 🎵 {s['name']}\n"
                        my_q_msg += f"   ⏱️ {s['duration']} | 📍 Queue Position: {s['pos']}\n\n"
                    
                    my_q_msg += "Use '-clear [number]' to remove your own songs. Example: -clear 2"
                else:
                    my_q_msg = "✨ You have no songs in the queue right now! ✨"
    
                await self.send_user_dm(user.id, my_q_msg)
    
            elif msg == "-like":
                if getattr(self, "song_queue", None) and len(self.song_queue) > 0:
                    song_name = self.song_queue[0]["song"]
                    if not getattr(self, "song_stats", None):
                        self.set_song_stats(song_name, time.time(), 210)
                    
                    self.song_stats["likes"].add(user.username)
                    self.song_stats["dislikes"].discard(user.username)
                    self._persist_like_dislike(song_name)
                    
                    await self.highrise.send_whisper(user.id, "You liked the current song! 👍")
                    if hasattr(self, "get_room_np_message"):
                        msg_np = self.get_room_np_message()
                        await self.highrise.chat(msg_np)
                else:
                    await self.highrise.send_whisper(user.id, "No song is playing right now!")
    
            elif msg == "-dislike":
                if getattr(self, "song_queue", None) and len(self.song_queue) > 0:
                    song_name = self.song_queue[0]["song"]
                    if not getattr(self, "song_stats", None):
                        self.set_song_stats(song_name, time.time(), 210)
                    
                    self.song_stats["dislikes"].add(user.username)
                    self.song_stats["likes"].discard(user.username)
                    self._persist_like_dislike(song_name)
                    
                    await self.highrise.send_whisper(user.id, "You disliked the current song! 👎")
                    if hasattr(self, "get_room_np_message"):
                        msg_np = self.get_room_np_message()
                        await self.highrise.chat(msg_np)
                else:
                    await self.highrise.send_whisper(user.id, "No song is playing right now!")
    
            
    
            elif msg == "-ql":
                q_len = max(0, len(getattr(self, "song_queue", [])) - 1)
                msg_text = f"The current song queue length is: {q_len} song(s)."
                await self.send_user_dm(user.id, msg_text)
                await self.highrise.send_whisper(user.id, "Queue length has been sent to your DMs! 📩")
    
            elif msg == "-status":
                user_req_count = self.user_requests.get(user.username, 0)
                await self.highrise.send_whisper(user.id, f"You have requested {user_req_count}/3 songs.")
    
            elif msg == "-skip":
                if not hasattr(self, "song_queue") or len(self.song_queue) == 0:
                    await self.highrise.send_whisper(user.id, "❌ No song is currently playing!")
                    return
    
                # Check if user is staff or the requester
                current_song = self.song_queue[0]
                is_staff = self.is_privileged(user.username, config)
                is_requester = current_song.get("user") == user.username
                
                if is_staff or is_requester:
                    # Pop the song before stopping the stream to avoid auto_dj_loop double popping
                    if len(self.song_queue) > 0:
                        skipped_song = self.song_queue.pop(0)
                        await self.highrise.chat(f"⏭️ @{user.username} skipped: {skipped_song.get('song', 'the song')}")
                    # Reset song_stats and flags so auto_dj_loop immediately picks next song
                    self.song_stats = None
                    self._stream_starting = False
                    self._autodj_searching = False

                    # Stop the current stream — auto_dj_loop will start the next one
                    await self.stop_stream()

                    if len(self.song_queue) > 0:
                        next_song = self.song_queue[0]
                        requester = next_song.get("user", "AutoDJ")
                        name = next_song.get("song", "Unknown")
                        await self.highrise.chat(f"▶️ Next up: {name} (Requested by @{requester})")
                    else:
                        await self.highrise.chat("🎵 Queue is now empty! Use -play [song name] to request the next song. 🎶")
                else:
                    await self.highrise.send_whisper(user.id, "🚫 You can only skip your own songs or if you are a Moderator.")
    
    
            elif msg.startswith("-clear "):
                try:
                    idx = int(msg[7:].strip()) - 1
                    if 0 <= idx < len(self.song_queue):
                        if self.song_queue[idx]["user"] == user.username or user.username in owners:
                            removed = self.song_queue.pop(idx)
                            await self.highrise.send_whisper(user.id, f"Removed '{removed['song']}' from the queue.")
                            if idx == 0:
                                if len(self.song_queue) > 0: self.set_song_stats(self.song_queue[0]["song"], time.time(), random.randint(180, 240))
                        else:
                            await self.highrise.send_whisper(user.id, "You cannot remove a song you didn't request!")
                    else:
                        await self.highrise.send_whisper(user.id, "Invalid song number in queue.")
                except ValueError:
                    await self.highrise.send_whisper(user.id, "Please provide a valid song number to clear. Example: -clear 2")
    
            elif msg == "-af":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 Favorites commands are LOCKED! Type -sub to unlock. 🔓")
                    return
                if getattr(self, "song_queue", None) and len(self.song_queue) > 0:
                    current_song = self.song_queue[0]["song"]
                    try:
                        with open("favorites.json", "r") as f:
                            favs = json.load(f)
                    except Exception:
                        favs = {}
                    
                    user_favs = favs.get(user.username, [])
                    if current_song in user_favs:
                        await self.highrise.send_whisper(user.id, "This song is already in your favorites!")
                    elif len(user_favs) >= 10:
                        await self.highrise.send_whisper(user.id, "You can only have up to 10 favorite songs.")
                    else:
                        user_favs.append(current_song)
                        favs[user.username] = user_favs
                        with open("favorites.json", "w") as f:
                            json.dump(favs, f, indent=4)
                        await self.highrise.send_whisper(user.id, f"Added to favorites! You have {len(user_favs)}/10.")
                else:
                    await self.highrise.send_whisper(user.id, "No song is playing right now!")
    
            elif msg.startswith("-df"):
                parts = msg.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Please specify the favorite number to remove: -df 1")
                else:
                    try:
                        idx = int(parts[1]) - 1
                        try:
                            with open("favorites.json", "r") as f:
                                favs = json.load(f)
                        except Exception:
                            favs = {}
                        user_favs = favs.get(user.username, [])
                        if 0 <= idx < len(user_favs):
                            removed = user_favs.pop(idx)
                            favs[user.username] = user_favs
                            with open("favorites.json", "w") as f:
                                json.dump(favs, f, indent=4)
                            await self.highrise.send_whisper(user.id, f"Removed '{removed}' from favorites.")
                        else:
                            await self.highrise.send_whisper(user.id, "Invalid favorite number.")
                    except ValueError:
                        await self.highrise.send_whisper(user.id, "Please specify a valid number.")
    
            elif msg == "-lf":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 Favorites list is LOCKED! Type -sub to unlock. 🔓")
                    return
                try:
                    with open("favorites.json", "r") as f:
                        favs = json.load(f)
                except Exception:
                    favs = {}
                user_favs = favs.get(user.username, [])
                
                if not user_favs:
                    lf_msg = "✨ You have no favorites yet! Use -af to add the current song. ✨"
                else:
                    # Load song stats for likes and listens
                    try:
                        with open(get_path("song_stats_persist.json"), "r") as f:
                            stats_data = json.load(f)
                    except Exception:
                        stats_data = {}
    
                    count = len(user_favs)
                    lf_msg = f"⭐ Your Favorite Songs ({count}/10):\n\n"
                    for i, song in enumerate(user_favs):
                        song_key = song.lower().strip()
                        song_stats = stats_data.get(song_key, {"likes": [], "listens": 0, "duration": 210})
                        likes = len(song_stats.get("likes", []))
                        listens = song_stats.get("listens", 0)
                        duration = song_stats.get("duration", 210)
                        mins, secs = divmod(duration, 60)
                        
                        lf_msg += f"{i+1}. 🎵 {song}\n"
                        lf_msg += f"   ⏱️{mins:02d}:{secs:02d} | ❤️ {likes} | 🎧 {listens}\n\n"
                    
                    lf_msg += "Use -df [number] to remove a favorite"
                    
                await self.send_user_dm(user.id, lf_msg)
    
            elif msg.startswith("-pf"):
                parts = message.split()
                if len(parts) < 2:
                    try:
                        with open("favorites.json", "r") as f:
                            favs = json.load(f)
                    except Exception:
                        favs = {}
                    user_favs = favs.get(user.username, [])
                    if not user_favs:
                        await self.highrise.send_whisper(user.id, "You have no favorites yet! Use -af to add the current song.")
                    else:
                        fav_msg = "Your favorites:\n"
                        for i, song in enumerate(user_favs):
                            fav_msg += f"{i+1}. {song}\n"
                        fav_msg += "Use -pf [number] to play one!"
                        await self.highrise.send_whisper(user.id, fav_msg)
                else:
                    try:
                        idx = int(parts[1]) - 1
                        try:
                            with open("favorites.json", "r") as f:
                                favs = json.load(f)
                        except Exception:
                            favs = {}
                        user_favs = favs.get(user.username, [])
                        if 0 <= idx < len(user_favs):
                            song_query = user_favs[idx]
    
                            # Slot Check
                            paid_mode = config.get("paid_mode", False)
                            if paid_mode and not self.is_privileged(user.username, config):
                                slots = self.get_slots(user.id, user.username)
                                if slots <= 0:
                                    await self.highrise.send_whisper(user.id, "❌ You don't have enough slots to play favorites! Tip 1 gold for 1 slot.")
                                    return
                            
                            # Search Search
                            await self.highrise.send_whisper(user.id, f"🔍 Searching for favorite: '{song_query}'...")
                            yt_title, yt_link, yt_duration = await self.search_youtube_song(song_query)
                            
                            # Search result handled
                            if not yt_title:
                                await self.highrise.send_whisper(user.id, f"❌ I couldn't find your favorite '{song_query}' right now.")
                                return
                                
                            if paid_mode and not self.is_privileged(user.username, config):
                                self.consume_slot(user.id, user.username)
                            
                            user_req_count = self.user_requests.get(user.username, 0)
                            self.song_queue.append({
                                "user": user.username, 
                                "song": yt_title, 
                                "duration": yt_duration,
                                "link": yt_link
                            })
                            if len(self.song_queue) == 1:
                                pass # Let auto_dj_loop detect it and start the stream
                            self.user_requests[user.username] = user_req_count + 1
                            queue_pos = len(self.song_queue) - 1
                            pos_text = f"𝑷𝒐𝒔𝒊𝒕𝒊𝒐𝒏: {queue_pos}" if queue_pos > 0 else "𝙉𝙤𝙬 𝙋𝙡𝙖𝙮𝙞𝙣𝙜"
                            play_msg = (f"<#B8860B>✨ ❤️ 𝐅𝐀𝐕𝐎𝐑𝐈𝐓𝐄 𝐀𝐃𝐃𝐄𝐃 ❤️ ✨\n"
                                        f"<#cdaa54>🎧 𝙎𝙤𝙣𝙜: {yt_title}\n"
                                        f"<#e2ce9d>👤 𝙍𝙚𝙦𝙪𝙚𝙨𝙩: @{user.username} ({pos_text})")
                            await self.highrise.chat(play_msg)
                        else:
                            await self.highrise.send_whisper(user.id, "Invalid favorite number.")
                    except ValueError:
                        await self.highrise.send_whisper(user.id, "Please specify a valid number.")
    
            elif msg.startswith("-gift "):
                # msg format: -gift @username song name
                parts = message.split(" ", 2)
                if len(parts) < 3 or not parts[1].startswith("@"):
                    await self.highrise.send_whisper(user.id, "Usage: -gift @username [song name]")
                else:
                    target_user = parts[1][1:] # Remove @
                    song_name = parts[2].strip()
    
                    # Slot Check
                    paid_mode = config.get("paid_mode", False)
                    if paid_mode and not self.is_privileged(user.username, config):
                        slots = self.get_slots(user.id, user.username)
                        if slots <= 0:
                            await self.highrise.send_whisper(user.id, "❌ You don't have enough slots to gift! Tip 1 gold for 1 slot.")
                            return
                    
                    # Search Search
                    await self.highrise.send_whisper(user.id, f"🔍 Searching for gift: '{song_name}'...")
                    yt_title, yt_link, yt_duration = await self.search_youtube_song(song_name)
                    
                    # Search result handled
                    if not yt_title:
                        await self.highrise.send_whisper(user.id, "❌ Sorry, I couldn't find that song to gift. Try a different name.")
                        return
    
                    if paid_mode and not self.is_privileged(user.username, config):
                        self.consume_slot(user.id, user.username)
                    
                    user_req_count = self.user_requests.get(user.username, 0)
                    self.song_queue.append({
                        "user": f"{user.username} (Gift to {target_user})", 
                        "song": yt_title,
                        "duration": yt_duration,
                        "link": yt_link
                    })
                    if len(self.song_queue) == 1:
                        pass # Let auto_dj_loop detect it and start the stream
                    self.user_requests[user.username] = user_req_count + 1
                    queue_pos = len(self.song_queue) - 1
                    pos_text = f"𝑷𝒐𝒔𝒊𝒕𝒊𝒐𝒏: {queue_pos}" if queue_pos > 0 else "𝙉𝙤𝙬 𝙋𝙡𝙖𝙮𝙞𝙣𝙜"
                    gift_msg = (f"<#B8860B>✨ 🎁 𝐒𝐎𝐍𝐆 𝐃𝐄𝐃𝐈𝐂𝐀𝐓𝐈𝐎𝐍 🎁 ✨\n"
                                f"<#cdaa54>🎧 𝙎𝙤𝙣𝙜: {yt_title}\n"
                                f"<#e2ce9d>👤 𝙁𝙧𝙤𝙢: @{user.username} ➡️ 𝙏𝙤: @{target_user}\n"
                                f"<#e2ce9d>🔢 {pos_text}")
                    await self.highrise.chat(gift_msg)
    
            elif msg.startswith("-give "):
                # format: -give (amount) @username
                parts = message.split()
                if len(parts) < 3:
                    await self.highrise.send_whisper(user.id, "Usage: -give [amount] @username")
                    return
                
                try:
                    if parts[1].startswith("@"):
                        target_str = parts[1]
                        amount = int(parts[2])
                    else:
                        amount = int(parts[1])
                        target_str = parts[2]
                except (ValueError, IndexError):
                    await self.highrise.send_whisper(user.id, "Usage: -give [amount] @username")
                    return
                
                if not target_str.startswith("@"):
                    await self.highrise.send_whisper(user.id, "Please mention a user with @.")
                    return
                if amount <= 0:
                    await self.highrise.send_whisper(user.id, "Amount must be greater than 0.")
                    return
    
                target_username = target_str[1:].lower()
                try:
                    with open(get_path("slots.json"), "r") as f:
                        slots = json.load(f)
                except Exception:
                    slots = {}
    
                sender_id = str(user.id)
                is_staff = self.is_privileged(user.username, config)
                
                if not is_staff:
                    if sender_id not in slots or slots[sender_id]["amount"] < amount:
                        await self.highrise.send_whisper(user.id, "❌ You don't have enough music slots to give.")
                        return
                    slots[sender_id]["amount"] -= amount
    
                # Find target
                target_id = None
                for uid, data in slots.items():
                    if data.get("username", "").lower() == target_username:
                        target_id = uid
                        break
                
                if not target_id:
                    try:
                        response = await self.highrise.get_room_users()
                        room_users = response.content if hasattr(response, "content") else response[0] if isinstance(response, tuple) else response
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_username:
                                target_id = str(r_user.id)
                                if target_id not in slots:
                                    slots[target_id] = {"username": r_user.username, "amount": 0}
                                break
                    except: pass
    
                if not target_id:
                    await self.highrise.send_whisper(user.id, f"Could not find user @{target_username} in room or database.")
                    return
                
                slots[target_id]["amount"] += amount
                with open(get_path("slots.json"), "w") as f:
                    json.dump(slots, f, indent=4)
                
                await self.highrise.chat(f"🎁 @{user.username} gave {amount} music slots to @{target_username}!")
    
            elif msg == "-prices":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                    return
                temp_price = config.get("vip_temp_price", 500)
                perm_price = config.get("vip_perm_price", 2000)
                paid_mode = config.get("paid_mode", False)
                status = "🟢 ON" if paid_mode else "🔴 OFF"
                
                prices_msg = (f"<#B8860B>✨ ━━ 💎 𝐕𝐈𝐏 𝐏𝐑𝐈𝐂𝐄𝐒 ━━ ✨\n\n"
                              f"<#cdaa54>⏳ Temporary VIP: {temp_price} Gold\n"
                              f"<#cdaa54>♾️ Permanent VIP: {perm_price} Gold\n\n"
                              f"<#e2ce9d>💳 Paid Mode Status: {status}\n"
                              f"<#B8860B>Use -buyvip temp OR -buyvip perm to purchase!")
                
                await self.send_user_dm(user.id, prices_msg)
    
            elif msg.startswith("-buyvip "):
                parts = message.split()
                if len(parts) < 2 or parts[1].lower() not in ["temp", "perm"]:
                    await self.highrise.send_whisper(user.id, "Usage: -buyvip temp OR -buyvip perm")
                    return
                    
                vip_type = parts[1].lower()
                temp_price = config.get("vip_temp_price", 500)
                perm_price = config.get("vip_perm_price", 2000)
                cost = temp_price if vip_type == "temp" else perm_price
                
                try:
                    with open("balances.json", "r") as f:
                        balances = json.load(f)
                except Exception:
                    balances = {}
                    
                user_id_str = str(user.id)
                user_bal = balances.get(user_id_str, {}).get("gold", 0)
                
                if user_bal < cost:
                    await self.highrise.send_whisper(user.id, f"You don't have enough gold! You need {cost} gold for {vip_type} VIP.")
                    return
                    
                try:
                    with open(get_path("vip_users.json"), "r") as f:
                        vip_users = json.load(f)
                except Exception:
                    vip_users = {}
                    
                current_vip = vip_users.get(user.username, {}).get("type")
                if current_vip == "perm":
                    await self.highrise.send_whisper(user.id, "You are already a Permanent VIP!")
                    return
                if current_vip == "temp" and vip_type == "temp":
                    await self.highrise.send_whisper(user.id, "You already have Temporary VIP. You can upgrade to perm using -buyvip perm.")
                    return
                    
                # Deduct balance
                balances[user_id_str]["gold"] -= cost
                with open("balances.json", "w") as f:
                    json.dump(balances, f, indent=4)
                    
                # Add to VIP
                vip_users[user.username] = {"type": vip_type, "timestamp": time.time()}
                with open(get_path("vip_users.json"), "w") as f:
                    json.dump(vip_users, f, indent=4)
                    
                vip_name = "Temporary" if vip_type == "temp" else "Permanent"
                await self.highrise.chat(f"💎 {user.username} successfully bought {vip_name} VIP for {cost} gold!")
    
            elif msg.startswith("-mod "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -mod @username")
                    return
                target = parts[1].replace("@", "")
                if target not in config.get("moderators", []):
                    config.setdefault("moderators", []).append(target)
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    await self.highrise.chat(f"🛡️ @{target} has been made a Moderator by @{user.username}!")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is already a Moderator.")
    
            elif msg.startswith("-remmod "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -remmod @username")
                    return
                target = parts[1].replace("@", "")
                if target in config.get("moderators", []):
                    config["moderators"].remove(target)
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    await self.highrise.chat(f"❌ @{target} is no longer a Moderator.")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is not a Moderator.")
    
            elif msg.startswith("-owner "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -owner @username")
                    return
                target = parts[1].replace("@", "")
                if target not in config.get("owners", []):
                    config.setdefault("owners", []).append(target)
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    await self.highrise.chat(f"👑 @{target} has been made an Owner by @{user.username}!")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is already an Owner.")
    
            elif msg.startswith("-remowner "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -remowner @username")
                    return
                target = parts[1].replace("@", "")
                if target in config.get("owners", []):
                    config["owners"].remove(target)
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    await self.highrise.chat(f"❌ @{target} is no longer an Owner.")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is not an Owner.")
    
            elif msg.startswith("-vip "):
                mods = config.get("moderators", [])
                if user.username not in owners + mods:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command (Moderator/Owner only).")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -vip @username")
                    return
                target = parts[1].replace("@", "")
                try:
                    with open(get_path("vip_users.json"), "r") as f:
                        vip_users = json.load(f)
                except Exception:
                    vip_users = {}
                if target not in vip_users:
                    vip_users[target] = {"type": "perm", "timestamp": time.time()}
                    with open(get_path("vip_users.json"), "w") as f:
                        json.dump(vip_users, f, indent=4)
                    await self.highrise.chat(f"💎 @{target} has been given VIP access by @{user.username}!")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is already a VIP.")
    
            elif msg.startswith("-remvip "):
                mods = config.get("moderators", [])
                if user.username not in owners + mods:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command (Moderator/Owner only).")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -remvip @username")
                    return
                target = parts[1].replace("@", "")
                try:
                    with open(get_path("vip_users.json"), "r") as f:
                        vip_users = json.load(f)
                except Exception:
                    vip_users = {}
                if target in vip_users:
                    del vip_users[target]
                    with open(get_path("vip_users.json"), "w") as f:
                        json.dump(vip_users, f, indent=4)
                    await self.highrise.chat(f"❌ @{target} is no longer a VIP.")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is not a VIP.")
    
            elif msg.startswith("-block "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -block @username")
                    return
                target = parts[1].replace("@", "")
                blocked = config.get("blocked_users", [])
                if target not in blocked:
                    blocked.append(target)
                    config["blocked_users"] = blocked
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    await self.highrise.chat(f"🚫 @{target} has been BLOCKED. They can no longer use any commands.")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is already blocked.")
    
            elif msg.startswith("-unblock "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -unblock @username")
                    return
                target = parts[1].replace("@", "")
                blocked = config.get("blocked_users", [])
                if target in blocked:
                    blocked.remove(target)
                    config["blocked_users"] = blocked
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    await self.highrise.chat(f"✅ @{target} has been UNBLOCKED.")
                else:
                    await self.highrise.send_whisper(user.id, f"@{target} is not blocked.")
                    
            elif msg == "-rolelist":
                mods = config.get("moderators", [])
                if user.username.lower() not in [o.lower() for o in owners] and user.username.lower() not in [m.lower() for m in mods]:
                    await self.highrise.send_whisper(user.id, "ℹ️ You don't have permission to use this command.")
                    return

                role_msg = "<#B8860B>✨ ━━ 🛡️ 𝐑𝐎𝐋𝐄 𝐋𝐈𝐒𝐓 ━━ ✨\n\n"
                role_msg += "<#cdaa54>👑 𝐎𝐰𝐧𝐞𝐫𝐬:\n"
                unique_owners = list(set(config.get("owners", [])))
                if not unique_owners:
                    role_msg += "<#e2ce9d>  - None\n"
                else:
                    for o in unique_owners:
                        role_msg += f"<#e2ce9d>  - @{o}\n"
                
                role_msg += "\n<#cdaa54>🛡️ 𝐌𝐨𝐝𝐞𝐫𝐚𝐭𝐨𝐫𝐬:\n"
                unique_mods = list(set(config.get("moderators", [])))
                if not unique_mods:
                    role_msg += "<#e2ce9d>  - None\n"
                else:
                    for m in unique_mods:
                        role_msg += f"<#e2ce9d>  - @{m}\n"
                
                await self.send_room_chunks(role_msg)
                
            elif msg == "-viplist":
                try:
                    with open(get_path("vip_users.json"), "r") as f:
                        vip_users = json.load(f)
                except Exception:
                    vip_users = {}
                
                mods = config.get("moderators", [])
                if user.username.lower() not in [o.lower() for o in owners] and user.username.lower() not in [m.lower() for m in mods] and user.username.lower() not in [v.lower() for v in vip_users]:
                    await self.highrise.send_whisper(user.id, "You need VIP, Moderator or Owner role to view the VIP list.")
                    return
                vip_msg = "<#B8860B>✨ ━━ 💎 𝐕𝐈𝐏 𝐌𝐄𝐌𝐁𝐄𝐑𝐒 ━━ ✨\n\n"
                if not vip_users:
                    vip_msg += "<#e2ce9d>No VIPs currently."
                else:
                    for v, data in vip_users.items():
                        v_type = data.get("type", "perm").capitalize()
                        vip_msg += f"<#cdaa54>💎 @{v} <#e2ce9d>({v_type})\n"
                await self.send_room_chunks(vip_msg)
                    
            elif msg.startswith("-vipcost "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command.")
                    return
                parts = message.split()
                if len(parts) < 3 or parts[1].lower() not in ["temp", "perm"]:
                    await self.highrise.send_whisper(user.id, "Usage: -vipcost temp/perm [amount]")
                    return
                v_type = parts[1].lower()
                try:
                    amount = int(parts[2])
                except ValueError:
                    await self.highrise.send_whisper(user.id, "Amount must be a valid number.")
                    return
                    
                if v_type == "temp":
                    config["vip_temp_price"] = amount
                else:
                    config["vip_perm_price"] = amount
                    
                with open(get_path("config.json"), "w") as f:
                    json.dump(config, f, indent=4)
                    
                await self.highrise.chat(f"✅ VIP {v_type.capitalize()} price updated to {amount} gold!")
    
            elif msg.startswith("-invite"):
                print(f"Triggered -invite by {user.username}")
                mods = config.get("moderators", [])
                if user.username not in owners + mods:
                    await self.highrise.send_whisper(user.id, "You do not have permission to use this command (Moderator/Owner only).")
                    print(f"Permission denied for {user.username}. Owners/mods: {owners + mods}")
                    return
    
                room_id = config.get("room_id", "Unknown")
                
                jokes = [
                    "Why did the music teacher need a ladder? To reach the high notes! 🥁",
                    "Why do music bots make terrible liars? Because you can always read their pitch! 🤖🎶",
                    "What does a robot DJ eat for a snack? Micro-chips and salsa! 🍟🎵",
                    "Why did the VIP go to jail? Because he got caught in a jam! 🍓🚓",
                    "What happens when you play Beethoven backwards? He decomposes. 🎹💀",
                    "How do you make a bandstand? Take away their chairs! 🪑🎺",
                    "What is a ghost's favorite kind of music? Rhythm and BOO-s! 👻🎧",
                    "What kind of music do balloons hate? Pop music! 🎈💥"
                ]
    
                music_msgs = [
                    "Hey! We're vibing to some great music right now and you're invited! 🎶",
                    "The DJ is dropping the hottest tracks in the room! Come request your favorite song. 🎧",
                    "Need a break? Join our chill music session and relax with us! 🎵",
                    "Non-stop bangers playing right now! You definitely don't want to miss this. 🎸"
                ]
    
                vip_msgs = [
                    "Experience our custom song queue and get exclusive VIP perks! 💎",
                    "Unlock premium VIP access, skip the queue, and show off your golden status! ✨",
                    "Special roles and commands are ready for our VIP members. Don't miss out! 👑",
                    "Get VIP now to permanently secure your exclusive room benefits! 💰"
                ]
    
                await self.highrise.chat("Sending out invites to everyone... This might take a moment! 📡")
                
                try:
                    response = await self.highrise.get_conversations()
                    print(f"get_conversations response type: {type(response)}")
                    
                    conversations = []
                    if hasattr(response, "conversations"):
                        conversations = response.conversations
                    elif isinstance(response, tuple) and len(response) > 0:
                        conversations = response[0]
                    else:
                        print(f"Could not parse conversations from response: {response}")
                        await self.highrise.chat("Failed to load conversations list from the server.")
                        return
    
                    count = 0
                    for conv in conversations:
                        joke = random.choice(jokes)
                        m_msg = random.choice(music_msgs)
                        v_msg = random.choice(vip_msgs)
                        
                        colors = ["#FF69B4", "#00FFFF", "#32CD32", "#FF4500", "#9370DB", "#FFD700", "#00FA9A", "#1E90FF", "#FF6347", "#DA70D6", "#FF1493", "#7FFF00", "#00BFFF", "#FF8C00", "#FFA500"]
                        c1 = random.choice(colors)
                        c2 = random.choice(colors)
                        c3 = random.choice(colors)
                        c4 = random.choice(colors)
                        
                        invite_msg = (f"<{c1}>✨ ━━ 🎵 𝐉𝐎𝐈𝐍 𝐓𝐇𝐄 𝐏𝐀𝐑𝐓𝐘! 🎵 ━━ ✨\n\n"
                                      f"<{c2}>{m_msg}\n"
                                      f"<{c3}>{v_msg}\n\n"
                                      f"<{c1}>Joke of the moment:\n"
                                      f"<{c4}>{joke}\n\n"
                                      f"<{c2}>Click the Enter Room button and join the party! 🎉")
                        try:
                            # 1. Send the rich text message first
                            await self.highrise.send_message(conv.id, invite_msg)
                            await asyncio.sleep(0.5)
                            
                            # 2. Send the actual room invite button
                            if room_id and room_id != "Unknown":
                                await self.highrise.send_message(conv.id, "Click below to join!", message_type="invite", room_id=room_id)
                                await asyncio.sleep(0.5)
                            
                            count += 1
                        except Exception as e:
                            print(f"Failed to send to {conv.id}: {e}")
                            
                    await self.highrise.chat(f"✅ Successfully sent {count} room invites!")
                except Exception as e:
                    print(f"Exception in get_conversations: {e}")
                    await self.highrise.chat(f"Error executing invite command: {e}")
    
            elif msg == "-myslots":
                slots = self.get_slots(user.id, user.username)
                await self.highrise.send_whisper(user.id, f"🎟️ You currently have {slots} music slots.")
    
            elif msg.startswith("-buyslot "):
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -buyslot [amount]")
                    return
                
                try:
                    amount = int(parts[1])
                except ValueError:
                    await self.highrise.send_whisper(user.id, "Please provide a valid number for slots.")
                    return
                
                if amount <= 0:
                    await self.highrise.send_whisper(user.id, "Amount must be at least 1.")
                    return
    
                try:
                    with open("balances.json", "r") as f:
                        balances = json.load(f)
                except Exception:
                    balances = {}
                
                user_id = str(user.id)
                if user_id not in balances or balances[user_id].get("gold", 0) < amount:
                    await self.highrise.send_whisper(user.id, f"❌ You don't have enough gold! Tip the bot first. (Need {amount} gold)")
                    return
    
                # Deduct gold
                balances[user_id]["gold"] -= amount
                with open("balances.json", "w") as f:
                    json.dump(balances, f, indent=4)
    
                # Award slots
                try:
                    with open(get_path("slots.json"), "r") as f:
                        slots = json.load(f)
                except Exception:
                    slots = {}
                
                if user_id not in slots:
                    slots[user_id] = {"username": user.username, "amount": 0}
                
                slots[user_id]["amount"] += amount
                with open(get_path("slots.json"), "w") as f:
                    json.dump(slots, f, indent=4)
                
                await self.highrise.chat(f"🎰 @{user.username} bought {amount} music slots! Enjoy the music! 🎵")
    
            elif msg.startswith("-giveslot"):
                await self.highrise.send_whisper(user.id, "Please use -give [amount] @username to transfer slots.")
    
            elif msg.startswith("-paidmode "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can toggle Paid Mode.")
                    return
                
                mode = msg.split()[1]
                if mode == "on":
                    config["paid_mode"] = True
                    await self.highrise.chat("📢 Paid Mode is now ON. Slots are required to play music (Except VIP/Staff)!")
                elif mode == "off":
                    config["paid_mode"] = False
                    await self.highrise.chat("📢 Paid Mode is now OFF. Everyone can play music for free!")
                else:
                    await self.highrise.send_whisper(user.id, "Usage: -paidmode on/off")
                
                with open(get_path("config.json"), "w") as f:
                    json.dump(config, f, indent=4)
    
            elif msg.startswith("-autodj "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can toggle AutoDJ.")
                    return
                
                mode_parts = msg.split()
                if len(mode_parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -autodj on/off")
                    return
                
                mode = mode_parts[1]
                if mode == "on":
                    self.autodj = True
                    config["autodj"] = True
                    await self.highrise.chat("📢 English AutoDJ is now ON. Bot will play songs automatically when queue is empty!")
                elif mode == "off":
                    self.autodj = False
                    config["autodj"] = False
                    await self.highrise.chat("📢 English AutoDJ is now OFF.")
                else:
                    await self.highrise.send_whisper(user.id, "Usage: -autodj on/off")
                
                with open(get_path("config.json"), "w") as f:
                    json.dump(config, f, indent=4)
    
            # Line 1110 approx
            elif msg == "-help":
                if not self.is_subscribed(user.username) and not self.is_privileged(user.username, config):
                    await self.highrise.send_whisper(user.id, "🚫 Help menu is LOCKED! Type -sub to unlock. 🔓")
                    return
                welcome_msg = (
                    f"🌟 <#B8860B>𝐖𝐞𝐥𝐜𝐨𝐦𝐞 𝐭𝐨 @{user.username} 𝐀𝐧𝐧𝐨𝐮𝐧𝐜𝐞𝐦𝐞𝐧𝐭! 🌟\n\n"
                    f"🎉 <#cdaa54>𝐓𝐡𝐚𝐧𝐤 𝐲𝐨𝐮 𝐟𝐨𝐫 𝐬𝐮𝐛𝐬𝐜𝐫𝐢𝐛𝐢𝐧𝐠! 🎉\n\n"
                    f"✨ <#e2ce9d>Stay tuned for automatic updates on our exciting events, giveaways, and exclusive invitations. Be the first to know whenever we're hosting something amazing!\n\n"
                    f"💬 <#cdaa54>𝐃𝐢𝐬𝐜𝐥𝐚𝐢𝐦𝐞𝐫: Users who block the bot will be automatically unsubscribed from our notification system.\n\n"
                    f"💖 <#e2ce9d>Happy connecting and exploring with us! 🎉 𝐘𝐨𝐮'𝐯𝐞 𝐛𝐞𝐞𝐧 𝐚𝐮𝐭𝐨𝐦𝐚𝐭𝐢𝐜𝐚𝐥𝐥𝐲 𝐬𝐮𝐛𝐬𝐜𝐫𝐢𝐛𝐞𝐝!\n"
                    f"🎧 <#cdaa54>Now you can access the queue, currently playing songs, your favorites, and more!\n"
                    f"📍 To view the queue: -q\n"
                    f"📍 To view now playing: -np\n"
                    f"📍 To manage favorites: -af, -lf, -df\n"
                    f"📍 To unsubscribe: -unsub\n"
                    f"📍 To toggle AutoDJ: -autodj"
                )
                help_msg1 = (
                    f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (1/4) ━━ ✨\n\n"
                    f"<#cdaa54>🎵 𝐌𝐮𝐬𝐢𝐜 𝐐𝐮𝐞𝐮𝐞:\n"
                    f"<#e2ce9d>-play [song] - Request a song\n"
                    f"-np - Now playing info\n"
                    f"-next - View next song\n"
                    f"-q - View entire queue\n"
                    f"-mq - View your queue\n"
                    f"-ql - Show queue length\n"
                    f"-skip - Skip current song\n"
                    f"-clear [num] - Clear your song\n"
                    f"-autodj [on/off] - Toggle English AutoDJ"
                )
                help_msg2 = (
                    f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (2/4) ━━ ✨\n\n"
                    f"<#cdaa54>🎟️ 𝐒𝐥𝐨𝐭 𝐒𝐲𝐬𝐭𝐞𝐦:\n"
                    f"<#e2ce9d>-myslots - Check your slots\n"
                    f"-buyslot [amt] - Buy slots with gold\n"
                    f"-give [amt] @user - Transfer slots\n"
                    f"💡 Tip 1g for 1 Slot! (Use -buyslot after tipping)\n\n"
                    f"<#cdaa54>❤️ 𝐅𝐚𝐯𝐨𝐫𝐢𝐭𝐞𝐬 & 𝐋𝐢𝐤𝐞𝐬:\n"
                    f"<#e2ce9d>-af / -df - Add/Delete from favorites\n"
                    f"-lf / -pf [no] - List/Play favorites\n"
                    f"-gift @user [song] - Gift a song\n"
                    f"-like / -dislike - Rate the playing song"
                )
                help_msg3 = (
                    f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (3/4) ━━ ✨\n\n"
                    f"<#cdaa54>💎 𝐕𝐈𝐏 & 𝐄𝐜𝐨𝐧𝐨𝐦𝐲:\n"
                    f"<#e2ce9d>-bal - Check your gold balance\n"
                    f"-prices - View VIP costs\n"
                    f"-buyvip [temp/perm] - Buy VIP rank"
                )
                help_msg4 = (
                    f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (4/4) ━━ ✨\n\n"
                    f"<#cdaa54>🛡️ 𝐀𝐝𝐦𝐢𝐧𝐢𝐬𝐭𝐫𝐚𝐭𝐢𝐨𝐧:\n"
                    f"<#e2ce9d>-paidmode on/off - Toggle slot system\n"
                    f"-vip / -remvip @user - Set manual VIP\n"
                    f"-viplist / -rolelist - View members list\n"
                    f"-mod / -remmod @user - Manage Moderators\n"
                    f"-owner / -remowner @user - Manage Owners\n"
                    f"-invite - Send mass PM invites\n"
                    f"-getfit @user - Copy user fit\n\n"
    
                    f"<#cdaa54>🤖 𝐆𝐞𝐧𝐞𝐫𝐚𝐥:\n"
                    f"<#e2ce9d>-ping - Test bot latency\n"
                    f"-unsub - Stop receiving DMs\n"
                    f"-setpos - Set DJ spot (Owners)"
                )
                
                await self.send_user_dm(user.id, welcome_msg)
                await asyncio.sleep(0.5)
                await self.send_user_dm(user.id, help_msg1)
                await asyncio.sleep(0.5)
                await self.send_user_dm(user.id, help_msg2)
                await asyncio.sleep(0.5)
                await self.send_user_dm(user.id, help_msg3)
                await asyncio.sleep(0.5)
                await self.send_user_dm(user.id, help_msg4)
                if config.get("paid_mode"):
                    await asyncio.sleep(0.5)
                    await self.highrise.send_whisper(user.id, "💡 Note: Paid Mode is ON. You need slots to play music. Tip 1g for 1 slot!")
    
            # Since we moved -unsub logic higher up, let's remove the old empty -unsub block if it exists
            # (It was at line 1191 in my previous view)
            elif msg == "-setpos":
                if user.username not in owners:
                    await self.highrise.chat("You do not have permission to use that command.")
                    return
    
                response = await self.highrise.get_room_users()
                
                # Support both new SDK (response.content) and older SDK formats
                if hasattr(response, "content"):
                    room_users = response.content
                elif isinstance(response, tuple) and len(response) == 2:
                    room_users = response[0]
                else:
                    room_users = response
    
                if not isinstance(room_users, list):
                    print(f"Failed to parse room users object: {type(response)}")
                    await self.highrise.chat("An error occurred while finding your location.")
                    return
    
                for room_user, pos in room_users:
                    if room_user.id == user.id:
                        if isinstance(pos, Position):
                            # Save position to config
                            config["bot_position"] = {
                                "x": pos.x,
                                "y": pos.y,
                                "z": pos.z,
                                "facing": pos.facing
                            }
                            with open(get_path("config.json"), "w") as f:
                                json.dump(config, f, indent=4)
                            
                            await self.highrise.walk_to(pos)
                            await self.highrise.chat("My position has been updated!")
                        else:
                            await self.highrise.chat("I cannot move to an AnchorPosition.")
                        break
    
            elif msg.startswith("-getfit "):
                # Ultra-Advanced Public Command with Multi-Phase Extraction
                try:
                    # Clean the target name rigorously
                    target_name = message[8:].strip().split()[0].replace("@", "")
                    if not target_name:
                        await self.highrise.chat("Usage: -getfit @username")
                        return
                    
                    await self.highrise.chat(f"🔍 Initializing deep scan for @{target_name}... ✨")
                    try:
                        await self.highrise.send_emote("emote-fashionista", self.bot_id)
                    except: pass
    
                    target_id = None
                    raw_outfit_data = None
                    
                    # Enhanced normalization engine
                    def normalize_outfit(data):
                        if data is None: return None
                        normalized = []
                        
                        # 1. Unpack Response Objects
                        if not isinstance(data, list):
                            # Try to find a list attribute like 'outfit' or 'items'
                            for attr in ["outfit", "content", "items", "user_outfit"]:
                                val = getattr(data, attr, None)
                                if isinstance(val, list):
                                    data = val
                                    break
                                # Deep check for .content.outfit
                                if attr == "content" and hasattr(val, "outfit"):
                                    data = val.outfit
                                    break
                        
                        # If still not a list, try to treat it as an object with dict keys
                        if not isinstance(data, list):
                            return None
    
                        if len(data) == 0:
                            return [] 
    
                        # 2. Convert to Item Models
                        for i in data:
                            try:
                                if hasattr(i, "id") and hasattr(i, "type"):
                                    # Already an Item-like object
                                    i.active_palette = getattr(i, "active_palette", getattr(i, "palette", 0))
                                    normalized.append(i)
                                elif isinstance(i, dict):
                                    # Dictionary from Web API
                                    item_id = i.get("id") or i.get("item_id") or i.get("itemShortId")
                                    if not item_id: continue
                                    palette = i.get("active_palette", i.get("palette", i.get("color", 0)))
                                    normalized.append(Item(
                                        type=str(i.get("type", "clothing")),
                                        amount=1,
                                        id=str(item_id),
                                        account_bound=bool(i.get("account_bound", False)),
                                        active_palette=int(palette)
                                    ))
                            except Exception as e:
                                print(f"[DEBUG] Extraction skip: {e}")
                        
                        return normalized if normalized else None
    
                    # --- PHASE 1: SEARCH & DISCOVERY ---
                    # Detect if the input is already a User ID (usually 24-32 alphanumeric chars)
                    is_id = len(target_name) >= 24 and any(c.isdigit() for c in target_name)
                    
                    # Check room users first (if it's a name)
                    if not is_id:
                        try:
                            room_users_raw = await self.highrise.get_room_users()
                            room_users = room_users_raw.content if hasattr(room_users_raw, "content") else (room_users_raw[0] if isinstance(room_users_raw, tuple) else room_users_raw)
                            for u, _ in room_users:
                                if u.username.lower() == target_name.lower():
                                    target_id = u.id
                                    target_name = u.username
                                    break
                        except: pass
    
                    # --- PHASE 2: GLOBAL EXTRACTION CHAIN ---
                    temp_outfit = None
                    if target_id:
                        # Found in room, grab outfit
                        try:
                            raw_outfit_data = await self.highrise.get_user_outfit(target_id)
                            temp_outfit = normalize_outfit(raw_outfit_data)
                        except: pass
                    
                    # If not found or extraction from room failed/empty
                    if not temp_outfit or len(temp_outfit) == 0:
                        try:
                            async with aiohttp.ClientSession() as session:
                                uid = None
                                fallback_outfit_data = []
                                
                                if is_id:
                                    uid = target_name
                                else:
                                    # Input is a name - search globally with Web API
                                    search_url = f"https://webapi.highrise.game/users/{target_name}"
                                    async with session.get(search_url) as resp:
                                        if resp.status == 200:
                                            prof_data = await resp.json()
                                            user_data = prof_data.get("user", {})
                                            uid = user_data.get("user_id")
                                            target_name = user_data.get("username", target_name)
                                            fallback_outfit_data = user_data.get("outfit", [])
    
                                if uid:
                                    # GLOBAL SCAN: Try to pull from Gateway first for most accurate live data
                                    try:
                                        raw_outfit_data = await self.highrise.get_user_outfit(uid)
                                        temp_outfit = normalize_outfit(raw_outfit_data)
                                    except: pass
    
                                    # WEB PROFILE SCAN: Last resort if Gateway fails
                                    if not temp_outfit or len(temp_outfit) == 0:
                                        await self.highrise.chat(f"⏳ Global search dry... Trying direct Cloud Link for @{target_name} ☁️")
                                        if fallback_outfit_data:
                                            temp_outfit = normalize_outfit(fallback_outfit_data)
                                        else:
                                            # (Fallback if we started with an ID and skipped the name search)
                                            async with session.get(f"https://webapi.highrise.game/users/{uid}") as prof_resp:
                                                if prof_resp.status == 200:
                                                    prof_data = await prof_resp.json()
                                                    raw_outfit_data = prof_data.get("user", {}).get("outfit", [])
                                                    temp_outfit = normalize_outfit(raw_outfit_data)
                        except: pass
    
                    target_outfit = temp_outfit
    
                    if target_outfit is None:
                        await self.highrise.chat(f"❌ Extraction failed for @{target_name}. This error usually means the user is private or their profile data is restricted.")
                        return
                    
                    if len(target_outfit) == 0:
                        await self.highrise.chat(f"👻 @{target_name} is currently invisible or wearing nothing compatible!")
                        return
    
                    # 3. Apply all items without skipping
                    final_outfit = []
                    skipped = 0
                    for item in target_outfit:
                        final_outfit.append(item)
    
                    # 5. Visual Transformation & Execution
                    await asyncio.sleep(1)
                    await self.highrise.chat(f"👕 Style extracted! Applying @{target_name}'s look...")
                    try:
                        await self.highrise.send_emote("emote-teleporting", self.bot_id)
                    except: pass
                    await asyncio.sleep(1.2)
                    
                    try:
                        await self.highrise.set_outfit(final_outfit)
                    except Exception as e:
                        # Fallback: If full outfit fails (common with restricted body/face combos), try clothing only
                        print(f"[DEBUG] Full outfit set failed: {e} | Trying Safe Mode Fallback...")
                        safe_outfit = [i for i in final_outfit if i.type == "clothing"]
                        await self.highrise.set_outfit(safe_outfit)
                    
                    # Persistent Save
                    try:
                        save_data = [{"type": i.type, "id": i.id, "amount": 1, "account_bound": i.account_bound, "palette": i.active_palette} for i in final_outfit]
                        with open(get_path("config.json"), "r") as f:
                            config = json.load(f)
                        config["bot_outfit"] = save_data
                        with open(get_path("config.json"), "w") as f:
                            json.dump(config, f, indent=4)
                    except: pass
    
                    if skipped > 0:
                        await self.highrise.chat(f"✅ Look applied! (Extraction: {len(final_outfit)} items matched, {skipped} items skipped due to ownership).")
                    else:
                        await self.highrise.chat(f"🏁 ✨ 𝐏𝐄𝐑𝐅𝐄𝐂𝐓 𝐄𝐗𝐓�𝐀𝐂𝐓𝐈��! I'm now a 1:1 copy of @{target_name}! ✨")
                except Exception as e:
                    await self.highrise.chat(f"❌ Error during extraction: {str(e)[:100]}")
    
            elif msg.startswith("-savefit "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -savefit [slot] [name]")
                    return
                
                slot = parts[1]
                # Name is optional, defaults to "Fit [Slot]"
                name = " ".join(parts[2:]) if len(parts) > 2 else f"Fit {slot}"
                    
                current_outfit = config.get("bot_outfit")
                if not current_outfit:
                    await self.highrise.send_whisper(user.id, "I have no custom outfit to save right now!")
                    return
                    
                try:
                    slots = {}
                    if os.path.exists("outfit_slots.json"):
                        with open("outfit_slots.json", "r") as f:
                            slots = json.load(f)
                    
                    # Store as a dict to support names
                    slots[slot] = {
                        "name": name,
                        "outfit": current_outfit
                    }
                    
                    with open("outfit_slots.json", "w") as f:
                        json.dump(slots, f, indent=4)
                    
                    await self.highrise.chat(f"✅ Outfit '{name}' saved to Slot {slot}! Use -loadfit {slot} to wear it.")
                except Exception as e:
                    await self.highrise.send_whisper(user.id, f"Failed to save fit: {e}")
    
            elif msg.startswith("-loadfit "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -loadfit [slot/name]")
                    return
                target = parts[1].lower()
                
                try:
                    if not os.path.exists("outfit_slots.json"):
                        await self.highrise.send_whisper(user.id, "No saved outfits found.")
                        return
                        
                    with open("outfit_slots.json", "r") as f:
                        slots = json.load(f)
                    
                    slot_data = None
                    # Try load by slot number
                    if target in slots:
                        slot_data = slots[target]
                    else:
                        # Try load by name
                        for slot, data in slots.items():
                            if isinstance(data, dict) and data.get("name", "").lower() == target:
                                slot_data = data
                                break
                    
                    if not slot_data:
                        await self.highrise.send_whisper(user.id, f"Could not find outfit '{target}'.")
                        return
                    
                    # Support both old and new data structure
                    saved_fit = slot_data["outfit"] if isinstance(slot_data, dict) else slot_data
                    name = slot_data.get("name", "Outfit") if isinstance(slot_data, dict) else "Saved Fit"
    
                    new_outfit = [Item(type=i["type"], amount=i["amount"], id=i["id"], 
                                       account_bound=i["account_bound"], active_palette=i.get("active_palette", i.get("palette", 0))) 
                                  for i in saved_fit]
                    
                    # Visual effect
                    await self.highrise.send_emote("emote-teleporting", self.bot_id)
                    await asyncio.sleep(1.5)
                    
                    await self.highrise.set_outfit(new_outfit)
                    
                    # Update persistent config as well
                    config["bot_outfit"] = saved_fit
                    with open(get_path("config.json"), "w") as f:
                        json.dump(config, f, indent=4)
                    
                    await self.highrise.chat(f"✨ '{name}' Loaded successfully! ✨")
                except Exception as e:
                    await self.highrise.send_whisper(user.id, f"Failed to load fit: {e}")
    
            elif msg == "-fitlist" or msg == "-fits":
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                try:
                    if not os.path.exists("outfit_slots.json"):
                        await self.highrise.chat("No saved outfit slots yet. Use -savefit [slot] [name] to start!")
                        return
                    with open("outfit_slots.json", "r") as f:
                        slots = json.load(f)
                    
                    if not slots:
                        await self.highrise.chat("Your outfit library is currently empty. Use -savefit [slot] [name] to save your first look! ✨")
                        return
    
                    slots_text = "✨ ━━ 🎧 𝐎𝐔𝐓𝐅𝐈𝐓 𝐋𝐈𝐁𝐑𝐀𝐑𝐘 ━━ ✨\n\n"
                    
                    # Sort slots: Numbers first, then names
                    num_slots = []
                    name_slots = []
                    for s_key in slots.keys():
                        if s_key.isdigit():
                            num_slots.append(int(s_key))
                        else:
                            name_slots.append(s_key)
                    
                    num_slots.sort()
                    name_slots.sort()
    
                    # Display Numeric Slots
                    if num_slots:
                        slots_text += "📂 <#B8860B>𝐍𝐮𝐦𝐞𝐫𝐢𝐜 𝐒𝐥𝐨𝐭𝐬:\n"
                        for s_num in num_slots:
                            s_key = str(s_num)
                            slot_data = slots[s_key]
                            name = slot_data.get("name", "Unnamed") if isinstance(slot_data, dict) else "Legacy Save"
                            slots_text += f"  <#cdaa54>Slot {s_num}: ✅ {name}\n"
                        slots_text += "\n"
    
                    # Display Custom Named Slots
                    if name_slots:
                        slots_text += "🏷️ <#B8860B>𝐂𝐮𝐬𝐭𝐨𝐦 𝐒𝐥𝐨𝐭𝐬:\n"
                        for s_key in name_slots:
                            slot_data = slots[s_key]
                            name = slot_data.get("name", "Unnamed") if isinstance(slot_data, dict) else "Legacy Save"
                            slots_text += f"  <#cdaa54>{s_key}: ✅ {name}\n"
                        slots_text += "\n"
    
                    slots_text += "💡 <#e2ce9d>Type -loadfit [slot_name] to change!"
                    
                    await self.send_room_chunks(slots_text)
                except Exception as e:
                    await self.highrise.send_whisper(user.id, f"Error listing fits: {e}")
    
            elif msg.startswith("-removefit "):
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.send_whisper(user.id, "Usage: -removefit [slot_number/name]")
                    return
                target = parts[1].lower()
                
                try:
                    if not os.path.exists("outfit_slots.json"):
                        await self.highrise.send_whisper(user.id, "No saved outfits found.")
                        return
                        
                    with open("outfit_slots.json", "r") as f:
                        slots = json.load(f)
                    
                    removed = False
                    # Try removal by slot number
                    if target in slots:
                        del slots[target]
                        removed = True
                    else:
                        # Try removal by name
                        for slot, data in list(slots.items()):
                            if isinstance(data, dict) and data.get("name", "").lower() == target:
                                del slots[slot]
                                removed = True
                                break
                    
                    if removed:
                        with open("outfit_slots.json", "w") as f:
                            json.dump(slots, f, indent=4)
                        await self.highrise.chat(f"🗑️ Outfit '{target}' has been removed successfully!")
                    else:
                        await self.highrise.send_whisper(user.id, f"Could not find an outfit named/numbered '{target}'.")
                except Exception as e:
                    await self.highrise.send_whisper(user.id, f"Failed to remove fit: {e}")
    
            elif msg == "-resetfit":
                if user.username not in owners:
                    await self.highrise.send_whisper(user.id, "Only owners can use this command.")
                    return
                config["bot_outfit"] = None
                with open(get_path("config.json"), "w") as f:
                    json.dump(config, f, indent=4)
                await self.highrise.chat("🔄 Bot outfit has been reset! Please restart the bot to apply standard look.")
        
        except Exception as e:
            print(f"CRITICAL ERROR in on_chat (User: {user.username}): {e}")
            import traceback
            traceback.print_exc()


    async def on_message(self, user_id: str, conversation_id: str, is_new_conversation: bool) -> None:
        """Called when a user sends a direct message to the bot."""
        # Need to fetch the message content first if we want to read it.
        # However, the SDK triggers on_message but getting the actual text might require get_messages.
        try:
            response = await self.highrise.get_messages(conversation_id)
            if hasattr(response, "messages") and len(response.messages) > 0:
                message_text = response.messages[0].content
                msg = message_text.lower().strip()
                
                try:
                    with open(get_path("config.json"), "r") as f:
                        config = json.load(f)
                except: config = {}
                
                # Cache the conversation ID and persist it
                user_id_str = str(user_id)
                self.conv_ids[user_id_str] = conversation_id
                try:
                    with open(get_path("conv_ids.json"), "w") as f:
                        json.dump(self.conv_ids, f)
                except: pass

                # Dynamic Authentication: If we don't know who this is, securely fetch their profile
                if user_id_str not in self.user_id_to_name:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(f"https://webapi.highrise.game/users/{user_id_str}") as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    fetched_username = data.get("user", {}).get("username")
                                    if fetched_username:
                                        self.user_id_to_name[user_id_str] = fetched_username
                    except: pass

                # Block Checker (DMs)
                username = self.user_id_to_name.get(str(user_id), "").lower()
                if msg.startswith("-") and username in [u.lower() for u in config.get("blocked_users", [])]:
                    # Check if they are owner by searching config
                    if username not in [u.lower() for u in config.get("owners", [])]:
                        await self.highrise.send_message(conversation_id, "🚫 You are BLOCKED from using bot commands.")
                        return

                if msg == "-sub":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if username == "Unknown":
                        return
                    
                    try:
                        subs = []
                        if os.path.exists(get_path("subscribed_users.json")):
                            with open(get_path("subscribed_users.json"), "r") as f:
                                subs = json.load(f)
                        
                        if username not in subs:
                            subs.append(username)
                            with open(get_path("subscribed_users.json"), "w") as f:
                                json.dump(subs, f, indent=4)
                            await self.highrise.send_message(conversation_id, f"🎵 Thanks for subscribing, @{username} ! 😄 Enjoy free music 🎧 and VIP perks! 💖")
                        else:
                            await self.highrise.send_message(conversation_id, "You are already subscribed! ✨")
                    except Exception as e:
                        print(f"Error in DM -sub: {e}")

                elif msg == "-np":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    song_msg = self.get_np_message()
                    await self.highrise.send_message(conversation_id, song_msg)

                elif msg == "-q":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    
                    # Already in DMs, use the full detailed format
                    q_msg = self.get_formatted_queue(detailed=True)
                    await self.send_dm_id_chunks(conversation_id, q_msg)

                elif msg in ["-myq", "-mq"]:
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    user_songs = []
                    if getattr(self, "song_queue", None) and len(self.song_queue) > 1:
                        for i, song_info in enumerate(self.song_queue[1:]):
                            if song_info['user'] == username:
                                user_songs.append((i + 1, song_info['song']))
                    
                    if user_songs:
                        my_q_msg = "<#B8860B>✨ ━━ 🎧 𝐌𝐘 𝐐𝐔𝐄𝐔𝐄 ━━ ✨\n\n"
                        for pos, song in user_songs:
                            my_q_msg += f"<#cdaa54> {pos}. {song}\n"
                    else:
                        my_q_msg = "<#B8860B>✨ You have no songs in the queue right now! ✨"
                    await self.send_dm_id_chunks(conversation_id, my_q_msg)

                elif msg == "-lf":
                    username = self.user_id_to_name.get(str(user_id))
                    if not username or username == "Unknown":
                        return
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    try:
                        with open("favorites.json", "r") as f:
                            favs = json.load(f)
                    except: favs = {}
                    user_favs = favs.get(username, [])
                    if not user_favs:
                        lf_msg = "<#B8860B>✨ You have no favorites yet! Use -af in the room. ✨"
                    else:
                        lf_msg = "<#B8860B>✨ ━━ 🎧 𝐌𝐘 𝐅𝐀𝐕𝐎𝐑𝐈𝐓𝐄𝐒 ━━ ✨\n\n"
                        for i, song in enumerate(user_favs):
                            lf_msg += f"<#cdaa54> {i+1}. {song}\n\n"
                        lf_msg += "<#B8860B>Use -pf [number] in the room to play one!"
                    await self.send_dm_id_chunks(conversation_id, lf_msg)

                elif msg == "-prices":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    try:
                        with open(get_path("config.json"), "r") as f:
                            config = json.load(f)
                    except: config = {}
                    temp_price = config.get("vip_temp_price", 500)
                    perm_price = config.get("vip_perm_price", 2000)
                    paid_mode = config.get("paid_mode", False)
                    status = "🟢 ON" if paid_mode else "🔴 OFF"
                    prices_msg = (f"<#B8860B>✨ ━━ 💎 𝐕𝐈𝐏 𝐏𝐑𝐈𝐂𝐄𝐒 ━━ ✨\n\n"
                                  f"<#cdaa54>⏳ Temporary VIP: {temp_price} Gold\n"
                                  f"<#cdaa54>♾️ Permanent VIP: {perm_price} Gold\n\n"
                                  f"<#e2ce9d>💳 Paid Mode Status: {status}\n"
                                  f"<#B8860B>Use -buyvip temp OR -buyvip perm in the room!")
                    await self.highrise.send_message(conversation_id, prices_msg)

                elif msg == "-rolelist":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    owners = config.get("owners", [])
                    mods = config.get("moderators", [])
                    if username.lower() not in [o.lower() for o in owners] and username.lower() not in [m.lower() for m in mods]:
                        await self.highrise.send_message(conversation_id, "ℹ️ You don't have permission to use this command.")
                        return

                    role_msg = "<#B8860B>✨ ━━ 🛡️ 𝐑𝐎𝐋𝐄 𝐋𝐈𝐒𝐓 ━━ ✨\n\n"
                    role_msg += "<#cdaa54>👑 𝐎𝐰𝐧𝐞𝐫𝐬:\n"
                    unique_owners = list(set(owners))
                    if not unique_owners:
                        role_msg += "<#e2ce9d>  - None\n"
                    else:
                        for o in unique_owners:
                            role_msg += f"<#e2ce9d>  - @{o}\n"
                    
                    role_msg += "\n<#cdaa54>🛡️ 𝐌𝐨𝐝𝐞𝐫𝐚𝐭𝐨𝐫𝐬:\n"
                    unique_mods = list(set(mods))
                    if not unique_mods:
                        role_msg += "<#e2ce9d>  - None\n"
                    else:
                        for m in unique_mods:
                            role_msg += f"<#e2ce9d>  - @{m}\n"
                    
                    await self.send_dm_id_chunks(conversation_id, role_msg)

                elif msg == "-viplist":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    owners = config.get("owners", [])
                    mods = config.get("moderators", [])
                    try:
                        with open(get_path("vip_users.json"), "r") as f:
                            vip_users = json.load(f)
                    except: vip_users = {}
                    
                    if username.lower() not in [o.lower() for o in owners] and username.lower() not in [m.lower() for m in mods] and username.lower() not in [v.lower() for v in vip_users]:
                        await self.highrise.send_message(conversation_id, "You need VIP, Moderator or Owner role to view the VIP list.")
                        return

                    vip_msg = "<#B8860B>✨ ━━ 💎 𝐕𝐈𝐏 𝐌𝐄𝐌𝐁𝐄𝐑𝐒 ━━ ✨\n\n"
                    if not vip_users:
                        vip_msg += "<#e2ce9d>No VIPs currently."
                    else:
                        for v, data in vip_users.items():
                            v_type = data.get("type", "perm").capitalize()
                            vip_msg += f"<#cdaa54>💎 @{v} <#e2ce9d>({v_type})\n"
                    await self.send_dm_id_chunks(conversation_id, vip_msg)

                # Line 1110 approx
                elif msg == "-help":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    username = self.user_id_to_name.get(str(user_id), "User")
                    welcome_msg = (
                        f"🌟 <#B8860B>𝐖𝐞𝐥𝐜𝐨𝐦𝐞 𝐭𝐨 @{username} 𝐀𝐧𝐧𝐨𝐮𝐧𝐜𝐞𝐦𝐞𝐧𝐭! 🌟\n\n"
                        f"🎉 <#cdaa54>𝐓𝐡𝐚𝐧𝐤 𝐲𝐨𝐮 𝐟𝐨𝐫 𝐬𝐮𝐛𝐬𝐜𝐫𝐢𝐛𝐢𝐧𝐠! 🎉\n\n"
                        f"✨ <#e2ce9d>Stay tuned for automatic updates on our exciting events, giveaways, and exclusive invitations. Be the first to know whenever we're hosting something amazing!\n\n"
                        f"💬 <#cdaa54>𝐃𝐢𝐬𝐜𝐥𝐚𝐢𝐦𝐞𝐫: Users who block the bot will be automatically unsubscribed from our notification system.\n\n"
                        f"💖 <#e2ce9d>Happy connecting and exploring with us! 🎉 𝐘𝐨𝐮'𝐯𝐞 𝐛𝐞𝐞𝐧 𝐚𝐮𝐭𝐨𝐦𝐚𝐭𝐢𝐜𝐚𝐥𝐥𝐲 𝐬𝐮𝐛𝐬𝐜𝐫𝐢𝐛𝐞𝐝!\n"
                        f"🎧 <#cdaa54>Now you can access the queue, currently playing songs, your favorites, and more!\n"
                        f"📍 To view the queue: -q\n"
                        f"📍 To view now playing: -np\n"
                        f"📍 To manage favorites: -af, -lf, -df\n"
                        f"📍 To unsubscribe: -unsub"
                    )
                    help_msg1 = (
                        f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (1/4) ━━ ✨\n\n"
                        f"<#cdaa54>🎵 𝐌𝐮𝐬𝐢𝐜 𝐐𝐮𝐞𝐮𝐞:\n"
                        f"<#e2ce9d>-play [song] - Request a song\n"
                        f"-np - Now playing info\n"
                        f"-next - View next song\n"
                        f"-q - View entire queue\n"
                        f"-mq - View your queue\n"
                        f"-skip - Skip current song\n"
                        f"-clear [num] - Clear your song"
                    )
                    help_msg2 = (
                        f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (2/4) ━━ ✨\n\n"
                        f"<#cdaa54>🎟️ 𝐒𝐥𝐨𝐭 𝐒𝐲𝐬𝐭𝐞𝐦:\n"
                        f"<#e2ce9d>-myslots - Check your slots\n"
                        f"-give [amt] @user - Transfer slots\n"
                        f"💡 Tip 1g for 1 Slot! New users get 5 free.\n\n"
                        f"<#cdaa54>❤️ 𝐅𝐚𝐯𝐨𝐫𝐢𝐭𝐞𝐬 & 𝐋𝐢𝐤𝐞𝐬:\n"
                        f"<#e2ce9d>-af / -df - Add/Delete from favorites\n"
                        f"-lf / -pf [no] - List/Play favorites\n"
                        f"-gift @user [song] - Gift a song\n"
                        f"-like / -dislike - Rate the playing song"
                    )
                    help_msg3 = (
                        f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (3/4) ━━ ✨\n\n"
                        f"<#cdaa54>💎 𝐕𝐈𝐏 & 𝐄𝐜𝐨𝐧𝐨𝐦𝐲:\n"
                        f"<#e2ce9d>-bal - Check your gold balance\n"
                        f"-prices - View VIP costs\n"
                        f"-buyvip [temp/perm] - Buy VIP rank"
                    )
                    help_msg4 = (
                        f"<#B8860B>✨ ━━ 📜 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 (4/4) ━━ ✨\n\n"
                        f"<#cdaa54>🛡️ 𝐀𝐝𝐦𝐢𝐧𝐢𝐬𝐭𝐫𝐚𝐭𝐢𝐨𝐧:\n"
                        f"<#e2ce9d>-paidmode on/off - Toggle slot system\n"
                        f"-vip / -remvip @user - Set manual VIP\n"
                        f"-viplist / -rolelist - View members list\n"
                        f"-mod / -remmod @user - Manage Moderators\n"
                        f"-owner / -remowner @user - Manage Owners\n"
                        f"-invite - Send mass PM invites\n"
                        f"-getfit @user - Copy user fit\n\n"

                        f"<#cdaa54>🤖 𝐆𝐞𝐧𝐞𝐫𝐚𝐥:\n"
                        f"<#e2ce9d>-ping - Test bot latency\n"
                        f"-unsub - Stop receiving DMs\n"
                        f"-setpos - Set DJ spot (Owners)"
                    )
                    await self.highrise.send_message(conversation_id, welcome_msg)
                    await asyncio.sleep(0.5)
                    await self.highrise.send_message(conversation_id, help_msg1)
                    await asyncio.sleep(0.5)
                    await self.highrise.send_message(conversation_id, help_msg2)
                    await asyncio.sleep(0.5)
                    await self.highrise.send_message(conversation_id, help_msg3)
                    await asyncio.sleep(0.5)
                    await self.highrise.send_message(conversation_id, help_msg4)

                elif msg == "-unsub":
                    try:
                        username = self.user_id_to_name.get(str(user_id), "Unknown")
                        subs = []
                        if os.path.exists(get_path("subscribed_users.json")):
                            with open(get_path("subscribed_users.json"), "r") as f:
                                subs = json.load(f)
                        
                        if username in subs:
                            subs.remove(username)
                            with open(get_path("subscribed_users.json"), "w") as f:
                                json.dump(subs, f, indent=4)
                            await self.highrise.send_message(conversation_id, "❌ You've successfully unsubscribed. 😢\n\nYou can resubscribe at any time to get access to the queue, currently playing songs, emote list, and other features! 🎶🎧")
                        else:
                            await self.highrise.send_message(conversation_id, "You are not currently subscribed. 👤")
                    except Exception as e:
                        print(f"Error in DM -unsub: {e}")

                elif msg == "-next":
                    username = self.user_id_to_name.get(str(user_id), "Unknown")
                    if not self.is_subscribed(username) and not self.is_privileged(username, config):
                        await self.highrise.send_message(conversation_id, "🚫 This command is LOCKED! Type -sub to unlock. 🔓")
                        return
                    queue = getattr(self, "song_queue", [])
                    if len(queue) > 1:
                        next_song = queue[1]
                        next_msg = (f"<#B8860B>✨ ━━ 🎧 𝐍𝐄𝐗𝐓 𝐒𝐎𝐍𝐆 ━━ ✨\n\n"
                                    f"🎶 𝙎𝙤𝙣𝙜: {next_song['song']}\n"
                                    f"👤 𝙍𝙚𝙦𝙪𝙚𝙨𝙩: @{next_song['user']}")
                    else:
                        next_msg = "<#B8860B>✨ There is no song queued after the current one! ✨"
                    await self.highrise.send_message(conversation_id, next_msg)
                
                elif msg == "-ping":
                    await self.highrise.send_message(conversation_id, "Pong!")
                    
        except Exception as e:
            print(f"Error handling DM: {e}")


if __name__ == "__main__":
    try:
        import sys
        import subprocess
        
        while True:
            try:
                with open(get_path("config.json"), "r") as f:
                    config = json.load(f)
            except FileNotFoundError:
                print("Error: config.json not found!")
                config = {}
            except json.JSONDecodeError:
                print("Error: config.json is not a valid JSON file!")
                config = {}
            except Exception as e:
                print(f"Error loading config.json: {e}")
                config = {}

            bot_token = os.environ.get("BOT_TOKEN", config.get("bot_token", ""))
            room_id = os.environ.get("ROOM_ID", config.get("room_id", ""))

            if not bot_token or not room_id:
                print("Error: BOT_TOKEN and ROOM_ID must be set (env vars or config.json)")
                time.sleep(5)
                continue
                
            print(f"[{time.strftime('%H:%M:%S')}] Starting bot for room: {room_id}")
            command = [sys.executable, "-W", "ignore", "-m", "highrise", "main:MyBot", str(room_id), str(bot_token)]
            process = subprocess.Popen(command, cwd=BASE_DIR)
            process.wait()
            
            print(f"[{time.strftime('%H:%M:%S')}] Bot process exited with code {process.returncode}. Restarting in 5 seconds...")
            time.sleep(5)
    except KeyboardInterrupt:
        print("Bot manually stopped.")
    except Exception as e:
        print(f"Manual start error: {e}")
        import traceback
        traceback.print_exc()