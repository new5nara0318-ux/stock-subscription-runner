#!/usr/bin/env python3
"""
핑 자동 요청 프로그램
- 지정된 호스트에 주기적으로 ping 전송
- GUI 인터페이스 제공
- 결과 실시간 표시
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import subprocess
import threading
import time
from datetime import datetime


class PingAutoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("핑 자동 요청 프로그램")
        self.root.geometry("600x500")
        
        self.running = False
        self.ping_thread = None
        
        self.setup_ui()
        
    def setup_ui(self):
        # 호스트 입력
        host_frame = ttk.Frame(self.root, padding="10")
        host_frame.pack(fill=tk.X)
        
        ttk.Label(host_frame, text="호스트/IP:").pack(side=tk.LEFT)
        self.host_entry = ttk.Entry(host_frame, width=30)
        self.host_entry.pack(side=tk.LEFT, padx=5)
        self.host_entry.insert(0, "8.8.8.8")
        
        # 간격 설정
        interval_frame = ttk.Frame(self.root, padding="10")
        interval_frame.pack(fill=tk.X)
        
        ttk.Label(interval_frame, text="간격(초):").pack(side=tk.LEFT)
        self.interval_entry = ttk.Entry(interval_frame, width=10)
        self.interval_entry.pack(side=tk.LEFT, padx=5)
        self.interval_entry.insert(0, "1")
        
        # 버튼
        btn_frame = ttk.Frame(self.root, padding="10")
        btn_frame.pack(fill=tk.X)
        
        self.start_btn = ttk.Button(btn_frame, text="시작", command=self.start_ping)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="중지", command=self.stop_ping, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        self.clear_btn = ttk.Button(btn_frame, text="지우기", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)
        
        # 통계
        stats_frame = ttk.Frame(self.root, padding="10")
        stats_frame.pack(fill=tk.X)
        
        self.stats_label = ttk.Label(stats_frame, text="전송: 0 | 성공: 0 | 실패: 0")
        self.stats_label.pack()
        
        # 로그
        log_frame = ttk.Frame(self.root, padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(log_frame, text="핑 결과:").pack(anchor=tk.W)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=70)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 통계 변수
        self.total_sent = 0
        self.total_success = 0
        self.total_failed = 0
        
    def start_ping(self):
        host = self.host_entry.get().strip()
        if not host:
            return
            
        try:
            interval = float(self.interval_entry.get())
            if interval < 0.1:
                interval = 0.1
        except ValueError:
            interval = 1
            
        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        
        self.ping_thread = threading.Thread(target=self.ping_loop, args=(host, interval))
        self.ping_thread.daemon = True
        self.ping_thread.start()
        
    def stop_ping(self):
        self.running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        
    def clear_log(self):
        self.log_text.delete(1.0, tk.END)
        self.total_sent = 0
        self.total_success = 0
        self.total_failed = 0
        self.update_stats()
        
    def ping_loop(self, host, interval):
        while self.running:
            self.send_ping(host)
            time.sleep(interval)
            
    def send_ping(self, host):
        self.total_sent += 1
        
        try:
            # Windows ping 명령
            result = subprocess.run(
                ['ping', '-n', '1', host],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            timestamp = datetime.now().strftime("%H:%M:%S")
            
            if result.returncode == 0:
                self.total_success += 1
                # 응답 시간 추출
                output = result.stdout
                if "time=" in output or "시간=" in output:
                    # 응답 시간이 있는 경우
                    lines = output.split('\n')
                    for line in lines:
                        if "time=" in line or "시간=" in line:
                            self.log(f"[{timestamp}] ✅ {line.strip()}")
                            break
                else:
                    self.log(f"[{timestamp}] ✅ 응답 있음")
            else:
                self.total_failed += 1
                self.log(f"[{timestamp}] ❌ 응답 없음")
                
        except subprocess.TimeoutExpired:
            self.total_failed += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log(f"[{timestamp}] ❌ 타임아웃")
        except Exception as e:
            self.total_failed += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log(f"[{timestamp}] ❌ 에러: {str(e)}")
            
        self.update_stats()
        
    def log(self, message):
        self.root.after(0, lambda: self.log_text.insert(tk.END, message + "\n"))
        self.root.after(0, lambda: self.log_text.see(tk.END))
        
    def update_stats(self):
        self.root.after(0, lambda: self.stats_label.config(
            text=f"전송: {self.total_sent} | 성공: {self.total_success} | 실패: {self.total_failed}"
        ))


def main():
    root = tk.Tk()
    app = PingAutoApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
