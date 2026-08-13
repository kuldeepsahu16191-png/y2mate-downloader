"""
Y2mate Backend Server
Provides YouTube video info and download capabilities using yt-dlp
"""

import os
import json
import tempfile
import subprocess
import re
import signal
import sys
import uuid
import threading
import time
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Add the script's directory to PATH so yt-dlp can locate local ffmpeg.exe/ffprobe.exe
os.environ['PATH'] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ.get('PATH', '')

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), 'y2mate_downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Check for cookies.txt
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
if os.environ.get('YOUTUBE_COOKIES'):
    try:
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(os.environ.get('YOUTUBE_COOKIES'))
        print("Successfully loaded cookies from YOUTUBE_COOKIES environment variable.")
    except Exception as e:
        print(f"Error writing cookies.txt from environment variable: {e}")
elif os.path.exists('/etc/secrets/cookies.txt'):
    COOKIE_FILE = '/etc/secrets/cookies.txt'
    print("Successfully loaded cookies from Render Secret File.")
elif not os.path.exists(COOKIE_FILE):
    parent_cookie = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cookies.txt')
    if os.path.exists(parent_cookie):
        COOKIE_FILE = parent_cookie
    else:
        COOKIE_FILE = None

# Validate cookies file format to prevent yt-dlp crash
if COOKIE_FILE and os.path.exists(COOKIE_FILE):
    is_valid_format = False
    try:
        with open(COOKIE_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            first_line = f.readline().strip()
            # Netscape cookies file should begin with a hash tag comment, usually '# Netscape' or '# HTTP' or similar.
            if first_line.startswith('# Netscape') or first_line.startswith('# HTTP') or first_line.startswith('#'):
                content = f.read()
                if 'youtube.com' in content or 'google.com' in content:
                    is_valid_format = True
                else:
                    print("Cookie file exists but does not contain YouTube or Google cookies.")
    except Exception as e:
        print(f"Error validating cookie file: {e}")
        
    if not is_valid_format:
        print(f"WARNING: Cookie file '{COOKIE_FILE}' is not in Netscape cookies format or has no YouTube/Google cookies. Disabling cookie config to prevent yt-dlp crash/block.")
        COOKIE_FILE = None

# Check if curl-cffi is available for browser impersonation
HAS_CURL_CFFI = False
try:
    import curl_cffi
    HAS_CURL_CFFI = True
except ImportError:
    pass

def build_ytdlp_cmd(args, use_cookies=True):
    """Build the yt-dlp command with necessary bypass options like impersonation and cookies."""
    cmd = ['yt-dlp']
    if HAS_CURL_CFFI:
        cmd.extend(['--impersonate', 'chrome'])
    
    # Always use android_vr as the player client. It bypasses bot detection reliably
    # and returns formats up to 4K, with or without cookies. The default web/tv clients
    # get blocked with "Sign in to confirm you're not a bot" on datacenter IPs.
    cmd.extend(['--extractor-args', 'youtube:player_client=android_vr'])

    # Check if we have cookies
    if use_cookies and COOKIE_FILE and os.path.exists(COOKIE_FILE):
        cmd.extend(['--cookies', COOKIE_FILE])
        
    cmd.extend(args)
    return cmd

# Global dictionary to track background downloads
tasks = {}

def extract_video_id(url):
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def format_duration(seconds):
    if not seconds:
        return "0:00"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def cleanup_old_files():
    """Remove downloaded files older than 30 minutes"""
    import time
    now = time.time()
    for f in os.listdir(DOWNLOAD_DIR):
        filepath = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > 1800:
            try:
                os.remove(filepath)
            except:
                pass

@app.after_request
def add_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/api/info', methods=['GET', 'OPTIONS'])
def get_video_info():
    if request.method == 'OPTIONS':
        return '', 204

    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL or search query is required'}), 400

    video_id = extract_video_id(url)
    if video_id:
        target = f'https://www.youtube.com/watch?v={video_id}'
    else:
        # Treat as search query
        target = f'ytsearch1:{url}'

    try:
        cmd = build_ytdlp_cmd([
            '--dump-json',
            '--no-download',
            '--no-warnings',
            '--no-check-certificates',
            target
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # Fallback if cookies are active but request failed (common with expired/invalid cookies)
        if result.returncode != 0 and COOKIE_FILE:
            print("Video info fetch failed with cookies. Retrying WITHOUT cookies...")
            fallback_cmd = build_ytdlp_cmd([
                '--dump-json',
                '--no-download',
                '--no-warnings',
                '--no-check-certificates',
                target
            ], use_cookies=False)
            result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else 'Failed to fetch video info'
            return jsonify({'error': error_msg}), 500

        info = json.loads(result.stdout)
        
        # If it was a search result, it might be inside a list or have entries
        if 'entries' in info and len(info['entries']) > 0:
            info = info['entries'][0]

        # Parse available formats dynamically
        import shutil
        has_ffmpeg = shutil.which('ffmpeg') is not None
        
        formats_raw = info.get('formats', [])
        video_formats = []
        audio_formats = []
        seen_heights = set()
        
        for f in formats_raw:
            # Check for video formats (yt-dlp will merge video and audio)
            if f.get('vcodec') != 'none':
                width = f.get('width')
                height = f.get('height')
                if width and height:
                    res_val = min(width, height)
                elif height:
                    res_val = height
                else:
                    continue
                
                # Standard resolutions matching logic with 15px crop tolerance
                matched_res = None
                for std_res in [144, 240, 360, 480, 720, 1080, 1440, 2160, 4320]:
                    if abs(res_val - std_res) <= 15:
                        matched_res = std_res
                        break
                
                if matched_res and matched_res not in seen_heights:
                    seen_heights.add(matched_res)
                    size_bytes = f.get('filesize') or f.get('filesize_approx')
                    size_str = "Unknown size"
                    if size_bytes:
                        size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                    else:
                        # Fallback sizing estimate from typical bitrates
                        tbr = f.get('tbr') or (500 if matched_res <= 360 else 1000 if matched_res <= 480 else 2500 if matched_res <= 720 else 4500)
                        duration = info.get('duration', 0)
                        if duration:
                            size_str = f"{(tbr * 1000 * duration) / (8 * 1024 * 1024):.1f} MB"
                            
                    video_formats.append({
                        'quality': f"{matched_res}p",
                        'ext': 'mp4', # standard container we target
                        'size': size_str,
                        'height': matched_res
                    })
            
            # Check for audio streams
            elif f.get('vcodec') == 'none' and f.get('acodec') != 'none':
                abr = f.get('abr')
                if abr:
                    audio_formats.append({
                        'abr': int(abr),
                        'size_bytes': f.get('filesize') or f.get('filesize_approx')
                    })

        # Post-process video formats
        video_formats.sort(key=lambda x: x['height'], reverse=True)
        
        # Post-process audio formats (map to standard bitrates 320, 256, 192, 128, 64)
        unique_audio = []
        duration = info.get('duration', 0)
        
        for std_abr in [320, 256, 192, 128, 64]:
            # Try to estimate file sizes based on matching download stream or duration estimation
            matching_size = None
            for a in audio_formats:
                if abs(a['abr'] - std_abr) <= 30 and a['size_bytes']:
                    matching_size = a['size_bytes']
                    break
            
            size_str = "Unknown size"
            if matching_size:
                size_str = f"{matching_size / (1024 * 1024):.1f} MB"
            elif duration:
                size_str = f"{(std_abr * 1000 * duration) / (8 * 1024 * 1024):.1f} MB"
                
            # MP3 Option (converted/transcoded via ffmpeg)
            unique_audio.append({
                'quality': f"{std_abr}kbps",
                'ext': 'mp3',
                'size': size_str,
                'abr': std_abr
            })
            # M4A/MP4A Option (native AAC stream)
            unique_audio.append({
                'quality': f"{std_abr}kbps",
                'ext': 'm4a',
                'size': size_str,
                'abr': std_abr
            })

        # Fallbacks if list remains empty
        if not video_formats:
            for height in [2160, 1440, 1080, 720, 480, 360]:
                bitrate_map = {2160: 20000, 1440: 10000, 1080: 4500, 720: 2500, 480: 1000, 360: 500}
                size_str = "Unknown size"
                if duration:
                    size_str = f"{(bitrate_map[height] * 1000 * duration) / (8 * 1024 * 1024):.1f} MB"
                video_formats.append({
                    'quality': f"{height}p",
                    'ext': 'mp4',
                    'size': size_str,
                    'height': height
                })

        if not unique_audio:
            for std_abr in [320, 256, 192, 128]:
                size_str = "Unknown size"
                if duration:
                    size_str = f"{(std_abr * 1000 * duration) / (8 * 1024 * 1024):.1f} MB"
                unique_audio.append({
                    'quality': f"{std_abr}kbps",
                    'ext': 'mp3',
                    'size': size_str,
                    'abr': std_abr
                })
                unique_audio.append({
                    'quality': f"{std_abr}kbps",
                    'ext': 'm4a',
                    'size': size_str,
                    'abr': std_abr
                })

        # Check if video is vertical
        is_vertical = False
        if info.get('width') and info.get('height'):
            is_vertical = info.get('height') > info.get('width')

        response = {
            'id': info.get('id'),
            'title': info.get('title', 'Unknown Title'),
            'channel': info.get('channel', info.get('uploader', 'Unknown Channel')),
            'channel_url': info.get('channel_url', info.get('uploader_url')),
            'thumbnail': info.get('thumbnail', f'https://img.youtube.com/vi/{video_id}/maxresdefault.jpg'),
            'duration': info.get('duration', 0),
            'duration_formatted': format_duration(info.get('duration', 0)),
            'view_count': info.get('view_count', 0),
            'like_count': info.get('like_count', 0),
            'upload_date': info.get('upload_date'),
            'description': (info.get('description', '') or '')[:500],
            'video_formats': video_formats,
            'audio_formats': unique_audio,
            'has_ffmpeg': has_ffmpeg,
            'is_vertical': is_vertical
        }

        return jsonify(response)

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out. Please try again.'}), 500
    except json.JSONDecodeError:
        return jsonify({'error': 'Failed to parse video information'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/search', methods=['GET', 'OPTIONS'])
def search_videos():
    if request.method == 'OPTIONS':
        return '', 204

    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'Search query is required'}), 400

    try:
        cmd = build_ytdlp_cmd([
            '--dump-json',
            '--flat-playlist',
            '--no-warnings',
            '--no-check-certificates',
            f'ytsearch9:{q}'
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        # Fallback if cookies are active but request failed (common with expired/invalid cookies)
        if result.returncode != 0 and COOKIE_FILE:
            print("YouTube search failed with cookies. Retrying WITHOUT cookies...")
            fallback_cmd = build_ytdlp_cmd([
                '--dump-json',
                '--flat-playlist',
                '--no-warnings',
                '--no-check-certificates',
                f'ytsearch9:{q}'
            ], use_cookies=False)
            result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else 'Failed to search YouTube'
            return jsonify({'error': error_msg}), 500

        results = []
        lines = result.stdout.strip().split('\n')
        for line in lines:
            if not line:
                continue
            try:
                info = json.loads(line)
                video_id = info.get('id')
                if not video_id:
                    continue

                # Parse thumbnail
                thumbnail_url = ''
                thumbnails = info.get('thumbnails', [])
                if thumbnails:
                    for t in reversed(thumbnails):
                        if t.get('url'):
                            thumbnail_url = t.get('url')
                            break
                if not thumbnail_url:
                    thumbnail_url = f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"

                # Parse duration
                duration = info.get('duration')
                duration_formatted = info.get('duration_string')
                if not duration_formatted and duration is not None:
                    duration_formatted = format_duration(duration)

                # Format view count
                view_count = info.get('view_count', 0)
                view_count_formatted = '0'
                if view_count:
                    try:
                        vc = int(view_count)
                        if vc >= 1000000:
                            view_count_formatted = f"{vc / 1000000:.1f}M"
                        elif vc >= 1000:
                            view_count_formatted = f"{vc / 1000:.1f}K"
                        else:
                            view_count_formatted = str(vc)
                    except:
                        view_count_formatted = str(view_count)

                results.append({
                    'id': video_id,
                    'title': info.get('title', 'Unknown Title'),
                    'channel': info.get('channel', info.get('uploader', 'Unknown Channel')),
                    'duration': duration,
                    'duration_formatted': duration_formatted or '0:00',
                    'thumbnail': thumbnail_url,
                    'view_count': view_count,
                    'view_count_formatted': view_count_formatted
                })
            except Exception as e:
                pass

        return jsonify({'results': results})

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Search request timed out. Please try again.'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def background_download(task_id, video_id, url, quality, download_type, is_vertical, requested_ext):
    try:
        cleanup_old_files()

        safe_quality = re.sub(r'[^a-zA-Z0-9]', '_', quality)
        output_template = os.path.join(DOWNLOAD_DIR, f'{video_id}_{safe_quality}.%(ext)s')

        cmd = build_ytdlp_cmd([
            '--no-warnings',
            '--no-check-certificates',
            '--newline',
            '--concurrent-fragments', '5',
            '--retries', '10',
            '--fragment-retries', '10',
            '-o', output_template,
        ])

        import shutil
        has_ffmpeg = shutil.which('ffmpeg') is not None

        if download_type == 'audio':
            if requested_ext == 'm4a':
                if has_ffmpeg:
                    cmd.extend([
                        '-x',
                        '--audio-format', 'm4a',
                    ])
                cmd.extend(['--format', 'bestaudio[ext=m4a]/bestaudio/best'])
            else:
                if has_ffmpeg:
                    cmd.extend([
                        '-x',
                        '--audio-format', 'mp3',
                    ])
                    if 'kbps' in quality:
                        abr = quality.replace('kbps', '')
                        cmd.extend(['--audio-quality', abr])
                cmd.extend(['--format', 'bestaudio/best'])
        else:
            height_map = {
                '4320p': 4320,
                '2160p': 2160,
                '1440p': 1440,
                '1080p': 1080,
                '720p': 720,
                '480p': 480,
                '360p': 360,
                '240p': 240,
                '144p': 144,
            }
            height = height_map.get(quality, 720)
            
            # Calculate standard 16:9 width for landscape bounding box
            width = int(height * 16 / 9)
            # Add tolerance for standard crops
            if height == 480: width = 854
            elif height == 360: width = 640
            elif height == 240: width = 426
            elif height == 144: width = 256
            elif height == 1440: width = 2560
            elif height == 2160: width = 3840
            elif height == 4320: width = 7680

            if is_vertical:
                # Vertical format logic: match width <= height (std_res) and height <= width (landscape_limit)
                format_str = (
                    f'bestvideo[width<={height}][height<={width}][ext=mp4]+bestaudio[ext=m4a]/'
                    f'bestvideo[width<={height}][height<={width}]+bestaudio/'
                    f'best[width<={height}]/best'
                )
            else:
                # Landscape format logic: match height <= height (std_res) and width <= width (landscape_limit)
                format_str = (
                    f'bestvideo[height<={height}][width<={width}][ext=mp4]+bestaudio[ext=m4a]/'
                    f'bestvideo[height<={height}][width<={width}]+bestaudio/'
                    f'best[height<={height}]/best'
                )

            cmd.extend([
                '--format', format_str,
                '--merge-output-format', 'mp4',
            ])

        cmd.append(f'https://www.youtube.com/watch?v={video_id}')

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        tasks[task_id]["status"] = "downloading"
        
        # Regex to parse percentage, speed, and ETA
        progress_re = re.compile(r'\[download\]\s+(\d+\.\d+|\d+)%\s+of\s+(?:~)?\S+\s+at\s+(\S+)\s+ETA\s+(\S+)')
        percent_re = re.compile(r'\[download\]\s+(\d+\.\d+|\d+)%')

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            
            line_str = line.strip()
            if not line_str:
                continue

            # Update status based on highlights
            if "Merging formats" in line_str or "[Merger]" in line_str:
                tasks[task_id]["status"] = "processing"
                tasks[task_id]["progress"] = 90
            elif "[ExtractAudio]" in line_str or "Destination:" in line_str and download_type == 'audio':
                tasks[task_id]["status"] = "processing"
                tasks[task_id]["progress"] = 90
            
            match = progress_re.search(line_str)
            if match:
                tasks[task_id]["progress"] = float(match.group(1))
                tasks[task_id]["speed"] = match.group(2)
                tasks[task_id]["eta"] = match.group(3)
                if tasks[task_id]["status"] == "pending":
                    tasks[task_id]["status"] = "downloading"
            else:
                pct_match = percent_re.search(line_str)
                if pct_match:
                    tasks[task_id]["progress"] = float(pct_match.group(1))
                    if tasks[task_id]["status"] == "pending":
                        tasks[task_id]["status"] = "downloading"

        try:
            stdout_rem, stderr_rem = proc.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            proc.kill()
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["error"] = "Download timed out (1 hour limit)."
            return

        if proc.returncode != 0:
            # Fallback if cookies are active but request failed (common with expired/invalid cookies)
            if COOKIE_FILE:
                print("Download failed with cookies. Retrying WITHOUT cookies...")
                tasks[task_id]["status"] = "pending"
                tasks[task_id]["progress"] = 0
                
                # Rebuild cmd without cookies
                fallback_cmd = build_ytdlp_cmd([
                    '--no-warnings',
                    '--no-check-certificates',
                    '--newline',
                    '--concurrent-fragments', '5',
                    '--retries', '10',
                    '--fragment-retries', '10',
                    '-o', output_template,
                ], use_cookies=False)
                
                if download_type == 'audio':
                    if requested_ext == 'm4a':
                        if has_ffmpeg:
                            fallback_cmd.extend(['-x', '--audio-format', 'm4a'])
                        fallback_cmd.extend(['--format', 'bestaudio[ext=m4a]/bestaudio/best'])
                    else:
                        if has_ffmpeg:
                            fallback_cmd.extend(['-x', '--audio-format', 'mp3'])
                            if 'kbps' in quality:
                                abr = quality.replace('kbps', '')
                                fallback_cmd.extend(['--audio-quality', abr])
                        fallback_cmd.extend(['--format', 'bestaudio/best'])
                else:
                    fallback_cmd.extend([
                        '--format', format_str,
                        '--merge-output-format', 'mp4',
                    ])
                fallback_cmd.append(f'https://www.youtube.com/watch?v={video_id}')

                proc = subprocess.Popen(
                    fallback_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )
                
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    line_str = line.strip()
                    if not line_str:
                        continue
                    if "Merging formats" in line_str or "[Merger]" in line_str:
                        tasks[task_id]["status"] = "processing"
                        tasks[task_id]["progress"] = 90
                    elif "[ExtractAudio]" in line_str or "Destination:" in line_str and download_type == 'audio':
                        tasks[task_id]["status"] = "processing"
                        tasks[task_id]["progress"] = 90
                    match = progress_re.search(line_str)
                    if match:
                        tasks[task_id]["progress"] = float(match.group(1))
                        tasks[task_id]["speed"] = match.group(2)
                        tasks[task_id]["eta"] = match.group(3)
                    else:
                        pct_match = percent_re.search(line_str)
                        if pct_match:
                            tasks[task_id]["progress"] = float(pct_match.group(1))

                stdout_rem, stderr_rem = proc.communicate(timeout=3600)

            if proc.returncode != 0:
                error_msg = stderr_rem.strip() if stderr_rem else ""
                if not error_msg:
                    try:
                        error_msg = proc.stderr.read().strip() or "Download failed"
                    except:
                        error_msg = "Download failed"
                tasks[task_id]["status"] = "failed"
                tasks[task_id]["error"] = error_msg
                return

        time.sleep(1)

        import glob
        all_files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{video_id}*'))
        valid_extensions = ['.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.ogg', '.wav', '.opus']
        downloaded_files = [
            f for f in all_files
            if os.path.isfile(f) and any(f.lower().endswith(ext) for ext in valid_extensions)
        ]

        if not downloaded_files:
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["error"] = "Downloaded file not found."
            return

        downloaded_file = max(downloaded_files, key=os.path.getctime)
        filename = os.path.basename(downloaded_file)

        if download_type == 'audio':
            title_cmd = build_ytdlp_cmd([
                '--get-title',
                '--no-warnings',
                '--no-check-certificates',
                f'https://www.youtube.com/watch?v={video_id}'
            ])
            try:
                title_result = subprocess.run(title_cmd, capture_output=True, text=True, timeout=30)
                if title_result.returncode != 0 and COOKIE_FILE:
                    fallback_title_cmd = build_ytdlp_cmd([
                        '--get-title',
                        '--no-warnings',
                        '--no-check-certificates',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ], use_cookies=False)
                    title_result = subprocess.run(fallback_title_cmd, capture_output=True, text=True, timeout=30)
                if title_result.returncode == 0 and title_result.stdout.strip():
                    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title_result.stdout.strip())
                    ext = os.path.splitext(downloaded_file)[1]
                    new_filename = f'{safe_title}{ext}'
                    new_path = os.path.join(DOWNLOAD_DIR, new_filename)
                    os.rename(downloaded_file, new_path)
                    downloaded_file = new_path
                    filename = new_filename
            except:
                pass

        tasks[task_id]["status"] = "completed"
        tasks[task_id]["progress"] = 100
        tasks[task_id]["file_path"] = downloaded_file
        tasks[task_id]["filename"] = filename

    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = str(e)


@app.route('/api/download', methods=['POST', 'OPTIONS'])
def download_video():
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'URL is required'}), 400

    url = data['url'].strip()
    quality = data.get('quality', 'best')
    download_type = data.get('type', 'video')
    is_vertical = data.get('is_vertical', False)
    requested_ext = data.get('ext', 'mp3')

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "speed": "",
        "eta": "",
        "error": "",
        "file_path": "",
        "filename": "",
        "download_type": download_type
    }

    # Start download background thread
    thread = threading.Thread(
        target=background_download,
        args=(task_id, video_id, url, quality, download_type, is_vertical, requested_ext)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "pending"})


@app.route('/api/download/status/<task_id>', methods=['GET', 'OPTIONS'])
def download_status(task_id):
    if request.method == 'OPTIONS':
        return '', 204
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)


@app.route('/api/download/file/<task_id>', methods=['GET', 'OPTIONS'])
def download_file(task_id):
    if request.method == 'OPTIONS':
        return '', 204
    task = tasks.get(task_id)
    if not task or task["status"] != "completed":
        return jsonify({'error': 'File not ready or task failed'}), 400
    return send_file(
        task["file_path"],
        as_attachment=True,
        download_name=task["filename"]
    )


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'status': 'ok',
        'message': 'Y2mate API Server is running'
    })


# Global cache for yt-dlp version to avoid running subprocess on every health check
CACHED_YT_VERSION = None

@app.route('/api/health', methods=['GET'])
def health_check():
    global CACHED_YT_VERSION
    import shutil
    has_ffmpeg = shutil.which('ffmpeg') is not None
    
    # Check yt-dlp version (cached)
    if CACHED_YT_VERSION is None:
        try:
            version_cmd = ['yt-dlp', '--version']
            version_res = subprocess.run(version_cmd, capture_output=True, text=True, timeout=10)
            if version_res.returncode == 0:
                CACHED_YT_VERSION = version_res.stdout.strip()
            else:
                CACHED_YT_VERSION = "unknown"
        except Exception as e:
            CACHED_YT_VERSION = f"error: {str(e)}"

    return jsonify({
        'status': 'ok',
        'service': 'y2mate-api',
        'has_ffmpeg': has_ffmpeg,
        'has_curl_cffi': HAS_CURL_CFFI,
        'yt_dlp_version': CACHED_YT_VERSION,
        'cookie_file': COOKIE_FILE,
        'cookie_file_exists': COOKIE_FILE is not None and os.path.exists(COOKIE_FILE)
    })



def signal_handler(sig, frame):
    print("\nShutting down Y2mate API server...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == '__main__':
    print("Starting Y2mate API server...")
    port = int(os.environ.get('PORT', 5000))
    print(f"API available on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
