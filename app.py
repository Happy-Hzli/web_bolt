# --- File: app.py ---

import sqlite3
import requests
import uuid
import os
import socket
from datetime import datetime, timedelta, timezone
from flask import (Flask, render_template, jsonify, request, redirect, url_for,
                   session, g, flash, Response)

# --- App Configuration ---
DB_NAME = 'orders.db'
API_BASE_URL = 'https://5sim.net/v1'
ADMIN_USERNAME = ''
ADMIN_PASSWORD = ''
# MODIFIED: Set the public-facing URL here
PUBLIC_BASE_URL = ''

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- Datetime Adapter/Converter for SQLite ---
# This properly handles the DeprecationWarning and ensures datetimes are objects
sqlite3.register_adapter(datetime, lambda val: val.isoformat())
sqlite3.register_converter("DATETIME", lambda val: datetime.fromisoformat(val.decode()))


# --- Database Functions ---
def get_db_conn():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_NAME, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# --- 5sim.net API Functions ---
def get_api_config_for_order(order_id):
    conn = get_db_conn()
    config = conn.execute(
        'SELECT ac.* FROM api_configs ac JOIN orders o ON ac.id = o.config_id WHERE o.id = ?',
        (order_id,)
    ).fetchone()
    return config


def get_phone_number_from_api(config):
    if not config: return None, None
    headers = {'Authorization': f'Bearer {config["api_token"]}', 'Accept': 'application/json'}
    url = f"{API_BASE_URL}/user/buy/activation/{config['country']}/{config['operator']}/{config['product']}"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data.get("phone"), data.get("id")
    except requests.RequestException as e:
        print(f"API Error (get_phone_number): {e}")
    return None, None


# --- Admin Routes ---
@app.route('/admin')
def admin_index():
    return redirect(url_for('admin_login'))


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if 'logged_in' in session:
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        if request.form['username'] == ADMIN_USERNAME and request.form['password'] == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            flash('凭据无效，请重试。', 'danger')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('logged_in', None)
    flash('您已成功退出登录。', 'success')
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
def admin_dashboard():
    if not session.get('logged_in'): return redirect(url_for('admin_login'))
    conn = get_db_conn()
    config_filter = request.args.get('config_filter', 'all')
    base_query = (
        'SELECT o.id, o.status, o.first_used_at, o.phone_number, o.replacement_count, ac.template_name '
        'FROM orders o JOIN api_configs ac ON o.config_id = ac.id'
    )
    query_params = []
    if config_filter.isdigit():
        base_query += ' WHERE o.config_id = ?'
        query_params.append(int(config_filter))
    base_query += ' ORDER BY CASE o.status WHEN 1 THEN 0 WHEN 0 THEN 1 ELSE 2 END, o.first_used_at DESC'
    orders = conn.execute(base_query, query_params).fetchall()
    api_configs = conn.execute('SELECT id, template_name FROM api_configs ORDER BY template_name').fetchall()
    return render_template('admin_dashboard.html', orders=orders, api_configs=api_configs, base_url=PUBLIC_BASE_URL,
                           current_filter=config_filter)


@app.route('/admin/generate', methods=['POST'])
def admin_generate_links():
    if not session.get('logged_in'): return redirect(url_for('admin_login'))
    config_id = request.form.get('config_id')
    num_to_generate_str = request.form.get('num_links', '10')
    if not config_id:
        flash('您必须选择一个API配置模板才能生成链接。', 'danger')
        return redirect(url_for('admin_dashboard'))
    try:
        num_to_generate = int(num_to_generate_str)
        conn = get_db_conn()
        for _ in range(num_to_generate):
            order_id = str(uuid.uuid4())
            conn.execute("INSERT INTO orders (id, config_id, status, replacement_count) VALUES (?, ?, 0, 0)",
                         (order_id, config_id))
        conn.commit()
        flash(f'成功生成了 {num_to_generate} 个新链接！', 'success')
    except (ValueError, TypeError):
        flash('指定的链接数量无效。', 'danger')
    return redirect(url_for('admin_dashboard', config_filter=config_id))


@app.route('/admin/export_selected', methods=['POST'])
def admin_export_selected():
    if not session.get('logged_in'): return jsonify({'success': False, 'message': '未授权'}), 401
    data = request.get_json()
    order_ids = data.get('order_ids', [])
    if not order_ids: return "No links selected for export.", 400
    links = [f"{PUBLIC_BASE_URL}order/{order_id}\n" for order_id in order_ids]
    response = Response("".join(links), mimetype="text/plain")
    response.headers.set("Content-Disposition", "attachment", filename="selected_links.txt")
    return response


@app.route('/admin/delete_orders', methods=['POST'])
def admin_delete_orders():
    if not session.get('logged_in'): return jsonify({'success': False, 'message': '未授权'}), 401
    data = request.get_json()
    order_ids = data.get('order_ids', [])
    if not order_ids: return jsonify({'success': False, 'message': '没有提供要删除的ID'}), 400
    try:
        conn = get_db_conn()
        placeholders = ', '.join('?' for _ in order_ids)
        query = f'DELETE FROM orders WHERE id IN ({placeholders})'
        conn.execute(query, order_ids)
        conn.commit()
        flash(f'成功删除了 {len(order_ids)} 个链接。', 'success')
        return jsonify({'success': True, 'message': '链接已删除'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/configs', methods=['GET'])
def admin_configs():
    if not session.get('logged_in'): return redirect(url_for('admin_login'))
    conn = get_db_conn()
    configs = conn.execute('SELECT * FROM api_configs ORDER BY template_name').fetchall()
    return render_template('admin_configs.html', configs=configs)


@app.route('/admin/configs/add', methods=['POST'])
def admin_add_config():
    if not session.get('logged_in'): return redirect(url_for('admin_login'))
    try:
        conn = get_db_conn()
        conn.execute(
            'INSERT INTO api_configs (template_name, api_token, country, operator, product, country_display_name, country_area_code) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (request.form['template_name'],
             request.form['api_token'],
             request.form['country'],
             request.form['operator'],
             request.form['product'],
             request.form['country_display_name'],
             request.form['country_area_code'])
        )
        conn.commit()
        flash(f"API配置模板 '{request.form['template_name']}' 添加成功！", 'success')
    except sqlite3.IntegrityError:
        flash(f"错误：名为 '{request.form['template_name']}' 的模板已存在。", 'danger')
    except Exception as e:
        flash(f"发生错误：{e}", 'danger')
    return redirect(url_for('admin_configs'))


@app.route('/admin/configs/delete/<int:config_id>', methods=['POST'])
def admin_delete_config():
    if not session.get('logged_in'):
        return redirect(url_for('admin_login'))

    conn = get_db_conn()
    try:
        conn.execute('DELETE FROM api_configs WHERE id = ?', (config_id,))
        conn.commit()
        flash('API配置模板及其所有关联链接已成功删除。', 'success')
    except Exception as e:
        flash(f"删除时发生错误：{e}", 'danger')

    return redirect(url_for('admin_configs'))


# --- User-Facing Routes ---
@app.route('/order/<string:order_id>')
def get_order_page(order_id):
    conn = get_db_conn()
    order_details = conn.execute(
        'SELECT o.*, ac.country_display_name, ac.country_area_code '
        'FROM orders o JOIN api_configs ac ON o.config_id = ac.id '
        'WHERE o.id = ?',
        (order_id,)
    ).fetchone()

    if not order_details: return "链接无效或不存在！", 404

    template_data = dict(order_details)
    template_data['order_id'] = order_id

    if order_details['status'] == 0:
        api_config = get_api_config_for_order(order_id)
        if not api_config: return "此链接的配置丢失或无效，请联系技术支持。", 500
        phone_number, external_id = get_phone_number_from_api(api_config)
        if phone_number and external_id:
            first_used_time = datetime.now(timezone.utc)
            conn.execute("UPDATE orders SET phone_number=?, external_id=?, status=1, first_used_at=? WHERE id=?",
                         (phone_number, external_id, first_used_time, order_id))
            conn.commit()
            template_data['phone_number'] = phone_number
            return render_template('order.html', **template_data)
        else:
            return "无法从服务商获取手机号，请刷新页面重试。", 500
    else:
        return render_template('order.html', **template_data)


# --- API Routes ---
@app.route('/api/order/get_new_number', methods=['POST'])
def get_new_number():
    data = request.get_json()
    order_id = data.get('order_id')
    if not order_id: return jsonify({'success': False, 'message': '缺少订单ID'}), 400

    conn = get_db_conn()
    order = conn.execute('SELECT replacement_count, verification_code FROM orders WHERE id = ?', (order_id,)).fetchone()

    if not order:
        return jsonify({'success': False, 'message': '找不到原始订单'}), 404

    if order['verification_code']:
        return jsonify({'success': False, 'message': '已收到验证码，无法更换号码'}), 403

    if order['replacement_count'] >= 3:
        return jsonify({'success': False, 'message': '已达到最大更换次数'}), 403

    api_config = get_api_config_for_order(order_id)
    if not api_config: return jsonify({'success': False, 'message': '找不到API配置'}), 500

    new_phone, new_external_id = get_phone_number_from_api(api_config)

    if new_phone and new_external_id:
        new_count = order['replacement_count'] + 1
        conn.execute(
            'UPDATE orders SET phone_number=?, external_id=?, replacement_count=?, verification_code=NULL, status=1, first_used_at=? WHERE id=?',
            (new_phone, new_external_id, new_count, datetime.now(timezone.utc), order_id)
        )
        conn.commit()
        return jsonify({'success': True, 'new_phone_number': new_phone, 'new_replacement_count': new_count})
    else:
        return jsonify({'success': False, 'message': '无法从服务商获取新号码'}), 500


@app.route('/api/check_code/<string:order_id>')
def check_verification_code(order_id):
    conn = get_db_conn()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order or not order['external_id']: return jsonify({'found': False, 'message': '订单未正确初始化。'})
    if order['first_used_at']:
        first_used_time = order['first_used_at'].replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > first_used_time + timedelta(minutes=20): return jsonify({'timeout': True})
    if order['verification_code']: return jsonify({'success': True, 'found': True, 'code': order['verification_code']})

    api_config = get_api_config_for_order(order_id)
    if not api_config: return jsonify({'found': False, 'message': 'API配置丢失。'})

    headers = {'Authorization': f'Bearer {api_config["api_token"]}', 'Accept': 'application/json'}
    url = f"{API_BASE_URL}/user/check/{order['external_id']}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        check_data = response.json()
        if check_data.get("sms") and len(check_data["sms"]) > 0 and check_data["sms"][0].get("code"):
            sms_code = check_data["sms"][0]["code"]
            conn.execute("UPDATE orders SET verification_code = ? WHERE id = ?", (sms_code, order_id))
            conn.commit()
            return jsonify({'success': True, 'found': True, 'code': sms_code})
    except Exception as e:
        print(f"API Error (check_code): {e}")
    return jsonify({'success': True, 'found': False})


@app.route('/api/reset_code/<string:order_id>', methods=['POST'])
def reset_verification_code(order_id):
    conn = get_db_conn()
    conn.execute("UPDATE orders SET verification_code = NULL WHERE id = ?", (order_id,))
    conn.commit()
    return jsonify({'success': True})


def get_local_ip():
    """
    获取本机的内网IP地址。
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        if s:
            s.close()
    return ip


if __name__ == '__main__':
    # --- 服务器配置 ---
    HOST = '0.0.0.0'  # 监听所有网络接口，允许外部访问
    PORT = 7000

    # --- 获取访问地址 ---
    local_ip = get_local_ip()
    public_url_base = PUBLIC_BASE_URL.split('//')[1].split(':')[0]

    print("--- 验证码系统 v17 (智能启动版) ---")
    print("\n[INFO] 服务器正在启动...")
    print(f"[INFO] 监听地址: http://{HOST}:{PORT}")
    print("-" * 40)
    print("✅ 您可以通过以下地址访问您的应用：\n")
    print(f"   - 本机访问: http://127.0.0.1:{PORT}/admin")
    if local_ip != '127.0.0.1':
        print(f"   - 局域网访问: http://{local_ip}:{PORT}/admin")
    print(f"   - 公网访问: http://{public_url_base}:{PORT}/admin")
    print("-" * 40)

    print("⚠️  重要提示:\n")
    print("   1. 如果公网地址无法访问，请检查您的服务器提供商（如阿里云、腾讯云等）的")
    print("      '安全组' 或 '防火墙' 设置，确保 TCP 7000 端口已对公网开放。")
    print("\n   2. 确保您的应用服务正在前台运行，不要关闭此终端窗口。")
    print("-" * 40)

    # 启动 Flask 应用
    app.run(host=HOST, port=PORT, debug=True)
