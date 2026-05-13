from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import subprocess, tempfile, shutil, os, re, json, urllib.request
import threading, time

app = Flask(__name__, static_folder='.')
CORS(app)

# ── Whisper 모델 (서버 시작시 한번만 로드) ──────────
whisper_model = None
def load_whisper():
    global whisper_model
    try:
        from faster_whisper import WhisperModel
        print('[Whisper] tiny 모델 로딩 중...')
        whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        print('[Whisper] 로딩 완료')
    except Exception as e:
        print(f'[Whisper] 로딩 실패: {e}')
threading.Thread(target=load_whisper, daemon=True).start()

# ── 셀프 핑 ──────────────────────────────────────────
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', '')
def self_ping():
    if not RENDER_URL:
        return
    url = RENDER_URL.rstrip('/') + '/ping'
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=10)
            print('[핑] OK')
        except Exception as e:
            print(f'[핑] 실패: {e}')
threading.Thread(target=self_ping, daemon=True).start()

# ── 학습 완료 기록 (중복 방지) ───────────────────────
# {videoId: True} 형태로 메모리에 유지
learned_videos = set()
LEARNED_FILE = 'learned_videos.json'

def load_learned():
    global learned_videos
    if os.path.exists(LEARNED_FILE):
        with open(LEARNED_FILE, 'r') as f:
            learned_videos = set(json.load(f))
        print(f'[학습기록] {len(learned_videos)}개 로드됨')

def save_learned():
    with open(LEARNED_FILE, 'w') as f:
        json.dump(list(learned_videos), f)

load_learned()

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/ping')
def ping():
    return jsonify({"status": "ok"})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "whisper": whisper_model is not None, "learned_count": len(learned_videos)})

@app.route('/videos')
def videos():
    channel_id = request.args.get('channelId', '')
    api_key    = request.args.get('apiKey', '')
    if not channel_id or not api_key:
        return jsonify({"error": "channelId, apiKey 필요"}), 400

    # 1단계: 영상 목록
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?key={api_key}&channelId={channel_id}"
        f"&part=snippet,id&order=date&maxResults=50&type=video"
    )
    try:
        with urllib.request.urlopen(search_url, timeout=15) as res:
            data = json.loads(res.read())
        if "error" in data:
            return jsonify({"error": data["error"]["message"]}), 400

        items = data.get("items", [])
        video_ids = [item["id"]["videoId"] for item in items]

        # 2단계: duration 가져오기 (쇼츠 판별용)
        duration_map = {}
        if video_ids:
            ids_str = ','.join(video_ids)
            detail_url = (
                f"https://www.googleapis.com/youtube/v3/videos"
                f"?key={api_key}&id={ids_str}&part=contentDetails,snippet"
            )
            try:
                with urllib.request.urlopen(detail_url, timeout=15) as res2:
                    detail_data = json.loads(res2.read())
                for vi in detail_data.get("items", []):
                    vid = vi["id"]
                    dur_str = vi["contentDetails"]["duration"]  # ex: PT1M30S
                    # ISO 8601 파싱
                    import re as re2
                    m = re2.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur_str)
                    if m:
                        h = int(m.group(1) or 0)
                        mn = int(m.group(2) or 0)
                        s = int(m.group(3) or 0)
                        duration_map[vid] = h*3600 + mn*60 + s
            except Exception as e:
                print(f'[duration] 가져오기 실패: {e}')

        # 3단계: 타입 판별
        def classify(item, title):
            vid = item["id"]["videoId"]
            live = item["snippet"].get("liveBroadcastContent", "none")
            if live in ("live", "completed"):
                return "live"
            dur = duration_map.get(vid, 999)
            if dur <= 60 or "#shorts" in title.lower() or "#쇼츠" in title.lower():
                return "shorts"
            return "video"

        result = []
        for item in items:
            title = item["snippet"]["title"]
            result.append({
                "videoId": item["id"]["videoId"],
                "title": title,
                "publishedAt": item["snippet"]["publishedAt"],
                "liveBroadcastContent": item["snippet"].get("liveBroadcastContent", "none"),
                "duration": duration_map.get(item["id"]["videoId"], 0),
                "type": classify(item, title),
                "already_learned": item["id"]["videoId"] in learned_videos
            })

        return jsonify({"videos": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/subtitle')
def subtitle():
    video_id = request.args.get('videoId', '')
    if not video_id:
        return jsonify({"error": "videoId 필요"}), 400

    # 이미 학습된 영상이면 스킵
    if video_id in learned_videos:
        return jsonify({"success": False, "error": "이미 학습됨", "videoId": video_id, "skip": True})

    tmpdir = tempfile.mkdtemp()
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"

        # 1단계: 자막 시도
        cmd = [
            "yt-dlp", "--skip-download",
            "--write-auto-sub", "--write-sub",
            "--sub-lang", "ko,en",
            "--sub-format", "vtt",
            "--output", os.path.join(tmpdir, "%(id)s"),
            url
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        vtt_files = [f for f in os.listdir(tmpdir) if f.endswith('.vtt')]

        if vtt_files:
            # 자막 있음 → vtt 파싱
            with open(os.path.join(tmpdir, vtt_files[0]), encoding="utf-8") as f:
                raw = f.read()
            lines = []
            prev = ''
            for line in raw.splitlines():
                line = line.strip()
                if not line: continue
                if line.startswith(('WEBVTT','Kind:','Language:','X-')): continue
                if re.match(r'^\d{2}:\d{2}', line) or '-->' in line: continue
                line = re.sub(r'<[^>]+>', '', line).strip()
                if line and line != prev:
                    lines.append(line)
                    prev = line
            text = '\n'.join(lines)
            if text.strip():
                learned_videos.add(video_id)
                save_learned()
                return jsonify({"success": True, "text": text, "videoId": video_id, "method": "subtitle"})

        # 2단계: 자막 없음 → 음성 다운로드 후 Whisper
        if whisper_model is None:
            return jsonify({"success": False, "error": "Whisper 모델 로딩 중, 잠시 후 재시도", "videoId": video_id})

        print(f'[Whisper] 음성 다운로드 시작: {video_id}')
        audio_path = os.path.join(tmpdir, f'{video_id}.mp3')
        dl_cmd = [
            "yt-dlp",
            "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "9",  # 최저 품질 (파일 작게)
            "--output", audio_path,
            url
        ]
        dl_result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=120)

        # 실제 저장된 파일 찾기
        mp3_files = [f for f in os.listdir(tmpdir) if f.endswith('.mp3')]
        if not mp3_files:
            return jsonify({"success": False, "error": "음성 다운로드 실패", "videoId": video_id})

        audio_file = os.path.join(tmpdir, mp3_files[0])
        print(f'[Whisper] 변환 시작: {audio_file}')

        segments, info = whisper_model.transcribe(audio_file, language="ko", beam_size=1)
        lines = [seg.text.strip() for seg in segments if seg.text.strip()]
        text = '\n'.join(lines)

        # 음성 파일 즉시 삭제
        os.remove(audio_file)
        print(f'[Whisper] 완료: {len(lines)}줄, 음성파일 삭제됨')

        if not text.strip():
            return jsonify({"success": False, "error": "음성에서 텍스트 추출 실패", "videoId": video_id})

        learned_videos.add(video_id)
        save_learned()
        return jsonify({"success": True, "text": text, "videoId": video_id, "method": "whisper"})

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "시간 초과", "videoId": video_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "videoId": video_id})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port)
