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
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding='utf-8'))
    return {'title': '앱 이름을 입력하세요', 'description': '앱 설명을 입력하세요.', 'images': []}


def save_data(data: dict):
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
        rows = conn.cursor().execute(
            """
            SELECT plan_id, subject, title
            FROM dbo.study_plan
            WHERE user_id = ? AND subject IS NOT NULL AND LTRIM(RTRIM(subject)) <> ''
            ORDER BY subject, plan_id
            """,
            (user_id,),
        ).fetchall()
        conn.close()

        grouped = {}
        for plan_id, subject_name, title in rows:
            subject_name = (subject_name or '').strip()
            if not subject_name:
                continue
            entry = grouped.setdefault(subject_name, {'name': subject_name, 'titles': [], 'plan_count': 0})
            entry['plan_count'] += 1
            if title:
                seen = {item['title'] for item in entry['titles']}
                if title not in seen:
                    entry['titles'].append({'title': title, 'plan_id': int(plan_id)})

        subjects = []
        for subject_name, entry in grouped.items():
            percent = min(100, max(20, int(entry['plan_count']) * 25))
            subjects.append({
                'name': subject_name,
                'percent': percent,
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
        task_columns = get_table_columns(conn, 'study_plan_task')
        select_sql = """
            SELECT sp.plan_id, sp.subject, sp.title, st.task_id, st.plan_date, st.task_title, st.order_no
        """
        if 'link_url' in task_columns:
            select_sql += ", st.link_url"
        select_sql += """
            FROM dbo.study_plan sp
            INNER JOIN dbo.study_plan_task st ON st.plan_id = sp.plan_id
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
            }
            if 'link_url' in task_columns:
                task['link_url'] = row[7] if len(row) > 7 else None
            tasks.append(task)
        return tasks
    except Exception:
        return []


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
    overall = 0
    if subjects:
        overall = round(sum(item['percent'] for item in subjects) / len(subjects))

    return render_template(
        'mobile_study_home.html',
        user_name=user_name,
        subjects=subjects,
        user_id=user_id,
        total_subjects=total_subjects,
        overall=overall,
    )


def get_dday_items():
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
        normalized.append({
            'id': item.get('id', index + 1),
            'title': title,
            'target_date': target_date,
        })
    session['dday_items'] = normalized
    return normalized


@app.route('/dday', methods=['GET', 'POST'])
def dday_page():
    if not session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        title = str(request.form.get('dday_title') or '').strip()
        target_date = str(request.form.get('target_date') or '').strip()

        if title and target_date:
            items = get_dday_items()
            items.append({
                'id': max((int(item.get('id', 0)) for item in items), default=0) + 1,
                'title': title,
                'target_date': target_date,
            })
            session['dday_items'] = items

        return redirect(url_for('dday_page'))

    items = get_dday_items()
    today = date.today()
    processed = []
    for item in items:
        target = datetime.strptime(item['target_date'], '%Y-%m-%d').date()
        delta_days = (target - today).days
        if delta_days == 0:
            badge = 'D-Day'
        elif delta_days > 0:
            badge = f'D-{delta_days}'
        else:
            badge = f'D+{abs(delta_days)}'
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

    items = get_dday_items()
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

    rows = []
    for line in str(raw_text).splitlines():
        text = line.strip()
        if not text:
            continue

        pattern = r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})'
        match = re.search(pattern, text)
        if not match:
            continue

        date_value = normalize_date_value(match.group(1))
        if not date_value:
            continue

        title = text[:match.start()].strip()
        if not title:
            remainder = text[match.end():].strip()
            if remainder:
                title = remainder
            else:
                parts = [p.strip() for p in re.split(r'[\t|,]+', text) if p.strip()]
                if len(parts) >= 2:
                    title = ' '.join(parts[1:]).strip()

        if not title:
            continue

        rows.append((date_value, title))

    return rows


def get_plan_detail(plan_id):
    try:
        conn = get_db_conn()
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
            SELECT task_id, plan_id, plan_date, task_title, order_no
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

    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        existing_dates = set(
            row[0] for row in cursor.execute(
                "SELECT plan_date FROM dbo.study_plan_task WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        )

        for order_no, (plan_date, task_title) in enumerate(parsed_rows, start=1):
            if str(plan_date) in {str(existing_date) for existing_date in existing_dates}:
                continue
            cursor.execute(
                "INSERT INTO dbo.study_plan_task (plan_id, plan_date, task_title, order_no, created_at) VALUES (?, ?, ?, ?, SYSDATETIMEOFFSET())",
                (plan_id, plan_date, task_title, order_no),
            )
            existing_dates.add(str(plan_date))

        conn.commit()
        conn.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return redirect(url_for('plan_edit', plan_id=plan_id))

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

        try:
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE dbo.study_plan SET title = ?, subject = ? WHERE plan_id = ?",
                (title or detail['title'], subject or detail['subject'], plan_id),
            )

            for task_id, plan_date, task_title in zip(task_ids, task_dates, task_titles):
                if not task_id:
                    continue
                cursor.execute(
                    "UPDATE dbo.study_plan_task SET plan_date = ?, task_title = ? WHERE task_id = ?",
                    (plan_date or None, task_title or '', int(task_id)),
                )

            conn.commit()
            conn.close()
        except Exception:
            return redirect(url_for('dashboard'))

        return redirect(url_for('dashboard'))

    return render_template('plan_edit.html', plan=detail)


@app.route('/today')
def today_study():
    if not session.get('user_id'):
        return redirect(url_for('index'))

    target_date = request.args.get('date') or date.today().isoformat()
    tasks = get_today_tasks(session['user_id'], target_date)
    return render_template(
        'today_study.html',
        user_name=session.get('user_name', '사용자'),
        tasks=tasks,
        today_date=target_date,
    )


@app.route('/admin')
def admin():
    return render_template('admin.html', data=load_data())


@app.route('/api/create-study-plan', methods=['POST'])
def create_study_plan():
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
        user_id = get_or_create_user_id(conn)

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
