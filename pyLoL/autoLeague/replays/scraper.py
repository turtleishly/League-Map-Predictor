import os
import shutil
import time
import json
import subprocess
import base64
import requests
import re
import ctypes
from ctypes import wintypes
from pathlib import Path
from threading import Thread, Event, Lock
import pydirectinput
import mss
import mss.tools
from requests.auth import HTTPBasicAuth
from tqdm import tqdm


class InGamePlayerPoller(Thread):
    """
    Lightweight background poller for the in-game Live Client API (port 2999).
    Polls /liveclientdata/playerlist asynchronously so it never slows down
    or drops frames in the 10x replay screenshot loop.
    """
    def __init__(self, stop_event, poll_interval=0.04):
        super().__init__(daemon=True)
        self.stop_event = stop_event
        self.poll_interval = poll_interval
        self.latest_data = {}
        self.lock = Lock()

    def run(self):
        requests.packages.urllib3.disable_warnings()
        url = "https://127.0.0.1:2999/liveclientdata/playerlist"
        while not self.stop_event.is_set():
            try:
                res = requests.get(url, verify=False, timeout=0.25)
                if res.status_code == 200:
                    data = res.json()
                    parsed = {}
                    for p in data:
                        if not isinstance(p, dict):
                            continue
                        cname = p.get("championName", "")
                        if not cname:
                            continue
                        scores = p.get("scores", {})
                        parsed[cname] = {
                            "is_dead": bool(p.get("isDead", False)),
                            "respawn_timer": round(float(p.get("respawnTimer", 0.0)), 1),
                            "kills": int(scores.get("kills", 0)),
                            "deaths": int(scores.get("deaths", 0)),
                            "assists": int(scores.get("assists", 0)),
                            "creep_score": int(scores.get("creepScore", 0)),
                            "level": int(p.get("level", 1)),
                            "team": str(p.get("team", "")),
                            "position": str(p.get("position", "")),
                            "items": [int(item.get("itemID", 0)) for item in p.get("items", []) if isinstance(item, dict) and item.get("itemID")]
                        }
                    if parsed:
                        with self.lock:
                            self.latest_data = parsed
            except Exception:
                pass
            time.sleep(self.poll_interval)

    def get_latest(self):
        with self.lock:
            return self.latest_data.copy()

TEAM_HOTKEY_DICT = {'Red': 'f2', 'Blue': 'f1', 'All': 'f3'}

DEFAULT_BANNED_CHAMPIONS = (
    "Shyvana",
    "Fiddlesticks",
    "Fiddle",
    "XinZhao",
    "Xin Zhao",
    "Mel",
    "Locke",
    "Neeko",
    "Garen",
    "Ambessa",
    "Aurora",
    "Yunara",
    "Zaahen"
)


class ReplayScraper(object):
    """
    Robust League of Legends Replay Scraper.
    
    Handles:
    - LCU client authentication & lockfile management
    - Automatic replay downloading (.rofl) via LCU API
    - Multi-layer Validation:
        1. Riot API online pre-filter (queue & champions before download)
        2. Local ROFL header regex (pre-launch)
        3. In-Game Live Client API (port 2999 /liveclientdata/gamestats) to reject ARAM / Arena / URF
        4. In-Game Live Client API (port 2999 /liveclientdata/playerlist) for 100% champion accuracy
    - Automatic cleanup: deletes match folder immediately if a game is aborted
    - Dynamic Fog of War control per POV (All = False, Blue = True)
    - Minimap screenshot capturing via mss across multiple POVs (All / Blue / Red)
    - Producer-Consumer continuous pipeline for overnight scraping
    - Resilient process management, failure recovery, and unattended batch execution
    """

    DEFAULT_BANNED_CHAMPIONS = DEFAULT_BANNED_CHAMPIONS

    def __init__(
        self,
        game_dir=r"C:\Riot Games\League of Legends\Game",
        replay_dir=r"C:\Users\Aiden\Videos\League of Legends\Replays",
        save_dir=r"C:\Users\Aiden\Coding\python_ws\LeagueMapPredictor\Dataset",
        scraper_dir=r"C:\Users\Aiden\Coding\python_ws\LeagueMapPredictor\pyLoL\autoLeague\replays",
        lockfile_path=r"C:\Riot Games\League of Legends\lockfile",
        replay_speed=10,
        region="SG2",
        monitor=None,
        ftp_server='ftp_server_ip',
        ftp_username='ftp_username',
        ftp_password='ftp_password',
        local_folder_path=r'C:\dataset',
        remote_folder_path='/home/username'
    ):
        self.game_dir = str(game_dir)
        self.replay_dir = str(replay_dir)
        self.save_dir = str(save_dir)
        self.scraper_dir = str(scraper_dir)
        self.lockfile_path = str(lockfile_path)
        self.replay_speed = int(replay_speed)
        self.region = region

        self.DEFAULT_BANNED_CHAMPIONS = DEFAULT_BANNED_CHAMPIONS

        # Default minimap capture region (1920x1080 resolution)
        self.monitor = monitor or {"top": 546, "left": 1386, "width": 512, "height": 512}

        # FTP / Legacy settings
        self.ftp_server = ftp_server
        self.ftp_username = ftp_username
        self.ftp_password = ftp_password
        self.local_folder_path = local_folder_path
        self.remote_folder_path = remote_folder_path

        # Ensure directories exist
        os.makedirs(self.save_dir, exist_ok=True)
        if os.path.exists(self.replay_dir):
            rofls = [f for f in os.listdir(self.replay_dir) if f.endswith(".rofl")]
            print(f"📁 Replay Directory: '{self.replay_dir}' | Available .rofl files: {len(rofls)}")
        else:
            print(f"⚠️ Replay Directory not found: {self.replay_dir}")

    # ─────────────────────────────────────────────────────────────
    # LCU Lockfile & Auth Helpers
    # ─────────────────────────────────────────────────────────────
    def get_lcu_auth(self):
        """Reads port and password from the League Client lockfile."""
        if not os.path.exists(self.lockfile_path):
            raise FileNotFoundError(
                f"❌ Lockfile not found at '{self.lockfile_path}'. "
                "Please make sure the League of Legends Client UX is running."
            )
        try:
            with open(self.lockfile_path, "r", encoding="utf-8") as f:
                parts = f.read().strip().split(":")
                port = parts[2]
                password = parts[3]
                return port, password
        except Exception as e:
            raise RuntimeError(f"❌ Failed to parse lockfile: {e}")

    # ─────────────────────────────────────────────────────────────
    # Champion & Game Mode Inspection (Online, Header, In-Game)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def get_match_champions_api(match_id, api_key, routing_region="sea"):
        """Fetches the 10 champion names in a match using the Riot Match-V5 API."""
        clean_id = str(match_id).replace("-", "_")
        url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/{clean_id}?api_key={api_key}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                participants = data.get("info", {}).get("participants", [])
                champs = [p.get("championName", "") for p in participants if "championName" in p]
                if champs:
                    return champs
            elif res.status_code == 401:
                print(f"\n🔑 Riot API Key has expired (401 Unauthorized)! Please refresh your key at https://developer.riotgames.com/")
        except Exception:
            pass
        return []

    def get_match_roles_api(self, match_id, api_key, routing_region="sea", save_to_match_dir=True):
        """
        Fetches the 10 champion official roles using the Riot Match-V5 API.
        Optionally saves to Dataset/{match_id}/roles.json.
        Returns dict: {champion_name: 'Blue_Top', ...}
        """
        clean_id = str(match_id).replace("-", "_")
        url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/{clean_id}?api_key={api_key}"
        pos_map = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "Bot", "UTILITY": "Support"}
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                participants = data.get("info", {}).get("participants", [])
                roles = {}
                for p in participants:
                    champ = p.get("championName", "")
                    team_prefix = "Blue" if p.get("teamId") == 100 else "Red"
                    pos_suffix = pos_map.get(str(p.get("teamPosition", "")).upper(), "Mid")
                    if champ:
                        roles[champ] = f"{team_prefix}_{pos_suffix}"
                if len(roles) == 10:
                    if save_to_match_dir:
                        save_name = str(match_id).replace(".rofl", "")
                        target_dir = os.path.join(self.save_dir, save_name)
                        os.makedirs(target_dir, exist_ok=True)
                        roles_file = os.path.join(target_dir, "roles.json")
                        with open(roles_file, "w", encoding="utf-8") as rf:
                            json.dump(roles, rf, indent=2)
                    return roles
        except Exception:
            pass
        return {}

    @staticmethod
    def get_match_champions_rofl(rofl_path):
        """Extracts champion names directly from a local .rofl file header (0 API calls)."""
        if not os.path.exists(rofl_path):
            return []
        try:
            with open(rofl_path, "rb") as f:
                header = f.read(524288) # Read up to 512KB
                found_champs = re.findall(rb'(?i)"(?:skin|championName)":\s*"([^"]+)"', header)
                if found_champs:
                    return [c.decode("utf-8", errors="ignore") for c in found_champs]
        except Exception:
            pass
        return []

    @staticmethod
    def get_match_champions_ingame():
        """
        Queries the in-game Live Client API (port 2999) while the game is running.
        Returns the exact 10 champion names currently on the map with 100% accuracy.
        """
        requests.packages.urllib3.disable_warnings()
        try:
            res = requests.get("https://127.0.0.1:2999/liveclientdata/playerlist", verify=False, timeout=2.0)
            if res.status_code == 200:
                players = res.json()
                champs = [p.get("championName", "") for p in players if isinstance(p, dict) and "championName" in p]
                if champs:
                    return champs
        except Exception:
            pass
        return []

    @staticmethod
    def get_match_gamestats_ingame():
        """
        Queries the in-game Live Client API (port 2999) for game mode and map name.
        """
        requests.packages.urllib3.disable_warnings()
        try:
            res = requests.get("https://127.0.0.1:2999/liveclientdata/gamestats", verify=False, timeout=2.0)
            if res.status_code == 200:
                return res.json()
        except Exception:
            pass
        return {}

    def get_match_champions(self, match_id, api_key=None, routing_region="sea"):
        """
        Gets the 10 champions for a match:
        First checks local .rofl header. If empty and api_key provided, queries Riot API.
        """
        raw_id = re.split(r'[-_]', str(match_id))[-1]
        for fname in os.listdir(self.replay_dir) if os.path.exists(self.replay_dir) else []:
            if fname.endswith(".rofl") and raw_id in fname:
                champs = self.get_match_champions_rofl(os.path.join(self.replay_dir, fname))
                if champs:
                    return champs
                break

        if api_key:
            return self.get_match_champions_api(match_id, api_key, routing_region=routing_region)

        return []

    @staticmethod
    def check_banned_champions(champions, banned_list):
        """
        Checks if any champion in `champions` matches `banned_list`.
        Returns (is_allowed: bool, detected_banned: list).
        """
        if not banned_list:
            return True, []
        if not champions:
            return True, []
            
        normalized_banned = {b.lower().replace(" ", "").replace("'", "") for b in banned_list}
        detected = []
        
        for c in champions:
            clean = c.lower().replace(" ", "").replace("'", "")
            if clean in normalized_banned:
                detected.append(c)
                
        return (len(detected) == 0), list(set(detected))

    # ─────────────────────────────────────────────────────────────
    # Process & Window Management
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def focus_league_window():
        """Brings the League of Legends game window reliably to the foreground."""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            hwnd = user32.FindWindowW(None, "League of Legends (TM) Client")
            if not hwnd:
                def enum_proc(h, lparam):
                    length = user32.GetWindowTextLengthW(h)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(h, buff, length + 1)
                        if "League of Legends" in buff.value and user32.IsWindowVisible(h):
                            lparam[0] = h
                            return False
                    return True

                EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p))
                found = [0]
                user32.EnumWindows(EnumWindowsProc(lambda h, lp: enum_proc(h, found)), 0)
                hwnd = found[0]

            if not hwnd:
                return False

            user32.ShowWindow(hwnd, 9)  # 9 = SW_RESTORE

            fore_hwnd = user32.GetForegroundWindow()
            if fore_hwnd != hwnd:
                fore_thread = user32.GetWindowThreadProcessId(fore_hwnd, None)
                app_thread = kernel32.GetCurrentThreadId()

                if fore_thread != app_thread:
                    user32.AttachThreadInput(fore_thread, app_thread, True)
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    user32.AttachThreadInput(fore_thread, app_thread, False)
                else:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)

            time.sleep(0.4)
            return (user32.GetForegroundWindow() == hwnd)
        except Exception as e:
            print(f"⚠️ Notice focusing League window: {e}")
            return False

    def kill_client(self):
        """Forcefully closes any running in-game League of Legends executable."""
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "League of Legends.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
        except Exception:
            pass
        time.sleep(2.0)

    # ─────────────────────────────────────────────────────────────
    # Checkpoint & Skip Logic
    # ─────────────────────────────────────────────────────────────
    def is_match_scraped(self, game_id, teams=("All", "Blue"), min_frames=100):
        """
        Checks if a match has already been successfully scraped for all requested teams.
        """
        clean_id = re.split(r'[-_]', str(game_id))[-1] if not str(game_id).startswith(self.region) else str(game_id)
        candidates = [str(game_id), clean_id, f"{self.region}-{clean_id}", f"{self.region}_{clean_id}"]
        
        for cand in candidates:
            match_folder = os.path.join(self.save_dir, cand)
            if not os.path.isdir(match_folder):
                continue
            
            all_teams_valid = True
            for team in teams:
                team_folder_0 = os.path.join(match_folder, "0", team)
                team_folder_direct = os.path.join(match_folder, team)
                
                target_folder = team_folder_0 if os.path.isdir(team_folder_0) else team_folder_direct
                if not os.path.isdir(target_folder):
                    all_teams_valid = False
                    break
                
                png_count = len([f for f in os.listdir(target_folder) if f.endswith(".png")])
                if png_count < min_frames:
                    all_teams_valid = False
                    break
            
            if all_teams_valid:
                return True

        return False

    # ─────────────────────────────────────────────────────────────
    # Replay Downloader via LCU API
    # ─────────────────────────────────────────────────────────────
    def download_replay(self, game_id, timeout=60):
        """
        Requests the League client to download the .rofl file for game_id.
        Returns the path to the downloaded .rofl file if successful.
        """
        raw_id = re.split(r'[-_]', str(game_id))[-1]
        
        if os.path.exists(self.replay_dir):
            for fname in os.listdir(self.replay_dir):
                if fname.endswith(".rofl") and raw_id in fname:
                    return os.path.join(self.replay_dir, fname)

        try:
            port, password = self.get_lcu_auth()
            url = f"https://127.0.0.1:{port}/lol-replays/v1/rofls/{raw_id}/download"
            auth = HTTPBasicAuth("riot", password)
            payload = {"componentType": "match-history", "contextData": "match-history"}

            requests.packages.urllib3.disable_warnings()
            res = requests.post(url, auth=auth, verify=False, json=payload, timeout=10)
            
            if res.status_code not in (200, 204):
                return None

            start_wait = time.time()
            while time.time() - start_wait < timeout:
                for fname in os.listdir(self.replay_dir):
                    if fname.endswith(".rofl") and raw_id in fname:
                        time.sleep(3.0)
                        return os.path.join(self.replay_dir, fname)
                time.sleep(2.0)

        except Exception as e:
            print(f"⚠️ Error triggering download for {raw_id}: {e}")

        return None

    def clear_replays(self):
        """
        Deletes all existing .rofl replay files from the replay directory.
        Useful when a new League of Legends patch makes older replays unplayable.
        Returns the count of deleted files.
        """
        deleted_count = 0
        if os.path.exists(self.replay_dir):
            for fname in os.listdir(self.replay_dir):
                if fname.endswith(".rofl"):
                    try:
                        os.remove(os.path.join(self.replay_dir, fname))
                        deleted_count += 1
                    except Exception as e:
                        print(f"⚠️ Could not delete '{fname}': {e}")
        return deleted_count

    def fetch_and_download_replays(
        self,
        api_key,
        count=3,
        tier="PLATINUM",
        division="I",
        queue="RANKED_SOLO_5x5",
        routing_region="sea",
        banned_champions=None,
        days_back=5,
        timeout=60
    ):
        """
        Discovers and downloads `count` fresh .rofl files from Riot API via LCU.
        Filters out already-scraped matches and matches with banned champions.
        """
        banned_champions = banned_champions or self.DEFAULT_BANNED_CHAMPIONS
        downloaded = []
        page = 1
        start_time_epoch = int(time.time()) - (days_back * 24 * 60 * 60)

        print(f"🔍 Searching for {count} fresh replays in {self.region} ({tier} {division})...")
        while len(downloaded) < count and page <= 5:
            puuid_url = f"https://{self.region.lower()}.api.riotgames.com/lol/league-exp/v4/entries/{queue}/{tier.upper()}/{division.upper()}?page={page}&api_key={api_key}"
            try:
                res = requests.get(puuid_url, timeout=10)
                if res.status_code == 401:
                    print("❌ Riot API key is expired (401 Unauthorized)!")
                    break
                elif res.status_code != 200:
                    print(f"⚠️ League API returned status {res.status_code}. Retrying...")
                    time.sleep(3)
                    page += 1
                    continue
                entries = res.json()
                if not entries:
                    break
                puuids = [e['puuid'] for e in entries if 'puuid' in e]
            except Exception as e:
                print(f"⚠️ Error fetching summoners: {e}")
                break

            for puuid in puuids:
                if len(downloaded) >= count:
                    break
                match_url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&startTime={start_time_epoch}&count=5&api_key={api_key}"
                try:
                    m_res = requests.get(match_url, timeout=10)
                    if m_res.status_code != 200:
                        continue
                    for match_id in m_res.json():
                        if len(downloaded) >= count:
                            break
                        if self.is_match_scraped(match_id):
                            continue
                        raw_id = re.split(r'[-_]', match_id)[-1]
                        if any(raw_id in f for f in os.listdir(self.replay_dir) if f.endswith(".rofl")):
                            continue

                        # Champion blacklist check
                        if banned_champions:
                            champs = self.get_match_champions_api(match_id, api_key, routing_region)
                            if champs:
                                is_allowed, banned_found = self.check_banned_champions(champs, banned_champions)
                                if not is_allowed:
                                    print(f"🚫 Skipping {match_id}: contains banned champion(s): {banned_found}")
                                    continue

                        print(f"⬇️ Downloading replay for {match_id}...")
                        rofl = self.download_replay(match_id, timeout=timeout)
                        if rofl:
                            print(f"✅ Downloaded: {os.path.basename(rofl)}")
                            downloaded.append(match_id)
                        else:
                            print(f"⚠️ Could not download {match_id}")
                        time.sleep(2.0)
                    time.sleep(1.2)
                except Exception:
                    pass
            page += 1

        print(f"🎉 Successfully downloaded {len(downloaded)}/{count} replays.")
        return downloaded

    # ─────────────────────────────────────────────────────────────
    # In-Game Replay API Configurations
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def post_playback(paused=False, start_time=55, speed=10):
        """Sends playback state to the in-game Replay API (port 2999)."""
        requests.packages.urllib3.disable_warnings()
        return requests.post(
            "https://127.0.0.1:2999/replay/playback",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            data=json.dumps({
                "paused": paused,
                "seeking": False,
                "time": float(start_time - 5),
                "speed": float(speed)
            }),
            verify=False,
            timeout=2.0
        )

    @staticmethod
    def post_render_config(fog_of_war=False):
        """
        Configures clean in-game camera, minimap, and render options.
        Note: fogOfWar is kept False in the HTTP payload to avoid the replay engine
        culling the entire minimap when environment=False. Vision perspective is handled
        accurately via spectator keys (f1 for Blue, f2 for Red, f3 for All).
        """
        requests.packages.urllib3.disable_warnings()
        return requests.post(
            "https://127.0.0.1:2999/replay/render",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            data=json.dumps({
                "banners": True,
                "cameraAttached": False,
                "cameraLookSpeed": 1.0,
                "cameraMode": "fps",
                "cameraMoveSpeed": 10000.0,
                "cameraPosition": {"x": 10585.7, "y": 57447.3, "z": 850.0},
                "cameraRotation": {"x": 347.9, "y": 85.0, "z": 3.0},
                "characters": False,
                "depthFogEnabled": False,
                "depthOfFieldEnabled": False,
                "environment": False,
                "farClip": 66010.0,
                "fieldOfView": 19.0,
                "floatingText": False,
                "fogOfWar": False,
                "healthBarChampions": False,
                "healthBarMinions": False,
                "healthBarPets": False,
                "healthBarStructures": True,
                "healthBarWards": False,
                "heightFogEnabled": False,
                "interfaceAll": True,
                "interfaceAnnounce": False,
                "interfaceChat": False,
                "interfaceFrames": True,
                "interfaceKillCallouts": False,
                "interfaceMinimap": True,
                "interfaceNeutralTimers": False,
                "interfaceQuests": None,
                "interfaceReplay": True,
                "interfaceScore": True,
                "interfaceScoreboard": True,
                "interfaceTarget": False,
                "interfaceTimeline": False,
                "navGridOffset": 0.0,
                "nearClip": 50.0,
                "outlineHover": False,
                "outlineSelect": True,
                "particles": False,
                "selectionName": "",
                "selectionOffset": {"x": 0.0, "y": 0.0, "z": 0.0},
                "skyboxOffset": 0.0,
                "skyboxPath": "",
                "skyboxRadius": 2500.0,
                "skyboxRotation": 0.0,
                "sunDirection": {"x": 0.315, "y": -0.946, "z": 0.063}
            }),
            verify=False,
            timeout=2.0
        )

    # ─────────────────────────────────────────────────────────────
    # Single POV Scraper
    # ─────────────────────────────────────────────────────────────
    def scrape_pov(
        self,
        game_id,
        team="All",
        start_sec=55,
        end_sec=1495,
        speed=None,
        remove_fog_of_war=None,
        banned_champions=None,
        require_classic_sr=True,
        loading_timeout=60
    ):
        """
        Launches the League client for a specific replay and records minimap screenshots for one POV.
        Validates game mode (Summoner's Rift only) and champion blacklist via in-game Live Client API.
        Automatically cleans up match directory if aborted.
        """
        speed = speed or self.replay_speed
        if remove_fog_of_war is None:
            remove_fog_of_war = (team.lower() == "all")

        raw_id = re.split(r'[-_]', str(game_id))[-1]
        save_folder_name = str(game_id).replace(".rofl", "")
        match_folder = os.path.join(self.save_dir, save_folder_name)
        output_dir = os.path.join(match_folder, "0", str(team))
        os.makedirs(output_dir, exist_ok=True)

        # 1. Kill any existing game process
        self.kill_client()

        # 2. Instruct LCU to watch replay
        port, password = self.get_lcu_auth()
        watch_url = f"https://127.0.0.1:{port}/lol-replays/v1/rofls/{raw_id}/watch"
        auth = HTTPBasicAuth("riot", password)
        payload = {"componentType": "match-history", "contextData": "match-history"}

        requests.packages.urllib3.disable_warnings()
        res = requests.post(watch_url, auth=auth, verify=False, json=payload)
        if res.status_code != 204:
            print(f"⚠️ LCU watch command returned status {res.status_code}: {res.text}")

        # 3. Wait for game engine to load and connect to port 2999
        print(f"🚀 Waiting for replay {raw_id} ({team} POV) to load...", end="", flush=True)
        connected = False
        start_wait = time.time()
        time.sleep(15.0)

        while time.time() - start_wait < loading_timeout:
            try:
                res = self.post_playback(paused=False, start_time=start_sec, speed=speed)
                if res.status_code == 200:
                    connected = True
                    print(" ✅ Connected!")
                    break
            except Exception:
                pass
            print(".", end="", flush=True)
            time.sleep(2.0)

        if not connected:
            print(f"\n❌ Timed out waiting for game API for {raw_id}. Skipping POV.")
            self.kill_client()
            shutil.rmtree(match_folder, ignore_errors=True)
            return -1

        # 🛡️ 4. IN-GAME GAME MODE & MAP CHECK (SUMMONER'S RIFT ONLY)
        if require_classic_sr:
            gamestats = self.get_match_gamestats_ingame()
            if gamestats:
                game_mode = str(gamestats.get("gameMode", "")).upper()
                map_name = str(gamestats.get("mapName", ""))
                map_num = gamestats.get("mapNumber", 11)

                is_aram = ("ARAM" in game_mode or "HA" in map_name or "MAP12" in map_name.upper() or map_num == 12)
                is_arena = ("CHERRY" in game_mode or "ARENA" in game_mode or map_num == 30)
                is_not_classic = (game_mode != "CLASSIC" and game_mode != "") or (map_num not in (11, 0))

                if is_aram or is_arena or is_not_classic:
                    print(f"\n🚫 Non-Summoner's Rift game mode detected ({game_mode}, Map: {map_name})! Aborting match.")
                    self.kill_client()
                    shutil.rmtree(match_folder, ignore_errors=True)
                    print(f"🗑️ Deleted non-SR match folder '{match_folder}'")
                    return -3 # -3 signifies invalid game mode

        # 🛡️ 5. IN-GAME LIVE CLIENT CHAMPION CHECK (PORT 2999)
        if banned_champions:
            ingame_champs = self.get_match_champions_ingame()
            if ingame_champs:
                is_allowed, banned_found = self.check_banned_champions(ingame_champs, banned_champions)
                if not is_allowed:
                    print(f"\n🚫 In-Game API detected banned champion(s): {banned_found}! Aborting match immediately.")
                    self.kill_client()
                    shutil.rmtree(match_folder, ignore_errors=True)
                    print(f"🗑️ Deleted banned match folder '{match_folder}'")
                    return -2 # -2 signifies banned champion detected in-game

        # 6. Configure render & playback explicitly
        try:
            self.post_render_config()
            self.post_playback(paused=False, start_time=start_sec, speed=speed)
        except Exception as e:
            print(f"⚠️ Render initialization notice: {e}")

        # 7. Focus League window and apply POV Hotkeys (f1 for Blue, f2 for Red, f3 for All)
        time.sleep(1.0)
        is_focused = self.focus_league_window()
        if not is_focused:
            time.sleep(0.5)
            is_focused = self.focus_league_window()

        if is_focused:
            key = TEAM_HOTKEY_DICT.get(team, 'f3')
            pydirectinput.press(key)
            if remove_fog_of_war or team.lower() == "all":
                time.sleep(0.2)
                pydirectinput.press('f')
        else:
            print(f"\n⚠️ Notice: League window not in foreground; hotkeys skipped to protect IDE/notebook.")
        time.sleep(1.0)

        # 8. Capture Loop & Anti-Hang Safety Setup
        capture_count = 0
        last_saved_sec = -1
        last_sync_time = start_sec
        start_clock = time.time()
        current_anchor = start_sec
        stalled_resync_count = 0

        # Background player state poller (death status, respawn timer, KDA, items)
        poller_stop = Event()
        poller = InGamePlayerPoller(stop_event=poller_stop, poll_interval=0.04)
        poller.start()
        frame_player_states = {}

        # Query total replay length to avoid waiting past the match conclusion
        replay_length = float(end_sec)
        try:
            res = requests.get("https://127.0.0.1:2999/replay/playback", verify=False, timeout=2.0)
            if res.status_code == 200:
                p_data = res.json()
                current_anchor = p_data.get('time', start_sec)
                last_sync_time = current_anchor
                replay_length = p_data.get('length', float(end_sec))
        except Exception:
            pass

        effective_end_sec = min(float(end_sec), replay_length - 1.0)
        if effective_end_sec <= start_sec:
            effective_end_sec = float(end_sec)

        # Hard wall-clock watchdog ceiling (e.g. at 5x speed, 25m takes ~5m; cap at 1.5x + 45s buffer)
        expected_real_duration = max(10.0, (effective_end_sec - start_sec) / speed)
        max_wall_time = (expected_real_duration * 1.5) + 45.0
        wall_start_clock = time.time()

        consecutive_api_failures = 0
        active_threads = []

        try:
            with mss.mss() as sct:
                while True:
                    # Watchdog 1: Hard wall-clock timeout
                    if (time.time() - wall_start_clock) > max_wall_time:
                        print(f"\n⏱️ Reached maximum real-time safety limit ({max_wall_time:.1f}s). Ending capture for {team} POV.")
                        break

                    elapsed_real = time.time() - start_clock
                    game_time = current_anchor + (elapsed_real * speed)

                    # Periodic Resync every 100 game seconds
                    if game_time - last_sync_time >= 100:
                        try:
                            res = requests.get("https://127.0.0.1:2999/replay/playback", verify=False, timeout=1.0)
                            if res.status_code == 200:
                                p_data = res.json()
                                true_time = p_data.get('time', game_time)
                                live_length = p_data.get('length', replay_length)

                                # Watchdog 2: Reached end of replay match
                                if true_time >= (live_length - 2.0):
                                    print(f"\n🏁 Replay reached the end of the match ({true_time:.1f}s / {live_length:.1f}s).")
                                    break

                                # Watchdog 3: Stalled playback detection (game ended or paused indefinitely)
                                if abs(true_time - current_anchor) < 0.5:
                                    stalled_resync_count += 1
                                    if stalled_resync_count >= 2:
                                        print(f"\n⚠️ Replay playback clock stalled at {true_time:.1f}s (game concluded). Ending POV.")
                                        break
                                else:
                                    stalled_resync_count = 0

                                current_anchor = true_time
                                start_clock = time.time()
                                game_time = true_time
                                consecutive_api_failures = 0
                        except Exception:
                            consecutive_api_failures += 1
                            if consecutive_api_failures >= 5:
                                print(f"\n⚠️ Game API disconnected (game concluded or closed early).")
                                break
                        last_sync_time = game_time

                    if game_time >= effective_end_sec:
                        break

                    game_sec = int(game_time)
                    if game_sec != last_saved_sec and game_sec >= (start_sec - 5):
                        out_path = os.path.join(output_dir, f"{game_sec}.png")
                        sct_img = sct.grab(self.monitor)
                        
                        t = Thread(target=mss.tools.to_png, args=(sct_img.rgb, sct_img.size), kwargs={"output": out_path})
                        t.start()
                        active_threads.append(t)

                        # Match player state (death status, timer, KDA, items) to this exact frame
                        current_pstate = poller.get_latest()
                        if current_pstate:
                            frame_player_states[str(game_sec)] = current_pstate

                        last_saved_sec = game_sec
                        capture_count += 1

                    time.sleep(0.045)

        except Exception as e:
            print(f"\n⚠️ Capture loop exception: {e}")
        finally:
            poller_stop.set()
            poller.join(timeout=1.0)
            for t in active_threads:
                t.join(timeout=0.5)
            self.kill_client()

            # Save frame-synced player states to the match parent folder
            if frame_player_states:
                json_path = os.path.join(match_folder, "player_states.json")
                try:
                    existing_data = {}
                    if os.path.exists(json_path):
                        try:
                            with open(json_path, "r", encoding="utf-8") as jf:
                                existing_data = json.load(jf)
                        except Exception:
                            existing_data = {}
                    existing_data.update(frame_player_states)
                    with open(json_path, "w", encoding="utf-8") as jf:
                        json.dump(existing_data, jf, indent=2)
                    print(f"📊 Saved player states ({len(existing_data)} frames) → '{json_path}'")
                except Exception as e:
                    print(f"⚠️ Failed to save player states: {e}")

        print(f"📸 Captured {capture_count} frames for {game_id} ({team} POV)")
        return capture_count

    # ─────────────────────────────────────────────────────────────
    # Full Match Scraper (All + Blue POVs)
    # ─────────────────────────────────────────────────────────────
    def scrape_match(
        self,
        game_id,
        teams=("All", "Blue"),
        start_sec=55,
        end_sec=1495,
        speed=None,
        skip_existing=True,
        banned_champions=DEFAULT_BANNED_CHAMPIONS,
        require_classic_sr=True,
        api_key=None,
        min_frames=100
    ):
        """
        Scrapes all specified POVs for a given match, verifying game mode and champions before launch.
        """
        save_folder_name = str(game_id).replace(".rofl", "")
        match_folder = os.path.join(self.save_dir, save_folder_name)

        if skip_existing and self.is_match_scraped(game_id, teams=teams, min_frames=min_frames):
            return {"status": "skipped", "game_id": str(game_id), "reason": "Already scraped"}

        # 🔍 Champion Blacklist Pre-Check (Local Header / API)
        if banned_champions:
            champs = self.get_match_champions(game_id, api_key=api_key)
            if champs:
                is_allowed, banned_found = self.check_banned_champions(champs, banned_champions)
                if not is_allowed:
                    print(f"🚫 Match {game_id} skipped: contains banned champion(s): {banned_found}")
                    shutil.rmtree(match_folder, ignore_errors=True)
                    return {
                        "status": "filtered",
                        "game_id": str(game_id),
                        "reason": f"Banned champions: {banned_found}"
                    }

        # Fetch & cache ground-truth roles for Data Cleaning
        if api_key:
            self.get_match_roles_api(game_id, api_key=api_key)

        results = {}
        for team in teams:
            remove_fog = (team.lower() == "all")
            frames = self.scrape_pov(
                game_id=game_id,
                team=team,
                start_sec=start_sec,
                end_sec=end_sec,
                speed=speed,
                remove_fog_of_war=remove_fog,
                banned_champions=banned_champions,
                require_classic_sr=require_classic_sr
            )
            
            if frames == -2:
                shutil.rmtree(match_folder, ignore_errors=True)
                return {
                    "status": "filtered",
                    "game_id": str(game_id),
                    "reason": "Banned champion detected via In-Game API"
                }
            elif frames == -3:
                shutil.rmtree(match_folder, ignore_errors=True)
                return {
                    "status": "filtered",
                    "game_id": str(game_id),
                    "reason": "Non-Summoner's Rift mode (ARAM/Arena)"
                }
                
            results[team] = frames
            if frames < min_frames:
                shutil.rmtree(match_folder, ignore_errors=True)
                return {
                    "status": "failed",
                    "game_id": str(game_id),
                    "failed_team": team,
                    "frames": results
                }

        return {"status": "success", "game_id": str(game_id), "frames": results}

    # ─────────────────────────────────────────────────────────────
    # Continuous Producer-Consumer Pipeline (Search -> Download -> Scrape)
    # ─────────────────────────────────────────────────────────────
    def run_continuous_pipeline(
        self,
        api_key,
        target_match_count=50,
        tier="PLATINUM",
        division="I",
        queue="RANKED_SOLO_5x5",
        routing_region="sea",
        teams=("All", "Blue"),
        start_sec=55,
        end_sec=1495,
        speed=10,
        banned_champions=DEFAULT_BANNED_CHAMPIONS,
        require_classic_sr=True,
        delete_rofl_after_scrape=False,
        log_file="continuous_pipeline_summary.json"
    ):
        """
        End-to-End Autonomous Pipeline with In-Game Champion & Game Mode (Classic SR) Verification.
        """
        print("\n" + "=" * 75)
        print(f"🌟 CONTINUOUS SCRAPING PIPELINE | Goal: {target_match_count} Matches")
        print(f"Rank Filter: {tier} {division} | Queue: {queue} | Region: {self.region} ({routing_region})")
        print(f"POVs: {teams} | Speed: {speed}x | Auto-Cleanup ROFL: {delete_rofl_after_scrape}")
        if banned_champions:
            print(f"🚫 Champion Blacklist: {list(banned_champions)}")
        print("=" * 75 + "\n")

        stop_event = Event()
        discovered_matches = set()
        queued_downloads = set()
        scraped_matches = []
        skipped_matches = []
        filtered_ids_set = set()
        filtered_matches_log = []
        failed_matches = []
        failed_ids_set = set()        # ← NEW: prevents infinite retry of broken matches
        match_attempt_count = {}      # ← NEW: tracks retry count per match
        MAX_RETRIES_PER_MATCH = 3     # ← NEW: max attempts before permanently skipping

        summary_path = os.path.join(self.save_dir, log_file)

        # ── Background Producer: Search & Download Worker ─────────────
        def download_worker():
            page = 1
            while not stop_event.is_set() and (len(scraped_matches) + len(queued_downloads) < target_match_count + 5):
                # 1. Fetch PUUIDs for Ranked Tier
                puuid_url = f"https://{self.region.lower()}.api.riotgames.com/lol/league-exp/v4/entries/{queue}/{tier.upper()}/{division.upper()}?page={page}&api_key={api_key}"
                try:
                    res = requests.get(puuid_url, timeout=10)
                    if res.status_code == 401:
                        print(f"\n❌ [Downloader] Riot API Key has EXPIRED (401 Unauthorized)! Please refresh your key at https://developer.riotgames.com/")
                        break
                    elif res.status_code != 200:
                        print(f"\n[Downloader] Warning: League-V4 returned {res.status_code}. Retrying in 10s...")
                        time.sleep(10)
                        continue
                    entries = res.json()
                    if not entries:
                        print(f"\n[Downloader] No more entries on page {page}.")
                        break
                    puuids = [e['puuid'] for e in entries if 'puuid' in e]
                except Exception as e:
                    print(f"\n[Downloader] Error fetching PUUIDs: {e}")
                    time.sleep(10)
                    continue

                # 2. Fetch Match IDs for PUUIDs (Ranked queue = 420 for Solo/Duo)
                start_time_epoch = int(time.time()) - (5 * 24 * 60 * 60)
                for puuid in puuids:
                    if stop_event.is_set() or (len(scraped_matches) + len(queued_downloads) >= target_match_count + 5):
                        break
                    # queue=420 filters specifically for Ranked Solo games on Riot API
                    match_url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&startTime={start_time_epoch}&count=5&api_key={api_key}"
                    try:
                        m_res = requests.get(match_url, timeout=10)
                        if m_res.status_code == 200:
                            for m_id in m_res.json():
                                if m_id in discovered_matches:
                                    continue
                                discovered_matches.add(m_id)
                                
                                if self.is_match_scraped(m_id, teams=teams):
                                    continue

                                if banned_champions:
                                    champs = self.get_match_champions_api(m_id, api_key, routing_region)
                                    if champs:
                                        is_allowed, banned_found = self.check_banned_champions(champs, banned_champions)
                                        if not is_allowed:
                                            filtered_ids_set.add(m_id)
                                            filtered_matches_log.append({"game_id": m_id, "banned": banned_found})
                                            continue

                                raw_id = re.split(r'[-_]', m_id)[-1]
                                rofl_exists = any(raw_id in f for f in os.listdir(self.replay_dir) if f.endswith(".rofl"))
                                if not rofl_exists:
                                    port, password = self.get_lcu_auth()
                                    dl_url = f"https://127.0.0.1:{port}/lol-replays/v1/rofls/{raw_id}/download"
                                    auth = HTTPBasicAuth("riot", password)
                                    payload = {"componentType": "match-history", "contextData": "match-history"}
                                    requests.packages.urllib3.disable_warnings()
                                    dl_res = requests.post(dl_url, auth=auth, verify=False, json=payload, timeout=5)
                                    if dl_res.status_code in (200, 204):
                                        queued_downloads.add(m_id)
                                        time.sleep(6.0)
                        time.sleep(1.2)
                    except Exception:
                        pass
                page += 1

        downloader_thread = Thread(target=download_worker, daemon=True)
        downloader_thread.start()

        # ── Main Consumer: Replay Scraper Loop ────────────────────────
        try:
            with tqdm(total=target_match_count, desc="Scraped Matches", ncols=100) as pbar:
                while len(scraped_matches) < target_match_count:
                    local_files = [f for f in os.listdir(self.replay_dir) if f.endswith(".rofl")]
                    
                    unscraped_replays = []
                    for f in local_files:
                        m_id = f.replace(".rofl", "")
                        
                        if m_id in filtered_ids_set or m_id.replace("-", "_") in filtered_ids_set:
                            continue
                        
                        # ← NEW: skip matches that already failed max retries
                        if m_id in failed_ids_set:
                            continue
                            
                        if not self.is_match_scraped(m_id, teams=teams):
                            unscraped_replays.append(f)

                    if unscraped_replays:
                        current_rofl = unscraped_replays[0]
                        match_id = current_rofl.replace(".rofl", "")
                        pbar.set_postfix({"active": match_id})

                        pbar.write(f"\n🎬 Starting scrape for {match_id}...")
                        res = self.scrape_match(
                            game_id=match_id,
                            teams=teams,
                            start_sec=start_sec,
                            end_sec=end_sec,
                            speed=speed,
                            skip_existing=True,
                            banned_champions=banned_champions,
                            require_classic_sr=require_classic_sr,
                            api_key=api_key
                        )

                        if res["status"] == "success":
                            scraped_matches.append(match_id)
                            pbar.update(1)
                            pbar.write(f"✅ Finished {match_id} ({len(scraped_matches)}/{target_match_count}) | Frames: {res['frames']}")
                            
                            if delete_rofl_after_scrape:
                                try:
                                    os.remove(os.path.join(self.replay_dir, current_rofl))
                                    pbar.write(f"🗑️ Cleaned up '{current_rofl}' to save disk space.")
                                except Exception:
                                    pass
                        elif res["status"] == "skipped":
                            skipped_matches.append(match_id)
                            pbar.write(f"⏩ Skipped {match_id} (already complete)")
                        elif res["status"] == "filtered":
                            filtered_ids_set.add(match_id)
                            filtered_matches_log.append(res)
                            pbar.write(f"🚫 Filtered {match_id} ({res.get('reason')})")
                        else:
                            # ← NEW: track retry count and permanently skip after MAX_RETRIES
                            match_attempt_count[match_id] = match_attempt_count.get(match_id, 0) + 1
                            attempts = match_attempt_count[match_id]
                            if attempts >= MAX_RETRIES_PER_MATCH:
                                failed_ids_set.add(match_id)
                                if match_id not in failed_matches:
                                    failed_matches.append(match_id)
                                pbar.write(f"❌ Permanently skipping {match_id} after {attempts} failed attempts")
                            else:
                                pbar.write(f"⚠️ Scrape failed for {match_id} (attempt {attempts}/{MAX_RETRIES_PER_MATCH}), will retry...")

                        # Save incremental JSON summary
                        try:
                            with open(summary_path, "w", encoding="utf-8") as sf:
                                json.dump({
                                    "target": target_match_count,
                                    "scraped": scraped_matches,
                                    "skipped": skipped_matches,
                                    "filtered": filtered_matches_log,
                                    "failed": failed_matches
                                }, sf, indent=2)
                        except Exception:
                            pass

                    else:
                        if downloader_thread.is_alive():
                            pbar.set_postfix({"status": "Waiting for replay download..."})
                            time.sleep(5.0)
                        else:
                            pbar.write("⚠️ Downloader thread finished and no further replays are available.")
                            break

        except KeyboardInterrupt:
            print("\n🛑 Pipeline interrupted by user. Shutting down gracefully...")
        finally:
            stop_event.set()
            self.kill_client()

        print("\n" + "=" * 75)
        print("🏁 CONTINUOUS PIPELINE FINISHED")
        print(f"🎯 Target: {target_match_count} | ✅ Successfully Scraped: {len(scraped_matches)}")
        print(f"⏩ Skipped: {len(skipped_matches)} | 🚫 Filtered (Banned Champs/ARAM): {len(filtered_matches_log)}")
        print(f"❌ Failed: {len(failed_matches)}")
        print(f"📄 Summary log: {summary_path}")
        print("=" * 75)

        return {
            "scraped": scraped_matches,
            "skipped": skipped_matches,
            "filtered": filtered_matches_log,
            "failed": failed_matches
        }

    # ─────────────────────────────────────────────────────────────
    # Legacy Compatibility Methods
    # ─────────────────────────────────────────────────────────────
    def get_replay_dir(self):
        return self.replay_dir

    def arg_list(self, replay_path):
        return [
            str(os.path.join(self.game_dir, "League of Legends.exe")),
            replay_path,
            "-SkipRads",
            "-SkipBuild",
            "-EnableLNP",
            "-UseNewX3D=1",
            "-UseNewX3DFramebuffers=1"
        ]

    def run_client_ver1(self, replay_path, gameId, start, end, speed, paused, team, remove_fog_of_war, use_nas=False):
        return self.scrape_pov(
            game_id=gameId,
            team=team,
            start_sec=start,
            end_sec=end,
            speed=speed,
            remove_fog_of_war=remove_fog_of_war
        )

    @staticmethod
    def post_initialize(paused, start, speed):
        return ReplayScraper.post_playback(paused=paused, start_time=start, speed=speed)

    @staticmethod
    def replay_view_initialize():
        return ReplayScraper.post_render_config()

    @staticmethod
    def save_to_png(rgb, size, output):
        mss.tools.to_png(rgb, size, output=output)