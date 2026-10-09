import asyncio, logging, os, secrets
from datetime import datetime, timedelta, timezone

import aiosqlite, httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton as B

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
PANEL = os.environ["PANEL_URL"].rstrip("/")
P_USER, P_PASS = os.environ["PANEL_USERNAME"], os.environ["PANEL_PASSWORD"]
GROUP_IDS = [int(x) for x in os.environ.get("GROUP_IDS", "").split(",") if x.strip()]
DB = "bot.db"
router = Router()


# ---------------- Panel (PasarGuard) ----------------
class Panel:
    def __init__(self):
        self.token = None
        self.http = httpx.AsyncClient(base_url=PANEL, timeout=30)

    async def login(self):
        r = await self.http.post("/api/admin/token", data={"username": P_USER, "password": P_PASS})
        r.raise_for_status()
        self.token = r.json()["access_token"]

    async def req(self, method, url, **kw):
        if not self.token:
            await self.login()
        for _ in range(2):
            r = await self.http.request(method, url, headers={"Authorization": f"Bearer {self.token}"}, **kw)
            if r.status_code == 401:
                await self.login()
                continue
            r.raise_for_status()
            return r.json()

    async def create_user(self, username, gb, days):
        expire = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        data = await self.req("POST", "/api/user", json={
            "username": username, "status": "active",
            "data_limit": int(gb * 1024**3), "data_limit_reset_strategy": "no_reset",
            "expire": expire, "group_ids": GROUP_IDS, "proxy_settings": {},
        })
        sub = data.get("subscription_url", "")
        return PANEL + sub if sub.startswith("/") else sub


panel = Panel()


# ---------------- DB ----------------
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER,
            gb REAL, days INTEGER, users INTEGER, price INTEGER, status TEXT, receipt TEXT);
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER,
            order_id INTEGER, username TEXT, link TEXT, gb REAL, days INTEGER);
        """)
        for k, v in {"price_gb": "0", "price_day": "0", "price_user": "0", "card": "تنظیم نشده"}.items():
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
        await db.commit()


async def get(k):
    async with aiosqlite.connect(DB) as db:
        return (await (await db.execute("SELECT v FROM settings WHERE k=?", (k,))).fetchone())[0]


async def put(k, v):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE settings SET v=? WHERE k=?", (str(v), k))
        await db.commit()


async def q(sql, *a, one=False, commit=False):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, a)
        if commit:
            await db.commit()
            return cur.lastrowid
        rows = await cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def fmt(n):
    return f"{int(n):,}"


async def price(gb, days, users):
    pg, pd, pu = [float(await get(k)) for k in ("price_gb", "price_day", "price_user")]
    return int(users * (gb * pg + days * pd + pu))


# ---------------- States ----------------
class Buy(StatesGroup):
    gb = State(); days = State(); users = State(); receipt = State()


class Adm(StatesGroup):
    value = State()


STEPS = {
    "gb": ("حجم (گیگابایت)", [10, 20, 50, 100], "days"),
    "days": ("مدت (روز)", [30, 60, 90, 180], "users"),
    "users": ("تعداد کاربر", [1, 2, 3, 5], None),
}
LABEL = {"gb": "گیگ", "days": "روز", "users": "کاربر"}


def main_menu(uid):
    rows = [[B(text="🛒 خرید کانفیگ", callback_data="buy")],
            [B(text="📦 سرویس‌های من", callback_data="my")],
            [B(text="💰 تعرفه‌ها", callback_data="prices")]]
    if uid in ADMINS:
        rows.append([B(text="⚙️ پنل مدیریت", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("سلام 👋\nبه ربات فروش کانفیگ خوش اومدی.", reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("منوی اصلی:", reply_markup=main_menu(c.from_user.id))


@router.callback_query(F.data == "prices")
async def prices(c: CallbackQuery):
    pg, pd, pu = await get("price_gb"), await get("price_day"), await get("price_user")
    await c.message.edit_text(
        f"💰 تعرفه‌ها\n\nهر گیگ: {fmt(float(pg))} تومان\nهر روز: {fmt(float(pd))} تومان\n"
        f"هر کاربر: {fmt(float(pu))} تومان\n\nقیمت هر کاربر = (گیگ × قیمت گیگ) + (روز × قیمت روز) + هزینه کاربر",
        reply_markup=kb([B(text="🔙 بازگشت", callback_data="home")]))


# ---------------- Buy flow ----------------
async def ask(target, step, state):
    title, presets, _ = STEPS[step]
    await state.set_state(getattr(Buy, step))
    rows = [[B(text=f"{p} {LABEL[step]}", callback_data=f"v:{step}:{p}") for p in presets[i:i + 2]]
            for i in range(0, len(presets), 2)]
    rows.append([B(text="✏️ مقدار دلخواه", callback_data=f"c:{step}")])
    rows.append([B(text="❌ انصراف", callback_data="home")])
    text = f"{title} را انتخاب کنید:"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await target.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "buy")
async def buy(c: CallbackQuery, state: FSMContext):
    if float(await get("price_gb")) == 0 and float(await get("price_day")) == 0:
        return await c.answer("هنوز قیمت‌گذاری انجام نشده.", show_alert=True)
    await state.clear()
    await ask(c, "gb", state)


async def advance(target, state, step, val):
    await state.update_data(**{step: val})
    nxt = STEPS[step][2]
    if nxt:
        return await ask(target, nxt, state)
    d = await state.get_data()
    total = await price(d["gb"], d["days"], d["users"])
    await state.update_data(price=total)
    text = (f"🧾 خلاصه سفارش\n\nحجم: {d['gb']} گیگ\nمدت: {d['days']} روز\nتعداد کاربر: {d['users']}\n"
            f"💵 مبلغ قابل پرداخت: {fmt(total)} تومان")
    markup = kb([B(text="✅ تایید و پرداخت", callback_data="confirm")], [B(text="❌ انصراف", callback_data="home")])
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("v:"))
async def preset(c: CallbackQuery, state: FSMContext):
    _, step, val = c.data.split(":")
    await advance(c, state, step, float(val) if step == "gb" else int(val))


@router.callback_query(F.data.startswith("c:"))
async def custom(c: CallbackQuery, state: FSMContext):
    step = c.data.split(":")[1]
    await c.message.edit_text(f"عدد {LABEL[step]} را بفرستید:", reply_markup=kb([B(text="❌ انصراف", callback_data="home")]))


async def typed(m: Message, state: FSMContext, step):
    try:
        v = float(m.text.replace(",", "."))
        assert 0 < v <= 100000
    except Exception:
        return await m.answer("عدد معتبر بفرستید.")
    await advance(m, state, step, v if step == "gb" else int(v))


@router.message(Buy.gb)
async def t1(m: Message, state: FSMContext): await typed(m, state, "gb")


@router.message(Buy.days)
async def t2(m: Message, state: FSMContext): await typed(m, state, "days")


@router.message(Buy.users)
async def t3(m: Message, state: FSMContext): await typed(m, state, "users")


@router.callback_query(F.data == "confirm")
async def confirm(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "price" not in d:
        return await c.answer("سفارش منقضی شد.", show_alert=True)
    oid = await q("INSERT INTO orders(tg_id,gb,days,users,price,status) VALUES(?,?,?,?,?,'awaiting')",
                  c.from_user.id, d["gb"], d["days"], d["users"], d["price"], commit=True)
    await state.update_data(order_id=oid)
    await state.set_state(Buy.receipt)
    card = await get("card")
    await c.message.edit_text(
        f"سفارش #{oid}\n💵 مبلغ: {fmt(d['price'])} تومان\n\n💳 شماره کارت:\n{card}\n\n"
        "بعد از واریز، «عکس رسید» را همین‌جا بفرستید.",
        reply_markup=kb([B(text="❌ انصراف", callback_data="home")]))


@router.message(Buy.receipt, F.photo)
async def receipt(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    oid = d["order_id"]
    await q("UPDATE orders SET status='pending', receipt=? WHERE id=?", m.photo[-1].file_id, oid, commit=True)
    await state.clear()
    await m.answer("✅ رسید ثبت شد؛ بعد از تایید مدیر کانفیگ برایتان ارسال می‌شود.",
                   reply_markup=kb([B(text="🏠 منوی اصلی", callback_data="home")]))
    for a in ADMINS:
        await bot.send_photo(
            a, m.photo[-1].file_id,
            caption=f"سفارش #{oid}\nکاربر: {m.from_user.full_name} ({m.from_user.id})\n"
                    f"{d['gb']} گیگ / {d['days']} روز / {d['users']} کاربر\nمبلغ: {fmt(d['price'])} تومان",
            reply_markup=kb([B(text="✅ تایید", callback_data=f"ok:{oid}"), B(text="❌ رد", callback_data=f"no:{oid}")]))


@router.message(Buy.receipt)
async def receipt_bad(m: Message):
    await m.answer("لطفاً عکس رسید را بفرستید.")


# ---------------- Admin approval ----------------
@router.callback_query(F.data.startswith(("ok:", "no:")), F.from_user.id.in_(ADMINS))
async def decide(c: CallbackQuery, bot: Bot):
    act, oid = c.data.split(":")
    row = await q("SELECT tg_id,gb,days,users,status FROM orders WHERE id=?", int(oid), one=True)
    if not row or row[4] != "pending":
        return await c.answer("این سفارش قبلاً بررسی شده.", show_alert=True)
    tg, gb, days, users, _ = row
    if act == "no":
        await q("UPDATE orders SET status='rejected' WHERE id=?", int(oid), commit=True)
        await bot.send_message(tg, f"❌ سفارش #{oid} رد شد. با پشتیبانی تماس بگیرید.")
        return await c.message.edit_caption(caption=c.message.caption + "\n\n❌ رد شد")
    await q("UPDATE orders SET status='processing' WHERE id=?", int(oid), commit=True)
    links = []
    try:
        for _ in range(users):
            name = f"u{tg}_{secrets.token_hex(3)}"
            link = await panel.create_user(name, gb, days)
            await q("INSERT INTO services(tg_id,order_id,username,link,gb,days) VALUES(?,?,?,?,?,?)",
                    tg, int(oid), name, link, gb, days, commit=True)
            links.append(link)
    except Exception as e:
        logging.exception("panel error")
        await q("UPDATE orders SET status='pending' WHERE id=?", int(oid), commit=True)
        return await c.answer(f"خطا در ساخت کانفیگ: {e}", show_alert=True)
    await q("UPDATE orders SET status='done' WHERE id=?", int(oid), commit=True)
    await bot.send_message(tg, f"✅ سفارش #{oid} تایید شد.\n\n" + "\n\n".join(f"🔗 {l}" for l in links),
                           reply_markup=kb([B(text="📦 سرویس‌های من", callback_data="my")]))
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ تایید و ساخته شد")


# ---------------- My services ----------------
@router.callback_query(F.data == "my")
async def my(c: CallbackQuery):
    rows = await q("SELECT id,username,gb,days FROM services WHERE tg_id=? ORDER BY id DESC LIMIT 20", c.from_user.id)
    btns = [[B(text=f"{r[1]} | {r[2]:g}GB/{r[3]}روز", callback_data=f"s:{r[0]}")] for r in rows]
    btns.append([B(text="🔙 بازگشت", callback_data="home")])
    await c.message.edit_text("📦 سرویس‌های شما:" if rows else "سرویسی ندارید.",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


@router.callback_query(F.data.startswith("s:"))
async def svc(c: CallbackQuery):
    r = await q("SELECT username,link FROM services WHERE id=? AND tg_id=?", int(c.data[2:]), c.from_user.id, one=True)
    if r:
        await c.message.edit_text(f"👤 {r[0]}\n\n🔗 {r[1]}", reply_markup=kb([B(text="🔙 بازگشت", callback_data="my")]))


# ---------------- Admin panel ----------------
SETTINGS = {"price_gb": "قیمت هر گیگ (تومان)", "price_day": "قیمت هر روز (تومان)",
            "price_user": "هزینه هر کاربر (تومان)", "card": "شماره کارت"}


@router.callback_query(F.data == "admin", F.from_user.id.in_(ADMINS))
async def admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[B(text=f"✏️ {t}: {await get(k)}", callback_data=f"set:{k}")] for k, t in SETTINGS.items()]
    rows.append([B(text="🔙 بازگشت", callback_data="home")])
    await c.message.edit_text("⚙️ پنل مدیریت — برای تغییر روی هر مورد بزنید:",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("set:"), F.from_user.id.in_(ADMINS))
async def set_ask(c: CallbackQuery, state: FSMContext):
    k = c.data[4:]
    await state.set_state(Adm.value)
    await state.update_data(key=k)
    await c.message.edit_text(f"مقدار جدید «{SETTINGS[k]}» را بفرستید:", reply_markup=kb([B(text="❌ انصراف", callback_data="admin")]))


@router.message(Adm.value, F.from_user.id.in_(ADMINS))
async def set_val(m: Message, state: FSMContext):
    k = (await state.get_data())["key"]
    v = m.text.strip()
    if k != "card":
        try:
            float(v)
        except ValueError:
            return await m.answer("عدد معتبر بفرستید.")
    await put(k, v)
    await state.clear()
    await m.answer("✅ ذخیره شد.", reply_markup=kb([B(text="⚙️ پنل مدیریت", callback_data="admin")]))


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
