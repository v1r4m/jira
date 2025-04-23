from flask import Flask, jsonify, request, send_file, render_template
import requests
import pandas as pd
import io
import base64
import re
import os

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Jira 설정
JIRA_URL = os.getenv("JIRA_URL")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

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

@app.route("/export", methods=["POST"])
def export_from_log_text():
    log_text = request.form.get("log_text", "")
    lines = log_text.splitlines()

    current_build = ""
    current_time = ""
    data = []

    for line in lines:
        build_number = extract_build_number(line)
        if build_number:
            current_build = build_number
            current_time = extract_build_time(line)
            continue

        issue_keys = extract_issue_keys(line)
        for key in issue_keys:
            issue_info = get_jira_issue(key)
            data.append({
                "build_number": current_build,
                "build_time": current_time,
                "issue_key": key,
                "jira_link": f"{JIRA_URL}/browse/{key}",
                "comment": line.strip(),
                "assignee": issue_info.get("assignee"),
                "reporter": issue_info.get("reporter"),
                "created": issue_info.get("created"),
                "approver": issue_info.get("approver_name"),
                "approval_time": issue_info.get("approval_time")
            })

    df = pd.DataFrame(data)
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    return send_file(
        io.BytesIO(csv_buffer.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="manual_jira_report.csv"
    )

if __name__ == "__main__":
    app.run(debug=True, port=5001)
