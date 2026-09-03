import os
import sqlite3
import asyncio
import math
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавь в переменные окружения.")

# ==================== БАЗА ДАННЫХ ====================
conn = sqlite3.connect('credit_planner.db', check_same_thread=False)
cursor = conn.cursor()

# Создаем все таблицы
cursor.executescript('''
-- Пользователи
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Доходы (зарплата)
CREATE TABLE IF NOT EXISTS incomes (
    income_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    source TEXT,
    amount REAL,
    day_of_month INTEGER,
    currency TEXT DEFAULT 'RUB',
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Кредиты
CREATE TABLE IF NOT EXISTS credits (
    credit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    creditor TEXT,
    total_amount REAL,
    interest_rate REAL,
    start_date DATE,
    term_months INTEGER,
    monthly_payment REAL,
    remaining_debt REAL,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- График платежей
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_id INTEGER,
    payment_number INTEGER,
    due_date DATE,
    planned_amount REAL,
    principal_amount REAL,
    interest_amount REAL,
    status TEXT DEFAULT 'pending',
    FOREIGN KEY (credit_id) REFERENCES credits(credit_id)
);

-- История платежей
CREATE TABLE IF NOT EXISTS payments (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_id INTEGER,
    amount REAL,
    payment_date DATE,
    type TEXT DEFAULT 'planned',
    comment TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (credit_id) REFERENCES credits(credit_id)
);

-- Бюджет на месяц
CREATE TABLE IF NOT EXISTS monthly_budget (
    budget_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    year INTEGER,
    month INTEGER,
    total_income REAL,
    total_credit_payments REAL,
    free_money REAL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
''')
conn.commit()

# ==================== СОСТОЯНИЯ FSM ====================
class AddCreditStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_creditor = State()
    waiting_for_amount = State()
    waiting_for_rate = State()
    waiting_for_date = State()
    waiting_for_term = State()

class AddPaymentStates(StatesGroup):
    waiting_for_credit_id = State()
    waiting_for_amount = State()
    waiting_for_date = State()

class SalaryStates(StatesGroup):
    waiting_for_source = State()
    waiting_for_amount = State()
    waiting_for_day = State()

# ==================== УТИЛИТЫ ====================
months_ru = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
             'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

def calculate_monthly_payment(amount, annual_rate, months):
    """Расчет аннуитетного платежа"""
    if annual_rate == 0:
        return amount / months
    monthly_rate = annual_rate / 100 / 12
    payment = amount * (monthly_rate * (1 + monthly_rate) ** months) / ((1 + monthly_rate) ** months - 1)
    return round(payment, 2)

def generate_schedule(credit_id, amount, annual_rate, months, start_date):
    """Генерация графика платежей"""
    monthly_payment = calculate_monthly_payment(amount, annual_rate, months)
    remaining = amount
    monthly_rate = annual_rate / 100 / 12
    
    cursor.execute("DELETE FROM schedules WHERE credit_id = ?", (credit_id,))
    
    for i in range(1, months + 1):
        interest = remaining * monthly_rate if annual_rate > 0 else 0
        principal = monthly_payment - interest
        if i == months:
            principal = remaining
            interest = 0
            monthly_payment = principal + interest
        
        due_date_obj = datetime.strptime(start_date, '%Y-%m-%d') + timedelta(days=30*i)
        due_date = due_date_obj.strftime('%Y-%m-%d')
        
        cursor.execute('''
            INSERT INTO schedules (credit_id, payment_number, due_date, planned_amount, principal_amount, interest_amount)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (credit_id, i, due_date, round(monthly_payment, 2), round(principal, 2), round(interest, 2)))
        
        remaining -= principal
        if remaining < 0.01:
            remaining = 0
    
    conn.commit()
    return monthly_payment

# ==================== ФУНКЦИИ БАЗЫ ДАННЫХ ====================
def get_or_create_user(user_id, username):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username or str(user_id)))
        conn.commit()
    return user

def get_user_credits(user_id):
    cursor.execute('''
        SELECT credit_id, name, creditor, total_amount, remaining_debt, 
               interest_rate, monthly_payment, status, start_date, term_months
        FROM credits 
        WHERE user_id = ? AND status = 'active'
        ORDER BY start_date
    ''', (user_id,))
    return cursor.fetchall()

def get_all_credits(user_id):
    cursor.execute('''
        SELECT credit_id, name, creditor, total_amount, remaining_debt, 
               interest_rate, monthly_payment, status, start_date, term_months
        FROM credits 
        WHERE user_id = ?
        ORDER BY status DESC, start_date
    ''', (user_id,))
    return cursor.fetchall()

def get_credit_by_id(credit_id):
    cursor.execute('SELECT * FROM credits WHERE credit_id = ?', (credit_id,))
    return cursor.fetchone()

def get_next_payments(user_id, days=30):
    today = date.today().isoformat()
    future = (date.today() + timedelta(days=days)).isoformat()
    
    cursor.execute('''
        SELECT s.*, c.name, c.creditor, c.user_id
        FROM schedules s
        JOIN credits c ON s.credit_id = c.credit_id
        WHERE c.user_id = ? AND s.due_date BETWEEN ? AND ?
        AND s.status = 'pending'
        ORDER BY s.due_date
    ''', (user_id, today, future))
    return cursor.fetchall()

def get_total_debt(user_id):
    cursor.execute("SELECT SUM(remaining_debt) FROM credits WHERE user_id = ? AND status = 'active'", (user_id,))
    result = cursor.fetchone()[0]
    return result if result else 0

def get_user_incomes(user_id):
    cursor.execute('''
        SELECT income_id, source, amount, day_of_month 
        FROM incomes 
        WHERE user_id = ? AND is_active = 1
        ORDER BY day_of_month
    ''', (user_id,))
    return cursor.fetchall()

def calculate_monthly_budget(user_id, year, month):
    """Рассчитать бюджет на месяц"""
    incomes = get_user_incomes(user_id)
    total_income = sum([inc[2] for inc in incomes])
    
    start_date = date(year, month, 1).isoformat()
    if month == 12:
        end_date = date(year + 1, 1, 1).isoformat()
    else:
        end_date = date(year, month + 1, 1).isoformat()
    
    cursor.execute('''
        SELECT SUM(planned_amount)
        FROM schedules s
        JOIN credits c ON s.credit_id = c.credit_id
        WHERE c.user_id = ? AND s.due_date >= ? AND s.due_date < ?
        AND s.status = 'pending'
    ''', (user_id, start_date, end_date))
    total_payments = cursor.fetchone()[0] or 0
    
    free_money = total_income - total_payments
    
    cursor.execute('''
        INSERT OR REPLACE INTO monthly_budget (user_id, year, month, total_income, total_credit_payments, free_money)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, year, month, total_income, total_payments, free_money))
    conn.commit()
    
    return total_income, total_payments, free_money

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ==================== ХЭНДЛЕРЫ КОМАНД ====================

# ---------- СТАРТ ----------
@dp.message(Command("start"))
async def start(message: Message):
    get_or_create_user(message.from_user.id, message.from_user.username)
    total_debt = get_total_debt(message.from_user.id)
    
    today = date.today()
    income, payments, free = calculate_monthly_budget(message.from_user.id, today.year, today.month)
    
    next_pays = get_next_payments(message.from_user.id, 7)
    warning = ""
    if next_pays:
        next_pay = next_pays[0]
        due_date = next_pay[3]
        days_left = (datetime.strptime(due_date, '%Y-%m-%d').date() - date.today()).days
        if days_left <= 3:
            warning = f"\n⚠️ *Внимание!* Платеж по кредиту *{next_pay[10]}* через {days_left} дня!"
    
    await message.answer(
        f"🏦 *Кредитный Планировщик*\n\n"
        f"Привет, {message.from_user.first_name}!\n"
        f"💰 Общая задолженность: *{total_debt:,.2f} руб.*\n"
        f"📊 Бюджет на {months_ru[today.month-1]}:\n"
        f"   Доход: *{income:,.2f} руб.*\n"
        f"   Платежи: *{payments:,.2f} руб.*\n"
        f"   Свободно: *{free:,.2f} руб.*\n"
        f"{warning}\n\n"
        f"📋 *Команды:*\n"
        f"/add — Добавить кредит\n"
        f"/list — Список кредитов\n"
        f"/pay — Внести платеж\n"
        f"/next — Ближайшие платежи\n"
        f"/forecast — Прогноз с зарплатой\n"
        f"/salary — Настроить зарплату\n"
        f"/my_salary — Мои доходы\n"
        f"/dashboard — Полная статистика\n"
        f"/optimize — Оптимизация платежей\n"
        f"/help — Помощь",
        parse_mode="Markdown"
    )

# ---------- ПОМОЩЬ ----------
@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "🏦 *Кредитный Планировщик — помощь*\n\n"
        "📌 *Управление кредитами:*\n"
        "/add — Добавить новый кредит (пошаговый мастер)\n"
        "/list — Список всех активных кредитов\n"
        "/pay — Внести платеж по кредиту\n"
        "/next — Ближайшие платежи (30 дней)\n"
        "/close_credit — Закрыть кредит досрочно\n\n"
        "💰 *Доходы и бюджет:*\n"
        "/salary — Добавить источник дохода\n"
        "/my_salary — Показать все доходы\n"
        "/forecast — Прогноз платежей с учетом зарплаты\n"
        "/optimize — Оптимизировать даты платежей\n\n"
        "📊 *Статистика:*\n"
        "/dashboard — Полная финансовая статистика\n"
        "/history — История всех платежей\n\n"
        "💡 *Совет:* Все данные хранятся локально, бот — удобный планировщик.",
        parse_mode="Markdown"
    )

# ---------- ДОБАВЛЕНИЕ КРЕДИТА ----------
@dp.message(Command("add"))
async def add_credit(message: Message, state: FSMContext):
    get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer("🏷️ Введи *название* кредита (например: 'Ипотека Сбербанк'):", parse_mode="Markdown")
    await state.set_state(AddCreditStates.waiting_for_name)

@dp.message(AddCreditStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("🏢 Кому должен? (Банк, друг, МФО):")
    await state.set_state(AddCreditStates.waiting_for_creditor)

@dp.message(AddCreditStates.waiting_for_creditor)
async def process_creditor(message: Message, state: FSMContext):
    await state.update_data(creditor=message.text)
    await message.answer("💰 Введи *общую сумму* кредита (в рублях, например: 1000000):", parse_mode="Markdown")
    await state.set_state(AddCreditStates.waiting_for_amount)

@dp.message(AddCreditStates.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.').replace(' ', ''))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await message.answer("📊 Введи *годовую ставку* в % (например: 15.5, если 0 — введи 0):", parse_mode="Markdown")
        await state.set_state(AddCreditStates.waiting_for_rate)
    except ValueError:
        await message.answer("❌ Введи положительное число!")

@dp.message(AddCreditStates.waiting_for_rate)
async def process_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(',', '.'))
        if rate < 0:
            raise ValueError
        await state.update_data(rate=rate)
        await message.answer("📅 Введи *дату начала* (в формате ГГГГ-ММ-ДД, например: 2026-01-15):", parse_mode="Markdown")
        await state.set_state(AddCreditStates.waiting_for_date)
    except ValueError:
        await message.answer("❌ Введи число (0 или больше)!")

@dp.message(AddCreditStates.waiting_for_date)
async def process_date(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text, '%Y-%m-%d')
        await state.update_data(start_date=message.text)
        await message.answer("🗓️ Введи *срок кредита* в месяцах (например: 60):", parse_mode="Markdown")
        await state.set_state(AddCreditStates.waiting_for_term)
    except ValueError:
        await message.answer("❌ Неверный формат! Используй ГГГГ-ММ-ДД")

@dp.message(AddCreditStates.waiting_for_term)
async def process_term(message: Message, state: FSMContext):
    try:
        months = int(message.text)
        if months <= 0:
            raise ValueError
        
        data = await state.get_data()
        
        monthly_payment = calculate_monthly_payment(data['amount'], data['rate'], months)
        
        cursor.execute('''
            INSERT INTO credits (user_id, name, creditor, total_amount, interest_rate, 
                                start_date, term_months, monthly_payment, remaining_debt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (message.from_user.id, data['name'], data['creditor'], data['amount'], 
              data['rate'], data['start_date'], months, monthly_payment, data['amount']))
        
        credit_id = cursor.lastrowid
        generate_schedule(credit_id, data['amount'], data['rate'], months, data['start_date'])
        conn.commit()
        
        today = date.today()
        calculate_monthly_budget(message.from_user.id, today.year, today.month)
        
        await message.answer(
            f"✅ *Кредит добавлен!*\n\n"
            f"Название: {data['name']}\n"
            f"Сумма: {data['amount']:,.2f} руб.\n"
            f"Ставка: {data['rate']}%\n"
            f"Срок: {months} мес.\n"
            f"Ежемесячный платеж: *{monthly_payment:,.2f} руб.*\n\n"
            f"📌 ID кредита: `{credit_id}`",
            parse_mode="Markdown"
        )
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Введи целое число (количество месяцев)!")

# ---------- СПИСОК КРЕДИТОВ ----------
@dp.message(Command("list"))
async def list_credits(message: Message):
    credits = get_user_credits(message.from_user.id)
    
    if not credits:
        await message.answer("📭 У тебя пока нет активных кредитов.")
        return
    
    text = "📋 *Твои активные кредиты:*\n\n"
    for credit in credits:
        credit_id, name, creditor, total, remaining, rate, payment, status, start, term = credit
        progress = int(((total - remaining) / total) * 100) if total > 0 else 0
        text += f"🔹 *{name}*\n"
        text += f"   Кому: {creditor}\n"
        text += f"   Остаток: *{remaining:,.2f} руб.* ({progress}% погашено)\n"
        text += f"   Платеж: {payment:,.2f} руб./мес.\n"
        text += f"   Ставка: {rate}%\n"
        text += f"   ID: `{credit_id}`\n\n"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- ВНЕСЕНИЕ ПЛАТЕЖА ----------
@dp.message(Command("pay"))
async def start_payment(message: Message, state: FSMContext):
    credits = get_user_credits(message.from_user.id)
    if not credits:
        await message.answer("❌ У тебя нет активных кредитов.")
        return
    
    text = "💳 *Внесение платежа*\n\nВыбери кредит по ID:\n"
    for credit in credits:
        text += f"ID `{credit[0]}` — {credit[1]} (остаток: {credit[4]:,.2f} руб.)\n"
    
    text += "\nВведи ID кредита:"
    await message.answer(text, parse_mode="Markdown")
    await state.set_state(AddPaymentStates.waiting_for_credit_id)

@dp.message(AddPaymentStates.waiting_for_credit_id)
async def process_payment_credit(message: Message, state: FSMContext):
    try:
        credit_id = int(message.text)
        credit = get_credit_by_id(credit_id)
        if not credit or credit[1] != message.from_user.id:
            await message.answer("❌ Кредит не найден!")
            return
        
        if credit[8] <= 0:
            await message.answer("✅ Этот кредит уже полностью погашен!")
            await state.clear()
            return
        
        await state.update_data(credit_id=credit_id)
        await message.answer(f"💰 Введи сумму платежа для кредита *{credit[3]}*:\n"
                            f"(Остаток долга: {credit[8]:,.2f} руб.)",
                            parse_mode="Markdown")
        await state.set_state(AddPaymentStates.waiting_for_amount)
    except ValueError:
        await message.answer("❌ Введи ID кредита (число)!")

@dp.message(AddPaymentStates.waiting_for_amount)
async def process_payment_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.').replace(' ', ''))
        if amount <= 0:
            raise ValueError
        
        await state.update_data(amount=amount)
        await message.answer("📅 Введи дату платежа (ГГГГ-ММ-ДД) или напиши `сегодня`:")
        await state.set_state(AddPaymentStates.waiting_for_date)
    except ValueError:
        await message.answer("❌ Введи положительное число!")

@dp.message(AddPaymentStates.waiting_for_date)
async def process_payment_date(message: Message, state: FSMContext):
    data = await state.get_data()
    
    if message.text.lower() == "сегодня":
        payment_date = date.today().isoformat()
    else:
        try:
            datetime.strptime(message.text, '%Y-%m-%d')
            payment_date = message.text
        except ValueError:
            await message.answer("❌ Неверный формат! Используй ГГГГ-ММ-ДД или напиши 'сегодня'")
            return
    
    credit = get_credit_by_id(data['credit_id'])
    new_remaining = credit[8] - data['amount']
    if new_remaining < 0:
        new_remaining = 0
    
    cursor.execute("UPDATE credits SET remaining_debt = ? WHERE credit_id = ?", 
                   (new_remaining, data['credit_id']))
    
    cursor.execute('''
        UPDATE schedules 
        SET status = 'paid' 
        WHERE credit_id = ? AND due_date <= ? AND status = 'pending'
        ORDER BY due_date
        LIMIT 1
    ''', (data['credit_id'], payment_date))
    
    cursor.execute('''
        INSERT INTO payments (credit_id, amount, payment_date, type)
        VALUES (?, ?, ?, 'actual')
    ''', (data['credit_id'], data['amount'], payment_date))
    
    if new_remaining == 0:
        cursor.execute("UPDATE credits SET status = 'closed' WHERE credit_id = ?", 
                      (data['credit_id'],))
        await message.answer(f"🎉 *Кредит полностью погашен!*\nСумма: {data['amount']:,.2f} руб.", 
                            parse_mode="Markdown")
    else:
        await message.answer(f"✅ *Платеж зачислен!*\n"
                            f"Сумма: {data['amount']:,.2f} руб.\n"
                            f"Остаток долга: *{new_remaining:,.2f} руб.*",
                            parse_mode="Markdown")
    
    conn.commit()
    
    today = date.today()
    calculate_monthly_budget(message.from_user.id, today.year, today.month)
    
    await state.clear()

# ---------- БЛИЖАЙШИЕ ПЛАТЕЖИ ----------
@dp.message(Command("next"))
async def next_payments(message: Message):
    payments = get_next_payments(message.from_user.id, 30)
    
    if not payments:
        await message.answer("📭 В ближайшие 30 дней платежей нет.")
        return
    
    text = "📅 *Ближайшие платежи (30 дней):*\n\n"
    for payment in payments:
        schedule_id, credit_id, num, due_date, plan_amount, principal, interest, status, name, creditor, user_id = payment
        days_left = (datetime.strptime(due_date, '%Y-%m-%d').date() - date.today()).days
        if days_left < 0:
            days_text = "🔴 *ПРОСРОЧЕН!*"
        elif days_left == 0:
            days_text = "🔴 *СЕГОДНЯ!*"
        elif days_left <= 3:
            days_text = f"⚠️ через {days_left} дн."
        else:
            days_text = f"через {days_left} дн."
        
        text += f"📌 *{name}* ({creditor})\n"
        text += f"   Платеж #{num}: {plan_amount:,.2f} руб.\n"
        text += f"   Дата: {due_date} ({days_text})\n\n"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- ЗАКРЫТЬ КРЕДИТ ----------
@dp.message(Command("close_credit"))
async def close_credit(message: Message):
    credits = get_user_credits(message.from_user.id)
    if not credits:
        await message.answer("❌ Нет активных кредитов для закрытия.")
        return
    
    text = "📌 *Закрытие кредита*\n\nВыбери кредит по ID:\n"
    for credit in credits:
        text += f"ID `{credit[0]}` — {credit[1]} (остаток: {credit[4]:,.2f} руб.)\n"
    
    text += "\nВведи ID кредита, который хочешь закрыть:"
    await message.answer(text, parse_mode="Markdown")
    
    @dp.message()
    async def close_credit_handler(message: Message):
        try:
            credit_id = int(message.text)
            credit = get_credit_by_id(credit_id)
            if not credit or credit[1] != message.from_user.id:
                await message.answer("❌ Кредит не найден!")
                return
            
            cursor.execute("UPDATE credits SET status = 'closed' WHERE credit_id = ?", (credit_id,))
            conn.commit()
            await message.answer(f"✅ Кредит *{credit[3]}* закрыт досрочно!", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Введи ID кредита (число)!")

# ---------- НАСТРОЙКА ЗАРПЛАТЫ ----------
@dp.message(Command("salary"))
async def setup_salary(message: Message, state: FSMContext):
    await message.answer(
        "💵 *Настройка дохода*\n\n"
        "Введи *название* источника (например: 'Зарплата', 'Подработка'):",
        parse_mode="Markdown"
    )
    await state.set_state(SalaryStates.waiting_for_source)

@dp.message(SalaryStates.waiting_for_source)
async def process_salary_source(message: Message, state: FSMContext):
    await state.update_data(source=message.text)
    await message.answer("💰 Введи *сумму* (в рублях):", parse_mode="Markdown")
    await state.set_state(SalaryStates.waiting_for_amount)

@dp.message(SalaryStates.waiting_for_amount)
async def process_salary_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.').replace(' ', ''))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await message.answer(
            "📅 Введи *число месяца* (1-31), когда приходит доход:\n"
            "Например: `10` или `25`",
            parse_mode="Markdown"
        )
        await state.set_state(SalaryStates.waiting_for_day)
    except ValueError:
        await message.answer("❌ Введи положительное число!")

@dp.message(SalaryStates.waiting_for_day)
async def process_salary_day(message: Message, state: FSMContext):
    try:
        day = int(message.text)
        if day < 1 or day > 31:
            raise ValueError
        
        data = await state.get_data()
        
        cursor.execute('''
            INSERT INTO incomes (user_id, source, amount, day_of_month)
            VALUES (?, ?, ?, ?)
        ''', (message.from_user.id, data['source'], data['amount'], day))
        conn.commit()
        
        today = date.today()
        calculate_monthly_budget(message.from_user.id, today.year, today.month)
        
        await message.answer(
            f"✅ *Доход добавлен!*\n\n"
            f"Источник: {data['source']}\n"
            f"Сумма: {data['amount']:,.2f} руб.\n"
            f"День: {day}-го числа",
            parse_mode="Markdown"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Введи число от 1 до 31!")

# ---------- МОИ ДОХОДЫ ----------
@dp.message(Command("my_salary"))
async def show_salaries(message: Message):
    incomes = get_user_incomes(message.from_user.id)
    
    if not incomes:
        await message.answer(
            "📭 У тебя пока нет настроенных доходов.\n"
            "Используй `/salary`, чтобы добавить."
        )
        return
    
    text = "💵 *Твои доходы:*\n\n"
    for inc in incomes:
        text += f"• {inc[1]}: *{inc[2]:,.2f} руб.* (число {inc[3]})\n"
    
    total = sum([inc[2] for inc in incomes])
    text += f"\n📊 Общий доход: *{total:,.2f} руб.*"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- ПРОГНОЗ ПЛАТЕЖЕЙ ----------
@dp.message(Command("forecast"))
async def payment_forecast(message: Message):
    user_id = message.from_user.id
    
    incomes = get_user_incomes(user_id)
    
    if not incomes:
        await message.answer(
            "❌ Сначала настрой доходы командой `/salary`"
        )
        return
    
    credits = get_user_credits(user_id)
    
    if not credits:
        await message.answer("📭 У тебя нет активных кредитов.")
        return
    
    today = date.today()
    current_month = today.month
    current_year = today.year
    
    if current_month == 12:
        days_in_month = 31
    else:
        days_in_month = (date(current_year, current_month + 1, 1) - 
                        date(current_year, current_month, 1)).days
    
    total_monthly_income = sum([inc[2] for inc in incomes])
    total_payments = 0
    payment_details = []
    
    for credit in credits:
        credit_id, name, creditor, total, remaining, rate, monthly_payment, status, start, term = credit
        
        start_date = date(current_year, current_month, 1).isoformat()
        end_date = date(current_year, current_month, days_in_month).isoformat()
        
        cursor.execute('''
            SELECT due_date, planned_amount, payment_number
            FROM schedules
            WHERE credit_id = ? AND due_date BETWEEN ? AND ?
            AND status = 'pending'
            ORDER BY due_date
        ''', (credit_id, start_date, end_date))
        payments = cursor.fetchall()
        
        for pay_date, pay_amount, pay_num in payments:
            pay_day = datetime.strptime(pay_date, '%Y-%m-%d').day
            total_payments += pay_amount
            
            income_before = 0
            for inc_amount, inc_day in [(i[2], i[3]) for i in incomes]:
                if inc_day <= pay_day:
                    income_before += inc_amount
            
            total_previous_payments = sum([
                p['amount'] for p in payment_details 
                if datetime.strptime(p['date'], '%Y-%m-%d').day <= pay_day
            ])
            
            available_money = income_before - total_previous_payments
            warning = "🔴 НЕ ХВАТИТ!" if available_money < pay_amount else "✅ ОК"
            
            payment_details.append({
                'date': pay_date,
                'amount': pay_amount,
                'name': name,
                'warning': warning,
                'available': available_money
            })
    
    text = f"📊 *Прогноз платежей на {months_ru[current_month-1]} {current_year}*\n\n"
    text += f"💰 Общий доход в месяце: *{total_monthly_income:,.2f} руб.*\n"
    text += f"💳 Обязательные платежи: *{total_payments:,.2f} руб.*\n"
    
    free_money = total_monthly_income - total_payments
    if free_money >= 0:
        text += f"✅ Свободно: *{free_money:,.2f} руб.*\n\n"
    else:
        text += f"🔴 *Дефицит: {abs(free_money):,.2f} руб.*\n\n"
    
    if payment_details:
        text += "📅 *Детали платежей:*\n"
        for pay in payment_details:
            text += f"• {pay['date']}: {pay['name']} — {pay['amount']:,.2f} руб. {pay['warning']}\n"
    
    text += "\n💡 *Рекомендации:*\n"
    
    if free_money < 0:
        text += "• Попробуй перенести платежи на даты после зарплаты\n"
        text += "• Рассмотри рефинансирование или реструктуризацию\n"
    elif free_money > total_monthly_income * 0.3:
        text += "• Отлично! Можно направить излишек на досрочное погашение\n"
    elif free_money > 0:
        text += "• Платежи сбалансированы, но бюджет не слишком свободный\n"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- ОПТИМИЗАЦИЯ ПЛАТЕЖЕЙ ----------
@dp.message(Command("optimize"))
async def optimize_payments(message: Message):
    user_id = message.from_user.id
    
    cursor.execute('''
        SELECT day_of_month FROM incomes 
        WHERE user_id = ? AND is_active = 1
        ORDER BY day_of_month
    ''', (user_id,))
    salary_days = [row[0] for row in cursor.fetchall()]
    
    if not salary_days:
        await message.answer("❌ Сначала настрой доходы через /salary")
        return
    
    cursor.execute('''
        SELECT s.schedule_id, s.credit_id, s.due_date, s.planned_amount, c.name, c.creditor
        FROM schedules s
        JOIN credits c ON s.credit_id = c.credit_id
        WHERE c.user_id = ? AND s.status = 'pending'
        ORDER BY s.due_date
    ''', (user_id,))
    payments = cursor.fetchall()
    
    if not payments:
        await message.answer("📭 Нет запланированных платежей.")
        return
    
    text = "🔄 *Анализ оптимальности платежей*\n\n"
    moved_count = 0
    recommendations = []
    
    for payment in payments:
        schedule_id, credit_id, due_date, amount, name, creditor = payment
        due_day = datetime.strptime(due_date, '%Y-%m-%d').day
        
        closest_salary = None
        for salary_day in salary_days:
            if salary_day <= due_day:
                closest_salary = salary_day
        
        if due_day > 1 and (closest_salary is None or closest_salary < due_day - 3):
            recommendations.append({
                'name': name,
                'due_date': due_date,
                'amount': amount,
                'current_day': due_day,
                'salary_day': salary_days[0] if salary_days else None,
                'recommendation': 'перенести на день зарплаты' if salary_days else 'нет зарплаты до платежа'
            })
            moved_count += 1
    
    if moved_count == 0:
        text += "✅ Все платежи уже оптимально расположены относительно дат зарплат!"
    else:
        text += f"📌 Найдено платежей для оптимизации: *{moved_count}*\n\n"
        for rec in recommendations:
            text += f"• *{rec['name']}*: {rec['due_date']} ({rec['amount']:,.2f} руб.)\n"
            if rec['salary_day']:
                text += f"  → Рекомендуется перенести на {rec['salary_day']}-е число\n"
            text += "\n"
        
        text += "💡 *Как оптимизировать:*\n"
        text += "• Используй команду `/pay` для досрочного погашения\n"
        text += "• Свяжись с кредитором для изменения даты платежа\n"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- ДАШБОРД ----------
@dp.message(Command("dashboard"))
async def dashboard(message: Message):
    user_id = message.from_user.id
    
    cursor.execute('''
        SELECT 
            COUNT(*) as total_credits,
            SUM(total_amount) as total_borrowed,
            SUM(remaining_debt) as total_remaining,
            AVG(interest_rate) as avg_rate
        FROM credits 
        WHERE user_id = ? AND status = 'active'
    ''', (user_id,))
    stats = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*) FROM credits WHERE user_id = ? AND status = 'closed'", (user_id,))
    closed = cursor.fetchone()[0]
    
    cursor.execute('''
        SELECT SUM(interest_amount) 
        FROM schedules s
        JOIN credits c ON s.credit_id = c.credit_id
        WHERE c.user_id = ? AND s.status = 'pending'
    ''', (user_id,))
    overpayment = cursor.fetchone()[0] or 0
    
    incomes = get_user_incomes(user_id)
    total_income = sum([inc[2] for inc in incomes])
    
    today = date.today()
    income, payments, free = calculate_monthly_budget(user_id, today.year, today.month)
    
    text = f"📊 *Финансовый дашборд*\n\n"
    text += f"📌 Активных кредитов: *{stats[0] or 0}*\n"
    text += f"✅ Закрыто кредитов: *{closed}*\n"
    text += f"💰 Всего взято: *{stats[1] or 0:,.2f} руб.*\n"
    text += f"💳 Остаток долга: *{stats[2] or 0:,.2f} руб.*\n"
    text += f"📈 Средняя ставка: *{stats[3] or 0:.1f}%*\n"
    text += f"💸 Предстоящая переплата: *{overpayment:,.2f} руб.*\n\n"
    text += f"💵 Ежемесячный доход: *{total_income:,.2f} руб.*\n"
    text += f"📊 Бюджет на {months_ru[today.month-1]}:\n"
    text += f"   Свободно: *{free:,.2f} руб.*\n"
    
    if total_income > 0:
        debt_to_income = (stats[2] or 0) / total_income * 100
        text += f"📊 Соотношение долг/доход: *{debt_to_income:.1f}%*\n"
        if debt_to_income > 40:
            text += "⚠️ *Высокая долговая нагрузка!*"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- ИСТОРИЯ ПЛАТЕЖЕЙ ----------
@dp.message(Command("history"))
async def history(message: Message):
    user_id = message.from_user.id
    
    cursor.execute('''
        SELECT p.payment_date, p.amount, c.name, c.creditor
        FROM payments p
        JOIN credits c ON p.credit_id = c.credit_id
        WHERE c.user_id = ?
        ORDER BY p.payment_date DESC
        LIMIT 20
    ''', (user_id,))
    payments = cursor.fetchall()
    
    if not payments:
        await message.answer("📭 История платежей пуста.")
        return
    
    text = "📜 *История последних платежей:*\n\n"
    for pay in payments:
        text += f"📅 {pay[0]}: *{pay[2]}* — {pay[1]:,.2f} руб.\n"
    
    await message.answer(text, parse_mode="Markdown")

# ==================== ЗАПУСК БОТА ====================
async def main():
    print("🚀 Бот-планировщик кредитов запущен!")
    print("📊 Поддерживаемые команды:")
    print("  /start - Главное меню")
    print("  /add - Добавить кредит")
    print("  /list - Список кредитов")
    print("  /pay - Внести платеж")
    print("  /next - Ближайшие платежи")
    print("  /forecast - Прогноз с зарплатой")
    print("  /salary - Настроить зарплату")
    print("  /my_salary - Мои доходы")
    print("  /dashboard - Статистика")
    print("  /optimize - Оптимизация платежей")
    print("  /history - История платежей")
    print("  /close_credit - Закрыть кредит")
    print("  /help - Помощь")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
