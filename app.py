from flask import (
    Flask,
    request,
    redirect,
    render_template_string,
    send_file,
    session,
    url_for,
)
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime
from functools import wraps

import gspread
from google.oauth2.service_account import Credentials


app = Flask(__name__)


# ============================================================
# 基本設定
# ============================================================

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
VIEW_PASSWORD = os.environ.get("VIEW_PASSWORD", "")

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    secrets.token_hex(32),
)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.config["SESSION_PERMANENT"] = False


# GoogleスプレッドシートのID
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()

# Renderの環境変数に登録したサービスアカウントJSON
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "",
).strip()


FIELDS = (
    "department",
    "report_date",
    "reporter",
    "subject",
    "summary",
    "details",
    "issues",
    "actions",
    "future_plans",
)


SHEET_HEADERS = [
    "id",
    "department",
    "report_date",
    "reporter",
    "subject",
    "summary",
    "details",
    "issues",
    "actions",
    "future_plans",
    "hidden",
    "created_at",
]


# ============================================================
# Googleスプレッドシート接続
# ============================================================

def get_worksheet():
    """
    Googleスプレッドシートの1枚目を取得する。
    1行目が空の場合は、必要な見出しを自動作成する。
    """

    if not GOOGLE_SHEET_ID:
        raise RuntimeError(
            "環境変数 GOOGLE_SHEET_ID が設定されていません。"
        )

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            "環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。"
        )

    try:
        service_account_info = json.loads(
            GOOGLE_SERVICE_ACCOUNT_JSON
        )
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON のJSON形式が正しくありません。"
        ) from error

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )

    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    worksheet = spreadsheet.sheet1

    current_headers = worksheet.row_values(1)

    if current_headers != SHEET_HEADERS:
        if not current_headers:
            worksheet.update(
                "A1:L1",
                [SHEET_HEADERS],
            )
        else:
            worksheet.insert_row(
                SHEET_HEADERS,
                1,
            )

    return worksheet


def row_to_report(row):
    """
    Googleスプレッドシートの1行を、
    既存HTMLで使える辞書形式に変換する。
    """

    report = {}

    for index, field in enumerate(SHEET_HEADERS):
        if index < len(row):
            report[field] = row[index]
        else:
            report[field] = ""

    try:
        report["id"] = int(report["id"])
    except (TypeError, ValueError):
        report["id"] = 0

    try:
        report["hidden"] = int(report["hidden"])
    except (TypeError, ValueError):
        report["hidden"] = 0

    return report


def get_all_reports():
    """
    Googleスプレッドシートに保存されている全報告を取得する。
    """

    worksheet = get_worksheet()
    rows = worksheet.get_all_values()

    if len(rows) <= 1:
        return []

    reports = []

    for row in rows[1:]:
        if not any(str(value).strip() for value in row):
            continue

        reports.append(row_to_report(row))

    return reports


def get_next_report_id(reports):
    """
    次に使うIDを作成する。
    """

    ids = []

    for report in reports:
        try:
            ids.append(int(report.get("id", 0)))
        except (TypeError, ValueError):
            pass

    if not ids:
        return 1

    return max(ids) + 1


def report_to_row(report):
    """
    報告データをGoogleスプレッドシートの行に変換する。
    """

    return [
        str(report.get("id", "")),
        str(report.get("department", "")),
        str(report.get("report_date", "")),
        str(report.get("reporter", "")),
        str(report.get("subject", "")),
        str(report.get("summary", "")),
        str(report.get("details", "")),
        str(report.get("issues", "")),
        str(report.get("actions", "")),
        str(report.get("future_plans", "")),
        str(report.get("hidden", 0)),
        str(report.get("created_at", "")),
    ]


def find_sheet_row_by_id(report_id):
    """
    IDからGoogleスプレッドシート上の行番号を探す。
    1行目は見出しなので、データは2行目から始まる。
    """

    worksheet = get_worksheet()
    rows = worksheet.get_all_values()

    for row_number, row in enumerate(rows[1:], start=2):
        if not row:
            continue

        try:
            current_id = int(row[0])
        except (TypeError, ValueError, IndexError):
            continue

        if current_id == report_id:
            return row_number

    return None


def append_report(report):
    """
    新しい報告を末尾に追加する。
    """

    worksheet = get_worksheet()
    worksheet.append_row(
        report_to_row(report),
        value_input_option="USER_ENTERED",
    )


def update_report_hidden(report_id, hidden):
    """
    報告の非表示状態を変更する。
    """

    row_number = find_sheet_row_by_id(report_id)

    if row_number is None:
        return False

    worksheet = get_worksheet()

    # hidden列は11列目、K列
    worksheet.update_cell(
        row_number,
        11,
        str(hidden),
    )

    return True


# ============================================================
# 入力値の処理
# ============================================================

def normalize_date(value):
    if not value:
        return ""

    value = value.strip()
    value = value.replace("年", "-")
    value = value.replace("月", "-")
    value = value.replace("日", "")
    value = value.replace("/", "-")
    value = value.replace(".", "-")

    match = re.search(
        r"(20\d{2})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})",
        value,
    )

    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    return value


def clean_text(value):
    if not value:
        return ""

    value = value.replace("\r\n", "\n")
    value = value.replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)

    return value.strip()


def get_form_values():
    values = {}

    for field in FIELDS:
        values[field] = clean_text(
            request.form.get(field, "")
        )

    values["report_date"] = normalize_date(
        values["report_date"]
    )

    return values


# ============================================================
# 認証
# ============================================================

def require_view_password(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not VIEW_PASSWORD:
            return (
                "閲覧用パスワードが設定されていません。",
                500,
            )

        if not session.get("view_authenticated"):
            next_url = request.full_path

            if next_url.endswith("?"):
                next_url = next_url[:-1]

            return redirect(
                url_for(
                    "view_login",
                    next=next_url,
                )
            )

        return function(*args, **kwargs)

    return wrapper


def require_admin_password(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return (
                "管理者パスワードが設定されていません。",
                500,
            )

        password = request.form.get(
            "password",
            "",
        )

        if not secrets.compare_digest(
            password,
            ADMIN_PASSWORD,
        ):
            return (
                "管理者パスワードが違います。",
                403,
            )

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# 表示用CSS
# ============================================================

BASE_STYLE = """
<style>
@import url(
'https://fonts.googleapis.com/css2?family=DotGothic16&family=Noto+Sans+JP:wght@400;500;700;900&display=swap'
);

* {
    box-sizing: border-box;
}

:root {
    --lavender: #f1efff;
    --black: #111111;
    --pink: #ff3cac;
    --blue: #174bff;
    --white: #ffffff;
    --gray: #625f70;
    --yellow: #fff3b8;
}

body {
    margin: 0;
    color: var(--black);
    background:
        linear-gradient(
            rgba(17, 17, 17, 0.05) 1px,
            transparent 1px
        ),
        var(--lavender);
    background-size: 100% 32px;
    font-family: "Noto Sans JP", sans-serif;
    line-height: 1.7;
}

header {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-height: 76px;
    padding: 16px 5vw;
    background: rgba(241, 239, 255, 0.96);
    border-bottom: 2px solid var(--black);
}

.logo {
    font-family: "DotGothic16", sans-serif;
    font-size: 22px;
    letter-spacing: .1em;
}

.header-note {
    color: var(--gray);
    font-family: "DotGothic16", sans-serif;
    font-size: 11px;
}

main {
    width: min(1160px, 90%);
    margin: auto;
}

.hero {
    min-height: 360px;
    padding: 90px 0 70px;
    border-bottom: 2px solid var(--black);
}

.hero-label {
    display: inline-block;
    padding: 5px 13px;
    color: var(--white);
    background: var(--blue);
    border: 2px solid var(--black);
    font-family: "DotGothic16", sans-serif;
    font-size: 12px;
    letter-spacing: .12em;
    box-shadow: 5px 5px 0 var(--black);
}

h1 {
    max-width: 700px;
    margin: 25px 0 20px;
    font-family: "DotGothic16", "Noto Sans JP", sans-serif;
    font-size: clamp(42px, 8vw, 90px);
    line-height: 1.08;
}

.hero-text {
    max-width: 590px;
    color: var(--gray);
}

.hero-line {
    display: block;
    position: relative;
    z-index: 1;
    width: fit-content;
}

.hero-line::after {
    content: "";
    position: absolute;
    left: 0;
    right: 0;
    bottom: 2px;
    z-index: -1;
    height: 10px;
    transform: rotate(-2deg);
}

.hero-line-pink::after {
    background: var(--pink);
}

.hero-line-blue::after {
    background: var(--blue);
}

h2 {
    display: flex;
    align-items: center;
    gap: 13px;
    margin: 65px 0 25px;
    padding-bottom: 13px;
    border-bottom: 2px solid var(--black);
    font-family: "DotGothic16", "Noto Sans JP", sans-serif;
    font-size: 26px;
}

h2::before {
    content: "";
    width: 18px;
    height: 18px;
    background: var(--pink);
    border: 2px solid var(--black);
}

form {
    display: grid;
    gap: 4px;
    padding: 32px;
    background: var(--white);
    border: 2px solid var(--black);
    box-shadow: 9px 9px 0 var(--blue);
}

form p {
    margin: 0 0 16px;
    font-weight: 700;
}

input,
textarea,
select {
    width: 100%;
    margin-top: 7px;
    padding: 13px 15px;
    color: var(--black);
    background: #faf9ff;
    border: 2px solid var(--black);
    border-radius: 0;
    font: inherit;
}

textarea {
    min-height: 125px;
    resize: vertical;
}

input:focus,
textarea:focus,
select:focus {
    outline: 4px solid var(--pink);
    outline-offset: 2px;
    border-color: var(--blue);
}

button,
.nav-button,
.back-button {
    display: inline-block;
    width: fit-content;
    margin-top: 8px;
    padding: 13px 28px;
    color: var(--white);
    background: var(--blue);
    border: 2px solid var(--black);
    box-shadow: 5px 5px 0 var(--black);
    font: inherit;
    font-weight: 900;
    text-decoration: none;
    cursor: pointer;
}

button:hover,
.nav-button:hover,
.back-button:hover {
    background: var(--pink);
}

.notice {
    margin: 20px 0;
    padding: 14px 18px;
    color: #5a4200;
    background: var(--yellow);
    border: 2px solid var(--black);
    font-size: 14px;
}

.toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    align-items: center;
    margin: 24px 0;
}

.toolbar form {
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 0;
    background: transparent;
    border: 0;
    box-shadow: none;
}

.toolbar select {
    width: auto;
    min-width: 180px;
    margin: 0;
}

.report-list {
    display: grid;
    gap: 12px;
}

.report-band {
    background: var(--white);
    border: 2px solid var(--black);
    box-shadow: 5px 5px 0 var(--pink);
}

.report-band summary {
    display: grid;
    grid-template-columns: 125px 150px 1fr 30px;
    gap: 14px;
    align-items: center;
    padding: 14px 18px;
    cursor: pointer;
    list-style: none;
}

.report-band summary::-webkit-details-marker {
    display: none;
}

.report-band summary:hover {
    background: #f8f5ff;
}

.band-date,
.band-department {
    color: var(--gray);
    font-size: 13px;
}

.band-subject {
    font-weight: 900;
}

.band-arrow {
    color: var(--blue);
    font-size: 22px;
}

.report-content {
    padding: 20px 24px;
    border-top: 2px solid var(--black);
}

.report-content p {
    margin: 10px 0;
    white-space: pre-wrap;
}

.admin-form {
    margin-top: 25px;
    padding: 18px;
    box-shadow: none;
    border: 2px solid var(--black);
}

.admin-form p {
    margin-bottom: 8px;
}

footer {
    margin-top: 95px;
    padding: 32px 6vw;
    color: var(--white);
    background: var(--black);
    border-top: 8px solid var(--pink);
    text-align: center;
    font-family: "DotGothic16", sans-serif;
    font-size: 12px;
}

.register-main {
    width: min(900px, 90%);
    margin: 45px auto;
}

.register-main h1 {
    font-size: clamp(34px, 7vw, 64px);
    line-height: 1.2;
}

@media (max-width: 700px) {
    .header-note {
        display: none;
    }

    .hero {
        min-height: 430px;
        padding: 70px 0 60px;
    }

    form {
        padding: 22px;
    }

    .report-band summary {
        grid-template-columns: 1fr 25px;
        gap: 4px;
    }

    .band-date,
    .band-department,
    .band-subject {
        grid-column: 1;
    }

    .band-arrow {
        grid-column: 2;
        grid-row: 1 / span 3;
    }

    .toolbar {
        display: block;
    }

    .toolbar form {
        margin-top: 10px;
    }
}
</style>
"""
# ============================================================
# 閲覧認証画面
# ============================================================

VIEW_LOGIN_HTML = f"""
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>閲覧認証</title>
{BASE_STYLE}
</head>
<body>
<main class="register-main">
<h1>閲覧認証</h1>

<form method="post">
<p>
閲覧用の合言葉<br>
<input type="password" name="password" required autofocus>
</p>

{{% if error %}}
<div class="notice">{{{{ error }}}}</div>
{{% endif %}}

<button type="submit">閲覧する →</button>
</form>
</main>
</body>
</html>
"""


# ============================================================
# 登録画面
# ============================================================

REGISTER_HTML = f"""
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>報告を登録</title>
{BASE_STYLE}
</head>
<body>
<main class="register-main">
<a class="back-button" href="/">← トップへ戻る</a>

<h1>報告を登録</h1>

<div class="notice">
現在、AIによる自動解析機能は未実装です。<br>
報告内容を確認し、各項目へ手入力して登録してください。
</div>

<form method="post">
<p>
部署名<br>
<input type="text" name="department" required>
</p>

<p>
報告日<br>
<input type="date" name="report_date" required>
</p>

<p>
報告者<br>
<input type="text" name="reporter" required>
</p>

<p>
件名<br>
<input type="text" name="subject" required>
</p>

<p>
概要<br>
<textarea name="summary"></textarea>
</p>

<p>
詳細内容<br>
<textarea name="details"></textarea>
</p>

<p>
課題<br>
<textarea name="issues"></textarea>
</p>

<p>
対応内容<br>
<textarea name="actions"></textarea>
</p>

<p>
今後の予定<br>
<textarea name="future_plans"></textarea>
</p>

<button type="submit">登録する →</button>
</form>
</main>
</body>
</html>
"""


# ============================================================
# 一覧画面
# ============================================================

INDEX_HTML = f"""
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>報告共有システム</title>
{BASE_STYLE}
</head>
<body>

<header>
<div class="logo">REPORTS_</div>
<div class="header-note">DEPARTMENT REPORT ARCHIVE</div>
</header>

<main>
<section class="hero">
<div class="hero-label">INFORMATION SHARING SYSTEM</div>

<h1>
<span class="hero-line hero-line-pink">報告を、</span>
<span class="hero-line hero-line-blue">一つの場所へ。</span>
</h1>

<p class="hero-text">
各部署から寄せられた報告を、
見つけやすく、読みやすく共有します。
</p>

<a class="nav-button" href="/register">
報告を登録する →
</a>
</section>

<h2>報告を検索</h2>

<form method="get" action="/">
<p>
キーワード<br>
<input
    type="text"
    name="keyword"
    value="{{{{ keyword }}}}"
    placeholder="件名や報告内容を入力"
>
</p>

<p>
部署<br>
<select name="department">
<option value="">すべての部署</option>
{{% for item in departments %}}
<option
    value="{{{{ item }}}}"
    {{% if selected_department == item %}}
    selected
    {{% endif %}}
>
{{{{ item }}}}
</option>
{{% endfor %}}
</select>
</p>

<p>
日付の範囲<br>
<input
    type="date"
    name="start_date"
    value="{{{{ start_date }}}}"
>
から
<input
    type="date"
    name="end_date"
    value="{{{{ end_date }}}}"
>
</p>

<button type="submit">検索する →</button>
<a href="/">検索を消す</a>
</form>

<div class="toolbar">
<strong>報告の並び順：</strong>

<form method="get" action="/">
<input
    type="hidden"
    name="keyword"
    value="{{{{ keyword }}}}"
>
<input
    type="hidden"
    name="department"
    value="{{{{ selected_department }}}}"
>
<input
    type="hidden"
    name="start_date"
    value="{{{{ start_date }}}}"
>
<input
    type="hidden"
    name="end_date"
    value="{{{{ end_date }}}}"
>
<input
    type="hidden"
    name="show_hidden"
    value="{{{{ "1" if show_hidden else "" }}}}"
>

<select name="sort" onchange="this.form.submit()">
<option
    value="new"
    {{% if sort == "new" %}}selected{{% endif %}}
>
日付の新しい順
</option>

<option
    value="old"
    {{% if sort == "old" %}}selected{{% endif %}}
>
日付の古い順
</option>
</select>
</form>

<a href="/?show_hidden=1">非表示の報告を見る</a>
</div>

<div class="report-list">
{{% for report in reports %}}
<details class="report-band">
<summary>
<span class="band-date">
{{{{ report["report_date"] or "日付未入力" }}}}
</span>

<span class="band-department">
{{{{ report["department"] or "部署未入力" }}}}
</span>

<span class="band-subject">
{{{{ report["subject"] or "件名なし" }}}}
</span>

<span class="band-arrow">＋</span>
</summary>

<div class="report-content">
<p>
<strong>報告者：</strong>
{{{{ report["reporter"] or "未入力" }}}}
</p>

<p>
<strong>概要：</strong>
{{{{ report["summary"] or "未入力" }}}}
</p>

<p>
<strong>詳細内容：</strong>
{{{{ report["details"] or "未入力" }}}}
</p>

<p>
<strong>課題：</strong>
{{{{ report["issues"] or "未入力" }}}}
</p>

<p>
<strong>対応内容：</strong>
{{{{ report["actions"] or "未入力" }}}}
</p>

<p>
<strong>今後の予定：</strong>
{{{{ report["future_plans"] or "未入力" }}}}
</p>

{{% if report["hidden"] == 0 %}}
<form
    method="post"
    action="/hide/{{{{ report["id"] }}}}"
    class="admin-form"
>
<p>この報告を非表示にする</p>

<input
    type="password"
    name="password"
    placeholder="管理者パスワード"
    required
>

<button type="submit">非表示にする</button>
</form>
{{% else %}}
<p>この報告は非表示になっています。</p>

<form
    method="post"
    action="/restore/{{{{ report["id"] }}}}"
    class="admin-form"
>
<p>この報告を表示に戻す</p>

<input
    type="password"
    name="password"
    placeholder="管理者パスワード"
    required
>

<button type="submit">表示に戻す</button>
</form>
{{% endif %}
</div>
</details>
{{% else %}}
<p>該当する報告はありません。</p>
{{% endfor %}
</div>

<h2>データを保存</h2>

<form method="post" action="/backup">
<p>
管理者パスワード<br>
<input
    type="password"
    name="password"
    placeholder="管理者パスワード"
    required
>
</p>

<button type="submit">
バックアップをダウンロード
</button>
</form>
</main>

<footer>
REPORTS_ / INFORMATION SHARING SYSTEM
</footer>

</body>
</html>
"""


# ============================================================
# 閲覧認証
# ============================================================

@app.route("/view-login", methods=["GET", "POST"])
def view_login():
    if request.method == "GET":
        return render_template_string(
            VIEW_LOGIN_HTML,
            error="",
        )

    password = request.form.get(
        "password",
        "",
    )

    if not VIEW_PASSWORD:
        return (
            "閲覧用パスワードが設定されていません。",
            500,
        )

    if not secrets.compare_digest(
        password,
        VIEW_PASSWORD,
    ):
        return (
            render_template_string(
                VIEW_LOGIN_HTML,
                error="合言葉が違います。",
            ),
            403,
        )

    session["view_authenticated"] = True

    next_url = request.args.get(
        "next",
        "/",
    )

    if not next_url.startswith("/"):
        next_url = "/"

    return redirect(next_url)


# ============================================================
# 一覧・検索
# ============================================================

@app.route("/", methods=["GET"])
@require_view_password
def index():
    keyword = request.args.get(
        "keyword",
        "",
    ).strip()

    department = request.args.get(
        "department",
        "",
    ).strip()

    start_date = request.args.get(
        "start_date",
        "",
    ).strip()

    end_date = request.args.get(
        "end_date",
        "",
    ).strip()

    sort = request.args.get(
        "sort",
        "new",
    ).strip()

    if sort not in ("new", "old"):
        sort = "new"

    show_hidden = (
        request.args.get(
            "show_hidden",
            "",
        )
        == "1"
    )

    all_reports = get_all_reports()

    departments = sorted(
        {
            report["department"]
            for report in all_reports
            if report["department"]
        }
    )

    reports = []

    for report in all_reports:
        if not show_hidden and report["hidden"] == 1:
            continue

        if keyword:
            searchable_text = " ".join(
                [
                    report["subject"],
                    report["summary"],
                    report["details"],
                    report["issues"],
                    report["actions"],
                    report["future_plans"],
                ]
            ).lower()

            if keyword.lower() not in searchable_text:
                continue

        if department:
            if report["department"] != department:
                continue

        if start_date:
            report_date = report["report_date"]

            if not report_date or report_date < start_date:
                continue

        if end_date:
            report_date = report["report_date"]

            if not report_date or report_date > end_date:
                continue

        reports.append(report)

    def sort_key(report):
        report_date = report["report_date"] or "9999-99-99"
        report_id = report["id"]

        return (
            report_date,
            report_id,
        )

    reports.sort(
        key=sort_key,
        reverse=(sort == "new"),
    )

    return render_template_string(
        INDEX_HTML,
        reports=reports,
        departments=departments,
        keyword=keyword,
        selected_department=department,
        start_date=start_date,
        end_date=end_date,
        sort=sort,
        show_hidden=show_hidden,
    )


# ============================================================
# 報告登録
# ============================================================

@app.route("/register", methods=["GET", "POST"])
@require_view_password
def register_report():
    if request.method == "GET":
        return render_template_string(
            REGISTER_HTML
        )

    values = get_form_values()

    required_fields = {
        "department": "部署名",
        "report_date": "報告日",
        "reporter": "報告者",
        "subject": "件名",
    }

    for field, label in required_fields.items():
        if not values[field]:
            return (
                f"{label}を入力してください。",
                400,
            )

    reports = get_all_reports()

    report = {
        "id": get_next_report_id(reports),
        "department": values["department"],
        "report_date": values["report_date"],
        "reporter": values["reporter"],
        "subject": values["subject"],
        "summary": values["summary"],
        "details": values["details"],
        "issues": values["issues"],
        "actions": values["actions"],
        "future_plans": values["future_plans"],
        "hidden": 0,
        "created_at": datetime.now().isoformat(
            timespec="seconds"
        ),
    }

    append_report(report)

    return redirect("/")
# ============================================================
# 報告を非表示にする
# ============================================================

@app.route(
    "/hide/<int:report_id>",
    methods=["POST"],
)
@require_view_password
@require_admin_password
def hide_report(report_id):
    updated = update_report_hidden(
        report_id,
        1,
    )

    if not updated:
        return (
            "指定された報告が見つかりません。",
            404,
        )

    return redirect("/")


# ============================================================
# 報告を復元する
# ============================================================

@app.route(
    "/restore/<int:report_id>",
    methods=["POST"],
)
@require_view_password
@require_admin_password
def restore_report(report_id):
    updated = update_report_hidden(
        report_id,
        0,
    )

    if not updated:
        return (
            "指定された報告が見つかりません。",
            404,
        )

    return redirect("/?show_hidden=1")


# ============================================================
# Googleスプレッドシートのバックアップ
# ============================================================

@app.route(
    "/backup",
    methods=["POST"],
)
@require_view_password
@require_admin_password
def backup_database():
    reports = get_all_reports()

    if not reports:
        return (
            "バックアップ対象がありません。",
            404,
        )

    output = io.StringIO(
        newline=""
    )

    writer = csv.writer(
        output
    )

    writer.writerow(SHEET_HEADERS)

    for report in reports:
        writer.writerow(
            report_to_row(report)
        )

    memory_file = io.BytesIO(
        output.getvalue().encode(
            "utf-8-sig"
        )
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_name = (
        f"reports_backup_{timestamp}.csv"
    )

    return send_file(
        memory_file,
        as_attachment=True,
        download_name=backup_name,
        mimetype="text/csv",
    )


# ============================================================
# エラー処理
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):
    return (
        "送信データが大きすぎます。",
        413,
    )


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception(error)

    return (
        "サーバー内部でエラーが発生しました。"
        "Renderのログを確認してください。",
        500,
    )


# ============================================================
# ローカル起動
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000",
            )
        ),
        debug=False,
    )
