from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json, urllib.request, os, re
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
RENDER_URL = os.environ.get('SELF_URL', os.environ.get('RENDER_EXTERNAL_URL', ''))
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

    # 이미 학습된 영상 스킵
    if video_id in learned_videos:
        return jsonify({"success": False, "error": "이미 학습됨", "videoId": video_id, "skip": True})

    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

        # 한국어 우선, 없으면 영어, 없으면 자동생성
        transcript = None
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['ko'])
        except:
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])
            except:
                try:
                    # 자동생성 자막 포함 전체 시도
                    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                    t = transcript_list.find_generated_transcript(['ko', 'en'])
                    transcript = t.fetch()
                except:
                    pass

        if transcript:
            lines = [item['text'].strip() for item in transcript if item['text'].strip()]
            # 중복 제거
            deduped = []
            prev = ''
            for line in lines:
                if line != prev:
                    deduped.append(line)
                    prev = line
            text = '\n'.join(deduped)
            if text.strip():
                learned_videos.add(video_id)
                save_learned()
                return jsonify({"success": True, "text": text, "videoId": video_id, "method": "transcript"})

        return jsonify({"success": False, "error": "자막 없음", "videoId": video_id})

    except Exception as e:
        print(f'[자막] 오류: {e}')
        return jsonify({"success": False, "error": str(e), "videoId": video_id})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port)
