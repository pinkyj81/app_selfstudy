from pathlib import Path
import json
import os
import re
from datetime import date, datetime, timedelta

import pyodbc
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

BASE_DIR   = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env', override=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'landing-secret')
app.config['SESSION_COOKIE_NAME'] = 'study_app_session'

HOST = os.getenv('FLASK_HOST', '0.0.0.0')
PORT = int(os.getenv('FLASK_PORT', '5002'))

DB_SERVER = os.getenv('DB_SERVER', '').strip()
DB_DATABASE = os.getenv('DB_DATABASE', '').strip()
DB_USERNAME = os.getenv('DB_USERNAME', '').strip()
DB_PASSWORD = os.getenv('DB_PASSWORD', '').strip()
DB_DRIVER = (os.getenv('DB_DRIVER') or '').strip()

DATA_FILE  = BASE_DIR / 'data.json'
UPLOAD_DIR = BASE_DIR / 'static' / 'images'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED    = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def ensure_app_tables(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        IF OBJECT_ID('dbo.study_plan_dday_items', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.study_plan_dday_items (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                title NVARCHAR(200) NOT NULL,
                target_date DATE NOT NULL,
                created_at DATETIMEOFFSET NOT NULL CONSTRAINT DF_study_plan_dday_created_at DEFAULT SYSDATETIMEOFFSET()
            );
            CREATE INDEX IX_study_plan_dday_user_target ON dbo.study_plan_dday_items(user_id, target_date);
        END
        """
    )
    cursor.execute(
        """
        IF OBJECT_ID('dbo.study_app_meta', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.study_app_meta (
                meta_key NVARCHAR(100) NOT NULL PRIMARY KEY,
                meta_value NVARCHAR(MAX) NULL,
                updated_at DATETIMEOFFSET NOT NULL CONSTRAINT DF_study_app_meta_updated_at DEFAULT SYSDATETIMEOFFSET()
            );
        END
        """
    )
    cursor.execute(
        """
        IF OBJECT_ID('dbo.study_plan_task_completion', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.study_plan_task_completion (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                task_id INT NOT NULL,
                completed_date DATE NOT NULL,
                created_at DATETIMEOFFSET NOT NULL CONSTRAINT DF_study_plan_task_completion_created_at DEFAULT SYSDATETIMEOFFSET(),
                CONSTRAINT UQ_study_plan_task_completion UNIQUE (user_id, task_id, completed_date)
            );
            CREATE INDEX IX_study_plan_task_completion_user_date ON dbo.study_plan_task_completion(user_id, completed_date);
        END
        """
    )
    cursor.execute(
        """
        IF COL_LENGTH('dbo.study_plan_task', 'task_status') IS NULL
        BEGIN
            ALTER TABLE dbo.study_plan_task
            ADD task_status NVARCHAR(20) NOT NULL
                CONSTRAINT DF_study_plan_task_task_status DEFAULT N'진행중';
        END
        """
    )
    conn.commit()


def resolve_driver():
    installed = [d for d in pyodbc.drivers() if 'SQL Server' in d]
    for candidate in [DB_DRIVER, 'ODBC Driver 17 for SQL Server', 'ODBC Driver 18 for SQL Server', 'SQL Server']:
        if candidate and candidate in installed:
            return candidate
    return installed[0] if installed else DB_DRIVER


def get_db_conn():
    driver = resolve_driver()
    conn_str = (
        f'DRIVER={{{driver}}};'
        f'SERVER={DB_SERVER};'
        f'DATABASE={DB_DATABASE};'
        f'UID={DB_USERNAME};'
        f'PWD={DB_PASSWORD};'
        'Encrypt=yes;TrustServerCertificate=yes;'
    )
    return pyodbc.connect(conn_str, timeout=20)


def get_table_columns(conn, table_name: str):
    rows = conn.cursor().execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        (table_name,),
    ).fetchall()
    return {row[0].lower() for row in rows}


def get_primary_key_column(conn, table_name: str):
    row = conn.cursor().execute(
        """
        SELECT TOP 1 kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
        INNER JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS kcu
            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
        WHERE tc.TABLE_NAME = ? AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY kcu.ORDINAL_POSITION
        """,
        (table_name,),
    ).fetchone()
    if row:
        return row[0]

    row = conn.cursor().execute(
        """
        SELECT TOP 1 COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = ?
          AND COLUMNPROPERTY(OBJECT_ID('dbo.' + TABLE_NAME), COLUMN_NAME, 'IsIdentity') = 1
        """,
        (table_name,),
    ).fetchone()
    return row[0] if row else None


def get_last_inserted_id(conn, table_name: str):
    pk_col = get_primary_key_column(conn, table_name)
    if pk_col:
        row = conn.cursor().execute(f"SELECT MAX([{pk_col}]) FROM dbo.{table_name}").fetchone()
        return row[0] if row and row[0] is not None else None

    row = conn.cursor().execute("SELECT CAST(SCOPE_IDENTITY() AS int)").fetchone()
    return row[0] if row and row[0] is not None else None


def insert_plan_and_get_id(cursor, columns, values):
    sql = (
        f"INSERT INTO dbo.study_plan ({', '.join(columns)}) "
        f"OUTPUT INSERTED.plan_id "
        f"VALUES ({', '.join(['?'] * len(columns))})"
    )
    row = cursor.execute(sql, values).fetchone()
    return row[0] if row else None


def get_or_create_user_id(conn):
    try:
        user_cols = get_table_columns(conn, 'study_plan_user')
    except Exception:
        user_cols = set()

    if 'user_name' in user_cols:
        row = conn.cursor().execute("SELECT TOP 1 user_id FROM dbo.study_plan_user ORDER BY user_id").fetchone()
        if row:
            return row[0]
        conn.cursor().execute("INSERT INTO dbo.study_plan_user (user_name, created_at) VALUES (?, SYSDATETIMEOFFSET())", ('guest',))
        conn.commit()
        row = conn.cursor().execute("SELECT TOP 1 user_id FROM dbo.study_plan_user ORDER BY user_id DESC").fetchone()
        return row[0]
    return 1


def generate_date_list(start_date: str, end_date: str, selected_weekdays):
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        raise ValueError('시작일과 종료일 형식이 올바르지 않습니다.')

    if end < start:
        raise ValueError('종료일은 시작일보다 늦어야 합니다.')

    weekday_set = {int(day) for day in selected_weekdays}
    dates = []
    current = start
    while current <= end:
        if current.weekday() in weekday_set:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    if not dates:
        raise ValueError('선택한 요일에 해당하는 날짜가 없습니다.')
    return dates


def load_data() -> dict:
    default_data = {'title': '앱 이름을 입력하세요', 'description': '앱 설명을 입력하세요.', 'images': []}
    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        rows = conn.cursor().execute(
            "SELECT meta_key, meta_value FROM dbo.study_app_meta WHERE meta_key IN ('title', 'description', 'images')"
        ).fetchall()
        conn.close()

        data = dict(default_data)
        meta_map = {str(row[0]): row[1] for row in rows}
        if meta_map.get('title'):
            data['title'] = str(meta_map['title'])
        if meta_map.get('description'):
            data['description'] = str(meta_map['description'])

        images_raw = meta_map.get('images')
        if images_raw:
            try:
                parsed = json.loads(images_raw)
                if isinstance(parsed, list):
                    data['images'] = [str(name) for name in parsed if str(name).strip()]
            except Exception:
                data['images'] = []
        return data
    except Exception:
        if DATA_FILE.exists():
            return json.loads(DATA_FILE.read_text(encoding='utf-8'))
        return default_data


def save_data(data: dict):
    title = str(data.get('title') or '').strip()
    description = str(data.get('description') or '').strip()
    images_json = json.dumps(data.get('images') or [], ensure_ascii=False)

    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        cursor = conn.cursor()
        for key, value in (('title', title), ('description', description), ('images', images_json)):
            cursor.execute(
                """
                MERGE dbo.study_app_meta AS target
                USING (SELECT ? AS meta_key, ? AS meta_value) AS src
                    ON target.meta_key = src.meta_key
                WHEN MATCHED THEN
                    UPDATE SET meta_value = src.meta_value, updated_at = SYSDATETIMEOFFSET()
                WHEN NOT MATCHED THEN
                    INSERT (meta_key, meta_value) VALUES (src.meta_key, src.meta_value);
                """,
                (key, value),
            )
        conn.commit()
        conn.close()
    except Exception:
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def allowed(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED


def get_user_list():
    try:
        conn = get_db_conn()
        rows = conn.cursor().execute(
            "SELECT user_id, user_name FROM dbo.study_plan_user ORDER BY user_name"
        ).fetchall()
        conn.close()
        return [{'user_id': row[0], 'user_name': row[1]} for row in rows]
    except Exception:
        return []


def get_user_subjects(user_id):
    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        rows = conn.cursor().execute(
            """
            SELECT
                sp.plan_id,
                sp.subject,
                sp.title,
                COUNT(st.task_id) AS total_tasks,
                SUM(
                    CASE
                        WHEN ISNULL(st.task_status, N'진행중') = N'완료' THEN 1
                        WHEN tc.task_id IS NULL THEN 0
                        ELSE 1
                    END
                ) AS completed_tasks
            FROM dbo.study_plan sp
            INNER JOIN dbo.study_plan_task st
                ON st.plan_id = sp.plan_id
               AND CAST(st.plan_date AS date) >= CAST(GETDATE() AS date)
            LEFT JOIN (
                SELECT DISTINCT user_id, task_id, completed_date
                FROM dbo.study_plan_task_completion
            ) tc
                ON tc.user_id = sp.user_id
               AND tc.task_id = st.task_id
               AND tc.completed_date = CAST(st.plan_date AS date)
            WHERE sp.user_id = ?
              AND sp.subject IS NOT NULL
              AND LTRIM(RTRIM(sp.subject)) <> ''
            GROUP BY sp.plan_id, sp.subject, sp.title
            ORDER BY sp.subject, sp.plan_id
            """,
            (user_id,),
        ).fetchall()
        conn.close()

        grouped = {}
        for plan_id, subject_name, title, total_tasks, completed_tasks in rows:
            subject_name = (subject_name or '').strip()
            if not subject_name:
                continue
            plan_total = int(total_tasks or 0)
            plan_completed = int(completed_tasks or 0)
            entry = grouped.setdefault(subject_name, {
                'name': subject_name,
                'titles': [],
                'plan_count': 0,
                'total_tasks': 0,
                'completed_tasks': 0,
            })
            entry['plan_count'] += 1
            entry['total_tasks'] += plan_total
            entry['completed_tasks'] += plan_completed
            entry['titles'].append({
                'title': title or '무제',
                'plan_id': int(plan_id),
                'completed_count': plan_completed,
                'total_count': plan_total,
            })

        subjects = []
        for subject_name, entry in grouped.items():
            percent = 0
            if entry['total_tasks'] > 0:
                percent = round((entry['completed_tasks'] / entry['total_tasks']) * 100)
            subjects.append({
                'name': subject_name,
                'percent': percent,
                'completed_count': entry['completed_tasks'],
                'total_count': entry['total_tasks'],
                'plan_count': entry['plan_count'],
                'titles': entry['titles'],
            })
        return subjects
    except Exception:
        return []


def get_user_by_name(user_name):
    user_name = (user_name or '').strip()
    if not user_name:
        return None
    try:
        conn = get_db_conn()
        row = conn.cursor().execute(
            "SELECT user_id, user_name FROM dbo.study_plan_user WHERE user_name = ?",
            (user_name,),
        ).fetchone()
        conn.close()
        if row:
            return {'user_id': row[0], 'user_name': row[1]}
    except Exception:
        return None
    return None


def get_today_tasks(user_id, target_date=None):
    target_date = target_date or date.today().isoformat()
    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        task_columns = get_table_columns(conn, 'study_plan_task')
        select_sql = """
            SELECT sp.plan_id, sp.subject, sp.title, st.task_id, st.plan_date, st.task_title, st.order_no,
                   CASE WHEN tc.task_id IS NULL THEN 0 ELSE 1 END AS is_completed
        """
        if 'link_url' in task_columns:
            select_sql += ", st.link_url"
        select_sql += """
            FROM dbo.study_plan sp
            INNER JOIN dbo.study_plan_task st ON st.plan_id = sp.plan_id
            LEFT JOIN dbo.study_plan_task_completion tc
                ON tc.user_id = sp.user_id
               AND tc.task_id = st.task_id
               AND tc.completed_date = CAST(st.plan_date AS date)
            WHERE sp.user_id = ? AND CAST(st.plan_date AS date) = ?
            ORDER BY sp.subject, st.order_no, st.task_id
        """
        rows = conn.cursor().execute(select_sql, (user_id, target_date)).fetchall()
        conn.close()

        tasks = []
        for row in rows:
            task = {
                'plan_id': row[0],
                'subject': row[1] or '기타',
                'plan_title': row[2] or '무제',
                'task_id': row[3],
                'plan_date': row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4]),
                'task_title': row[5] or '학습 내용 없음',
                'order_no': row[6],
                'is_completed': bool(row[7]),
            }
            if 'link_url' in task_columns:
                task['link_url'] = row[8] if len(row) > 8 else None
            tasks.append(task)
        return tasks
    except Exception:
        return []


def get_today_progress(user_id, target_date=None):
    tasks = get_today_tasks(user_id, target_date)
    total = len(tasks)
    completed = sum(1 for task in tasks if task.get('is_completed'))
    percent = round((completed / total) * 100) if total > 0 else 0
    return {
        'total': total,
        'completed': completed,
        'percent': percent,
    }


@app.route('/')
def index():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('login_study_home.html', users=get_user_list())


@app.route('/login', methods=['POST'])
def login():
    user_name = str(request.form.get('user_name') or '').strip()

    if not user_name:
        return render_template('login_study_home.html', users=get_user_list(), error='사용자를 선택하세요.'), 400

    user = get_user_by_name(user_name)
    if user is None:
        return render_template('login_study_home.html', users=get_user_list(), error='존재하지 않는 사용자입니다.'), 400

    session['user_id'] = user['user_id']
    session['user_name'] = user['user_name']
    return redirect(url_for('dashboard'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/dashboard')
def dashboard():
    if not session.get('user_id'):
        return redirect(url_for('index'))

    user_id = session['user_id']
    user_name = session.get('user_name', '사용자')
    subjects = get_user_subjects(user_id)
    total_subjects = len(subjects)
    today_progress = get_today_progress(user_id)
    overall = today_progress['percent']
    dashboard_dday = get_dashboard_dday_text(user_id)

    return render_template(
        'mobile_study_home.html',
        user_name=user_name,
        subjects=subjects,
        user_id=user_id,
        total_subjects=total_subjects,
        overall=overall,
        today_total=today_progress['total'],
        today_completed=today_progress['completed'],
        dashboard_dday=dashboard_dday,
    )


def get_dashboard_dday_text(user_id):
    items = get_dday_items(user_id)
    if not items:
        return 'D-day 일정 없음'

    today = date.today()
    candidates = []
    for item in items:
        try:
            target = datetime.strptime(item['target_date'], '%Y-%m-%d').date()
        except Exception:
            continue
        delta_days = (target - today).days
        if delta_days < 0:
            continue
        title = str(item.get('title') or '').strip()
        candidates.append((title, delta_days))

    if not candidates:
        return 'D-day 일정 없음'

    title, nearest = min(candidates, key=lambda entry: entry[1])
    if nearest == 0:
        badge = 'D-Day'
    else:
        badge = f'D-{nearest}'

    if title:
        return f'{title} : {badge}'
    return badge


def get_dday_items(user_id):
    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        rows = conn.cursor().execute(
            """
            SELECT id, title, target_date
            FROM dbo.study_plan_dday_items
            WHERE user_id = ?
            ORDER BY target_date, id
            """,
            (user_id,),
        ).fetchall()
        conn.close()

        return [
            {
                'id': int(row[0]),
                'title': str(row[1] or '').strip(),
                'target_date': row[2].isoformat() if hasattr(row[2], 'isoformat') else str(row[2]),
            }
            for row in rows
        ]
    except Exception:
        items = session.get('dday_items', [])
        if not isinstance(items, list):
            return []
        normalized = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            title = str(item.get('title') or '').strip()
            target_date = str(item.get('target_date') or '').strip()
            if not title or not target_date:
                continue
            normalized.append({'id': item.get('id', index + 1), 'title': title, 'target_date': target_date})
        session['dday_items'] = normalized
        return normalized


def create_dday_item(user_id, title, target_date):
    conn = get_db_conn()
    ensure_app_tables(conn)
    conn.cursor().execute(
        "INSERT INTO dbo.study_plan_dday_items (user_id, title, target_date) VALUES (?, ?, ?)",
        (user_id, title, target_date),
    )
    conn.commit()
    conn.close()


def delete_dday_item_db(user_id, item_id):
    conn = get_db_conn()
    ensure_app_tables(conn)
    conn.cursor().execute(
        "DELETE FROM dbo.study_plan_dday_items WHERE user_id = ? AND id = ?",
        (user_id, item_id),
    )
    conn.commit()
    conn.close()


@app.route('/dday', methods=['GET', 'POST'])
def dday_page():
    if not session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        title = str(request.form.get('dday_title') or '').strip()
        target_date = str(request.form.get('target_date') or '').strip()

        if title and target_date:
            try:
                create_dday_item(session['user_id'], title, target_date)
            except Exception:
                items = get_dday_items(session['user_id'])
                items.append({
                    'id': max((int(item.get('id', 0)) for item in items), default=0) + 1,
                    'title': title,
                    'target_date': target_date,
                })
                session['dday_items'] = items

        return redirect(url_for('dday_page'))

    items = get_dday_items(session['user_id'])
    today = date.today()
    processed = []
    for item in items:
        try:
            target = datetime.strptime(item['target_date'], '%Y-%m-%d').date()
        except Exception:
            continue
        delta_days = (target - today).days
        if delta_days < 0:
            continue
        if delta_days == 0:
            badge = 'D-Day'
        else:
            badge = f'D-{delta_days}'
        processed.append({
            **item,
            'days_left': delta_days,
            'badge': badge,
        })

    processed.sort(key=lambda item: (item['days_left'], item['title']))
    return render_template('dday_manage.html', user_name=session.get('user_name', '사용자'), items=processed)


@app.route('/dday/delete/<int:item_id>', methods=['POST'])
def delete_dday_item(item_id):
    if not session.get('user_id'):
        return redirect(url_for('index'))

    try:
        delete_dday_item_db(session['user_id'], item_id)
    except Exception:
        items = get_dday_items(session['user_id'])
        session['dday_items'] = [item for item in items if int(item.get('id', 0)) != item_id]
    return redirect(url_for('dday_page'))


def normalize_date_value(text):
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%m/%d/%Y', '%m-%d-%Y', '%m.%d.%Y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_task_rows_from_text(raw_text):
    if raw_text is None:
        return []

    url_pattern = re.compile(r'(https?://\S+|www\.\S+|\S+\.com\S*|\S+\.net\S*|\S+\.org\S*|\S+\.co\.kr\S*|\S+\.kr\S*)', re.IGNORECASE)
    rows = []
    next_auto_order = 1
    for line in str(raw_text).splitlines():
        text = line.strip()
        if not text:
            continue

        # Format: "1 학습 내용 | https://..." (link part is optional)
        body, sep, link_part = text.partition('|')
        link_url = link_part.strip() if sep else ''
        body = body.strip()

        order_no = None
        title = ''
        numbered = re.match(r'^(\d+)\s*[\).:\-]?\s*(.+)$', body)
        if numbered:
            try:
                order_no = int(numbered.group(1))
            except Exception:
                order_no = None
            title = numbered.group(2).strip()
        else:
            title = re.sub(r'^[-*•]\s*', '', body).strip()
            order_no = next_auto_order

        if not link_url:
            found_url = url_pattern.search(title)
            if found_url:
                link_url = found_url.group(1).strip()
                title = (title[:found_url.start()] + ' ' + title[found_url.end():]).strip()
                title = re.sub(r'\s{2,}', ' ', title)

        if not title:
            continue

        rows.append({
            'order_no': int(order_no or next_auto_order),
            'task_title': title,
            'link_url': link_url,
        })
        next_auto_order = max(next_auto_order, int(order_no or next_auto_order) + 1)

    return rows


def get_plan_detail(plan_id):
    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        plan = conn.cursor().execute(
            """
            SELECT plan_id, title, subject, user_id, image_url, color
            FROM dbo.study_plan
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone()
        if not plan:
            conn.close()
            return None

        tasks = conn.cursor().execute(
            """
            SELECT task_id, plan_id, plan_date, task_title, order_no, ISNULL(task_status, N'진행중') AS task_status
            FROM dbo.study_plan_task
            WHERE plan_id = ?
            ORDER BY order_no, task_id
            """,
            (plan_id,),
        ).fetchall()
        conn.close()

        return {
            'plan_id': plan[0],
            'title': plan[1],
            'subject': plan[2],
            'user_id': plan[3],
            'image_url': plan[4],
            'color': plan[5],
            'tasks': [
                {
                    'task_id': row[0],
                    'plan_id': row[1],
                    'plan_date': row[2].isoformat() if hasattr(row[2], 'isoformat') else row[2],
                    'task_title': row[3],
                    'order_no': row[4],
                    'task_status': row[5] or '진행중',
                }
                for row in tasks
            ],
        }
    except Exception:
        return None


@app.route('/plan/<int:plan_id>/parse', methods=['POST'])
def plan_parse(plan_id):
    if not session.get('user_id'):
        return redirect(url_for('index'))

    detail = get_plan_detail(plan_id)
    if detail is None:
        return redirect(url_for('dashboard'))

    raw_text = request.form.get('paste_text', '')
    parsed_rows = parse_task_rows_from_text(raw_text)
    if not parsed_rows:
        return redirect(url_for('plan_edit', plan_id=plan_id))

    existing_order_dates = {}
    max_order = 0
    for task in detail.get('tasks', []):
        order_no = task.get('order_no')
        try:
            order_no_int = int(order_no)
        except Exception:
            order_no_int = 0
        if order_no_int > max_order:
            max_order = order_no_int

        plan_date_raw = task.get('plan_date')
        normalized = normalize_date_value(plan_date_raw)
        if normalized:
            try:
                if order_no_int > 0:
                    existing_order_dates[order_no_int] = datetime.strptime(normalized, '%Y-%m-%d').date()
            except Exception:
                pass

    def suggest_plan_date(order_no_value: int) -> str:
        if order_no_value in existing_order_dates:
            return existing_order_dates[order_no_value].isoformat()

        lower_orders = [o for o in existing_order_dates.keys() if o < order_no_value]
        upper_orders = [o for o in existing_order_dates.keys() if o > order_no_value]

        if lower_orders:
            prev_order = max(lower_orders)
            prev_date = existing_order_dates[prev_order]
            gap = max(1, order_no_value - prev_order)
            return (prev_date + timedelta(days=gap)).isoformat()

        if upper_orders:
            next_order = min(upper_orders)
            next_date = existing_order_dates[next_order]
            gap = max(1, next_order - order_no_value)
            return (next_date - timedelta(days=gap)).isoformat()

        return date.today().isoformat()

    staged_rows = []
    sorted_rows = sorted(parsed_rows, key=lambda item: int(item.get('order_no') or 0))
    for idx, row in enumerate(sorted_rows):
        order_no = int(row.get('order_no') or 0)
        if order_no <= 0:
            order_no = max_order + idx + 1

        task_title = str(row.get('task_title') or '').strip()
        if not task_title:
            continue

        plan_date = suggest_plan_date(order_no)
        staged_rows.append({
            'order_no': order_no,
            'task_title': task_title,
            'plan_date': plan_date,
            'task_status': '진행중',
            'link_url': str(row.get('link_url') or '').strip(),
        })

    replace_range = None
    if staged_rows:
        order_values = [int(row.get('order_no') or 0) for row in staged_rows if int(row.get('order_no') or 0) > 0]
        if order_values:
            replace_range = [min(order_values), max(order_values)]

    session[f'plan_parse_preview_{plan_id}'] = {
        'rows': staged_rows,
        'replace_range': replace_range,
    }

    return redirect(url_for('plan_edit', plan_id=plan_id))


@app.route('/plan/<int:plan_id>/edit', methods=['GET', 'POST'])
def plan_edit(plan_id):
    if not session.get('user_id'):
        return redirect(url_for('index'))

    detail = get_plan_detail(plan_id)
    if detail is None:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        title = str(request.form.get('title') or '').strip()
        subject = str(request.form.get('subject') or '').strip()
        task_ids = request.form.getlist('task_id')
        task_dates = request.form.getlist('task_date')
        task_titles = request.form.getlist('task_title')
        task_statuses = request.form.getlist('task_status')
        task_orders = request.form.getlist('task_order')
        task_links = request.form.getlist('task_link')

        try:
            conn = get_db_conn()
            ensure_app_tables(conn)
            cursor = conn.cursor()
            task_columns = get_table_columns(conn, 'study_plan_task')

            existing_task_ids = {
                int(row[0])
                for row in cursor.execute(
                    "SELECT task_id FROM dbo.study_plan_task WHERE plan_id = ?",
                    (plan_id,),
                ).fetchall()
                if row[0] is not None
            }
            submitted_task_ids = {
                int(task_id)
                for task_id in task_ids
                if str(task_id or '').strip()
            }
            removed_task_ids = sorted(existing_task_ids - submitted_task_ids)

            for removed_task_id in removed_task_ids:
                cursor.execute(
                    "DELETE FROM dbo.study_plan_task_completion WHERE task_id = ?",
                    (removed_task_id,),
                )
                cursor.execute(
                    "DELETE FROM dbo.study_plan_task WHERE task_id = ?",
                    (removed_task_id,),
                )

            cursor.execute(
                "UPDATE dbo.study_plan SET title = ?, subject = ? WHERE plan_id = ?",
                (title or detail['title'], subject or detail['subject'], plan_id),
            )

            for idx, (task_id, plan_date, task_title, task_status, task_order, task_link) in enumerate(zip(task_ids, task_dates, task_titles, task_statuses, task_orders, task_links), start=1):
                status_value = str(task_status or '진행중').strip()
                if status_value not in {'완료', '진행중', '미완료'}:
                    status_value = '진행중'
                order_value = idx
                title_value = str(task_title or '').strip()
                link_value = str(task_link or '').strip()

                if not task_id:
                    if not title_value:
                        continue
                    if 'link_url' in task_columns:
                        cursor.execute(
                            "INSERT INTO dbo.study_plan_task (plan_id, plan_date, task_title, order_no, task_status, link_url, created_at) VALUES (?, ?, ?, ?, ?, ?, SYSDATETIMEOFFSET())",
                            (plan_id, plan_date or None, title_value, order_value, status_value, link_value or None),
                        )
                    else:
                        cursor.execute(
                            "INSERT INTO dbo.study_plan_task (plan_id, plan_date, task_title, order_no, task_status, created_at) VALUES (?, ?, ?, ?, ?, SYSDATETIMEOFFSET())",
                            (plan_id, plan_date or None, title_value, order_value, status_value),
                        )
                    continue

                if 'link_url' in task_columns:
                    cursor.execute(
                        "UPDATE dbo.study_plan_task SET plan_date = ?, task_title = ?, task_status = ?, order_no = ?, link_url = ? WHERE task_id = ?",
                        (plan_date or None, title_value, status_value, order_value, link_value or None, int(task_id)),
                    )
                else:
                    cursor.execute(
                        "UPDATE dbo.study_plan_task SET plan_date = ?, task_title = ?, task_status = ?, order_no = ? WHERE task_id = ?",
                        (plan_date or None, title_value, status_value, order_value, int(task_id)),
                    )

            conn.commit()
            conn.close()
            session.pop(f'plan_parse_preview_{plan_id}', None)
        except Exception:
            return redirect(url_for('dashboard'))

        return redirect(url_for('dashboard'))

    preview_payload = session.get(f'plan_parse_preview_{plan_id}') or {}
    if isinstance(preview_payload, list):
        preview_rows = preview_payload
        replace_range = None
    else:
        preview_rows = preview_payload.get('rows') or []
        replace_range = preview_payload.get('replace_range')

    if preview_rows:
        parsed_order_set = set()
        for row in preview_rows:
            try:
                order_value = int(row.get('order_no') or 0)
            except Exception:
                order_value = 0
            if order_value > 0:
                parsed_order_set.add(order_value)

        range_min = None
        range_max = None
        if isinstance(replace_range, list) and len(replace_range) == 2:
            try:
                range_min = int(replace_range[0])
                range_max = int(replace_range[1])
            except Exception:
                range_min = None
                range_max = None

        merged_by_order = {}
        for task in detail.get('tasks', []):
            row = dict(task)
            row['task_id'] = row.get('task_id') or ''
            row['link_url'] = row.get('link_url') or ''
            try:
                key = int(row.get('order_no') or 0)
            except Exception:
                key = 0
            if key > 0:
                if (
                    range_min is not None
                    and range_max is not None
                    and range_min <= key <= range_max
                    and key not in parsed_order_set
                ):
                    continue
                merged_by_order[key] = row

        for row in preview_rows:
            try:
                key = int(row.get('order_no') or 0)
            except Exception:
                key = 0
            if key <= 0:
                continue

            task = merged_by_order.get(key)
            if task:
                task['task_title'] = str(row.get('task_title') or task.get('task_title') or '')
                task['task_status'] = str(row.get('task_status') or task.get('task_status') or '진행중')
                task['link_url'] = str(row.get('link_url') or task.get('link_url') or '')
                continue

            merged_by_order[key] = {
                'task_id': '',
                'plan_id': plan_id,
                'plan_date': str(row.get('plan_date') or ''),
                'task_title': str(row.get('task_title') or ''),
                'order_no': key,
                'task_status': str(row.get('task_status') or '진행중'),
                'link_url': str(row.get('link_url') or ''),
            }

        detail['tasks'] = [merged_by_order[k] for k in sorted(merged_by_order.keys())]

    return render_template('plan_edit.html', plan=detail)


@app.route('/today')
def today_study():
    if not session.get('user_id'):
        return redirect(url_for('index'))

    raw_date = request.args.get('date') or ''
    try:
        selected_date = date.fromisoformat(str(raw_date).strip()) if raw_date else date.today()
    except ValueError:
        selected_date = date.today()

    target_date = selected_date.isoformat()
    prev_date = (selected_date - timedelta(days=1)).isoformat()
    next_date = (selected_date + timedelta(days=1)).isoformat()
    today_date = date.today().isoformat()

    tasks = get_today_tasks(session['user_id'], target_date)
    return render_template(
        'today_study.html',
        user_name=session.get('user_name', '사용자'),
        tasks=tasks,
        today_date=target_date,
        prev_date=prev_date,
        next_date=next_date,
        current_today=today_date,
    )


@app.route('/api/today-progress')
def api_today_progress():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': '로그인이 필요합니다.'}), 401

    try:
        progress = get_today_progress(int(session['user_id']))
        return jsonify({'ok': True, **progress})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/today-task-toggle', methods=['POST'])
def today_task_toggle():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': '로그인이 필요합니다.'}), 401

    payload = request.get_json(silent=True) or {}
    task_id_raw = payload.get('task_id')
    plan_date = str(payload.get('plan_date') or '').strip()
    is_completed = bool(payload.get('is_completed'))

    if task_id_raw is None or not plan_date:
        return jsonify({'ok': False, 'error': 'task_id와 plan_date가 필요합니다.'}), 400

    try:
        task_id = int(task_id_raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'task_id 형식이 올바르지 않습니다.'}), 400

    user_id = int(session['user_id'])

    try:
        conn = get_db_conn()
        ensure_app_tables(conn)
        cursor = conn.cursor()

        owner_row = cursor.execute(
            """
            SELECT TOP 1 sp.user_id
            FROM dbo.study_plan_task st
            INNER JOIN dbo.study_plan sp ON sp.plan_id = st.plan_id
            WHERE st.task_id = ? AND CAST(st.plan_date AS date) = ?
            """,
            (task_id, plan_date),
        ).fetchone()

        if not owner_row or int(owner_row[0]) != user_id:
            conn.close()
            return jsonify({'ok': False, 'error': '권한이 없거나 존재하지 않는 작업입니다.'}), 403

        if is_completed:
            cursor.execute(
                """
                MERGE dbo.study_plan_task_completion AS target
                USING (SELECT ? AS user_id, ? AS task_id, ? AS completed_date) AS src
                    ON target.user_id = src.user_id
                   AND target.task_id = src.task_id
                   AND target.completed_date = src.completed_date
                WHEN NOT MATCHED THEN
                    INSERT (user_id, task_id, completed_date)
                    VALUES (src.user_id, src.task_id, src.completed_date);
                """,
                (user_id, task_id, plan_date),
            )
        else:
            cursor.execute(
                "DELETE FROM dbo.study_plan_task_completion WHERE user_id = ? AND task_id = ? AND completed_date = ?",
                (user_id, task_id, plan_date),
            )

        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/admin')
def admin():
    return render_template('admin.html', data=load_data())


@app.route('/wrong-note')
def wrong_note_page():
    return redirect('https://wrongnoteflask.onrender.com/')


@app.route('/api/create-study-plan', methods=['POST'])
def create_study_plan():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': '로그인이 필요합니다.'}), 401

    payload = request.get_json(silent=True) or {}

    title = str(payload.get('title') or '').strip()
    subject = str(payload.get('subject') or '').strip()
    start_date = str(payload.get('start_date') or '').strip()
    end_date = str(payload.get('end_date') or '').strip()
    image_url = str(payload.get('image_url') or '').strip()
    link_url = str(payload.get('link_url') or '').strip()
    selected_weekdays = payload.get('weekdays') or []

    if not title:
        return jsonify({'ok': False, 'error': '계획 제목을 입력하세요.'}), 400
    if not subject:
        return jsonify({'ok': False, 'error': '과목명을 입력하세요.'}), 400
    if not start_date or not end_date:
        return jsonify({'ok': False, 'error': '시작일과 종료일을 입력하세요.'}), 400
    if not selected_weekdays:
        return jsonify({'ok': False, 'error': '요일을 하나 이상 선택하세요.'}), 400

    try:
        date_list = generate_date_list(start_date, end_date, selected_weekdays)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        table_columns = get_table_columns(conn, 'study_plan')
        user_id = int(session['user_id'])

        columns = []
        values = []
        if 'user_id' in table_columns:
            columns.append('user_id'); values.append(user_id)
        if 'title' in table_columns:
            columns.append('title'); values.append(title)
        if 'subject' in table_columns:
            columns.append('subject'); values.append(subject)
        if 'image_url' in table_columns:
            columns.append('image_url'); values.append(image_url or None)
        if 'color' in table_columns:
            columns.append('color'); values.append('#dfeafc')
        if 'created_at' in table_columns:
            columns.append('created_at'); values.append(datetime.now())

        if not columns:
            raise ValueError('study_plan 테이블에 필요한 컬럼이 없습니다.')

        plan_id = insert_plan_and_get_id(cursor, columns, values)
        if plan_id is None:
            conn.rollback()
            raise ValueError('study_plan의 생성된 식별자를 찾지 못했습니다. PK 또는 Identity 컬럼을 확인하세요.')

        task_columns = get_table_columns(conn, 'study_plan_task')
        task_insert_sql = []
        for idx, day in enumerate(date_list, start=1):
            task_values = []
            task_columns_for_sql = []
            if 'plan_id' in task_columns:
                task_columns_for_sql.append('plan_id'); task_values.append(plan_id)
            if 'plan_date' in task_columns:
                task_columns_for_sql.append('plan_date'); task_values.append(day)
            if 'task_title' in task_columns:
                task_columns_for_sql.append('task_title'); task_values.append(title)
            if 'order_no' in task_columns:
                task_columns_for_sql.append('order_no'); task_values.append(idx)
            if 'created_at' in task_columns:
                task_columns_for_sql.append('created_at'); task_values.append(datetime.now())
            if not task_columns_for_sql:
                raise ValueError('study_plan_task 테이블에 필요한 컬럼이 없습니다.')

            sql = (
                f"INSERT INTO dbo.study_plan_task ({', '.join(task_columns_for_sql)}) "
                f"VALUES ({', '.join(['?'] * len(task_columns_for_sql))})"
            )
            cursor.execute(sql, task_values)

        conn.commit()
        return jsonify({'ok': True, 'count': len(date_list), 'plan_id': plan_id})

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(exc)}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.route('/api/save-info', methods=['POST'])
def save_info():
    body = request.get_json(silent=True) or {}
    data = load_data()
    if 'title'       in body: data['title']       = str(body['title']).strip()
    if 'description' in body: data['description'] = str(body['description']).strip()
    save_data(data)
    return jsonify({'ok': True})


@app.route('/api/upload-image', methods=['POST'])
def upload_image():
    file = request.files.get('image')
    if not file or not allowed(file.filename):
        return jsonify({'error': '이미지 파일만 업로드 가능합니다.'}), 400

    filename = secure_filename(file.filename)
    base, ext = os.path.splitext(filename)
    counter, target = 1, UPLOAD_DIR / filename
    while target.exists():
        filename = f'{base}_{counter}{ext}'
        target   = UPLOAD_DIR / filename
        counter += 1

    file.save(target)
    data = load_data()
    data['images'].append(filename)
    save_data(data)
    return jsonify({'ok': True, 'filename': filename}), 201


@app.route('/api/delete-image', methods=['POST'])
def delete_image():
    filename = str((request.get_json(silent=True) or {}).get('filename') or '').strip()
    if not filename:
        return jsonify({'error': '파일명이 필요합니다.'}), 400
    data = load_data()
    if filename in data['images']:
        data['images'].remove(filename)
        save_data(data)
    target = UPLOAD_DIR / filename
    if target.exists():
        target.unlink()
    return jsonify({'ok': True})


if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(host=HOST, port=PORT, debug=debug)
