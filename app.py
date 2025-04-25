from flask import Flask, jsonify, request, send_file, render_template, Response, stream_with_context
import requests
import pandas as pd
import io
import base64
import re
import os
import json
from queue import Queue
from threading import Thread
from collections import defaultdict
import tempfile
from werkzeug.utils import secure_filename

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Jira 설정
JIRA_URL = os.getenv("JIRA_URL")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# 진행 상황을 저장할 전역 큐
progress_queue = Queue()

# 임시 파일을 저장할 디렉토리 설정
TEMP_DIR = tempfile.gettempdir()

def get_jira_issue(issue_key):
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{JIRA_USER}:{JIRA_API_TOKEN}".encode()).decode(),
        "Accept": "application/json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        issue = response.json()
        fields = issue.get("fields", {})

        approvals = fields.get("customfield_10027", [])
        latest_approver = ""
        latest_date = ""
        for approval in approvals:
            if approval.get("finalDecision") == "approved":
                completed_date = approval.get("completedDate", {}).get("jira", "")
                for approver_entry in approval.get("approvers", []):
                    approver = approver_entry.get("approver", {})
                    name = approver.get("displayName", "")
                    # 최신 승인일자 기준으로 갱신
                    if completed_date > latest_date:
                        latest_date = completed_date
                        latest_approver = name

        return {
            "assignee": fields.get("assignee", {}).get("displayName", ""),
            "reporter": fields.get("reporter", {}).get("displayName", ""),
            "created": fields.get("created", ""),
            "approver_name": latest_approver,
            "approval_time": latest_date
        }
    return {}

def extract_issue_keys(text):
    return re.findall(r"ITSM-\d+", text)

def extract_build_number(line):
    match = re.search(r"#(\d+)", line)
    return match.group(1) if match else ""

def extract_build_time(line):
    match = re.search(r"\((.*?)\)", line)
    return match.group(1) if match else ""

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start-processing", methods=["POST"])
def start_processing():
    log_text = request.form.get("log_text", "")
    lines = log_text.splitlines()
    
    # 백그라운드에서 처리 시작
    thread = Thread(target=process_log, args=(lines,))
    thread.start()
    
    return jsonify({"status": "processing"})

@app.route("/progress")
def progress():
    def generate():
        while True:
            progress = progress_queue.get()
            yield f"data: {json.dumps(progress)}\n\n"
            if progress.get("complete"):
                break
    
    return Response(generate(), mimetype="text/event-stream")

def process_log(lines):
    builds = defaultdict(lambda: {
        'build_time': '',
        'issues': [],
        'comments': [],
        'assignees': set(),
        'reporters': set(),
        'approvers': set()
    })
    
    current_build = ""
    total_lines = len(lines)
    processed_lines = 0
    
    for line in lines:
        processed_lines += 1
        progress = int((processed_lines / total_lines) * 100)
        progress_queue.put({"progress": progress})
        
        build_number = extract_build_number(line)
        if build_number:
            current_build = build_number
            builds[current_build]['build_time'] = extract_build_time(line)
            continue
            
        if not current_build:  # 빌드 번호가 없는 라인은 스킵
            continue
            
        issue_keys = extract_issue_keys(line)
        if issue_keys:
            builds[current_build]['comments'].append(line.strip())
            for key in issue_keys:
                builds[current_build]['issues'].append(key)
                issue_info = get_jira_issue(key)
                if issue_info.get('assignee'):
                    builds[current_build]['assignees'].add(issue_info.get('assignee'))
                if issue_info.get('reporter'):
                    builds[current_build]['reporters'].add(issue_info.get('reporter'))
                if issue_info.get('approver_name'):
                    builds[current_build]['approvers'].add(issue_info.get('approver_name'))

    # 데이터를 DataFrame 형식에 맞게 변환
    data = []
    for build_number, build_info in builds.items():
        data.append({
            "build_number": build_number,
            "build_time": build_info['build_time'],
            "issue_keys": ', '.join(build_info['issues']),
            "jira_links": ', '.join([f"{JIRA_URL}/browse/{key}" for key in build_info['issues']]),
            "comments": '\n'.join(build_info['comments']),
            "assignees": ', '.join(build_info['assignees']),
            "reporters": ', '.join(build_info['reporters']),
            "approvers": ', '.join(build_info['approvers'])
        })

    # 처리 완료 알림
    progress_queue.put({"progress": 100, "complete": True})
    
    return data

@app.route("/process", methods=["POST"])
def process_log_text():
    def generate():
        log_text = request.form.get("log_text", "")
        lines = log_text.splitlines()
        
        builds = defaultdict(lambda: {
            'build_time': '',
            'issue_key': '',
            'comment': '',
            'assignee': '',
            'reporter': '',
            'approver': '',
            'approval_time': ''
        })
        
        current_build = ""
        total_lines = len(lines)
        processed_lines = 0
        
        yield f"data: {json.dumps({'progress': 0})}\n\n"
        
        for line in lines:
            processed_lines += 1
            progress = int((processed_lines / total_lines) * 100)
            
            if progress % 5 == 0:
                yield f"data: {json.dumps({'progress': progress})}\n\n"
            
            build_number = extract_build_number(line)
            if build_number:
                current_build = build_number
                builds[current_build]['build_time'] = extract_build_time(line)
                continue
                
            if not current_build:
                continue
                
            if builds[current_build]['issue_key']:
                continue
                
            issue_keys = extract_issue_keys(line)
            if issue_keys:
                key = issue_keys[0]
                issue_info = get_jira_issue(key)
                builds[current_build].update({
                    'issue_key': key,
                    'comment': line.strip(),
                    'assignee': issue_info.get('assignee', ''),
                    'reporter': issue_info.get('reporter', ''),
                    'approver': issue_info.get('approver_name', ''),
                    'approval_time': issue_info.get('approval_time', '')
                })

        # 데이터를 DataFrame 형식으로 변환
        data = []
        for build_number, build_info in builds.items():
            if build_info['issue_key']:
                data.append({
                    "build_number": build_number,
                    "build_time": build_info['build_time'],
                    "issue_key": build_info['issue_key'],
                    "jira_link": f"{JIRA_URL}/browse/{build_info['issue_key']}",
                    "comment": build_info['comment'],
                    "assignee": build_info['assignee'],
                    "reporter": build_info['reporter'],
                    "approver": build_info['approver'],
                    "approval_time": build_info['approval_time']
                })

        df = pd.DataFrame(data)
        columns = [
            "build_number",
            "build_time",
            "issue_key",
            "jira_link",
            "comment",
            "assignee",
            "reporter",
            "approver",
            "approval_time"
        ]
        df = df[columns]
        
        # 임시 파일로 저장
        temp_file = tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.csv',
            delete=False,
            dir=TEMP_DIR
        )
        
        df.to_csv(temp_file.name, index=False)
        filename = os.path.basename(temp_file.name)

        yield f"data: {json.dumps({'progress': 100, 'complete': True, 'filename': filename})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/download/<filename>")
def download_csv(filename):
    try:
        filepath = os.path.join(TEMP_DIR, secure_filename(filename))
        
        @after_this_request
        def remove_file(response):
            try:
                os.remove(filepath)
            except Exception as e:
                app.logger.error(f"Error removing file {filepath}: {e}")
            return response
            
        return send_file(
            filepath,
            mimetype="text/csv",
            as_attachment=True,
            download_name="manual_jira_report.csv"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8084)
