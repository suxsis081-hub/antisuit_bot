import asyncio, base64, hashlib, hmac, json, os, sqlite3, time
from urllib.parse import parse_qsl

from aiohttp import web, ClientSession, FormData
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           Message, PreCheckoutQuery, WebAppInfo)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]          # https://твой-домен
SD_API_KEY = os.environ["SD_API_KEY"]
PORT = int(os.getenv("PORT", 8080))

GEN_COST = 30                                  # поцелуйчиков за 1 генерацию
START_BONUS = 30                               # подарок новичку (поставь 0, если не нужен)
PACKS = {120: 50, 360: 150, 720: 300}          # поцелуйчики -> звёзды (120 = 50 ⭐)
SD_URL = "https://api.stability.ai/v2beta/stable-image/generate/core"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- база ----------
db = sqlite3.connect("kisses.db", isolation_level=None)
db.execute("create table if not exists users(id integer primary key, balance integer not null)")
db.execute("create table if not exists payments(charge_id text primary key)")

def balance(uid):
    row = db.execute("select balance from users where id=?", (uid,)).fetchone()
    if row is None:
        db.execute("insert into users values(?,?)", (uid, START_BONUS))
        return START_BONUS
    return row[0]

def spend(uid, n):
    balance(uid)
    return db.execute("update users set balance=balance-? where id=? and balance>=?",
                      (n, uid, n)).rowcount == 1

def add(uid, n):
    balance(uid)
    db.execute("update users set balance=balance+? where id=?", (n, uid))

# ---------- проверка подписи Telegram ----------
def user_from_init_data(init_data: str):
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        return None
    return json.loads(data["user"])["id"]

def auth(request):
    uid = user_from_init_data(request.headers.get("X-Init-Data", ""))
    if uid is None:
        raise web.HTTPUnauthorized(text="bad auth")
    return uid

# ---------- генерация ----------
async def sd_generate(prompt: str) -> bytes:
    form = FormData(default_to_multipart=True)
    form.add_field("prompt", prompt)
    form.add_field("output_format", "jpeg")
    async with ClientSession() as s:
        async with s.post(SD_URL, data=form, timeout=120,
                          headers={"Authorization": f"Bearer {SD_API_KEY}",
                                   "Accept": "image/*"}) as r:
            if r.status != 200:
                raise RuntimeError(await r.text())
            return await r.read()

# ---------- API ----------
async def api_me(request):
    uid = auth(request)
    return web.json_response({"balance": balance(uid), "cost": GEN_COST,
                              "packs": [{"kisses": k, "stars": v} for k, v in PACKS.items()]})

async def api_generate(request):
    uid = auth(request)
    prompt = ((await request.json()).get("prompt") or "").strip()[:1000]
    if not prompt:
        return web.json_response({"error": "Напиши, что хочется увидеть"}, status=400)
    if not spend(uid, GEN_COST):
        return web.json_response({"error": "Не хватает поцелуйчиков", "balance": balance(uid)}, status=402)
    try:
        img = await sd_generate(prompt)
    except Exception as e:
        add(uid, GEN_COST)                      # вернуть, если не получилось
        print("SD error:", e)
        return web.json_response({"error": "Не получилось. Поцелуйчики вернулись", "balance": balance(uid)}, status=500)
    return web.json_response({"image": "data:image/jpeg;base64," + base64.b64encode(img).decode(),
                              "balance": balance(uid)})

async def api_invoice(request):
    uid = auth(request)
    kisses = int((await request.json()).get("kisses", 0))
    if kisses not in PACKS:
        return web.json_response({"error": "bad pack"}, status=400)
    link = await bot.create_invoice_link(
        title=f"{kisses} поцелуйчиков", description="Пополнение баланса",
        payload=f"{uid}:{kisses}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label=f"{kisses} 💋", amount=PACKS[kisses])])
    return web.json_response({"link": link})

async def index(request):
    return web.FileResponse("static/index.html")

# ---------- бот ----------
@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть 💋", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Здесь тихо и уютно. Загляни внутрь — расскажи, что хочешь увидеть.", reply_markup=kb)

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(lambda m: m.successful_payment)
async def paid(m: Message):
    p = m.successful_payment
    uid, kisses = map(int, p.invoice_payload.split(":"))
    if uid != m.from_user.id or kisses not in PACKS:
        return
    try:
        db.execute("insert into payments values(?)", (p.telegram_payment_charge_id,))
    except sqlite3.IntegrityError:
        return                                   # уже зачислено
    add(uid, kisses)
    await m.answer(f"Спасибо 💋 Зачислено: {kisses}. Баланс: {balance(uid)}")

# ---------- запуск ----------
async def main():
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/me", api_me),
                    web.post("/api/generate", api_generate),
                    web.post("/api/invoice", api_invoice)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
