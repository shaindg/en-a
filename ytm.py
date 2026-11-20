#!/usr/bin/env python3
import curses
import json
import os
import socket
import subprocess
import threading
import time

IPC_SOCKET = "/tmp/ytm_mpv.sock"
CONFIG_DIR = os.path.expanduser("~/.config/ytm")
FAV_FILE = os.path.join(CONFIG_DIR, "favorites.json")
MUSIC_DIR = os.path.expanduser("~/Music")


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(MUSIC_DIR, exist_ok=True)


def load_favorites():
    ensure_dirs()
    if not os.path.exists(FAV_FILE):
        return []
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_favorites(favs):
    ensure_dirs()
    with open(FAV_FILE, "w", encoding="utf-8") as f:
        json.dump(favs, f, ensure_ascii=False, indent=2)


def format_time(seconds):
    if seconds is None or seconds < 0:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


class Player:
    def __init__(self):
        self.process = None
        self.current_title = "Hazır"
        self.paused = False
        self.volume = 100

    def play(self, url, title):
        self.stop()
        self.current_title = title
        self.paused = False
        if os.path.exists(IPC_SOCKET):
            os.remove(IPC_SOCKET)

        cmd = [
            "mpv",
            "--no-video",
            "--really-quiet",
            f"--volume={self.volume}",
            f"--input-ipc-server={IPC_SOCKET}",
            url,
        ]
        self.process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def send_cmd(self, command):
        if not os.path.exists(IPC_SOCKET):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.15)
                client.connect(IPC_SOCKET)
                client.sendall((json.dumps({"command": command}) + "\n").encode())
                data = client.recv(1024).decode()
                return json.loads(data)
        except Exception:
            return None

    def get_property(self, prop):
        res = self.send_cmd(["get_property", prop])
        if res and res.get("error") == "success":
            return res.get("data")
        return None

    def get_progress(self):
        if not self.is_playing():
            return 0.0, 0.0
        pos = self.get_property("time-pos") or 0.0
        dur = self.get_property("duration") or 0.0
        return float(pos), float(dur)

    def toggle_pause(self):
        if self.is_playing():
            self.paused = not self.paused
            self.send_cmd(["set_property", "pause", self.paused])

    def change_volume(self, delta):
        self.volume = max(0, min(130, self.volume + delta))
        if self.is_playing():
            self.send_cmd(["set_property", "volume", self.volume])

    def seek(self, seconds):
        if self.is_playing():
            self.send_cmd(["seek", seconds, "relative"])

    def is_playing(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        if os.path.exists(IPC_SOCKET):
            os.remove(IPC_SOCKET)
        self.current_title = "Durduruldu"
        self.paused = False


def search_youtube(query, limit=15):
    cmd = [
        "yt-dlp",
        f"ytsearch{limit}:{query}",
        "--dump-json",
        "--flat-playlist",
        "--no-warnings",
        "--quiet",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    results = []
    for line in res.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            duration = data.get("duration")
            dur_str = format_time(duration) if duration else "--:--"
            results.append(
                {
                    "id": data.get("id"),
                    "title": data.get("title", "Bilinmeyen"),
                    "url": f"https://www.youtube.com/watch?v={data.get('id')}",
                    "duration": dur_str,
                    "channel": data.get("uploader", "Bilinmeyen Kanal"),
                }
            )
        except json.JSONDecodeError:
            continue
    return results


def download_mp3(track, callback):
    ensure_dirs()
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--embed-thumbnail",
        "--add-metadata",
        "-o",
        os.path.join(MUSIC_DIR, "%(title)s.%(ext)s"),
        "--quiet",
        track["url"],
    ]
    res = subprocess.run(cmd)
    if res.returncode == 0:
        callback(f"İndirildi: {track['title'][:35]} -> ~/Music")
    else:
        callback(f"İndirme hatası: {track['title'][:30]}")


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()  # Terminal arka planını kullan

    # Renk çiftleri (-1 = varsayılan şeffaf/terminal arka planı)
    curses.init_pair(1, -1, -1)  # Normal metin
    curses.init_pair(2, curses.COLOR_WHITE, -1)  # Vurgulu metin
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)  # Seçili satır

    stdscr.bkgd(" ", curses.color_pair(1))
    stdscr.nodelay(True)
    stdscr.timeout(120)

    player = Player()
    favorites = load_favorites()
    results = list(favorites)
    view_mode = "FAVORİLER" if favorites else "ARAMA"
    selected = 0
    playing_idx = -1
    repeat_mode = "LİSTE"  # KAPALI, TEK, LİSTE
    status_msg = (
        f"Favoriler yüklendi ({len(favorites)} parça). Yeni arama için '/' bas."
        if favorites
        else "Arama yapmak için '/' tuşuna bas."
    )
    searching = False

    def set_status(msg):
        nonlocal status_msg
        status_msg = msg

    def do_search(q):
        nonlocal results, selected, playing_idx, status_msg, searching, view_mode
        searching = True
        status_msg = f"Aranıyor: {q} ..."
        found = search_youtube(q)
        results = found
        view_mode = f"ARAMA: {q}"
        selected = 0
        playing_idx = -1
        status_msg = (
            f"'{q}' için {len(results)} sonuç bulundu."
            if results
            else "Sonuç bulunamadı."
        )
        searching = False

    try:
        while True:
            # Şarkı bittiyse repeat moduna göre aksiyon al
            if (
                playing_idx != -1
                and not player.is_playing()
                and not player.paused
                and results
            ):
                if repeat_mode == "TEK":
                    track = results[playing_idx]
                    player.play(track["url"], track["title"])
                elif repeat_mode == "LİSTE":
                    playing_idx = (playing_idx + 1) % len(results)
                    selected = playing_idx
                    track = results[playing_idx]
                    player.play(track["url"], track["title"])
                else:
                    if playing_idx + 1 < len(results):
                        playing_idx += 1
                        selected = playing_idx
                        track = results[playing_idx]
                        player.play(track["url"], track["title"])
                    else:
                        playing_idx = -1
                        player.current_title = "Liste tamamlandı"

            stdscr.erase()
            h, w = stdscr.getmaxyx()

            # 1. Üst Bilgi Çubuğu (Çerçeveli, terminal arka planlı)
            top_bar = f" / {view_mode} | Tekrar: [{repeat_mode}] | Ses: %{player.volume} "
            stdscr.attron(curses.A_BOLD)
            stdscr.addnstr(0, 1, top_bar, w - 2)
            stdscr.attroff(curses.A_BOLD)
            stdscr.addnstr(1, 1, "─" * max(0, w - 2), w - 2)

            # 2. Durum / Bildirim Satırı
            stdscr.addnstr(2, 1, f"> {status_msg}", w - 2)

            # 3. Şarkı Listesi
            list_start = 4
            list_height = max(1, h - 8)
            offset = max(0, selected - list_height + 1)
            fav_urls = {f["url"] for f in favorites}

            for idx in range(offset, min(len(results), offset + list_height)):
                row = list_start + (idx - offset)
                item = results[idx]
                is_fav = "★ " if item["url"] in fav_urls else "  "
                prefix = "▶ " if idx == playing_idx else is_fav
                channel = item.get("channel", "")[:18]
                line = f"{prefix}{idx + 1:02d}. [{item['duration']}] {item['title']}  ({channel})"

                if idx == selected:
                    stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
                    stdscr.addnstr(row, 1, line.ljust(w - 2), w - 2)
                    stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)
                elif idx == playing_idx:
                    stdscr.attron(curses.A_BOLD)
                    stdscr.addnstr(row, 1, line, w - 2)
                    stdscr.attroff(curses.A_BOLD)
                else:
                    stdscr.addnstr(row, 1, line, w - 2)

            # 4. Alt Oynatıcı ve İlerleme Çubuğu
            stdscr.addnstr(h - 4, 1, "─" * max(0, w - 2), w - 2)

            state = (
                "DURAKLATILDI"
                if player.paused
                else ("ÇALIYOR" if player.is_playing() else "BOŞTA")
            )
            now_playing = f"[{state}] {player.current_title}"
            stdscr.attron(curses.A_BOLD)
            stdscr.addnstr(h - 3, 1, now_playing, w - 2)
            stdscr.attroff(curses.A_BOLD)

            # Süre çubuğu hesaplama
            pos, dur = player.get_progress()
            time_left = format_time(pos)
            time_right = format_time(dur)
            bar_width = max(10, w - 18)
            ratio = min(1.0, pos / dur) if dur > 0 else 0.0
            filled = int(bar_width * ratio)
            bar = "━" * filled + "─" * (bar_width - filled)
            progress_line = f"{time_left} {bar} {time_right}"
            stdscr.addnstr(h - 2, 1, progress_line, w - 2)

            # 5. Kısayol Rehberi (En alt satır)
            keys_help = "[/]Ara [Enter]Çal [Space]Duraklat [f]Fav Ekle [F]Favoriler [d]Sil [D]MP3 İndir [r]Tekrar [q]Çık"
            stdscr.attron(curses.A_DIM)
            stdscr.addnstr(h - 1, 1, keys_help, w - 2)
            stdscr.attroff(curses.A_DIM)

            stdscr.refresh()
            key = stdscr.getch()

            if key == -1:
                continue
            elif key in (ord("q"), ord("Q")):
                break
            elif key == ord("/") and not searching:
                curses.echo()
                curses.curs_set(1)
                stdscr.nodelay(False)
                stdscr.move(2, 1)
                stdscr.clrtoeol()
                stdscr.addstr(2, 1, "> Ara: ")
                query_bytes = stdscr.getstr(2, 8, 60)
                curses.noecho()
                curses.curs_set(0)
                stdscr.nodelay(True)
                stdscr.timeout(120)
                query = query_bytes.decode("utf-8", errors="ignore").strip()
                if query:
                    threading.Thread(
                        target=do_search, args=(query,), daemon=True
                    ).start()
            elif key in (curses.KEY_DOWN, ord("j")) and results:
                selected = (selected + 1) % len(results)
            elif key in (curses.KEY_UP, ord("k")) and results:
                selected = (selected - 1) % len(results)
            elif key in (10, 13, curses.KEY_ENTER) and results:
                playing_idx = selected
                track = results[playing_idx]
                player.play(track["url"], track["title"])
            elif key == ord(" "):
                player.toggle_pause()
            elif key in (ord("+"), ord("=")):
                player.change_volume(5)
            elif key == ord("-"):
                player.change_volume(-5)
            elif key == curses.KEY_RIGHT:
                player.seek(10)
            elif key == curses.KEY_LEFT:
                player.seek(-10)
            elif key == ord("n") and results and playing_idx != -1:
                playing_idx = (playing_idx + 1) % len(results)
                selected = playing_idx
                track = results[playing_idx]
                player.play(track["url"], track["title"])
            elif key == ord("r"):
                modes = ["KAPALI", "TEK", "LİSTE"]
                repeat_mode = modes[(modes.index(repeat_mode) + 1) % len(modes)]
                set_status(f"Tekrar modu: {repeat_mode}")
            elif key == ord("f") and results:
                track = results[selected]
                if not any(f["url"] == track["url"] for f in favorites):
                    favorites.append(track)
                    save_favorites(favorites)
                    set_status(f"Favorilere eklendi: {track['title'][:40]}")
                else:
                    set_status("Bu parça zaten favorilerde.")
            elif key == ord("F"):
                favorites = load_favorites()
                results = list(favorites)
                view_mode = "FAVORİLER"
                selected = 0
                playing_idx = -1
                set_status(f"Favoriler açıldı ({len(favorites)} parça).")
            elif key == ord("d") and results and view_mode == "FAVORİLER":
                removed = results.pop(selected)
                favorites = [f for f in favorites if f["url"] != removed["url"]]
                save_favorites(favorites)
                selected = max(0, min(selected, len(results) - 1))
                set_status(f"Favorilerden silindi: {removed['title'][:40]}")
            elif key == ord("D") and results:
                track = results[selected]
                set_status(f"Arka planda MP3 indiriliyor: {track['title'][:35]}...")
                threading.Thread(
                    target=download_mp3, args=(track, set_status), daemon=True
                ).start()

    finally:
        player.stop()


if __name__ == "__main__":
    curses.wrapper(main)