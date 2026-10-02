import os
import csv
from io import BytesIO, StringIO
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, flash, request, send_file
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
import openpyxl

# ----------------------------------------------------------------------
# APP & DATABASE CONFIGURATION
# ----------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = 'enterprise-secret-key-change-this-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///store_inventory_web.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'warning'

# ----------------------------------------------------------------------
# DATABASE MODELS
# ----------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='Staff')  # Admin, Manager, Staff

    def set_password(self, password):
        self.password_hash = generate_password_hash(password.strip())

    def check_password(self, password):
        return check_password_hash(self.password_hash, password.strip())


class Item(db.Model):
    __tablename__ = 'items'
    id = db.Column(db.Integer, primary_key=True)
    item_code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    unit = db.Column(db.String(50), nullable=False, default='Pcs')
    opening_stock = db.Column(db.Float, nullable=False, default=0.0)
    rate = db.Column(db.Float, nullable=False, default=0.0)
    reorder_level = db.Column(db.Float, nullable=False, default=10.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    stock_ins = db.relationship('StockInTransaction', backref='item', lazy=True, cascade="all, delete-orphan")
    stock_outs = db.relationship('StockOutTransaction', backref='item', lazy=True, cascade="all, delete-orphan")


class StockInTransaction(db.Model):
    __tablename__ = 'stock_in_transactions'
    id = db.Column(db.Integer, primary_key=True)
    txn_date = db.Column(db.Date, nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    rate = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    notes = db.Column(db.String(255), nullable=True)


class StockOutTransaction(db.Model):
    __tablename__ = 'stock_out_transactions'
    id = db.Column(db.Integer, primary_key=True)
    txn_date = db.Column(db.Date, nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    rate = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    notes = db.Column(db.String(255), nullable=True)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ----------------------------------------------------------------------
# ROLE-BASED ACCESS CONTROL (RBAC) DECORATORS
# ----------------------------------------------------------------------
def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                flash("Access denied: You do not have permission to view this page.", "danger")
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ----------------------------------------------------------------------
# HELPER SERVICES
# ----------------------------------------------------------------------
def get_inventory_summary():
    items = Item.query.all()
    summary = []
    for item in items:
        in_qty = db.session.query(db.func.coalesce(db.func.sum(StockInTransaction.quantity), 0.0)).filter(StockInTransaction.item_id == item.id).scalar()
        out_qty = db.session.query(db.func.coalesce(db.func.sum(StockOutTransaction.quantity), 0.0)).filter(StockOutTransaction.item_id == item.id).scalar()
        
        remaining = item.opening_stock + in_qty - out_qty
        valuation = remaining * item.rate
        is_low_stock = remaining <= item.reorder_level
        
        summary.append({
            'id': item.id,
            'code': item.item_code,
            'name': item.name,
            'unit': item.unit,
            'rate': item.rate,
            'opening': item.opening_stock,
            'in_qty': in_qty,
            'out_qty': out_qty,
            'remaining': remaining,
            'valuation': valuation,
            'reorder_level': item.reorder_level,
            'is_low_stock': is_low_stock
        })
    return summary

# ----------------------------------------------------------------------
# ROUTES & CONTROLLERS
# ----------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        
        # Case-insensitive search
        user = User.query.filter(db.func.lower(User.username) == db.func.lower(username)).first()
        
        if user and user.check_password(password):
            login_user(user)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid username or password.", "danger")
            
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    summary = get_inventory_summary()
    total_items = len(summary)
    total_value = sum(x['valuation'] for x in summary)
    low_stock_count = sum(1 for x in summary if x['is_low_stock'])
    total_units = sum(x['remaining'] for x in summary)
    
    return render_template(
        'dashboard.html',
        summary=summary,
        total_items=total_items,
        total_value=total_value,
        low_stock_count=low_stock_count,
        total_units=total_units
    )


@app.route('/items', methods=['GET', 'POST'])
@login_required
@roles_required('Admin', 'Manager')
def items():
    if request.method == 'POST':
        code = request.form.get('item_code').strip()
        name = request.form.get('name').strip()
        unit = request.form.get('unit').strip() or 'Pcs'
        opening = float(request.form.get('opening_stock') or 0.0)
        rate = float(request.form.get('rate') or 0.0)
        reorder = float(request.form.get('reorder_level') or 10.0)

        existing = Item.query.filter(db.func.lower(Item.item_code) == db.func.lower(code)).first()
        if existing:
            flash(f"Item Code '{code}' already exists.", "danger")
        else:
            new_item = Item(
                item_code=code, name=name, unit=unit,
                opening_stock=opening, rate=rate, reorder_level=reorder
            )
            db.session.add(new_item)
            db.session.commit()
            flash("New Item created successfully!", "success")
            return redirect(url_for('items'))

    items_list = Item.query.all()
    return render_template('items.html', items=items_list)


@app.route('/items/edit/<int:item_id>', methods=['POST'])
@login_required
@roles_required('Admin', 'Manager')
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    item.item_code = request.form.get('item_code').strip()
    item.name = request.form.get('name').strip()
    item.unit = request.form.get('unit').strip() or 'Pcs'
    item.opening_stock = float(request.form.get('opening_stock') or 0.0)
    item.rate = float(request.form.get('rate') or 0.0)
    item.reorder_level = float(request.form.get('reorder_level') or 10.0)
    
    db.session.commit()
    flash(f"Item '{item.name}' updated successfully!", "success")
    return redirect(url_for('items'))


@app.route('/items/delete/<int:item_id>', methods=['POST'])
@login_required
@roles_required('Admin')
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash(f"Item '{item.name}' deleted successfully!", "success")
    return redirect(url_for('items'))


@app.route('/stock-in', methods=['GET', 'POST'])
@login_required
def stock_in():
    if request.method == 'POST':
        item_id = int(request.form.get('item_id'))
        txn_date = datetime.strptime(request.form.get('txn_date'), '%Y-%m-%d').date()
        qty = float(request.form.get('quantity'))
        rate = float(request.form.get('rate'))
        notes = request.form.get('notes')

        total = qty * rate
        txn = StockInTransaction(
            item_id=item_id, txn_date=txn_date, quantity=qty,
            rate=rate, total_amount=total, notes=notes
        )
        db.session.add(txn)
        db.session.commit()
        flash("Stock IN transaction recorded.", "success")
        return redirect(url_for('stock_in'))

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    query = StockInTransaction.query
    if start_date:
        query = query.filter(StockInTransaction.txn_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
    if end_date:
        query = query.filter(StockInTransaction.txn_date <= datetime.strptime(end_date, '%Y-%m-%d').date())
        
    logs = query.order_by(StockInTransaction.txn_date.desc()).all()
    all_items = Item.query.all()
    return render_template('stock_in.html', logs=logs, items=all_items, start_date=start_date, end_date=end_date)


@app.route('/stock-out', methods=['GET', 'POST'])
@login_required
def stock_out():
    if request.method == 'POST':
        item_id = int(request.form.get('item_id'))
        txn_date = datetime.strptime(request.form.get('txn_date'), '%Y-%m-%d').date()
        qty = float(request.form.get('quantity'))
        rate = float(request.form.get('rate'))
        notes = request.form.get('notes')

        summary = [s for s in get_inventory_summary() if s['id'] == item_id]
        if summary and summary[0]['remaining'] < qty:
            flash(f"Negative Stock Prevented! Requested: {qty}, Available: {summary[0]['remaining']}", "danger")
            return redirect(url_for('stock_out'))

        total = qty * rate
        txn = StockOutTransaction(
            item_id=item_id, txn_date=txn_date, quantity=qty,
            rate=rate, total_amount=total, notes=notes
        )
        db.session.add(txn)
        db.session.commit()
        flash("Stock OUT transaction recorded.", "success")
        return redirect(url_for('stock_out'))

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    query = StockOutTransaction.query
    if start_date:
        query = query.filter(StockOutTransaction.txn_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
    if end_date:
        query = query.filter(StockOutTransaction.txn_date <= datetime.strptime(end_date, '%Y-%m-%d').date())
        
    logs = query.order_by(StockOutTransaction.txn_date.desc()).all()
    all_items = Item.query.all()
    return render_template('stock_out.html', logs=logs, items=all_items, start_date=start_date, end_date=end_date)


@app.route('/users', methods=['GET', 'POST'])
@login_required
@roles_required('Admin')
def users():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        role = request.form.get('role')

        if User.query.filter(db.func.lower(User.username) == db.func.lower(username)).first():
            flash("Username already taken.", "danger")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"User '{username}' created with role '{role}'.", "success")
            return redirect(url_for('users'))

    users_list = User.query.all()
    return render_template('users.html', users=users_list)


@app.route('/users/edit/<int:user_id>', methods=['POST'])
@login_required
@roles_required('Admin')
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    user.role = request.form.get('role')
    db.session.commit()
    flash(f"Updated role for user '{user.username}'.", "success")
    return redirect(url_for('users'))


@app.route('/users/change-password/<int:user_id>', methods=['POST'])
@login_required
@roles_required('Admin')
def change_user_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = (request.form.get('new_password') or '').strip()
    if new_password:
        user.set_password(new_password)
        db.session.commit()
        flash(f"Password updated successfully for '{user.username}'.", "success")
    else:
        flash("Password cannot be empty.", "danger")
    return redirect(url_for('users'))


@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@roles_required('Admin')
def delete_user(user_id):
    if user_id == current_user.id:
        flash("You cannot delete your own logged-in account!", "danger")
        return redirect(url_for('users'))
    
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.username}' deleted successfully.", "success")
    return redirect(url_for('users'))


@app.route('/export/excel')
@login_required
@roles_required('Admin', 'Manager')
def export_excel():
    summary = get_inventory_summary()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Current Stock Status"

    ws.append(["Item Code", "Item Name", "Unit", "Unit Rate", "Opening Stock", "Stock IN", "Stock OUT", "Remaining Stock", "Total Valuation", "Status"])
    for row in summary:
        status = "LOW STOCK" if row['is_low_stock'] else "OK"
        ws.append([
            row['code'], row['name'], row['unit'], row['rate'],
            row['opening'], row['in_qty'], row['out_qty'],
            row['remaining'], row['valuation'], status
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, download_name="Stock_Report.xlsx", as_attachment=True, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route('/export/csv')
@login_required
@roles_required('Admin', 'Manager')
def export_csv():
    summary = get_inventory_summary()
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Item Code", "Item Name", "Unit", "Rate", "Opening Stock", "In Qty", "Out Qty", "Remaining", "Valuation", "Status"])
    for row in summary:
        status = "LOW STOCK" if row['is_low_stock'] else "OK"
        cw.writerow([
            row['code'], row['name'], row['unit'], row['rate'],
            row['opening'], row['in_qty'], row['out_qty'], row['remaining'], row['valuation'], status
        ])

    output = BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    return send_file(output, download_name="Stock_Report.csv", as_attachment=True, mimetype="text/csv")


# ----------------------------------------------------------------------
# INITIALIZATION & SEEDING
# ----------------------------------------------------------------------
def init_app():
    with app.app_context():
        db.create_all()
        if not User.query.first():
            admin = User(username='admin', role='Admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print("--> Seeded default Admin user (username: admin, password: admin123)")

if __name__ == '__main__':
    init_app()
    app.run(host='0.0.0.0', port=5000, debug=True)