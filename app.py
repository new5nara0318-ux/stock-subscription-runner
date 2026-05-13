#!/usr/bin/env python3
"""
핑 자동 모니터링 웹 서비스 (UptimeRobot 스타일)
- 사용자 인증
- 구독형 서비스
- 다중 알림 (이메일, Slack, Telegram)
- 상태 페이지
- API
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import subprocess
import threading
import time
from datetime import datetime
import json
import os
import sqlite3
import hashlib
import requests
import uuid
from typing import List, Dict
from functools import wraps

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this-in-production'

# Flask-Login 설정
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# 데이터베이스 설정
DB_PATH = 'ping_monitor.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monitors (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            host TEXT NOT NULL,
            name TEXT,
            interval INTEGER DEFAULT 60,
            active BOOLEAN DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ping_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id TEXT,
            status TEXT,
            time_ms TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            config TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS status_pages (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            name TEXT,
            slug TEXT UNIQUE,
            is_public BOOLEAN DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

# 구독 플랜 설정
PLANS = {
    'free': {'monitors': 50, 'interval': 300, 'status_pages': 1, 'price': 0},
    'solo': {'monitors': 10, 'interval': 60, 'status_pages': 5, 'price': 7},
    'team': {'monitors': 50, 'interval': 60, 'status_pages': 10, 'price': 15},
    'enterprise': {'monitors': -1, 'interval': 30, 'status_pages': -1, 'price': 49}
}

# User 모델
class User(UserMixin):
    def __init__(self, id, email, plan):
        self.id = id
        self.email = email
        self.plan = plan

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    user = conn.execute("SELECT id, email, plan FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if user:
        return User(user[0], user[1], user[2])
    return None

# 데이터 저장소
class PingMonitor:
    def __init__(self):
        self.monitors: Dict[str, Dict] = {}
        self.results: List[Dict] = []
        self.lock = threading.Lock()
        
    def add_monitor(self, host: str, interval: int = 60, name: str = "", user_id: int = None) -> str:
        monitor_id = str(uuid.uuid4())
        with self.lock:
            self.monitors[monitor_id] = {
                'id': monitor_id,
                'host': host,
                'interval': interval,
                'name': name or host,
                'active': True,
                'user_id': user_id,
                'created_at': datetime.now().isoformat()
            }
            # DB에 저장
            if user_id:
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO monitors (id, user_id, host, name, interval) VALUES (?, ?, ?, ?, ?)",
                    (monitor_id, user_id, host, name or host, interval)
                )
                conn.commit()
                conn.close()
            # 백그라운드 스레드 시작
            thread = threading.Thread(target=self._ping_loop, args=(monitor_id,))
            thread.daemon = True
            thread.start()
        return monitor_id
        
    def remove_monitor(self, monitor_id: str):
        with self.lock:
            if monitor_id in self.monitors:
                self.monitors[monitor_id]['active'] = False
        # DB에서 삭제
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        conn.commit()
        conn.close()
                
    def _ping_loop(self, monitor_id: str):
        while True:
            with self.lock:
                monitor = self.monitors.get(monitor_id)
                if not monitor or not monitor['active']:
                    break
                host = monitor['host']
                interval = monitor['interval']
                user_id = monitor.get('user_id')
                
            # 핑 전송
            result = self._send_ping(host)
            result['monitor_id'] = monitor_id
            result['monitor_name'] = monitor['name']
            result['timestamp'] = datetime.now().isoformat()
            
            with self.lock:
                self.results.append(result)
                # 최근 1000개만 유지
                if len(self.results) > 1000:
                    self.results = self.results[-1000:]
            
            # DB에 결과 저장
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO ping_results (monitor_id, status, time_ms) VALUES (?, ?, ?)",
                (monitor_id, result['status'], result['time_ms'])
            )
            conn.commit()
            conn.close()
            
            # 알림 전송 (실패 시)
            if result['status'] != 'success' and user_id:
                self._send_notification(user_id, monitor_id, result)
                    
            time.sleep(interval)
            
    def _send_ping(self, host: str) -> Dict:
        try:
            result = subprocess.run(
                ['ping', '-n', '1', host],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                # 응답 시간 추출
                output = result.stdout
                time_ms = "N/A"
                if "time=" in output or "시간=" in output:
                    for line in output.split('\n'):
                        if "time=" in line or "시간=" in line:
                            # 시간 추출
                            import re
                            match = re.search(r'time[=<](\d+)', line)
                            if match:
                                time_ms = match.group(1) + "ms"
                            break
                            
                return {
                    'status': 'success',
                    'host': host,
                    'time_ms': time_ms,
                    'output': output[:200]
                }
            else:
                return {
                    'status': 'failed',
                    'host': host,
                    'time_ms': 'N/A',
                    'output': 'No response'
                }
        except subprocess.TimeoutExpired:
            return {
                'status': 'timeout',
                'host': host,
                'time_ms': 'N/A',
                'output': 'Timeout'
            }
        except Exception as e:
            return {
                'status': 'error',
                'host': host,
                'time_ms': 'N/A',
                'output': str(e)
            }
            
    def get_monitors(self) -> List[Dict]:
        with self.lock:
            return list(self.monitors.values())
            
    def get_results(self, monitor_id: str = None, limit: int = 100) -> List[Dict]:
        with self.lock:
            if monitor_id:
                filtered = [r for r in self.results if r.get('monitor_id') == monitor_id]
                return filtered[-limit:]
            return self.results[-limit:]
            
    def get_stats(self, monitor_id: str = None) -> Dict:
        with self.lock:
            results = self.results
            if monitor_id:
                results = [r for r in results if r.get('monitor_id') == monitor_id]
                
            total = len(results)
            success = len([r for r in results if r['status'] == 'success'])
            failed = total - success
            
            uptime = (success / total * 100) if total > 0 else 0
            
            return {
                'total': total,
                'success': success,
                'failed': failed,
                'uptime': f"{uptime:.1f}%"
            }
    
    def _send_notification(self, user_id: int, monitor_id: str, result: Dict):
        """알림 전송 (이메일, Slack, Telegram)"""
        conn = sqlite3.connect(DB_PATH)
        notifs = conn.execute("SELECT type, config FROM notifications WHERE user_id=?", (user_id,)).fetchall()
        conn.close()
        
        monitor = self.monitors.get(monitor_id, {})
        message = f"❌ {monitor.get('name', monitor.get('host'))} 다운!\n상태: {result['status']}\n시간: {result['timestamp']}"
        
        for notif_type, config in notifs:
            try:
                config_data = json.loads(config)
                if notif_type == 'email':
                    # 이메일 전송 (SMTP 설정 필요)
                    pass
                elif notif_type == 'slack':
                    requests.post(config_data['webhook'], json={'text': message})
                elif notif_type == 'telegram':
                    requests.post(
                        f"https://api.telegram.org/bot{config_data['token']}/sendMessage",
                        json={'chat_id': config_data['chat_id'], 'text': message}
                    )
            except:
                pass

monitor = PingMonitor()

# 인증 관련 라우트
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        conn = sqlite3.connect(DB_PATH)
        user = conn.execute("SELECT id, email, password, plan FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        
        if user and user[2] == hashlib.sha256(password.encode()).hexdigest():
            login_user(User(user[0], user[1], user[3]))
            return redirect(url_for('dashboard'))
        
        return 'Invalid credentials', 401
    
    return '''
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
        <div class="bg-gray-800 p-8 rounded-lg w-96">
            <h1 class="text-2xl font-bold mb-6 text-center">로그인</h1>
            <form method="POST">
                <input type="email" name="email" placeholder="이메일" class="w-full bg-gray-700 rounded px-4 py-2 mb-4 text-white" required>
                <input type="password" name="password" placeholder="비밀번호" class="w-full bg-gray-700 rounded px-4 py-2 mb-4 text-white" required>
                <button type="submit" class="w-full bg-green-600 hover:bg-green-700 py-2 rounded font-semibold">로그인</button>
            </form>
            <p class="mt-4 text-center text-gray-400">
                계정이 없으신가요? <a href="/register" class="text-blue-400">회원가입</a>
            </p>
        </div>
    </body>
    </html>
    '''

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        hashed_pw = hashlib.sha256(password.encode()).hexdigest()
        
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed_pw))
            conn.commit()
            conn.close()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            return 'Email already exists', 400
    
    return '''
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
        <div class="bg-gray-800 p-4 rounded-lg w-80">
            <h1 class="text-lg font-bold mb-3 text-center">회원가입</h1>
            <form method="POST" onsubmit="return validateForm()">
                <input type="email" name="email" id="email" placeholder="이메일" class="w-full bg-gray-700 rounded px-3 py-2 mb-2 text-white text-sm" required>
                <input type="password" name="password" id="password" placeholder="비밀번호" class="w-full bg-gray-700 rounded px-3 py-2 mb-3 text-white text-sm" required>
                <button type="submit" id="submitBtn" class="w-full bg-green-600 hover:bg-green-700 py-2 rounded text-sm font-semibold">회원가입</button>
            </form>
            <p class="mt-2 text-center text-gray-400 text-xs">
                이미 계정이 있으신가요? <a href="/login" class="text-blue-400">로그인</a>
            </p>
            <p id="error" class="mt-2 text-center text-red-400 text-xs hidden"></p>
        </div>
        <script>
            function validateForm() {
                const email = document.getElementById('email').value;
                const password = document.getElementById('password').value;
                const btn = document.getElementById('submitBtn');
                const error = document.getElementById('error');
                
                if (!email || !password) {
                    error.textContent = '이메일과 비밀번호를 입력해주세요.';
                    error.classList.remove('hidden');
                    return false;
                }
                
                btn.textContent = '처리 중...';
                btn.disabled = true;
                return true;
            }
        </script>
    </body>
    </html>
    '''

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# 대시보드 템플릿
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>핑 모니터링 대시보드</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <nav class="bg-gray-800 border-b border-gray-700">
        <div class="container mx-auto px-4 py-4 flex justify-between items-center">
            <h1 class="text-xl font-bold">🔍 PingMonitor Pro</h1>
            <div class="flex items-center gap-4">
                <span class="text-gray-400">{{ email }}</span>
                <span class="bg-blue-600 px-3 py-1 rounded text-sm">{{ plan }}</span>
                <a href="/logout" class="text-red-400 hover:text-red-300">로그아웃</a>
            </div>
        </div>
    </nav>
    
    <div class="container mx-auto px-4 py-8">
        <!-- 플랜 정보 -->
        <div class="bg-gray-800 rounded-lg p-6 mb-8">
            <div class="flex justify-between items-center">
                <div>
                    <h2 class="text-xl font-semibold">현재 플랜: {{ plan }}</h2>
                    <p class="text-gray-400">모니터: {{ used_monitors }}/{{ max_monitors }} | 상태 페이지: {{ used_status_pages }}/{{ max_status_pages }}</p>
                </div>
                <a href="/pricing" class="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded">플랜 업그레이드</a>
            </div>
        </div>
        
        <!-- 모니터 추가 -->
        <div class="bg-gray-800 rounded-lg p-6 mb-8">
            <h2 class="text-xl font-semibold mb-4">새 모니터 추가</h2>
            <div class="flex gap-4">
                <input type="text" id="host" placeholder="호스트 (예: 8.8.8.8)" 
                    class="flex-1 bg-gray-700 rounded px-4 py-2 text-white">
                <input type="text" id="name" placeholder="이름 (예: Google DNS)" 
                    class="flex-1 bg-gray-700 rounded px-4 py-2 text-white">
                <input type="number" id="interval" placeholder="간격(초)" value="60"
                    class="w-24 bg-gray-700 rounded px-4 py-2 text-white">
                <button onclick="addMonitor()" 
                    class="bg-green-600 hover:bg-green-700 px-6 py-2 rounded font-semibold">
                    추가
                </button>
            </div>
        </div>
        
        <!-- 통계 -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
            <div class="bg-gray-800 rounded-lg p-6">
                <h3 class="text-gray-400 mb-2">전체 요청</h3>
                <p id="total" class="text-3xl font-bold">0</p>
            </div>
            <div class="bg-gray-800 rounded-lg p-6">
                <h3 class="text-gray-400 mb-2">성공</h3>
                <p id="success" class="text-3xl font-bold text-green-500">0</p>
            </div>
            <div class="bg-gray-800 rounded-lg p-6">
                <h3 class="text-gray-400 mb-2">가동률</h3>
                <p id="uptime" class="text-3xl font-bold text-blue-500">0%</p>
            </div>
        </div>
        
        <!-- 모니터 목록 -->
        <div class="bg-gray-800 rounded-lg p-6 mb-8">
            <h2 class="text-xl font-semibold mb-4">모니터 목록</h2>
            <div id="monitorList" class="space-y-2"></div>
        </div>
        
        <!-- 결과 -->
        <div class="bg-gray-800 rounded-lg p-6">
            <h2 class="text-xl font-semibold mb-4">최근 결과</h2>
            <div id="results" class="space-y-2 max-h-96 overflow-y-auto"></div>
        </div>
    </div>
    
    <script>
        function addMonitor() {
            const host = document.getElementById('host').value;
            const name = document.getElementById('name').value;
            const interval = document.getElementById('interval').value;
            
            fetch('/api/monitors', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({host, name, interval: parseInt(interval)})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('host').value = '';
                    document.getElementById('name').value = '';
                    loadMonitors();
                } else {
                    alert(data.error || '실패');
                }
            });
        }
        
        function removeMonitor(id) {
            if (confirm('정말 삭제하시겠습니까?')) {
                fetch(`/api/monitors/${id}`, {method: 'DELETE'})
                .then(r => r.json())
                .then(data => {
                    if (data.success) loadMonitors();
                });
            }
        }
        
        function loadMonitors() {
            fetch('/api/monitors')
            .then(r => r.json())
            .then(data => {
                const list = document.getElementById('monitorList');
                list.innerHTML = data.map(m => `
                    <div class="flex justify-between items-center bg-gray-700 rounded p-3">
                        <div>
                            <span class="font-semibold">${m.name}</span>
                            <span class="text-gray-400 ml-2">${m.host}</span>
                            <span class="text-gray-500 ml-2">(${m.interval}s)</span>
                        </div>
                        <button onclick="removeMonitor('${m.id}')" 
                            class="bg-red-600 hover:bg-red-700 px-3 py-1 rounded text-sm">
                            삭제
                        </button>
                    </div>
                `).join('');
            });
        }
        
        function loadResults() {
            fetch('/api/results')
            .then(r => r.json())
            .then(data => {
                const results = document.getElementById('results');
                results.innerHTML = data.map(r => `
                    <div class="flex justify-between items-center bg-gray-700 rounded p-2 text-sm">
                        <span class="${r.status === 'success' ? 'text-green-500' : 'text-red-500'}">
                            ${r.status === 'success' ? '✅' : '❌'} ${r.monitor_name}
                        </span>
                        <span class="text-gray-400">${r.time_ms}</span>
                        <span class="text-gray-500">${new Date(r.timestamp).toLocaleTimeString()}</span>
                    </div>
                `).join('');
            });
        }
        
        function loadStats() {
            fetch('/api/stats')
            .then(r => r.json())
            .then(data => {
                document.getElementById('total').textContent = data.total;
                document.getElementById('success').textContent = data.success;
                document.getElementById('uptime').textContent = data.uptime;
            });
        }
        
        setInterval(() => { loadResults(); loadStats(); }, 2000);
        loadMonitors();
        loadResults();
        loadStats();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    # 랜딩 페이지
    return '''
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white min-h-screen">
        <nav class="bg-gray-800 border-b border-gray-700">
            <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                <h1 class="text-xl font-bold">🔍 PingMonitor Pro</h1>
                <div class="flex gap-4">
                    <a href="/login" class="text-gray-300 hover:text-white">로그인</a>
                    <a href="/register" class="bg-green-600 hover:bg-green-700 px-4 py-2 rounded">회원가입</a>
                </div>
            </div>
        </nav>
        
        <div class="container mx-auto px-4 py-16">
            <div class="text-center mb-16">
                <h1 class="text-5xl font-bold mb-4">서버 모니터링의 새로운 기준</h1>
                <p class="text-xl text-gray-400 mb-8">실시간 핑 모니터링으로 서버 가동률을 99.9%로 유지하세요</p>
                <a href="/register" class="bg-green-600 hover:bg-green-700 px-8 py-3 rounded-lg text-lg font-semibold">무료로 시작하기</a>
            </div>
            
            <div class="grid grid-cols-1 md:grid-cols-3 gap-8 mb-16">
                <div class="bg-gray-800 rounded-lg p-6 text-center">
                    <div class="text-4xl mb-4">⚡</div>
                    <h3 class="text-xl font-semibold mb-2">실시간 모니터링</h3>
                    <p class="text-gray-400">30초 간격으로 서버 상태를 확인하고 즉시 알림 받기</p>
                </div>
                <div class="bg-gray-800 rounded-lg p-6 text-center">
                    <div class="text-4xl mb-4">📊</div>
                    <h3 class="text-xl font-semibold mb-2">상태 페이지</h3>
                    <p class="text-gray-400">고객에게 서비스 상태를 투명하게 공유</p>
                </div>
                <div class="bg-gray-800 rounded-lg p-6 text-center">
                    <div class="text-4xl mb-4">🔔</div>
                    <h3 class="text-xl font-semibold mb-2">다중 알림</h3>
                    <p class="text-gray-400">이메일, Slack, Telegram으로 즉시 알림</p>
                </div>
            </div>
            
            <div class="bg-gray-800 rounded-lg p-8 text-center">
                <h2 class="text-3xl font-bold mb-8">가격 플랜</h2>
                <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
                    <div class="border border-gray-700 rounded-lg p-6">
                        <h3 class="text-xl font-bold mb-2">Free</h3>
                        <p class="text-3xl font-bold mb-4">$0</p>
                        <p class="text-gray-400">50개 모니터</p>
                    </div>
                    <div class="border border-blue-500 rounded-lg p-6">
                        <h3 class="text-xl font-bold mb-2">Solo</h3>
                        <p class="text-3xl font-bold mb-4">$7/월</p>
                        <p class="text-gray-400">10개 모니터</p>
                    </div>
                    <div class="border border-green-500 rounded-lg p-6">
                        <h3 class="text-xl font-bold mb-2">Team</h3>
                        <p class="text-3xl font-bold mb-4">$15/월</p>
                        <p class="text-gray-400">50개 모니터</p>
                    </div>
                    <div class="border border-purple-500 rounded-lg p-6">
                        <h3 class="text-xl font-bold mb-2">Enterprise</h3>
                        <p class="text-3xl font-bold mb-4">$49/월</p>
                        <p class="text-gray-400">무제한</p>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/dashboard')
@login_required
def dashboard():
    # 사용자 플랜 정보
    plan = PLANS.get(current_user.plan, PLANS['free'])
    
    # 사용자 모니터 수
    conn = sqlite3.connect(DB_PATH)
    user_monitors = conn.execute("SELECT COUNT(*) FROM monitors WHERE user_id=?", (current_user.id,)).fetchone()[0]
    conn.close()
    
    max_monitors = plan['monitors'] if plan['monitors'] != -1 else '무제한'
    used_monitors = user_monitors
    max_status_pages = plan['status_pages'] if plan['status_pages'] != -1 else '무제한'
    used_status_pages = 0  # TODO: 구현 필요
    
    return DASHBOARD_TEMPLATE.replace('{{ email }}', current_user.email)\
                              .replace('{{ plan }}', current_user.plan.upper())\
                              .replace('{{ used_monitors }}', str(used_monitors))\
                              .replace('{{ max_monitors }}', str(max_monitors))\
                              .replace('{{ used_status_pages }}', str(used_status_pages))\
                              .replace('{{ max_status_pages }}', str(max_status_pages))

@app.route('/pricing')
@login_required
def pricing():
    return '''
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white min-h-screen">
        <nav class="bg-gray-800 border-b border-gray-700">
            <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                <h1 class="text-xl font-bold">🔍 PingMonitor Pro</h1>
                <a href="/dashboard" class="text-blue-400">대시보드로</a>
            </div>
        </nav>
        
        <div class="container mx-auto px-4 py-8">
            <h1 class="text-3xl font-bold mb-8 text-center">플랜 선택</h1>
            
            <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
                <div class="bg-gray-800 rounded-lg p-6 border-2 border-gray-700">
                    <h2 class="text-2xl font-bold mb-2">Free</h2>
                    <p class="text-4xl font-bold mb-4">$0</p>
                    <ul class="space-y-2 mb-6">
                        <li>✅ 50개 모니터</li>
                        <li>✅ 5분 간격</li>
                        <li>✅ 1개 상태 페이지</li>
                    </ul>
                    <button class="w-full bg-gray-600 py-2 rounded" disabled>현재 플랜</button>
                </div>
                
                <div class="bg-gray-800 rounded-lg p-6 border-2 border-blue-500">
                    <h2 class="text-2xl font-bold mb-2">Solo</h2>
                    <p class="text-4xl font-bold mb-4">$7<span class="text-lg">/월</span></p>
                    <ul class="space-y-2 mb-6">
                        <li>✅ 10개 모니터</li>
                        <li>✅ 1분 간격</li>
                        <li>✅ 5개 상태 페이지</li>
                        <li>✅ 이메일 알림</li>
                    </ul>
                    <a href="/payment/solo" class="block w-full bg-blue-600 hover:bg-blue-700 py-2 rounded text-center">업그레이드</a>
                </div>
                
                <div class="bg-gray-800 rounded-lg p-6 border-2 border-green-500">
                    <h2 class="text-2xl font-bold mb-2">Team</h2>
                    <p class="text-4xl font-bold mb-4">$15<span class="text-lg">/월</span></p>
                    <ul class="space-y-2 mb-6">
                        <li>✅ 50개 모니터</li>
                        <li>✅ 1분 간격</li>
                        <li>✅ 10개 상태 페이지</li>
                        <li>✅ Slack/Telegram 알림</li>
                        <li>✅ 팀 협업</li>
                    </ul>
                    <a href="/payment/team" class="block w-full bg-green-600 hover:bg-green-700 py-2 rounded text-center">업그레이드</a>
                </div>
                
                <div class="bg-gray-800 rounded-lg p-6 border-2 border-purple-500">
                    <h2 class="text-2xl font-bold mb-2">Enterprise</h2>
                    <p class="text-4xl font-bold mb-4">$49<span class="text-lg">/월</span></p>
                    <ul class="space-y-2 mb-6">
                        <li>✅ 무제한 모니터</li>
                        <li>✅ 30초 간격</li>
                        <li>✅ 무제한 상태 페이지</li>
                        <li>✅ 모든 알림 채널</li>
                        <li>✅ API 액세스</li>
                        <li>✅ 우선 지원</li>
                    </ul>
                    <a href="/payment/enterprise" class="block w-full bg-purple-600 hover:bg-purple-700 py-2 rounded text-center">문의하기</a>
                </div>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/payment/<plan>')
@login_required
def payment(plan):
    plan_info = {
        'solo': {'name': 'Solo', 'price': '$7'},
        'team': {'name': 'Team', 'price': '$15'},
        'enterprise': {'name': 'Enterprise', 'price': '$49'}
    }
    
    if plan not in plan_info:
        return redirect(url_for('pricing'))
    
    info = plan_info[plan]
    
    return f'''
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white min-h-screen">
        <nav class="bg-gray-800 border-b border-gray-700">
            <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                <h1 class="text-xl font-bold">🔍 PingMonitor Pro</h1>
                <a href="/pricing" class="text-blue-400">뒤로</a>
            </div>
        </nav>
        
        <div class="container mx-auto px-4 py-16">
            <div class="max-w-md mx-auto bg-gray-800 rounded-lg p-8">
                <h1 class="text-2xl font-bold mb-6 text-center">{info['name']} 플랜 결제</h1>
                
                <div class="bg-gray-700 rounded p-4 mb-6">
                    <p class="text-lg">결제 금액: <span class="text-2xl font-bold">{info['price']}</span>/월</p>
                </div>
                
                <form onsubmit="processPayment(event)">
                    <div class="mb-4">
                        <label class="block mb-2">카드 번호</label>
                        <input type="text" placeholder="4242 4242 4242 4242" class="w-full bg-gray-700 rounded px-4 py-2 text-white" required>
                    </div>
                    <div class="flex gap-4 mb-4">
                        <div class="flex-1">
                            <label class="block mb-2">만료일</label>
                            <input type="text" placeholder="MM/YY" class="w-full bg-gray-700 rounded px-4 py-2 text-white" required>
                        </div>
                        <div class="flex-1">
                            <label class="block mb-2">CVC</label>
                            <input type="text" placeholder="123" class="w-full bg-gray-700 rounded px-4 py-2 text-white" required>
                        </div>
                    </div>
                    <button type="submit" id="payBtn" class="w-full bg-green-600 hover:bg-green-700 py-3 rounded font-semibold">{info['price']} 결제하기</button>
                </form>
                
                <p class="mt-4 text-center text-gray-400 text-sm">테스트 모드: 실제 결제되지 않습니다</p>
            </div>
        </div>
        
        <script>
            function processPayment(e) {{
                e.preventDefault();
                const btn = document.getElementById('payBtn');
                btn.textContent = '처리 중...';
                btn.disabled = true;
                
                setTimeout(() => {{
                    window.location.href = '/payment-success?plan={plan}';
                }}, 1500);
            }}
        </script>
    </body>
    </html>
    '''

@app.route('/payment-success')
@login_required
def payment_success():
    plan = request.args.get('plan', 'solo')
    
    # 사용자 플랜 업데이트
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, current_user.id))
    conn.commit()
    conn.close()
    
    return f'''
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
        <div class="text-center">
            <div class="text-6xl mb-4">✅</div>
            <h1 class="text-3xl font-bold mb-4">결제 완료!</h1>
            <p class="text-gray-400 mb-8">{plan.upper()} 플랜으로 업그레이드되었습니다.</p>
            <a href="/dashboard" class="bg-green-600 hover:bg-green-700 px-8 py-3 rounded-lg font-semibold">대시보드로</a>
        </div>
    </body>
    </html>
    '''

# API 엔드포인트 (인증 필요)
@app.route('/api/monitors', methods=['GET'])
@login_required
def get_monitors():
    conn = sqlite3.connect(DB_PATH)
    monitors = conn.execute(
        "SELECT id, host, name, interval, active FROM monitors WHERE user_id=?",
        (current_user.id,)
    ).fetchall()
    conn.close()
    
    return jsonify([{
        'id': m[0],
        'host': m[1],
        'name': m[2],
        'interval': m[3],
        'active': m[4]
    } for m in monitors])

@app.route('/api/monitors', methods=['POST'])
@login_required
def add_monitor():
    data = request.json
    
    # 플랜 제한 확인
    plan = PLANS.get(current_user.plan, PLANS['free'])
    conn = sqlite3.connect(DB_PATH)
    current_count = conn.execute("SELECT COUNT(*) FROM monitors WHERE user_id=?", (current_user.id,)).fetchone()[0]
    
    if plan['monitors'] != -1 and current_count >= plan['monitors']:
        conn.close()
        return jsonify({'success': False, 'error': '플랜 제한 도달. 업그레이드 필요.'}), 400
    
    monitor_id = monitor.add_monitor(
        host=data['host'],
        interval=data.get('interval', 60),
        name=data.get('name', ''),
        user_id=current_user.id
    )
    conn.close()
    
    return jsonify({'success': True, 'id': monitor_id})

@app.route('/api/monitors/<monitor_id>', methods=['DELETE'])
@login_required
def remove_monitor_api(monitor_id):
    # 소유권 확인
    conn = sqlite3.connect(DB_PATH)
    mon = conn.execute("SELECT user_id FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    if not mon or mon[0] != current_user.id:
        conn.close()
        return jsonify({'success': False, 'error': '권한 없음'}), 403
    
    monitor.remove_monitor(monitor_id)
    conn.close()
    return jsonify({'success': True})

@app.route('/api/results')
@login_required
def get_results():
    monitor_id = request.args.get('monitor_id')
    conn = sqlite3.connect(DB_PATH)
    
    if monitor_id:
        # 소유권 확인
        mon = conn.execute("SELECT user_id, name FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not mon or mon[0] != current_user.id:
            conn.close()
            return jsonify([])
        
        monitor_name = mon[1]
        results = conn.execute(
            "SELECT status, time_ms, timestamp FROM ping_results WHERE monitor_id=? ORDER BY timestamp DESC LIMIT 50",
            (monitor_id,)
        ).fetchall()
    else:
        # 사용자의 모든 모니터 결과
        user_monitors = conn.execute("SELECT id, name FROM monitors WHERE user_id=?", (current_user.id,)).fetchall()
        monitor_ids = {m[0]: m[1] for m in user_monitors}
        
        if not monitor_ids:
            results = []
        else:
            placeholders = ','.join(['?'] * len(monitor_ids))
            results = conn.execute(
                f"SELECT monitor_id, status, time_ms, timestamp FROM ping_results WHERE monitor_id IN ({placeholders}) ORDER BY timestamp DESC LIMIT 50",
                list(monitor_ids.keys())
            ).fetchall()
    
    conn.close()
    
    if monitor_id:
        return jsonify([{
            'status': r[0],
            'time_ms': r[1],
            'timestamp': r[2],
            'monitor_name': monitor_name
        } for r in results])
    else:
        return jsonify([{
            'status': r[1],
            'time_ms': r[2],
            'timestamp': r[3],
            'monitor_name': monitor_ids.get(r[0], 'Unknown')
        } for r in results])

@app.route('/api/stats')
@login_required
def get_stats():
    monitor_id = request.args.get('monitor_id')
    conn = sqlite3.connect(DB_PATH)
    
    if monitor_id:
        # 소유권 확인
        mon = conn.execute("SELECT user_id FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if not mon or mon[0] != current_user.id:
            conn.close()
            return jsonify({'total': 0, 'success': 0, 'failed': 0, 'uptime': '0%'})
        
        results = conn.execute(
            "SELECT status FROM ping_results WHERE monitor_id=?",
            (monitor_id,)
        ).fetchall()
    else:
        user_monitors = conn.execute("SELECT id FROM monitors WHERE user_id=?", (current_user.id,)).fetchall()
        monitor_ids = [m[0] for m in user_monitors]
        
        if not monitor_ids:
            results = []
        else:
            placeholders = ','.join(['?'] * len(monitor_ids))
            results = conn.execute(
                f"SELECT status FROM ping_results WHERE monitor_id IN ({placeholders})",
                monitor_ids
            ).fetchall()
    
    conn.close()
    
    total = len(results)
    success = len([r for r in results if r[0] == 'success'])
    failed = total - success
    uptime = (success / total * 100) if total > 0 else 0
    
    return jsonify({
        'total': total,
        'success': success,
        'failed': failed,
        'uptime': f"{uptime:.1f}%"
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
